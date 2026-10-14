import json
import time

import pytest

from harbour import app, login_agent, login_sessions, logins, remote_probe, store
from test_harbour import client


@pytest.fixture
def history(client, tmp_path):
    store.execute('DELETE FROM host_login_events')
    store.execute('DELETE FROM host_login_sessions')
    store.execute('DELETE FROM host_login_session_cursor')
    spool = login_agent.Spool(tmp_path/'session-spool.db')

    def collect(records, snapshot=None):
        for record in records:
            spool.append(record, 'source:'+str(record['occurred_at'])+':'+record['event_type']+':'+str(record.get('pid'))+':'+str(record.get('boot_id')))
        batch = spool.exchange()
        batch['status'] = {'sources': {'journal': {'state': 'listening'}}}
        if snapshot is not None:
            batch['current_sessions'] = snapshot
        server = app.get_server('atlas')
        logins.collect(server, batch, logins.connection_signature(server), lambda row: row is not None)
        return client.get('/api/servers/atlas/logins?view=sessions').json()
    return collect


def event(kind, at, pid='12', **extra):
    row = dict(occurred_at=at, source='journal', boot_id='aabb-ccdd', pid=pid,
               username='dragon', service='ssh', event_type=kind,
               result={'authentication': 'success', 'session_start': 'opened', 'session_end': 'closed'}[kind],
               method='publickey' if kind == 'authentication' else 'pam')
    if kind == 'authentication':
        row.update(source_ip='10.11.12.5', source_port=65331, key_fingerprint='SHA256:example', credential_verified=True)
    return {**row, **extra}


def snapshot(at, pid='12', **extra):
    return dict(state='ok', boot_id='aabbccdd', checked_at=at, sessions=[
        dict(pid=pid, username='dragon', process_started_at=at-100)], **extra)


def test_one_row_updates_in_place_and_keeps_address_and_original_evidence(client, history):
    now = time.time()
    data = history([event('authentication', now-5), event('session_start', now-4)], snapshot(now))
    assert len(data['events']) == 1
    row = data['events'][0]
    assert row['source_ip'] == '10.11.12.5' and row['session_status'] == 'active'
    assert len(data['current_sessions']) == 1
    identity = row['id']
    data = history([event('session_end', now-1)], {**snapshot(now), 'sessions': []})
    assert len(data['events']) == 1
    row = data['events'][0]
    assert row['id'] == identity and row['session_status'] == 'closed' and row['duration_seconds'] == 3
    assert row['method'] == 'publickey' and row['key_fingerprint'] == 'SHA256:example'
    assert [r['result'] for r in row['evidence_events']] == ['success', 'opened', 'closed']
    assert row['evidence_events'][1]['source_ip'] is None
    assert len(client.get('/api/servers/atlas/logins?view=events').json()['events']) == 3
    assert data['current_sessions'] == []
    assert client.get('/api/servers/atlas/logins?view=sessions&search=10.11.12.5').json()['events'][0]['id'] == identity


@pytest.mark.parametrize('change', ['stale', 'paused', 'error', 'missing', 'invalid'])
def test_missing_or_stale_verification_is_not_an_active_session(client, history, change):
    now = time.time()
    history([event('authentication', now-5), event('session_start', now-4)], snapshot(now))
    if change == 'stale':
        store.execute('UPDATE host_login_state SET fetched_at=?', (now-500,))
    elif change == 'paused':
        store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    elif change == 'error':
        store.execute("UPDATE servers SET error='Host offline' WHERE id='atlas'")
    else:
        history([], None if change == 'missing' else {**snapshot(now), 'sessions': [{'pid': 'bad'}]})
    data = client.get('/api/servers/atlas/logins?view=sessions').json()
    assert data['events'][0]['session_status'] == 'unconfirmed'
    assert not data['session_presence']['available'] and data['current_sessions'] == []


def test_absent_process_and_pid_reuse_do_not_leave_stale_connected_rows(client, history):
    now = time.time()
    history([event('authentication', now-50), event('session_start', now-49)])
    newer = snapshot(now)
    newer['sessions'][0]['process_started_at'] = now-5
    data = history([], newer)
    assert data['events'][0]['session_status'] == 'ended'
    assert data['events'][0]['ended_at'] is None
    assert len(data['current_sessions']) == 1
    assert data['current_sessions'][0]['event_count'] == 0
    assert data['current_sessions'][0]['source_ip'] is None  # Never inherit the old connection's address/key.


def test_boot_change_pid_and_user_boundaries_and_failed_authentication(client, history):
    now = time.time()
    data = history([event('authentication', now-8), event('session_start', now-7),
                    event('authentication', now-6, pid='13'), event('session_start', now-5, pid='13'),
                    event('session_end', now-4, boot_id='another-boot'),
                    event('session_end', now-3, username='someone-else'),
                    event('authentication', now-2, result='failure')], snapshot(now))
    assert len(data['events']) == 5  # Two sessions, two unmatched closes and one failed attempt.
    assert len(data['current_sessions']) == 1
    assert len(client.get('/api/servers/atlas/logins?view=sessions&outcome=failure').json()['events']) == 1
    data = history([], {**snapshot(now), 'boot_id': 'new-boot', 'sessions': []})
    assert all(row['session_status'] == 'ended' for row in data['events'] if row.get('record_type') == 'session')


def test_overlapping_open_records_are_not_guessed_into_one_session(client, history):
    now = time.time()
    data = history([event('session_start', now-5), event('session_start', now-4), event('session_end', now-1)], snapshot(now))
    sessions = [r for r in data['events'] if r.get('record_type') == 'session']
    assert len(sessions) == 2 and all(s['session_status'] == 'unconfirmed' for s in sessions)
    assert data['current_sessions'][0]['event_count'] == 0


def test_harbour_key_filter_applies_to_the_whole_session(client, history, monkeypatch):
    now = time.time()
    monkeypatch.setattr(store, 'DEMO', False)
    key = client.post('/api/keys', json={}).json()
    store.execute("UPDATE servers SET key_id=? WHERE id='atlas'", (key['id'],))
    server = app.get_server('atlas')
    data = history([event('authentication', now-5, username=server['username'], key_fingerprint=logins.key_fingerprint(server)),
                    event('session_start', now-4, username=server['username']), event('session_end', now-1, username=server['username'])])
    assert data['events'][0]['harbour_key'] == 1
    assert client.get('/api/servers/atlas/logins?view=sessions&hide_harbour=true').json()['events'] == []


def test_rebuild_and_retention_are_idempotent(client, history):
    now = time.time()
    history([event('authentication', now-5), event('session_start', now-4), event('session_end', now-1)])
    store.execute('DELETE FROM host_login_sessions')
    store.execute('DELETE FROM host_login_session_cursor')
    store.execute('UPDATE host_login_events SET session_ref=NULL')
    login_sessions.backfill(); login_sessions.backfill()
    rows = client.get('/api/servers/atlas/logins?view=sessions').json()['events']
    assert len(rows) == 1 and rows[0]['event_count'] == 3
    store.execute('UPDATE host_login_events SET collected_at=?', (now-91*86400,))
    store.execute('UPDATE host_login_sessions SET collected_at=?', (now-91*86400,))
    logins.compact()
    assert client.get('/api/servers/atlas/logins?view=sessions').json()['events'] == []


def test_process_snapshot_requires_authenticated_child_and_privileged_parent():
    output = '''749 1 0 Wed Oct 14 10:00:00 2026 sshd sshd: /usr/sbin/sshd -D [listener]
12 749 0 Wed Oct 14 11:30:46 2026 sshd-session sshd-session: dragon [priv]
13 12 1000 Wed Oct 14 11:30:50 2026 sshd-session sshd-session: dragon@notty
14 12 1000 Wed Oct 14 11:30:51 2026 sshd-session sshd-session: dragon@pts/1
15 749 0 Wed Oct 14 11:30:51 2026 sshd-session sshd-session: unknown [priv]
20 1 1000 Wed Oct 14 11:30:51 2026 sshd sshd: fake [priv]
21 20 1000 Wed Oct 14 11:30:52 2026 sshd sshd: fake@notty
30 1 0 Wed Oct 14 11:30:46 2026 /usr/sbin/sshd sshd: macuser [priv]
31 30 501 Wed Oct 14 11:30:50 2026 /usr/sbin/sshd sshd: macuser@ttys001'''
    rows = remote_probe.ssh_session_processes(output)
    assert [(r['pid'], r['username']) for r in rows] == [('12', 'dragon'), ('30', 'macuser')]
    assert rows[0]['process_started_at'] == 1791977446


def test_unavailable_snapshot_does_not_block_collector_history(monkeypatch):
    monkeypatch.setattr(remote_probe, 'collected_login_events', lambda _: {'state': 'not_installed'})
    monkeypatch.setattr(remote_probe, 'current_ssh_sessions', lambda: {'state': 'unavailable'})
    assert remote_probe.login_events()['state'] == 'not_installed'
