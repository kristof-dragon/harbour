import json
import time

from test_harbour import client
from harbour import history, remote_probe, store


def sensor(id_,label,value,cpu=True):
    return {'id':id_,'label':label,'celsius':value,'cpu':cpu}


def test_package_selection_ignores_hot_cores_and_other_devices():
    readings=[sensor('core','coretemp · Core 0',99),sensor('nvme','nvme · Composite',105,False),sensor('package1','coretemp · Package id 1',80),sensor('package0','coretemp · Package id 0',55)]
    t=remote_probe.temperature_summary(readings)
    assert t['package']==55 and t['package_sensor_id']=='package0' and t['package_count']==2
    assert remote_probe.temperature_summary(readings[:2])['package'] is None
    assert remote_probe.temperature_summary([sensor('fake','nvme · Package id 0',98,False)])['package'] is None


def test_amd_die_and_peci_package_readings_only():
    readings=[sensor('ctl','k10temp · Tctl',80),sensor('ccd','k10temp · Tccd1',91),sensor('die','k10temp · Tdie',52)]
    assert remote_probe.temperature_summary(readings)['package']==52
    assert remote_probe.temperature_summary(readings[:2])['package'] is None
    assert remote_probe.temperature_summary([sensor('old','coretemp · Physical id 0',48)])['package']==48
    assert remote_probe.temperature_summary([sensor('target','peci_cputemp.cpu0 · Tjmax',100),sensor('die','peci_cputemp.cpu0 · Die',50)])['package']==50


def test_package_thermal_zone_alongside_nvme(tmp_path):
    hw=tmp_path/'class/hwmon/hwmon0';hw.mkdir(parents=True)
    (hw/'name').write_text('nvme');(hw/'temp1_input').write_text('94000')
    zone=tmp_path/'class/thermal/thermal_zone0';zone.mkdir(parents=True)
    (zone/'type').write_text('x86_pkg_temp');(zone/'temp').write_text('57000')
    assert remote_probe.temperatures(str(tmp_path))['package']==57


def test_warning_scope_and_legacy_history_use_package_aggregates(client):
    row=store.one('SELECT snapshot FROM servers WHERE id="atlas"')
    snapshot=json.loads(row['snapshot'])
    readings=[sensor('package0','coretemp · Package id 0',54),sensor('core0','coretemp · Core 0',99),sensor('nvme','nvme · Composite',91,False)]
    # Legacy snapshots had a max across every sensor. It must never be used now.
    snapshot['metrics']['temperature']={'sensors':readings,'max':99,'cpu_max':99}
    store.execute('UPDATE servers SET snapshot=? WHERE id="atlas"',(json.dumps(snapshot),))
    server=client.get('/api/dashboard').json()['servers'][0]
    assert server['metrics']['temperature']['package']==54
    assert not any(w['id']=='temperature:core0' for w in server['warnings'])
    assert any(w['id']=='temperature:nvme' and w['kind']=='other' for w in server['warnings'])
    store.execute('DELETE FROM resource_history')
    payload=history.sample_payload(snapshot['metrics'],up=True)
    payload.update(temperature_sum=99,temperature_max=99)  # Old scalar is ambiguous.
    now=time.time()
    with store.db() as con:history.put(con,'atlas',int(now//60*60),60,payload)
    point=history.series('atlas',1,now=now)['points'][0]
    assert point['temperature']==54 and point['temperature_peak']==54
    assert history.series('atlas',1,now=now)['temperature_source']=='coretemp · Package id 0'
    # Once only a non-package sensor was available, don't substitute its value.
    payload['sensors'].pop('package0')
    store.execute('UPDATE resource_history SET payload=?',(json.dumps(payload),))
    assert history.series('atlas',1,now=now)['points'][0]['temperature'] is None


def test_package_threshold_warns_and_history_survives_rollup(client):
    snapshot=json.loads(store.one('SELECT snapshot FROM servers WHERE id="atlas"')['snapshot'])
    readings=[sensor('pkg','coretemp · Package id 0',84),sensor('core','coretemp · Core 0',96)]
    snapshot['metrics']['temperature']=remote_probe.temperature_summary(readings)
    store.execute('UPDATE servers SET snapshot=? WHERE id="atlas"',(json.dumps(snapshot),))
    server=client.get('/api/dashboard').json()['servers'][0]
    temp_warnings=[w for w in server['warnings'] if w['id'].startswith('temperature:')]
    assert len(temp_warnings)==1 and temp_warnings[0]['kind']=='cpu_package'
    store.execute('DELETE FROM resource_history')
    now=time.time();history.record('atlas',snapshot['metrics'],up=True,now=now-9*86400)
    history.compact(now=now)
    assert history.series('atlas',240,now=now)['points'][0]['temperature']==84
