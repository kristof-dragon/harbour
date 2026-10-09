import io
import asyncio
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
import uvicorn

from test_harbour import client
from harbour import app as module, collector_bundle, collector_setup as setup, remote_collector_setup as remote, ssh, store


def config(kind='resources', action='extract'):
    import hashlib
    data = collector_bundle.build(kind)
    prefix, installer, files = collector_bundle.BUNDLES[kind]
    return {'kind': kind, 'action': action, 'prefix': prefix, 'installer': installer, 'files': files,
            'reader': 'harbour', 'interval': 30, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}, data


@pytest.mark.parametrize('kind', ['resources', 'logins'])
@pytest.mark.parametrize('action', ['push', 'extract'])
def test_home_upload_extraction_and_no_overwrite(tmp_path, monkeypatch, kind, action):
    monkeypatch.setattr(remote.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_dir=str(tmp_path)))
    cfg, data = config(kind, action)
    first, folder = remote.receive(cfg, io.BytesIO(data))
    second, _ = remote.receive(cfg, io.BytesIO(data))
    assert first.parent == second.parent == tmp_path and first != second
    assert first.read_bytes() == data and first.stat().st_mode & 0o777 == 0o600
    if action == 'extract':
        assert folder.parent == tmp_path and folder.stat().st_mode & 0o777 == 0o700
        assert {p.name for p in folder.iterdir()} == set(cfg['files'])
        assert (folder / cfg['installer']).read_bytes() == Path(remote.__file__).with_name(cfg['installer']).read_bytes()
    else:
        assert folder is None and all(p.is_file() for p in tmp_path.iterdir())


def test_interrupted_or_corrupt_upload_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_dir=str(tmp_path)))
    cfg, data = config()
    for bad in (data[:20], b'x' * len(data)):
        with pytest.raises(RuntimeError):
            remote.receive(cfg, io.BytesIO(bad))
        assert list(tmp_path.iterdir()) == []


def test_prompt_split_and_unicode_output(monkeypatch):
    emitted, prompts = [], []
    monkeypatch.setattr(remote, 'output', emitted.append)
    parser = remote.SudoOutput(b'unique-prompt:', lambda: prompts.append(True))
    for byte in 'Starting £\nunique-prompt:Rejected\nunique-prompt:Installed ✓'.encode():
        parser.feed(bytes([byte]))
    parser.feed(b'', final=True)
    assert ''.join(emitted) == 'Starting £\nRejected\nInstalled ✓'
    assert len(prompts) == 2


@pytest.mark.parametrize('needs_password', [False, True])
def test_real_subprocess_sudo_prompt_never_echoes_password(tmp_path, monkeypatch, needs_password):
    # Stand-in sudo only; no system service or elevated command is run.
    fake = tmp_path / 'sudo'
    fake.write_text('#!' + sys.executable + '\nimport sys\n'
                    + ("sys.stdout.write(sys.argv[sys.argv.index('-p')+1]);sys.stdout.flush()\n"
                       "assert sys.stdin.readline() == '  ephemeral £  \\n'\n" if needs_password else '')
                    + "print('Installed fixture')\n")
    fake.chmod(0o700)
    monkeypatch.setattr(remote.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(remote.shutil, 'which', lambda _: str(fake))
    events = []
    read_fd, write_fd = os.pipe()
    def emit(kind, **values):
        events.append((kind, values))
        if kind == 'password':
            os.write(write_fd, (json.dumps({'password': '  ephemeral £  '}) + '\n').encode())
    monkeypatch.setattr(remote, 'emit', emit)
    try:
        with os.fdopen(read_fd, 'rb') as stream:
            assert remote.run_installer(['/unused/installer'], stream) == 0
    finally:
        os.close(write_fd)
    assert sum(kind == 'password' for kind, _ in events) == int(needs_password)
    assert 'ephemeral' not in str(events)
    assert 'Installed fixture' in str(events)


def frames(response):
    assert response.status_code == 200, response.text
    assert response.headers['x-accel-buffering'] == 'no'
    return [json.loads(line) for line in response.text.splitlines()]


def test_stream_permissions_validation_demo_and_bundle_identity(client, monkeypatch):
    url = '/api/servers/atlas/collector/resources'
    assert client.post(url, json={'action': 'push'}).status_code == 400
    monkeypatch.setattr(store, 'DEMO', False)
    assert client.post(url, json={'action': 'bad'}).status_code == 422
    assert client.post(url.replace('resources', 'arbitrary'), json={'action': 'push'}).status_code == 422
    assert client.post(url, json={'action': 'push'}, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.post(url, json={'action': 'push'}).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    for kind, name in [('resources', 'resource-recorder'), ('logins', 'login-collector')]:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(client.get('/api/' + name + '/download').content)) as actual:
            with zipfile.ZipFile(io.BytesIO(collector_bundle.build(kind))) as expected:
                assert {n: actual.read(n) for n in actual.namelist()} == {n: expected.read(n) for n in expected.namelist()}
    client.post('/api/users', json={'name': 'viewer', 'password': 'long-viewer-password', 'role': 'user'})
    user = client.post('/api/login', json={'name': 'viewer', 'password': 'long-viewer-password'}).json()
    client.headers['X-CSRF-Token'] = user['csrf']
    assert client.post(url, json={'action': 'push'}).status_code == 403
    assert client.post('/api/collector-operations/x/cancel').status_code == 403


def test_stream_result_failure_and_polling_independence(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    sentinel = object()
    monkeypatch.setattr(ssh, 'resource_sessions', {'atlas': sentinel})
    def transfer(op):
        assert not module.resource_lock('atlas').locked()
        assert module.server_lock('atlas').locked()
        assert ssh.resource_sessions['atlas'] is sentinel
        op.emit({'kind': 'output', 'text': 'progress\n'})
        return {'kind': 'result', 'ok': True, 'message': 'Uploaded'}
    monkeypatch.setattr(setup, 'transfer', transfer)
    result = frames(client.post('/api/servers/atlas/collector/resources', json={'action': 'push'}))
    assert [f['kind'] for f in result] == ['started', 'output', 'result']
    assert result[-1]['ok'] and not setup.operations and not module.server_lock('atlas').locked()
    monkeypatch.setattr(setup, 'transfer', lambda _: (_ for _ in ()).throw(RuntimeError('SENSITIVE')))
    result = frames(client.post('/api/servers/atlas/collector/logins', json={'action': 'install'}))
    assert not result[-1]['ok'] and 'SENSITIVE' not in str(result)
    # Avoid the app's shutdown treating our isolation sentinel as a real session.
    ssh.resource_sessions.clear()


def test_interactive_password_one_time_ownership_and_host_lock(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    entered, release = threading.Event(), threading.Event()
    received = []
    def transfer(op):
        entered.set()
        received.append(op.password())
        release.wait(5)
        return {'kind': 'result', 'ok': True, 'message': 'Installed'}
    monkeypatch.setattr(setup, 'transfer', transfer)
    responses = []
    thread = threading.Thread(target=lambda: responses.append(client.post('/api/servers/atlas/collector/resources', json={'action': 'install'})))
    thread.start()
    try:
        assert entered.wait(3)
        op = next(iter(setup.operations.values()))
        deadline = time.monotonic() + 3
        while not op.prompt and time.monotonic() < deadline:
            time.sleep(.01)
        url = '/api/collector-operations/' + op.id + '/password'
        assert client.delete('/api/servers/atlas').status_code == 409
        assert client.post('/api/servers/atlas/collector/logins', json={'action': 'push'}).status_code == 409
        assert client.post(url, json={'prompt': 'stale', 'password': 'secret'}).status_code == 409
        prompt = op.prompt
        original = op.session
        op.session = 'another-session'
        assert client.post(url, json={'prompt': prompt, 'password': 'secret'}).status_code == 404
        op.session = original
        assert client.post(url, json={'prompt': prompt, 'password': 'bad\npassword'}).status_code == 422
        body = {'prompt': prompt, 'password': '  ephemeral £  '}
        assert client.post(url, json=body).status_code == 200
        assert client.post(url, json=body).status_code == 409
    finally:
        release.set(); thread.join(timeout=5)
    assert not thread.is_alive() and received == ['  ephemeral £  ']
    assert frames(responses[0])[-1]['ok']
    assert 'ephemeral' not in responses[0].text
    assert 'ephemeral' not in str(store.rows('SELECT * FROM auth_log'))
    assert not setup.operations and not module.server_lock('atlas').locked()


def test_cancel_while_waiting_for_password_releases_host(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    entered = threading.Event()
    def transfer(op):
        entered.set(); op.password()
        pytest.fail('Cancelled setup must not install')
    monkeypatch.setattr(setup, 'transfer', transfer)
    thread = threading.Thread(target=lambda: client.post('/api/servers/atlas/collector/resources', json={'action': 'install'}))
    thread.start()
    try:
        assert entered.wait(3)
        op = next(iter(setup.operations.values()))
        assert client.post('/api/collector-operations/' + op.id + '/cancel').status_code == 200
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive() and op.done.is_set()
    assert not setup.operations and not module.server_lock('atlas').locked()


@pytest.mark.parametrize('action', ['push', 'extract', 'install'])
@pytest.mark.parametrize('kind', ['resources', 'logins'])
def test_full_bootstrap_protocol_in_subprocess(client, tmp_path, monkeypatch, action, kind):
    """Run the exact remote bootstrap and byte protocol; stub only SSH and sudo."""
    monkeypatch.setattr(store, 'DEMO', False)
    home = tmp_path / 'home'; home.mkdir()
    fake_sudo = tmp_path / 'fixture-sudo'
    fake_sudo.write_text('#!' + sys.executable + '\nimport sys\n'
                        "sys.stdout.write(sys.argv[sys.argv.index('-p')+1]);sys.stdout.flush()\n"
                        "assert sys.stdin.readline() == 'one-time password\\n'\n"
                        "print('Fixture service started')\n")
    fake_sudo.chmod(0o700)
    original = Path.read_bytes
    def source(path):
        data = original(path)
        if path.name == 'remote_collector_setup.py':
            injection = ("from types import SimpleNamespace\n"
                         "pwd.getpwuid=lambda _: SimpleNamespace(pw_dir=" + repr(str(home)) + ")\n"
                         "shutil.which=lambda _: " + repr(str(fake_sudo)) + "\n"
                         "os.geteuid=lambda: 1000\n")
            data = data.replace(b"if __name__ == '__main__':", injection.encode() + b"\nif __name__ == '__main__':")
        return data
    monkeypatch.setattr(Path, 'read_bytes', source)
    processes, connections = [], []
    class Channel:
        def __init__(self, process):
            self.process = process; self.open = {'stdout': True, 'stderr': True}
        def ready(self, name):
            return self.open[name] and bool(select.select([getattr(self.process, name)], [], [], 0)[0])
        def recv_ready(self): return self.ready('stdout')
        def recv_stderr_ready(self): return self.ready('stderr')
        def read(self, name, size):
            data = os.read(getattr(self.process, name).fileno(), size)
            if not data: self.open[name] = False
            return data
        def recv(self, size): return self.read('stdout', size)
        def recv_stderr(self, size): return self.read('stderr', size)
        def exit_status_ready(self): return self.process.poll() is not None
        def recv_exit_status(self): return self.process.wait(timeout=5)
    class Input:
        def __init__(self, stream): self.stream = stream
        def write(self, data): self.stream.write(data.encode() if isinstance(data, str) else data)
        def flush(self): self.stream.flush()
    class Client:
        def get_transport(self): return SimpleNamespace(set_keepalive=lambda _: None)
        def exec_command(self, command, **kwargs):
            process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(process)
            return Input(process.stdin), SimpleNamespace(channel=Channel(process)), None
        def close(self):
            for process in processes:
                if process.poll() is None: process.terminate(); process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr): stream.close()
    def connect(server, cancel, **credentials):
        connections.append(server['id']); client = Client(); cancel.bind(client.close); return client
    monkeypatch.setattr(ssh, 'connect', connect)
    monkeypatch.setattr(ssh, 'stored_key', lambda _: 'fixture-key')
    monkeypatch.setattr(setup.Operation, 'password', lambda _: 'one-time password')
    op = setup.Operation(module.get_server('atlas'), kind, action, {'session_token': 'test'}, threading.Lock())
    result = setup.transfer(op)
    assert result['ok'], result
    assert connections == ['atlas']
    assert Path(result['archive']).parent == home
    assert (result['directory'] is not None) == (action != 'push')
    events = []
    while not op.events.empty(): events.append(op.events.get_nowait())
    assert any(event['kind'] == 'progress' for event in events)
    assert 'one-time password' not in str(events)
    if action == 'install': assert 'Fixture service started' in str(events)


@pytest.mark.parametrize('disconnect', [False, True])
def test_live_http_stream_and_disconnect_cancel(client, monkeypatch, disconnect):
    """Exercise FastAPI middleware and real HTTP streaming, not buffered TestClient."""
    monkeypatch.setattr(store, 'DEMO', False)
    active, received = [], []
    def transfer(op):
        active.append(op)
        op.emit({'kind': 'output', 'text': 'Live progress before completion\n'})
        received.append(op.password())
        return {'kind': 'result', 'ok': True, 'message': 'Installed'}
    monkeypatch.setattr(setup, 'transfer', transfer)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(module.app, lifespan='off', log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline: time.sleep(.01)
        try:
            with httpx.Client(base_url='http://127.0.0.1:' + str(listener.getsockname()[1]), timeout=5) as live:
                login = live.post('/api/login', json={'name': 'admin', 'password': 'test-password-strong'})
                assert login.status_code == 200
                live.headers['X-CSRF-Token'] = login.json()['csrf']
                with live.stream('POST', '/api/servers/atlas/collector/resources', json={'action': 'install'}) as response:
                    assert response.status_code == 200 and response.headers['x-accel-buffering'] == 'no'
                    seen = []
                    for line in response.iter_lines():
                        event = json.loads(line); seen.append(event)
                        if event['kind'] == 'output':
                            assert not active[0].done.is_set()
                            if disconnect: break
                        if event['kind'] == 'password':
                            result = live.post('/api/collector-operations/' + active[0].id + '/password',
                                               json={'prompt': event['prompt'], 'password': 'ephemeral-live'})
                            assert result.status_code == 200
                deadline = time.monotonic() + 5
                while not active[0].done.is_set() and time.monotonic() < deadline: time.sleep(.02)
                assert active[0].done.is_set() and not setup.operations
                assert not module.server_lock('atlas').locked()
                if disconnect:
                    assert active[0].cancel.is_set() and received == []
                else:
                    assert seen[-1]['ok'] and received == ['ephemeral-live']
                    assert 'ephemeral-live' not in str(seen)
        finally:
            server.should_exit = True; thread.join(timeout=5)


def test_disconnect_before_response_body_releases_reservation():
    lock = threading.Lock(); lock.acquire()
    op = setup.Operation({'id': 'pre-response'}, 'resources', 'push', {'session_token': 'session'}, lock)
    setup.operations[op.id] = op
    async def content():
        pytest.fail('Body must not start after connection failure')
        yield ''
    async def send(_):
        raise OSError('Peer disconnected before headers')
    async def receive():
        return {'type': 'http.disconnect'}
    async def run():
        with pytest.raises(Exception):
            await setup.SetupResponse(op, content())({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    asyncio.run(run())
    assert op.cancel.is_set() and op.done.is_set() and not lock.locked()
    assert op.id not in setup.operations
