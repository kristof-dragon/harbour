import json
import time

from harbour import store
from test_harbour import client
from test_login_sessions import history, event, snapshot


def test_login_table_filters_all_pages_and_orders_real_durations(client, history):
    now=time.time()
    records=[]
    for i in range(31):
        start=now-1000+i*10
        duration=[2,10,100][i%3]
        records += [event('authentication',start,pid=str(i+100),username='alice' if i==30 else 'dragon'),
                    event('session_end',start+duration,pid=str(i+100),username='alice' if i==30 else 'dragon')]
    history(records,snapshot(now,pid='9999'))
    url='/api/servers/atlas/logins/table'
    first=client.get(url,params={'sort':'duration','order':'asc'}).json()
    assert first['pagination']==dict(page=1,size=25,total=31,pages=2)
    assert [x['duration_seconds'] for x in first['events']]==sorted(x['duration_seconds'] for x in first['events'])
    second=client.get(url,params={'sort':'duration','order':'asc','page':2}).json()
    assert len(second['events'])==6 and all(x['duration_seconds']==100 for x in second['events'])
    assert not {x['id'] for x in first['events']} & {x['id'] for x in second['events']}
    filtered=client.get(url,params={'filters':json.dumps({'username':'alice'})}).json()
    assert filtered['pagination']['total']==1 and filtered['events'][0]['username']=='alice'
    filtered=client.get(url,params={'filters':json.dumps({'duration':'>=10','status':'closed','source':'10.11'})}).json()
    assert filtered['pagination']['total']==20
    assert client.get(url,params={'filters':json.dumps({'time':[now+1,now+86400]})}).json()['pagination']['total']==0
    assert client.get(url,params={'page':99999}).json()['pagination']['page']==2
    assert client.get('/api/servers/missing/logins/table').status_code==404
    for params in [{'sort':'time; DROP TABLE servers'}, {'size':1000000}, {'order':'random'}, {'filters':'{"username":true}'}, {'filters':'{"unknown":"x"}'}, {'filters':'{"duration":"oops"}'}]:
        assert client.get(url,params=params).status_code==422


def test_raw_login_table_and_stale_status(client,history):
    now=time.time();history([event('authentication',now-3),event('session_start',now-2)],snapshot(now))
    url='/api/servers/atlas/logins/table'
    assert client.get(url,params={'filters':'{"status":"connected"}'}).json()['pagination']['total']==1
    assert client.get(url,params={'view':'events'}).json()['pagination']['total']==2
    store.execute('UPDATE host_login_state SET fetched_at=?',(now-1000,))
    data=client.get(url,params={'filters':'{"status":"connected"}'}).json()
    assert data['pagination']['total']==0 and data['current_sessions']==[]
    assert client.get(url,params={'filters':'{"status":"unconfirmed"}'}).json()['pagination']['total']==1


def test_security_and_install_tables_search_beyond_first_page(client):
    with store.db() as con:
        con.execute('DELETE FROM auth_log')
        for i in range(70):
            con.execute('INSERT INTO auth_log(username,address,created,outcome,detail) VALUES (?,?,?,?,?)',('alice' if i==0 else 'dragon','192.0.2.1',1000+i,'success',''))
            con.execute('INSERT INTO install_artifacts(id,target_name,host,actor,kind,action,created,status) VALUES (?,?,?,?,?,?,?,?)',(str(i),'oldest' if i==0 else 'router','192.0.2.2','dragon','logins','install',1000+i,'complete'))
    data=client.get('/api/security/log',params={'filters':'{"username":"alice"}','page':1,'size':25}).json()
    assert data['total']==1 and data['entries'][0]['created']==1000
    data=client.get('/api/install-artifacts/table',params={'filters':'{"target":"oldest"}'}).json()
    assert data['pagination']['total']==1 and data['artifacts'][0]['id']=='0'
    assert client.get('/api/security/log?size=0').status_code==422
    assert client.get('/api/install-artifacts/table?sort=status&order=asc&page=3').json()['pagination']['total']==70
