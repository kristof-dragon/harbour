"""Current OS presence is independent of old login records and SSH visibility."""
import plistlib
import subprocess
import time
from types import SimpleNamespace

import pytest

from harbour import app, bridge, login_agent, logins, remote_probe, store
from test_harbour import client
from test_login_sessions import snapshot


def mac_user(uid=501, name='dragon', audit=100003, session=257, **extra):
    return {'kCGSessionLoginDoneKey': True, 'kCGSSessionUserIDKey': uid,
        'kCGSSessionAuditIDKey': audit, 'kCGSSessionIDKey': session,
        'kCGSSessionUserNameKey': name, 'kCGSSessionOnConsoleKey': True, **extra}


def test_macos_includes_locked_and_switched_out_users_but_not_loginwindow():
    rows = remote_probe.parse_macos_sessions({'IOConsoleUsers': [mac_user(),
        mac_user(502, 'second-user', 100004, 258, kCGSSessionOnConsoleKey=False, CGSSessionScreenIsLocked=True),
        {'kCGSessionLoginDoneKey': False, 'kCGSSessionUserNameKey': 'loginwindow'}]})
    assert len(rows) == 2
    assert rows[1]['locked'] is True and rows[1]['on_console'] is False
    assert rows[0]['locked'] is None  # A missing lock hint is not proof of an unlocked screen.
    assert rows[0]['presence_id'] == 'audit:100003'
    assert all(r['started_at'] is None and r['source_ip'] is None and r.get('key_fingerprint') is None for r in rows)
    assert remote_probe.parse_macos_sessions([{'IOConsoleUsers': []}]) == []


@pytest.mark.parametrize('data', [{}, [], [{'IOConsoleUsers': []}, {'IOConsoleUsers': []}],
    {'IOConsoleUsers': [mac_user(kCGSessionLoginDoneKey='true')]},
    {'IOConsoleUsers': [mac_user(kCGSSessionAuditIDKey=None)]},
    {'IOConsoleUsers': [mac_user(), mac_user()]},
    {'IOConsoleUsers': [mac_user(kCGSSessionUserNameKey='')]},
    {'IOConsoleUsers': [mac_user(kCGSSessionIDKey=True)]},
    {'IOConsoleUsers': [mac_user()]*129}])
def test_macos_unrecognised_or_partial_registry_is_not_an_empty_verified_list(data):
    with pytest.raises(ValueError):
        remote_probe.parse_macos_sessions(data)


@pytest.mark.parametrize('failure', [None, 'missing', 'permission', 'timeout', 'oversize'])
def test_macos_probe_uses_readonly_bounded_commands(monkeypatch, failure):
    commands = []
    monkeypatch.setattr(remote_probe.sys, 'platform', 'darwin')
    def run(command, **options):
        commands.append(command)
        assert options['timeout'] == 3
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 3)
        return SimpleNamespace(returncode=1 if failure=='permission' else 0,
            stdout=b'x'*2_000_001 if failure=='oversize' else plistlib.dumps({} if failure=='missing' else {'IOConsoleUsers': [mac_user()]}))
    monkeypatch.setattr(remote_probe.subprocess, 'run', run)
    monkeypatch.setattr(remote_probe, 'run', lambda command, **options: 'boot-1234')
    result = remote_probe.current_os_sessions()
    assert commands == [['/usr/sbin/ioreg', '-n', 'Root', '-d', '1', '-a']]
    assert result['state'] == ('unavailable' if failure else 'ok')
    if not failure:
        assert result['scope'] == 'macos-console' and result['sessions'][0]['username'] == 'dragon'
        clean = logins.validate_os_presence(result)
        assert clean['source'] == 'macOS console registry' and clean['sessions'][0]['presence_id'] == 'audit:100003'


@pytest.fixture
def collect(client, tmp_path):
    client.put('/api/bridge/settings', json={'enabled': True})
    spool = login_agent.Spool(tmp_path/'desktop.db')
    def send(users=None, ssh=True, state=None):
        now = time.time()
        server = app.get_server('atlas')
        batch = spool.exchange()
        batch.update(status={'sources': {'native': {'state': 'listening'}}},
            current_sessions=snapshot(now) if ssh else {'state': 'unavailable'},
            os_sessions={'state': 'ok', 'scope': 'macos-console', 'boot_id': 'boot-1234', 'checked_at': now,
                'sessions': remote_probe.parse_macos_sessions({'IOConsoleUsers': [mac_user()] if users is None else users})})
        if state:
            batch['state'] = state
        logins.collect(server, batch, logins.connection_signature(server), lambda row: row is not None)
        return client.get('/api/bridge').json()
    return send


def test_fleet_and_per_server_include_ssh_and_desktop_sessions(client, collect):
    result = collect()
    current = [r for r in result['active'] if r['server_id']=='atlas']
    assert {r['service'] for r in current} == {'ssh', 'graphical-login'}
    desktop = next(r for r in current if r['service']=='graphical-login')
    assert desktop['started_at'] is None and desktop.get('duration_seconds') is None
    assert desktop['presence_source'] == 'macOS console registry'
    server = client.get('/api/servers/atlas/logins/table').json()
    assert {r['ref'] for r in server['current_sessions']} == {r['ref'] for r in current}
    assert server['session_presence']['available'] and server['session_presence']['os_available']
    assert server['session_presence']['os_source'] == 'macOS console registry'
    coverage = next(r for r in result['coverage'] if r['id']=='atlas')
    assert coverage['os_verified'] and coverage['os_source'] == 'macOS console registry'


def test_desktop_presence_survives_unavailable_ssh_process_visibility(client, collect):
    result = collect(ssh=False)
    assert [r['service'] for r in result['active']] == ['graphical-login']
    server = client.get('/api/servers/atlas/logins/table').json()
    assert not server['session_presence']['available'] and server['session_presence']['os_available']
    assert len(server['current_sessions']) == 1


@pytest.mark.parametrize('condition', ['stale', 'paused', 'host-error', 'changed', 'no-collector'])
def test_unverified_desktop_presence_never_looks_connected(client, collect, condition):
    collect()
    if condition == 'stale':
        store.execute("UPDATE host_login_state SET fetched_at=? WHERE server_id='atlas'", (time.time()-500,))
    elif condition == 'paused':
        store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    elif condition == 'host-error':
        store.execute("UPDATE servers SET error='unreachable' WHERE id='atlas'")
    elif condition == 'changed':
        store.execute("UPDATE servers SET host='different.example' WHERE id='atlas'")
    else:
        collect(state='not_installed')
    data = client.get('/api/bridge').json()
    assert not data['active']
    assert not next(r for r in data['coverage'] if r['id']=='atlas')['os_verified']
    per_server = client.get('/api/servers/atlas/logins/table').json()
    assert not per_server['session_presence']['os_available']
    assert not any(r['service']=='graphical-login' for r in per_server['current_sessions'])


def test_desktop_tags_survive_lock_switch_and_logout_without_session_id_reuse(client, collect):
    first = next(r for r in collect()['active'] if r['service']=='graphical-login')
    ref = first['ref']
    assert client.put('/api/bridge/records/'+ref+'/annotation', json={'tags':['review']}).status_code == 200
    locked = next(r for r in collect([mac_user(kCGSSessionOnConsoleKey=False, CGSSessionScreenIsLocked=True)])['active'] if r['service']=='graphical-login')
    assert locked['ref'] == ref and locked['tags'] == ['review']
    assert locked['session_status'] == 'active' and locked['on_console'] is False
    newer = next(r for r in collect([mac_user(audit=100008)])['active'] if r['service']=='graphical-login')
    assert newer['ref'] != ref and not newer['tags']
    collect([])
    retained = client.get('/api/bridge/records/'+ref).json()['record']
    assert retained['session_status'] == 'unconfirmed' and retained['tags'] == ['review']
    assert not retained.get('ended_at') and not retained.get('duration_seconds')


def test_linux_console_and_desktop_lock_hints():
    output = 'Id=1\nName=one\nState=active\nClass=user\nService=login\nType=tty\nLockedHint=no\nActive=yes\n\nId=2\nName=two\nState=online\nClass=user\nService=gdm-password\nType=wayland\nLockedHint=yes\nActive=no'
    rows = remote_probe.parse_os_sessions(output)
    assert rows[0]['session_type'] == 'console' and rows[0]['locked'] is False
    assert rows[1]['session_type'] == 'desktop' and rows[1]['locked'] is True and rows[1]['on_console'] is False


def test_os_presence_validation_rejects_untrusted_flags_and_scope():
    value = dict(state='ok', scope='macos-console', boot_id='boot', checked_at=time.time(),
        sessions=remote_probe.parse_macos_sessions({'IOConsoleUsers':[mac_user()]}))
    assert logins.validate_os_presence(value)['state'] == 'ok'
    assert logins.validate_os_presence({**value, 'scope':'unknown'})['state'] == 'unavailable'
    value['sessions'][0]['locked'] = 'false'
    assert logins.validate_os_presence(value)['state'] == 'unavailable'
