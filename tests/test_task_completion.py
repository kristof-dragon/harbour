import copy
import json
import threading
import time

import pytest

from test_harbour import client, wait_job
from test_task_queue import submit
from harbour import app as module, demo, store


@pytest.mark.parametrize('action', ['pull', 'up', 'pull_up', 'restart', 'start', 'stop', 'prune'])
def test_docker_completion_does_not_wait_for_followup(client, monkeypatch, action):
    monkeypatch.setattr(store, 'DEMO', False)
    calls = []
    def request(server, payload, on_event=None):
        calls.append(payload['operation'])
        assert payload['operation'] == 'execute'
        on_event({'kind': 'completed', 'completed': len(payload['expected']), 'total': len(payload['expected'])})
        return {'ok': True, 'output': 'Docker commands finished'}
    monkeypatch.setattr(module.ssh, 'request', request)
    targets = [] if action == 'prune' else ['group:immich']
    preview = client.post('/api/servers/atlas/plan', json={'action': action, 'targets': targets}).json()
    result = client.post('/api/servers/atlas/execute', json={'token': preview['token']})
    job = wait_job(client, result.json()['id'])
    assert job['status'] == 'succeeded' and job['progress']['completed'] == job['progress']['total']
    assert calls == ['execute'] and 'atlas' in module.detail_refreshes
    assert not module.server_lock('atlas').locked()


def test_next_job_starts_before_deferred_refresh_and_paused_host_gets_local_details(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    store.execute('UPDATE servers SET monitoring_enabled=0,update_checked=0')
    command_started = threading.Event();release = threading.Event();calls = []
    def request(server, payload, on_event=None):
        calls.append(payload['action'])
        if payload['action'] == 'pull_up':
            command_started.set();assert release.wait(3)
        return {'ok': True, 'output': 'Done'}
    monkeypatch.setattr(module.ssh, 'request', request)
    first = submit(client, 'pull_up');assert command_started.wait(1)
    try:second = submit(client, 'restart')
    finally:release.set()
    assert wait_job(client, first)['status'] == 'succeeded'
    assert wait_job(client, second)['status'] == 'succeeded'
    assert calls == ['pull_up', 'restart']
    refresh_started = threading.Event();finish_refresh = threading.Event();refreshes = []
    def refresh(id_, updates=False):
        refreshes.append((id_, updates));refresh_started.set();assert finish_refresh.wait(3)
        raise RuntimeError('Synthetic monitoring failure after task completion')
    monkeypatch.setattr(module, 'refresh_server', refresh)
    try:
        module.poll_due();assert refresh_started.wait(1)
        # A deliberately blocked follow-up cannot keep completed jobs running.
        assert client.get('/api/jobs/'+first).json()['status'] == 'succeeded'
        assert client.get('/api/jobs/'+second).json()['status'] == 'succeeded'
        assert refreshes == [('atlas', False)] and 'atlas' not in module.detail_refreshes
    finally:finish_refresh.set()
    deadline=time.monotonic()+1
    while module.server_lock('atlas').locked() and time.monotonic()<deadline:time.sleep(.01)
    module.poll_due()
    assert refreshes == [('atlas', False)]  # Paused hosts remain paused afterwards.


def test_local_refresh_reconciles_recreated_image_without_registry_requests(client, monkeypatch):
    monkeypatch.setattr(store, 'DEMO', False)
    before = module.get_server('atlas');previous = json.loads(before['snapshot'])
    snapshot = copy.deepcopy(previous)
    changed = snapshot['services'][0]
    changed.update(id='recreated-web', image_id=changed['update']['digest'], version='1.143.0')
    for service in snapshot['services']:service['update']={'status':'unchecked'}
    calls = []
    def request(server, payload, on_event=None):
        calls.append(payload)
        assert payload['operation'] == 'snapshot' and payload['updates'] is False
        return snapshot
    monkeypatch.setattr(module.ssh, 'request', request)
    module.detail_refreshes.add('atlas')
    store.execute('UPDATE servers SET last_attempt=?', (time.time(),))
    module.poll_due()
    deadline=time.monotonic()+2
    while module.server_lock('atlas').locked() and time.monotonic()<deadline:time.sleep(.01)
    current=module.get_server('atlas');services=json.loads(current['snapshot'])['services']
    assert len(calls)==1 and current['update_checked']==before['update_checked']
    assert services[0]['version']=='1.143.0' and services[0]['update']['status']=='current'
    assert services[1]['update']==module.remote_probe.classify_update(services[1], previous['services'][1]['update'])  # Unrelated update retained.


def test_reconciliation_does_not_guess_for_changed_tags_platforms_or_unknown_digests():
    old=demo.service('web','example/web:latest','stack',True,version='1.0')
    for change in [{'image':'example/web:another-tag'}, {'platform':{'os':'linux','architecture':'riscv64'}}, {'image_id':'sha256:'+'c'*64}]:
        new={**copy.deepcopy(old),'id':'new-container','image_id':old['update']['digest'],'version':'2.0','update':{'status':'unchecked'},**change}
        module.preserve_update_checks([new],[old])
        assert new['update']['status']=='unchecked'
    same_image={**copy.deepcopy(old),'id':'new-container','update':{'status':'unchecked'}}
    module.preserve_update_checks([same_image],[old])
    assert same_image['update']['status']=='available'  # Recreating without upgrading isn't an update.


def test_scheduled_registry_checks_still_run_when_due(client,monkeypatch):
    store.execute('UPDATE servers SET last_attempt=?',(time.time(),))
    store.execute("UPDATE servers SET last_attempt=0,update_checked=0 WHERE id='atlas'")
    calls=[];done=threading.Event()
    def refresh(id_, updates=False):calls.append((id_,updates));done.set()
    monkeypatch.setattr(module,'refresh_server',refresh)
    module.poll_due();assert done.wait(1)
    assert calls==[('atlas',True)]
