"""OpenWrt diagnostics configuration, durable measurements and incident export."""
import ipaddress
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import store
from .auth import admin, authenticated

router = APIRouter(prefix='/api/servers/{id_}/network')
DEFAULTS = {'enabled': False, 'interval': 1.0, 'targets': ['1.1.1.1', '8.8.8.8'],
            'router_probes': True, 'wan_device': '', 'client_mac': '', 'latency_limit_ms': 150.0}
MAX_ROWS, MAX_BYTES, RETENTION = 200000, 64 * 1024 * 1024, 6 * 3600


def initialize():
    with store.db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS network_settings (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
          config TEXT NOT NULL, revision TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS network_samples (
          id INTEGER PRIMARY KEY, server_id TEXT REFERENCES servers(id) ON DELETE CASCADE,
          measured REAL NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS network_time ON network_samples(server_id,measured,id);
        CREATE TABLE IF NOT EXISTS network_events (
          id TEXT PRIMARY KEY, server_id TEXT REFERENCES servers(id) ON DELETE CASCADE,
          measured REAL NOT NULL, label TEXT NOT NULL, automatic INTEGER NOT NULL,
          archive TEXT, complete INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS network_status (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
          updated REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS network_runs (
          id TEXT PRIMARY KEY, server_id TEXT REFERENCES servers(id) ON DELETE CASCADE,
          started REAL NOT NULL, config TEXT NOT NULL, observer TEXT NOT NULL);
        ''')


def host(id_):
    row = store.one('SELECT * FROM servers WHERE id=?', (id_,))
    if not row:
        raise HTTPException(404, 'Server not found')
    if row['server_type'] != 'openwrt':
        raise HTTPException(400, 'Network diagnostics require the OpenWRT server type')
    return row


def config(id_):
    row = store.one('SELECT * FROM network_settings WHERE server_id=?', (id_,))
    return ({**DEFAULTS, **json.loads(row['config'])}, row['revision']) if row else (dict(DEFAULTS), '')


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    interval: float = 1
    targets: list[str] = Field(default_factory=lambda: list(DEFAULTS['targets']), min_length=1, max_length=4)
    router_probes: bool = True
    wan_device: str = Field(default='', max_length=32, pattern=r'^[A-Za-z0-9_.:@-]*$')
    client_mac: str = Field(default='', pattern=r'^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$|^$')
    latency_limit_ms: float = Field(default=150, ge=10, le=10000, allow_inf_nan=False)

    @field_validator('interval')
    @classmethod
    def interval_value(cls, value):
        if value not in (.25, .5, 1, 2):
            raise ValueError('Choose 250 ms, 500 ms, 1 second or 2 seconds')
        return value

    @field_validator('targets')
    @classmethod
    def addresses(cls, values):
        result = []
        for value in values:
            ip = ipaddress.ip_address(value)
            if ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip.is_loopback or '%' in value:
                raise ValueError('Use unicast IP addresses without a scope identifier')
            result.append(str(ip))
        if len(set(result)) != len(result):
            raise ValueError('Probe targets must be distinct')
        return result


@router.get('')
def get(id_: str, user=Depends(authenticated)):
    server = host(id_)
    settings, revision = config(id_)
    row = store.one('SELECT * FROM network_status WHERE server_id=?', (id_,))
    status = json.loads(row['payload']) if row else {}
    status.update(updated=row['updated'] if row else None,
                  recorder_online=bool(row and time.time()-row['updated'] < 10))
    return {'settings': settings, 'revision': revision, 'status': status,
            'device': json.loads(server['snapshot']).get('metrics', {}).get('openwrt'),
            'events': store.rows('SELECT id,measured,label,automatic,complete FROM network_events WHERE server_id=? ORDER BY measured DESC LIMIT 50', (id_,)),
            'retention': {'seconds': RETENTION, 'max_rows': MAX_ROWS, 'max_payload_bytes': MAX_BYTES}}


@router.put('')
def save(id_: str, body: Settings, user=Depends(admin)):
    host(id_)
    if store.DEMO:
        raise HTTPException(400, 'Live network recording is disabled in demo mode')
    revision = uuid.uuid4().hex
    store.execute('INSERT OR REPLACE INTO network_settings VALUES (?,?,?)',
                  (id_, body.model_dump_json(), revision))
    return {'ok': True}


def samples(id_, start, end, limit=15000):
    rows = store.rows('SELECT id,measured,kind,target,payload FROM network_samples WHERE server_id=? AND measured>=? AND measured<=? ORDER BY measured,id LIMIT ?',
                      (id_, start, end, limit+1))
    return [{'id': r['id'], 'at': r['measured'], 'kind': r['kind'], 'target': r['target'],
             **json.loads(r['payload'])} for r in rows[:limit]], len(rows) > limit


@router.get('/samples')
def readings(id_: str, seconds: int = Query(default=120, ge=10, le=900), user=Depends(authenticated)):
    host(id_)
    end = time.time()
    rows, truncated = samples(id_, end-seconds, end)
    return {'start': end-seconds, 'end': end, 'samples': rows, 'truncated': truncated}


class Marker(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=160)


def mark(id_, label, automatic=False, measured=None):
    identity = uuid.uuid4().hex
    store.execute('INSERT INTO network_events(id,server_id,measured,label,automatic) VALUES (?,?,?,?,?)',
                  (identity, id_, measured or time.time(), label, int(automatic)))
    return identity


@router.post('/markers')
def marker(id_: str, body: Marker, user=Depends(admin)):
    host(id_)
    return {'id': mark(id_, body.label)}


@router.get('/export/{event_id}')
def export(id_: str, event_id: str, user=Depends(authenticated)):
    server = host(id_)
    event = store.one('SELECT * FROM network_events WHERE id=? AND server_id=?', (event_id, id_))
    if not event:
        raise HTTPException(404, 'Incident not found')
    settings, _ = config(id_)
    if event['archive']:
        bundle = json.loads(event['archive'])
    else:
        rows, truncated = samples(id_, event['measured']-120, event['measured']+300, 30000)
        bundle = {'samples': rows, 'truncated': truncated, 'settings': settings,
                  'device': json.loads(server['snapshot']).get('metrics', {}).get('openwrt', {}).get('board')}
    bundle.update(schema=1, incident={k: event[k] for k in ('id', 'measured', 'label', 'automatic', 'complete')},
                  server_name=server['name'], exported_at=time.time(),
                  scope='Recorder-to-target ICMP and router telemetry; not FaceTime media measurements')
    if 'runs' not in bundle:
        bundle['runs'] = run_metadata(id_, bundle['samples'])
    return Response(json.dumps(bundle, allow_nan=False), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="harbour-incident-{event_id}.json"'})


def run_metadata(id_, rows):
    identities = {row.get('run_id') for row in rows}
    return [{**r, 'config': json.loads(r['config'])} for r in
            store.rows('SELECT id,started,config,observer FROM network_runs WHERE server_id=?', (id_,)) if r['id'] in identities]


def persist(id_, rows, status, signature, revision):
    """Never commit results belonging to a removed, paused or reconfigured target."""
    from .logins import connection_signature
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        server = con.execute('SELECT * FROM servers WHERE id=?', (id_,)).fetchone()
        cfg = con.execute('SELECT * FROM network_settings WHERE server_id=?', (id_,)).fetchone()
        if (not server or server['server_type'] != 'openwrt' or not server['monitoring_enabled']
                or connection_signature(dict(server)) != signature or not cfg or cfg['revision'] != revision
                or not json.loads(cfg['config']).get('enabled')):
            return False
        con.executemany('INSERT INTO network_samples(server_id,measured,kind,target,payload) VALUES (?,?,?,?,?)',
                        [(id_, r['at'], r['kind'], r.get('target', ''), json.dumps({k:v for k,v in r.items() if k not in ('at','kind','target')}, allow_nan=False)) for r in rows])
        con.execute('INSERT OR REPLACE INTO network_status VALUES (?,?,?)', (id_, time.time(), json.dumps(status)))
    return True


def maintain(id_):
    now = time.time()
    server = store.one('SELECT snapshot FROM servers WHERE id=?', (id_,))
    if not server:
        return
    for event in store.rows('SELECT * FROM network_events WHERE server_id=? AND complete=0 AND measured<?', (id_, now-300)):
        rows, truncated = samples(id_, event['measured']-120, event['measured']+300, 30000)
        archive = json.dumps({'samples': rows, 'truncated': truncated, 'runs': run_metadata(id_, rows),
                              'settings': config(id_)[0], 'device': json.loads(server['snapshot']).get('metrics', {}).get('openwrt', {}).get('board')})
        store.execute('UPDATE network_events SET archive=?,complete=1 WHERE id=?', (archive, event['id']))
    with store.db() as con:
        con.execute('DELETE FROM network_samples WHERE server_id=? AND measured<?', (id_, now-RETENTION))
        # Bound retained payload as well as row count. Newest rows win; any
        # collection/storage gaps remain visible in source timestamps.
        con.execute('''DELETE FROM network_samples WHERE id IN (
          SELECT id FROM (SELECT id, row_number() OVER (ORDER BY measured DESC,id DESC) AS n,
          sum(length(payload)) OVER (ORDER BY measured DESC,id DESC) AS bytes
          FROM network_samples WHERE server_id=?) WHERE n>? OR bytes>?)''', (id_, MAX_ROWS, MAX_BYTES))
        con.execute('''DELETE FROM network_events WHERE id IN (
          SELECT id FROM (SELECT id,row_number() OVER (ORDER BY measured DESC) AS n,
          sum(length(coalesce(archive,''))) OVER (ORDER BY measured DESC) AS bytes
          FROM network_events WHERE server_id=?) WHERE n>20 OR bytes>33554432)''', (id_,))
        con.execute('DELETE FROM network_runs WHERE server_id=? AND started<? AND id NOT IN (SELECT id FROM network_runs WHERE server_id=? ORDER BY started DESC LIMIT 256)', (id_, now-RETENTION-600, id_))
