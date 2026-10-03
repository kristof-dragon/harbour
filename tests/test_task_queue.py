import copy
import json
import threading
import time

import pytest

from test_harbour import client, wait_job
from harbour import app as module, history, remote_probe, ssh, store


def submit(client, action='pull', host='atlas', targets=None):
    preview=client.post(f'/api/servers/{host}/plan',json={'action':action,'targets':targets or ['group:immich']})
    assert preview.status_code==200,preview.text
    response=client.post(f'/api/servers/{host}/execute',json={'token':preview.json()['token']})
    assert response.status_code==200,response.text
    return response.json()['id']


def test_fifo_queue_continues_after_failure_without_blocking_other_hosts(client,monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    entered=threading.Event();release=threading.Event();calls=[]
    def request(server,payload,on_event=None):
        calls.append((server['id'],payload['action']))
        if server['id']=='atlas' and payload['action']=='pull':
            on_event({'kind':'step','completed':0,'total':1,'label':'Pulling images'})
            entered.set();assert release.wait(5)
            raise RuntimeError('Synthetic pull failure')
        return {'ok':True,'output':'Done'}
    monkeypatch.setattr(ssh,'request',request)
    monkeypatch.setattr(module,'refresh_server',lambda *args:None)
    first=submit(client);assert entered.wait(2)
    try:
        # Old container inventory is allowed only while already busy; execution still validates it.
        store.execute("UPDATE servers SET checked=? WHERE id='atlas'",(time.time()-240,))
        second=submit(client,'stop');third=submit(client,'start')
        queued=client.get('/api/jobs/'+second).json()
        assert queued['status']=='queued' and queued['queue_position']==1 and queued['waiting_for']=='pull'
        assert client.get('/api/jobs/'+third).json()['queue_position']==2
        other=submit(client,'restart','luna',['group:nextcloud'])
        assert wait_job(client,other)['status']=='succeeded'
        assert [c for c in calls if c[0]=='atlas']==[('atlas','pull')]
    finally:release.set()
    assert wait_job(client,first)['status']=='failed'
    assert wait_job(client,second)['status']=='succeeded'
    assert wait_job(client,third)['status']=='succeeded'
    assert [c for c in calls if c[0]=='atlas']==[('atlas','pull'),('atlas','stop'),('atlas','start')]
    assert not module.job_queues


def test_resources_record_while_docker_is_busy_and_preserve_container_inventory(client,monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    store.execute('UPDATE servers SET last_attempt=?',(time.time(),))
    store.execute("UPDATE servers SET last_attempt=0 WHERE id='atlas'")
    before=module.get_server('atlas');snapshot=json.loads(before['snapshot'])
    metrics=copy.deepcopy(snapshot['metrics']);metrics['cpu']=63.2;metrics['docker']=None
    completed=threading.Event();requests=[]
    def request(server,payload,on_event=None):
        requests.append(payload);completed.set()
        return {'metrics':metrics,'latency_ms':5.4}
    monkeypatch.setattr(ssh,'request',request)
    operation_lock=module.server_lock('atlas');operation_lock.acquire()
    original=store.one('SELECT COUNT(*) AS n FROM resource_history WHERE server_id=?',('atlas',))['n']
    try:
        module.poll_due();assert completed.wait(2)
        deadline=time.monotonic()+2
        while module.resource_locks['atlas'].locked() and time.monotonic()<deadline:time.sleep(.01)
        row=module.get_server('atlas');updated=json.loads(row['snapshot'])
        assert updated['services']==snapshot['services']
        assert updated['metrics']['cpu']==63.2 and updated['metrics']['docker']==snapshot['metrics']['docker']
        assert row['checked']>before['checked'] and row['latency_ms']==5.4
        assert store.one('SELECT COUNT(*) AS n FROM resource_history WHERE server_id=?',('atlas',))['n']>=original
        point=history.series('atlas',1)['points'][-1]
        assert point['cpu']==63.2 and point['samples']>=1
        module.poll_due();assert requests==[{'operation':'resources'}]
    finally:operation_lock.release()


def test_resource_probe_never_calls_docker(monkeypatch):
    monkeypatch.setattr(remote_probe,'inventory',lambda:pytest.fail('Docker inventory must not run'))
    def metrics(docker=True):
        assert docker is False
        return {'cpu':42}
    monkeypatch.setattr(remote_probe,'metrics',metrics)
    assert remote_probe.handle({'operation':'resources'})=={'metrics':{'cpu':42}}


def test_connection_change_while_queued_fails_without_remote_execution(client,monkeypatch):
    lock=module.server_lock('atlas');lock.acquire()
    try:
        job=submit(client)
        store.execute("UPDATE servers SET host='different.invalid' WHERE id='atlas'")
    finally:lock.release()
    monkeypatch.setattr(ssh,'request',lambda *a,**kw:pytest.fail('Changed host must not be contacted'))
    module.dispatch_queued('atlas')
    result=wait_job(client,job)
    assert result['status']=='failed' and 'changed while queued' in result['output']


def test_quiet_command_keeps_heartbeat_and_elapsed_information(client,monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    entered=threading.Event();release=threading.Event()
    def request(server,payload,on_event=None):
        on_event({'kind':'step','completed':0,'total':1,'label':'Applying containers'})
        on_event({'kind':'heartbeat'});entered.set();release.wait(3)
        return {'ok':True,'output':''}
    monkeypatch.setattr(ssh,'request',request)
    monkeypatch.setattr(module,'refresh_server',lambda *args:None)
    job=submit(client,'up');assert entered.wait(2)
    try:
        progress=client.get('/api/jobs/'+job).json()['progress']
        assert progress['started']>0 and progress['heartbeat']>0 and progress['phase']=='executing'
    finally:release.set()
    assert wait_job(client,job)['status']=='succeeded'


def test_refresh_all_includes_paused_hosts_and_requires_admin(client):
    store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    response=client.post('/api/servers/refresh-all')
    assert response.status_code==200 and not response.json()['errors']
    jobs=response.json()['jobs'];assert len(jobs)==4 and any(j['server_id']=='atlas' for j in jobs)
    for job in jobs:assert wait_job(client,job['id'])['status']=='succeeded'
    client.post('/api/users',json={'name':'viewer','password':'viewer-password-long','role':'user'})
    result=client.post('/api/login',json={'name':'viewer','password':'viewer-password-long'}).json()
    client.headers['X-CSRF-Token']=result['csrf']
    assert client.post('/api/servers/refresh-all').status_code==403


def test_queued_compose_identity_survives_recreation_but_commands_must_match(client,monkeypatch):
    services=json.loads(module.get_server('atlas')['snapshot'])['services']
    expected=remote_probe.plan(services,'restart',['immich-web'])
    recreated=copy.deepcopy(services);recreated[0]['id']='new-container-id'
    monkeypatch.setattr(remote_probe,'inventory',lambda:recreated)
    calls=[];monkeypatch.setattr(remote_probe,'run_stream',lambda argv,*a,**kw:calls.append(argv) or 'done')
    payload={'operation':'execute','action':'restart','targets':['immich-web'],'target_refs':{'immich-web':{'project':'immich','name':'web'}},'expected':expected}
    assert remote_probe.handle(payload,lambda e:None)['ok'] and len(calls)==1
    recreated[0]['working_dir']='/different/path'
    with pytest.raises(ValueError,match='changed after the preview'):remote_probe.handle(payload,lambda e:None)
    assert len(calls)==1


def test_resource_pool_does_not_wait_for_saturated_docker_workers(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    blocker=threading.Event();entered=threading.Event()
    with ThreadPoolExecutor(max_workers=1) as busy:
        monkeypatch.setattr(module,'pool',busy)
        busy.submit(lambda:(entered.set(),blocker.wait(3)));assert entered.wait(1)
        store.execute('UPDATE servers SET last_attempt=?',(time.time(),))
        store.execute("UPDATE servers SET last_attempt=0 WHERE id='atlas'")
        seen=threading.Event()
        original=module.poll_resources
        def resources(server,lock):
            try:original(server,lock)
            finally:seen.set()
        monkeypatch.setattr(module,'poll_resources',resources)
        try:
            module.poll_due()  # Full poll waits for a worker and reserves the Docker lock.
            module.poll_due()  # Lightweight pool is independent.
            assert seen.wait(1) and module.server_lock('atlas').locked()
        finally:blocker.set()


def test_dashboard_keeps_all_pending_jobs_beyond_recent_activity_limit(client):
    for index in range(60):
        store.execute("INSERT INTO jobs (id,server_id,server_name,actor,action,status,targets,created,target_names) VALUES (?,'atlas','Atlas','admin','pull','queued','[]',?,'[\"test\"]')", (f'pending-{index}',time.time()+index))
    jobs=client.get('/api/dashboard').json()['jobs']
    assert len(jobs)==60
    assert next(j for j in jobs if j['id']=='pending-0')['queue_position']==1
    assert next(j for j in jobs if j['id']=='pending-59')['queue_position']==60
