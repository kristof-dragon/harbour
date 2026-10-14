import time
import pytest
from test_harbour import client
from harbour import call_tests, call_endpoint, store


def settings():
    return {'endpoint':'192.0.2.45','endpoint_id':'remote','profile':'facetime','duration':3600,'baseline':60,'recovery':60}


def test_long_duration_and_schedule_interval_validation():
    assert call_tests.Settings(**settings()).duration==3600
    with pytest.raises(ValueError):call_tests.Settings(**{**settings(),'duration':3601})
    with pytest.raises(ValueError):call_tests.ScheduleInput(enabled=True,interval_minutes=60,test=settings())
    assert call_tests.ScheduleInput(enabled=True,interval_minutes=120,test=settings()).test.duration==3600


def prepare(client,monkeypatch):
    client.put('/api/servers/atlas/type',json={'server_type':'openwrt'})
    monkeypatch.setattr(store,'DEMO',False)
    monkeypatch.setattr(call_endpoint,'saved',lambda _: {'endpoint':'192.0.2.45','port':5201,'deployed':True})
    response=client.put('/api/servers/atlas/network/call-tests/schedule',json={'enabled':True,'interval_minutes':120,'test':settings()})
    assert response.status_code==200,response.text
    return response.json()


def test_schedule_due_skip_overlap_disable_and_restart(client,monkeypatch):
    schedule=prepare(client,monkeypatch)
    now=time.time();assert schedule['next_run']>now+7100
    started=[]
    def start(id_,body,source='manual',shutdown=None,schedule_row=None):
        started.append((id_,body,source))
        return {'id':'scheduled-test'}
    monkeypatch.setattr(call_tests,'start_test',start)
    call_tests.run_due(now)
    assert not started
    store.execute("UPDATE call_test_schedules SET next_run=? WHERE server_id='atlas'",(now-15000,))
    call_tests.run_due(now)
    assert len(started)==1 and started[0][2]=='schedule'
    schedule=call_tests.schedule('atlas');assert schedule['next_run']>now and schedule['last_test']=='scheduled-test'
    def busy(*args,**kw):raise call_tests.HTTPException(409,'A traffic test is already running')
    monkeypatch.setattr(call_tests,'start_test',busy)
    call_tests.run_due(schedule['next_run']+1)
    assert 'Skipped' in call_tests.schedule('atlas')['last_outcome']
    store.execute("UPDATE call_test_schedules SET next_run=1 WHERE server_id='atlas'")
    call_tests.initialize(recover=True)
    assert call_tests.schedule('atlas')['next_run']>now and 'offline' in call_tests.schedule('atlas')['last_outcome']
    assert client.delete('/api/servers/atlas/network/call-tests/schedule').status_code==200
    assert call_tests.schedule('atlas') is None


def test_schedule_permissions_and_endpoint_match(client,monkeypatch):
    schedule=prepare(client,monkeypatch)
    base='/api/servers/atlas/network/call-tests/schedules'
    body={'enabled':True,'interval_minutes':120,'test':{**settings(),'endpoint':'192.0.2.46'}}
    assert client.put('/api/servers/atlas/network/call-tests/schedule',json=body).status_code==400
    requests=[('POST',base,body),('PUT',base+'/'+schedule['id'],body),
              ('DELETE',base+'/'+schedule['id'],None),('POST',base+'/'+schedule['id']+'/start',None)]
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.delete('/api/servers/atlas/network/call-tests/schedule').status_code==403
    for method,url,payload in requests:
        assert client.request(method,url,json=payload).status_code==403
    client.headers['X-CSRF-Token']=csrf
    client.post('/api/users',json={'name':'reader','password':'reader-password-strong','role':'user'})
    user=client.post('/api/login',json={'name':'reader','password':'reader-password-strong'}).json();client.headers['X-CSRF-Token']=user['csrf']
    assert client.put('/api/servers/atlas/network/call-tests/schedule',json=body).status_code==403
    for method,url,payload in requests:
        assert client.request(method,url,json=payload).status_code==403
    assert client.get(base).status_code==200
    assert client.get('/api/servers/atlas/network/call-tests/live').status_code==200


def test_multiple_edit_start_now_history_and_targeted_delete(client, monkeypatch):
    import json
    from test_call_tests import prepare as prepare_test
    prepare_test(client, monkeypatch)
    monkeypatch.setattr(call_endpoint, 'saved', lambda _: {'endpoint':'192.0.2.45','port':5201,'deployed':True})
    monkeypatch.setattr(call_endpoint, 'status', lambda _: {'max_duration':3600,'live_metrics':True})
    monkeypatch.setattr(call_tests, 'execute', lambda *args: None)
    base='/api/servers/atlas/network/call-tests'
    payload={'name':'Morning FaceTime','enabled':True,'interval_minutes':120,'test':settings()}
    first=client.post(base+'/schedules',json=payload).json()
    second=client.post(base+'/schedules',json={**payload,'name':'Teams','test':{**settings(),'profile':'teams-meeting'}}).json()
    assert first['id']!=second['id'] and len(client.get(base+'/schedules').json()['schedules'])==2
    assert client.delete(base+'/schedule').status_code==409
    assert client.put(base+'/schedule',json=payload).status_code==409
    started=client.post(base+'/schedules/'+first['id']+'/start')
    assert started.status_code==200,started.text
    test_id=started.json()['id']
    row=call_tests.schedule('atlas',first['id'])
    assert row['last_test']==test_id and row['next_run']==first['next_run']
    edited=client.put(base+'/schedules/'+first['id'],json={**payload,'name':'Zoom instead','test':{**settings(),'profile':'zoom-meeting'}}).json()
    assert edited['config']['profile']=='zoom-meeting' and edited['last_test']==test_id
    assert edited['next_run']==first['next_run']
    recorded=json.loads(store.one('SELECT config FROM network_call_tests WHERE id=?',(test_id,))['config'])
    assert recorded['profile']=='facetime' and recorded['schedule_id']==first['id'] and recorded['source']=='schedule-now'
    assert recorded['schedule_name']=='Morning FaceTime'
    filtered=client.get(base,params={'schedule_id':first['id']}).json()
    assert [t['id'] for t in filtered['tests']]==[test_id]
    assert client.get(base,params={'schedule_id':second['id']}).json()['tests']==[]
    store.execute("UPDATE network_call_tests SET status='complete' WHERE id=?",(test_id,))
    assert client.delete(base+'/schedules/'+first['id']).status_code==200
    assert call_tests.schedule('atlas',second['id']) is not None
    assert client.get(base+'/'+test_id).status_code==200
    assert client.put(base+'/schedules/'+first['id'],json=payload).status_code==404
    call_tests.active.clear()


def test_due_schedules_wait_fairly_without_overlapping_or_catchup(client, monkeypatch):
    import json
    first=prepare(client,monkeypatch)
    base='/api/servers/atlas/network/call-tests'
    second=client.post(base+'/schedules',json={'name':'Second','enabled':True,'interval_minutes':120,'test':settings()}).json()
    now=time.time()
    store.execute('UPDATE call_test_schedules SET next_run=? WHERE id=?',(now-20,first['id']))
    store.execute('UPDATE call_test_schedules SET next_run=? WHERE id=?',(now-10,second['id']))
    starts=[]
    def start(id_,body,**kw):
        row=kw['schedule_row'];test_id='test-'+row['id'];starts.append(row['id'])
        store.execute("INSERT INTO network_call_tests(id,server_id,created,status,phase,config) VALUES (?,?,?,'running','loading',?)",
                      (test_id,id_,now,json.dumps({'schedule_id':row['id']})))
        return {'id':test_id}
    monkeypatch.setattr(call_tests,'start_test',start)
    call_tests.run_due(now)
    assert starts==[first['id']]
    assert call_tests.schedule('atlas',second['id'])['next_run']==now-10
    assert 'Waiting' in call_tests.schedule('atlas',second['id'])['last_outcome']
    store.execute("UPDATE network_call_tests SET status='complete'")
    call_tests.run_due(now+1)
    assert starts==[first['id'],second['id']]
    assert all(row['next_run']>now for row in call_tests.schedules('atlas'))
    call_tests.run_due(now+2)
    assert len(starts)==2


def test_legacy_schedule_migration_preserves_timing_and_run_attribution(client, monkeypatch):
    import json
    first=prepare(client,monkeypatch)
    store.execute('DROP TABLE call_test_schedules')
    store.execute('''CREATE TABLE call_test_schedules (server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
        config TEXT NOT NULL,enabled INTEGER NOT NULL,interval_seconds INTEGER NOT NULL,next_run REAL,last_run REAL,last_test TEXT,last_outcome TEXT)''')
    store.execute('INSERT INTO call_test_schedules VALUES (?,?,?,?,?,?,?,?)',
                  ('atlas',json.dumps(settings()),1,7200,first['next_run'],123,'historic','Complete'))
    store.execute("INSERT INTO network_call_tests(id,server_id,created,status,phase,config) VALUES ('historic','atlas',123,'complete','finished',?)",
                  (json.dumps({'source':'schedule','profile':'facetime'}),))
    call_tests.initialize(recover=False)
    migrated=call_tests.schedules('atlas')
    assert len(migrated)==1 and migrated[0]['next_run']==first['next_run'] and migrated[0]['last_test']=='historic'
    history=call_tests.listing('atlas',migrated[0]['id'])['tests']
    assert [t['id'] for t in history]==['historic']
    call_tests.initialize(recover=False)
    assert call_tests.schedules('atlas')[0]['id']==migrated[0]['id']


def test_pause_resume_explicit_time_and_cross_server_access(client,monkeypatch):
    first=prepare(client,monkeypatch)
    base='/api/servers/atlas/network/call-tests/schedules/'+first['id']
    payload={'name':'Nightly','enabled':False,'interval_minutes':120,'test':settings()}
    paused=client.put(base,json=payload).json()
    assert not paused['enabled'] and paused['next_run'] is None
    at=time.time()+10000
    resumed=client.put(base,json={**payload,'enabled':True,'first_run':at}).json()
    assert resumed['next_run']==at
    client.put('/api/servers/beacon/type',json={'server_type':'openwrt'})
    assert client.delete(base.replace('/atlas/','/beacon/')).status_code==404
    assert call_tests.schedule('atlas',first['id']) is not None


def test_result_retention_waits_for_scheduled_report_processing(client,monkeypatch):
    import json
    from test_call_tests import prepare as prepare_test
    prepare_test(client,monkeypatch)
    monkeypatch.setattr(call_tests,'execute',lambda *args:None)
    for group in ('first','second',''):
        for n in range(20):
            store.execute("INSERT INTO network_call_tests(id,server_id,created,status,phase,config) VALUES (?,'atlas',?,'complete','finished',?)",
                          (f'{group}-{n}',n,json.dumps({'schedule_id':group} if group else {})))
    call_tests.start_test('atlas',call_tests.Settings(endpoint='192.0.2.45'),source='schedule',
                          schedule_row={'id':'first','name':'First'})
    rows=call_tests.listing('atlas','first')['tests']
    assert len(rows)==21 and 'first-0' in [r['id'] for r in rows]
    assert len(call_tests.listing('atlas','second')['tests'])==20
    assert store.one("SELECT COUNT(*) AS n FROM network_call_tests WHERE json_extract(config,'$.schedule_id') IS NULL")['n']==20
    call_tests.active.clear()
