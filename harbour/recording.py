"""Recorder delivery checkpoints and durable observations of host boots."""
import hashlib
import io
import json
import math
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from . import history, logins, resources, store
from .auth import admin, authenticated

router = APIRouter()


def initialize(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS resource_delivery (
            server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
            connection TEXT NOT NULL, recorder_id TEXT NOT NULL, seq INTEGER NOT NULL,
            acknowledgement TEXT);
        CREATE TABLE IF NOT EXISTS host_boots (
            server_id TEXT REFERENCES servers(id) ON DELETE CASCADE,
            host_identity TEXT NOT NULL, boot_id TEXT NOT NULL,
            boot_at REAL NOT NULL, first_sample REAL NOT NULL, last_sample REAL NOT NULL,
            detected_at REAL NOT NULL, initial INTEGER NOT NULL,
            PRIMARY KEY(server_id,host_identity,boot_id));
    ''')


def options(server):
    config = resources.preferences(server)
    known = resources.catalog(server)
    excluded = [row for key, row in known.items() if config.get(key, {}).get('monitor') is False]
    return {'sample_seconds': server.get('record_seconds', 60), 'disk_seconds': server.get('disk_seconds', 300),
            'memory': config.get('memory', {}).get('monitor', True),
            'excluded_temperatures': [r['sensor_id'] for r in excluded if r['group'] == 'temperature'],
            'excluded_hardware': [r['sensor_id'] for r in excluded if r['group'] == 'hardware'],
            'excluded_disks': [key for key, value in json.loads(server.get('volume_settings') or '{}').items() if value.get('monitor') is False]}


def payload(server, request):
    row = store.one('SELECT * FROM resource_delivery WHERE server_id=?', (server['id'],))
    result = {**request, 'collection': options(server)}
    if row and row['connection'] == logins.connection_signature(server) and row['acknowledgement']:
        result['recorder_ack'] = json.loads(row['acknowledgement'])
    return result


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def accept(con, server, result):
    """Validate whole batch before advancing its transactional high-water mark."""
    status = result.get('recording', {})
    batch = result.get('samples', [])
    if not batch:
        return []
    identity = status.get('recorder_id')
    if not isinstance(identity, str) or len(identity) != 32 or not isinstance(batch, list) or len(batch) > 200:
        raise ValueError('Invalid recorder batch identity or size')
    sequences = []
    for item in batch:
        seq, metrics = item.get('seq'), item.get('metrics')
        if type(seq) is not int or seq < 1 or not isinstance(metrics, dict):
            raise ValueError('Invalid resource sample')
        if not finite(metrics.get('measured_at')) or not finite(metrics.get('uptime')) or metrics['uptime'] < 0:
            raise ValueError('Invalid resource sample time')
        if not isinstance(metrics.get('disks'), list) or len(json.dumps(metrics, allow_nan=False)) > 500000:
            raise ValueError('Invalid resource sample payload')
        sequences.append(seq)
    ack = status.get('acknowledgement') or {}
    if (sequences != sorted(set(sequences)) or ack.get('through') != sequences[-1]
            or ack.get('recorder_id') != identity or not isinstance(ack.get('token'), str) or len(ack['token']) != 64):
        raise ValueError('Invalid resource acknowledgement')
    signature = logins.connection_signature(server)
    row = con.execute('SELECT * FROM resource_delivery WHERE server_id=?', (server['id'],)).fetchone()
    previous = row['seq'] if row and row['recorder_id'] == identity else 0
    accepted = [dict(item['metrics']) for item in batch if item['seq'] > previous]
    if sequences[-1] >= previous:
        con.execute('INSERT OR REPLACE INTO resource_delivery VALUES (?,?,?,?,?)',
                    (server['id'], signature, identity, sequences[-1], json.dumps(ack)))
    return accepted


def observe_boot(con, server, metrics, received_at=None):
    identity = hashlib.sha256(json.dumps([server[k] for k in ('host', 'port', 'fingerprint')]).encode()).hexdigest()
    boot = metrics.get('boot_id') or (metrics.get('cpu_counters') or {}).get('boot_id')
    sampled = metrics.get('measured_at', received_at or time.time())
    uptime = metrics.get('uptime')
    if not isinstance(boot, str) or not 1 <= len(boot) <= 200 or not finite(sampled) or not finite(uptime) or uptime < 0:
        return
    prior = con.execute('SELECT 1 FROM host_boots WHERE server_id=? AND host_identity=?', (server['id'], identity)).fetchone()
    con.execute('''INSERT INTO host_boots VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(server_id,host_identity,boot_id) DO UPDATE SET
        first_sample=MIN(first_sample,excluded.first_sample),last_sample=MAX(last_sample,excluded.last_sample)''',
        (server['id'], identity, boot, sampled - uptime, sampled, sampled, received_at or time.time(), int(not prior)))


@router.get('/api/resource-recorder/download')
def download(user=Depends(admin)):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in ('resource_agent.py', 'resource_install.py', 'remote_probe.py', 'cpu.py', 'RESOURCE_RECORDER.md'):
            bundle.writestr(name, Path(__file__).with_name(name).read_bytes())
    return Response(archive.getvalue(), media_type='application/zip', headers={
        'Content-Disposition': 'attachment; filename="harbour-resource-recorder.zip"', 'Cache-Control': 'no-store'})


@router.get('/api/servers/{id_}/boots')
def boots(id_: str, user=Depends(authenticated)):
    server = store.one('SELECT * FROM servers WHERE id=?', (id_,))
    if not server:
        raise HTTPException(404, 'Server not found')
    return {'boots': store.rows('SELECT boot_id,boot_at,first_sample,last_sample,detected_at,initial FROM host_boots WHERE server_id=? ORDER BY first_sample DESC LIMIT 100', (id_,)),
            'retention_days': history.policy()['retention_days']}


def prune():
    store.execute('DELETE FROM host_boots WHERE last_sample<?', (time.time() - history.policy()['retention_days'] * 86400,))
