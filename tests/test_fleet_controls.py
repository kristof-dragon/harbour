import builtins
import copy
import io
import json
import time
from types import SimpleNamespace

import pytest

from test_harbour import client, wait_job
from harbour import app as module, history, remote_probe, store, volumes


def readings():
    return json.loads(store.one('SELECT snapshot FROM servers WHERE id="atlas"')['snapshot'])


def test_volume_warning_card_selection_and_history_are_independent(client):
    snapshot = readings()
    snapshot['metrics']['disks'] = [
        {'mount': '/', 'total': 10e9, 'used': 2e9, 'free': 8e9, 'percent': 20},
        {'mount': '/home', 'total': 1e12, 'used': 4e11, 'free': 6e11, 'percent': 40},
        {'mount': '/var/log', 'total': 50e6, 'used': 10e6, 'free': 40e6, 'percent': 20}]
    store.execute('UPDATE servers SET snapshot=? WHERE id="atlas"', (json.dumps(snapshot),))
    initial = client.get('/api/dashboard').json()['servers'][0]
    assert [d['mount'] for d in initial['metrics']['disks'] if d['card']] == ['/']
    assert '40.0 MB free' in next(w['detail'] for w in initial['warnings'] if w['id'] == 'disk:/var/log')
    config = [{'mount':'/','monitor':True,'warn':False,'card':False},
              {'mount':'/home','monitor':True,'warn':True,'card':True},
              {'mount':'/var/log','monitor':False,'warn':False,'card':False}]
    assert client.put('/api/servers/atlas/volumes', json={'volumes':config}).status_code == 200
    store.initialize()
    updated = client.get('/api/dashboard').json()['servers'][0]
    assert not any(w['id'].startswith('disk:') for w in updated['warnings'])
    assert len(updated['metrics']['disks']) == 3
    store.execute('DELETE FROM resource_history')
    now = int(time.time()//60)*60
    history.record('atlas', snapshot['metrics'], up=True, now=now-120)
    result = history.series('atlas', 1, now=now)
    assert result['points'][0]['disk'] == 40
    assert {d['mount'] for d in result['points'][0]['disks']} == {'/','/home'}
    assert history.series('atlas', 1, now=now, disk_mount='/')['points'][0]['disk'] == 20
    assert client.get('/api/servers/atlas/history?disk=%2F').status_code == 200
    # Disabling a volume stops future storage but preserves its old buckets.
    config[1].update(monitor=False,warn=False,card=False)
    client.put('/api/servers/atlas/volumes', json={'volumes':config})
    history.record('atlas', snapshot['metrics'], up=True, now=now-60)
    points = history.series('atlas',1,now=now,disk_mount='/home')['points']
    assert points[0]['disk'] == 40 and points[1]['disk'] is None
    snapshot['metrics']['disks'] = snapshot['metrics']['disks'][:1]
    store.execute('UPDATE servers SET snapshot=? WHERE id="atlas"', (json.dumps(snapshot),))
    disks = client.get('/api/dashboard').json()['servers'][0]['metrics']['disks']
    assert not next(d for d in disks if d['mount']=='/home')['present']
    assert client.put('/api/servers/atlas/volumes',json={'volumes':[{'mount':'/unknown','monitor':True,'warn':True,'card':True}]}).status_code==400
    assert client.put('/api/servers/atlas/volumes',json={'volumes':[{'mount':'/','monitor':False,'warn':True,'card':True}]}).status_code==400


@pytest.mark.parametrize('name',['cpu-thermal','cpu_thermal','soc-thermal','soc_thermal','package-thermal','package_thermal','bcm2835_thermal'])
def test_arm_package_zones_are_found_alongside_unrelated_hwmon(tmp_path, name):
    hw = tmp_path/'class/hwmon/hwmon0'; hw.mkdir(parents=True)
    (hw/'name').write_text('nvme'); (hw/'temp1_input').write_text('96000')
    for index,(label,value) in enumerate([(name,52000),('bigcore0-thermal',94000),('gpu-thermal',85000)]):
        zone=tmp_path/f'class/thermal/thermal_zone{index}';zone.mkdir(parents=True)
        (zone/'type').write_text(label);(zone/'temp').write_text(str(value))
    result=remote_probe.temperatures(str(tmp_path))
    assert result['package']==52 and result['package_label']==name
    assert next(s for s in result['sensors'] if s['label']=='bigcore0-thermal')['kind']=='cpu_auxiliary'
    # hwmon and thermal can expose the same sensor under hyphen/underscore aliases.
    hw2=tmp_path/'class/hwmon/hwmon1';hw2.mkdir()
    (hw2/'name').write_text(name.replace('-','_'));(hw2/'temp1_input').write_text('52000')
    assert remote_probe.temperatures(str(tmp_path))['package_count']==1


def test_intel_package_is_not_duplicated_by_thermal_zone(tmp_path):
    hw = tmp_path/'class/hwmon/hwmon0'; hw.mkdir(parents=True)
    (hw/'name').write_text('coretemp')
    (hw/'temp1_label').write_text('Package id 0')
    (hw/'temp1_input').write_text('52000')
    zone = tmp_path/'class/thermal/thermal_zone0'; zone.mkdir(parents=True)
    (zone/'type').write_text('x86_pkg_temp'); (zone/'temp').write_text('53000')
    result = remote_probe.temperatures(str(tmp_path))
    assert result['package'] == 52 and result['package_count'] == 1


def test_plain_mode_never_invokes_docker_and_can_be_switched(client,monkeypatch):
    before = store.one('SELECT COUNT(*) n FROM resource_history')['n']
    assert client.put('/api/servers/atlas/type',json={'server_type':'plain'}).status_code==200
    server=client.get('/api/dashboard').json()['servers'][0]
    assert server['server_type']=='plain' and server['services']==[] and server['updates']==0
    assert not any(w['id'].startswith('service:') for w in server['warnings'])
    for action in ['pull','up','pull_up','restart']:
        assert client.post('/api/servers/atlas/plan',json={'action':action,'targets':['group:immich']}).status_code==400
    assert client.post('/api/servers/atlas/check-updates').status_code==400
    snapshot=readings()
    monkeypatch.setattr(remote_probe,'inventory',lambda:pytest.fail('Plain mode called Docker inventory'))
    def metrics(docker=True):
        assert docker is False
        return snapshot['metrics']
    monkeypatch.setattr(remote_probe,'metrics',metrics)
    assert remote_probe.handle({'operation':'snapshot','server_type':'plain','updates':True})['services']==[]
    with pytest.raises(ValueError,match='disabled'):
        remote_probe.handle({'operation':'execute','server_type':'plain'})
    lock=module.server_lock('atlas');lock.acquire()
    try:assert client.put('/api/servers/atlas/type',json={'server_type':'docker'}).status_code==409
    finally:lock.release()
    assert client.put('/api/servers/atlas/type',json={'server_type':'docker'}).status_code==200
    assert store.one('SELECT COUNT(*) n FROM resource_history')['n']==before
    assert store.one('SELECT server_type FROM servers WHERE id="atlas"')['server_type']=='docker'


def test_plain_creation_and_refresh_payload(client,monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    monkeypatch.setattr(module,'queue_job',lambda *args,**kw:{'id':'synthetic'})
    key=client.post('/api/keys',json={}).json()
    result=client.post('/api/servers',json={'name':'Plain','host':'host.example','port':22,'username':'harbour','fingerprint':'SHA256:'+'a'*43,'key_id':key['id'],'server_type':'plain'})
    assert result.status_code==200,result.text
    id_=result.json()['id']; server=module.get_server(id_)
    assert server['server_type']=='plain'
    snap=readings()
    def request(server,payload):
        assert payload=={'operation':'resources'}
        return copy.deepcopy(snap)
    monkeypatch.setattr(module.ssh,'request',request)
    module.refresh_server(id_,updates=True)
    saved=json.loads(module.get_server(id_)['snapshot'])
    assert saved['services']==[] and saved['metrics']['docker'] is None


def test_mount_discovery_keeps_equal_capacity_mounts_and_plain_metrics_skip_docker(monkeypatch):
    original_open=builtins.open
    def open_(path,*args,**kwargs):
        data={'/proc/stat':'cpu 100 0 0 100 0 0 0 0\n','/proc/meminfo':'MemTotal: 1000 kB\nMemAvailable: 500 kB\n','/proc/mounts':'/dev/root / ext4 rw 0 0\n/dev/root /home ext4 rw 0 0\n','/proc/uptime':'1000 0\n','/etc/os-release':'PRETTY_NAME="Test Linux"\n'}
        return io.StringIO(data[path]) if path in data else original_open(path,*args,**kwargs)
    monkeypatch.setattr(builtins,'open',open_)
    monkeypatch.setattr(remote_probe.time,'sleep',lambda _:None)
    monkeypatch.setattr(remote_probe.os,'statvfs',lambda _:SimpleNamespace(f_blocks=100,f_bavail=60,f_bfree=60,f_frsize=4096))
    monkeypatch.setattr(remote_probe,'run',lambda *a,**kw:pytest.fail('Plain metrics executed Docker'))
    monkeypatch.setattr(remote_probe,'timezone_info',lambda:{})
    result=remote_probe.metrics(docker=False)
    assert [d['mount'] for d in result['disks']]==['/','/home'] and result['docker'] is None


def test_order_persists_and_requires_complete_unique_list(client):
    ids=[s['id'] for s in client.get('/api/dashboard').json()['servers']]
    order=list(reversed(ids))
    assert client.put('/api/server-order',json={'ids':order}).status_code==200
    store.initialize()
    assert [s['id'] for s in client.get('/api/dashboard').json()['servers']]==order
    for bad in [ids[:-1],[ids[0]]*len(ids),ids+['unknown']]:
        assert client.put('/api/server-order',json={'ids':bad}).status_code==409
    client.post('/api/users',json={'name':'reader','password':'reader-password-long','role':'user'})
    login=client.post('/api/login',json={'name':'reader','password':'reader-password-long'}).json()
    client.headers['X-CSRF-Token']=login['csrf']
    assert client.put('/api/server-order',json={'ids':ids}).status_code==403
    assert client.put('/api/servers/atlas/type',json={'server_type':'plain'}).status_code==403
    assert client.put('/api/servers/atlas/volumes',json={'volumes':[]}).status_code==403


def test_pull_apply_order_short_circuit_and_standalone_rejection(client,monkeypatch):
    services=readings()['services'];targets=['group:immich','group:gateway']
    plan=remote_probe.plan(services,'pull_up',targets)
    assert len(plan)==4 and all('pull' in c['argv'] for c in plan[:2]) and all('up' in c['argv'] for c in plan[2:])
    individual=remote_probe.plan(services,'pull_up',['immich-web'])
    assert '--no-deps' in individual[1]['argv']
    standalone=next(s['id'] for s in services if not s['project'])
    with pytest.raises(ValueError,match='standalone'):
        remote_probe.plan(services,'pull_up',[standalone])
    monkeypatch.setattr(remote_probe,'inventory',lambda:services)
    called=[]
    def failing(argv,**kwargs):
        called.append(argv)
        if len(called)==2:raise RuntimeError('Registry unavailable')
        return 'done'
    monkeypatch.setattr(remote_probe,'run',failing)
    result=remote_probe.handle({'operation':'execute','action':'pull_up','targets':targets,'expected':plan})
    assert not result['ok'] and result['completed']==1 and len(called)==2
    assert all('up' not in argv for argv in called)
    preview=client.post('/api/servers/atlas/plan',json={'action':'pull_up','targets':['group:immich']})
    assert preview.status_code==200,preview.text
    result=client.post('/api/servers/atlas/execute',json={'token':preview.json()['token']})
    assert wait_job(client,result.json()['id'])['status']=='succeeded'
    assert client.get('/api/dashboard').json()['servers'][0]['updates']==1


def test_sample_times_survive_rollup_for_gap_detection(client):
    store.execute('DELETE FROM resource_history');now=1800000000;stamp=now-9*86400
    for when in [stamp+3,stamp+64,stamp+127]:
        history.record('atlas',readings()['metrics'],up=True,now=when)
    history.compact(now=now)
    point=history.series('atlas',240,now=now)['points'][0]
    assert point['sample_first']==stamp+3 and point['sample_last']==stamp+127
    assert point['samples']==3


def test_available_version_uses_exact_manifest_and_never_pulls(monkeypatch):
    config_digest='sha256:'+'b'*64; manifest_digest='sha256:'+'c'*64
    raw=[{'Descriptor':{'digest':manifest_digest,'platform':{'os':'linux','architecture':'arm64'}},
          'OCIManifest':{'config':{'digest':config_digest}}}]
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='manifest':return json.dumps(raw)
        assert argv[-1]=='registry.example:5000/team/service@'+manifest_digest
        assert argv[1:4]==['buildx','imagetools','inspect']
        return json.dumps({'config':{'Labels':{'org.opencontainers.image.version':'2.1.0'}}})
    monkeypatch.setattr(remote_probe,'run',run)
    service={'image':'registry.example:5000/team/service:latest','image_id':'sha256:'+'a'*64,'version':'2.0.0','platform':{'os':'linux','architecture':'arm64'}}
    result=remote_probe.check_updates([service,copy.deepcopy(service)])
    assert result[0]['update']['version']=='2.1.0' and result[0]['version']=='2.0.0'
    assert result[1]['update']['status']=='available' and len(calls)==2
    def missing(argv,**kwargs):
        if argv[1]=='manifest':return json.dumps(raw)
        raise RuntimeError('Buildx unavailable')
    monkeypatch.setattr(remote_probe,'run',missing)
    result=remote_probe.check_updates([service])[0]
    assert result['update']['status']=='unverified' and result['update']['version'] is None
    raw[0]['OCIManifest']['annotations']={'org.opencontainers.image.version':'2.2.0'}
    assert remote_probe.check_updates([service])[0]['update']['version']=='2.2.0'
