"""Synthetic incident evidence; no live routers or network probes."""
import copy
import json
import subprocess
import sys
import time

import pytest

from harbour import incidents, network, network_incidents, store
from test_harbour import client
from test_openwrt import enable


def probe(at=1000, source='recorder', target='192.0.2.1', status='reply', rtt_ms=200, **extra):
    return {'at': at, 'kind': 'probe', 'source': source, 'target': target, 'status': status,
            'rtt_ms': rtt_ms, 'run_id': 'run-one', 'timing_quality': 'kernel_receive', **extra}


def telemetry(at=1000, **extra):
    return {'at': at, 'kind': 'telemetry', 'run_id': 'run-one', 'collection_ms': 50,
            'metrics': {'cpu': 95, 'boot_id': 'boot-one', 'memory': {'percent': 30},
                        'openwrt': {'cpu_percent': {'cpu0': 99}, 'sample_seconds': 1}}, **extra}


def bundle(rows=None, at=1000, identity='event-one', **extra):
    return {'schema': 1, 'server_name': 'Example router', 'server_id': 'example',
            'incident': {'id': identity, 'measured': at, 'automatic': 1, 'complete': 1, 'label': 'Probe degraded'},
            'samples': rows if rows is not None else [probe(), telemetry()], 'truncated': False,
            'settings': {'latency_limit_ms': 999},
            'runs': [{'id': 'run-one', 'config': {'latency_limit_ms': 150, 'interval': 1}}], **extra}


def codes(report):
    return {f['code'] for f in report['findings']}


def test_run_thresholds_trigger_refs_and_before_after():
    data = bundle([probe(999, rtt_ms=20), probe(), telemetry(), probe(1001, rtt_ms=30)])
    original = copy.deepcopy(data)
    report = incidents.analyze(data)
    assert data == original
    assert report['trigger']['verification'] == 'reconstructed'
    assert report['trigger']['evidence']['latency_limit_ms'] == 150
    assert report['trigger']['evidence_refs'] == [1]
    assert codes(report) == {'probe_degradation', 'cpu_pressure'}
    assert report['paths'][0]['high_latency'] == 1
    assert report['paths'][0]['before']['p95_ms'] == 20
    assert report['paths'][0]['after']['p95_ms'] == 200
    for evidence in report['evidence']:
        assert evidence['sample'] == data['samples'][evidence['ref']]
    assert report['root_cause'] == 'not_established'


def test_late_replies_duplicates_uncertain_and_gaps_are_not_extra_losses():
    rows = [probe(status='timeout', rtt_ms=None, sequence=1),
            probe(status='late_reply', rtt_ms=2300, sequence=1),
            probe(1001, rtt_ms=9000, timing_quality='uncertain', sequence=2),
            probe(1002, status='unobserved', rtt_ms=None, sequence=3),
            {'at': 1002, 'kind': 'gap', 'detail': 'Collector delayed'}, probe(1003, rtt_ms=10, sequence=4)]
    rows.append(copy.deepcopy(rows[0]))
    report = incidents.analyze(bundle(rows))
    p = report['paths'][0]
    assert (p['attempts'], p['usable_attempts'], p['timeouts'], p['timeout_percent']) == (4, 2, 1, 50)
    assert p['uncertain'] == 2 and p['late_replies'] == 1 and p['peak_ms'] == 10
    assert report['coverage']['duplicates_removed'] == 1
    assert report['coverage']['kinds']['gap'] == 1
    assert any('uncertain' in q for q in report['quality'])


@pytest.mark.parametrize('change', [{'at': 1010}, {'run_id': 'other-run'}, {'run_id': ''},
                                  {'collection_ms': 2500}, {'metrics': {'cpu': 100, 'openwrt': {'sample_seconds': 10}}}])
def test_correlations_require_nearby_same_run_and_timely_telemetry(change):
    assert 'cpu_pressure' not in codes(incidents.analyze(bundle([probe(), telemetry(**change)])))


def test_path_comparison_requires_matching_target_and_run():
    r = incidents.analyze(bundle([probe(), probe(source='router', rtt_ms=5),
                                  probe(source='router', target='198.51.100.1'),
                                  probe(source='router', run_id='other')]))
    assert 'router_reply_nearby' in codes(r)
    assert 'router_path_degradation' not in codes(r)


def test_multiple_run_settings_and_missing_metadata():
    data = bundle([probe(), probe(1001, run_id='run-two', rtt_ms=200)])
    data['runs'].append({'id': 'run-two', 'config': {'latency_limit_ms': 500}})
    report = incidents.analyze(data)
    assert report['paths'][0]['high_latency'] == 1
    assert report['paths'][0]['thresholds_ms'] == [150, 500]
    missing = incidents.analyze(bundle([probe()], runs=[]))
    assert missing['trigger']['verification'] == 'unavailable'
    assert any('fallback' in q for q in missing['quality'])


def test_missing_invalid_and_empty_evidence_stays_unknown():
    data = bundle([{}, None, {'at': float('nan'), 'kind': 'probe'}, probe(500),
                   {'at': 1000, 'kind': 'telemetry', 'metrics': {'openwrt': {'interfaces': {'wan': None}, 'stations': [None]}}}], truncated=True)
    data['incident']['complete'] = 0
    report = incidents.analyze(data)
    assert not report['paths']
    assert not report['findings']
    assert report['coverage']['invalid_samples'] == 3
    assert report['coverage']['outside_window'] == 1
    assert any('truncated' in q for q in report['quality'])
    empty = incidents.analyze(bundle([]))
    assert empty['coverage']['first_at'] is None
    assert not empty['paths']
    with pytest.raises(ValueError):
        incidents.analyze({'schema': 2})
    with pytest.raises(ValueError):
        incidents.analyze(bundle([probe()] * (incidents.MAX_SAMPLES+1)))


def test_resource_rules_use_interval_deltas_and_selected_client():
    t = telemetry()
    ow = t['metrics']['openwrt']
    ow.update(softirq_percent={'cpu0': 60}, queue_stats=[{'discipline': 'fq_codel', 'backlog_bytes': 90000}],
              interfaces={'wan': {'rx_errors_delta': 2}}, radio_stats={'phy0': {'busy_percent': 90}},
              conntrack='95\n100', stations=[{'mac': 'aa', 'tx_failed_delta': 1}])
    t['metrics']['memory']['percent'] = 95
    report = incidents.analyze(bundle([probe(), t]))
    assert {'cpu_pressure', 'softirq_pressure', 'queue_backlog', 'interface_errors', 'radio_busy',
            'conntrack_pressure', 'wifi_retries', 'memory_pressure'} <= codes(report)
    data = bundle([probe(), t])
    data['runs'][0]['config']['client_mac'] = 'bb'
    ow['interfaces']['wan'] = {'rx_errors': 12345, 'rx_errors_delta': None}
    ow['queue_stats'][0] = {'discipline': 'noqueue', 'backlog_bytes': 90000}
    report = incidents.analyze(data)
    assert not {'interface_errors', 'wifi_retries', 'queue_backlog'} & codes(report)


def test_loss_counters_reboot_and_log_receipt_evidence():
    t2 = telemetry(at=1001)
    t2['metrics']['boot_id'] = 'boot-two'
    health = [{'at': 999+i, 'kind': 'recorder_health', 'run_id': run, 'dropped_before_storage': loss}
              for i, (run, loss) in enumerate([('a', 100), ('a', 103), ('b', 20), ('b', 21)])]
    report = incidents.analyze(bundle([telemetry(999), t2, *health,
        {'at': 1002, 'kind': 'log', 'line': 'kernel: link down', 'timestamp_basis': 'receipt'}]))
    assert 'router_reboot' in codes(report) and 'router_log_event' in codes(report)
    assert report['coverage']['dropped_in_observed_intervals'] == 4
    assert any('4 additional' in q for q in report['quality'])
    assert not incidents.analyze(bundle(health[:1]))['coverage']['dropped_in_observed_intervals']


def test_patterns_deduplicate_exports_and_group_overlapping_windows():
    a = incidents.analyze(bundle())
    b = incidents.analyze(bundle([probe(1300), telemetry(1300)], at=1300, identity='two'))
    c = incidents.analyze(bundle([probe(3000), telemetry(3000)], at=3000, identity='three'))
    duplicate = copy.deepcopy(a)
    duplicate['server_name'] = 'Renamed router'
    result = incidents.patterns([a, b, c, duplicate])
    assert result['incident_count'] == 3
    assert result['window_clusters'] == 2
    cpu = next(p for p in result['patterns'] if p['key'] == 'finding:cpu_pressure')
    assert cpu['incidents'] == 3 and cpu['window_clusters'] == 2
    b['server_id'] = 'another'
    assert incidents.patterns([a, b])['window_clusters'] == 2


def sealed_event(at=None):
    data = bundle(at=at or time.time()-400)
    for r in data['samples']:
        r['at'] = data['incident']['measured']
    event = network.mark('atlas', 'Example incident', automatic=True, measured=data['incident']['measured'])
    store.execute('UPDATE network_events SET archive=?,complete=1 WHERE id=?', (json.dumps(data), event))
    return event


def test_api_uses_sealed_archive_after_raw_pruning_caches_and_exports(client, monkeypatch):
    enable(client, monkeypatch)
    event = sealed_event()
    base = '/api/servers/atlas/network/incidents'
    data = client.get(base).json()
    assert data['total'] == 1 and data['reports'][0]['incident']['id'] == event
    assert store.one('SELECT * FROM network_analysis WHERE event_id=?', (event,))
    with monkeypatch.context() as patch:
        patch.setattr(incidents, 'analyze', lambda _: pytest.fail('Sealed summary should be cached'))
        assert client.get(base).status_code == 200
    detail = client.get(base+'/'+event).json()
    assert detail['evidence'] and detail['paths'][0]['high_latency'] == 1
    original = client.get('/api/servers/atlas/network/export/'+event).json()
    assert incidents.analyze(original) == detail
    download = client.get(base+'?download=true')
    assert 'attachment' in download.headers['content-disposition']
    assert download.json()['patterns']['incident_count'] == 1
    assert client.get(base+'/'+event+'?download=true').status_code == 200
    store.execute('DELETE FROM network_events WHERE id=?', (event,))
    assert not store.one('SELECT * FROM network_analysis WHERE event_id=?', (event,))
    assert client.get(base+'/'+event).status_code == 404


def test_api_open_window_reanalysis_filters_limits_and_permissions(client, monkeypatch):
    server, signature, revision = enable(client, monkeypatch)
    at = time.time()
    event = network.mark('atlas', 'Manual example', measured=at)
    base = '/api/servers/atlas/network/incidents'
    assert client.get(base+'/'+event).json()['coverage']['samples'] == 0
    network.persist('atlas', [probe(at)], {}, signature, revision)
    data = client.get(base+'/'+event).json()
    assert data['coverage']['samples'] == 1 and data['trigger']['mode'] == 'manual'
    assert not store.one('SELECT * FROM network_analysis WHERE event_id=?', (event,))
    sealed_event(at-500)
    assert client.get(base+'?kind=automatic').json()['total'] == 1
    assert client.get(base+f'?since={at-50}').json()['total'] == 1
    first = client.get(base+'?limit=1').json()
    assert first['has_more'] and first['total'] == 2
    assert client.get(base+'?limit=1&offset=1').json()['reports'][0]['incident']['id'] != event
    for query in ('limit=201', 'offset=-1', 'kind=bad', 'since=nan', 'since=5&until=4'):
        assert client.get(base+'?'+query).status_code == 422
    assert client.get('/api/servers/boreal/network/incidents/'+event).status_code in (400, 404)
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-strong', 'role': 'user'})
    login = client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-strong'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    assert client.get(base).status_code == 200 and client.get(base+'/'+event).status_code == 200
    client.cookies.clear()
    assert client.get(base).status_code == 401


def test_recorded_trigger_cooldown_and_replay_are_atomic(client, monkeypatch):
    server, signature, revision = enable(client, monkeypatch)
    at = time.time()-400
    cfg = network.config('atlas')[0]
    run = {'id': 'run-one', 'started': at, 'config': cfg, 'observer': 'example'}
    rows = [probe(at, rtt_ms=cfg['latency_limit_ms']), probe(at+1, timing_quality='uncertain'),
            probe(at+2, source='router'), probe(at+3)]
    assert network.persist('atlas', rows, {}, signature, revision, run, 1)
    assert network.persist('atlas', rows, {}, signature, revision, run, 1)
    events = store.rows('SELECT * FROM network_events')
    assert len(events) == 1 and events[0]['measured'] == at+3
    trigger = json.loads(events[0]['trigger'])
    assert trigger['target'] == '192.0.2.1' and trigger['reason'] == 'latency'
    network.persist('atlas', [probe(at+302)], {}, signature, revision, run, 2)
    assert len(store.rows('SELECT * FROM network_events')) == 1
    network.persist('atlas', [probe(at+303)], {}, signature, revision, run, 3)
    assert len(store.rows('SELECT * FROM network_events')) == 2
    network.maintain('atlas')
    result = client.get('/api/servers/atlas/network/incidents/'+events[0]['id']).json()
    assert result['trigger']['verification'] == 'recorded'
    assert result['trigger']['evidence'] == trigger
    from test_network_archives import contents
    archived = contents(store.one('SELECT * FROM network_archives'))
    assert next(row for row in archived if row['type'] == 'incident' and row['id'] == events[0]['id'])['trigger'] == trigger


def test_corrupt_incident_is_reported_without_hiding_other_reports(client, monkeypatch):
    enable(client, monkeypatch)
    event = sealed_event()
    store.execute('UPDATE network_events SET archive=? WHERE id=?', ('{bad', event))
    sealed_event()
    result = client.get('/api/servers/atlas/network/incidents').json()
    assert len(result['reports']) == len(result['errors']) == 1
    assert client.get('/api/servers/atlas/network/incidents/'+event).status_code == 422


def test_cli_multiple_exports_and_bad_inputs(tmp_path):
    first, second = tmp_path/'one.json', tmp_path/'two.json'
    first.write_text(json.dumps(bundle()))
    second.write_text(json.dumps(bundle([probe(2000)], at=2000, identity='two')))
    result = subprocess.run([sys.executable, '-m', 'harbour.incidents', str(first), str(second)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed['patterns']['incident_count'] == 2
    second.write_text('{"schema": NaN}')
    invalid = subprocess.run([sys.executable, '-m', 'harbour.incidents', str(second)], capture_output=True, text=True)
    assert invalid.returncode == 2 and 'Non-finite' in invalid.stderr


def test_probe_process_restart_does_not_deduplicate_reused_sequences():
    rows = [probe(1000, sequence=1, sample_key='run:probe:first:1'),
            probe(1001, sequence=1, sample_key='run:probe:restart:1'),
            probe(1002, sequence=1), probe(1003, sequence=1)]
    report = incidents.analyze(bundle([*rows, rows[0]]))
    assert report['paths'][0]['attempts'] == 4
    assert report['coverage']['duplicates_removed'] == 1


def test_schema_upgrade_preserves_existing_incidents(client, monkeypatch):
    enable(client, monkeypatch)
    event = sealed_event()
    with store.db() as con:
        con.execute('DROP TABLE network_analysis')
        con.execute('ALTER TABLE network_events DROP COLUMN trigger')
    network.initialize()
    network.initialize()
    result = client.get('/api/servers/atlas/network/incidents/'+event)
    assert result.status_code == 200 and result.json()['trigger']['verification'] == 'reconstructed'


def test_cache_changes_with_analyzer_version_and_archive_contents(client, monkeypatch):
    enable(client, monkeypatch)
    event = sealed_event()
    base = '/api/servers/atlas/network/incidents'
    assert client.get(base).json()['reports'][0]['paths']
    archive = json.loads(store.one('SELECT archive FROM network_events WHERE id=?', (event,))['archive'])
    archive['samples'] = []
    store.execute('UPDATE network_events SET archive=? WHERE id=?', (json.dumps(archive), event))
    assert not client.get(base).json()['reports'][0]['paths']
    monkeypatch.setattr(incidents, 'VERSION', 2)
    assert client.get(base).json()['reports'][0]['analyzer_version'] == 2


@pytest.mark.parametrize('invalid', ['NaN', 'Infinity', '1e999'])
def test_file_ingestor_rejects_nonfinite_numbers(tmp_path, invalid):
    source = tmp_path/'invalid.json'
    source.write_text('{"value":'+invalid+'}')
    with pytest.raises(ValueError, match='Non-finite'):
        incidents.read_bundle(source)
