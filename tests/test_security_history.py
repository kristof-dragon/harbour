import copy
import json
import math
import sqlite3
import time
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from test_harbour import client
from harbour import app as module, auth, demo, history, remote_probe, ssh, store


def login(c, **extra):
    r = c.post('/api/login', json={'name': 'admin', 'password': 'test-password-strong', **extra})
    if r.status_code == 200:
        c.headers['X-CSRF-Token'] = r.json()['csrf']
    return r


def test_ip_ban_fifth_failure_persisted_with_audit(client):
    for index in range(5):
        r = client.post('/api/login', json={'name': 'attempted-' + str(index), 'password': 'bad'})
        assert r.status_code == (429 if index == 4 else 401)
    assert r.headers['retry-after']
    assert login(client).status_code == 429
    rows = store.rows('SELECT * FROM auth_log WHERE outcome LIKE "failure%"')
    assert len(rows) == 5 and rows[-1]['username'] == 'attempted-4'
    assert rows[-1]['address'] == 'testclient' and rows[-1]['created'] > 0
    store.initialize()
    assert login(client).status_code == 429
    store.execute('UPDATE ip_bans SET until=?', (time.time()-1,))
    assert login(client).status_code == 200
    assert not store.rows('SELECT * FROM login_attempts')


def test_idle_limit_poll_does_not_extend_and_activity_does(client):
    before = store.one('SELECT last_activity FROM sessions')['last_activity']
    assert client.get('/api/dashboard').status_code == 200
    assert store.one('SELECT last_activity FROM sessions')['last_activity'] == before
    assert client.post('/api/session/activity').status_code == 200
    assert store.one('SELECT last_activity FROM sessions')['last_activity'] >= before
    store.execute('UPDATE sessions SET last_activity=?', (time.time()-1801,))
    assert client.get('/api/me').status_code == 401
    assert not store.rows('SELECT * FROM sessions')
    login(client)
    store.execute('UPDATE sessions SET created=?', (time.time()-12*3600-1,))
    assert client.get('/api/me').status_code == 401


def test_browser_and_ip_binding_revoke_sessions(client):
    assert client.get('/api/me', headers={'User-Agent': 'another-browser'}).status_code == 401
    login(client)
    store.execute('UPDATE sessions SET address=?', ('198.51.100.4',))
    assert client.get('/api/me').status_code == 401
    assert store.one('SELECT COUNT(*) n FROM auth_log WHERE outcome="session_revoked"')['n'] == 2


def test_proxy_spoofing_and_chain(monkeypatch):
    monkeypatch.setenv('HARBOUR_TRUSTED_PROXIES', '198.51.100.200/32')
    def req(peer, forwarded):
        return Request({'type': 'http', 'client': (peer, 123), 'headers': [(b'x-forwarded-for', forwarded.encode())]})
    assert auth.client_ip(req('203.0.113.9','203.0.113.100')) == '203.0.113.9'
    assert auth.client_ip(req('198.51.100.200','203.0.113.100, 203.0.113.9')) == '203.0.113.9'
    assert auth.client_ip(req('198.51.100.200','::ffff:192.0.2.4')) == '192.0.2.4'
    with pytest.raises(Exception, match='400'):
        auth.client_ip(req('198.51.100.200',''))
    monkeypatch.setenv('HARBOUR_TRUSTED_PROXIES','0.0.0.0/0')
    with pytest.raises(ValueError): auth.trusted_proxies()


def test_secure_cookie_and_session_rotation(client, monkeypatch):
    monkeypatch.setenv('HARBOUR_SECURE_COOKIE','true')
    client.base_url = 'https://testserver'
    r=login(client)
    cookie=r.headers['set-cookie']
    assert '__Host-harbour_session=' in cookie and 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert client.get('/api/me').status_code == 200
    old_token=client.cookies.get('__Host-harbour_session')
    assert client.put('/api/password',json={'current':'test-password-strong','password':'a-new-secure-password'}).status_code==200
    assert client.cookies.get('__Host-harbour_session') != old_token


def test_totp_setup_replay_recovery_and_disable(client, monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    r=client.post('/api/security/totp/begin',json={'password':'test-password-strong'})
    assert r.status_code == 200, r.text
    secret=r.json()['secret'];code=pyotp.TOTP(secret).now()
    assert r.json()['qr'].startswith('data:image/svg+xml;base64,')
    assert secret not in store.one('SELECT secret FROM mfa_pending')['secret']
    r=client.post('/api/security/totp/confirm',json={'code':code})
    assert r.status_code==200,r.text
    codes=r.json()['recovery_codes'];assert len(codes)==8
    client.headers['X-CSRF-Token']=r.json()['csrf']
    assert r.json()['mfa_enabled']
    assert client.post('/api/logout').status_code==200
    assert login(client).status_code==401
    assert login(client,code=code).status_code==401  # Enrollment consumed this time step.
    assert login(client,code=codes[0]).status_code==200
    assert login(client,code=codes[0]).status_code==401  # One-use recovery code.
    assert store.one('SELECT COUNT(*) n FROM recovery_codes')['n']==7
    r=client.post('/api/security/totp/disable',json={'password':'test-password-strong','code':codes[1]})
    assert r.status_code==200,r.text
    assert not r.json()['mfa_enabled']
    assert not store.rows('SELECT * FROM recovery_codes')
    assert login(client).status_code==200


def test_policy_requires_reauthentication_and_viewer_denied(client):
    p={**store.SECURITY_DEFAULTS,'idle_minutes':10,'password':'wrong'}
    assert client.put('/api/security/policy',json=p).status_code==400
    assert auth.settings()['idle_minutes']==30
    p['password']='test-password-strong'
    r=client.put('/api/security/policy',json=p)
    assert r.status_code==200
    client.headers['X-CSRF-Token']=r.json()['csrf']
    assert auth.settings()['idle_minutes']==10
    client.post('/api/users',json={'name':'viewer','password':'viewer-strong-password','role':'user'})
    login_r=client.post('/api/login',json={'name':'viewer','password':'viewer-strong-password'})
    client.headers['X-CSRF-Token']=login_r.json()['csrf']
    for url in ['/api/security/log','/api/security/policy','/api/monitoring','/api/key-options']:
        assert client.get(url).status_code==403
    assert client.get('/api/servers/atlas/history').status_code==200
    assert client.put('/api/servers/atlas/monitoring',json={'enabled':False}).status_code==403
    assert client.patch('/api/servers/atlas',json={'name':'No'}).status_code==403


@pytest.mark.parametrize('algorithm,tier,prefix', [('ed25519','normal','ssh-ed25519'),('ecdsa','normal','ecdsa-sha2-nistp256'),('ecdsa','high','ecdsa-sha2-nistp384'),('ecdsa','xhigh','ecdsa-sha2-nistp521'),('rsa','normal','ssh-rsa')])
def test_supported_key_types(client,monkeypatch,algorithm,tier,prefix):
    monkeypatch.setattr(store,'DEMO',False)
    r=client.post('/api/keys',json={'algorithm':algorithm,'tier':tier})
    assert r.status_code==200,r.text
    assert r.json()['public_key'].startswith(prefix+' ')
    credential=json.loads(store.cipher().decrypt(store.one('SELECT encrypted FROM ssh_keys WHERE id=?',(r.json()['id'],))['encrypted'].encode()))
    assert ssh.parse_key(credential['private_key'])
    assert client.post('/api/keys',json={'algorithm':'ed25519','tier':'excessive'}).status_code==400


def test_rename_temperature_warning_and_server_information(client):
    assert client.patch('/api/servers/atlas',json={'name':'Atlas renamed'}).status_code==200
    assert client.put('/api/servers/atlas/thresholds',json={**store.DEFAULTS,'temperature':40}).status_code==200
    s=client.get('/api/dashboard').json()['servers'][0]
    assert s['name']=='Atlas renamed' and s['metrics']['kernel'] and s['metrics']['timezone']['name']
    assert s['metrics']['uptime']>0 and s['latency_ms']>0
    assert any(w['id'].startswith('temperature:') for w in s['warnings'])
    assert client.patch('/api/servers/atlas',json={'name':'  '}).status_code==422


def test_sensor_collection_faults_and_missing(tmp_path):
    root=tmp_path/'class/hwmon/hwmon0';root.mkdir(parents=True)
    for name,value in {'name':'coretemp','temp1_input':'54000','temp1_label':'Package id 0','temp2_input':'99000','temp2_fault':'1','temp3_input':'nan','temp4_input':'999999'}.items():
        (root/name).write_text(value)
    result=remote_probe.temperatures(str(tmp_path))
    assert result['package']==54 and result['package_label']=='coretemp · Package id 0' and len(result['sensors'])==1
    assert remote_probe.temperatures('/nonexistent')['package'] is None
    thermal=tmp_path/'other/class/thermal/thermal_zone0';thermal.mkdir(parents=True)
    (thermal/'temp').write_text('61000');(thermal/'type').write_text('cpu-thermal')
    assert remote_probe.temperatures(str(tmp_path/'other'))['package'] == 61
    assert remote_probe.temperatures(str(tmp_path/'other'))['sensors'][0]['celsius']==61


def clean_history():
    store.execute('DELETE FROM resource_history')
    return json.loads(store.one('SELECT snapshot FROM servers WHERE id="atlas"')['snapshot'])['metrics']


def test_weighted_history_rollup_retention_and_peaks(client):
    metrics=clean_history(); now=1800000000;stamp=now-9*86400
    for i,value in enumerate([10,20,90]):
        sample=copy.deepcopy(metrics);sample['cpu']=value
        history.record('atlas',sample,latency_ms=value,up=True,now=stamp+i*60)
    history.record('atlas',up=False,now=stamp+180)
    history.record('atlas',metrics,up=True,now=now-100*86400)
    history.compact(now=now)
    rows=store.rows('SELECT * FROM resource_history')
    assert all(r['resolution']==300 for r in rows)
    data=history.series('atlas',10*24,now=now,requested_resolution=3600)
    point=next(p for p in data['points'] if p['samples'])
    assert point['cpu']==40 and point['cpu_peak']==90 and point['samples']==3 and point['attempts']==4
    assert point['up_percent']==75 and point['temperature']>0
    before=copy.deepcopy(data)
    history.compact(now=now)
    assert history.series('atlas',10*24,now=now,requested_resolution=3600)==before
    assert all(math.isfinite(p['cpu']) for p in data['points'] if p['cpu'] is not None)


def test_missing_samples_not_zero_and_display_resolution(client):
    clean_history();now=time.time()
    history.record('atlas',up=False,now=now-120)
    data=history.series('atlas',1,now=now,requested_resolution=60)
    assert data['points'][0]['cpu'] is None and data['points'][0]['temperature'] is None
    assert data['points'][0]['up_percent']==0
    assert client.get('/api/servers/atlas/history?hours=nan').status_code==400
    assert client.get('/api/servers/atlas/history?resolution=61').status_code==400
    assert client.get('/api/servers/nope/history').status_code==404


def test_monitoring_policy_validation_and_pausing(client,monkeypatch):
    invalid={**store.MONITORING_DEFAULTS,'week1_minutes':10,'week2_minutes':15}
    assert client.put('/api/monitoring',json=invalid).status_code==400
    valid={**store.MONITORING_DEFAULTS,'poll_seconds':300,'retention_days':180}
    assert client.put('/api/monitoring',json=valid).status_code==200
    assert client.put('/api/servers/atlas/monitoring',json={'enabled':False,'poll_seconds':600}).status_code==200
    store.execute('UPDATE servers SET last_attempt=?',(time.time(),))
    store.execute('UPDATE servers SET last_attempt=0 WHERE id="atlas"')
    submitted=[]
    class FakePool:
        def submit(self, fn, server, lock):
            submitted.append(server['id']);lock.release()
    monkeypatch.setattr(module,'pool',FakePool())
    module.poll_due();assert not submitted
    client.put('/api/servers/atlas/monitoring',json={'enabled':True,'poll_seconds':600})
    module.poll_due();assert submitted==['atlas']
    store.execute('UPDATE servers SET checked=? WHERE id="atlas"',(time.time()-240,))
    s=client.get('/api/dashboard').json()['servers'][0]
    assert s['poll_seconds']==600 and not s['stale']


def test_poll_history_without_dashboard_and_failure_status(client,monkeypatch):
    clean_history()
    module.refresh_server('atlas')
    assert store.one('SELECT COUNT(*) n FROM resource_history')['n']==1
    monkeypatch.setattr(store,'DEMO',False)
    def fail(*args):raise ssh.ProbeError('connection refused','down')
    monkeypatch.setattr(ssh,'request',fail)
    with pytest.raises(ssh.ProbeError): module.refresh_server('atlas')
    row=store.one('SELECT * FROM servers WHERE id="atlas"')
    assert row['connection_status']=='down' and row['latency_ms'] is None
    d=history.series('atlas',1)
    assert d['points'][-1]['attempts']==2 and d['points'][-1]['samples']==1


def test_legacy_schema_migration_preserves_users_servers_and_thresholds(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);monkeypatch.setattr(store,'DEMO',True)
    with sqlite3.connect(tmp_path/'harbour.db') as con:
        con.executescript('''CREATE TABLE users(id TEXT PRIMARY KEY,name TEXT UNIQUE,password TEXT,role TEXT);
          CREATE TABLE sessions(token TEXT PRIMARY KEY,user_id TEXT,csrf TEXT,expires REAL);
          CREATE TABLE servers(id TEXT PRIMARY KEY,name TEXT,host TEXT,port INTEGER,username TEXT,fingerprint TEXT,key_id TEXT,thresholds TEXT,snapshot TEXT,error TEXT,checked REAL,update_checked REAL);
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);''')
        con.execute('INSERT INTO users VALUES (?,?,?,?)',('legacy','admin',store.password_hash('test-password-strong'),'admin'))
        con.execute('INSERT INTO sessions VALUES (?,?,?,?)',('old-token','legacy','old-csrf',time.time()+3600))
        con.execute('INSERT INTO settings VALUES (?,?)',('thresholds','{"cpu": 77}'))
        con.execute('INSERT INTO servers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('old','Saved server','localhost',22,'harbour','fingerprint',None,None,'{}',None,None,0))
    store.initialize()
    assert store.one('SELECT name FROM servers')['name']=='Saved server'
    assert store.one('SELECT id FROM users')['id']=='legacy'
    assert not store.rows('SELECT * FROM sessions')
    assert json.loads(store.one('SELECT value FROM settings WHERE key="thresholds"')['value'])['cpu']==77
    assert module.thresholds_for(store.one('SELECT * FROM servers'))['temperature']==80
    store.initialize()  # Migration is repeatable.


@pytest.mark.parametrize('failure,status', [(None,'up'),(OSError('connection refused'),'down'),(ssh.paramiko.AuthenticationException('denied'),'unknown')])
def test_ssh_latency_and_connection_classification(client,monkeypatch,failure,status):
    monkeypatch.setattr(store,'DEMO',False)
    key=client.post('/api/keys',json={}).json()['id']
    commands=[];closed=[]
    class Channel:
        def __init__(self,output=b''):self.output=output
        def exit_status_ready(self):return True
        def recv_exit_status(self):return 0
        def close(self):pass
        def recv_ready(self):return bool(self.output)
        def recv_stderr_ready(self):return False
        def recv(self,size):data=self.output;self.output=b'';return data
        def shutdown_write(self):pass
    class Stream:
        def __init__(self,channel):self.channel=channel
        def write(self,text):pass
        def flush(self):pass
    class FakeClient:
        def set_missing_host_key_policy(self,policy):assert isinstance(policy,ssh.PinnedHostKey)
        def connect(self,*args,**kwargs):
            assert not kwargs['allow_agent'] and not kwargs['look_for_keys']
            assert 'ssh-rsa' in kwargs['disabled_algorithms']['pubkeys']
            if failure:raise failure
        def get_transport(self):return self
        def set_keepalive(self,value):pass
        def exec_command(self,command,timeout):
            commands.append(command)
            stream=Stream(Channel(b'{"metrics": {}, "services": []}' if command=='python3 -' else b''))
            return stream,stream,stream
        def close(self):closed.append(True)
    monkeypatch.setattr(ssh.paramiko,'SSHClient',FakeClient)
    server={'key_id':key,'host':'test.example','port':22,'username':'harbour','fingerprint':'SHA256:fake'}
    if failure:
        with pytest.raises(ssh.ProbeError) as caught:ssh.request(server,{'operation':'snapshot'})
        assert caught.value.status==status
    else:
        result=ssh.request(server,{'operation':'snapshot'})
        assert result['latency_ms']>=0
        assert commands==['true','python3 -']
    assert closed==[True]


@pytest.mark.parametrize('age,resolution',[(21,900),(40,3600),(91,None)])
def test_older_retention_tiers(client,age,resolution):
    metrics=clean_history();now=1800000000
    history.record('atlas',metrics,up=True,now=now-age*86400)
    history.compact(now=now)
    rows=store.rows('SELECT * FROM resource_history')
    if resolution is None:assert not rows
    else:
        assert len(rows)==1 and rows[0]['resolution']==resolution
        assert json.loads(rows[0]['payload'])['n']==1
