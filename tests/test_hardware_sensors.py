"""Sensor units, sparse hardware, source identity and historical gaps."""
import json
import plistlib
import struct

import pytest

from test_harbour import client
from harbour import history, remote_probe as probe, store


def files(root, values):
    for name, value in values.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))


def test_hwmon_units_zero_faults_and_stable_physical_identity(tmp_path):
    device = tmp_path / 'devices/pci0000:00/0000:00:01.0/hwmon/hwmon3'
    files(device, {'name': 'amdgpu', 'fan1_input': 0, 'fan2_input': -1,
        'fan3_input': 3000, 'fan3_fault': 1, 'fan4_input': 4000, 'fan4_enable': 0,
        'in0_input': 12000, 'in0_label': '12V rail', 'curr1_input': -1250,
        'power1_average': 42500000, 'power1_input': 45000000, 'energy1_input': 2000000,
        'power2_input': 'nan', 'in1_input': 'inf', 'curr2_input': 'bad'})
    classdir = tmp_path / 'class/hwmon'
    classdir.mkdir(parents=True)
    (classdir / 'hwmon3').symlink_to(device)
    result = probe.linux_hardware(tmp_path)
    readings = result['hardware']
    assert len(readings) == 6
    by = {r['id'].split(':')[-1]: r for r in readings}
    assert by['fan1_input']['value'] == 0
    assert (by['in0_input']['value'], by['in0_input']['unit']) == (12, 'V')
    assert by['curr1_input']['value'] == -1.25
    assert by['power1_average']['value'] == 42.5 and '(average)' in by['power1_average']['label']
    assert by['power1_input']['value'] == 45
    assert by['energy1_input']['unit'] == 'J'
    ids = [r['id'] for r in readings]
    replacement = device.with_name('hwmon9')
    device.rename(replacement)
    (classdir / 'hwmon3').unlink()
    (classdir / 'hwmon9').symlink_to(replacement)
    assert [r['id'] for r in probe.linux_hardware(tmp_path)['hardware']] == ids


def test_battery_units_health_and_optional_values(tmp_path):
    files(tmp_path / 'class/power_supply/BAT0', {'type': 'Battery', 'present': 1,
        'capacity': 50, 'status': 'Discharging', 'health': 'Good', 'cycle_count': 42,
        'voltage_now': 12000000, 'current_now': -2000000, 'power_now': 24000000,
        'temp': 325, 'energy_full': 45000000, 'energy_full_design': 60000000,
        'charge_full': 5000000, 'charge_full_design': 6000000})
    files(tmp_path / 'class/power_supply/BAT1', {'type': 'Battery', 'present': 0, 'capacity': 20})
    files(tmp_path / 'class/power_supply/AC', {'type': 'Mains', 'capacity': 100})
    result = probe.linux_hardware(tmp_path)
    by = {s['id'].split(':')[-1]: s for s in result['hardware']}
    assert result['batteries'] == [{'name': 'BAT0', 'status': 'Discharging', 'condition': 'Good'}]
    assert by['capacity_health']['value'] == 75  # matching energy units; no double counting
    assert by['temp']['value'] == 32.5
    assert by['power_now']['value'] == 24 and by['voltage_now']['value'] == 12
    assert by['energy_full']['value'] == 45 and by['energy_full']['unit'] == 'Wh'
    assert by['charge_full']['value'] == 5 and by['charge_full']['unit'] == 'Ah'
    assert by['current_now']['value'] == -2
    assert 'energy_now' not in by


def test_powercap_joules_are_not_watts(tmp_path):
    files(tmp_path / 'class/powercap/intel-rapl:0', {'energy_uj': 123000000, 'name': 'package-0'})
    result = probe.linux_hardware(tmp_path)['hardware']
    assert len(result) == 1 and result[0]['value'] == 123 and result[0]['unit'] == 'J'
    assert 'wrapping counter' in result[0]['source']


def test_absent_and_unreadable_sysfs_are_optional(tmp_path, monkeypatch):
    assert probe.linux_hardware(tmp_path) == {'hardware': [], 'batteries': []}
    files(tmp_path / 'class/hwmon/hwmon0', {'name': 'test', 'fan1_input': 2000})
    original = probe.Path.read_text
    def read(path, *a, **k):
        if path.name == 'fan1_input':
            raise PermissionError('optional sensor denied')
        return original(path, *a, **k)
    monkeypatch.setattr(probe.Path, 'read_text', read)
    assert probe.linux_hardware(tmp_path)['hardware'] == []


@pytest.mark.parametrize('type_,raw,expected', [
    ('flt ', struct.pack('<f', 42.5), 42.5), ('sp78', bytes.fromhex('ff80'), -.5),
    ('fpe2', (8000).to_bytes(2, 'big'), 2000), ('ui8 ', b'\x02', 2),
    ('si16', bytes.fromhex('fffe'), -2), ('ui32', b'\0', None),
    ('ioft', bytes(8), None), ('flt ', struct.pack('<f', float('nan')), None)])
def test_smc_formats(type_, raw, expected):
    assert probe.smc_decode(type_, raw) == expected


def test_smc_maps_known_channels_and_releases_connection(monkeypatch):
    closed = []
    class Fake:
        conn = 7
        def read(self, key):
            return {'TCMb': 55, 'TCMz': 64, 'FNum': 2, 'F0Ac': 0, 'F1Ac': 2200,
                    'Tg05': 49, 'Tg0D': 50, 'Tg0L': 51, 'Tg0T': 52, 'TB0T': 32,
                    'PSTR': 18.5, 'PDTR': 15, 'VD0R': 28, 'ID0R': .5}.get(key)
        def close(self, conn): closed.append(conn)
    monkeypatch.setattr(probe, 'MacSMC', Fake)
    temps, readings = probe.macos_smc('Apple M1 Max')
    assert len(temps) == 7
    assert probe.temperature_summary(temps)['package'] == 55
    assert next(s for s in probe.temperature_summary(temps)['sensors'] if s['id'] == 'smc:TCMz')['kind'] == 'cpu_auxiliary'
    assert next(s for s in readings if s['id'] == 'smc:F0Ac')['value'] == 0
    assert len(readings) == 6 and closed == [7]
    temps, _ = probe.macos_smc('Apple M9')
    assert not any(s['id'].startswith('smc:Tg') for s in temps)
    assert closed == [7, 7]


def test_macos_battery_signed_current_sparse_fields_and_no_identifier_leaks(monkeypatch):
    battery = {'BatteryInstalled': True, 'CurrentCapacity': 50, 'MaxCapacity': 100,
               'CycleCount': 309, 'Voltage': 12000, 'Amperage': 2**64 - 2000,
               'DesignCapacity': 6000, 'AppleRawMaxCapacity': 4800,
               'Serial': 'MUST-NOT-LEAK'}
    monkeypatch.setattr(probe, 'run', lambda *a, **k: plistlib.dumps([battery]).decode())
    readings, batteries = probe.macos_battery()
    by = {r['id'].split(':')[-1]: r['value'] for r in readings}
    assert by == {'capacity': 50, 'cycles': 309, 'voltage': 12, 'current': -2, 'power': -24, 'health': 80}
    assert batteries[0]['status'] == 'Discharging'
    assert 'MUST-NOT-LEAK' not in json.dumps([readings, batteries])
    del battery['AppleRawMaxCapacity']
    assert not any(s['id'].endswith(':health') for s in probe.macos_battery()[0])


def test_macos_optional_sensor_failure_isolated(monkeypatch):
    def denied(*a, **k): raise PermissionError('denied')
    monkeypatch.setattr(probe, 'run', denied)
    monkeypatch.setattr(probe, 'MacSMC', denied)
    assert probe.macos_hardware() == {'temperature': probe.temperature_summary([]), 'hardware': [], 'batteries': []}


def metric(readings):
    return {'cpu': None, 'memory': {'percent': 50, 'used': 100, 'total': 200}, 'disks': [], 'hardware': readings}


def test_history_independent_counts_missing_values_and_unit_changes():
    def reading(value, unit='A', source='hwmon'):
        return probe.hardware_reading('sensor:1', 'Rail', value, unit, source)
    samples = [history.sample_payload(metric([reading(-2)])), history.sample_payload(metric([])),
               history.sample_payload(metric([reading(0)])), history.sample_payload(metric([reading(3, 'V')])),
               history.sample_payload(metric([reading(6, source='different')]))]
    merged = history.empty()
    for sample in samples:
        merged = history.merge(merged, sample)
    assert merged['n'] == 5 and len(merged['hardware']) == 3
    values = list(merged['hardware'].values())
    assert (values[0]['n'], values[0]['sum'], values[0]['value_max']) == (2, -2, 0)
    assert [s['unit'] for s in values] == ['A', 'V', 'A']
    assert history.sample_payload(metric([dict(reading(0), value=float('nan'))]))['hardware'] == {}
    legacy = history.empty()
    del legacy['hardware']
    assert history.merge(legacy, merged)['hardware'] == merged['hardware']


def test_sensor_history_retention_keeps_own_timestamps_and_peaks(client):
    store.execute('DELETE FROM resource_history')
    now = 2000000000
    start = now - 9 * 86400
    for offset, value in [(0, 1000), (60, None), (120, 2000)]:
        readings = [] if value is None else [probe.hardware_reading('fan:1', 'Case fan', value, 'RPM', 'hwmon')]
        history.record('atlas', metric(readings), up=True, now=start+offset)
    before = history.series('atlas', 24*10, now=now, requested_resolution=86400)
    history.compact(now=now)
    after = history.series('atlas', 24*10, now=now, requested_resolution=86400)
    assert before['points'] == after['points']
    readings = after['points'][0]['hardware']
    assert len(readings) == 1
    assert readings[0]['value'] == 1500 and readings[0]['peak'] == 2000 and readings[0]['samples'] == 2
    assert readings[0]['sample_first'] == start and readings[0]['sample_last'] == start+120
    history.record('atlas', metric([]), up=True, now=now)
    assert history.series('atlas', 1, now=now)['points'][0]['hardware'] == []


@pytest.mark.parametrize('raw', ['not a plist', '<plist><array>', '<plist><integer>invalid</integer></plist>'])
def test_malformed_optional_battery_plist(monkeypatch, raw):
    monkeypatch.setattr(probe, 'run', lambda *a, **k: raw)
    assert probe.macos_battery() == ([], [])


def test_two_gpus_have_distinct_visible_channel_labels(tmp_path):
    classdir = tmp_path / 'class/hwmon'
    classdir.mkdir(parents=True)
    for index, address in enumerate(('0000:01:00.0', '0000:02:00.0')):
        device = tmp_path / ('devices/pci0000:00/' + address + '/hwmon/hwmon' + str(index))
        files(device, {'name': 'amdgpu', 'fan1_input': 1200})
        (classdir / ('hwmon' + str(index))).symlink_to(device)
    readings = probe.linux_hardware(tmp_path)['hardware']
    assert [r['label'] for r in readings] == ['amdgpu · fan1 [0000:01:00.0]', 'amdgpu · fan1 [0000:02:00.0]']
    assert len(set(r['id'] for r in readings)) == 2
