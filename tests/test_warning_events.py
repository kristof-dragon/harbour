import json
import time

import pytest

from test_harbour import client
from test_resource_settings import option, save
from test_resource_recorder import poll_batch, sample
from harbour import app, notifications, resource_agent, store, warning_events as events


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(notifications, 'delivery_loop', lambda stop: None)


def start_history():
    store.execute("UPDATE settings SET value='0' WHERE key='warning_history_started_at'")


def reading(value, at, server_id='atlas', missing=False):
    start_history()
    server = app.get_server(server_id)
    metrics = json.loads(server['snapshot'])['metrics']
    metrics['measured_at'] = at
    metrics['temperature']['sensors'] = [] if missing else [dict(id='demo:cpu', label='CPU package', celsius=value, kind='cpu_package')]
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        events.observe_resources(con, server, metrics, app.thresholds_for(server), at)


def temperature_events(client, **params):
    response = client.get('/api/warning-events', params={'view': 'occurrences', 'search': 'Temperature', **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_spike_resolves_offline_and_survives_login_restart(client):
    # No dashboard requests while the incident starts, peaks and recovers.
    reading(82, 1000); reading(94, 1060); reading(60, 1120)
    assert client.post('/api/logout').status_code == 200
    store.initialize()
    login = client.post('/api/login', json={'name': 'admin', 'password': 'test-password-strong'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    event, = temperature_events(client)['events']
    assert event['status'] == 'resolved' and event['closed_at'] == 1120
    assert event['started_at'] == 1000 and event['last_seen'] == 1060
    assert event['initial_values']['value'] == 82 and event['extreme_values']['value'] == 94
    assert event['definition']['high'] == 80 and event['acknowledged_at'] is None
    assert client.get('/api/dashboard').json()['warning_events']['unacknowledged'] >= 1
    assert store.rows('SELECT * FROM notification_state') == []  # Telegram is disabled.


def test_acknowledgement_is_per_user_non_destructive_and_recurrence_is_new(client):
    reading(90, 1000)
    first = temperature_events(client)['events'][0]['id']
    assert client.put(f'/api/warning-events/{first}/acknowledgement', json={}).status_code == 200
    reading(95, 1060); reading(50, 1120); reading(96, 1180)
    latest, prior = temperature_events(client)['events']
    assert prior['acknowledged_at'] and latest['acknowledged_at'] is None
    assert latest['id'] != prior['id'] and prior['extreme_values']['value'] == 95
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-long', 'role': 'user'})
    login = client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-long'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    assert all(e['acknowledged_at'] is None for e in temperature_events(client)['events'])
    assert client.put(f'/api/warning-events/{first}/acknowledgement', json={}).status_code == 200
    assert len(temperature_events(client)['events']) == 2
    assert client.put(f'/api/warning-events/{first}/acknowledgement', json={'acknowledged': False}).status_code == 200
    assert temperature_events(client)['events'][1]['acknowledged_at'] is None
    token = client.headers.pop('X-CSRF-Token')
    assert client.put(f'/api/warning-events/{first}/acknowledgement', json={}).status_code == 403
    client.headers['X-CSRF-Token'] = token
    assert client.put('/api/warning-events/999999/acknowledgement', json={}).status_code == 404
    client.cookies.clear()
    assert client.get('/api/warning-events').status_code == 401


def test_unknown_failed_duplicate_and_out_of_order_readings_do_not_resolve_or_reopen(client):
    reading(90, 1000); reading(0, 1060, missing=True)
    with store.db() as con:
        events.observe_connection(con, app.get_server('atlas'), 1120, 'SSH timed out')
    event, = temperature_events(client)['events']
    assert event['status'] == 'active' and event['last_seen'] == 1000 and event['observation_gap']
    reading(100, 1000); reading(98, 999); reading(50, 1200); reading(99, 1199)
    event, = temperature_events(client)['events']
    assert event['status'] == 'resolved' and event['extreme_values']['value'] == 90


def test_low_limits_store_minimum_and_threshold_changes_are_not_recovery(client):
    now = time.time()
    assert save(client, [option('temperature:demo:cpu', warn=True, low=30, high=80)]).status_code == 200
    reading(20, now + 10); reading(12, now + 20)
    event = temperature_events(client)['events'][0]
    assert event['definition']['side'] == 'low' and event['extreme_values']['value'] == 12
    assert save(client, [option('temperature:demo:cpu', warn=True, low=10, high=85)]).status_code == 200
    old = temperature_events(client)['events'][0]
    assert old['close_reason'] == 'settings_changed' and old['status'] == 'ended'
    assert old['definition']['low'] == 30
    # A historical batch cannot be interpreted using newly changed thresholds.
    count = len(temperature_events(client)['events'])
    reading(99, now - 20)
    assert len(temperature_events(client)['events']) == count


def test_layout_changes_preserve_episode_and_pausing_is_explicit(client):
    reading(90, time.time())
    first = temperature_events(client)['events'][0]['id']
    assert client.patch('/api/servers/atlas/cards', json={'layout': {'sizes': {'cpu': 'small'}}}).status_code == 200
    assert temperature_events(client)['events'][0]['id'] == first
    assert temperature_events(client)['events'][0]['status'] == 'active'
    assert save(client, [], enabled=False).status_code == 200
    assert temperature_events(client)['events'][0]['close_reason'] == 'monitoring_paused'


def test_recorder_batch_catches_resolved_spike_once(client, tmp_path, monkeypatch):
    start_history()
    spool = resource_agent.Spool(tmp_path / 'warning-recorder.db')
    now = time.time()
    for i, value in enumerate([50, 91, 99, 55]):
        metrics = sample(now - 240 + i * 60, uptime=1000 + i * 60)
        metrics['temperature'] = {'sensors': [dict(id='probe', label='NVMe', celsius=value, kind='cpu_package')]}
        spool.add(metrics)
    batch = spool.exchange()
    poll_batch(monkeypatch, batch); poll_batch(monkeypatch, batch)
    rows = client.get('/api/warning-events', params={'search': 'NVMe'}).json()['events']
    event, = rows
    assert event['started_at'] == now - 180 and event['closed_at'] == now - 60
    assert event['extreme_values']['value'] == 99 and event['status'] == 'resolved'
    assert event['detected_at'] >= now


def test_warning_storage_failure_rolls_back_recorder_ack(client, tmp_path, monkeypatch):
    start_history()
    spool = resource_agent.Spool(tmp_path / 'warning-recorder.db'); spool.add(sample())
    monkeypatch.setattr(events, 'observe_resources', lambda *a: (_ for _ in ()).throw(RuntimeError('warning storage failed')))
    with pytest.raises(RuntimeError, match='warning storage failed'):
        poll_batch(monkeypatch, spool.exchange())
    assert store.one('SELECT * FROM resource_delivery') is None
    assert spool.exchange()['pending'] == 1


def test_inventory_and_connection_events_keep_resource_warnings_separate(client):
    start_history(); server = app.get_server('atlas')
    with store.db() as con:
        events.observe_inventory(con, server, {'services': [dict(id='web', name='Web', state='running', health='unhealthy')]}, 1000)
        events.observe_inventory(con, server, {}, 1060, 'Docker unavailable')
    service = client.get('/api/warning-events', params={'search': 'Web'}).json()['events'][0]
    assert service['status'] == 'active' and service['observation_gap']
    with store.db() as con:
        events.observe_inventory(con, server, {'services': [dict(id='web', name='Web', state='running', health='healthy')]}, 1120)
    assert client.get('/api/warning-events', params={'search': 'Web'}).json()['events'][0]['status'] == 'resolved'


def test_filters_pagination_and_bounded_retention(client):
    for i in range(4):
        reading(90, 1000 + i * 120); reading(50, 1060 + i * 120)
    page = temperature_events(client, limit=2, status='resolved')
    older = temperature_events(client, limit=2, before=page['next_before'])
    assert len(page['events']) == len(older['events']) == 2
    assert not set(e['id'] for e in page['events']) & set(e['id'] for e in older['events'])
    assert temperature_events(client, server_id='other')['events'] == []
    assert client.get('/api/warning-events', params={'search': "%' OR 1=1 --"}).json()['events'] == []
    assert client.get('/api/warning-events', params={'limit': 101}).status_code == 422
    reading(90, 1600)
    events.prune(now=1600 + 100 * 86400)
    event, = temperature_events(client)['events']
    assert event['status'] == 'active'


def test_bulk_ack_includes_resolved_warnings_and_restore_keeps_history(client):
    reading(90, 1000); reading(50, 1060)
    assert client.post('/api/dismiss-all').status_code == 200
    assert temperature_events(client, acknowledgement='unacknowledged')['events'] == []
    assert temperature_events(client)['events'][0]['status'] == 'resolved'
    assert client.delete('/api/dismissals').status_code == 200
    assert temperature_events(client, acknowledgement='unacknowledged')['events'] == []
    assert len(temperature_events(client, visibility='visible')['events']) == 1


def test_no_invented_history_before_installation_or_future_samples(client):
    server = app.get_server('atlas'); now = time.time()
    with store.db() as con:
        events.observe(con, server, 'cpu', 'resource', now - 3600, True, 'CPU', received_at=now)
        events.observe(con, server, 'cpu', 'resource', now + 10000, True, 'CPU', received_at=now)
    assert client.get('/api/warning-events').json()['events'] == []


def test_multibatch_recorder_backlog_is_not_skipped_by_latest_preview(client, tmp_path, monkeypatch):
    start_history(); now = time.time()
    spool = resource_agent.Spool(tmp_path / 'long-backlog.db')
    for i in range(205):
        metrics = sample(now - (205-i)*15, uptime=1000+i*15)
        metrics['temperature'] = {'sensors': [dict(id='probe', label='Late spike', celsius=96 if i == 202 else 50, kind='cpu_package')]}
        spool.add(metrics)
    batch = spool.exchange(); assert len(batch['samples']) < 205
    while batch['samples']:
        poll_batch(monkeypatch, batch)
        ack = json.loads(store.one('SELECT acknowledgement FROM resource_delivery')['acknowledgement'])
        batch = spool.exchange(ack)
    event, = client.get('/api/warning-events', params={'search': 'Late spike'}).json()['events']
    assert event['status'] == 'resolved' and event['extreme_values']['value'] == 96


def test_cached_disk_readings_do_not_extend_or_resolve_warning(client):
    start_history(); server = app.get_server('atlas')
    metrics = json.loads(server['snapshot'])['metrics']
    metrics.update(measured_at=1000, disk_measured_at=1000)
    metrics['disks'][0].update(percent=95, free=1e9)
    with store.db() as con:
        events.observe_resources(con, server, metrics, app.thresholds_for(server), 1000)
        metrics['measured_at'] = 1060
        metrics['disks'][0].update(percent=20, free=100e9)
        events.observe_resources(con, server, metrics, app.thresholds_for(server), 1060)
    event = client.get('/api/warning-events', params={'search': 'Disk space'}).json()['events'][0]
    assert event['status'] == 'active' and event['last_seen'] == 1000
    metrics.update(measured_at=1120, disk_measured_at=1120)
    with store.db() as con:
        events.observe_resources(con, server, metrics, app.thresholds_for(server), 1120)
    assert client.get('/api/warning-events', params={'search': 'Disk space'}).json()['events'][0]['status'] == 'resolved'


def test_global_threshold_change_ends_inherited_episode_without_rewriting_limits(client):
    reading(90, time.time())
    assert client.put('/api/thresholds', json={**store.DEFAULTS, 'temperature': 100}).status_code == 200
    event, = temperature_events(client)['events']
    assert event['status'] == 'ended' and event['definition']['high'] == 80
    assert event['close_reason'] == 'settings_changed'


def test_reconfiguration_blocks_old_samples_even_before_first_observation(client):
    start_history(); server = app.get_server('atlas'); now = time.time()
    assert store.rows('SELECT * FROM warning_observations WHERE server_id=?', ('atlas',)) == []
    with store.db() as con:
        events.invalidate(con, 'atlas', at=now)
        events.observe(con, server, 'new-sensor', 'resource', now - 10, True, 'New sensor', received_at=now)
    assert client.get('/api/warning-events').json()['events'] == []
    with store.db() as con:
        events.observe(con, server, 'new-sensor', 'resource', now + 10, True, 'New sensor', received_at=now + 10)
    assert len(client.get('/api/warning-events').json()['events']) == 1


def test_newly_discovered_sensor_backfill_does_not_use_new_global_limits(client):
    start_history(); now = time.time()
    store.execute("UPDATE servers SET snapshot='{}',resource_catalog='{}' WHERE id='atlas'")
    assert client.put('/api/thresholds', json={**store.DEFAULTS, 'temperature': 70}).status_code == 200
    server = app.get_server('atlas')
    metrics = {'measured_at': now - 60, 'temperature': {'sensors': [dict(id='new', label='New temperature', celsius=75, kind='cpu_package')]}}
    with store.db() as con:
        events.observe_resources(con, server, metrics, app.thresholds_for(server), now)
    assert client.get('/api/warning-events').json()['events'] == []
    metrics['measured_at'] = now + 60
    with store.db() as con:
        events.observe_resources(con, server, metrics, app.thresholds_for(server), now + 60)
    event, = client.get('/api/warning-events').json()['events']
    assert event['definition']['high'] == 70 and event['status'] == 'active'


def test_existing_backlog_is_one_warning_with_retained_occurrence_evidence(client):
    start_history(); server = app.get_server('atlas')
    with store.db() as con:
        for i in range(450):
            at = 1000 + i * 120
            events.observe(con, server, 'temperature:nvme', 'resource', at, True, 'NVMe temperature',
                           definition={'high': 65, 'unit': '°C'}, values={'value': 70 + i % 5}, received_at=at)
            events.observe(con, server, 'temperature:nvme', 'resource', at + 60, False, received_at=at + 60)
    store.initialize()  # Existing records need no destructive migration.
    page = client.get('/api/warning-events').json()
    warning, = page['events']
    assert page['unacknowledged'] == 1 and page['next_before'] is None
    assert warning['occurrence_count'] == 450 and warning['first_seen'] == 1000
    assert warning['status'] == 'resolved' and warning['started_at'] == 1000 + 449 * 120
    assert client.get('/api/dashboard').json()['warning_events']['unacknowledged'] == 1
    evidence = client.get('/api/warning-events', params={'view': 'occurrences', 'server_id': 'atlas',
                                                       'warning_key': 'temperature:nvme', 'limit': 100}).json()
    assert len(evidence['events']) == 100 and evidence['next_before']
    assert 'occurrence_count' not in evidence['events'][0]
    assert client.put(f"/api/warning-events/{warning['id']}/acknowledgement", json={}).status_code == 200
    assert client.get('/api/warning-events?acknowledgement=unacknowledged').json()['events'] == []
    assert store.one('SELECT COUNT(*) n FROM warning_events')['n'] == 450
    assert store.one('SELECT COUNT(*) n FROM warning_acknowledgements')['n'] == 450


def test_group_acknowledgement_preserves_new_recurrences_and_other_users(client):
    reading(90, 1000); reading(50, 1060); reading(95, 1120); reading(50, 1180)
    before = temperature_events(client, view='warnings')['events'][0]
    reading(91, 1240)
    client.put(f"/api/warning-events/{before['id']}/acknowledgement", json={})
    group, = temperature_events(client, view='warnings', acknowledgement='unacknowledged')['events']
    assert group['occurrence_count'] == 3 and group['status'] == 'active'
    assert group['id'] != before['id'] and group['acknowledged_at'] is None
    client.put(f"/api/warning-events/{group['id']}/acknowledgement", json={})
    assert temperature_events(client, view='warnings', acknowledgement='unacknowledged')['events'] == []
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-long', 'role': 'user'})
    login = client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-long'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    assert len(temperature_events(client, view='warnings', acknowledgement='unacknowledged')['events']) == 1
    client.put(f"/api/warning-events/{group['id']}/acknowledgement?scope=occurrence", json={})
    assert len(temperature_events(client, acknowledgement='unacknowledged')['events']) == 2
    # Older unseen evidence still makes this one unacknowledged warning.
    assert len(temperature_events(client, view='warnings', acknowledgement='unacknowledged')['events']) == 1


def test_grouping_precedes_status_filters_and_pagination_and_separates_hosts(client):
    start_history(); server = app.get_server('atlas')
    with store.db() as con:
        for i in range(5):
            for at, active in [(1000, True), (1060, False), (1120, True), (1180, False)]:
                events.observe(con, server, f'sensor:{i}', 'resource', at, active, f'Sensor {i}', received_at=at)
        events.observe(con, server, 'sensor:0', 'resource', 1240, True, 'Sensor 0', received_at=1240)
        other = dict(server, id='boreal', name='Other server')
        # Use a real second fixture host rather than combining identical labels.
        other['id'] = store.one("SELECT id FROM servers WHERE id<>'atlas' LIMIT 1")['id']
        events.observe(con, other, 'sensor:0', 'resource', 1240, True, 'Sensor 0', received_at=1240)
    active = client.get('/api/warning-events?status=active').json()['events']
    assert len(active) == 2 and len({e['server_id'] for e in active}) == 2
    resolved = client.get('/api/warning-events?status=resolved').json()['events']
    assert len(resolved) == 4 and all(e['warning_key'] != 'sensor:0' for e in resolved)
    seen = set(); before = None
    while True:
        params = {'limit': 2, **({'before': before} if before else {})}
        page = client.get('/api/warning-events', params=params).json()
        for warning in page['events']:
            identity = (warning['server_id'], warning['warning_key'])
            assert identity not in seen
            seen.add(identity)
        before = page['next_before']
        if before is None:
            break
    assert len(seen) == 6


def test_warning_and_update_actions_are_independent_and_restore_keeps_acknowledgement(client):
    reading(90, 1000); reading(50, 1060)
    start_history()
    with store.db() as con:
        events.observe_connection(con, app.get_server('atlas'), 1120, 'Unavailable')
    update_count = sum(s['updates'] for s in client.get('/api/dashboard').json()['servers'])
    assert update_count > 0
    result = client.post('/api/dismiss-all?kind=warnings')
    assert result.status_code == 200 and result.json()['dismissed'] >= 2
    assert client.get('/api/warning-events').json()['unacknowledged'] == 0
    assert len(client.get('/api/warning-events?visibility=visible').json()['events']) == 2
    assert sum(s['updates'] for s in client.get('/api/dashboard').json()['servers']) == update_count
    assert client.post('/api/hide-warnings').status_code == 200
    assert client.get('/api/warning-events?visibility=visible').json()['events'] == []
    client.post('/api/dismiss-all?kind=updates')
    assert sum(s['updates'] for s in client.get('/api/dashboard').json()['servers']) == 0
    client.delete('/api/dismissals?kind=warnings')
    restored = client.get('/api/warning-events?visibility=visible').json()['events']
    assert len(restored) == 2 and all(w['acknowledged_at'] for w in restored)
    assert client.get('/api/warning-events').json()['unacknowledged'] == 0
    assert sum(s['updates'] for s in client.get('/api/dashboard').json()['servers']) == 0
    client.post('/api/hide-warnings')
    client.delete('/api/dismissals?kind=updates')
    assert sum(s['updates'] for s in client.get('/api/dashboard').json()['servers']) == update_count
    assert client.get('/api/warning-events?visibility=visible').json()['events'] == []
    assert client.get('/api/warning-events').json()['unacknowledged'] == 0
    assert client.post('/api/dismiss-all?kind=invalid').status_code == 422
    assert client.delete('/api/dismissals?kind=invalid').status_code == 422
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/dismiss-all?kind=warnings').status_code == 403
    assert client.post('/api/hide-warnings').status_code == 403
    assert client.delete('/api/dismissals?kind=updates').status_code == 403


def test_visibility_does_not_acknowledge_and_only_recurrence_reactivates(client):
    reading(90, 1000)
    event = temperature_events(client, view='warnings')['events'][0]
    path = f"/api/warning-events/{event['id']}"
    # Removing alone must not silence an unacknowledged warning.
    assert client.put(path + '/visibility', json={'hidden': True}).status_code == 200
    assert temperature_events(client, view='warnings', visibility='visible')['events'] == []
    assert temperature_events(client, view='warnings')['unacknowledged'] == 1
    assert client.put(path + '/visibility', json={'hidden': False}).status_code == 200
    assert client.put(path + '/acknowledgement', json={}).status_code == 200
    reading(99, 1060)
    listed, = temperature_events(client, view='warnings', visibility='visible')['events']
    assert listed['acknowledged_at'] and listed['status'] == 'active'
    assert listed['occurrence_count'] == 1
    client.put(path + '/visibility', json={'hidden': True})
    store.initialize()
    client.delete('/api/dismissals?kind=warnings')
    assert temperature_events(client, visibility='visible')['events'][0]['acknowledged_at']
    assert temperature_events(client)['unacknowledged'] == 0
    client.put(path + '/visibility', json={'hidden': True})
    reading(50, 1120); reading(96, 1180)
    recurring, = temperature_events(client, view='warnings', visibility='visible')['events']
    assert recurring['occurrence_count'] == 2 and recurring['acknowledged_at'] is None
    assert recurring['hidden_at'] is None and recurring['status'] == 'active'
    assert temperature_events(client)['unacknowledged'] == 1
    assert client.put('/api/warning-events/999999/visibility', json={}).status_code == 404
    client.headers.pop('X-CSRF-Token')
    assert client.put(path + '/visibility', json={}).status_code == 403


def test_visibility_and_acknowledgement_are_personal_for_current_warnings(client):
    server = next(s for s in client.get('/api/dashboard').json()['servers'] if s['warnings'])
    warning = server['warnings'][0]
    body = {'server_id': server['id'], 'warning_id': warning['id']}
    assert client.post('/api/current-warning', json={**body, 'action': 'acknowledge'}).status_code == 200
    assert client.post('/api/current-warning', json={**body, 'action': 'hide'}).status_code == 200
    current = next(w for s in client.get('/api/dashboard').json()['servers'] if s['id'] == server['id'] for w in s['warnings'] if w['id'] == warning['id'])
    assert current['dismissed'] and current['hidden']
    client.delete('/api/dismissals?kind=updates')
    assert store.rows("SELECT * FROM dismissals WHERE service_id LIKE 'hidden-warning:%'")
    client.delete('/api/dismissals?kind=warnings')
    current = next(w for s in client.get('/api/dashboard').json()['servers'] if s['id'] == server['id'] for w in s['warnings'] if w['id'] == warning['id'])
    assert current['dismissed'] and not current.get('hidden')
    client.post('/api/current-warning', json={**body, 'action': 'hide'})
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-long', 'role': 'user'})
    login = client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-long'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    current = next(w for s in client.get('/api/dashboard').json()['servers'] if s['id'] == server['id'] for w in s['warnings'] if w['id'] == warning['id'])
    assert not current['dismissed'] and not current.get('hidden')
