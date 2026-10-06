import ctypes
import json
import os
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from test_harbour import client
from harbour import app as module, cpu, remote_probe, ssh, store


VM_STAT = '''Mach Virtual Memory Statistics: (page size of {page_size} bytes)
Pages free: 100.
Pages active: 400.
Pages inactive: 200.
Pages speculative: 10.
Pages wired down: 200.
Pages occupied by compressor: 100.
Pages stored in compressor: 500.
'''
MOUNTS = '''/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
/dev/disk3s2 on /System/Volumes/Preboot (apfs, local, nobrowse)
/dev/disk8s1 on /Volumes/Media (Archive) (apfs, local, journaled)
//user@nas/share on /Volumes/Team Share (smbfs, nodev, nosuid)
/dev/disk10s1 on /Volumes/USB (exfat, local)
/dev/disk11s1 on /Volumes/Ejected (hfs, local)
devfs on /dev (devfs, nobrowse)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
/dev/disk3s6 on /Volumes/Recovery (apfs, nobrowse)
snapshot on /Volumes/.timemachine/backup (apfs, read-only, nobrowse)
/dev/disk5 on /Library/Developer/CoreSimulator/Volumes/iOS (apfs, sealed, read-only)
'''


@pytest.fixture
def mac(monkeypatch):
    state = {'ticks': [100, 20, 600, 10], 'clock': 2000, 'commands': [], 'docker_down': False}
    monkeypatch.setattr(remote_probe.sys, 'platform', 'darwin')
    monkeypatch.setattr(remote_probe.os, 'cpu_count', lambda: 8)
    monkeypatch.setattr(remote_probe.os, 'getloadavg', lambda: (1.25, 2.5, 3.75))
    monkeypatch.setattr(remote_probe.time, 'time', lambda: state['clock'])
    monkeypatch.setattr(remote_probe, 'macos_cpu_ticks', lambda: state['ticks'])
    monkeypatch.setattr(remote_probe, 'macos_hardware', lambda: {'temperature': remote_probe.temperature_summary([]), 'hardware': [], 'batteries': []})
    monkeypatch.setattr(remote_probe, 'timezone_info', lambda: {'name': 'Europe/London'})
    monkeypatch.setattr(remote_probe, 'temperatures', lambda: pytest.fail('macOS used Linux sensors'))
    def run(argv, **kwargs):
        state['commands'].append(argv)
        commands = {
            ('/usr/sbin/sysctl', '-n', 'kern.boottime'): '{ sec = 1000, usec = 250000 } date ignored',
            ('/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'): 'boot-a\n',
            ('/usr/sbin/sysctl', '-n', 'hw.memsize'): str(1000 * 16384),
            ('/usr/bin/vm_stat',): VM_STAT.format(page_size=16384),
            ('/usr/bin/sw_vers', '-productVersion'): '15.7.1\n',
            ('/sbin/mount',): MOUNTS,
            ('docker', 'ps', '-aq', '--no-trunc'): '',
            ('docker', 'version', '--format', '{{.Server.Version}}'): '28.5.1\n',
        }
        if argv[0] == 'docker' and state['docker_down']:
            raise RuntimeError('Docker is stopped')
        return commands[tuple(argv)]
    monkeypatch.setattr(remote_probe, 'run', run)
    def statvfs(path):
        if path == '/Volumes/Ejected':
            raise FileNotFoundError(path)
        # APFS volume-only usage differs from shared-container occupancy.
        return SimpleNamespace(f_blocks=1000, f_bavail=200, f_bfree=700, f_frsize=4096)
    monkeypatch.setattr(remote_probe.os, 'statvfs', statvfs)
    return state


@pytest.mark.parametrize('server_type', ['plain', 'docker'])
def test_mac_snapshot_for_both_host_types(mac, server_type):
    result = remote_probe.handle({'operation': 'snapshot', 'server_type': server_type})
    m = result['metrics']
    assert m['os'] == 'macOS 15.7.1' and m['uptime'] == 999.75 and m['cores'] == 8
    assert [m[k] for k in ('load1', 'load5', 'load15')] == [1.25, 2.5, 3.75]
    assert m['cpu'] is None and cpu.valid(m['cpu_counters'])
    assert m['cpu_counters']['values'] == [100, 10, 20, 600, 0, 0, 0, 0]
    assert m['memory'] == {'total': 16384000, 'used': 11468800, 'percent': 70}
    assert m['temperature']['package'] is None and m['temperature']['sensors'] == []
    assert result['services'] == []
    assert m['docker'] == ('28.5.1' if server_type == 'docker' else None)
    assert any(c[0] == 'docker' for c in mac['commands']) == (server_type == 'docker')


def test_mac_disks_use_data_volume_shared_apfs_space_and_filter_system_mounts(mac):
    disks = remote_probe.macos_disks()
    assert [d['mount'] for d in disks] == ['/', '/Volumes/Media (Archive)', '/Volumes/Team Share', '/Volumes/USB']
    assert disks[0] == {'mount': '/', 'total': 4096000, 'used': 3276800, 'free': 819200, 'percent': 80}
    assert disks[1]['percent'] == 80
    assert disks[2]['percent'] == disks[3]['percent'] == 60


def test_older_mac_hfs_startup_and_mount_races(monkeypatch):
    monkeypatch.setattr(remote_probe, 'run', lambda *a, **k: '/dev/disk1 on / (hfs, local)\n/dev/disk1 on / (hfs, local)\n')
    monkeypatch.setattr(remote_probe.os, 'statvfs', lambda _: SimpleNamespace(f_blocks=100, f_bfree=50, f_bavail=40, f_frsize=4096))
    disks = remote_probe.macos_disks()
    assert len(disks) == 1 and disks[0]['mount'] == '/' and disks[0]['percent'] == 55.6


@pytest.mark.parametrize('page_size', [4096, 16384])
def test_memory_uses_reported_page_size_without_double_counting_compressor(page_size):
    assert remote_probe.macos_memory(1000 * page_size, VM_STAT.format(page_size=page_size)) == {
        'total': 1000 * page_size, 'used': 700 * page_size, 'percent': 70}


@pytest.mark.parametrize('output', ['', 'page size of 16384 bytes\nPages free: 100.', VM_STAT.format(page_size=0)])
def test_missing_memory_is_an_error_not_a_false_zero(output):
    with pytest.raises(RuntimeError, match='memory statistics'):
        remote_probe.macos_memory(100000, output)


def test_mach_binding_uses_unsigned_counters_and_releases_port(monkeypatch):
    class Function:
        def __init__(self, call): self.call = call
        def __call__(self, *args): return self.call(*args)
    released = []
    def read(host, flavor, ticks, count):
        assert (host, flavor) == (42, 3)
        assert count._obj.value == 4
        ticks[:] = [2**31 + 100, 20, 600, 10]
        return 0
    lib = SimpleNamespace(mach_host_self=Function(lambda: 42), host_statistics=Function(read),
                          mach_port_deallocate=Function(lambda *args: released.append(args)))
    monkeypatch.setattr(ctypes, 'CDLL', lambda path: lib)
    monkeypatch.setattr(ctypes.c_uint, 'in_dll', lambda *args: SimpleNamespace(value=7))
    assert remote_probe.macos_cpu_ticks() == [2**31 + 100, 20, 600, 10]
    assert released == [(7, 42)]
    lib.host_statistics = Function(lambda *args: 5)
    with pytest.raises(OSError): remote_probe.macos_cpu_ticks()
    assert released == [(7, 42), (7, 42)]


def test_mac_interval_cpu_reboots_wraps_and_other_os(mac):
    old = remote_probe.metrics(False)['cpu_counters']
    assert cpu.usage(None, old) == (None, None)
    mac.update(ticks=[200, 40, 960, 10], clock=2060)
    new = remote_probe.metrics(False)['cpu_counters']
    assert cpu.usage(old, new) == (25, 60)
    assert cpu.usage(old, {**new, 'boot_id': 'darwin:boot-b'}) == (None, None)
    assert cpu.usage(old, {**new, 'values': [1, 0, 0, 1, 0, 0, 0, 0]}) == (None, None)
    assert cpu.usage({**old, 'source': 'linux'}, new) == (None, None)


def test_optional_mac_metrics_do_not_block_resources(mac, monkeypatch):
    original = remote_probe.run
    def run(argv, **kwargs):
        if argv[-1] in ('kern.bootsessionuuid', '-productVersion'):
            raise OSError('Unavailable')
        return original(argv, **kwargs)
    def unavailable(): raise OSError('Unavailable')
    monkeypatch.setattr(remote_probe, 'run', run)
    monkeypatch.setattr(remote_probe, 'macos_cpu_ticks', unavailable)
    monkeypatch.setattr(remote_probe.os, 'getloadavg', unavailable)
    m = remote_probe.handle({'operation': 'resources'})['metrics']
    assert m['cpu_counters'] is None and m['load1'] is None and m['os'] == 'macOS'
    assert m['memory']['percent'] == 70 and m['disks']
    monkeypatch.setattr(remote_probe, 'macos_cpu_ticks', lambda: mac['ticks'])
    assert remote_probe.metrics(False)['cpu_counters']['boot_id'] == 'darwin:btime:1000:250000'


@pytest.mark.parametrize('server_type', ['plain', 'docker'])
def test_mac_poll_dashboard_history_and_docker_failure_are_independent(client, mac, monkeypatch, server_type):
    store.execute('DELETE FROM resource_history')
    store.execute('UPDATE servers SET server_type=? WHERE id=?', (server_type, 'atlas'))
    monkeypatch.setattr(store, 'DEMO', False)
    monkeypatch.setattr(module.ssh, 'request', lambda server, payload: remote_probe.handle(payload))
    def poll():
        lock = module.resource_lock('atlas')
        lock.acquire()
        module.poll_resources(module.get_server('atlas'), lock, raise_errors=True)
    poll()
    mac.update(ticks=[200, 40, 960, 10], clock=2060)
    poll()
    module.refresh_inventory('atlas')
    server = next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] == 'atlas')
    assert server['metrics']['cpu'] == 25 and server['metrics']['os'].startswith('macOS')
    assert server['connection_status'] == 'up' and server['error'] is None
    assert '_cpu_baseline' not in server and 'cpu_counters' not in server['metrics']
    assert not any(w['id'].startswith('temperature:') for w in server['warnings'])
    # Query fixture timestamps explicitly (not the test runner's wall clock).
    point = module.history.series('atlas', 1, now=2061)['points'][-1]
    assert point['cpu'] == 25 and point['temperature'] is None and point['load1'] == 1.25
    if server_type == 'docker':
        mac['docker_down'] = True
        with pytest.raises(RuntimeError, match='Docker is stopped'):
            module.refresh_inventory('atlas')
        mac.update(ticks=[300, 60, 1320, 10], clock=2120)
        poll()
        snapshot = json.loads(module.get_server('atlas')['snapshot'])
        assert snapshot['metrics']['cpu'] == 25 and snapshot['docker_error'] == 'Docker is stopped'
    else:
        assert not any(c[0] == 'docker' for c in mac['commands'])


@pytest.mark.parametrize('system', ['linux', 'darwin', 'freebsd'])
def test_dispatch_and_explicit_unsupported_os(monkeypatch, system):
    monkeypatch.setattr(remote_probe.sys, 'platform', system)
    monkeypatch.setattr(remote_probe, 'linux_metrics', lambda docker: ('linux', docker))
    monkeypatch.setattr(remote_probe, 'macos_metrics', lambda docker: ('darwin', docker))
    if system == 'freebsd':
        with pytest.raises(RuntimeError, match='Unsupported host OS'): remote_probe.metrics(False)
    else:
        assert remote_probe.metrics(False) == (system, False)


@pytest.mark.skipif(os.uname().sysname != 'Darwin', reason='Native macOS launcher check')
def test_remote_python_bootstrap_with_minimal_ssh_path_and_quoted_home(tmp_path):
    home = tmp_path / 'Mac User; literal $(no-shell-expansion)'
    home.mkdir()
    source = 'import json,os,sys; print(json.dumps([list(sys.version_info[:2]), os.environ["PATH"]]))'
    result = subprocess.run(shlex.split(ssh.REMOTE_PYTHON), input=source, text=True,
                            capture_output=True, timeout=15,
                            env={**os.environ, 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home)})
    assert result.returncode == 0, result.stderr
    version, path = json.loads(result.stdout)
    assert version >= [3, 9]
    assert str(home / '.docker/bin') in path.split(':')
    assert '/opt/homebrew/bin' in path.split(':') and '/usr/local/bin' in path.split(':')
    assert '/Applications/Docker.app/Contents/Resources/bin' in path.split(':')


def test_remote_python_bootstrap_preserves_linux_path(tmp_path):
    for name, body in [('uname', 'printf Linux'), ('python3', 'printf "%s" "$PATH"')]:
        executable = tmp_path / name
        executable.write_text('#!/bin/sh\n' + body + '\n')
        executable.chmod(0o700)
    path = str(tmp_path) + ':/usr/bin:/bin'
    result = subprocess.run(shlex.split(ssh.REMOTE_PYTHON), input='', text=True,
                            capture_output=True, timeout=5, env={**os.environ, 'PATH': path})
    assert result.returncode == 0 and result.stdout == path and not result.stderr


@pytest.mark.parametrize('username', ['Mac.User', 'Mac_User-1', 'harbour'])
def test_existing_ssh_account_names_accept_uppercase_and_dots(username):
    body = module.SSHLoginInput(host='mac.example', username=username, fingerprint='SHA256:' + 'a' * 43)
    assert body.username == username


@pytest.mark.parametrize('username', ['user;id', 'user name', '$(id)', '-root', 'user\nroot', 'a' * 65])
def test_ssh_account_names_still_reject_invalid_characters(username):
    with pytest.raises(ValueError):
        module.SSHLoginInput(host='mac.example', username=username, fingerprint='SHA256:' + 'a' * 43)
