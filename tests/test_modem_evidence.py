"""Modem-only interpretation, conservative timing, retention, and offline handoffs."""
import copy
import datetime as dt
import gzip
import io
import json
import time

import pytest
from harbour import modem, modem_evidence as evidence, network_reports, store
from test_harbour import client


def sample(at=1000, counter='0', *, uptime='1day(s)0h:0m:0s', revision='one', lock='1', event=None, channel='1'):
    def section(fields):
        return {'state': 'ok', 'observed_at': at, 'received_at': at+1,
                'fields': [{'field': k, 'value': v} for k, v in fields.items()], 'omitted_fields': 0}
    result = {'at': at, 'received_at': at+1, 'state': 'ok', 'configuration_revision': revision,
              'observer': 'fixture', 'endpoint': 'http://192.168.100.1', 'adapter': 'compal-one', 'poll_interval_seconds': 60,
              'sections': {'system': section({'GlobalSettings/SwVersion': 'fixture-one'}),
                           'device': section({'cm_system_info/cm_system_uptime': uptime}),
                           'downstream': section({'downstream_table/downstream[1]/chid': channel,
                               'downstream_table/downstream[1]/freq': '530000000',
                               'downstream_table/downstream[1]/PostRs': counter,
                               'downstream_table/downstream[1]/IsQamLocked': lock}),
                           'upstream': section({}), 'events': section({})}}
    if event:
        result['sections']['events'] = section({'eventlog_table/eventlog[1]/prior': 'critical',
            'eventlog_table/eventlog[1]/text': event, 'eventlog_table/eventlog[1]/time': '01-01-2000 00:00:00'})
    return result


def assess(rows, start=1000, end=1200):
    analyzer = evidence.Assessment(start, end)
    for row in rows:
        analyzer.add(row)
    return analyzer.finish()


def test_modem_only_counter_change_is_bracketed_and_self_contained():
    rows = [sample(1000, '9007199254740993'), sample(1063, '9007199254740994')]
    before = copy.deepcopy(rows)
    result = assess(rows)
    assert result['state'] == 'indications_observed' and rows == before
    finding = result['findings'][0]
    assert finding['code'] == 'PostRs_increase' and '(+1)' in finding['detail']
    assert (finding['start'], finding['end']) == (1000, 1064)
    assert set(finding['modem_evidence_refs']) <= result['evidence'].keys()
    table = evidence.correlate(result, [{'id': 'a', 'start': 1020, 'end': 1022, 'conclusion': 'Ping failed'},
                                        {'id': 'b', 'start': 1100, 'end': 1102, 'conclusion': 'Ping failed'}])
    assert table['rows'][0]['modem_finding_ids'] == [finding['id']]
    assert not table['rows'][1]['modem_finding_ids']
    assert result['root_cause'] == 'not_established'


@pytest.mark.parametrize('change', ['revision', 'observer', 'firmware', 'channel', 'gap', 'failed'])
def test_discontinuities_never_infer_counter_errors(change):
    a, b = sample(), sample(1063, '100')
    if change == 'revision': b['configuration_revision'] = 'new'
    elif change == 'observer': b['observer'] = 'new'
    elif change == 'firmware': b['sections']['system']['fields'][0]['value'] = 'new'
    elif change == 'channel': b = sample(1063, '100', channel='2')
    elif change == 'gap': b['at'], b['received_at'] = 2000, 2001
    elif change == 'failed': a['state'], a['sections'] = 'unavailable', {}
    result = assess([a, b], end=3000)
    assert result['state'] != 'indications_observed'


def test_cumulative_values_resets_reboots_and_unlocked_channels_are_distinct():
    assert assess([sample(counter='500')])['state'] == 'no_defined_indication'
    reset = assess([sample(counter='500'), sample(1063, '3')])
    assert reset['findings'][0]['code'] == 'counter_reset' and reset['state'] == 'no_defined_indication'
    reboot = assess([sample(counter='500'), sample(1063, '600', uptime='0day(s)0h:0m:10s')])
    assert [f['code'] for f in reboot['findings']] == ['uptime_decreased']
    assert assess([sample(lock='0')])['findings'][0]['code'] == 'channel_unlocked'
    assert evidence.uptime('123456') is None
    assert evidence.uptime('1day(s)0h:60m:0s') is None


def test_log_messages_are_deduplicated_historical_context_not_timed_faults():
    result = assess([sample(event='T4 time out'), sample(1063, event='T4 time out')])
    assert result['signal_counts']['reported_log'] == 1
    assert not result['findings'][0]['indication']
    assert result['findings'][0]['timing'] == 'unverified_modem_event_time'
    table = evidence.correlate(result, [{'start':1000, 'end':1100}])
    assert not table['rows'][0]['modem_finding_ids']


def test_missing_unknown_partial_and_bounded_examples_never_claim_healthy():
    assert assess([])['state'] == 'insufficient_evidence'
    unknown = sample(); unknown['sections'] = {'device': {'state':'ok', 'fields':[{'field':'unknown', 'value':'0'}]}}
    assert assess([unknown])['state'] == 'insufficient_evidence'
    failed = sample(); failed['state'], failed['sections'] = 'unavailable', {}
    assert assess([failed])['state'] == 'insufficient_evidence'
    result = assess([sample(1000+n*60, str(n)) for n in range(150)], end=10000)
    assert result['signal_counts']['PostRs_increase'] == 149
    assert result['findings_omitted'] > 0 and len(result['evidence']) <= evidence.MAX_PROOFS
    assert result['coverage']['snapshots'] == 150
    assert all(ref in result['evidence'] for f in result['findings'] for ref in f['modem_evidence_refs'])


def configure(client):
    store.execute("UPDATE servers SET server_type='openwrt' WHERE id='atlas'")
    assert client.put('/api/servers/atlas/modem', json={**modem.DEFAULT, 'password':'fixture-secret'}).status_code == 200
    return modem.config('atlas')['revision']


def test_full_export_dedup_and_daily_evidence_survive_short_history_pruning(client, monkeypatch):
    revision = configure(client)
    now = time.time()
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()-dt.timedelta(days=1)
    start, end = network_reports.bounds(day, 'UTC')
    for n in range(270):
        modem.persist('atlas', revision, sample(start+3600+n*60, str(n), revision=revision))
    assert store.one('SELECT count(*) AS n FROM report_modem_samples')['n'] == 270
    result = client.get('/api/servers/atlas/modem/export?hours=72').json()
    assert result['sample_count'] == 270 and len(result['samples']) == 270 and not result['truncated']
    assert result['assessment']['coverage']['snapshots'] == 270
    assert 'fixture-secret' not in json.dumps(result)
    assert client.get('/api/servers/atlas/modem/history?hours=72').json()['truncated']
    cfg = network_reports.Policy(timezone='UTC').model_dump()
    day_report = network_reports.compile_day('atlas', day, cfg)
    assert day_report['modem']['coverage']['snapshots'] == 270
    monkeypatch.setattr(modem, 'MAX_HISTORY', 1)
    modem.persist('atlas', revision, sample(now, '300', revision=revision))
    assert store.one('SELECT count(*) AS n FROM modem_samples')['n'] < 20
    again = network_reports.compile_day('atlas', day, cfg)
    assert again['modem'] == day_report['modem']
    result = client.get('/api/servers/atlas/modem/export?hours=72').json()
    assert result['sample_count'] == 271
    assert network_reports.stored_bytes('atlas')['modem_evidence'] > 0
    network_reports.retire_before('atlas', end)
    assert store.one('SELECT count(*) AS n FROM report_modem_samples')['n'] == 1


def test_export_period_and_baseline_are_explicit_and_include_every_reading(client):
    revision = configure(client)
    for at, counter in [(1000, '10'), (1060, '11'), (1120, '11')]:
        modem.persist('atlas', revision, sample(at, counter, revision=revision))
    with store.db() as con:
        writer = io.StringIO()
        evidence.write_export(writer, con, 'atlas', 1020, 1100, archived=True)
    result = json.loads(writer.getvalue())
    assert len(result['samples']) == 1 and result['samples'][0]['at'] == 1060
    assert result['assessment']['coverage']['baseline_at'] == 1000
    assert len(result['assessment']['evidence']) == 2
    assert result['assessment']['findings'][0]['code'] == 'PostRs_increase'
    assert client.get('/api/servers/atlas/modem/export?until=nan').status_code == 422


def test_handoff_has_independent_modem_analysis_all_readings_and_offline_proof(client, monkeypatch):
    from harbour import network_diagnostics as diagnostics
    from test_connection_analysis import fixture, result
    revision = configure(client)
    now = time.time()
    for at, counter in [(1000, '10'), (1002, '11')]:
        modem.persist('atlas', revision, sample(at, counter, revision=revision))
    t, raw = fixture(bad_ping=(1001,))
    report = result(t, raw); report['server_id'] = 'atlas'; report['selection_time'] = now
    original = copy.deepcopy(report)
    evidence.attach(report)
    assert report['conclusion'] == original['conclusion'] and report['contributors'] == original['contributors']
    assert report['modem']['state'] == 'indications_observed'
    assert report['modem_correlation']['rows'][0]['modem_finding_ids']
    diagnostics.persist(report, 'fixture')
    store.execute('DELETE FROM modem_samples')
    def forbidden(*args): raise AssertionError('No hardware request is allowed')
    monkeypatch.setattr(modem, 'Client', forbidden)
    response = client.get('/api/servers/atlas/network/analysis/call/test-one?download=true')
    assert response.status_code == 200
    handoff = json.loads(gzip.decompress(response.content))
    assert handoff['reports'][0]['report']['modem'] == report['modem']
    assert handoff['modem_evidence']['sample_count'] == 2
    assert 'fixture-secret' not in json.dumps(handoff)


def test_export_budget_fails_explicitly_instead_of_truncating(client, monkeypatch):
    revision = configure(client)
    modem.persist('atlas', revision, sample(time.time()))
    monkeypatch.setattr(evidence.resources, 'budget', lambda: 10)
    with pytest.raises(evidence.resources.BudgetExceeded):
        with store.db() as con:
            evidence.write_export(io.StringIO(), con, 'atlas', 0, time.time()+10, archived=True)


@pytest.mark.parametrize('field', ['t3Timeouts', 't4Timeouts', 'PreRs'])
def test_timeout_and_pre_rs_counter_rules_use_channel_identity(field):
    a, b = sample(), sample(1063)
    if field == 'PreRs':
        for item, value in [(a, '5'), (b, '6')]:
            item['sections']['downstream']['fields'].append({'field': 'downstream_table/downstream[1]/PreRs', 'value': value})
    else:
        for item, value in [(a, '5'), (b, '6')]:
            item['sections']['upstream']['fields'] = [{'field':'upstream_table/upstream[1]/'+k, 'value':v}
                for k,v in [('usid','2'),('freq','37000000'),(field,value)]]
    result = assess([a, b])
    finding = next(f for f in result['findings'] if f['code'] == field+'_increase')
    assert finding['indication'] == (field != 'PreRs')


def test_daily_html_escapes_modem_events_and_preserves_assessment(client):
    from harbour import report_html
    revision = configure(client)
    day = dt.datetime.now(dt.timezone.utc).date()-dt.timedelta(days=1)
    start, end = network_reports.bounds(day, 'UTC')
    modem.persist('atlas', revision, sample(start+1, lock='0', event='<script>evil()</script>'))
    report = network_reports.compile_day('atlas', day, network_reports.Policy(timezone='UTC').model_dump())
    html = report_html.render('Example', {'start_day':str(day),'end_day':str(day),'timezone':'UTC','days':[report],'missing_days':[]})
    assert '<script>evil()' not in html and '&lt;script&gt;evil()' in html
    assert 'Independent modem assessment' in html and 'Channel reports loss of lock' in html
