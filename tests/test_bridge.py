import json
import threading
import time
from types import SimpleNamespace

import pytest

from harbour import app, bridge, login_agent, login_live, logins, remote_probe, store
from test_harbour import client
from test_login_sessions import history, event, snapshot


def enable(client):
    assert client.put('/api/bridge/settings', json={'enabled': True}).status_code == 200


def test_feature_is_off_and_hidden_until_enabled_and_retains_labels(client, history):
    assert client.get('/api/dashboard').json()['bridge_enabled'] is False
    assert client.get('/api/bridge').status_code == 404
    enable(client)
    now = time.time()
    history([event('authentication', now-10)], snapshot(now))
    data = client.get('/api/bridge').json()
    assert len(data['active']) == 1
    ref = data['active'][0]['ref']
    assert client.put('/api/bridge/records/'+ref+'/identity', json={'label': 'Laptop'}).status_code == 200
    client.put('/api/bridge/settings', json={'enabled': False})
    assert client.get('/api/bridge/history').status_code == 404
    assert client.get('/api/bridge/identities').status_code == 404
    enable(client)
    assert client.get('/api/bridge').json()['active'][0]['label'] == 'Laptop'


def test_credential_recurrence_independent_tags_and_failed_keys(client, history):
    enable(client)
    now = time.time()
    data = history([event('authentication', now-100), event('session_end', now-80),
                    event('authentication', now-10, pid='13')], snapshot(now, pid='13'))
    records = client.get('/api/bridge/history').json()['records']
    assert len(records) == 2
    old, new = records[1]['ref'], records[0]['ref']
    assert client.put('/api/bridge/records/'+old+'/annotation', json={'tags': ['review', ' review '], 'notes': '<script>test</script>'}).status_code == 200
    assert client.get('/api/bridge/records/'+old).json()['record']['tags'] == ['review']
    assert client.get('/api/bridge/records/'+old).json()['record']['label'] == ''
    assert client.put('/api/bridge/records/'+old+'/identity', json={'label': 'Admin laptop', 'tags': ['trusted']} ).status_code == 200
    live = client.get('/api/bridge').json()['active'][0]
    assert live['label'] == 'Admin laptop' and live['identity_tags'] == ['trusted'] and live['tags'] == []
    detail = client.get('/api/bridge/records/'+new).json()
    assert detail['stats']['connections'] == 2 and detail['stats']['servers'] == 1
    assert len(client.get('/api/bridge/history', params={'ref': new}).json()['records']) == 2
    assert len(client.get('/api/bridge/history', params={'filters': json.dumps({'identity': 'review'})}).json()['records']) == 1
    history([event('authentication', now-3, pid='14', result='failure', credential_verified=False)], snapshot(now, pid='13'))
    records = client.get('/api/bridge/history').json()['records']
    rejected = next(r for r in records if r['result'] == 'failure')
    assert rejected['label'] == '' and rejected['match_basis'] == 'attempt'
    per_server = client.get('/api/servers/atlas/logins/table').json()
    assert per_server['current_sessions'][0]['label'] == 'Admin laptop'
    profile = live['profile_id']
    assert client.delete('/api/bridge/identities/'+profile).status_code == 200
    assert client.get('/api/bridge/records/'+old).json()['record']['tags'] == ['review']
    assert not client.get('/api/bridge/records/'+new).json()['record']['label']


def test_match_key_account_and_host_boundaries(client, history):
    enable(client)
    now = time.time()
    history([event('authentication', now-5)], snapshot(now))
    record = client.get('/api/bridge').json()['active'][0]
    key = bridge.match_key(record)
    assert key == bridge.match_key({**record, 'server_id': 'another', 'source_ip': '192.0.2.22'})
    assert key != bridge.match_key({**record, 'username': 'another'})
    assert key != bridge.match_key({**record, 'credential_verified': False})
    assert bridge.match_key({**record, 'key_fingerprint': None}) != bridge.match_key({**record, 'key_fingerprint': None, 'server_id': 'another'})
    row = store.one('SELECT * FROM ('+bridge.BASE+') WHERE ref=?', (record['ref'],))
    assert row['match_key'] == key


@pytest.mark.parametrize('condition', ['stale', 'paused', 'changed', 'missing', 'error'])
def test_no_stale_or_uncollected_active_sessions(client, history, condition):
    enable(client)
    now = time.time()
    history([event('authentication', now-5)], snapshot(now))
    if condition == 'stale':
        store.execute("UPDATE host_login_state SET fetched_at=? WHERE server_id='atlas'", (now-500,))
    elif condition == 'paused':
        store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    elif condition == 'changed':
        store.execute("UPDATE servers SET host='changed.example' WHERE id='atlas'")
    elif condition == 'error':
        store.execute("UPDATE servers SET error='offline' WHERE id='atlas'")
    else:
        store.execute("UPDATE host_login_state SET status=? WHERE server_id='atlas'", (json.dumps({'state': 'not_installed'}),))
    data = client.get('/api/bridge').json()
    assert not data['active']
    assert not next(s for s in data['coverage'] if s['id'] == 'atlas')['verified']


def test_unmatched_live_session_tag_has_stable_boot_process_identity(client, history):
    enable(client)
    now = time.time()
    history([], snapshot(now))
    ref = client.get('/api/bridge').json()['active'][0]['ref']
    assert ref.startswith('live:')
    assert client.put('/api/bridge/records/'+ref+'/annotation', json={'tags': ['investigate']}).status_code == 200
    assert client.get('/api/bridge').json()['active'][0]['tags'] == ['investigate']
    newer = snapshot(now+1)
    newer['sessions'][0]['process_started_at'] = now
    history([], newer)
    assert client.get('/api/bridge').json()['active'][0]['ref'] != ref
    saved = client.get('/api/bridge/records/'+ref).json()['record']
    assert saved['tags'] == ['investigate'] and saved['session_status'] == 'unconfirmed'


def test_readonly_and_csrf_and_validation(client, history):
    enable(client)
    now = time.time()
    history([event('authentication', now-5)], snapshot(now))
    ref = client.get('/api/bridge').json()['active'][0]['ref']
    assert client.put('/api/bridge/records/'+ref+'/annotation', json={'tags': ['x'*41]}).status_code == 422
    assert client.put('/api/bridge/records/'+ref+'/identity', json={'label': ' '}).status_code == 422
    assert client.get('/api/bridge/history', params={'sort': 'server_name; drop table servers'}).status_code == 422
    assert client.get('/api/bridge/history', params={'filters': json.dumps({'bad': 'x'})}).status_code == 422
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.put('/api/bridge/settings', json={'enabled': False}).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    client.post('/api/users', json={'name': 'viewer', 'password': 'viewer-password-strong', 'role': 'user'})
    viewer = client.post('/api/login', json={'name': 'viewer', 'password': 'viewer-password-strong'}).json()
    client.headers['X-CSRF-Token'] = viewer['csrf']
    assert client.get('/api/bridge').status_code == 200
    assert client.put('/api/bridge/settings', json={'enabled': False}).status_code == 403
    assert client.put('/api/bridge/records/'+ref+'/annotation', json={'tags': ['x']}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/bridge').status_code == 401
    assert client.get('/api/bridge/stream').status_code == 401


def test_pagination_filters_and_chronological_sort(client, history):
    enable(client)
    now = time.time()
    history([event('authentication', now-i-1, pid=str(i+100)) for i in range(60)], snapshot(now))
    first = client.get('/api/bridge/history', params={'size':25,'sort':'time','order':'asc'}).json()
    second = client.get('/api/bridge/history', params={'size':25,'sort':'time','order':'asc','page':2}).json()
    assert first['pagination']['total'] == 60
    assert len(first['records']) == len(second['records']) == 25
    assert first['records'][-1]['time'] < second['records'][0]['time']
    assert not {r['ref'] for r in first['records']} & {r['ref'] for r in second['records']}


def test_older_poll_cannot_replace_newer_presence(client, history):
    now = time.time()
    history([event('authentication', now-10)], snapshot(now))
    history([], {**snapshot(now-1), 'sessions': []})
    assert client.get('/api/servers/atlas/logins/table').json()['current_sessions']


def test_spool_notifies_only_committed_new_events(tmp_path):
    spool = login_agent.Spool(tmp_path/'notices.db')
    stop = threading.Event()
    first = spool.watch(None, None, stop)
    result = []
    worker = threading.Thread(target=lambda: result.append(spool.watch(first['generation'], first['revision'], stop)))
    worker.start()
    spool.append(event('authentication', time.time()), 'one')
    worker.join(2)
    assert not worker.is_alive() and result[0]['revision'] == first['revision']+1
    assert len(spool.exchange()['events']) == 1
    spool.append(event('authentication', time.time()), 'one')
    assert spool.revision == result[0]['revision']
    before = spool.revision
    with pytest.raises(TypeError):
        spool.append({'bad': set()}, 'invalid')
    assert spool.revision == before


def test_live_fetch_commits_before_ack_and_honours_pause(client, history):
    enable(client)
    now = time.time()
    history([event('authentication', now-10)], snapshot(now))
    server = app.get_server('atlas')
    captured = []
    batch = {'state': 'not_installed'}
    session = SimpleNamespace(signature=logins.connection_signature(server), retired=threading.Event(),
        exchange=lambda row, request, cancel: captured.append(request) or {'logins': batch})
    listener = login_live.Listener(session, server)
    listener.fetch()
    assert captured[0]['operation'] == 'logins' and captured[0]['logins_ack']
    store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    listener.fetch()
    assert len(captured) == 1


def test_logind_parser_excludes_ssh_and_greeters():
    text = '\n\n'.join('Id='+str(i)+'\nName=dragon\nState='+state+'\nClass='+cls+'\nService='+service+'\nType=tty\nTimestamp=Wed 2026-10-14 12:00:00 UTC' for i,state,cls,service in [
        (1,'active','user','login'),(2,'online','user','gdm-password'),(3,'active','user','sshd'),(4,'closing','user','login'),(5,'active','greeter','gdm-launch-environment')])
    rows = remote_probe.parse_os_sessions(text)
    assert [r['session_id'] for r in rows] == ['1', '2']
    assert rows[0]['started_at'] == 1791979200
    assert logins.validate_os_presence(dict(state='ok', checked_at=1791979300, boot_id='boot', sessions=rows))['state'] == 'ok'
    assert logins.validate_os_presence(dict(state='ok', checked_at=1791979100, boot_id='boot', sessions=rows))['state'] == 'unavailable'


def test_snapshot_tags_join_late_authentication_without_pid_reuse(client, history):
    enable(client)
    now = time.time()
    history([], snapshot(now))
    observed = client.get('/api/bridge').json()['active'][0]
    client.put('/api/bridge/records/'+observed['ref']+'/annotation', json={'tags': ['review']})
    history([event('authentication', now-5)], snapshot(now))
    current = client.get('/api/bridge').json()['active'][0]
    assert current['ref'].startswith('session:') and current['tags'] == ['review']
    assert not store.rows('SELECT * FROM bridge_snapshots')
    assert len(client.get('/api/bridge/history').json()['records']) == 1


def test_notifications_do_not_block_exchange_socket(tmp_path):
    import os
    import socket
    import tempfile
    spool = login_agent.Spool(tmp_path/'concurrent.db')
    collector = login_agent.Collector(spool, platform='linux')
    with tempfile.TemporaryDirectory(prefix='hbridge-', dir='/tmp') as directory:
        path = directory+'/s'
        thread = threading.Thread(target=login_agent.serve, args=(collector, path))
        thread.start()
        def query(body):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3); connection.connect(path)
                connection.sendall(json.dumps(body).encode()+b'\n')
                return json.loads(connection.makefile('rb').readline())
        try:
            deadline = time.time()+3
            while not os.path.exists(path) and time.time()<deadline:
                time.sleep(.01)
            notice = query({'operation': 'watch'})
            pending = []
            watcher = threading.Thread(target=lambda: pending.append(query({'operation':'watch', **{k:notice[k] for k in ('generation','revision')}})))
            watcher.start()
            assert query({'operation': 'exchange'})['status']['notifications'] is True
            spool.append(event('authentication', time.time()), 'new')
            watcher.join(2)
            assert pending[0]['revision'] == notice['revision']+1
            assert len(query({'operation': 'exchange'})['events']) == 1
        finally:
            collector.stop.set()
            with spool.changed:
                spool.changed.notify_all()
            thread.join(3)
            assert not thread.is_alive()


def test_live_listener_uses_existing_client_and_cleans_up(client, history, monkeypatch):
    enable(client)
    now = time.time()
    history([event('authentication', now-10)], snapshot(now))
    server = app.get_server('atlas')
    commands, writes, fetched = [], [], []
    class Channel:
        closed = False
        pending = True
        def recv_ready(self): return self.pending
        def recv(self, count): self.pending=False; return b'{"kind":"login-change","revision":1}\n'*3
        def recv_stderr_ready(self): return False
        def exit_status_ready(self): return not self.pending
        def close(self): self.closed=True
    channel = Channel()
    client_ = SimpleNamespace(exec_command=lambda command, **kwargs: commands.append(command) or (
        SimpleNamespace(write=writes.append, flush=lambda:None), SimpleNamespace(channel=channel), None))
    session = SimpleNamespace(signature=logins.connection_signature(server), retired=threading.Event(), client=client_)
    listener = login_live.Listener(session, server)
    monkeypatch.setattr(listener, 'fetch', lambda: fetched.append(True))
    listener.run()
    assert len(commands) == 1 and 'serve_login_notifications' in commands[0]
    assert len(writes) == 1 and b'def serve_login_notifications' in writes[0]
    assert fetched == [True] and channel.closed


def test_cancelled_live_response_never_commits(client, history, monkeypatch):
    enable(client)
    now = time.time()
    history([event('authentication', now-10)], snapshot(now))
    server = app.get_server('atlas')
    session = SimpleNamespace(signature=logins.connection_signature(server), retired=threading.Event())
    listener = login_live.Listener(session, server)
    def exchange(*args):
        listener.close()
        return {'logins': {'state':'not_installed'}}
    session.exchange = exchange
    listener.fetch()
    assert logins.summary(server)['state'] == 'connected'


def test_stream_reauthenticates_and_stops_on_disable(client, monkeypatch):
    import asyncio
    enable(client)
    calls = []
    monkeypatch.setattr(bridge.auth, 'authenticated', lambda request: calls.append(True))
    class Request:
        async def is_disconnected(self): return False
    async def consume():
        response = await bridge.stream(Request(), user={})
        iterator = response.body_iterator
        assert 'event: changed' in await anext(iterator)
        store.execute("UPDATE settings SET value=? WHERE key='bridge'", ('{"enabled":false}',))
        bridge.publish()
        assert 'event: disabled' in await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
    asyncio.run(consume())
    assert len(calls) == 2


def test_identity_label_can_be_managed_after_history_expires(client, history):
    enable(client)
    now = time.time()
    history([event('authentication', now-10)], snapshot(now))
    item = client.get('/api/bridge').json()['active'][0]
    client.put('/api/bridge/records/'+item['ref']+'/identity', json={'label':'Laptop'})
    profile = client.get('/api/bridge/identities').json()['profiles'][0]['id']
    store.execute('DELETE FROM host_login_sessions')
    store.execute('DELETE FROM host_login_events')
    assert client.get('/api/bridge/identities/'+profile).json()['label'] == 'Laptop'
    assert client.put('/api/bridge/identities/'+profile, json={'label':'Retired laptop', 'tags':['retired']}).status_code == 200
    assert client.get('/api/bridge/identities/'+profile).json()['tags'] == ['retired']
