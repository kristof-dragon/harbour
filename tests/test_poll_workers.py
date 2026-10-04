import copy
import json
import socket
import threading
import time

import pytest
import paramiko

from test_harbour import client
from harbour import app as module, history, polling, remote_probe, ssh, store

REAL_POLL_LOOP = module.poll_loop


def eventually(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


def samples(id_='atlas'):
    return sum(json.loads(r['payload'])['n'] for r in store.rows('SELECT payload FROM resource_history WHERE server_id=?', (id_,)))


def prepare():
    store.execute('DELETE FROM resource_history')
    store.execute("UPDATE servers SET server_type='plain',last_attempt=0,monitoring_enabled=0")
    store.execute("UPDATE servers SET monitoring_enabled=1 WHERE id='atlas'")
    return json.loads(module.get_server('atlas')['snapshot'])['metrics']


def test_more_slow_hosts_than_old_pool_capacity_do_not_delay_fast_host():
    release = threading.Event()
    started = {id_: threading.Event() for id_ in ['fast', *[f'slow-{i}' for i in range(6)]]}
    calls = []
    def run(id_, cancel):
        calls.append((id_, threading.current_thread().name))
        started[id_].set()
        if id_ != 'fast':
            while not release.wait(.005): cancel.check()
    workers = polling.ServerWorkers(run)
    try:
        workers.sync({id_: ('connection', True, .03) for id_ in started})
        threads = {id_: w.thread for id_, w in workers.workers.items()}
        for id_ in started: assert workers.request(id_)
        assert all(event.wait(1) for event in started.values())
        for id_ in started:
            if id_ != 'fast': assert not workers.request(id_)
        eventually(lambda: not workers.workers['fast'].active)
        time.sleep(.04)
        assert workers.request('fast')
        eventually(lambda: sum(id_ == 'fast' for id_, _ in calls) == 2)
        assert all(workers.workers[id_].thread is thread for id_, thread in threads.items())
        assert len(calls) == 8  # No overlapping slow checks and no catch-up queue.
    finally:
        release.set(); workers.close()
    assert not any(t.is_alive() for t in threads.values())


def test_worker_cadence_skips_missed_slots_and_applies_interval_changes():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(id_, cancel):
        calls.append(time.monotonic()); entered.set()
        if len(calls) == 1: release.wait(2)
    workers = polling.ServerWorkers(run)
    try:
        workers.sync({'one': ('host', True, .1)})
        assert workers.request('one') and entered.wait(1)
        time.sleep(.22)
        assert not workers.request('one')
        release.set(); eventually(lambda: not workers.workers['one'].active)
        assert not workers.request('one')  # Lost slots aren't replayed at completion.
        time.sleep(.11); assert workers.request('one')
        eventually(lambda: len(calls) == 2 and not workers.workers['one'].active)
        workers.sync({'one': ('host', True, 60)})
        assert not workers.request('one')
        workers.sync({'one': ('host', True, .001)})
        time.sleep(.01); assert workers.request('one')
        eventually(lambda: len(calls) == 3)
    finally: release.set(); workers.close()


def test_resources_continue_with_many_slow_or_failed_hosts(client, monkeypatch):
    metrics = prepare()
    for i in range(6):
        source = module.get_server('atlas') | {'id':f'slow-{i}', 'name':f'Slow {i}'}
        columns = ','.join(source)
        store.execute(f"INSERT INTO servers ({columns}) VALUES ({','.join('?' for _ in source)})", tuple(source.values()))
    store.execute("UPDATE servers SET monitoring_enabled=1 WHERE id='luna'")
    started = {f'slow-{i}':threading.Event() for i in range(6)}
    release = threading.Event()
    def request(server, payload, cancel=None):
        assert payload == {'operation':'resources'}
        if server['id'] in started:
            started[server['id']].set()
            while not release.wait(.01): cancel.check()
        if server['id'] == 'luna': raise ssh.ProbeError('synthetic timeout', 'down')
        return {'metrics':copy.deepcopy(metrics), 'latency_ms':4}
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(ssh, 'request', request)
    try:
        module.poll_due()
        assert all(event.wait(1) for event in started.values())
        eventually(lambda: samples() == 1 and module.get_server('luna')['connection_status'] == 'down')
        assert module.get_server('atlas')['connection_status'] == 'up'
        assert all(samples(id_) == 0 for id_ in started)
        assert history.series('luna',1)['points'][-1]['samples'] == 0
        threads = {id_:w.thread for id_,w in module.resource_workers.workers.items()}
        module.poll_due()
        assert samples() == 1
        assert all(module.resource_workers.workers[id_].thread is thread for id_,thread in threads.items())
    finally: release.set()
    eventually(lambda: all(not w.active for w in module.resource_workers.workers.values()))


def test_manual_and_background_resource_checks_never_overlap(client, monkeypatch):
    metrics = prepare()
    started, release = threading.Event(), threading.Event()
    calls = 0
    active = 0
    maximum = 0
    lock = threading.Lock()
    def request(server, payload, cancel=None):
        nonlocal calls, active, maximum
        with lock: calls += 1; active += 1; maximum = max(active, maximum)
        started.set()
        try:
            if calls == 1: assert release.wait(2)
            return {'metrics':copy.deepcopy(metrics)}
        finally:
            with lock: active -= 1
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(ssh, 'request', request)
    module.poll_due(); assert started.wait(1)
    errors = []
    def manual():
        try: module.refresh_server('atlas')
        except Exception as exc: errors.append(exc)
    thread = threading.Thread(target=manual); thread.start()
    try:
        module.poll_due(); time.sleep(.05)
        assert calls == 1
    finally: release.set(); thread.join(2)
    assert not thread.is_alive() and errors == [] and maximum == 1 and calls == 2
    assert samples() == 2


@pytest.mark.parametrize('change', ['pause', 'connection', 'delete'])
@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_late_response_from_changed_or_removed_host_is_discarded(client, monkeypatch, change, outcome):
    metrics = prepare()
    metrics['cpu_counters'] = {'values': [100, 0, 0, 900, 0, 0, 0, 0],
                               'boot_id': 'old-host', 'cores': 4, 'uptime': 1000}
    started, release = threading.Event(), threading.Event()
    def request(server, payload, cancel=None):
        started.set(); assert release.wait(2)  # Deliberately ignores cancellation.
        if outcome == 'failure': raise ssh.ProbeError('old connection failed', 'down')
        return {'metrics':copy.deepcopy(metrics)}
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(ssh, 'request', request)
    module.poll_due(); assert started.wait(1)
    worker = module.resource_workers.workers['atlas']
    before = module.get_server('atlas')['checked']
    try:
        if change == 'pause':
            assert client.put('/api/servers/atlas/monitoring',json={'enabled':False,'poll_seconds':15}).status_code == 200
        elif change == 'connection':
            store.execute("UPDATE servers SET host='new.invalid',checked=NULL,connection_status='pending' WHERE id='atlas'")
        else:
            assert client.delete('/api/servers/atlas').status_code == 200
        module.poll_due()
        assert worker.cancel.is_set()
    finally: release.set()
    eventually(lambda: not worker.active)
    assert samples() == 0
    server = store.one("SELECT * FROM servers WHERE id='atlas'")
    if change == 'delete':
        eventually(lambda: not worker.thread.is_alive())
        assert server is None and 'atlas' not in module.resource_workers.workers
    else:
        assert server['error'] is None
        assert server['checked'] == (None if change == 'connection' else before)
        assert '_cpu_baseline' not in json.loads(server['snapshot'])


def test_resume_new_host_and_global_interval_changes(client, monkeypatch):
    prepare()
    module.poll_due(); eventually(lambda: samples() == 1)
    worker = module.resource_workers.workers['atlas']
    assert client.put('/api/servers/atlas/monitoring',json={'enabled':False,'poll_seconds':15}).status_code == 200
    module.poll_due(); assert not worker.enabled
    assert client.put('/api/servers/atlas/monitoring',json={'enabled':True,'poll_seconds':15}).status_code == 200
    module.poll_due(); eventually(lambda: samples() == 2)
    assert module.resource_workers.workers['atlas'] is worker
    assert client.put('/api/monitoring',json={**store.MONITORING_DEFAULTS,'poll_seconds':300}).status_code == 200
    module.poll_due()
    assert worker.interval == 15  # Per-host override wins.
    assert module.resource_workers.workers['luna'].interval == 300
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(module, 'queue_job', lambda *args,**kw:{'id':'manual-not-started'})
    key = client.post('/api/keys',json={}).json()
    result = client.post('/api/servers',json={'name':'Added','host':'added.invalid','username':'harbour','fingerprint':'SHA256:'+'a'*43,'key_id':key['id'],'server_type':'plain'})
    assert result.status_code == 200
    monkeypatch.setattr(store, 'DEMO', True)
    # Provide the demo reading normally supplied by the first SSH request.
    store.execute('UPDATE servers SET snapshot=? WHERE id=?',(module.get_server('atlas')['snapshot'],result.json()['id']))
    module.poll_due(); eventually(lambda: samples(result.json()['id']) == 1)


def test_slow_inventory_never_overwrites_new_readings_or_creates_history_gaps(client, monkeypatch):
    metrics = prepare()
    store.execute("UPDATE servers SET server_type='docker' WHERE id='atlas'")
    inventory_started, release = threading.Event(), threading.Event()
    def request(server, payload, cancel=None):
        if payload['operation'] == 'resources':
            return {'metrics':copy.deepcopy(metrics) | {'cpu':77},'latency_ms':3}
        assert payload['resources'] is False
        inventory_started.set(); assert release.wait(2)
        return {'services':[], 'docker':'30', 'metrics':{'cpu':1}}
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(ssh, 'request', request)
    thread = threading.Thread(target=module.refresh_inventory,args=('atlas',)); thread.start()
    try:
        assert inventory_started.wait(1)
        lock=module.resource_lock('atlas'); lock.acquire(); module.poll_resources(module.get_server('atlas'),lock)
        before=module.get_server('atlas')
        assert samples() == 1
    finally: release.set(); thread.join(2)
    saved=module.get_server('atlas'); snapshot=json.loads(saved['snapshot'])
    assert saved['checked'] == before['checked'] and saved['latency_ms'] == 3
    assert snapshot['metrics']['cpu'] == 77 and snapshot['metrics']['docker'] == '30' and samples() == 1
    monkeypatch.setattr(ssh,'request',lambda *a,**kw:(_ for _ in ()).throw(ssh.ProbeError('Docker unavailable','down')))
    with pytest.raises(ssh.ProbeError): module.refresh_inventory('atlas')
    assert module.get_server('atlas')['connection_status'] == 'up' and samples() == 1
    public=client.get('/api/dashboard').json()['servers'][0]
    assert any(w['id']=='docker' for w in public['warnings']) and not any(w['id']=='connection' for w in public['warnings'])


def test_inventory_before_first_resource_does_not_create_partial_metrics(client, monkeypatch):
    store.execute("UPDATE servers SET snapshot='{}',checked=NULL WHERE id='atlas'")
    monkeypatch.setattr(store,'DEMO',False)
    monkeypatch.setattr(ssh,'request',lambda *a,**kw:{'services':[],'docker':'30'})
    module.refresh_inventory('atlas')
    snapshot=json.loads(module.get_server('atlas')['snapshot'])
    assert 'metrics' not in snapshot
    assert client.get('/api/dashboard').status_code == 200


def test_inventory_probe_never_samples_resources(monkeypatch):
    monkeypatch.setattr(remote_probe,'metrics',lambda **kw:pytest.fail('Inventory sampled resources'))
    monkeypatch.setattr(remote_probe,'inventory',lambda:[])
    monkeypatch.setattr(remote_probe,'run',lambda *a,**kw:'30.0.0')
    assert remote_probe.handle({'operation':'snapshot','resources':False}) == {'services':[],'docker':'30.0.0'}


def test_shutdown_cancels_active_worker_and_joins_all_threads(client, monkeypatch):
    prepare()
    started = threading.Event()
    def request(server,payload,cancel=None):
        started.set()
        assert cancel.event.wait(2)
        cancel.check()
    monkeypatch.setattr(store,'DEMO',False)
    monkeypatch.setattr(ssh,'request',request)
    module.poll_due(); assert started.wait(1)
    workers=list(module.resource_workers.workers.values())
    module.resource_workers.close()
    assert all(not w.thread.is_alive() for w in workers)
    assert not module.resource_lock('atlas').locked() and samples() == 0
    assert module.get_server('atlas')['error'] is None
    assert not module.resource_workers.request('atlas')


def test_ssh_cancellation_closes_active_connection_without_health_error(client, monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    monkeypatch.setattr(ssh,'stored_key',lambda id_:object())
    entered, closed = threading.Event(), threading.Event()
    class Channel:
        def exit_status_ready(self): entered.set(); return False
    class Connection:
        def set_missing_host_key_policy(self,policy): pass
        def connect(self,*args,**kwargs): pass
        def get_transport(self): return self
        def set_keepalive(self,interval): pass
        def close(self): closed.set()
        def exec_command(self,*args,**kwargs):
            return None,type('Reply',(),{'channel':Channel()})(),None
    monkeypatch.setattr(ssh.paramiko,'SSHClient',Connection)
    cancel=ssh.Cancellation(); errors=[]
    def run():
        try: ssh.request(module.get_server('atlas'),{'operation':'resources'},cancel=cancel)
        except Exception as exc: errors.append(exc)
    thread=threading.Thread(target=run); thread.start()
    assert entered.wait(1); cancel.stop(); thread.join(1)
    assert closed.is_set() and not thread.is_alive() and len(errors)==1 and isinstance(errors[0],ssh.Cancelled)


def test_live_scheduler_wakes_for_settings_changes_and_stops_cleanly(client):
    prepare()
    store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    thread=threading.Thread(target=REAL_POLL_LOOP);thread.start()
    try:
        assert client.put('/api/servers/atlas/monitoring',json={'enabled':True,'poll_seconds':15}).status_code == 200
        eventually(lambda:samples()==1)
        assert client.put('/api/servers/atlas/monitoring',json={'enabled':False,'poll_seconds':15}).status_code == 200
        eventually(lambda:not module.resource_workers.workers['atlas'].enabled)
        assert client.delete('/api/servers/atlas').status_code == 200
        eventually(lambda:'atlas' not in module.resource_workers.workers)
    finally:
        module.stop.set();module.scheduler_wake.set();thread.join(2)
    assert not thread.is_alive()


def test_worker_exception_does_not_strand_resource_lock(client,monkeypatch):
    def fail(*args,**kwargs):raise RuntimeError('Unexpected worker error')
    monkeypatch.setattr(module,'poll_resources',fail)
    with pytest.raises(RuntimeError,match='Unexpected worker error'):
        module.run_resource_worker('atlas',ssh.Cancellation())
    assert not module.resource_lock('atlas').locked()


def test_real_ssh_stalled_response_is_cancelled(client,monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    host_key=paramiko.ECDSAKey.generate()
    entered, finished, stop_host = threading.Event(),threading.Event(),threading.Event()
    channels=[];server_errors=[];client_errors=[]
    class Host(paramiko.ServerInterface):
        def check_auth_password(self,username,password):return paramiko.AUTH_SUCCESSFUL
        def get_allowed_auths(self,username):return 'password'
        def check_channel_request(self,kind,channel_id):return paramiko.OPEN_SUCCEEDED
        def check_channel_exec_request(self,channel,command):
            channels.append(channel);entered.set();return True  # Deliberately never reports an exit status.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(2)
        target=module.get_server('atlas') | {'host':'127.0.0.1','port':listener.getsockname()[1],
            'fingerprint':ssh.host_fingerprint(host_key),'auth_method':'password',
            'password_encrypted':store.cipher().encrypt(b'disposable-test-password').decode()}
        def host():
            try:
                connection,_=listener.accept()
                with paramiko.Transport(connection) as transport:
                    transport.add_server_key(host_key);transport.start_server(server=Host())
                    while transport.is_active() and not stop_host.wait(.01):pass
            except Exception as exc:server_errors.append(exc)
            finally:finished.set()
        host_thread=threading.Thread(target=host);host_thread.start()
        cancel=ssh.Cancellation()
        def probe():
            try:ssh.request(target,{'operation':'resources'},cancel=cancel)
            except Exception as exc:client_errors.append(exc)
        probe_thread=threading.Thread(target=probe);probe_thread.start()
        try:
            assert entered.wait(2)
            cancel.stop();probe_thread.join(2)
            assert finished.wait(2)
            assert not probe_thread.is_alive()
            assert len(client_errors)==1 and isinstance(client_errors[0],ssh.Cancelled)
        finally:
            cancel.stop();stop_host.set();probe_thread.join(2);host_thread.join(2)
        assert server_errors==[] and not host_thread.is_alive()
