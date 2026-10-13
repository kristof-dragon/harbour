import datetime as dt
import io
import json
import os
import threading
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace

import pytest

from test_harbour import client
from harbour import call_endpoint as endpoint, call_endpoint_service as service, call_endpoint_install as installer
from harbour import collector_bundle, collector_setup, store


def test_endpoint_settings_secrets_and_permissions(client, monkeypatch):
    url = '/api/servers/atlas/call-endpoint'
    body = {'endpoint': '192.0.2.10', 'http_port': 23456, 'token': 'private-test-token-' + 'x' * 32}
    assert client.put(url, json=body).status_code == 400
    monkeypatch.setattr(store, 'DEMO', False)
    for change in ({'endpoint': 'localhost'}, {'http_port': 5202}, {'token': 'x\n' * 32}, {'port': 65535}):
        assert client.put(url, json={**body, **change}).status_code == 422
    assert client.put(url, json=body).status_code == 200
    token = endpoint.saved('atlas', True)['token']
    assert token == body['token']
    assert token not in client.get('/api/call-endpoints').text
    assert token not in str(store.rows('SELECT * FROM call_endpoints'))
    assert client.put(url, json={'endpoint': '192.0.2.10', 'http_port': 23456}).status_code == 200
    assert endpoint.saved('atlas', True)['token'] == token
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.put(url, json=body).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    client.post('/api/users', json={'name': 'viewer', 'password': 'viewer-password-long', 'role': 'user'})
    user = client.post('/api/login', json={'name': 'viewer', 'password': 'viewer-password-long'}).json()
    client.headers['X-CSRF-Token'] = user['csrf']
    assert client.put(url, json=body).status_code == 403
    assert client.post('/api/servers/atlas/collector/call-endpoint', json={'action': 'install'}).status_code == 403


def test_endpoint_install_reuses_stream_and_rejects_routers(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    client.put('/api/servers/atlas/call-endpoint', json={'endpoint': '192.0.2.10'})
    assert client.post('/api/servers/atlas/collector/call-endpoint', json={'action': 'push'}).status_code == 400
    def transfer(op):
        assert op.kind == 'call-endpoint'
        assert len(endpoint.saved(op.server['id'], True)['token']) >= 32
        return {'kind': 'result', 'ok': True, 'message': 'Fixture deployed'}
    monkeypatch.setattr(collector_setup, 'transfer', transfer)
    response = client.post('/api/servers/atlas/collector/call-endpoint', json={'action': 'install'})
    assert response.status_code == 200 and 'Fixture deployed' in response.text
    assert endpoint.saved('atlas')['deployed']
    client.put('/api/servers/atlas/type', json={'server_type': 'openwrt'})
    assert client.post('/api/servers/atlas/collector/call-endpoint', json={'action': 'install'}).status_code == 400
    assert client.put('/api/servers/atlas/call-endpoint', json={'endpoint': '192.0.2.10'}).status_code == 400


def test_dated_logs_rotation_retention_and_unsafe_paths(tmp_path):
    logs = service.Logs(tmp_path, days=1, max_bytes=200)
    first = dt.datetime(2026, 10, 13, 23, 59, tzinfo=dt.timezone.utc)
    second = first + dt.timedelta(minutes=2)
    logs.append('upload', {'event': 'first'}, first)
    old = logs.files['upload']; os.utime(old, (1, 1))
    logs.append('upload', {'event': 'second'}, second)
    assert old.parent.name == '26-10-13' and logs.files['upload'].parent.name == '26-10-14'
    assert logs.files['upload'].stat().st_mode & 0o777 == 0o600
    with pytest.raises(OSError): logs.read('/logs/../../secret')
    secret = tmp_path / 'secret'; secret.write_text('private')
    link = tmp_path / '26-10-14' / 'linked.jsonl'; link.symlink_to(secret)
    with pytest.raises(OSError): logs.read('/logs/26-10-14/linked.jsonl')
    (tmp_path / '26-10-15').symlink_to(tmp_path / '26-10-14', target_is_directory=True)
    with pytest.raises(OSError): logs.read('/logs/26-10-15')
    listed = json.loads(logs.read('/logs/26-10-14')[0])
    assert len(listed['files']) == 1
    logs.prune(now=second.timestamp())
    assert not old.exists() and logs.files['upload'].exists()
    # A chunk that would cross the per-file cap starts a new file.
    logs.append('download', {'text': 'a' * (service.MAX_FILE - 200)}, second)
    original = logs.files['download']
    logs.append('download', {'text': 'b' * 300}, second)
    assert logs.files['download'] != original


def test_actual_http_auth_and_harbour_proxy_without_ssh(client, tmp_path, monkeypatch):
    logs = service.Logs(tmp_path / 'logs')
    logs.append('upload', {'event': 'test-result'})
    token = 'local-test-' + 'x' * 32
    server = service.Server(('127.0.0.1', 0), logs, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    port = server.server_port
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/logs')
        assert exc.value.code == 401
        monkeypatch.setattr(endpoint, 'saved', lambda id_, secret=False: {'endpoint': '127.0.0.1', 'http_port': port, 'token': token})
        response = client.get('/api/servers/atlas/call-endpoint/logs')
        day = response.json()['days'][0]
        files = client.get('/api/servers/atlas/call-endpoint/logs?day=' + day).json()['files']
        response = client.get('/api/servers/atlas/call-endpoint/logs/' + day + '/' + files[0]['name'])
        assert response.status_code == 200 and 'test-result' in response.text
        assert 'attachment;' in response.headers['content-disposition']
        assert token not in response.text
        assert client.get('/api/servers/atlas/call-endpoint/logs?day=../').status_code == 400
        server.token = 'wrong-token'
        assert client.get('/api/servers/atlas/call-endpoint/logs').status_code == 502
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


def test_installer_folder_identity_config_preservation_and_bundle(tmp_path, monkeypatch):
    source = tmp_path / 'bundle'; source.mkdir()
    with zipfile.ZipFile(io.BytesIO(collector_bundle.build('call-endpoint'))) as bundle:
        bundle.extractall(source)
    config = {'endpoint': '192.0.2.10', 'port': 53201, 'http_port': 53203,
              'retention_days': 30, 'token': 'private-' + 'x' * 32}
    (source / 'endpoint.json').write_text(json.dumps(config))
    monkeypatch.setattr(installer.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_dir=str(tmp_path), pw_uid=os.getuid(), pw_gid=os.getgid()))
    monkeypatch.setattr(installer.shutil, 'which', lambda _: '/fixture/docker')
    monkeypatch.setattr(installer.os, 'chown', lambda *args: None)
    commands = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kw: commands.append(command))
    installer.install('fixture', source)
    root = tmp_path / 'harbour-c-remote'
    assert (root / 'endpoint.json').stat().st_mode & 0o777 == 0o600
    assert config['token'] not in str(commands) and any('--wait' in c for c in commands)
    day = root / 'logs' / '26-10-13'; day.mkdir(); log = day / 'upload.jsonl'; log.write_text('evidence')
    installer.install('fixture', source)
    assert log.read_text() == 'evidence'
    assert json.loads((root / 'previous' / 'endpoint.json').read_text()) == config
    (root / '.harbour-endpoint').unlink()
    with pytest.raises(RuntimeError): installer.install('fixture', source)


def test_one_shot_deployment_callback_and_password_lifetime(client, monkeypatch):
    from harbour import ssh
    monkeypatch.setattr(store, 'DEMO', False)
    secret = ' one-shot SSH password is private '
    boot = 'b' * 32
    seen = []
    before = store.rows('SELECT * FROM servers')
    def transfer(op):
        seen.append(op)
        assert op.ephemeral and op.credential == {'password': secret}
        assert secret not in json.dumps(op.endpoint_config)
        assert 'password_encrypted' not in op.server
        with pytest.raises(RuntimeError, match='does not use sudo'):
            op.password()
        id_ = op.server['id']
        token = op.endpoint_config['callback_token']
        assert client.post('/api/call-endpoints/'+id_+'/ready', json={'boot_id':boot,'protocol':1}).status_code == 401
        response = client.post('/api/call-endpoints/'+id_+'/ready', headers={'Authorization':'Bearer '+token}, json={'boot_id':boot,'protocol':1})
        assert response.status_code == 200, response.text
        assert client.post('/api/call-endpoints/'+id_+'/ready', headers={'Authorization':'Bearer '+token}, json={'boot_id':'c'*32,'protocol':1}).status_code == 401
        return {'kind':'result','ok':True,'message':'Docker started'}
    monkeypatch.setattr(collector_setup, 'transfer', transfer)
    monkeypatch.setattr(endpoint, 'status', lambda id_: {'boot_id':boot,'endpoint_id':id_,'ready':True,'protocol':1})
    response = client.post('/api/call-endpoints/deploy', json={'endpoint':'192.0.2.44','username':'dockeruser',
        'password':secret,'fingerprint':'SHA256:'+'A'*43,'callback_url':'https://harbour.example.test'})
    assert response.status_code == 200 and 'Endpoint confirmed' in response.text, response.text
    assert secret not in response.text
    assert store.rows('SELECT * FROM servers') == before
    assert len(seen) == 1 and seen[0].credential is None and seen[0].endpoint_config is None
    id_ = seen[0].server['id']
    assert endpoint.saved(id_)['deployed'] and endpoint.saved(id_)['callback_received']
    assert secret not in str(store.rows('SELECT * FROM call_endpoints'))
    assert secret not in str(store.rows('SELECT * FROM auth_log'))
    assert 'callback_hash' not in client.get('/api/call-endpoints').text
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.post('/api/call-endpoints/deploy', json={'endpoint':'192.0.2.44','password':secret,'callback_url':'https://harbour.example.test'}).status_code == 403
    client.headers['X-CSRF-Token'] = csrf


def test_one_shot_failure_does_not_echo_or_retain_password(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    secret = 'private-failure-password'
    seen = []
    def transfer(op):
        seen.append(op)
        raise RuntimeError(secret)
    monkeypatch.setattr(collector_setup, 'transfer', transfer)
    body = {'endpoint':'192.0.2.45','password':secret,'fingerprint':'SHA256:'+'A'*43,'callback_url':'https://harbour.example.test'}
    response = client.post('/api/call-endpoints/deploy', json=body)
    assert secret not in response.text and 'No sudo is used' in response.text
    assert seen[0].credential is None and seen[0].endpoint_config is None
    assert not collector_setup.operations
    for url in ('https://user:secret@harbour.example.test', 'file:///tmp/harbour', 'http://host/?secret=x', 'http://host/#fragment'):
        response = client.post('/api/call-endpoints/deploy', json={**body,'callback_url':url})
        assert response.status_code == 422 and secret not in response.text


def test_indexed_run_records_survive_service_restart_and_match_direction(tmp_path):
    logs = service.Logs(tmp_path)
    test_id, boot = 'a'*32, 'b'*32
    data = {'extra_data':'harbour:'+test_id+':upload','start':{'cookie':'session-one'},'end':{}}
    logs.result('download',data,boot)
    assert json.loads(logs.run(test_id)[0])['records'] == []
    logs.result('upload',data,boot)
    logs.result('upload',{**data,'error':'must not overwrite'},boot)
    restored = service.Logs(tmp_path)
    record = json.loads(restored.run(test_id)[0])['records'][0]
    assert record['raw'] == data and record['boot_id'] == boot
    assert '/run-'+test_id+'-upload.jsonl' in record['log_path']
    with pytest.raises(OSError): restored.run('../secret')


def test_endpoint_migration_detaches_saved_server_lifetime(client):
    # Old releases used a foreign key. Preserve the endpoint after deleting a
    # monitored host, without changing the independently saved SSH credentials.
    store.execute('DROP TABLE call_endpoints')
    store.execute('CREATE TABLE call_endpoints(server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,config TEXT NOT NULL,deployed INTEGER DEFAULT 0)')
    store.execute('INSERT INTO call_endpoints VALUES(?,?,1)', ('atlas',json.dumps({'endpoint':'192.0.2.10','token_encrypted':'fixture'})))
    endpoint.initialize()
    store.execute("DELETE FROM servers WHERE id='atlas'")
    assert store.one("SELECT deployed FROM call_endpoints WHERE server_id='atlas'")['deployed'] == 1


def test_installer_nonroot_account_does_not_chown_or_escalate(tmp_path, monkeypatch):
    source=tmp_path/'bundle';source.mkdir()
    with zipfile.ZipFile(io.BytesIO(collector_bundle.build('call-endpoint'))) as bundle:bundle.extractall(source)
    (source/'endpoint.json').write_text(json.dumps({'port':54201,'http_port':54203,'token':'fixture-token','retention_days':30}))
    monkeypatch.setattr(installer.pwd,'getpwnam',lambda _:SimpleNamespace(pw_dir=str(tmp_path),pw_uid=1234,pw_gid=1234))
    monkeypatch.setattr(installer.os,'getuid',lambda:1234)
    monkeypatch.setattr(installer.shutil,'which',lambda _:'/fixture/docker')
    def forbidden(*args):raise AssertionError('Non-root installation must not chown')
    monkeypatch.setattr(installer.os,'chown',forbidden)
    commands=[]
    def run(command,**kw):
        commands.append(command)
        assert command[0]=='docker'
        return SimpleNamespace(stdout='["name=rootless"]')
    monkeypatch.setattr(installer.subprocess,'run',run)
    installer.install('dockeruser',source)
    assert 'REMOTE_UID=0' in (tmp_path/'harbour-c-remote'/'.env').read_text()
    assert all('sudo' not in command for command in commands)
