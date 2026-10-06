import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading

import paramiko
import pytest

from test_harbour import client
from harbour import app as module, remote_install_key, ssh, store


@pytest.fixture
def install_host(tmp_path):
    home = tmp_path / 'remote-home'
    home.mkdir()
    host_key = paramiko.ECDSAKey.generate()
    password = '  disposable SSH password £  '
    requests, failures, workers, channels = [], [], [], []
    stop = threading.Event()

    class Host(paramiko.ServerInterface):
        def check_auth_password(self, username, supplied):
            requests.append(('password', username))
            return paramiko.AUTH_SUCCESSFUL if username == 'harbour' and supplied == password else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            requests.append(('key', username))
            path = home / '.ssh' / 'authorized_keys'
            return paramiko.AUTH_SUCCESSFUL if username == 'harbour' and path.exists() and key.get_base64() in path.read_text() else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return 'password,publickey'

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            channels.append(channel)
            def execute():
                try:
                    if command == b'true':
                        channel.send_exit_status(0)
                    else:
                        assert command == ssh.REMOTE_PYTHON.encode()
                        channel.settimeout(5)
                        source = bytearray()
                        while chunk := channel.recv(65536):
                            source.extend(chunk)
                            assert len(source) < 50_000
                        assert source.startswith(Path(remote_install_key.__file__).read_bytes())
                        result = subprocess.run([sys.executable, '-'], input=source, capture_output=True,
                                                env={**os.environ, 'HOME': str(home)}, cwd=home, timeout=5)
                        if result.stdout:
                            channel.sendall(result.stdout)
                        if result.stderr:
                            channel.sendall_stderr(result.stderr)
                        channel.send_exit_status(result.returncode)
                except Exception as exc:
                    failures.append(exc)
                    channel.send_exit_status(1)
                finally:
                    channel.shutdown_write()
            worker = threading.Thread(target=execute, daemon=True)
            workers.append(worker)
            worker.start()
            return True

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(.2)
        def serve():
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with paramiko.Transport(connection) as transport:
                    transport.add_server_key(host_key)
                    try:
                        transport.start_server(server=Host())
                        while transport.is_active() and not stop.wait(.01):
                            pass
                    except (EOFError, paramiko.SSHException):
                        pass
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield {'home': home, 'password': password, 'requests': requests,
                   'target': {'host': '127.0.0.1', 'port': listener.getsockname()[1],
                              'username': 'harbour', 'fingerprint': ssh.host_fingerprint(host_key)}}
        finally:
            stop.set()
            thread.join(timeout=3)
            for worker in workers:
                worker.join(timeout=6)
            assert not thread.is_alive() and not any(worker.is_alive() for worker in workers)
            assert not failures


def test_install_uses_pinned_password_connection_then_verifies_key_without_saving_password(client, monkeypatch, install_host):
    monkeypatch.setattr(store, 'DEMO', False)
    key = client.post('/api/keys', json={}).json()
    host = install_host
    body = {**host['target'], 'key_id': key['id'], 'password': host['password']}
    first = client.post('/api/ssh/install-key', json=body)
    assert first.status_code == 200, first.text
    assert first.json() == {'status': 'installed', 'verified': True}
    path = host['home'] / '.ssh' / 'authorized_keys'
    assert path.read_text() == 'restrict ' + key['public_key'] + '\n'
    assert host['requests'] == [('password', 'harbour'), ('key', 'harbour')]
    assert client.post('/api/ssh/install-key', json=body).json()['status'] == 'present'
    assert len(path.read_text().splitlines()) == 1
    with store.db() as con:
        assert host['password'] not in '\n'.join(con.iterdump())
    assert host['password'] not in first.text
    before = len(host['requests'])
    mismatch = {**body, 'fingerprint': 'SHA256:' + 'x' * 43}
    result = client.post('/api/ssh/install-key', json=mismatch)
    assert result.status_code == 400 and 'fingerprint mismatch' in result.text
    assert len(host['requests']) == before  # Host identity is checked before a password is sent.
    result = client.post('/api/ssh/install-key', json={**body, 'password': 'wrong-password'})
    assert result.status_code == 400 and 'password sign-in failed' in result.text
    assert 'wrong-password' not in result.text
    with ssh.connect(host['target'], password=host['password']) as connection:
        assert connection.get_transport().is_authenticated()


def test_remote_key_install_preserves_existing_keys_and_refuses_links(tmp_path):
    public = 'ssh-ed25519 ' + 'a' * 64
    folder = tmp_path / '.ssh'
    folder.mkdir(mode=0o755)
    path = folder / 'authorized_keys'
    original = b'# existing keys\nrestrict ssh-ed25519 bbbbb old-key'
    path.write_bytes(original)
    assert remote_install_key.install_public_key(public, tmp_path) == {'status': 'installed'}
    expected = original + ('\nrestrict ' + public + ' harbour\n').encode()
    assert path.read_bytes() == expected
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert remote_install_key.install_public_key(public, tmp_path) == {'status': 'present'}
    assert path.read_bytes() == expected
    path.unlink()
    outside = tmp_path / 'unrelated'
    outside.write_text('keep me')
    path.symlink_to(outside)
    with pytest.raises(OSError):
        remote_install_key.install_public_key(public, tmp_path)
    assert outside.read_text() == 'keep me'
    path.unlink()
    os.link(outside, path)
    with pytest.raises(ValueError, match='Unsafe'):
        remote_install_key.install_public_key(public, tmp_path)
    assert outside.read_text() == 'keep me'
    path.unlink()
    folder.rmdir()
    folder.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        remote_install_key.install_public_key(public, tmp_path)


def test_authentication_switching_rotation_and_repair_keep_server_history(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(module, 'queue_job', lambda *a, **kw: {'id': 'test-job'})
    original = store.one("SELECT * FROM servers WHERE id='atlas'")
    before = store.rows("SELECT * FROM resource_history WHERE server_id='atlas'")
    target = {k: original[k] for k in ('name', 'host', 'port', 'username', 'fingerprint')}
    target['fingerprint'] = 'SHA256:' + 'a' * 43
    first = client.post('/api/keys', json={}).json()
    second = client.post('/api/keys', json={}).json()
    assert client.put('/api/servers/atlas/connection', json={**target, 'key_id': first['id']}).status_code == 200
    store.execute("UPDATE servers SET key_id=?,error='Authentication failed' WHERE id='backup'", (first['id'],))
    assert client.put('/api/servers/atlas/connection', json={**target, 'key_id': second['id']}).status_code == 200
    assert store.one('SELECT 1 FROM ssh_keys WHERE id=?', (first['id'],))  # Shared keys survive rotation.
    secret = '  saved password with spaces  '
    password_body = {**target, 'auth_method': 'password', 'password': secret}
    assert client.put('/api/servers/atlas/connection', json=password_body).status_code == 400
    password_body['password_auth_confirmed'] = True
    assert client.put('/api/servers/atlas/connection', json=password_body).status_code == 200
    saved = store.one("SELECT * FROM servers WHERE id='atlas'")
    assert saved['auth_method'] == 'password' and saved['key_id'] is None
    assert saved['password_encrypted'] != secret and store.cipher().decrypt(saved['password_encrypted'].encode()).decode() == secret
    assert not store.one('SELECT 1 FROM ssh_keys WHERE id=?', (second['id'],))
    assert saved['snapshot'] == original['snapshot'] and saved['thresholds'] == original['thresholds']
    assert saved['monitoring_enabled'] == original['monitoring_enabled'] and saved['poll_seconds'] == original['poll_seconds']
    assert store.rows("SELECT * FROM resource_history WHERE server_id='atlas'") == before
    public = client.get('/api/servers/atlas/connection')
    assert public.json()['has_password'] and public.json()['key'] is None
    assert secret not in public.text and saved['password_encrypted'] not in public.text
    assert secret not in client.get('/api/dashboard').text
    retained = {k: v for k, v in password_body.items() if k != 'password'}
    assert client.put('/api/servers/atlas/connection', json=retained).status_code == 200
    assert client.put('/api/servers/atlas/connection', json={**retained, 'username': 'different'}).status_code == 400
    assert client.put('/api/servers/atlas/connection', json={**target, 'key_id': first['id']}).status_code == 200
    saved = store.one("SELECT * FROM servers WHERE id='atlas'")
    assert saved['auth_method'] == 'key' and saved['password_encrypted'] is None
    with store.db() as con:
        assert secret not in '\n'.join(con.iterdump())


def test_password_creation_and_monitoring_select_the_encrypted_credential(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(module, 'queue_job', lambda *a, **kw: {'id': 'test-job'})
    password = '  remote login password  '
    target = {'name': 'Password host', 'host': 'example.test', 'port': 22, 'username': 'harbour',
              'fingerprint': 'SHA256:' + 'a' * 43, 'auth_method': 'password', 'password': password,
              'password_auth_confirmed': True}
    added = client.post('/api/servers', json=target)
    assert added.status_code == 200, added.text
    server = store.one('SELECT * FROM servers WHERE id=?', (added.json()['id'],))
    def connect(server, **credential):
        assert credential == {'password': password}
        raise paramiko.AuthenticationException(password)
    monkeypatch.setattr(ssh, 'connect', connect)
    with pytest.raises(ssh.ProbeError) as error:
        ssh.request(server, {})
    assert password not in str(error.value) and 'password sign-in failed' in str(error.value)


def test_connection_endpoints_enforce_permissions_validation_and_busy_lock(client, monkeypatch):
    body = {'host': 'example.test', 'username': 'harbour', 'fingerprint': 'SHA256:' + 'a' * 43,
            'key_id': 'missing', 'password': 'never-echo-this-password'}
    def forbidden(*a, **kw):
        pytest.fail('No SSH calls allowed')
    monkeypatch.setattr(ssh, 'install_key', forbidden)
    assert client.post('/api/ssh/install-key', json=body).status_code == 400  # Demo guard.
    monkeypatch.setattr(store, 'DEMO', False)
    assert client.post('/api/ssh/install-key', json=body).status_code == 400  # Missing key.
    invalid = client.post('/api/ssh/install-key', json={**body, 'password': ['never-echo-this-password']})
    assert invalid.status_code == 422 and body['password'] not in invalid.text
    key = client.post('/api/keys', json={}).json()
    body['key_id'] = key['id']
    for _ in range(4):
        assert module.key_installations.acquire(blocking=False)
    try:
        assert client.post('/api/ssh/install-key', json=body).status_code == 429
    finally:
        for _ in range(4): module.key_installations.release()
    connection = {k: v for k, v in body.items() if k != 'password'} | {'name': 'Atlas'}
    lock = module.server_lock('atlas')
    lock.acquire()
    try:
        assert client.put('/api/servers/atlas/connection', json=connection).status_code == 409
    finally:
        lock.release()
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.post('/api/ssh/install-key', json=body).status_code == 403
    assert client.put('/api/servers/atlas/connection', json=connection).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-long', 'role': 'user'})
    login = client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-long'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    assert client.post('/api/ssh/install-key', json=body).status_code == 403
    assert client.put('/api/servers/atlas/connection', json=connection).status_code == 403
    assert client.get('/api/servers/atlas/connection').status_code == 403
    client.cookies.clear()
    assert client.post('/api/ssh/install-key', json=body).status_code == 401
    assert client.get('/api/servers/atlas/connection').status_code == 401


def test_connection_changes_invalidate_previous_docker_action_previews(client, monkeypatch):
    planned = client.post('/api/servers/atlas/plan', json={'action': 'restart', 'targets': ['group:immich']})
    assert planned.status_code == 200
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(module, 'queue_job', lambda *a, **kw: {'id': 'test-job'})
    changed = client.put('/api/servers/atlas/connection', json={
        'name': 'Atlas', 'host': 'new-host.test', 'username': 'harbour', 'fingerprint': 'SHA256:' + 'a' * 43,
        'auth_method': 'password', 'password_auth_confirmed': True, 'password': 'new-server-password'})
    assert changed.status_code == 200
    import time
    store.execute("UPDATE servers SET checked=?,connection_status='up' WHERE id='atlas'", (time.time(),))
    result = client.post('/api/servers/atlas/execute', json={'token': planned.json()['token']})
    assert result.status_code == 409 and 'connection changed' in result.text
