"""Resource control authorization, transactional restart and durable outcomes."""
import fcntl
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from test_harbour import client
from harbour import resource_config as config, resource_settings as api, store
from scripts import resource_manager as manager, resource_profile


def settings(profile='standard', **changes):
    return dict(profile=profile, **(config.PROFILES[profile] | changes))


def parts(cfg):
    return [dict(service=name, id=name, memory_mib=cfg[key], swap_mib=cfg[key],
                 running=True, healthy=True, oom=False)
            for name, key in zip(manager.SERVICES, ('web_memory_mib', 'recorder_memory_mib'))]


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(manager.start, 'ROOT', tmp_path)
    (tmp_path / '.env').write_text('FIRST_RUN=False\nHARBOUR_SECRET=keep-private\nHARBOUR_PORT=8484\n')
    directory=manager.prepare()
    result=manager.Manager(directory)
    running={'cfg':settings()}
    monkeypatch.setattr(manager, 'host_memory', lambda:8192)
    monkeypatch.setattr(manager, 'inspect', lambda:(parts(running['cfg']), running['cfg']))
    monkeypatch.setattr(result, 'restart', lambda:running.update(cfg=manager.saved()))
    return result,running


def request(controller, **changes):
    return dict(action='apply', settings=settings(**changes), request_id='a'*36, revision=controller.status()['revision'])


def wait(controller):
    import time
    for _ in range(100):
        if controller.operation['status']!='applying': return controller.operation
        time.sleep(.03)
    pytest.fail('Resource operation did not finish')


@pytest.mark.parametrize('patch', [dict(processing_mib=True),dict(write_interval_seconds=1.5),
    dict(web_memory_mib=100),dict(processing_mib=128),dict(buffer_total_mib=512),dict(unknown=1),
    dict(profile='custom'),dict(profile=['standard'])])
def test_shared_validation_rejects_invalid_values(patch):
    with pytest.raises((ValueError,TypeError)): config.validate(settings()|patch)


def test_success_persists_and_verifies_limits_without_exposing_secrets(controller):
    control,running=controller
    message=request(control,write_interval_seconds=10)
    accepted=control.handle(message)
    assert accepted['operation']['status']=='applying'
    assert wait(control)['status']=='succeeded'
    current=control.status()
    assert current['applied'] and current['active']['write_interval_seconds']==10
    assert 'keep-private' not in json.dumps(current)
    assert 'HARBOUR_SECRET=keep-private' in (manager.start.ROOT/'.env').read_text()
    assert manager.Manager(control.directory).operation['status']=='succeeded'
    # Retrying a lost response returns the saved outcome, never another restart.
    assert control.handle(message)['operation']['status']=='succeeded'


def test_failed_start_restores_previous_settings_and_confirms_rollback(controller,monkeypatch):
    control,running=controller
    calls=[]
    def restart():
        calls.append(manager.saved())
        if len(calls)==1: raise RuntimeError('failure')
        running['cfg']=manager.saved()
    monkeypatch.setattr(control,'restart',restart)
    control.handle(request(control,profile='sbc-1g'))
    outcome=wait(control)
    assert outcome['status']=='failed' and outcome['rolled_back']
    assert manager.saved()==settings() and running['cfg']==settings()
    assert len(calls)==2


def test_readback_mismatch_also_rolls_back(controller,monkeypatch):
    control,_=controller
    monkeypatch.setattr(control,'restart',lambda:None)
    control.handle(request(control,write_interval_seconds=5))
    assert wait(control)['rolled_back']


def test_busy_stale_and_startup_lock_reject_without_rewriting(controller):
    control,_=controller
    message=request(control)
    assert control.handle(message|{'revision':'old'})['code']==409
    with (manager.start.ROOT/'.env.setup.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert control.handle(message)['code']==409
    control.handle(message)
    assert control.handle(message|{'request_id':'b'*36})['code']==409
    assert wait(control)['status']=='succeeded'


def test_interrupted_operation_is_not_replayed(controller):
    control,_=controller
    control.record(request_id='a'*36,status='applying',settings=settings(),message='Restarting')
    recovered=manager.Manager(control.directory)
    assert recovered.operation['status']=='failed'
    assert 'restarted during' in recovered.operation['message']


def test_host_memory_and_unknown_actions_are_rejected(controller,monkeypatch):
    control,_=controller
    monkeypatch.setattr(manager,'host_memory',lambda:980)
    with pytest.raises(ValueError,match='128 MiB'): control.handle(request(control))
    for body in ({'action':'shell','command':'true'}, {'action':'status','path':'/etc/passwd'}):
        with pytest.raises(ValueError): control.handle(body)


def test_reading_status_does_not_rewrite_file_metadata(controller,monkeypatch):
    control,_=controller
    control.status()  # First read hardens a legacy file's permissions if needed.
    monkeypatch.setattr(manager.start.os,'chmod',lambda *args:pytest.fail('Status must not create metadata writes'))
    assert control.status()['applied']


def test_both_caps_and_no_swap_must_be_verified():
    cfg=settings(); observed=parts(cfg)
    assert manager.verified(observed,cfg,cfg)
    observed[1]['swap_mib']=-1
    assert not manager.verified(observed,cfg,cfg)
    observed=parts(cfg);observed[0]['healthy']=False
    assert not manager.verified(observed,cfg,cfg)


def test_inspect_reads_actual_caps_and_filters_credentials(monkeypatch):
    cfg=settings()
    containers=[dict(Id='a'*64,Config=dict(Labels={'com.docker.compose.service':service},
        Env=['HARBOUR_SECRET=private']+['HARBOUR_RESOURCE_PROFILE=standard']+
        ['HARBOUR_'+k.upper()+'='+str(v) for k,v in config.PROFILES['standard'].items()]),
        HostConfig=dict(Memory=768*1048576,MemorySwap=768*1048576),State=dict(Running=True)) for service in manager.SERVICES]
    monkeypatch.setattr(manager,'compose',lambda *a:'a'*64)
    monkeypatch.setattr(manager,'command',lambda *a:json.dumps(containers))
    observed,actual=manager.inspect()
    assert actual==cfg and 'private' not in json.dumps([observed,actual])
    containers[1]['HostConfig']['Memory']=736*1048576
    assert manager.inspect()[1]['recorder_memory_mib']==736
    containers[1]['Config']['Env'].append('HARBOUR_PROCESSING_MIB=16')
    assert manager.inspect()[1] is None


def test_install_enables_persistent_user_service(controller,monkeypatch,tmp_path):
    monkeypatch.setattr(manager.sys,'platform','linux')
    monkeypatch.setattr(manager.Path,'home',lambda:tmp_path)
    calls=[]
    def command(args,*unused):
        calls.append(args)
        if args==['id','-un']: return 'example\n'
        if args[0]=='loginctl' and args[1]=='show-user': return 'no\n'
        return ''
    monkeypatch.setattr(manager,'command',command)
    manager.install()
    assert ['loginctl','enable-linger','example'] in calls
    assert ['systemctl','--user','restart','harbour-resources.service'] in calls
    unit=(tmp_path/'.config/systemd/user/harbour-resources.service').read_text()
    assert 'resource_manager.py' in unit and 'Restart=on-failure' in unit
    assert 'HARBOUR_SECRET' not in unit and 'After=default.target' not in unit


def test_admin_csrf_demo_and_unavailable_controller(client,monkeypatch):
    assert client.get('/api/resource-settings').json()['available'] is False
    body=dict(request_id='a'*36,revision='b'*64,settings=settings())
    assert client.post('/api/resource-settings',json=body).status_code==409
    monkeypatch.setattr(store,'DEMO',False)
    monkeypatch.setattr(api,'exchange',lambda message:dict(available=True,operation={'status':'applying'}))
    assert client.post('/api/resource-settings',json=body).status_code==202
    assert client.post('/api/resource-settings',json=body|{'settings':settings(processing_mib=128)}).status_code==422
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/resource-settings',json=body).status_code==403
    client.cookies.clear()
    assert client.get('/api/resource-settings').status_code==401


def test_viewers_cannot_read_or_change_deployment(client):
    client.post('/api/users',json=dict(name='viewer',password='long-viewer-password',role='user'))
    login=client.post('/api/login',json=dict(name='viewer',password='long-viewer-password')).json()
    client.headers['X-CSRF-Token']=login['csrf']
    assert client.get('/api/resource-settings').status_code==403
    assert client.post('/api/resource-settings',json=dict(request_id='a'*36,revision='b'*64,settings=settings())).status_code==403


@pytest.mark.skipif(not hasattr(socket,'SO_PEERCRED'),reason='Linux peer credentials')
def test_unrelated_local_uid_cannot_use_host_control_socket(monkeypatch):
    handler=manager.Handler.__new__(manager.Handler)
    handler.connection=SimpleNamespace(settimeout=lambda _:None,
        getsockopt=lambda *args:manager.struct.pack('3i',123,65432,65432))
    handler.rfile=SimpleNamespace(readline=lambda *args:pytest.fail('Unauthorized peer reached the request parser'))
    handler.handle()


@pytest.mark.skipif(not hasattr(socket,'SO_PEERCRED'),reason='Linux peer credentials')
def test_real_socket_protocol_is_bounded_and_closes_cleanly(controller,monkeypatch,tmp_path):
    control,_=controller
    path=tmp_path/'r.sock'
    monkeypatch.setattr(api,'SOCKET',str(path))
    with manager.socketserver.UnixStreamServer(str(path),manager.Handler) as server:
        server.manager=control
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            assert api.exchange({'action':'status'})['active']==settings()
            with pytest.raises(api.HTTPException) as caught: api.exchange({'action':'other'})
            assert caught.value.status_code==400
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.connect(str(path));connection.sendall(b'x'*4097)
                assert json.loads(connection.recv(4096))['code']==400
        finally:
            server.shutdown();thread.join(2)
