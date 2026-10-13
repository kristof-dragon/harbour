import json
import shutil
import subprocess
import time

import pytest
from test_harbour import client
from test_call_tests import prepare
from harbour import call_tests, call_endpoint, call_endpoint_service as endpoint, store


def test_live_remote_identity_staleness_and_unavailable(client,monkeypatch):
    prepare(client,monkeypatch)
    config={'endpoint_id':'remote','remote_live':True,'profile':'facetime'}
    store.execute("INSERT INTO network_call_tests(id,server_id,created,status,phase,config,live) VALUES ('live','atlas',?,'running','loading',?,?)",
                  (time.time(),json.dumps(config),json.dumps({'upload':{'cookie':'right-cookie'},
                  'download':{'observed_at':time.time()-30,'sender':False,'lost_percent':2}})))
    reading={'endpoint_id':'remote','directions':{'upload':{'cookie':'wrong-cookie','sender':False,'age_seconds':1,'lost_percent':7,'jitter_ms':9,'bits_per_second':1000000}}}
    monkeypatch.setattr(call_endpoint,'fetch',lambda *args,**kw:json.dumps(reading).encode())
    url='/api/servers/atlas/network/call-tests/live'
    assert 'upload_receiver' not in client.get(url).json()['running']['live']
    assert client.get(url).json()['running']['live']['download']['stale']
    reading['directions']['upload']['cookie']='right-cookie'
    live=client.get(url).json()['running']['live']['upload_receiver']
    assert live['lost_percent']==7 and not live['stale']
    reading['directions']['upload']['age_seconds']=30
    assert client.get(url).json()['running']['live']['upload_receiver']['stale']
    reading['directions']['upload']['sender']=True
    assert 'upload_receiver' not in client.get(url).json()['running']['live']
    def unavailable(*args,**kw):raise call_tests.HTTPException(502,'fixture private detail')
    monkeypatch.setattr(call_endpoint,'fetch',unavailable)
    data=client.get(url).json()['running']['live']
    assert 'remote_error' in data and 'private detail' not in str(data)


def test_endpoint_live_resets_identity_and_ages_on_its_own_clock(tmp_path,monkeypatch):
    logs=endpoint.Logs(tmp_path)
    ticks=[100.0]
    monkeypatch.setattr(endpoint.time,'monotonic',lambda:ticks[0])
    logs.observe('upload',{'event':'start','data':{'cookie':'first'}})
    logs.observe('upload',{'event':'interval','data':{'sum':{'sender':False,'lost_percent':2,'jitter_ms':8}}})
    ticks[0]=112.0
    assert logs.live_readings()['upload']['age_seconds']==12
    assert logs.live_readings()['upload']['cookie']=='first'
    logs.observe('upload',{'event':'start','data':{'cookie':'second'}})
    assert logs.live_readings()=={}


@pytest.mark.skipif(not shutil.which('iperf3'),reason='Requires real iperf3; exercised in pinned Linux image')
def test_actual_client_streams_before_completion_and_keeps_final_evidence(client,monkeypatch):
    executable=shutil.which('iperf3')
    prepare(client,monkeypatch)
    monkeypatch.setattr(call_tests.shutil,'which',lambda _:executable)
    original=call_tests.command
    def command(exe,config,direction):
        cmd=original(exe,config,direction)
        cmd[cmd.index('-c')+1]='127.0.0.1'
        return cmd
    monkeypatch.setattr(call_tests,'command',command)
    servers=[subprocess.Popen([executable,'-s','-1','-p',str(p),'-J'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for p in (35201,35202)]
    try:
        time.sleep(.2)
        response=client.post('/api/servers/atlas/network/call-tests',json={'endpoint':'192.0.2.1','port':35201,'duration':10,'baseline':0,'recovery':0,'upload_kbps':128,'download_kbps':256})
        assert response.status_code==200,response.text
        id_=response.json()['id']
        deadline=time.monotonic()+9
        while time.monotonic()<deadline:
            live=client.get('/api/servers/atlas/network/call-tests/live').json()['running']
            if live and live['live'].get('download',{}).get('packets',0)>0:
                break
            time.sleep(.2)
        else:pytest.fail('No receiver reading before test completion')
        assert live['status']=='running' and live['live']['download']['sender'] is False
        assert live['live']['download']['bits_per_second']>0
        assert live['live']['upload']['lost_percent'] is None
        assert live['live']['upload']['jitter_ms'] is None
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            row=client.get('/api/servers/atlas/network/call-tests/'+id_).json()
            if row['status']!='running':break
            time.sleep(.2)
        assert row['status']=='complete',row.get('error')
        assert row['result']['directions']['upload']['intervals_available']
        assert row['result']['directions']['download']['raw']['end']['sum_received']['packets']>0
    finally:
        call_tests.close_all()
        for process in servers:
            if process.poll() is None:process.terminate()
            process.wait(timeout=5)
