"""Budget admission, retained originals, low-write capture and durable counters."""
import gzip
import json
from types import SimpleNamespace

import pytest
from test_harbour import client
from harbour import resource_limits as limits, network, store, footprint, footprint_history as ledger
from scripts import resource_profile


def test_compressed_expansion_is_rejected_before_json_decode(monkeypatch):
    payload=gzip.compress(b'"'+b'x'*200000+b'"')
    monkeypatch.setattr(limits.json,'loads',lambda value: pytest.fail('Oversized JSON was parsed'))
    with pytest.raises(limits.BudgetExceeded): limits.unpack(payload,limit=1024)


def test_streamed_encoder_rejects_total_and_detects_corruption():
    with pytest.raises(limits.BudgetExceeded): limits.pack({'rows':['x'*100]*50},limit=1024)
    value={'rows':[{'at':n,'value':'sample'} for n in range(20)]}
    payload=limits.pack(value,limit=2048)
    assert limits.unpack(payload,limit=2048)==value
    with pytest.raises((EOFError,gzip.BadGzipFile)):
        limits.unpack(payload[:-6],limit=2048)


def test_samples_stop_before_oversized_database_payload_is_decoded(client,monkeypatch):
    store.execute('INSERT INTO network_samples(server_id,measured,kind,target,payload) VALUES (?,?,?,?,?)',
                  ('atlas',100,'telemetry','',json.dumps({'metrics':{'inventory':'x'*1100000}})))
    monkeypatch.setattr(network.json,'loads',lambda value: pytest.fail('Oversized DB payload was decoded'))
    assert network.samples('atlas',0,200)==([],True)


def test_samples_keep_diagnostic_counters_and_all_interfaces(client):
    interfaces={str(n):{'rx_errors_delta':n} for n in range(20)}
    metrics={'cpu':90,'measured_at':100,'inventory':'x'*50000,'openwrt':{'interfaces':interfaces,'sample_seconds':10}}
    store.execute('INSERT INTO network_samples(server_id,measured,kind,target,payload) VALUES (?,?,?,?,?)',
                  ('atlas',100,'telemetry','',json.dumps({'metrics':metrics})))
    rows,partial=network.samples('atlas',0,200)
    assert not partial and rows[0]['metrics']['openwrt']['interfaces']==interfaces
    assert 'inventory' not in rows[0]['metrics']
    assert 'inventory' in json.loads(store.one('SELECT payload FROM network_samples')['payload'])['metrics']


def test_oversized_prior_evidence_is_preserved_and_can_be_downloaded(client,monkeypatch):
    from test_call_evidence import fixture
    from harbour import call_evidence
    identity,_=fixture(client)
    payload=gzip.compress(json.dumps({'legacy':'x'*3000000}).encode())
    store.execute('INSERT INTO call_test_evidence VALUES (?,?)',(identity,payload))
    monkeypatch.setenv('HARBOUR_PROCESSING_MIB','2')
    with pytest.raises(limits.BudgetExceeded): call_evidence.read(identity)
    response=client.get('/api/servers/atlas/network/call-tests/'+identity+'/export')
    assert response.status_code==200 and response.content==payload
    assert store.one('SELECT payload FROM call_test_evidence')['payload']==payload


def test_profile_applies_existing_settings_without_rewriting_secrets(tmp_path,monkeypatch):
    path=tmp_path/'.env'; path.write_text('FIRST_RUN=False\nHARBOUR_SECRET=private-secret\nHARBOUR_PORT=1234\n')
    resource_profile.apply(path,'sbc-1g')
    _,values=resource_profile.start.read_env(path)
    cfg=resource_profile.validate(values)
    assert cfg['WEB_MEMORY_MIB']+cfg['RECORDER_MEMORY_MIB']==704
    assert values['HARBOUR_SECRET']=='private-secret' and values['HARBOUR_PORT']=='1234'
    assert path.stat().st_mode&0o777==0o600
    for key,value in values.items(): monkeypatch.setenv(key,value)
    configured={**network.DEFAULTS,'interval':.25,'router_probes':True}
    effective=limits.settings(configured)
    assert effective['interval']==2 and effective['telemetry_seconds']==10 and not effective['router_probes']
    assert configured['interval']==.25
    assert limits.flush_seconds()==10 and limits.buffer_bytes()==16*1024*1024
    with pytest.raises(ValueError): resource_profile.validate({**values,'HARBOUR_WEB_MEMORY_MIB':'768'})


def counter(at,write,identity='one',clock=None):
    return dict(sampled_at=at,clock=at if clock is None else clock,identity=identity,write_bytes=write,read_bytes=write*2,scope='cgroup')


def test_daily_history_survives_restart_reset_and_gaps(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    day=86400*20000
    monkeypatch.setattr(ledger,'time',SimpleNamespace(time=lambda:day+200))
    footprint.reset(); footprint.restore()
    footprint.observe('deployment',counter(day+10,100))
    footprint.observe('deployment',counter(day+40,400))
    footprint.checkpoint(force=True)
    footprint.reset(); footprint.restore()
    footprint.observe('deployment',counter(day+70,700))
    # Container replacement doesn't erase earlier bytes or fabricate downtime.
    footprint.observe('deployment',counter(day+100,10,'two'))
    footprint.observe('deployment',counter(day+130,310,'two'))
    footprint.observe('deployment',counter(day+900,810,'two'))
    saved=ledger.summary(('deployment',))['days'][0]
    assert saved['write_bytes']==900 and saved['write_seconds']==90
    assert saved['read_bytes']==1800
    footprint.reset()


def test_day_boundary_cached_sample_and_clock_step_are_not_double_counted(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    day=86400*20000
    monkeypatch.setattr(ledger,'time',SimpleNamespace(time=lambda:day+100))
    footprint.reset(); footprint.restore()
    footprint.observe('deployment',counter(day-10,0))
    footprint.observe('deployment',counter(day+10,200))
    footprint.observe('deployment',counter(day+10,200))
    footprint.observe('deployment',counter(day+3600,300,clock=day+40))
    days=ledger.summary(('deployment',))['days']
    assert len(days)==2 and all(d['write_bytes']==100 and d['write_seconds']==10 for d in days)
    footprint.reset()


def test_checkpoint_is_batched_and_history_failure_is_visible(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    footprint.reset(); footprint.restore()
    monkeypatch.setattr(ledger.sqlite3,'connect',lambda *a,**kw: (_ for _ in ()).throw(ledger.sqlite3.OperationalError('full')))
    footprint.checkpoint()  # No write inside five-minute checkpoint period.
    assert ledger.error is None
    footprint.checkpoint(force=True)
    assert 'could not be saved' in ledger.error
    footprint.reset()


def test_failed_collection_keeps_the_previous_saved_recording(client,monkeypatch):
    from test_call_evidence import fixture
    from harbour import call_evidence
    identity,remote=fixture(client)
    monkeypatch.setattr(call_evidence.call_endpoint,'fetch',lambda *args: json.dumps(remote).encode())
    url='/api/servers/atlas/network/call-tests/'+identity+'/collect'
    assert client.post(url).status_code==200
    saved=store.one('SELECT payload FROM call_test_evidence')['payload']
    monkeypatch.setattr(network,'samples',lambda *args,**kwargs: ([],True))
    response=client.post(url)
    assert response.status_code==413 and 'retained' in response.text
    assert store.one('SELECT payload FROM call_test_evidence')['payload']==saved


def test_oversized_incident_export_streams_original_without_parsing(client,monkeypatch):
    client.put('/api/servers/atlas/type',json={'server_type':'openwrt'})
    archive=json.dumps({'samples':[],'original':'x'*3000000})
    store.execute("INSERT INTO network_events(id,server_id,measured,label,automatic,complete,archive) VALUES ('large','atlas',100,'Fixture',0,1,?)",(archive,))
    monkeypatch.setenv('HARBOUR_PROCESSING_MIB','2')
    response=client.get('/api/servers/atlas/network/export/large')
    assert response.status_code==200
    result=json.loads(gzip.decompress(response.content))
    assert result['incident']['id']=='large' and result['saved_archive']['original']=='x'*3000000


def test_memory_categories_are_separate_and_missing_remains_unknown(tmp_path,monkeypatch):
    from test_footprint import fake_linux
    from harbour import remote_probe
    _,write=fake_linux(tmp_path,monkeypatch)
    root='/sys/fs/cgroup/harbour.service'
    write(root+'/memory.current','12000')
    write(root+'/memory.stat','anon 4000\nfile 7000\nkernel 1000\nfile_dirty 500\nfile_writeback 100\n')
    write(root+'/io.stat','')
    write(root+'/memory.max','335544320');write(root+'/memory.peak','20000')
    value=remote_probe.harbour_cgroup_footprint(root)
    assert value['app_bytes']==4000 and value['cache_bytes']==7000 and value['kernel_bytes']==1000
    assert value['memory_limit_bytes']==335544320 and value['dirty_cache_bytes']==500
    write(root+'/memory.max','max');write(root+'/memory.stat','anon 4000\nfile 7000\n')
    value=remote_probe.harbour_cgroup_footprint(root)
    assert value['kernel_bytes'] is value['memory_limit_bytes'] is value['dirty_cache_bytes'] is None


def test_diagnostic_projection_preserves_all_analyzer_inputs_and_zero_counters():
    from harbour.connection_analysis import telemetry_signals
    metrics={'cpu':95,'boot_id':'boot','memory':{'percent':91,'inventory':'unused'},'openwrt':{
        'sample_seconds':1,'cpu_percent':{'cpu0':98},'softirq_percent':{'cpu0':55},'conntrack':'99 100',
        'interfaces':{str(n):{'rx_errors_delta':n,'tx_errors_delta':0,'rx_dropped_delta':0,'tx_dropped_delta':0,'inventory':'x'*500} for n in range(19)},
        'stations':[{'mac':str(n),'interface':'wifi','tx_packets_delta':20,'tx_retries_delta':10,'tx_failed_delta':0,'signal_dbm':-60,'inventory':'x'*500} for n in range(11)],
        'queue_stats':[{'device':str(n),'discipline':'fq','backlog_bytes':70000 if n==50 else 0,'inventory':'x'*500} for n in range(51)],
        'radio_stats':{'wifi':{'busy_percent':95,'inventory':'x'*500}},'network':['x'*5000]}}
    selected=limits.diagnostic_telemetry(metrics)
    assert telemetry_signals({'metrics':metrics},{'client_mac':'0'})==telemetry_signals({'metrics':selected},{'client_mac':'0'})
    assert len(selected['openwrt']['interfaces'])==19 and len(selected['openwrt']['stations'])==11 and len(selected['openwrt']['queue_stats'])==51
    assert selected['openwrt']['interfaces']['0']['rx_errors_delta']==0
    assert selected['openwrt']['queue_stats'][0]['backlog_bytes']==0
    assert len(json.dumps(selected))<len(json.dumps(metrics))/3


def test_routing_context_is_retained_once_per_run_with_explicit_references(client):
    metrics={'cpu':20,'openwrt':{'routes':'default dev wan','rules':'lookup main','software_offload':False}}
    for n,run in enumerate(('a','a','b','b')):
        store.execute('INSERT INTO network_samples(server_id,measured,kind,target,payload) VALUES (?,?,?,?,?)',
                      ('atlas',100+n,'telemetry','',json.dumps({'run_id':run,'metrics':metrics})))
    rows,partial=network.samples('atlas',0,200,diagnostic=True)
    assert not partial
    assert rows[0]['metrics']['openwrt']['routes']==rows[2]['metrics']['openwrt']['routes']=='default dev wan'
    assert 'routes' not in rows[1]['metrics']['openwrt']
    assert rows[1]['routing_context_sample_id']==rows[0]['id'] and rows[3]['routing_context_sample_id']==rows[2]['id']
