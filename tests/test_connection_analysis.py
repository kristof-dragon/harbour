"""Offline synthetic conclusions and authenticated gzip handoffs; no network traffic."""
import copy
import gzip
import json
import time

import pytest
from harbour import connection_analysis as analysis, network_diagnostics as api, store
from test_harbour import client
from test_call_evidence import fixture as call_fixture
from test_incidents import probe, telemetry, bundle

SERVER = {'id': 'example', 'name': 'Example router', 'server_type': 'openwrt'}


def fixture(duration=20, losses=None, bad_ping=()):
    losses = losses or {}
    config = {'endpoint': '192.0.2.1', 'duration': duration, 'profile': 'fixture', 'port': 5201,
              'upload_kbps': 1000, 'download_kbps': 1000, 'baseline': 10, 'recovery': 10}
    settings = {'interval': 1, 'latency_limit_ms': 150, 'targets': ['192.0.2.1'], 'router_probes': True}
    samples = [probe(990+i, rtt_ms=200 if 990+i in bad_ping else 5) for i in range(duration+20)]
    for at in range(990, 1010+duration):
        m = telemetry(at)
        m['metrics'].update(cpu=95 if at in bad_ping else 5)
        m['metrics']['openwrt'].update(cpu_percent={}, routes='default via 192.0.2.254 dev wan', rules='main')
        samples.append(m)
    test = {'id': 'test-one', 'server_id': SERVER['id'], 'created': 990, 'status': 'complete', 'config': config,
            'result': {'load_started': 1000, 'load_finished': 1000+duration, 'directions': {}}}
    remote, timeline = {'records': []}, []
    for name in ('upload', 'download'):
        intervals = []
        for n in range(duration):
            stats = {'start': n, 'end': n+1, 'sender': False, 'packets': 100, 'lost_packets': 2 if n in losses.get(name, []) else 0,
                     'lost_percent': 2 if n in losses.get(name, []) else 0, 'jitter_ms': 3, 'bits_per_second': 1000000}
            intervals.append({'sum': stats})
            timeline.append({'direction': name, 'interval_index': n, 'start': 1000+n, 'end': 1001+n, 'uncertainty_seconds': .02})
        raw = {'extra_data': 'harbour:test-one:'+name, 'start': {'cookie': name}, 'intervals': intervals,
               'end': {'sum_sent': {'sender': True, 'bits_per_second': 1000000, 'seconds': duration}}}
        summary = {'packets': duration*100, 'lost_percent': 0, 'jitter_ms': 3}
        test['result']['directions'][name] = {'raw': copy.deepcopy(raw), 'summary': summary}
        remote['records'].append({'test_id': test['id'], 'direction': name, 'raw': raw})
    evidence = {'window': {'start': 990, 'end': 1010+duration}, 'remote': remote,
                'local': {'samples': samples, 'settings': settings, 'runs': [{'id': 'run-one', 'observer': 'test-host', 'config': settings}]},
                'analysis': {'timeline': timeline, 'paths': [], 'quality': []}}
    return test, evidence


def result(test, evidence):
    return analysis.analyze_call(test, evidence, SERVER)


def all_refs(value):
    if isinstance(value, dict):
        for key, v in value.items():
            if key == 'evidence_refs':
                yield from v
            else:
                yield from all_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from all_refs(v)


def test_normal_tests_have_covered_controls_and_stable_comparisons():
    t, e = fixture()
    original = copy.deepcopy(e)
    r = result(t, e)
    assert r['comparison_complete'] and not r['symptoms_observed'] and r['cohort']
    assert r['endpoint_ping_coverage'] == 1
    assert r['probe_summary'][0]['usable_attempts'] == 40
    assert r['probe_summary'][0]['evidence_refs']
    assert r['root_cause'] == 'not_established' and e == original
    assert 'Neither test' in r['conclusion']
    assert set(all_refs(r)) <= r['evidence'].keys()
    assert analysis.patterns([r])[0]['healthy_comparisons'] == 1


@pytest.mark.parametrize('change', ['missing', 'clustered', 'uncertain', 'cancelled', 'gap', 'unaligned', 'wrong_target', 'zero_packets', 'health'])
def test_missing_evidence_never_produces_a_healthy_comparison(change):
    t, e = fixture()
    rows = e['local']['samples']
    if change == 'missing': e['local']['samples'] = rows[:1]
    elif change == 'clustered':
        e['local']['samples'] = [probe(1000+n*.001, rtt_ms=5) for n in range(100)]
    elif change == 'uncertain':
        for row in rows: row['timing_quality'] = 'uncertain'
    elif change == 'cancelled': t['status'] = 'cancelled'
    elif change == 'gap': rows.append({'at': 1001, 'kind': 'gap', 'run_id': 'run-one'})
    elif change == 'unaligned':
        for row in e['analysis']['timeline']: row['uncertainty_seconds'] = 5
    elif change == 'wrong_target':
        for row in rows:
            if row['kind'] == 'probe': row['target'] = '198.51.100.1'
    elif change == 'zero_packets':
        for interval in e['remote']['records'][0]['raw']['intervals']: interval['sum']['packets'] = 0
    elif change == 'health': rows.append({'at': 1001, 'kind': 'health', 'run_id': 'run-one', 'probe_online': False})
    r = result(t, e)
    assert not r['comparison_complete'] and 'Neither test' not in r['conclusion']


@pytest.mark.parametrize('bad_ping,losses,expected', [
    ((1005,), {'upload': [5]}, 'both'), ((), {'download': [5]}, 'udp_only'),
    ((1005,), {}, 'ping_only'),
])
def test_classification_and_separate_directional_episodes(bad_ping, losses, expected):
    t, e = fixture(losses=losses, bad_ping=bad_ping)
    r = result(t, e)
    assert r['episodes'][0]['classification'] == expected
    assert r['episodes'][0]['sampled_span_seconds'] == 1
    assert set(all_refs(r)) <= r['evidence'].keys()
    if losses:
        affected = next(d for d in r['directions'] if d['direction'] in losses)
        assert affected['affected_seconds'] == 1 and affected['impairment_observed']
        assert len(affected['evidence_refs']) > 1  # Spike remains evidenced despite a normal final summary.


def test_unrelated_ping_and_unaligned_receiver_are_not_corroboration():
    t, e = fixture(losses={'upload': [5]})
    e['local']['samples'].append(probe(1005, target='198.51.100.1'))
    r = result(t, e)
    assert r['episodes'][0]['classification'] == 'udp_only'
    assert any('other destinations' in s for p in r['path_findings'] for s in p['conclusions'])
    for i in e['analysis']['timeline']: i['uncertainty_seconds'] = 5
    r = result(t, e)
    assert all(not ep['directions'] for ep in r['episodes'])
    assert r['directions'][0]['affected_seconds'] == 1 and r['directions'][0]['unaligned_intervals'] == 20


def test_router_association_controls_and_reboot_before_after_evidence():
    t, e = fixture(bad_ping=(1005,))
    r = result(t, e)
    cpu = next(f for f in r['contributors'] if f['code'] == 'cpu')
    assert cpu['counts']['affected_hits'] == 1 and cpu['counts']['control_total'] > 0
    assert cpu['control_fraction'] == 0 and cpu['assessment'] == 'Possible contributor'
    for row in e['local']['samples']:
        if row['kind'] == 'telemetry' and row['at'] >= 1005: row['metrics']['boot_id'] = 'boot-two'
    reboot = next(f for f in result(t, e)['contributors'] if f['code'] == 'reboot')
    assert len(reboot['evidence_refs']) == 2
    for row in e['local']['samples']:
        if row['kind'] == 'telemetry': row['collection_ms'] = 3000
    assert not any(f['code'] == 'cpu' for f in result(t, e)['contributors'])


def test_no_context_no_cohort_and_different_profiles_or_routes_do_not_merge():
    t, e = fixture()
    r = result(t, e)
    e['local']['runs'][0]['observer'] = ''
    assert result(t, e)['cohort'] is None
    t2, e2 = fixture(); t2['config']['upload_kbps'] = 500
    r2 = result(t2, e2)
    assert len(analysis.patterns([r, r2])) == 2
    duplicate = copy.deepcopy(r); duplicate['id'] = 'duplicate'
    p = analysis.patterns([r, duplicate])[0]
    assert p['tests'] == 1 and p['overlapping_tests_excluded'] == 1
    e['local']['runs'][0]['observer'] = 'test-host'
    e['local']['runs'].append({**e['local']['runs'][0], 'id': 'run-two'})
    assert result(t, e)['cohort'] == r['cohort']


def test_phase_delay_and_sender_shortfall_are_possible_not_causes():
    t, e = fixture()
    e['analysis']['paths'] = [{'source': 'recorder', 'target': '192.0.2.1', 'phase': phase,
                              'usable_attempts': 10, 'p95_ms': delay, 'timeouts': 0}
                             for phase, delay in [('baseline', 5), ('load', 80), ('recovery', 6)]]
    t['result']['directions']['upload']['raw']['end']['sum_sent']['bits_per_second'] = 100000
    r = result(t, e)
    assert r['load_response'][0]['recovered'] and r['load_response'][0]['p95_rise_ms'] == 75
    assert r['delivery'][0]['sender_shortfall'] and 'cannot be attributed' in r['delivery'][0]['conclusion']


def test_ping_incident_and_malformed_rows_stay_explicit():
    b = bundle([None, {'at': float('nan'), 'kind': 'probe'}, probe(), telemetry(metrics={'openwrt': {'interfaces': {'wan': None}, 'stations': [None]}})])
    r = analysis.analyze_incident(b, SERVER)
    assert r['episodes'][0]['classification'] == 'ping_unconfirmed'
    assert r['evidence_selection']['invalid'] == 2 and r['root_cause'] == 'not_established'
    assert not r['comparison_complete']


def test_api_saved_handoff_hashes_retention_and_no_remote_fetch(client, monkeypatch):
    from harbour import call_evidence
    identity, remote = call_fixture(client)
    monkeypatch.setattr(call_evidence.call_endpoint, 'fetch', lambda *args: json.dumps(remote).encode())
    assert client.post('/api/servers/atlas/network/call-tests/'+identity+'/collect').status_code == 200
    def forbidden(*args): raise AssertionError('Analysis must remain offline')
    monkeypatch.setattr(call_evidence.call_endpoint, 'fetch', forbidden)
    base = '/api/servers/atlas/network/analysis'
    listing = client.get(base).json()
    assert len(listing['reports']) == 1 and listing['pending'] == 0
    detail = client.get(base+'/call/'+identity)
    assert detail.status_code == 200
    download = client.get(base+'/download')
    assert download.status_code == 200 and download.headers['content-type'].startswith('application/gzip')
    assert int(download.headers['content-length']) == len(download.content)
    data = json.loads(gzip.decompress(download.content))
    assert data['kind'] == 'harbour-connection-analysis-handoff' and data['reader_guide'] and data['conclusion_brief']
    entry = data['reports'][0]
    assert entry['report_sha256'] == analysis.digest(entry['report'])
    assert set(all_refs(entry['report'])) <= entry['report']['evidence'].keys()
    assert 'private-connection-signature' not in json.dumps(data)
    store.execute('DELETE FROM network_call_tests WHERE id=?', (identity,))
    assert client.get(base+'/call/'+identity).json() == detail.json()
    assert client.get(base+'/call/'+identity+'?download=true').status_code == 200
    monkeypatch.setattr(api.tempfile, 'TemporaryFile', lambda **kwargs: (_ for _ in ()).throw(OSError('full')))
    assert client.get(base+'/download').status_code == 507
    assert client.get(base+'?until=nan').status_code == 422
    assert client.get('/api/servers/forge/network/analysis').status_code in (400,404)
    client.cookies.clear()
    assert client.get(base).status_code == 401 and client.get(base+'/download').status_code == 401


def test_api_backfill_and_bounded_cache(client, monkeypatch):
    from harbour import call_evidence
    identity, remote = call_fixture(client)
    monkeypatch.setattr(call_evidence.call_endpoint, 'fetch', lambda *args: json.dumps(remote).encode())
    client.post('/api/servers/atlas/network/call-tests/'+identity+'/collect')
    store.execute('DELETE FROM network_diagnostics')
    base = '/api/servers/atlas/network/analysis'
    assert client.get(base).json()['pending'] == 1
    filled = client.get(base+'?prepare=true')
    assert filled.status_code == 200 and filled.json()['pending'] == 0 and not filled.json()['errors']
    report = client.get(base+'/call/'+identity).json()
    monkeypatch.setattr(api, 'MAX_REPORTS', 2)
    for index in range(4):
        r = {**report, 'source_id': str(index), 'id': 'call:'+str(index), 'start': time.time()+index}
        api.persist(r, 'fixture')
    assert len(store.rows('SELECT * FROM network_diagnostics')) == 2


def test_api_incident_period_boundary_and_read_only_user(client):
    client.put('/api/servers/atlas/type', json={'server_type': 'openwrt'})
    end = time.time()
    # Marker is in the selected day, but its evidence window starts before it.
    store.execute("INSERT INTO network_events(id,server_id,measured,label,automatic,complete) VALUES('boundary','atlas',?,'Example',1,1)", (end-86400+30,))
    base = '/api/servers/atlas/network/analysis'
    selection = '?hours=24&until='+str(end)
    response = client.get(base+selection+'&prepare=true')
    assert response.status_code == 200 and response.json()['pending'] == 0
    assert response.json()['reports'][0]['source_id'] == 'boundary'
    exported = json.loads(gzip.decompress(client.get(base+'/download'+selection).content))
    assert exported['selection']['hours'] == 24 and exported['selection']['reports'] == 1
    client.post('/api/users', json={'name': 'reviewer', 'password': 'reviewer-password-strong', 'role': 'user'})
    client.post('/api/login', json={'name': 'reviewer', 'password': 'reviewer-password-strong'})
    assert client.get(base+'/incident/boundary?download=true').status_code == 200
