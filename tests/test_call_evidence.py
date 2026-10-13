import copy
import gzip
import json
import time

from test_harbour import client
from harbour import call_evidence, call_tests, network, store


def fixture(client):
    client.put('/api/servers/atlas/type', json={'server_type':'openwrt'})
    now, test_id = time.time(), 'a'*32
    clock = {'boot_id':'b'*32,'offset_seconds':120,'uncertainty_seconds':.02}
    stats = {'sender':False,'packets':100,'lost_packets':2,'lost_percent':2,'jitter_ms':7,'start':0,'end':1,'seconds':1}
    directions, records = {}, []
    for direction in ('upload','download'):
        raw = {'extra_data':'harbour:'+test_id+':'+direction,
               'start':{'cookie':'cookie-'+direction,'timestamp':{'timesecs':now-5}},
               'end':{'sum_received':stats},'intervals':[{'sum':stats}]}
        directions[direction] = {'raw':copy.deepcopy(raw),'summary':stats,'receiver':'remote' if direction=='upload' else 'on-site'}
        if direction == 'upload': raw['start']['timestamp']['timesecs'] += 120
        records.append({'test_id':test_id,'direction':direction,'raw':raw,'boot_id':'b'*32,'at':now+120-2})
    config = {'endpoint_id':'managed-endpoint','profile':'facetime','profile_snapshot':{'name':'FaceTime'},'connection_signature':'private-connection-signature'}
    result = {'load_started':now-5,'load_finished':now-2,'directions':directions,'clock_before':clock,'clock_after':dict(clock)}
    store.execute('INSERT INTO network_call_tests(id,server_id,created,finished,status,phase,config,result) VALUES(?,?,?,?,?,?,?,?)',
                  (test_id,'atlas',now-12,now,'complete','finished',json.dumps(config),json.dumps(result)))
    for phase, origin, rtt in [('baseline',now-11,5),('load',now-4,80)]:
        for n in range(10):
            store.execute('INSERT INTO network_samples(server_id,measured,kind,target,payload) VALUES(?,?,?,?,?)',
                ('atlas',origin+n/20,'probe','192.0.2.1',json.dumps({'source':'recorder','status':'reply','rtt_ms':rtt})))
    return test_id, {'test_id':test_id,'records':records}


def test_correlated_evidence_analysis_export_and_retry_preserve_samples(client, monkeypatch):
    test_id, remote = fixture(client)
    monkeypatch.setattr(call_evidence.call_endpoint, 'fetch', lambda *args: json.dumps(remote).encode())
    base='/api/servers/atlas/network/call-tests/'+test_id
    response=client.post(base+'/collect')
    assert response.status_code == 200, response.text
    analysis=response.json()['analysis']
    assert analysis['remote_matches'] == ['download','upload']
    assert analysis['clock_aligned'] and analysis['root_cause'] == 'not_established'
    assert any('p95 RTT rose' in f['text'] for f in analysis['findings'])
    assert {t['direction'] for t in analysis['timeline']} == {'upload','download'}
    view=client.get(base+'/analysis').json()
    assert view['analysis']['remote_matches']==['download','upload']
    assert 'raw' not in view['test']['result']['directions']['upload']
    exported=client.get(base+'/export')
    data=json.loads(gzip.decompress(exported.content))
    assert data['kind']=='harbour-call-flow-evidence' and len(data['evidence']['local']['samples'])==20
    assert data['evidence']['remote']['records']==remote['records']
    assert 'private-connection-signature' not in json.dumps(data)
    assert exported.headers['content-type'].startswith('application/gzip')
    store.execute("DELETE FROM network_samples WHERE server_id='atlas'")
    def unavailable(*args): raise call_evidence.HTTPException(502,'unavailable')
    monkeypatch.setattr(call_evidence.call_endpoint,'fetch',unavailable)
    assert client.post(base+'/collect').status_code==200
    saved=call_evidence.read(test_id)
    assert len(saved['local']['samples'])==20 and len(saved['remote']['records'])==2
    assert 'incomplete' in saved['remote']['error']


def test_wrong_cookie_clock_steps_and_missing_receiver_are_not_success(client, monkeypatch):
    test_id, remote=fixture(client)
    remote['records'][0]['raw']['start']['cookie']='another-test'
    monkeypatch.setattr(call_evidence.call_endpoint,'fetch',lambda *args:json.dumps(remote).encode())
    test=call_tests.test_row('atlas',test_id)
    test['result']['clock_after']['offset_seconds']+=10
    evidence=call_evidence.collect(test)
    a=evidence['analysis']
    assert a['remote_matches']==['download'] and not a['clock_aligned']
    assert all(r['direction']=='download' for r in a['timeline'])
    test['result']['directions']={};test['status']='cancelled'
    a=call_evidence.analyze(test,evidence)
    assert a['summary']=='Receiver evidence is incomplete.' and not a['remote_matches']


def test_clock_sample_uses_local_bracket_and_rejects_wall_clock_step(monkeypatch):
    monkeypatch.setattr(call_evidence.call_endpoint,'status',lambda _: {'wall_time':110,'boot_id':'b'*32,'monotonic':500})
    clock=iter([100,100.2]);mono=iter([10,10.2])
    monkeypatch.setattr(call_evidence.time,'time',lambda:next(clock))
    monkeypatch.setattr(call_evidence.time,'monotonic',lambda:next(mono))
    sample=call_evidence.clock_sample('endpoint')
    assert abs(sample['offset_seconds']-9.9)<.0001
    assert abs(sample['uncertainty_seconds']-.1)<.0001


def test_hour_test_keeps_last_minute_intervals_and_probes(client, monkeypatch):
    test_id, remote=fixture(client)
    monkeypatch.setattr(call_evidence.call_endpoint,'fetch',lambda *args:json.dumps(remote).encode())
    test=call_tests.test_row('atlas',test_id)
    test['created']-=3600;test['result']['load_started']=test['created']+10
    evidence=call_evidence.collect(test)
    assert len(evidence['analysis']['router_report']['windows'])==9
    assert sum(w['coverage']['samples'] for w in evidence['analysis']['router_report']['windows'])==20
    # Preserve receiver intervals beyond the former 602-interval parser cap.
    stats={'sender':False,'packets':100,'lost_percent':0,'start':0,'end':1}
    raw={'end':{'sum_received':stats},'intervals':[{'sum':{**stats,'start':n,'end':n+1}} for n in range(3600)]}
    parsed=call_tests.parse_output(json.dumps(raw),'download',0)
    assert len(parsed['intervals'])==3600 and parsed['intervals'][-1]['end']==3600


def test_brief_jitter_spike_is_reported_even_with_low_summary(client, monkeypatch):
    test_id, remote=fixture(client)
    test=call_tests.test_row('atlas',test_id)
    remote['records'][0]['raw']['intervals'][0]['sum']['jitter_ms']=85
    for direction in test['result']['directions'].values():
        direction['summary']['lost_percent']=0
        direction['summary']['jitter_ms']=2
    monkeypatch.setattr(call_evidence.call_endpoint,'fetch',lambda *args:json.dumps(remote).encode())
    result=call_evidence.collect(test)['analysis']
    assert result['summary']=='Receiver impairment was recorded.'
    assert any('85.0 ms' in f['text'] for f in result['findings'])


def test_long_running_window_survives_raw_retention_until_collected(client, monkeypatch):
    from harbour import network_archives
    client.put('/api/servers/atlas/type',json={'server_type':'openwrt'})
    monkeypatch.setattr(store,'DEMO',False)
    monkeypatch.setattr(network_archives,'maintain',lambda *args:None)
    now=time.time()
    assert client.put('/api/servers/atlas/network',json={'enabled':True,'retention_hours':1,'max_rows':1,'archive_enabled':False}).status_code==200
    store.execute("INSERT INTO network_call_tests(id,server_id,created,status,phase,config) VALUES('long','atlas',?,'running','loading','{}')",(now-3700,))
    for at in [now-4000,now-3690,now]:
        store.execute('INSERT INTO network_samples(server_id,measured,kind,target,payload) VALUES(?,?,?,?,?)',('atlas',at,'probe','192.0.2.1','{}'))
    network.maintain('atlas')
    rows=store.rows("SELECT measured FROM network_samples WHERE server_id='atlas' ORDER BY measured")
    assert [r['measured'] for r in rows]==[now-3690,now]
    store.execute("UPDATE network_call_tests SET status='complete' WHERE id='long'")
    network.maintain('atlas')
    assert len(store.rows("SELECT id FROM network_samples WHERE server_id='atlas'"))==1
