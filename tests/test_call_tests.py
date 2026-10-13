import json
import os
import subprocess
import sys
import time

import pytest

from harbour import call_profiles, call_tests, incidents, network, network_collector, store
from test_harbour import client


def prepare(client, monkeypatch):
    assert client.put('/api/servers/atlas/type', json={'server_type':'openwrt'}).status_code == 200
    monkeypatch.setattr(store, 'DEMO', False)
    assert client.put('/api/servers/atlas/network', json={'enabled':True}).status_code == 200
    monkeypatch.setattr(call_tests.shutil, 'which', lambda _: '/example/iperf3')


def wait_finished(client, id_):
    for _ in range(100):
        result = client.get('/api/servers/atlas/network/call-tests/'+id_).json()
        if result['status'] != 'running':
            return result
        time.sleep(.02)
    pytest.fail('Traffic test failed to stop')


def test_profiles_and_strict_inputs():
    assert call_tests.Settings(endpoint='192.0.2.1', profile='teams-meeting').download_kbps == 4000
    assert call_tests.Settings(endpoint='192.0.2.1', profile='zoom-meeting').upload_kbps == 2600
    assert call_profiles.profile('facetime')['basis'] == 'Harbour approximation'
    for changes in [{'endpoint':'example.com'}, {'endpoint':'127.0.0.1'}, {'endpoint':'1.1.1.1; reboot'},
                    {'endpoint':'ff02::1'}, {'profile':'invented'}, {'duration':0}, {'upload_kbps':0},
                    {'download_kbps':8001}, {'port':65535}, {'packet_bytes':9000}, {'baseline':121}]:
        with pytest.raises(ValueError):
            call_tests.Settings(**{'endpoint':'192.0.2.1', **changes})
    settings=call_tests.Settings(endpoint='2001:db8::1', profile='teams-meeting').model_dump()
    up=call_tests.command('iperf3',settings,'upload');down=call_tests.command('iperf3',settings,'download')
    assert up[up.index('-b')+1]=='2500000' and down[down.index('-b')+1]=='4000000'
    assert down[down.index('-p')+1]=='5202' and '-R' in down and '-R' not in up


def output():
    receiver={'sender':False,'packets':100,'lost_packets':3,'lost_percent':3,'jitter_ms':7,
              'bytes':116400,'bits_per_second':931200,'start':0,'end':1,'seconds':1}
    return {'start':{'version':'fixture'}, 'end':{'sum_received':receiver},
            'intervals':[{'sum':{'sender':True,'jitter_ms':0}}],
            'server_output_json':{'intervals':[{'sum':receiver}]}}


def test_receiver_measurements_and_missing_evidence():
    data=output()
    result=call_tests.parse_output(json.dumps(data),'upload',100)
    assert result['summary']['lost_percent']==3 and result['intervals'][0]['jitter_ms']==7
    assert result['receiver']=='remote'
    data.pop('server_output_json')
    result=call_tests.parse_output(json.dumps(data),'upload',100)
    assert not result['intervals_available'] and result['summary']['lost_percent']==3
    data['intervals']=[{'sum':data['end']['sum_received']}]
    assert call_tests.parse_output(json.dumps(data),'download',100)['receiver']=='on-site'
    data['end']['sum_received']['packets']=0
    with pytest.raises(ValueError, match='No usable receiver'):
        call_tests.parse_output(json.dumps(data),'download',100)
    with pytest.raises(ValueError):
        call_tests.parse_output('{"error":"server is busy"}','upload',100)
    with pytest.raises(ValueError):
        call_tests.parse_output('{"extra":NaN}','upload',100)


def test_success_exports_profile_snapshot_and_marker(client, monkeypatch):
    prepare(client,monkeypatch)
    def fake_load(config, executable, check):
        check()
        return {d:call_tests.parse_output(json.dumps(output()),d,100) for d in ('upload','download')}
    monkeypatch.setattr(call_tests,'run_load',fake_load)
    reply=client.post('/api/servers/atlas/network/call-tests',json={
        'endpoint':'192.0.2.10','profile':'teams-meeting','duration':10,'baseline':0,'recovery':0})
    assert reply.status_code==200,reply.text
    result=wait_finished(client,reply.json()['id'])
    assert result['status']=='complete'
    assert result['config']['download_kbps']==4000
    assert result['config']['profile_snapshot']['reviewed']=='2026-10-13'
    assert result['config']['estimated_payload_bytes']==8125000
    assert store.one('SELECT label FROM network_events WHERE id=?',(result['event_id'],))['label'].startswith('Synthetic load: Teams')
    listing=client.get('/api/servers/atlas/network').json()['call_tests']
    assert 'raw' not in listing['tests'][0]['result']['directions']['upload']
    assert 'raw' in result['result']['directions']['upload']
    assert result['result']['directions']['upload']['summary']['lost_percent']==3
    event=store.one('SELECT * FROM network_events WHERE id=?',(result['event_id'],))
    bundle=network.incident_bundle(network.host('atlas'),event)
    assert bundle['traffic_tests'][0]['config']['profile']=='teams-meeting'
    report=incidents.analyze(bundle)
    assert any('controlled traffic test' in note for note in report['quality'])


def test_stop_serialization_pause_reconfigure_and_restart(client,monkeypatch):
    prepare(client,monkeypatch)
    body={'endpoint':'192.0.2.10','baseline':120}
    for action in ('stop','pause','connection','restart'):
        store.execute("UPDATE servers SET monitoring_enabled=1,host='192.0.2.1' WHERE id='atlas'")
        reply=client.post('/api/servers/atlas/network/call-tests',json=body)
        assert reply.status_code==200,reply.text
        id_=reply.json()['id']
        assert client.post('/api/servers/atlas/network/call-tests',json=body).status_code==409
        if action=='stop':
            assert client.post('/api/servers/atlas/network/call-tests/'+id_+'/stop').status_code==200
        elif action=='pause':
            store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
        elif action=='connection':
            store.execute("UPDATE servers SET host='192.0.2.2' WHERE id='atlas'")
        else:
            call_tests.close_all()
        assert wait_finished(client,id_)['status']=='cancelled'
    store.execute("INSERT INTO network_call_tests(id,server_id,created,status,phase,config) VALUES ('stale','atlas',0,'running','loading','{}')")
    call_tests.initialize()
    assert store.one("SELECT status FROM network_call_tests WHERE id='stale'")['status']=='interrupted'


def test_permissions_and_inactive_monitoring(client,monkeypatch):
    prepare(client,monkeypatch)
    body={'endpoint':'192.0.2.10'}
    store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    assert client.post('/api/servers/atlas/network/call-tests',json=body).status_code==400
    client.post('/api/users',json={'name':'reader','password':'reader-password-strong','role':'user'})
    user=client.post('/api/login',json={'name':'reader','password':'reader-password-strong'}).json()
    client.headers['X-CSRF-Token']=user['csrf']
    assert client.get('/api/servers/atlas/network/call-tests').status_code==200
    assert client.post('/api/servers/atlas/network/call-tests',json=body).status_code==403
    assert client.post('/api/servers/atlas/network/call-tests/example/stop').status_code==403


def test_process_failure_and_cancel_stop_both_children():
    config=call_tests.Settings(endpoint='192.0.2.1',duration=10).model_dump()
    children=[]
    def launch(command,**kwargs):
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],**kwargs)
        children.append(process)
        return process
    began=time.monotonic()
    def check():
        if time.monotonic()-began>.2:
            raise call_tests.Cancelled('test cancellation')
    with pytest.raises(call_tests.Cancelled):
        call_tests.run_load(config,'ignored',check,launch)
    assert len(children)==2 and all(p.poll() is not None for p in children)
    assert time.monotonic()-began<3


@pytest.mark.parametrize('mode',['failure','overflow'])
def test_failed_or_oversized_child_terminates_other_direction(monkeypatch,mode):
    config=call_tests.Settings(endpoint='192.0.2.1',duration=10).model_dump()
    children=[]
    monkeypatch.setattr(call_tests,'OUTPUT_LIMIT',1024)
    def launch(command,**kwargs):
        source=('import sys;sys.exit(1)' if mode=='failure' else 'import sys;print("x"*2048);sys.stdout.flush()') if not children else 'import time;time.sleep(30)'
        process=subprocess.Popen([sys.executable,'-c',source],**kwargs)
        children.append(process)
        return process
    with pytest.raises(ValueError):
        call_tests.run_load(config,'ignored',lambda:None,launch)
    assert len(children)==2 and all(p.poll() is not None for p in children)


def test_twelve_router_targets_preserve_result_identity(tmp_path):
    targets=['192.0.2.'+str(n) for n in range(1,13)]
    settings=network.Settings(targets=targets).model_dump()
    with pytest.raises(ValueError):
        network.Settings(targets=targets+['192.0.2.13'])
    # Exercise the actual concurrent shell command with an inert ping fixture.
    ping=tmp_path/'ping'
    ping.write_text('#!/bin/sh\nfor arg in "$@"; do target="$arg"; done\n'
                    'case "$target" in\n*.2) echo "1 packets transmitted, 0 packets received"; exit 1;;\n'
                    '*.3) echo "no route"; exit 2;;\n'
                    '*) echo "64 bytes from $target: time=4.5 ms";;\nesac\n')
    ping.chmod(0o700)
    command=network_collector.probe_command(settings)
    raw=subprocess.check_output(['/bin/sh','-c',command],env={**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH']},text=True)
    rows=list(network_collector.probe_results(raw,settings,100))
    assert [r['target'] for r in rows]==targets
    assert rows[1]['status']=='timeout' and rows[2]['kind']=='gap'
    assert all(r['rtt_ms']==4.5 for i,r in enumerate(rows) if i not in (1,2))


def test_large_incident_window_keeps_all_probe_rows(client,monkeypatch):
    prepare(client,monkeypatch)
    at=time.time()
    with store.db() as con:
        con.executemany('INSERT INTO network_samples(server_id,measured,kind,target,payload) VALUES (?,?,?,?,?)',
                        [('atlas',at,'probe','192.0.2.1','{"status":"reply","source":"recorder","rtt_ms":5}')] * 31000)
    event=network.mark('atlas','Large synthetic incident',measured=at)
    bundle=network.incident_bundle(network.host('atlas'),store.one('SELECT * FROM network_events WHERE id=?',(event,)))
    assert len(bundle['samples'])==31000 and not bundle['truncated']
