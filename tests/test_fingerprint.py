import base64
import hashlib
import socket
import threading
import time

import paramiko
import pytest

from test_harbour import client
from harbour import app as module, ssh, store


@pytest.fixture
def ssh_host():
    key = paramiko.ECDSAKey.generate()
    requests = []
    stop = threading.Event()
    class Host(paramiko.ServerInterface):
        def check_auth_none(self, username):
            requests.append('auth-none')
            return paramiko.AUTH_FAILED
        def check_auth_password(self, username, password):
            requests.append('password')
            return paramiko.AUTH_FAILED
        def check_auth_publickey(self, username, key):
            requests.append('public-key')
            return paramiko.AUTH_FAILED
        def check_channel_request(self, kind, channel_id):
            requests.append('channel')
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
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
                except OSError:
                    return
                with paramiko.Transport(connection) as transport:
                    transport.add_server_key(key)
                    transport.start_server(server=Host())
                    while transport.is_active() and not stop.wait(.01):
                        pass
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], key, requests
        finally:
            stop.set()
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_actual_handshake_reads_host_key_without_authentication(client, monkeypatch, ssh_host):
    monkeypatch.setattr(store, 'DEMO', False)
    port, key, requests = ssh_host
    before = len(store.rows('SELECT * FROM servers'))
    keys_before = len(store.rows('SELECT * FROM ssh_keys'))
    result = client.post('/api/ssh/fingerprint', json={'host': '127.0.0.1', 'port': port})
    assert result.status_code == 200
    data = result.json()
    expected = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
    assert data == {'host': '127.0.0.1', 'port': port, 'address': '127.0.0.1',
                    'key_type': 'ecdsa-sha2-nistp256', 'fingerprint': expected}
    assert requests == []  # No auth, channel, command, or credential use.
    assert len(store.rows('SELECT * FROM servers')) == before
    assert len(store.rows('SELECT * FROM ssh_keys')) == keys_before
    ssh.PinnedHostKey(data['fingerprint']).missing_host_key(None, '127.0.0.1', key)
    with pytest.raises(paramiko.SSHException, match='mismatch'):
        ssh.PinnedHostKey(data['fingerprint']).missing_host_key(None, '127.0.0.1', paramiko.ECDSAKey.generate())
    # Only onboarding stores the explicitly chosen pin alongside the SSH credential.
    credential = client.post('/api/keys', json={}).json()
    monkeypatch.setattr(module, 'queue_job', lambda *a, **kw: {'id': 'test-job'})
    added = client.post('/api/servers', json={'name': 'Test host', 'host': '127.0.0.1', 'port': port,
                       'username': 'harbour', 'fingerprint': data['fingerprint'], 'key_id': credential['id']})
    assert added.status_code == 200
    saved = store.one('SELECT * FROM servers WHERE id=?', (added.json()['id'],))
    assert saved['fingerprint'] == expected and saved['key_id'] == credential['id']


def test_fingerprint_probe_requires_admin_csrf_and_non_demo(client, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('Probe must not open a socket')
    monkeypatch.setattr(ssh, 'probe_host_key', forbidden)
    body = {'host': 'example.test', 'port': 22}
    assert client.post('/api/ssh/fingerprint', json=body).status_code == 400
    monkeypatch.setattr(store, 'DEMO', False)
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.post('/api/ssh/fingerprint', json=body).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    assert client.post('/api/users', json={'name': 'viewer-probe', 'password': 'viewer-test-password', 'role': 'user'}).status_code == 200
    login = client.post('/api/login', json={'name': 'viewer-probe', 'password': 'viewer-test-password'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    assert client.post('/api/ssh/fingerprint', json=body).status_code == 403
    client.cookies.clear()
    assert client.post('/api/ssh/fingerprint', json=body).status_code == 401


def test_probe_validates_target_limits_concurrency_and_releases_on_error(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    calls = []
    def fail(*args):
        calls.append(args)
        raise TimeoutError('Connection timed out')
    monkeypatch.setattr(ssh, 'probe_host_key', fail)
    for body in ({'host': 'ssh://example.test'}, {'host': 'example.test', 'port': 0},
                 {'host': 'example.test', 'port': 65536}, {'host': 'example.test', 'username': 'unexpected'}):
        assert client.post('/api/ssh/fingerprint', json=body).status_code == 422
    assert not calls
    for _ in range(4):
        assert module.fingerprint_probes.acquire(blocking=False)
    try:
        assert client.post('/api/ssh/fingerprint', json={'host': 'example.test'}).status_code == 429
        assert not calls
    finally:
        for _ in range(4):
            module.fingerprint_probes.release()
    for _ in range(5):
        result = client.post('/api/ssh/fingerprint', json={'host': 'example.test'})
        assert result.status_code == 502 and 'timed out' in result.json()['detail']
    assert len(calls) == 5


def test_probe_always_closes_transport_and_socket(monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    events = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): events.append('socket closed')
        def getpeername(self): return ('127.0.0.1', 22)
    class Transport:
        def __init__(self, connection, disabled_algorithms):
            assert disabled_algorithms == ssh.DISABLED_ALGORITHMS
        def start_client(self, timeout):
            assert timeout <= 12
            raise TimeoutError('handshake timeout')
        def close(self): events.append('transport closed')
    monkeypatch.setattr(ssh.socket, 'create_connection', lambda address, timeout: Connection())
    monkeypatch.setattr(ssh.paramiko, 'Transport', Transport)
    with pytest.raises(TimeoutError):
        ssh.probe_host_key('127.0.0.1')
    assert events == ['transport closed', 'socket closed']
    monkeypatch.setattr(store, 'DEMO', True)
    with pytest.raises(RuntimeError, match='demo'):
        ssh.probe_host_key('127.0.0.1')
