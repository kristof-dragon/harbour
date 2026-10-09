import copy
import io
import json
import os
import plistlib
import socket
import subprocess
import sys
import threading
import tempfile
import time
import zipfile
from pathlib import Path

import paramiko
import pytest

from test_harbour import client
from harbour import app as module, history, notifications, recording, remote_probe, resource_agent, resource_install, ssh, store


def sample(at=None, uptime=1000, boot='boot-a', busy=100):
    return {'measured_at': time.time() if at is None else at, 'uptime': uptime, 'boot_id': boot,
            'boot_at': (time.time() if at is None else at) - uptime,
            'cpu_counters': {'boot_id': boot, 'uptime': uptime, 'cores': 4, 'values': [busy, 0, 0, int(uptime)*4-busy, 0, 0, 0, 0]},
            'cpu': None, 'cores': 4, 'memory': {'total': 1000, 'used': 200, 'percent': 20},
            'disks': [{'mount': '/', 'percent': 30, 'used': 300, 'free': 700, 'total': 1000}],
            'temperature': remote_probe.temperature_summary([]), 'hardware': [], 'os': 'Test', 'kernel': 'test'}


def test_durable_spool_replay_ack_restart_and_bounds(tmp_path):
    path = tmp_path / 'queue.db'
    spool = resource_agent.Spool(path, max_samples=2)
    spool.add(sample(uptime=1000)); spool.add(sample(uptime=1060, busy=160))
    batch = spool.exchange()
    assert len(batch['samples']) == 2 and batch['latest']['cpu'] == 25
    restarted = resource_agent.Spool(path, max_samples=2)
    assert restarted.exchange() == batch
    assert restarted.exchange({**batch['acknowledgement'], 'token': 'x'*64})['pending'] == 2
    assert restarted.exchange(batch['acknowledgement'])['pending'] == 0
    assert restarted.exchange()['latest']['uptime'] == 1060
    for n in range(3):
        restarted.add(sample(uptime=1120+n*60, busy=220+n*60))
    tail = restarted.exchange()
    assert tail['dropped'] == 1 and [s['seq'] for s in tail['samples']] == [4, 5]
    with restarted.connect() as con:
        expected = con.execute('SELECT SUM(length(CAST(payload AS BLOB))) FROM samples').fetchone()[0]
        assert restarted.get(con, 'bytes') == expected


def poll_batch(monkeypatch, batch):
    monkeypatch.setattr(store, 'DEMO', False)
    result = {'metrics': copy.deepcopy(batch['latest']), 'samples': copy.deepcopy(batch['samples']),
              'recording': {k: v for k, v in batch.items() if k not in ('latest', 'samples')}, 'latency_ms': 2}
    result['recording'].update(mode='local', sample_seconds=60)
    monkeypatch.setattr(ssh, 'request', lambda *args, **kwargs: copy.deepcopy(result))
    lock = module.resource_lock('atlas'); lock.acquire()
    module.poll_resources(module.get_server('atlas'), lock, raise_errors=True)


def test_backfill_uses_source_times_deduplicates_and_records_boots(client, tmp_path, monkeypatch):
    store.execute('DELETE FROM resource_history')
    spool = resource_agent.Spool(tmp_path / 'queue.db')
    now = time.time()
    spool.add(sample(now-180, 1000)); spool.add(sample(now-120, 1060, busy=160))
    spool.add(sample(now-60, 10, boot='boot-b', busy=4))
    batch = spool.exchange()
    poll_batch(monkeypatch, batch); poll_batch(monkeypatch, batch)
    points = history.series('atlas', 1)['points']
    assert sum(p['samples'] for p in points) == 3
    assert max(p['sample_last'] for p in points if p['sample_last'] is not None) == now-60
    assert sum(p['attempts'] for p in points) == 2  # Two transfers, not three historical SSH checks.
    assert all(p['up_percent'] is None for p in points if p['samples'] and not p['attempts'])
    boots = client.get('/api/servers/atlas/boots').json()['boots']
    assert len(boots) == 2 and {b['initial'] for b in boots} == {0, 1}
    # Restarting the recorder retains identity; it cannot create a host reboot.
    spool = resource_agent.Spool(tmp_path / 'queue.db')
    assert spool.exchange()['recorder_id'] == batch['recorder_id']
    ack = json.loads(store.one('SELECT acknowledgement FROM resource_delivery')['acknowledgement'])
    assert spool.exchange(ack)['pending'] == 0
    poll_batch(monkeypatch, spool.exchange())
    assert sum(p['samples'] for p in history.series('atlas', 1)['points']) == 3
    assert len(client.get('/api/servers/atlas/boots').json()['boots']) == 2
    public = client.get('/api/dashboard').text
    assert ack['token'] not in public and 'cpu_counters' not in public


def test_failed_storage_does_not_ack_batch(client, tmp_path, monkeypatch):
    spool = resource_agent.Spool(tmp_path / 'queue.db'); spool.add(sample())
    monkeypatch.setattr(history, 'record', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('storage unavailable')))
    with pytest.raises(RuntimeError, match='storage unavailable'):
        poll_batch(monkeypatch, spool.exchange())
    assert store.one('SELECT * FROM resource_delivery') is None
    assert store.rows('SELECT * FROM host_boots') == []
    assert spool.exchange()['pending'] == 1


def test_malformed_batch_is_not_partially_stored(client, tmp_path, monkeypatch):
    spool = resource_agent.Spool(tmp_path / 'queue.db'); spool.add(sample())
    batch = spool.exchange(); batch['samples'].append({'seq': 2, 'metrics': {'measured_at': 'bad'}})
    with pytest.raises(ValueError):
        poll_batch(monkeypatch, batch)
    assert store.one('SELECT * FROM resource_delivery') is None


def test_credential_change_does_not_duplicate_unacknowledged_samples(client, tmp_path, monkeypatch):
    store.execute('DELETE FROM resource_history')
    spool = resource_agent.Spool(tmp_path / 'queue.db'); spool.add(sample())
    poll_batch(monkeypatch, spool.exchange())
    store.execute("UPDATE servers SET username='new-reader' WHERE id='atlas'")
    poll_batch(monkeypatch, spool.exchange())
    assert sum(p['samples'] for p in history.series('atlas', 1)['points']) == 1


def test_cached_samples_do_not_advance_alert_duration(client, monkeypatch):
    monkeypatch.setattr(notifications, 'delivery_loop', lambda *_: None)
    body = {'enabled': True, 'bot_token': '123456789:'+'a'*35, 'chat_id': '12345',
            'rules': [{'server_id': 'atlas', 'kind': kind, 'enabled': True, 'delay_seconds': 60, 'repeat_seconds': 0} for kind in ('cpu', 'disk')]}
    assert client.put('/api/notifications', json=body).status_code == 200
    snapshot = json.loads(module.get_server('atlas')['snapshot'])
    snapshot['metrics'].update(measured_at=1000, disk_measured_at=900, cpu=99)
    snapshot['metrics']['disks'][0]['percent'] = 99
    snapshot['recording'] = {'mode': 'local'}
    store.execute("UPDATE servers SET snapshot=?,checked=1000,poll_seconds=15 WHERE id='atlas'", (json.dumps(snapshot),))
    module.observe_notifications('atlas')
    store.execute("UPDATE servers SET checked=1060 WHERE id='atlas'")
    module.observe_notifications('atlas')
    assert {r['kind']: r['checked'] for r in store.rows('SELECT * FROM notification_state')} == {'cpu': 1000, 'disk': 900}
    snapshot['metrics']['measured_at'] = 1060
    store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
    module.observe_notifications('atlas')
    assert {r['kind']: r['checked'] for r in store.rows('SELECT * FROM notification_state')} == {'cpu': 1060, 'disk': 900}


def test_interval_settings_persist_and_old_clients_preserve_them(client):
    server = module.get_server('atlas')
    values = {'name': server['name'], 'server_type': 'plain', 'enabled': True, 'poll_seconds': 120,
              'thresholds': None, 'volumes': [], 'record_seconds': 30, 'disk_seconds': 600, 'inventory_seconds': 900}
    assert client.put('/api/servers/atlas/settings', json=values).status_code == 200
    for key in ('record_seconds', 'disk_seconds', 'inventory_seconds'): values.pop(key)
    assert client.put('/api/servers/atlas/settings', json=values).status_code == 200
    saved = module.get_server('atlas')
    assert [saved[k] for k in ('record_seconds', 'disk_seconds', 'inventory_seconds')] == [30, 600, 900]
    assert client.put('/api/servers/atlas/settings', json=values | {'disk_seconds': 0}).status_code == 422


def test_cached_disks_not_reinserted_and_cpu_duration_weighted(client):
    store.execute('DELETE FROM resource_history')
    first = sample(); first.update(cpu=20, cpu_sample_seconds=60)
    second = sample(); second.update(cpu=80, cpu_sample_seconds=180, disks_sampled=False)
    history.record('atlas', first, up=True); history.record('atlas', second, up=True)
    data = history.series('atlas', 1)['points'][-1]
    assert data['cpu'] == 65 and data['cpu_peak'] == 80 and data['uptime'] == 1000
    payload = json.loads(store.rows('SELECT payload FROM resource_history')[-1]['payload'])
    assert payload['disks']['/']['n'] == 1


def test_sampler_disk_cadence_and_setting_change(monkeypatch):
    calls = []
    def collect(docker, collection):
        calls.append(copy.deepcopy(collection))
        return sample()
    monkeypatch.setattr(remote_probe, 'metrics', collect)
    sampler = remote_probe.ResourceSampler()
    first, second = sampler.sample({'disk_seconds': 300}), sampler.sample({'disk_seconds': 300})
    assert [c['disks'] for c in calls] == [True, False]
    assert first['disk_measured_at'] == second['disk_measured_at']
    assert first['disks_sampled'] and not second['disks_sampled']
    sampler.sample({'disk_seconds': 60})
    assert calls[-1]['disks']


def test_recorder_selected_automatically_and_stale_recorder_falls_back(monkeypatch):
    monkeypatch.setattr(remote_probe, 'login_events', lambda *_: {})
    monkeypatch.setattr(remote_probe.resource_sampler, 'sample', lambda *_: sample(uptime=2000))
    batch = {'protocol': 1, 'sample_seconds': 60, 'latest': sample(), 'samples': []}
    monkeypatch.setattr(remote_probe, 'recorder_exchange', lambda *_: copy.deepcopy(batch))
    assert remote_probe.resource_response({})['recording']['mode'] == 'local'
    batch['latest']['measured_at'] -= 1000
    result = remote_probe.resource_response({})
    assert result['recording']['mode'] == 'fallback' and result['metrics']['uptime'] == 2000
    batch.clear(); batch.update(mode='remote', state='not_installed')
    assert remote_probe.resource_response({})['recording']['mode'] == 'remote'


def test_private_socket_exchange_and_persistent_settings(tmp_path):
    spool = resource_agent.Spool(tmp_path / 'queue.db'); spool.add(sample())
    recorder = resource_agent.Recorder(spool)
    runtime = tempfile.TemporaryDirectory(prefix='harbour-rec-', dir='/tmp')
    path = Path(runtime.name) / 'r.sock'
    thread = threading.Thread(target=resource_agent.serve, args=(recorder, path)); thread.start()
    try:
        for _ in range(100):
            if path.exists(): break
            time.sleep(.01)
        assert path.stat().st_mode & 0o777 == 0o600
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(5); connection.connect(str(path))
            connection.sendall(b'{"settings":{"sample_seconds":120,"disk_seconds":600}}\n')
            response = json.loads(connection.makefile('rb').readline())
        assert response['sample_seconds'] == 120 and response['pending'] == 1
        assert resource_agent.Recorder(spool).options['disk_seconds'] == 600
    finally:
        recorder.stop.set(); thread.join(2)
    assert not thread.is_alive() and not path.exists()
    runtime.cleanup()


def test_install_definitions_and_uninstall_retention(tmp_path, monkeypatch):
    linux = resource_install.definition('linux', '/usr/bin/python3', 'harbour').decode()
    assert 'User="harbour"' in linux and 'RestrictAddressFamilies=AF_UNIX' in linux
    mac = plistlib.loads(resource_install.definition('darwin', '/opt/homebrew/bin/python3', 'example-user'))
    assert mac['UserName'] == 'example-user' and mac['RunAtLoad'] and mac['KeepAlive']
    assert '/Library/Application Support/HarbourResources/recorder.sock' in mac['ProgramArguments']
    install, runtime, state = [tmp_path / name for name in ('install', 'runtime', 'state')]
    for path in (install, runtime, state): path.mkdir()
    (install / 'resource_agent.py').write_text('pass'); (install / 'installed.json').write_text('{}')
    (state / 'resources.db').write_text('retained')
    target = tmp_path / 'unit'; target.write_text('service')
    monkeypatch.setattr(resource_install, 'INSTALL', install); monkeypatch.setattr(resource_install, 'RUNTIME', runtime)
    monkeypatch.setattr(resource_install, 'state_path', lambda *_: state)
    monkeypatch.setattr(resource_install, 'service_path', lambda *_: target)
    monkeypatch.setattr(resource_install.os, 'geteuid', lambda: 0)
    commands = []
    monkeypatch.setattr(resource_install.subprocess, 'run', lambda args, **kw: commands.append(args))
    resource_install.uninstall('linux', dry_run=True)
    assert target.exists() and commands == []
    resource_install.uninstall('linux')
    assert not target.exists() and not install.exists() and (state / 'resources.db').exists()
    assert commands[0] == ['systemctl', 'disable', '--now', 'harbour-resources.service']
    resource_install.uninstall('linux', purge=True)
    assert not state.exists()


def test_bundle_settings_permissions_and_host_removal(client):
    response = client.get('/api/resource-recorder/download')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {'resource_agent.py', 'remote_probe.py', 'cpu.py', 'resource_install.py', 'RESOURCE_RECORDER.md'} == set(archive.namelist())
    server = module.get_server('atlas')
    with store.db() as con: recording.observe_boot(con, server, sample())
    client.post('/api/users', json={'name': 'reader', 'password': 'long-reader-password', 'role': 'user'})
    login = client.post('/api/login', json={'name': 'reader', 'password': 'long-reader-password'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    assert client.get('/api/servers/atlas/boots').status_code == 200
    assert client.get('/api/resource-recorder/download').status_code == 403


def test_real_ssh_resource_connection_and_python_reader_reused(client, monkeypatch):
    """Exercise the actual framed stdin/stdout protocol through pinned SSH."""
    monkeypatch.setattr(store, 'DEMO', False)
    source = b'''import sys,json
def serve_resources():
 for line in iter(sys.stdin.buffer.readline,b''):
  request=json.loads(line)
  print(json.dumps({'ready':True}),flush=True)
  print(json.dumps({'metrics':{'request':request},'recording':{'mode':'remote'}}),flush=True)
'''
    monkeypatch.setattr(ssh.Path, 'read_bytes', lambda _: source)
    host_key = paramiko.ECDSAKey.generate()
    connections, commands, errors, threads, processes = [], [], [], [], []
    stopped = threading.Event()
    class Host(paramiko.ServerInterface):
        def check_auth_password(self, username, password): return paramiko.AUTH_SUCCESSFUL
        def check_channel_request(self, kind, channel_id): return paramiko.OPEN_SUCCEEDED
        def check_channel_exec_request(self, channel, command):
            commands.append(command)
            def execute():
                process = subprocess.Popen(['/bin/sh', '-c', command.decode().replace('exec python3', 'exec ' + sys.executable)],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
                def feed():
                    try:
                        while chunk := channel.recv(65536):
                            process.stdin.write(chunk); process.stdin.flush()
                    except (OSError, EOFError): pass
                    finally: process.stdin.close()
                feeder = threading.Thread(target=feed, daemon=True); feeder.start(); threads.append(feeder)
                try:
                    for line in process.stdout:
                        channel.sendall(line)
                    process.wait(timeout=5)
                except (OSError, EOFError, subprocess.TimeoutExpired) as exc: errors.append(exc)
                finally:
                    if process.poll() is None: process.kill()
                    channel.close()
            worker = threading.Thread(target=execute, daemon=True); worker.start(); threads.append(worker)
            return True
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); listener.listen(); listener.settimeout(.1)
        def host():
            while not stopped.is_set():
                try: connection, _ = listener.accept()
                except socket.timeout: continue
                transport = paramiko.Transport(connection); connections.append(transport)
                transport.add_server_key(host_key); transport.start_server(server=Host())
                while transport.is_active() and not stopped.wait(.01): pass
                transport.close()
        worker = threading.Thread(target=host, daemon=True); worker.start()
        server = module.get_server('atlas') | {'host': '127.0.0.1', 'port': listener.getsockname()[1],
            'auth_method': 'password', 'password_encrypted': store.cipher().encrypt(b'fixture').decode(),
            'fingerprint': ssh.host_fingerprint(host_key)}
        try:
            for _ in range(2):
                result = ssh.request(server, {'operation': 'resources'})
                assert result['metrics']['request']['collection']['sample_seconds'] == 60
                assert result['latency_ms'] is not None
            assert len(connections) == len(commands) == 1
            ssh.sync_resources([server | {'monitoring_enabled': False}])
            assert not ssh.resource_sessions
        finally:
            ssh.close_resources(); stopped.set()
            for transport in connections: transport.close()
            worker.join(2)
            for thread in threads: thread.join(5)
            for process in processes:
                if process.poll() is None: process.kill()
        assert errors == []
