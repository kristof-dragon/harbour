import copy
import io
import json
from unittest.mock import patch

import pytest

from test_harbour import client
from test_telegram import settings
from harbour import app as module, cpu, history, notifications, remote_probe, ssh, store


def counters(uptime=1000, values=None, boot='boot-a', cores=4):
    return {'uptime': uptime, 'boot_id': boot, 'cores': cores,
            'values': values or [100, 10, 20, 600, 100, 5, 5, 20]}


def advance(sample, elapsed=60, busy=600, idle=1800):
    result = copy.deepcopy(sample)
    result['uptime'] += elapsed
    result['values'][0] += busy
    result['values'][3] += idle
    return result


@pytest.mark.parametrize('elapsed', [15, 60, 180, 3600])
def test_cpu_is_counter_delta_ratio_for_actual_poll_interval(elapsed):
    previous = counters()
    assert cpu.usage(previous, advance(previous, elapsed)) == (25, elapsed)
    assert cpu.usage(previous, advance(previous, elapsed, busy=0)) == (0, elapsed)
    assert cpu.usage(previous, advance(previous, elapsed, idle=0)) == (100, elapsed)
    # Preserve existing semantics: idle + iowait are excluded; IRQ and steal
    # are included. Core count must not divide an already aggregate ratio.
    new = counters(uptime=1000 + elapsed, values=[220, 40, 80, 1400, 200, 15, 15, 90])
    assert cpu.usage(previous, new) == (25, elapsed)


@pytest.mark.parametrize('change', ['first', 'reboot', 'cores', 'same_time', 'backward_time', 'same_counters', 'reset', 'iowait_decrease', 'malformed'])
def test_cpu_rebaselines_without_fabricated_zero_or_spike(change):
    previous = counters()
    new = advance(previous)
    if change == 'first': previous = None
    if change == 'reboot': new['boot_id'] = 'boot-b'
    if change == 'cores': new['cores'] = 8
    if change == 'same_time': new['uptime'] = 1000
    if change == 'backward_time': new['uptime'] = 900
    if change == 'same_counters': new['values'] = previous['values']
    if change == 'reset': new['values'][0] = 1
    if change == 'iowait_decrease': new['values'][4] -= 1
    if change == 'malformed': new['values'][0] = float('nan')
    assert cpu.usage(previous, new) == (None, None)


def test_probe_reads_counters_once_without_sleep_or_guest_double_counting(monkeypatch):
    monkeypatch.setattr(remote_probe.sys, 'platform', 'linux')
    files = {'/proc/stat': 'cpu 100 10 20 600 100 5 5 20 1000 2000\nbtime 12345\n',
             '/proc/uptime': '1000.25 4000\n', '/proc/sys/kernel/random/boot_id': 'boot-a\n',
             '/proc/meminfo': 'MemTotal: 1000 kB\nMemAvailable: 500 kB\n', '/proc/mounts': '',
             '/etc/os-release': 'PRETTY_NAME="Test Linux"\n'}
    reads = []
    def open_(path, *args, **kwargs):
        reads.append(path)
        if path not in files: raise FileNotFoundError(path)
        return io.StringIO(files[path])
    monkeypatch.setattr(remote_probe.time, 'sleep', lambda _: pytest.fail('CPU probe slept'))
    monkeypatch.setattr(remote_probe, 'run', lambda *a, **kw: pytest.fail('Resource probe spawned a command'))
    monkeypatch.setattr(remote_probe, 'temperatures', lambda: {})
    monkeypatch.setattr(remote_probe, 'timezone_info', lambda: {})
    monkeypatch.setattr(remote_probe.os, 'cpu_count', lambda: 4)
    with patch('builtins.open', open_):
        result = remote_probe.handle({'operation': 'resources'})['metrics']
        assert result['cpu'] is None and result['cpu_counters'] == counters(uptime=1000.25)
        assert reads.count('/proc/stat') == 1
        files.pop('/proc/sys/kernel/random/boot_id')
        assert remote_probe.metrics(docker=False)['cpu_counters']['boot_id'] == 'btime:12345'


@pytest.fixture
def poll(client, monkeypatch):
    template = json.loads(module.get_server('atlas')['snapshot'])['metrics']
    store.execute('DELETE FROM resource_history')
    monkeypatch.setattr(store, 'DEMO', False)
    def run(sample, id_='atlas', failure=False):
        def request(server, payload):
            assert payload == {'operation': 'resources'}
            if failure: raise ssh.ProbeError('synthetic failure', 'down')
            return {'metrics': {**copy.deepcopy(template), 'cpu': None, 'cpu_counters': sample}}
        monkeypatch.setattr(ssh, 'request', request)
        lock = module.resource_lock(id_)
        lock.acquire()
        module.poll_resources(module.get_server(id_), lock, raise_errors=True)
        return json.loads(module.get_server(id_)['snapshot'])
    return run


def test_poll_baseline_persists_through_failure_and_restart_and_is_private(client, poll):
    first = counters()
    snapshot = poll(first)
    assert snapshot['metrics']['cpu'] is None
    assert snapshot['metrics']['load1'] is not None
    point = client.get('/api/servers/atlas/history?hours=1').json()['points'][0]
    assert point['cpu'] is None and point['cpu_peak'] is None and point['memory'] > 0
    with pytest.raises(ssh.ProbeError): poll(None, failure=True)
    assert json.loads(module.get_server('atlas')['snapshot'])['_cpu_baseline'] == snapshot['_cpu_baseline']
    with patch.object(store, 'DEMO', True):
        store.initialize()  # Reopen the fixture database in its original mode.
    snapshot = poll(advance(first, elapsed=180))
    assert snapshot['metrics']['cpu'] == 25 and snapshot['metrics']['cpu_sample_seconds'] == 180
    server = next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] == 'atlas')
    assert server['error'] is None and server['connection_status'] == 'up'
    assert '_cpu_baseline' not in server and 'cpu_counters' not in server['metrics']
    point = client.get('/api/servers/atlas/history?hours=1').json()['points'][0]
    assert point['cpu'] == point['cpu_peak'] == 25 and point['samples'] == 2 and point['attempts'] == 3


def test_baselines_are_per_host_and_reset_for_reboot_and_changed_connection(client, poll):
    first = counters()
    poll(first)
    assert poll(advance(first), 'luna')['metrics']['cpu'] is None
    restarted = advance(first, elapsed=180, busy=99999)
    restarted['boot_id'] = 'boot-b'  # Even if new counters already exceed the old ones.
    assert poll(restarted)['metrics']['cpu'] is None
    assert poll(advance(restarted))['metrics']['cpu'] == 25
    store.execute("UPDATE servers SET host='new.invalid' WHERE id='atlas'")
    changed = advance(restarted, elapsed=120)
    assert poll(changed)['metrics']['cpu'] is None
    assert poll(advance(changed))['metrics']['cpu'] == 25


def test_cpu_rollups_mix_legacy_readings_with_pending_samples_without_dilution(client):
    metrics = json.loads(module.get_server('atlas')['snapshot'])['metrics']
    store.execute('DELETE FROM resource_history')
    now = 1800000000
    stamp = (now - 35 * 86400) // 3600 * 3600
    legacy = history.sample_payload({**metrics, 'cpu': 40}, up=True)
    legacy.pop('cpu_n')
    with store.db() as con: history.put(con, 'atlas', stamp, 60, legacy)
    for offset, value in [(60, None), (70, None), (120, 80)]:
        history.record('atlas', {**metrics, 'cpu': value}, up=True, now=stamp + offset)
    history.compact(now=now)
    point = history.series('atlas', 1000, now=now, requested_resolution=3600)['points'][0]
    assert point['cpu'] == 60 and point['cpu_peak'] == 80 and point['samples'] == 4
    before = copy.deepcopy(point)
    history.compact(now=now)
    assert history.series('atlas', 1000, now=now, requested_resolution=3600)['points'][0] == before


def test_pending_cpu_does_not_rearm_alerts_or_clear_dismissals(client, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, 'send_message', lambda value, message: sent.append(message))
    settings(client, rules=[{'server_id': 'atlas', 'kind': kind, 'enabled': True,
                            'delay_seconds': 0, 'repeat_seconds': 0} for kind in ('cpu', 'memory')])
    snapshot = json.loads(module.get_server('atlas')['snapshot'])
    def observe(value, at):
        snapshot['metrics']['cpu'] = value
        store.execute("UPDATE servers SET snapshot=?,checked=? WHERE id='atlas'", (json.dumps(snapshot), at))
        module.observe_notifications('atlas')
        module.clear_resolved_warning_dismissals('atlas')
    observe(99, 1000)
    notifications.deliver_one(1000)
    assert len(sent) == 1
    client.post('/api/dismiss-all')
    snapshot['metrics']['memory']['percent'] = 99
    observe(None, 1060)
    notifications.deliver_one(1060)
    assert len(sent) == 2 and 'Memory warning' in sent[-1]
    cpu_state = store.one("SELECT * FROM notification_state WHERE kind='cpu'")
    assert cpu_state['checked'] == 0 and json.loads(cpu_state['entities'])['cpu']['sent']
    assert store.one("SELECT 1 FROM dismissals WHERE server_id='atlas' AND service_id='warning:cpu'")
    observe(99, 1120)
    notifications.deliver_one(1120)
    assert len(sent) == 2  # Unknown is not recovery; repeat=0 stays sent.
    observe(0, 1180)
    assert not store.one("SELECT 1 FROM dismissals WHERE server_id='atlas' AND service_id='warning:cpu'")
    observe(99, 1240)
    notifications.deliver_one(1240)
    assert len(sent) == 3
