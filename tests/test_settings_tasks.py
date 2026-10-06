import copy
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from test_harbour import client, wait_job
from harbour import app as module, remote_probe, ssh, store


def settings_payload():
    return dict(name='Renamed host',server_type='plain',thresholds={**store.DEFAULTS,'cpu':93},
                volumes=[dict(mount='/',monitor=True,warn=False,card=True)],enabled=False,poll_seconds=120)


def test_single_settings_save_persists_every_section(client):
    payload=settings_payload()
    assert client.put('/api/servers/atlas/settings',json=payload).status_code==200
    row=module.get_server('atlas')
    assert row['name']==payload['name'] and row['server_type']=='plain'
    assert json.loads(row['thresholds'])['cpu']==93
    assert not row['monitoring_enabled'] and row['poll_seconds']==120
    assert json.loads(row['volume_settings'])['/']==dict(monitor=True,warn=False,card=True)
    assert json.loads(row['snapshot'])['services']==[]
    payload.update(server_type='docker',thresholds=None,enabled=True,poll_seconds=None)
    assert client.put('/api/servers/atlas/settings',json=payload).status_code==200
    row=module.get_server('atlas')
    assert row['thresholds'] is None and row['poll_seconds'] is None and row['monitoring_enabled']


def test_rejected_settings_and_storage_error_do_not_partially_save(client):
    before=module.get_server('atlas')
    invalid=settings_payload();invalid['volumes'][0]['mount']='/not-discovered'
    assert client.put('/api/servers/atlas/settings',json=invalid).status_code==400
    assert module.get_server('atlas')==before
    invalid=settings_payload();invalid['poll_seconds']=1
    assert client.put('/api/servers/atlas/settings',json=invalid).status_code==422
    lock=module.server_lock('atlas');lock.acquire()
    try:assert client.put('/api/servers/atlas/settings',json=settings_payload()).status_code==409
    finally:lock.release()
    store.execute("CREATE TRIGGER fail_settings BEFORE UPDATE OF snapshot ON servers BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
    with pytest.raises(Exception,match='simulated failure'):
        client.put('/api/servers/atlas/settings',json=settings_payload())
    assert module.get_server('atlas')==before


def test_new_actions_require_admin_and_docker_mode(client):
    assert client.put('/api/servers/atlas/type',json={'server_type':'plain'}).status_code==200
    for action in ['start','stop','prune']:
        assert client.post('/api/servers/atlas/plan',json={'action':action,'targets':[] if action=='prune' else ['group:immich']}).status_code==400
    client.post('/api/users',json={'name':'reader','password':'reader-password-long','role':'user'})
    login=client.post('/api/login',json={'name':'reader','password':'reader-password-long'}).json();client.headers['X-CSRF-Token']=login['csrf']
    assert client.put('/api/servers/atlas/settings',json=settings_payload()).status_code==403
    assert client.post('/api/servers/atlas/plan',json={'action':'prune','targets':[]}).status_code==403
    assert client.get('/api/jobs/any').status_code==403


@pytest.mark.parametrize('action',['start','stop'])
def test_start_stop_plans_target_existing_containers_and_demo_records_names(client,action):
    snapshot=json.loads(module.get_server('atlas')['snapshot']);services=snapshot['services']
    c=next(s for s in services if not s['project'])
    assert remote_probe.plan(services,action,[c['id']])[0]['argv']==['docker',action,c['id']]
    command=remote_probe.plan(services,action,['immich-web'])[0]['argv']
    assert command[-2:]==[action,'web'] and '--no-deps' not in command
    body={'action':action,'targets':['immich-web']}
    plan=client.post('/api/servers/atlas/plan',json=body).json()
    response=client.post('/api/servers/atlas/execute',json={'token':plan['token']})
    job=wait_job(client,response.json()['id'])
    assert job['status']=='succeeded' and job['progress']['completed']==1
    assert any('immich' in name for name in job['target_names'])
    updated=json.loads(module.get_server('atlas')['snapshot'])
    service=next(s for s in updated['services'] if s['id']=='immich-web')
    assert service['state']==('running' if action=='start' else 'exited')
    store.execute("UPDATE servers SET snapshot='{}' WHERE id='atlas'")
    assert client.get('/api/jobs/'+job['id']).json()['target_names']==job['target_names']
    entry=next(j for j in client.get('/api/dashboard').json()['jobs'] if j['id']==job['id'])
    assert entry['target_names']==job['target_names']


def test_prune_is_explicit_bounded_and_confirmed(client,monkeypatch):
    expected=[{'argv':['docker','system','prune','--force'],'cwd':None,'label':'Docker system prune'}]
    assert remote_probe.plan([], 'prune', [])==expected
    with pytest.raises(ValueError):remote_probe.plan([],'prune',['anything'])
    preview=client.post('/api/servers/atlas/plan',json={'action':'prune','targets':[]}).json()
    assert preview['commands'][0]['command']=='docker system prune --force'
    result=client.post('/api/servers/atlas/execute',json={'token':preview['token']})
    job=wait_job(client,result.json()['id'])
    assert job['status']=='succeeded' and job['target_names']==['Entire Docker host']
    assert client.post('/api/servers/atlas/execute',json={'token':preview['token']}).status_code==409


def test_process_output_is_delivered_before_exit_and_timeout_stops_process(tmp_path):
    acknowledgement=tmp_path/'ack'
    events=[]
    def emit(event):
        events.append(event)
        if 'first' in event.get('text',''):acknowledgement.touch()
    script="import pathlib,time; print('first',flush=True); p=pathlib.Path("+repr(str(acknowledgement))+");\nwhile not p.exists(): time.sleep(.01)\nprint('second',flush=True)"
    result=remote_probe.run_stream([sys.executable,'-c',script],emit,timeout=3)
    assert 'first' in result and 'second' in result and acknowledgement.exists()
    pidfile=tmp_path/'pid'
    with pytest.raises(TimeoutError):
        remote_probe.run_stream([sys.executable,'-c',"import os,pathlib,time; pathlib.Path("+repr(str(pidfile))+").write_text(str(os.getpid())); time.sleep(10)"],lambda e:None,timeout=.3)
    with pytest.raises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)


def test_progress_frames_survive_split_unicode_and_many_events():
    events=[];parser=ssh.ProgressFrames(events.append)
    raw=(json.dumps({'event':{'kind':'output','text':'héllo'}},ensure_ascii=False)+'\n'+json.dumps({'result':{'ok':True}})+'\n').encode()
    for byte in raw:parser.feed(bytes([byte]))
    assert events[0]['text']=='héllo' and parser.finish()=={'ok':True}
    with pytest.raises(RuntimeError,match='before the operation result'):ssh.ProgressFrames().finish()
    parser=ssh.ProgressFrames()
    for _ in range(2100):parser.feed((json.dumps({'event':{'kind':'output','text':'x'*4096}})+'\n').encode())
    assert not parser.pending  # Streaming output is bounded even beyond 8 MB overall.


def test_streamed_pull_apply_stops_after_failed_pull(client,monkeypatch):
    services=json.loads(module.get_server('atlas')['snapshot'])['services'];targets=['group:immich','group:gateway']
    expected=remote_probe.plan(services,'pull_up',targets);events=[];calls=[]
    monkeypatch.setattr(remote_probe,'inventory',lambda:services)
    def run(argv,emit,**kwargs):
        calls.append(argv);emit({'kind':'output','text':'live output'})
        if len(calls)==2:raise RuntimeError('download failed')
        return 'done'
    monkeypatch.setattr(remote_probe,'run_stream',run)
    result=remote_probe.handle({'operation':'execute','action':'pull_up','targets':targets,'expected':expected},events.append)
    assert not result['ok'] and result['completed']==1 and len(calls)==2
    assert [e['completed'] for e in events if e['kind']=='completed']==[1]
    assert all('up' not in c for c in calls)


def test_live_progress_persists_and_survives_ssh_failure(client,monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    reached=threading.Event();release=threading.Event()
    def request(server,payload,on_event=None):
        on_event({'kind':'step','completed':0,'total':1,'label':'stop · immich'})
        on_event({'kind':'output','text':'container stopping\n'})
        on_event({'kind':'heartbeat'});reached.set();release.wait(3)
        raise RuntimeError('SSH disconnected; check server')
    monkeypatch.setattr(module.ssh,'request',request)
    user=store.one('SELECT * FROM users WHERE name="admin"')
    result=module.queue_job('atlas','stop',['immich-web'],user,[{'argv':['docker','stop','synthetic'],'cwd':None,'label':'immich'}])
    try:
        assert reached.wait(2)
        job=client.get('/api/jobs/'+result['id']).json()
        assert job['status']=='running' and job['progress']['label']=='stop · immich'
    finally:release.set()
    job=wait_job(client,result['id'])
    assert job['status']=='failed' and 'container stopping' in job['output'] and 'SSH disconnected' in job['output']
    assert job['progress']['completed']==0


def test_real_ssh_stream_delivers_output_before_command_finishes(client,tmp_path,monkeypatch):
    """Real loopback SSH + a fake docker executable; no real Docker changes."""
    import socket
    import subprocess
    import paramiko
    acknowledgement=tmp_path/'ssh-ack'
    executable=tmp_path/'docker'
    executable.write_text('#!'+sys.executable+'\nimport sys,time,pathlib\n'
        'if sys.argv[1:]==["system","prune","--force"]:\n'
        ' print("Live prune output",flush=True)\n'
        ' p=pathlib.Path('+repr(str(acknowledgement))+')\n'
        ' deadline=time.monotonic()+4\n'
        ' while not p.exists() and time.monotonic()<deadline: time.sleep(.01)\n'
        ' if not p.exists(): sys.exit(2)\n'
        ' print("Total reclaimed space: 0 B",flush=True)\n')
    executable.chmod(0o700)
    host_key=paramiko.ECDSAKey.generate();workers=[];failures=[];done=threading.Event();channels=[]
    class Host(paramiko.ServerInterface):
        def check_auth_password(self,username,password):
            return paramiko.AUTH_SUCCESSFUL if (username,password)==('test-user','test-password') else paramiko.AUTH_FAILED
        def get_allowed_auths(self,username):return 'password'
        def check_channel_request(self,kind,channel_id):return paramiko.OPEN_SUCCEEDED
        def check_channel_exec_request(self,channel,command):
            channels.append(channel)
            def execute():
                try:
                    if command==b'true':channel.send_exit_status(0)
                    else:
                        assert command==ssh.REMOTE_PYTHON.encode()
                        source=bytearray();channel.settimeout(5)
                        while chunk:=channel.recv(65536):source.extend(chunk)
                        with subprocess.Popen([sys.executable,'-'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                              env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ.get('PATH','')}) as proc:
                            proc.stdin.write(source);proc.stdin.close()
                            while chunk:=os.read(proc.stdout.fileno(),4096):channel.sendall(chunk)
                            channel.send_exit_status(proc.wait(timeout=5))
                except Exception as exc:failures.append(exc)
                finally:channel.shutdown_write()
            worker=threading.Thread(target=execute,daemon=True);workers.append(worker);worker.start();return True
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        def serve():
            try:
                connection,_=listener.accept()
                with paramiko.Transport(connection) as transport:
                    transport.add_server_key(host_key);transport.start_server(server=Host())
                    while not done.is_set() and transport.is_active():
                        channel=transport.accept(.1)
                        if channel:channels.append(channel)
            except Exception as exc:failures.append(exc)
        worker=threading.Thread(target=serve,daemon=True);worker.start()
        monkeypatch.setattr(store,'DEMO',False)
        server=dict(host='127.0.0.1',port=listener.getsockname()[1],username='test-user',auth_method='password',
                    fingerprint=ssh.host_fingerprint(host_key),password_encrypted=store.cipher().encrypt(b'test-password').decode())
        events=[]
        def emit(event):
            events.append(event)
            if 'Live prune output' in event.get('text',''):acknowledgement.touch()
        try:
            result=ssh.request(server,{'operation':'execute','action':'prune','targets':[],
                                      'expected':remote_probe.plan([],'prune',[])},on_event=emit)
            assert result['ok'] and acknowledgement.exists()
            assert 'Total reclaimed space: 0 B' in result['output']
            assert [e['completed'] for e in events if e['kind']=='completed']==[1]
        finally:
            done.set();worker.join(6)
            for thread in workers:thread.join(6)
        assert not failures
