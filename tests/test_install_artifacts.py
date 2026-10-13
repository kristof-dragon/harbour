import asyncio
import json
import sqlite3
import threading
import time

import pytest

from test_harbour import client
from harbour import app, collector_setup as setup, install_artifacts as artifacts, store


def deploy(client, kind='resources'):
    response = client.post('/api/servers/atlas/collector/' + kind, json={'action': 'install'})
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines()]


def test_full_output_download_is_durable_and_secrets_are_redacted(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    secret, token = 'one-shot-ssh-password', 'private-endpoint-token'
    first, tail = 'BEGIN deployment\n', '\nEND deployment\n'
    seen = []
    def transfer(op):
        seen.append(op)
        op.redactor.add(secret)
        op.redactor.add(token)
        for chunk in [first, secret[:8], secret[8:], '\n', token[:10], token[10:], '\n', 'x' * 200000, tail]:
            op.emit({'kind': 'output', 'text': chunk})
        # Evidence commits before the progress reader consumes it.
        assert artifacts.read(op.id)['output'].startswith(first)
        return {'kind': 'result', 'ok': True, 'message': 'Installed'}
    monkeypatch.setattr(setup, 'transfer', transfer)
    frames = deploy(client)
    id_ = frames[0]['id']
    listing = client.get('/api/install-artifacts').json()['artifacts']
    assert len(listing) == 1 and listing[0]['id'] == id_ and listing[0]['status'] == 'succeeded'
    assert listing[0]['actor'] == 'admin' and listing[0]['finished'] >= listing[0]['created']
    response = client.get('/api/install-artifacts/' + id_ + '/download')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert 'attachment;' in response.headers['content-disposition']
    assert first in response.text and tail in response.text and 'x' * 200000 in response.text
    assert secret not in response.text and token not in response.text and '[REDACTED]' in response.text
    assert secret not in str(frames) and token not in str(frames)
    assert not seen[0].redactor.secrets and not seen[0].redactor.pending
    # No in-memory Operation is needed, even after migrations/restarts/off-boarding.
    assert not setup.operations
    store.execute('DELETE FROM servers WHERE id=?', ('atlas',))
    monkeypatch.setattr(store, 'DEMO', True)  # Match the fixture database's original mode.
    store.initialize()
    artifacts.recover()
    assert client.get('/api/install-artifacts/' + id_ + '/download').text == response.text
    for path in store.DATA.glob('harbour.db*'):
        assert secret.encode() not in path.read_bytes() and token.encode() not in path.read_bytes()


def test_failed_and_cancelled_attempts_are_retained(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    def failed(op):
        op.emit({'kind': 'output', 'text': 'Before failure\n'})
        raise RuntimeError('private internal exception')
    monkeypatch.setattr(setup, 'transfer', failed)
    frames = deploy(client)
    saved = artifacts.read(frames[0]['id'])
    assert saved['status'] == 'failed' and 'Before failure' in saved['output']
    assert 'private internal exception' not in saved['output']
    entered = threading.Event()
    captured = []
    def waiting(op):
        captured.append(op)
        op.emit({'kind': 'output', 'text': 'Before cancellation\n'})
        entered.set()
        while True:
            op.cancel.check()
            time.sleep(.01)
    monkeypatch.setattr(setup, 'transfer', waiting)
    thread = threading.Thread(target=lambda: deploy(client))
    thread.start()
    try:
        assert entered.wait(3)
        id_ = captured[0].id
        assert client.get('/api/install-artifacts/' + id_ + '/download').status_code == 200
        client.post('/api/collector-operations/' + id_ + '/cancel')
    finally:
        thread.join(5)
    assert not thread.is_alive()
    saved = artifacts.read(id_)
    assert saved['status'] == 'stopped' and 'Before cancellation' in saved['output']
    assert 'remote state is unconfirmed' in saved['output']


def test_restart_pagination_and_admin_access(client):
    for i in range(52):
        op = setup.Operation({'id': 'gone', 'host': '192.0.2.1', 'name': 'Deleted host'},
                             'call-endpoint', 'install', {'session_token': 'unused'}, threading.Lock())
        artifacts.create(op, 'former-admin')
        artifacts.append(op.id, 'Persisted before restart\n')
    artifacts.recover()
    artifacts.recover()  # Idempotent and does not erase previous evidence.
    first = client.get('/api/install-artifacts').json()
    second = client.get('/api/install-artifacts', params={'before': first['next']}).json()
    assert len(first['artifacts']) == 50 and len(second['artifacts']) == 2 and second['next'] is None
    assert all(row['status'] == 'interrupted' for row in first['artifacts'])
    assert artifacts.read(op.id)['output'].count('Harbour restarted') == 1
    assert client.get('/api/install-artifacts/unknown').status_code == 404
    assert client.get('/api/install-artifacts/unknown/download').status_code == 404
    client.post('/api/users', json={'name': 'viewer', 'password': 'long-viewer-password', 'role': 'user'})
    viewer = client.post('/api/login', json={'name': 'viewer', 'password': 'long-viewer-password'}).json()
    client.headers['X-CSRF-Token'] = viewer['csrf']
    for url in ['/api/install-artifacts', '/api/install-artifacts/' + op.id, '/api/install-artifacts/' + op.id + '/download']:
        assert client.get(url).status_code == 403
    client.cookies.clear()
    assert client.get('/api/install-artifacts').status_code == 401


def test_output_limit_preserves_prior_output_and_failure_reason(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    def transfer(op):
        op.emit({'kind': 'output', 'text': 'Before excessive output\n'})
        op.emit({'kind': 'output', 'text': 'x' * (artifacts.MAX_OUTPUT + 1)})
    monkeypatch.setattr(setup, 'transfer', transfer)
    frames = deploy(client)
    saved = artifacts.read(frames[0]['id'])
    assert saved['status'] == 'failed'
    assert 'Before excessive output' in saved['output'] and 'output exceeded its limit' in saved['output']


def test_storage_failure_prevents_unrecorded_deployment(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    def unavailable(*args):
        raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(artifacts, 'create', unavailable)
    monkeypatch.setattr(setup, 'transfer', lambda _: (_ for _ in ()).throw(AssertionError('Must not deploy')))
    response = client.post('/api/servers/atlas/collector/resources', json={'action': 'install'})
    assert response.status_code == 503 and 'No deployment was started' in response.text
    assert not setup.operations and not app.server_lock('atlas').locked()


def test_storage_failure_during_output_stops_with_explicit_error(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    def transfer(op):
        op.emit({'kind': 'output', 'text': 'Prior output\n'})
        def unavailable(*args):
            raise sqlite3.OperationalError('disk full')
        monkeypatch.setattr(artifacts, 'append', unavailable)
        op.emit({'kind': 'output', 'text': 'Cannot save this\n'})
        raise AssertionError('Must stop after failed artifact write')
    monkeypatch.setattr(setup, 'transfer', transfer)
    frames = deploy(client)
    assert not frames[-1]['ok'] and 'could not save its Install artifact' in frames[-1]['message']
    saved = artifacts.read(frames[0]['id'])
    assert saved['status'] == 'failed' and 'Prior output' in saved['output']


def test_install_artifact_survives_disconnect_before_headers(client):
    lock = threading.Lock()
    lock.acquire()
    op = setup.Operation({'id': 'not-started'}, 'resources', 'install',
                         {'session_token': 'unused'}, lock, credential={'password': 'private'})
    artifacts.create(op, 'admin')
    op.artifact = True
    setup.operations[op.id] = op
    async def content():
        pytest.fail('Deployment must not start')
        yield ''
    async def send(_):
        raise OSError('Disconnected')
    async def receive():
        return {'type': 'http.disconnect'}
    async def run():
        with pytest.raises(Exception):
            await setup.SetupResponse(op, content())({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    asyncio.run(run())
    assert not lock.locked() and op.credential is None and not op.redactor.secrets
    saved = artifacts.read(op.id)
    assert saved['status'] == 'stopped' and 'No remote command was run' in saved['output']
