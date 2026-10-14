"""Footprint is diagnostic only: resets, unavailable data, and attribution boundaries."""
import json
import math
from pathlib import Path

import pytest

from test_harbour import client
from harbour import footprint, logins, network_runtime, remote_probe, store
from harbour.network_buffer import Buffer


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(footprint, 'run', lambda stop: stop.wait())
    footprint.reset()
    yield
    footprint.reset()


def sample(clock=100, read=1000, write=2000, identity='boot:group'):
    return dict(scope='cgroup', clock=clock, sampled_at=clock, read_bytes=read, write_bytes=write,
                memory_bytes=100, cache_bytes=20, identity=identity, io_available=True)


def test_rates_reset_and_missing_counters_never_become_zero():
    assert footprint.observe('one', sample())['write_per_second'] is None
    row = footprint.observe('one', sample(130, 1300, 2600))
    assert row['read_per_second'] == 10 and row['write_per_second'] == 20
    assert row['observed_write_bytes'] == 600 and row['window_seconds'] == 30
    assert footprint.observe('one', sample(130, 1300, 2600)) == row  # Cached host poll does not advance its clock.
    assert footprint.observe('one', sample(160, 1400, None))['write_per_second'] is None
    assert footprint.observe('one', sample(190, 1500, 3000))['write_per_second'] is None
    assert footprint.observe('one', sample(220, 1800, 3600))['write_per_second'] == 20
    assert footprint.observe('one', sample(250, 0, 0))['write_per_second'] is None
    assert footprint.observe('one', sample(280, 10, 20, 'newboot:group'))['write_per_second'] is None
    assert footprint.observe('one', {})['read_per_second'] is None
    assert 'one' not in footprint.tracks
    row = footprint.observe('bad', {**sample(), 'write_bytes': math.nan, 'memory_bytes': -1, 'cache_bytes': True})
    assert row['memory_bytes'] is row['cache_bytes'] is row['write_bytes'] is None
    json.dumps(row, allow_nan=False)


def test_long_gap_starts_new_observation_window():
    footprint.observe('one', sample())
    row = footprint.observe('one', sample(100+86401, 2000, 4000))
    # A gap longer than a day cannot masquerade as a fresh observed daily rate.
    assert row['write_per_second'] is None


def fake_linux(tmp_path, monkeypatch):
    def mapped(value):
        p = Path(value)
        return tmp_path / str(p).lstrip('/') if p.is_absolute() else p
    monkeypatch.setattr(remote_probe, 'Path', mapped)
    monkeypatch.setattr(remote_probe.sys, 'platform', 'linux')
    def write(name, value):
        p = mapped(name); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value)
    write('/proc/sys/kernel/random/boot_id', 'boot-a')
    return mapped, write


def test_cgroup_counts_backing_device_once_and_retains_missing_io(tmp_path, monkeypatch):
    path, write = fake_linux(tmp_path, monkeypatch)
    root = '/sys/fs/cgroup/harbour.service'
    write(root+'/memory.current', '12345')
    write(root+'/memory.stat', 'anon 1000\nfile 11345\n')
    write(root+'/io.stat', '253:0 rbytes=40 wbytes=80\n8:0 rbytes=44 wbytes=88\n')
    write('/sys/dev/block/8:0/dev', '8:0')
    path('/sys/dev/block/8:0/slaves').mkdir()
    write('/sys/dev/block/253:0/slaves/sda/dev', '8:0')
    row = remote_probe.harbour_cgroup_footprint(root)
    assert row['read_bytes'] == 44 and row['write_bytes'] == 88
    assert row['memory_bytes'] == 12345 and row['cache_bytes'] == 11345
    write(root+'/io.stat', '253:0 rbytes=40 wbytes=80\n')
    assert remote_probe.harbour_cgroup_footprint(root)['write_bytes'] is None
    path(root+'/io.stat').unlink()
    row = remote_probe.harbour_cgroup_footprint(root)
    assert row['write_bytes'] is None and row['memory_bytes'] == 12345
    write(root+'/io.stat', '')
    assert remote_probe.harbour_cgroup_footprint(root)['write_bytes'] == 0


@pytest.mark.parametrize('membership', ['0::/\n', '1:memory:/harbour.service\n'])
def test_host_root_uses_process_not_whole_host_cgroup(tmp_path, monkeypatch, membership):
    _, write = fake_linux(tmp_path, monkeypatch)
    write('/proc/self/cgroup', membership)
    write('/proc/self/stat', '123 (name with spaces) '+' '.join(['S']+['0']*18+['77']))
    write('/proc/self/statm', '20 10')
    write('/proc/self/io', 'read_bytes: 123\nwrite_bytes: 456\n')
    monkeypatch.setattr(remote_probe, 'harbour_cgroup_footprint', lambda p: pytest.fail('Must not measure host root'))
    row = remote_probe.harbour_self_footprint()
    assert row['scope'] == 'process' and row['write_bytes'] == 456
    assert row['identity'].endswith(':77')


def test_acknowledgement_volume_excludes_retries_and_dropped_rows():
    buffer = Buffer(10)
    buffer.append(b'abc'); buffer.append(b'defg')
    assert not buffer.append(b'0123456789')
    seq, _ = buffer.batch()
    buffer.acknowledge(seq); buffer.acknowledge(seq)
    assert buffer.status()['committed_bytes'] == 7 and buffer.status()['committed_samples'] == 2


def test_report_freshness_attribution_overlap_and_no_writes(client, monkeypatch):
    clock = [1000.]
    monkeypatch.setattr(footprint.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(footprint.time, 'time', lambda: clock[0])
    monkeypatch.setattr(footprint, 'harbour_self_footprint', lambda: sample(clock[0], clock[0]*2, clock[0]*4))
    server = store.one('SELECT * FROM servers WHERE id=?', ('atlas',))
    signature = logins.connection_signature(server)
    network = {'runtime_updated': 1030, 'run_id': 'run1', 'signature': signature, 'committed_bytes': 3000}
    # Same cgroup reported twice must not double-count memory or I/O.
    monkeypatch.setattr(network_runtime, 'read_status', lambda _: {'process': sample(clock[0], clock[0]*2, clock[0]*4), 'servers': {'atlas': network}})
    body = {'harbour_footprint': {'resources': sample()}, 'metrics': {'cpu': 20}}
    footprint.ingest('atlas', body, signature)
    footprint.collect()
    clock[0] = 1030
    body['harbour_footprint']['resources'] = sample(130, 1300, 2600)
    footprint.ingest('atlas', body, signature)
    # The sampler must not write diagnostic rows or modify database contents.
    monkeypatch.setattr(store, 'execute', lambda *a, **k: pytest.fail('Diagnostics wrote to database'))
    report = footprint.collect()
    assert report['total']['memory_bytes'] == 100 and report['total']['write_per_second'] == 4
    assert report['components'][1]['overlaps'] and report['total']['complete']
    row = next(s for s in report['servers'] if s['id'] == 'atlas')
    assert row['incoming']['bytes'] > 0 and row['incoming']['per_second'] > 0
    assert row['collectors'][1]['write_per_second'] == 20
    assert row['collectors'][0]['write_per_second'] is None
    assert 'password_encrypted' not in json.dumps(report) and signature not in json.dumps(report)
    clock[0] = 2000
    report = footprint.collect()
    row = next(s for s in report['servers'] if s['id'] == 'atlas')
    assert not row['collectors'][1]['fresh'] and row['collectors'][1]['write_per_second'] is None
    assert row['network'] is None
    footprint.remote['atlas']['signature'] = 'different host'
    footprint.remote['atlas']['received_at'] = 2000
    assert not next(s for s in footprint.collect()['servers'] if s['id'] == 'atlas')['collectors'][1]['fresh']


def test_runtime_snapshot_is_scoped_and_does_not_include_worker_secrets(monkeypatch):
    import tempfile
    import time
    monkeypatch.setattr(remote_probe, 'harbour_self_footprint', lambda: sample())
    with tempfile.TemporaryDirectory(prefix='harbour-footprint-', dir='/tmp') as directory:
        monkeypatch.setattr(network_runtime, 'path', lambda: directory+'/status.sock')
        status = network_runtime.StatusServer()
        try:
            status.snapshot = {'one': dict(run_id='run', runtime_updated=time.time(), signature='hash', committed_bytes=20,
                                          password='must not appear', settings={'token': 'secret'})}
            result = network_runtime.read_status('__footprint__')
            assert result['servers']['one']['committed_bytes'] == 20
            assert 'password' not in json.dumps(result) and 'secret' not in json.dumps(result)
        finally:
            status.close()


def test_endpoint_is_admin_only_and_storage_is_size_not_writes(client):
    footprint.latest = {'measured_at': 0, 'total': {}}
    response = client.get('/api/footprint')
    assert response.status_code == 200 and response.json()['stale']
    sizes = footprint.storage()
    assert sizes['bytes'] >= 0 and sizes['net_change_bytes'] == 0
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-strong', 'role': 'user'})
    client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-strong'})
    assert client.get('/api/footprint').status_code == 403
    client.cookies.clear()
    assert client.get('/api/footprint').status_code == 401
