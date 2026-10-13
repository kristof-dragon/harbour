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
    def start(id_,body,source='manual',shutdown=None):
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
    prepare(client,monkeypatch)
    body={'enabled':True,'interval_minutes':120,'test':{**settings(),'endpoint':'192.0.2.46'}}
    assert client.put('/api/servers/atlas/network/call-tests/schedule',json=body).status_code==400
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.delete('/api/servers/atlas/network/call-tests/schedule').status_code==403
    client.headers['X-CSRF-Token']=csrf
    client.post('/api/users',json={'name':'reader','password':'reader-password-strong','role':'user'})
    user=client.post('/api/login',json={'name':'reader','password':'reader-password-strong'}).json();client.headers['X-CSRF-Token']=user['csrf']
    assert client.put('/api/servers/atlas/network/call-tests/schedule',json=body).status_code==403
