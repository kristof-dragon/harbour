import json
import threading

import pytest

from test_harbour import client
from test_telegram import settings as telegram_settings
from harbour import app, history, notifications, remote_probe, resources, store


@pytest.fixture(autouse=True)
def isolated_delivery(monkeypatch):
    monkeypatch.setattr(notifications, 'delivery_loop', lambda stop: None)


def server():
    return app.get_server('atlas')


def metrics():
    return json.loads(server()['snapshot'])['metrics']


def dashboard(client):
    return next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] == 'atlas')


def option(identity, **patch):
    return dict(id=identity, monitor=True, warn=False, card=True, limit_mode='custom', low=None, high=None) | patch


def save(client, choices, **patch):
    body = dict(name='Atlas', server_type='docker', thresholds=None, volumes=[], resources=choices,
                enabled=True, poll_seconds=None) | patch
    return client.put('/api/servers/atlas/settings', json=body)


def test_discovery_defaults_and_existing_warning_thresholds(client):
    result = dashboard(client)
    rows = {r['id']: r for r in result['metrics']['resources']}
    assert {'cpu', 'memory', 'resource:load1', 'temperature:demo:cpu'} <= rows.keys()
    assert rows['cpu']['monitor'] and rows['cpu']['card'] and rows['cpu']['warn']
    assert rows['cpu']['high'] == store.DEFAULTS['cpu']
    fan = next(r for r in rows.values() if r['unit'] == 'RPM')
    assert fan['card'] and fan['monitor'] and not fan['warn'] and fan['low'] is None
    assert save(client, [option(fan['id'], warn=True, low=1300, high=6000)]).status_code == 200
    result = dashboard(client)
    assert any(w['id'] == fan['id'] for w in result['warnings'])
    assert next(r for r in result['metrics']['resources'] if r['id'] == fan['id'])['warning']
    assert save(client, [option(fan['id'], warn=False, low=1300, high=6000, card=False)]).status_code == 200
    assert not any(w['id'] == fan['id'] for w in dashboard(client)['warnings'])
    store.initialize()
    assert not next(r for r in dashboard(client)['metrics']['resources'] if r['id'] == fan['id'])['card']


@pytest.mark.parametrize('patch', [dict(monitor=False, warn=True, low=1), dict(monitor=False, card=True),
    dict(warn=True), dict(low=5, high=5), dict(low=6, high=5), dict(id='unknown')])
def test_invalid_resource_settings_reject_entire_save(client, patch):
    before = server()
    choice = option('cpu', **patch)
    assert save(client, [choice], name='Must not save').status_code == 400
    assert server() == before


def test_limits_validate_finite_duplicates_and_discovered_only(client):
    assert save(client, [option('cpu'), option('cpu')]).status_code == 400
    assert save(client, [option('cpu', high='Infinity')]).status_code == 422
    assert save(client, [option('cpu', low='NaN')]).status_code == 422
    response = save(client, [option('cpu', limit_mode='default', warn=True, high=2)], thresholds={**store.DEFAULTS, 'cpu': 97})
    assert response.status_code == 200
    assert next(r for r in dashboard(client)['metrics']['resources'] if r['id'] == 'cpu')['high'] == 97


def test_monitor_disables_future_history_but_retains_discovery_and_old_readings(client):
    store.execute('DELETE FROM resource_history')
    m = metrics()
    fan = resources.hardware_id(m['hardware'][0])
    stamp = 1800000000
    history.record('atlas', m, up=True, now=stamp)
    identities = ['cpu', 'memory', 'resource:load1', 'temperature:demo:cpu', fan]
    assert save(client, [option(id_, monitor=False, warn=False, card=False) for id_ in identities]).status_code == 200
    history.record('atlas', m, up=True, now=stamp+60)
    points = history.series('atlas', 1, now=stamp+70, requested_resolution=60)['points']
    assert points[0]['cpu'] is not None and points[0]['memory'] is not None
    assert points[1]['cpu'] is None and points[1]['memory'] is None and points[1]['load1'] is None
    assert points[1]['load5'] is not None
    assert 'demo:cpu' not in {s['id'] for s in points[1]['sensors']}
    assert m['hardware'][0]['id'] not in {s['id'] for s in points[1]['hardware']}
    assert m['cpu'] is not None and m['memory'] is not None  # original snapshot was not mutated
    assert all(next(r for r in dashboard(client)['metrics']['resources'] if r['id'] == id_)['present'] for id_ in identities)
    rolled = history.series('atlas', 1, now=stamp+70, requested_resolution=300)['points'][0]
    assert rolled['memory'] == points[0]['memory']  # no dilution with disabled readings


def test_missing_resource_survives_discovery_with_preferences_and_no_false_zero(client, monkeypatch):
    m = metrics()
    fan_id = resources.hardware_id(m['hardware'][0])
    assert save(client, [option(fan_id, warn=True, low=1300)]).status_code == 200
    monkeypatch.setattr(store, 'DEMO', False)
    m['hardware'] = []
    monkeypatch.setattr(app.ssh, 'request', lambda *a, **k: {'metrics': m, 'latency_ms': 1})
    lock = threading.Lock(); lock.acquire()
    app.poll_resources(server(), lock, raise_errors=True)
    missing = next(r for r in dashboard(client)['metrics']['resources'] if r['id'] == fan_id)
    assert missing['value'] is None and not missing['present'] and not missing['warning']
    assert missing['low'] == 1300 and missing['card']
    assert save(client, [option(fan_id, monitor=False, warn=False, card=False)]).status_code == 200


def test_thresholds_both_directions_zero_and_signed_current(client):
    m = metrics()
    current = remote_probe.hardware_reading('battery:current', 'Battery current', -2, 'A', 'battery')
    m['hardware'] = [current]
    snapshot = json.loads(server()['snapshot']); snapshot['metrics'] = m
    store.execute('UPDATE servers SET snapshot=? WHERE id=?', (json.dumps(snapshot), 'atlas'))
    identity = resources.hardware_id(current)
    assert save(client, [option(identity, warn=True, low=-2, high=0)]).status_code == 200
    warning = next(w for w in dashboard(client)['warnings'] if w['id'] == identity)
    assert 'at or below -2 A' in warning['detail']
    snapshot['metrics']['hardware'][0]['value'] = 0
    store.execute('UPDATE servers SET snapshot=? WHERE id=?', (json.dumps(snapshot), 'atlas'))
    assert 'at or above 0 A' in next(w for w in dashboard(client)['warnings'] if w['id'] == identity)['detail']


def test_per_entity_notification_gap_does_not_rearm_or_block_other_resources(client, monkeypatch):
    telegram_settings(client, rules=[dict(server_id='atlas', kind='resource', enabled=True, delay_seconds=60, repeat_seconds=0)])
    sent = []
    monkeypatch.setattr(notifications, 'send_message', lambda config, text: sent.append(text))
    def observe(at, values, missing=()):
        store.execute("UPDATE servers SET checked=?,connection_status='up' WHERE id='atlas'", (at,))
        notifications.observe(server(), [dict(id=id_, title=id_, detail='low') for id_ in values], now=at, unavailable=missing)
        notifications.deliver_one(at)
    observe(1000, ['resource:fan']); observe(1060, ['resource:fan'])
    assert len(sent) == 1
    observe(1120, ['resource:battery'], ['resource:fan'])
    state = json.loads(store.one("SELECT entities FROM notification_state WHERE kind='resource'")['entities'])
    assert state['resource:fan']['sent'] and state['resource:fan']['since'] is None
    observe(1180, ['resource:battery'], ['resource:fan'])
    assert len(sent) == 2 and 'resource:battery' in sent[-1]
    observe(1240, ['resource:battery', 'resource:fan']); observe(1300, ['resource:battery', 'resource:fan'])
    assert len(sent) == 2
    observe(1360, ['resource:battery'])  # actual recovery re-arms only the fan
    observe(1420, ['resource:fan']); observe(1480, ['resource:fan'])
    assert len(sent) == 3


def test_warning_edit_cancels_pending_delivery_without_resetting_other_resources(client):
    telegram_settings(client, rules=[dict(server_id='atlas', kind='resource', enabled=True, delay_seconds=0, repeat_seconds=0)])
    ids = [resources.hardware_id(s) for s in metrics()['hardware']]
    for id_ in ids:
        assert save(client, [option(id_, warn=True, high=1)]).status_code == 200
    notifications.observe(server(), [dict(id=id_, title=id_, detail='high') for id_ in ids], now=1000)
    assert save(client, [option(ids[0], warn=False, high=1)]).status_code == 200
    state = json.loads(store.one("SELECT entities FROM notification_state WHERE kind='resource'")['entities'])
    assert set(state) == {ids[1]}
    assert save(client, [option(ids[1], warn=True, high=1, card=False)]).status_code == 200
    assert json.loads(store.one("SELECT entities FROM notification_state WHERE kind='resource'")['entities']) == state


def test_sensor_unit_change_gets_new_preferences(client):
    sensor = metrics()['hardware'][0]
    old = resources.hardware_id(sensor)
    assert save(client, [option(old, warn=True, low=1300)]).status_code == 200
    sensor = {**sensor, 'unit': '%'}
    new = resources.hardware_id(sensor)
    assert new != old
    assert resources.options(server(), dict(id=new, group='hardware'), store.DEFAULTS)['warn'] is False


def test_migration_adds_resource_columns_without_losing_existing_settings(client):
    assert save(client, [option('cpu', card=False)]).status_code == 200
    store.initialize(); store.initialize()
    assert not resources.preferences(server())['cpu']['card']
    with store.db() as con:
        con.execute('ALTER TABLE servers DROP COLUMN resource_settings')
        con.execute('ALTER TABLE servers DROP COLUMN resource_catalog')
    store.initialize()
    assert server()['resource_settings'] == '{}' and server()['volume_settings'] == '{}'


def test_first_poll_after_upgrade_retains_previous_discoveries():
    sensor = remote_probe.hardware_reading('fan0', 'Fan', 1400, 'RPM', 'hwmon')
    old = {'snapshot': json.dumps({'metrics': {'hardware': [sensor]}}), 'resource_catalog': '{}'}
    known = resources.catalog(old, {'hardware': []})
    assert resources.hardware_id(sensor) in known
    assert 'value' not in known[resources.hardware_id(sensor)]
