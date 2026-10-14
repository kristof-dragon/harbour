"""Calendar reconciliation, protected retention and printable evidence reports."""
import datetime as dt
import gzip
import json
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from harbour import network, network_archives as archives, network_reports as reports, report_facts, report_html, store, storage_usage
from test_harbour import client
from test_openwrt import enable
from test_network_archives import settings
from test_incidents import probe, telemetry


def epoch(text):
    return dt.datetime.fromisoformat(text).timestamp()


def set_clock(monkeypatch, now):
    for module in (network, archives):
        monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda:now))
    monkeypatch.setattr('harbour.network_runtime.read_status', lambda _: {})


def archive_rows(client, monkeypatch, now, rows, **cfg):
    _, signature, revision = enable(client, monkeypatch)
    if cfg:
        settings(client, **cfg)
    set_clock(monkeypatch, now)
    assert network.persist('atlas', rows, {}, signature, revision)
    network.maintain('atlas')
    return signature, revision


def test_dst_calendar_days_and_single_schedule_slot():
    cfg=reports.Policy(process_at='01:30').model_dump()
    assert reports.bounds('2026-03-29','Europe/London')[1]-reports.bounds('2026-03-29','Europe/London')[0] == 23*3600
    assert reports.bounds('2026-10-25','Europe/London')[1]-reports.bounds('2026-10-25','Europe/London')[0] == 25*3600
    # Both occurrences of the autumn 01:30 resolve to the same one-per-date run.
    a=epoch('2026-10-25T01:45:00+01:00');b=epoch('2026-10-25T01:45:00+00:00')
    assert reports.slot(a,cfg)==reports.slot(b,cfg)
    assert reports.scheduled(dt.date(2026,3,29),cfg)==epoch('2026-03-29T02:30:00+01:00')


def test_calendar_reconciliation_schedule_and_idempotence(client,monkeypatch):
    now=epoch('2026-10-14T02:45:00+01:00')
    before=epoch('2026-10-13T23:59:59+01:00');after=before+2
    rows=[probe(before,sample_key='a'),probe(after,rtt_ms=3,sample_key='b'),telemetry(before)]
    archive_rows(client,monkeypatch,now,rows)
    archive=store.one('SELECT * FROM network_archives')
    assert archive['day']==epoch('2026-10-13T03:00:00+01:00') and not archive['complete']
    marker=network.mark('atlas','<img src=x onerror=alert(1)>', measured=before)
    now=epoch('2026-10-14T03:01:00+01:00');set_clock(monkeypatch,now)
    network.maintain('atlas');reports.run_due('atlas',now=now)
    result=reports.period('atlas',dt.date(2026,10,13),dt.date(2026,10,13))
    day=result['days'][0]
    assert day['kinds']=={'probe':1,'telemetry':1}
    assert day['paths'][0]['high']==1 and day['markers']==1
    assert any(e['id']=='marker:'+marker for e in day['timeline'])
    assert day['telemetry']['cpu_percent']['max']==95
    payload=store.one('SELECT payload FROM report_days WHERE day=?',('2026-10-13',))['payload']
    reports.run_due('atlas',now=now+60)
    assert store.one('SELECT payload FROM report_days WHERE day=?',('2026-10-13',))['payload']==payload
    response=client.get('/api/servers/atlas/network/reports/period?start=2026-10-13&end=2026-10-13')
    assert response.status_code==200 and 'text/html' in response.headers['content-type']
    assert '<img src=x' not in response.text and '&lt;img src=x' in response.text
    assert '@media print' in response.text and 'sample_sha256' in response.text
    assert '2026-10-13' in response.text and 'Daily' not in response.headers.get('Content-Disposition','')


def test_late_readings_revise_previous_day_without_double_counting(client,monkeypatch):
    at=epoch('2026-10-13T20:00:00+01:00');now=epoch('2026-10-14T04:00:00+01:00')
    signature,revision=archive_rows(client,monkeypatch,now,[probe(at,rtt_ms=4,sample_key='first')])
    reports.run_due('atlas',now=now)
    assert reports.period('atlas',dt.date(2026,10,13),dt.date(2026,10,13))['days'][0]['paths'][0]['attempts']==1
    network.persist('atlas',[probe(at+1,sample_key='late')],{},signature,revision)
    network.maintain('atlas');reports.request_run('atlas',user={'role':'admin'})
    reports.run_due('atlas',now=now+60)
    day=reports.period('atlas',dt.date(2026,10,13),dt.date(2026,10,13))['days'][0]
    assert day['paths'][0]['attempts']==2
    reports.request_run('atlas',user={'role':'admin'});reports.run_due('atlas',now=now+120)
    assert reports.period('atlas',dt.date(2026,10,13),dt.date(2026,10,13))['days'][0]['paths'][0]['attempts']==2


def test_raw_cleanup_waits_for_verified_report_and_retains_support(client,monkeypatch):
    at=epoch('2026-10-11T20:00:00+01:00');now=epoch('2026-10-11T22:00:00+01:00')
    archive_rows(client,monkeypatch,now,[probe(at,sample_key='source')],archive_retention_days=1,retention_hours=1)
    old=store.one('SELECT * FROM network_archives');assert not store.rows('SELECT * FROM network_samples')
    set_clock(monkeypatch,epoch('2026-10-12T04:00:00+01:00'));network.maintain('atlas')
    now=epoch('2026-10-14T04:00:00+01:00');set_clock(monkeypatch,now);network.maintain('atlas')
    assert archives.path(old).exists()
    original=reports.compile_day
    monkeypatch.setattr(reports,'compile_day',lambda *a: (_ for _ in ()).throw(OSError('full')))
    reports.run_due('atlas',now=now)
    assert archives.path(old).exists() and store.one('SELECT error FROM report_state')['error']
    monkeypatch.setattr(reports,'compile_day',original);reports.run_due('atlas',now=now+60)
    assert not archives.path(old).exists()
    day=reports.period('atlas',dt.date(2026,10,11),dt.date(2026,10,11))['days'][0]
    evidence=next(t for t in day['timeline'] if t['kind']=='Probe observations')['evidence'][0]
    assert evidence['rtt_ms']==200 and evidence['provenance']['archive_id']==old['id']


def test_legacy_import_is_streamed_verified_and_replay_safe(client,monkeypatch):
    now=epoch('2026-10-13T20:00:00+01:00');at=now-60
    archive_rows(client,monkeypatch,now,[probe(at,sample_key='old')])
    old=store.one('SELECT * FROM network_archives')
    store.execute('DELETE FROM report_archives')
    set_clock(monkeypatch,now+86400);network.maintain('atlas')
    old=store.one('SELECT * FROM network_archives WHERE id=?',(old['id'],))
    reports.import_archive(old);reports.import_archive(old)
    day=reports.compile_day('atlas',dt.date(2026,10,13),reports.policy('atlas'))
    assert day['paths'][0]['attempts']==1 and reports.ready(old)
    original=archives.path(old).read_bytes();archives.path(old).write_bytes(original[:-8])
    with pytest.raises((EOFError,gzip.BadGzipFile,ValueError)):
        reports.import_archive(old)
    assert not reports.ready(old)


def test_late_dirty_revision_during_publication_survives(client,monkeypatch):
    at=epoch('2026-10-12T20:00:00+01:00');now=epoch('2026-10-14T04:00:00+01:00')
    archive_rows(client,monkeypatch,now,[probe(at)])
    original=reports.compile_day
    def concurrent(*args):
        report=original(*args)
        with store.db() as con: reports.mark_dirty(con,'atlas',at,at)
        return report
    monkeypatch.setattr(reports,'compile_day',concurrent)
    reports.run_due('atlas',now=now)
    assert store.one('SELECT day FROM report_dirty WHERE day=?',(int(at//86400)*86400,))


def test_incomplete_old_backlog_prevents_cleanup(client,monkeypatch):
    now=epoch('2026-10-14T04:00:00+01:00');at=now-86400
    signature,revision=archive_rows(client,monkeypatch,now,[probe(at)])
    network.persist('atlas',[probe(at+1)],{},signature,revision)
    reports.run_due('atlas',now=now)
    assert 'older recordings' in store.one('SELECT error FROM report_state')['error']
    assert not store.rows('SELECT * FROM report_days')


def test_statistics_weighted_missing_uncertain_and_periods():
    data=report_facts.compact([probe(60,rtt_ms=10),probe(61,rtt_ms=1000,timing_quality='uncertain'),
        probe(62,status='timeout'),probe(63,status='late_reply')],[],{'latency_limit_ms':150},[],'a'*32,1)
    p=report_facts.path_totals(data['minutes'])[0]
    assert (p['attempts'],p['usable'],p['replies'],p['uncertain'],p['late'],p['timeouts'])==(3,2,1,1,1,1)
    assert p['sum_ms']==10 and p['observed_seconds']==2
    second={**p,'sum_ms':90,'replies':9,'attempts':9,'usable':9,'timeouts':0,'histogram':[0]*len(p['histogram'])}
    total=report_html.merged_paths([{'paths':[p]},{'paths':[second]}])[0]
    assert total['sum_ms']/total['replies']==10 and total['timeouts']/total['usable']==1/11


def test_report_permissions_policy_and_missing_dates(client,monkeypatch):
    enable(client,monkeypatch)
    base='/api/servers/atlas/network/reports'
    assert client.put(base+'/policy',json={'timezone':'Not/AZone'}).status_code==422
    assert client.put(base+'/policy',json={'process_at':'24:00'}).status_code==422
    assert client.get(base+'/period?start=2026-10-14&end=2026-10-13').status_code==422
    data=client.get(base+'/period?start=2026-10-12&end=2026-10-18&format=json').json()
    assert len(data['missing_days'])==7
    client.post('/api/users',json={'name':'reader','password':'reader-password-strong','role':'user'})
    token=client.post('/api/login',json={'name':'reader','password':'reader-password-strong'}).json()
    client.headers['X-CSRF-Token']=token['csrf']
    assert client.get(base).json()['storage'] is None
    assert client.post(base+'/run').status_code==403
    assert client.put(base+'/policy',json={}).status_code==403
    assert client.get(base+'/period?start=2026-10-12&end=2026-10-18').status_code==200
    client.cookies.clear();assert client.get(base).status_code==401


def test_storage_meter_counts_backups_archives_and_ignores_symlinks(client,tmp_path):
    (store.DATA/'network-archives').mkdir(exist_ok=True);(store.DATA/'network-archives'/'test.gz').write_bytes(b'x'*23)
    (store.DATA/'recovery').mkdir();(store.DATA/'recovery'/'saved.db').write_bytes(b'x'*42)
    (store.DATA/'recovery'/'duplicate').symlink_to(store.DATA/'harbour.db')
    value=storage_usage.collect(force=True)
    assert value['categories']['recordings']['bytes']==23 and value['categories']['recovery']['bytes']==42
    assert value['bytes']==sum(c['bytes'] for c in value['categories'].values())
    assert value['reusable_database_bytes'] is not None


def test_every_identified_call_episode_retains_evidence_beyond_old_cap():
    from harbour import connection_analysis
    rows=[probe(1000+i,rtt_ms=200 if i%2==0 else 4) for i in range(1000)]
    result=connection_analysis.analyze('incident','many',{'id':'atlas','name':'Test'},1000,2000,
        rows,[{'id':'run-one','config':{'interval':.5,'latency_limit_ms':150}}],{})
    assert len(result['episodes'])==500 and result['episodes_omitted']==0
    assert all(ep['evidence_refs'] and all(ref in result['evidence'] for ref in ep['evidence_refs']) for ep in result['episodes'])


def test_scheduled_processing_does_not_publish_at_midnight(client,monkeypatch):
    before=epoch('2026-10-13T23:00:00+01:00');now=epoch('2026-10-14T00:30:00+01:00')
    archive_rows(client,monkeypatch,now,[probe(before)])
    reports.run_due('atlas',now=now)
    assert not store.one('SELECT 1 FROM report_days WHERE day=?',('2026-10-13',))
    now=epoch('2026-10-14T03:01:00+01:00');set_clock(monkeypatch,now);network.maintain('atlas');reports.run_due('atlas',now=now)
    assert store.one('SELECT 1 FROM report_days WHERE day=?',('2026-10-13',))


def test_changing_schedule_cannot_reopen_sealed_archive(client,monkeypatch):
    now=epoch('2026-10-13T04:00:00+01:00')
    signature,revision=archive_rows(client,monkeypatch,now,[probe(now-1)])
    now+=86400;set_clock(monkeypatch,now);network.maintain('atlas')
    reports.save_policy('atlas',reports.Policy(process_at='23:00'),user={'role':'admin'})
    network.persist('atlas',[probe(now-1)],{},signature,revision);network.maintain('atlas')
    assert not store.one('SELECT error FROM network_archive_status')['error']
    assert sum(r['sample_count'] for r in store.rows('SELECT sample_count FROM network_archives'))==2
    assert not store.rows('SELECT id FROM network_samples WHERE archived=0')


def test_failed_call_analysis_keeps_raw_evidence_beyond_retention(client,monkeypatch):
    now=epoch('2026-10-14T04:00:00+01:00');at=now-10*86400
    archive_rows(client,monkeypatch,now,[])
    store.execute("INSERT INTO network_call_tests(id,server_id,created,finished,status,phase,config) VALUES ('bad-call','atlas',?,?,'complete','finished','{}')",(at,at+60))
    store.execute("INSERT INTO call_test_evidence VALUES ('bad-call',?)",(b'not a valid gzip payload',))
    reports.run_due('atlas',now=now)
    assert store.one("SELECT 1 FROM network_call_tests WHERE id='bad-call'")
    assert not store.one("SELECT verified FROM report_sources WHERE source_id='bad-call'")['verified']
    day=dt.datetime.fromtimestamp(at,ZoneInfo('Europe/London')).date()
    report=reports.period('atlas',day,day)['days'][0]
    assert report['calls'][0]['analysis_failed']


def test_successful_call_metadata_survives_scheduled_raw_cleanup(client,monkeypatch):
    now=epoch('2026-10-14T04:00:00+01:00');at=now-10*86400
    archive_rows(client,monkeypatch,now,[])
    store.execute("INSERT INTO network_call_tests(id,server_id,created,finished,status,phase,config) VALUES ('old-call','atlas',?,?,'failed','finished','{}')",(at,at+60))
    reports.run_due('atlas',now=now)
    assert not store.one("SELECT 1 FROM network_call_tests WHERE id='old-call'")
    day=dt.datetime.fromtimestamp(at,ZoneInfo('Europe/London')).date()
    report=reports.period('atlas',day,day)['days'][0]
    assert report['calls'][0]['status']=='failed' and report['calls'][0]['quality']


def test_adjacent_observation_groups_keep_counts_and_evidence():
    rows=[probe(60+i*60,sample_key=str(i)) for i in range(3)]
    compact=report_facts.compact(rows,[],{'latency_limit_ms':150},[],'a'*32,1)
    timeline=report_facts.timeline(compact['minutes'],[])
    assert len(timeline)==1 and timeline[0]['counts']['slow']==3
    assert timeline[0]['grouped_minutes']==3
    assert timeline[0]['evidence'][0]['at']==60 and timeline[0]['evidence'][-1]['at']==180
    assert 'normal readings' in timeline[0]['detail']


def test_age_retention_keeps_all_facts_for_oldest_retained_calendar_day(client,monkeypatch):
    now=epoch('2026-10-14T04:00:00+01:00')
    at=epoch('2026-10-07T00:01:00+01:00')
    signature,revision=archive_rows(client,monkeypatch,now,[probe(at,sample_key='early')])
    network.persist('atlas',[probe(at+12*3600,sample_key='later')],{},signature,revision)
    network.maintain('atlas')
    cfg={**reports.policy('atlas'),'retention_days':7}
    reports.compile_day('atlas',dt.date(2026,10,7),cfg)
    reports.scheduled_cleanup('atlas',cfg,now)
    day=reports.compile_day('atlas',dt.date(2026,10,7),cfg)
    assert day['paths'][0]['attempts']==2
    assert store.one('SELECT retired_before FROM report_state')['retired_before']==epoch('2026-10-07T00:00:00+01:00')


def test_late_reading_does_not_resurrect_day_retired_by_budget(client,monkeypatch):
    now=epoch('2026-10-14T04:00:00+01:00');at=epoch('2026-10-11T12:00:00+01:00')
    signature,revision=archive_rows(client,monkeypatch,now,[probe(at,sample_key='old'),probe(at+2*86400,sample_key='new')])
    reports.run_due('atlas',now=now)
    assert store.one("SELECT 1 FROM report_days WHERE day='2026-10-11'")
    reports.retire_before('atlas',epoch('2026-10-12T00:00:00+01:00'))
    network.persist('atlas',[probe(at+1,sample_key='late')],{},signature,revision)
    network.maintain('atlas');reports.request_run('atlas',user={'role':'admin'})
    reports.run_due('atlas',now=now+60)
    assert not store.one("SELECT 1 FROM report_days WHERE day='2026-10-11'")
    assert store.one("SELECT 1 FROM report_days WHERE day='2026-10-13'")


def test_call_ending_exactly_at_midnight_is_not_counted_next_day(client,monkeypatch):
    enable(client,monkeypatch)
    boundary=epoch('2026-10-14T00:00:00+01:00')
    for name,start,end in [('ended',boundary-60,boundary),('instant',boundary,boundary)]:
        reports.capture_call({'server_id':'atlas','source':'call','source_id':name,'start':start,'end':end})
    cfg=reports.policy('atlas')
    before=reports.compile_day('atlas',dt.date(2026,10,13),cfg)
    after=reports.compile_day('atlas',dt.date(2026,10,14),cfg)
    assert [c['source_id'] for c in before['calls']]==['ended']
    assert [c['source_id'] for c in after['calls']]==['instant']
