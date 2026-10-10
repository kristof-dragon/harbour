"""Synthetic persistence/failure cases; never connects to a real router."""
import contextlib
import gzip
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from harbour import network, network_archives as archives, network_recorder as recorder, store
from test_harbour import client
from test_openwrt import enable


def settings(client, **changes):
    config, revision = network.config('atlas')
    config.update(changes)
    response = client.put('/api/servers/atlas/network', json=config)
    assert response.status_code == 200, response.text
    return network.config('atlas')[0]


def readings(signature, revision, at=None, count=3, **extra):
    at = at or time.time()
    rows = [{'at': at+i*.25, 'kind': 'probe', 'target': '192.0.2.10',
             'source': 'recorder', 'status': 'reply', 'rtt_ms': i+1, **extra} for i in range(count)]
    assert network.persist('atlas', rows, {}, signature, revision)
    return rows


def clock(monkeypatch, when):
    for module in (network, archives):
        monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: when))


def contents(row):
    with gzip.open(archives.path(row, not row['complete']), 'rt') as stream:
        return [json.loads(line) for line in stream]


def test_upgrade_keeps_readings_and_applies_extended_defaults(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    readings(signature, revision, at=time.time()-86400)
    # Simulate the v0.1.26 schema and saved configuration.
    with store.db() as con:
        con.execute('DROP INDEX network_pending')
        con.execute('DROP INDEX network_sizes')
        con.execute('ALTER TABLE network_samples DROP COLUMN archived')
        con.execute('ALTER TABLE network_samples DROP COLUMN payload_bytes')
        con.execute('ALTER TABLE network_events DROP COLUMN archived_version')
        con.execute('UPDATE network_settings SET config=? WHERE server_id=?', (json.dumps({'enabled': True}), 'atlas'))
    network.initialize()
    network.initialize()
    cfg, _ = network.config('atlas')
    assert cfg['retention_hours'] == 168 and cfg['archive_enabled']
    assert len(store.rows('SELECT * FROM network_samples')) == 3
    assert store.one('SELECT min(payload_bytes) AS n FROM network_samples')['n'] > 0
    network.maintain('atlas')
    assert len(store.rows('SELECT * FROM network_samples')) == 3


@pytest.mark.parametrize('change', [
    {'retention_hours': 0}, {'retention_hours': 8761}, {'retention_hours': 1.5},
    {'max_rows': -1}, {'max_payload_mib': -1}, {'archive_retention_days': 0},
    {'archive_max_mib': -1}, {'incident_max_count': -1}, {'incident_max_mib': -1},
])
def test_invalid_limits_rejected(client, monkeypatch, change):
    enable(client, monkeypatch)
    assert client.put('/api/servers/atlas/network', json=change).status_code == 422


def test_retention_changes_do_not_interrupt_probes(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    settings(client, retention_hours=720, max_rows=0, max_payload_mib=0, archive_max_mib=0)
    assert network.config('atlas')[1] == revision
    readings(signature, revision)
    response = client.get('/api/servers/atlas/network').json()
    assert response['retention'] == {'seconds': 720*3600, 'max_rows': 0, 'max_payload_bytes': 0}
    settings(client, interval=1)
    assert network.config('atlas')[1] != revision


def test_raw_age_row_and_byte_limits_preserve_daily_evidence(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    settings(client, retention_hours=1, max_rows=2, max_payload_mib=1)
    readings(signature, revision, at=time.time()-7200, count=3)
    readings(signature, revision, at=time.time()-10, count=4, detail='x'*600000)
    network.maintain('atlas')
    retained = store.rows('SELECT * FROM network_samples')
    assert len(retained) == 1 and json.loads(retained[0]['payload'])['rtt_ms'] == 4
    row = store.one('SELECT * FROM network_archives')
    data = contents(row)
    assert len([r for r in data if r['type']=='sample']) == 7
    assert row['sample_count'] == 7 and row['size'] < 20000
    assert os.stat(archives.path(row, True)).st_mode & 0o777 == 0o600


def test_daily_rollover_download_and_delayed_measurement(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    day = int(time.time()//86400)*86400
    clock(monkeypatch, day+3600)
    old_samples = readings(signature, revision, at=day-86400)
    event = network.mark('atlas', 'Example incident', measured=day-3600)
    store.execute('INSERT INTO network_runs(id,server_id,started,config,observer) VALUES (?,?,?,?,?)', ('example-run', 'atlas', day, '{}', 'example-observer'))
    readings(signature, revision, at=day+1, count=1, run_id='example-run')
    network.maintain('atlas')
    first = store.one('SELECT * FROM network_archives')
    url = '/api/servers/atlas/network/archives/'+first['id']
    assert client.get(url).status_code == 404  # Today's file is still growing.
    clock(monkeypatch, day+86400+60)
    # A late result goes into today's file with its original measured time.
    readings(signature, revision, at=day+86399, count=1)
    store.execute('UPDATE servers SET monitoring_enabled=0 WHERE id="atlas"')
    network.maintain('atlas')
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/gzip'
    data = [json.loads(x) for x in gzip.decompress(response.content).splitlines()]
    assert [r['at'] for r in data if r['type']=='sample'][:3] == [r['at'] for r in old_samples]
    assert any(r['type']=='incident' and r['id']==event and r['complete'] for r in data)
    assert any(r['type']=='context' and any(run['id']=='example-run' for run in r['runs']) for r in data)
    new = store.one('SELECT * FROM network_archives WHERE complete=0')
    assert contents(new)[-1]['at'] == day+86399
    assert client.get('/api/servers/boreal/network/archives/'+first['id']).status_code in (400,404)
    client.post('/api/users', json={'name':'reader','password':'reader-password-strong','role':'user'})
    auth = client.post('/api/login', json={'name':'reader','password':'reader-password-strong'}).json()
    client.headers['X-CSRF-Token'] = auth['csrf']
    assert client.get(url).status_code == 200
    assert client.put('/api/servers/atlas/network', json={'archive_enabled':False}).status_code == 403
    client.cookies.clear()
    assert client.get(url).status_code == 401


def test_failed_append_retries_without_duplicates_and_prevents_pruning(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    settings(client, retention_hours=1, max_rows=1)
    readings(signature, revision, at=time.time()-7200)
    original_db = store.db
    @contextlib.contextmanager
    def fail_checkpoint():
        with original_db() as con:
            class Broken:
                def execute(self, sql, *args):
                    if 'UPDATE network_archives SET size=' in sql:
                        raise OSError('Simulated interrupted checkpoint')
                    return con.execute(sql, *args)
                def __getattr__(self, key):
                    return getattr(con, key)
            yield Broken()
    monkeypatch.setattr(store, 'db', fail_checkpoint)
    network.maintain('atlas')
    assert len(store.rows('SELECT * FROM network_samples WHERE archived=0')) == 3
    assert client.get('/api/servers/atlas/network').json()['archives']['error']
    row = store.one('SELECT * FROM network_archives')
    assert row['size'] == 0 and archives.path(row, True).stat().st_size > 0
    monkeypatch.setattr(store, 'db', original_db)
    network.maintain('atlas')
    row = store.one('SELECT * FROM network_archives')
    assert len([r for r in contents(row) if r['type']=='sample']) == 3
    assert not store.rows('SELECT * FROM network_samples')
    assert not client.get('/api/servers/atlas/network').json()['archives']['error']
    # SQLite may reuse sample row IDs after all raw readings have been removed.
    readings(signature, revision, count=1)
    monkeypatch.setattr(store, 'db', fail_checkpoint)
    network.maintain('atlas')
    monkeypatch.setattr(store, 'db', original_db)
    network.maintain('atlas')
    row = store.one('SELECT * FROM network_archives')
    assert row['sample_count'] == 4
    assert [r['archive_sequence'] for r in contents(row) if r['type']=='sample'] == [1,2,3,4]


def test_bounded_archive_backlog_survives_short_retention_and_rollover(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    settings(client, retention_hours=1, max_rows=1)
    day = int(time.time()//86400)*86400
    clock(monkeypatch, day+3600)
    readings(signature, revision, at=day-7200, count=5)
    monkeypatch.setattr(archives, 'BATCH_SIZE', 2)
    monkeypatch.setattr(archives, 'MAX_BATCHES', 1)
    network.maintain('atlas')
    assert len(store.rows('SELECT * FROM network_samples WHERE archived=0')) == 3
    clock(monkeypatch, day+86400+60)
    network.maintain('atlas')
    network.maintain('atlas')
    assert not store.rows('SELECT * FROM network_samples')
    rows = store.rows('SELECT * FROM network_archives ORDER BY day')
    assert rows[0]['complete'] == 1
    assert sum(row['sample_count'] for row in rows) == 5
    assert len([r for row in rows for r in contents(row) if r['type']=='sample']) == 5


def test_fsync_failure_does_not_acknowledge_readings(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    readings(signature, revision)
    monkeypatch.setattr(archives.os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('Disk full')))
    network.maintain('atlas')
    assert len(store.rows('SELECT * FROM network_samples WHERE archived=0')) == 3
    assert store.one('SELECT size FROM network_archives')['size'] == 0


def test_rollover_recovers_rename_before_checkpoint_and_empty_attempt(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    readings(signature, revision)
    network.maintain('atlas')
    row = store.one('SELECT * FROM network_archives')
    os.replace(archives.path(row, True), archives.path(row))
    clock(monkeypatch, row['day']+86400+60)
    network.maintain('atlas')
    assert store.one('SELECT complete FROM network_archives')['complete'] == 1
    empty = {'id':'0'*32, 'server_id':'atlas', 'day':row['day']-86400, 'size':0}
    store.execute('INSERT INTO network_archives(id,server_id,day,updated) VALUES (?,?,?,?)',
                  (empty['id'],'atlas',empty['day'],empty['day']))
    network.maintain('atlas')
    assert not store.one('SELECT id FROM network_archives WHERE id=?', (empty['id'],))
    assert not store.one('SELECT error FROM network_archive_status')['error']


def test_disabled_archive_allows_pruning_and_completed_file_retention(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    day = int(time.time()//86400)*86400
    for offset in range(3):
        clock(monkeypatch, day+offset*86400+60)
        readings(signature, revision, count=1)
        network.maintain('atlas')
    files = store.rows('SELECT * FROM network_archives ORDER BY day')
    cfg = settings(client, archive_enabled=False, retention_hours=1, archive_retention_days=1)
    clock(monkeypatch, day+3*86400+60)
    network.maintain('atlas')
    assert not archives.path(files[0]).exists() and not archives.path(files[1]).exists()
    assert archives.path(files[2]).exists()
    assert not store.rows('SELECT * FROM network_samples')
    # File size cap applies to completed compressed files.
    store.execute('UPDATE network_archives SET size=?', (2*1024*1024,))
    cfg = settings(client, archive_max_mib=1)
    archives.trim('atlas', cfg, day+3*86400)
    assert not store.rows('SELECT * FROM network_archives')
    assert not archives.path(files[2]).exists()


def test_deleting_server_cleans_only_managed_files(client, monkeypatch):
    _, signature, revision = enable(client, monkeypatch)
    readings(signature, revision)
    network.maintain('atlas')
    row = store.one('SELECT * FROM network_archives')
    unrelated = archives.root()/'keep.txt'
    unrelated.write_text('Not an archive')
    store.execute('DELETE FROM servers WHERE id="atlas"')
    archives.cleanup_removed()
    assert not archives.path(row, True).exists()
    assert unrelated.is_file()


def test_backlog_defers_incident_sealing(client, monkeypatch):
    from harbour import network_runtime
    _, signature, revision = enable(client, monkeypatch)
    at = time.time()-400
    readings(signature, revision, at=at)
    event = network.mark('atlas', 'Example incident', measured=at)
    monkeypatch.setattr(network_runtime, 'read_status', lambda _: {'oldest_unsaved_seconds':30})
    network.maintain('atlas')
    assert not store.one('SELECT complete FROM network_events WHERE id=?', (event,))['complete']
    readings(signature, revision, at=at+1)
    monkeypatch.setattr(network_runtime, 'read_status', lambda _: {'oldest_unsaved_seconds':0})
    network.maintain('atlas')
    assert store.one('SELECT complete FROM network_events WHERE id=?', (event,))['complete']
    assert len(client.get('/api/servers/atlas/network/export/'+event).json()['samples']) == 6
