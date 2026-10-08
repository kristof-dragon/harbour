import json

import pytest

from test_harbour import client
from test_resource_settings import dashboard, option, save, server, telegram_settings
from harbour import app, notifications, resources, store


@pytest.fixture(autouse=True)
def isolated_delivery(monkeypatch):
    monkeypatch.setattr(notifications, 'delivery_loop', lambda stop: None)


def patch(client, **body):
    return client.patch('/api/servers/atlas/cards', json=body)


def test_layout_persists_and_overlay_shares_sizes_without_resetting_order(client):
    assert dashboard(client)['card_layout'] == dict(default_size='medium', sizes={}, order=[], positions={})
    fan = next(r['id'] for r in dashboard(client)['metrics']['resources'] if r['unit'] == 'RPM')
    expected = dict(default_size='small', sizes={'cpu': 'large', fan: 'medium'}, order=[fan, 'load', 'cpu'], positions={fan: {'x': .5, 'row': 1}, 'cpu': {'x': 0, 'row': 0}})
    assert patch(client, layout=expected).status_code == 200
    store.initialize()
    assert dashboard(client)['card_layout'] == expected
    assert save(client, [], card_layout={'sizes': {'cpu': 'medium'}}).status_code == 200
    updated = dashboard(client)['card_layout']
    assert updated['order'] == expected['order'] and updated['sizes'][fan] == 'medium'
    assert updated['sizes']['cpu'] == 'medium'
    assert updated['positions'] == expected['positions']
    assert patch(client, layout={'positions': {'cpu': {'x': .25, 'row': 2}}}).status_code == 200
    expected['positions']['cpu'] = {'x': .25, 'row': 2}
    assert patch(client, layout={'default_size': 'large', 'reset_sizes': True}).status_code == 200
    assert dashboard(client)['card_layout'] == dict(default_size='large', sizes={}, order=expected['order'], positions=expected['positions'])
    other = next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] != 'atlas')
    assert other['card_layout']['sizes'] == {} and other['card_layout']['default_size'] == 'medium'


@pytest.mark.parametrize('layout,status', [
    ({'sizes': {'unknown': 'large'}}, 400), ({'order': ['cpu', 'cpu']}, 400),
    ({'order': ['unknown']}, 400), ({'sizes': {'cpu': 'huge'}}, 422),
    ({'positions': {'unknown': {'x': 0, 'row': 0}}}, 400),
    ({'positions': {'cpu': {'x': -1, 'row': 0}}}, 422),
    ({'positions': {'cpu': {'x': 1.1, 'row': 0}}}, 422),
    ({'positions': {'cpu': {'x': 0, 'row': -1}}}, 422),
    ({'positions': {'cpu': {'x': 0, 'row': 4097}}}, 422),
    ({'positions': {'cpu': {'x': 0, 'row': 1.5}}}, 422),
    ({'default_size': 'auto'}, 422), ({'sizes': {'cpu': None}}, 422)])
def test_bad_layout_rejects_resource_changes_atomically(client, layout, status):
    before = server()
    assert patch(client, layout=layout, resources=[option('cpu', card=False)]).status_code == status
    assert server() == before


def test_bad_warning_rejects_layout_and_popup_updates_real_warnings(client):
    before = server()
    assert patch(client, layout={'sizes': {'load': 'small'}}, resources=[option('resource:load1', warn=True)]).status_code == 400
    assert server() == before
    assert patch(client, resources=[option('cpu', warn=True, high=1)]).status_code == 200
    assert any(w['id'] == 'cpu' for w in dashboard(client)['warnings'])
    assert patch(client, resources=[option('cpu', monitor=False, warn=False, card=False)]).status_code == 200
    assert not any(w['id'] == 'cpu' for w in dashboard(client)['warnings'])
    row = next(r for r in dashboard(client)['metrics']['resources'] if r['id'] == 'cpu')
    assert not row['monitor'] and not row['card']


def test_visual_changes_do_not_rearm_alerts_and_monitor_changes_invalidate(client):
    telegram_settings(client, rules=[dict(server_id='atlas', kind='resource', enabled=True, delay_seconds=0, repeat_seconds=0)])
    fan = next(r['id'] for r in dashboard(client)['metrics']['resources'] if r['unit'] == 'RPM')
    choice = option(fan, warn=True, high=1)
    assert patch(client, resources=[choice]).status_code == 200
    notifications.observe(server(), [dict(id=fan, title='Fan', detail='high')], now=1000)
    before = store.one("SELECT entities FROM notification_state WHERE kind='resource'")['entities']
    assert patch(client, layout={'sizes': {fan: 'large'}, 'order': [fan], 'positions': {fan: {'x': .5, 'row': 3}}}, resources=[{**choice, 'card': False}]).status_code == 200
    assert store.one("SELECT entities FROM notification_state WHERE kind='resource'")['entities'] == before
    assert patch(client, resources=[{**choice, 'warn': False}]).status_code == 200
    remaining = store.one("SELECT entities FROM notification_state WHERE kind='resource'")
    assert remaining is None or fan not in json.loads(remaining['entities'])


def test_layout_migration_and_disappeared_sensor_preferences(client):
    fan = next(r['id'] for r in dashboard(client)['metrics']['resources'] if r['unit'] == 'RPM')
    store.execute('UPDATE servers SET resource_catalog=? WHERE id=?', (json.dumps(resources.catalog(server())), 'atlas'))
    assert patch(client, layout={'sizes': {fan: 'large'}, 'order': [fan]}).status_code == 200
    snapshot = json.loads(server()['snapshot']); snapshot['metrics']['hardware'] = []
    store.execute('UPDATE servers SET snapshot=? WHERE id=?', (json.dumps(snapshot), 'atlas'))
    assert patch(client, layout={'order': ['cpu']}).status_code == 200
    assert dashboard(client)['card_layout']['order'] == ['cpu', fan]
    assert dashboard(client)['card_layout']['sizes'][fan] == 'large'
    assert save(client, [option('cpu', card=False)]).status_code == 200
    with store.db() as con:
        con.execute('ALTER TABLE servers DROP COLUMN card_layout')
    store.initialize(); store.initialize()
    assert dashboard(client)['card_layout'] == dict(default_size='medium', sizes={}, order=[], positions={})
    assert not resources.preferences(server())['cpu']['card']


def test_cards_require_admin_csrf_and_respect_server_lock(client):
    lock = app.server_lock('atlas'); lock.acquire()
    try:
        assert patch(client, layout={'default_size': 'small'}).status_code == 409
    finally:
        lock.release()
    token = client.headers.pop('X-CSRF-Token')
    assert patch(client, layout={'default_size': 'small'}).status_code == 403
    client.headers['X-CSRF-Token'] = token
    assert client.post('/api/users', json=dict(name='viewer', password='viewer-password-strong', role='user')).status_code == 200
    viewer = client.post('/api/login', json=dict(name='viewer', password='viewer-password-strong')).json()
    client.headers['X-CSRF-Token'] = viewer['csrf']
    assert patch(client, layout={'default_size': 'small'}).status_code == 403


def test_disk_popover_and_overlay_share_flags_and_pending_alerts(client):
    telegram_settings(client, rules=[dict(server_id='atlas', kind='disk', enabled=True, delay_seconds=0, repeat_seconds=0)])
    notifications.observe(server(), [dict(id='disk:/', title='Disk', detail='full')], now=1000)
    original = store.one("SELECT entities FROM notification_state WHERE kind='disk'")['entities']
    volume = dict(mount='/', monitor=True, warn=True, card=False)
    assert patch(client, layout={'sizes': {'disk': 'small'}}, volumes=[volume]).status_code == 200
    assert store.one("SELECT entities FROM notification_state WHERE kind='disk'")['entities'] == original
    assert not next(d for d in dashboard(client)['metrics']['disks'] if d['mount'] == '/')['card']
    assert save(client, [], volumes=[{**volume, 'warn': False}]).status_code == 200
    assert store.one("SELECT entities FROM notification_state WHERE kind='disk'") is None
    assert dashboard(client)['card_layout']['sizes']['disk'] == 'small'
    before = server()
    assert patch(client, layout={'sizes': {'disk': 'large'}}, volumes=[{**volume, 'monitor': False}]).status_code == 400
    assert server() == before
