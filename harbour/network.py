"""OpenWrt diagnostics configuration, durable measurements and incident export."""
import ipaddress
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import store, resource_limits as resources
from .auth import admin, authenticated

router = APIRouter(prefix='/api/servers/{id_}/network')
DEFAULTS = {'enabled': False, 'interval': 1.0, 'targets': ['1.1.1.1', '8.8.8.8'],
            'router_probes': True, 'wan_device': '', 'client_mac': '', 'latency_limit_ms': 150.0,
            'retention_hours': 168, 'max_rows': 0, 'max_payload_mib': 1024,
            'archive_enabled': True, 'archive_retention_days': 90, 'archive_max_mib': 4096,
            'incident_max_count': 200, 'incident_max_mib': 128, 'buffer_mib': 128, 'telemetry_seconds': 1}


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
        CREATE TABLE IF NOT EXISTS network_analysis (
          event_id TEXT PRIMARY KEY REFERENCES network_events(id) ON DELETE CASCADE,
          version INTEGER NOT NULL, digest TEXT NOT NULL, report TEXT NOT NULL);
        ''')
        # Both the web service and the recorder may start against an older DB.
        con.execute('BEGIN IMMEDIATE')
        for table, name, definition in (
                ('network_samples', 'archived', 'INTEGER NOT NULL DEFAULT 0'),
                ('network_samples', 'payload_bytes', 'INTEGER NOT NULL DEFAULT 0'),
                ('network_runs', 'written_through', 'INTEGER NOT NULL DEFAULT 0'),
                ('network_events', 'archived_version', 'INTEGER NOT NULL DEFAULT -1'),
                ('network_events', 'archive_bytes', 'INTEGER'),
                ('network_events', 'trigger', 'TEXT')):
            if name not in {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
                if name == 'payload_bytes':
                    con.execute('UPDATE network_samples SET payload_bytes=length(payload)')
        con.execute('CREATE INDEX IF NOT EXISTS network_pending ON network_samples(server_id,archived,id)')
        con.execute('CREATE INDEX IF NOT EXISTS network_sizes ON network_samples(server_id,measured,id,payload_bytes)')
        con.execute('CREATE INDEX IF NOT EXISTS network_event_time ON network_events(server_id,measured,id)')
    from . import network_archives
    network_archives.initialize()
    from . import call_tests
    call_tests.initialize(recover=False)


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
    targets: list[str] = Field(default_factory=lambda: list(DEFAULTS['targets']), min_length=1, max_length=12)
    router_probes: bool = True
    wan_device: str = Field(default='', max_length=32, pattern=r'^[A-Za-z0-9_.:@-]*$')
    client_mac: str = Field(default='', pattern=r'^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$|^$')
    latency_limit_ms: float = Field(default=150, ge=10, le=10000, allow_inf_nan=False)
    telemetry_seconds: int = Field(default=1, ge=1, le=60, strict=True)
    buffer_mib: int = Field(default=128, ge=8, le=4096, strict=True)
    retention_hours: int = Field(default=168, ge=1, le=8760, strict=True)
    max_rows: int = Field(default=0, ge=0, le=100000000, strict=True)
    max_payload_mib: int = Field(default=1024, ge=0, le=1048576, strict=True)
    archive_enabled: bool = True
    archive_retention_days: int = Field(default=90, ge=1, le=3650, strict=True)
    archive_max_mib: int = Field(default=4096, ge=0, le=1048576, strict=True)
    incident_max_count: int = Field(default=200, ge=0, le=10000, strict=True)
    incident_max_mib: int = Field(default=128, ge=0, le=1048576, strict=True)

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
    from . import call_tests, network_archives
    server = host(id_)
    settings, revision = config(id_)
    row = store.one('SELECT * FROM network_status WHERE server_id=?', (id_,))
    status = json.loads(row['payload']) if row else {}
    status.update(updated=row['updated'] if row else None,
                  recorder_online=bool(row and time.time()-row['updated'] < 10))
    from .network_runtime import read_status
    status.update(read_status(id_))
    return {'settings': settings, 'effective_settings':resources.settings(settings), 'resource_profile':'sbc-1g' if resources.small() else 'standard', 'write_interval_seconds':resources.flush_seconds(), 'total_buffer_bytes':resources.buffer_bytes(), 'revision': revision, 'status': status,
            'device': json.loads(server['snapshot']).get('metrics', {}).get('openwrt'),
            'events': store.rows('SELECT id,measured,label,automatic,complete FROM network_events WHERE server_id=? ORDER BY measured DESC LIMIT 50', (id_,)),
            'retention': {'seconds': settings['retention_hours']*3600, 'max_rows': settings['max_rows'],
                          'max_payload_bytes': settings['max_payload_mib']*1024*1024},
            'archives': network_archives.summary(id_), 'call_tests': call_tests.listing(id_)}


@router.get('/archives/{archive_id}')
def download_archive(id_: str, archive_id: str, user=Depends(authenticated)):
    from . import network_archives
    host(id_)
    row = store.one('SELECT * FROM network_archives WHERE id=? AND server_id=? AND complete=1', (archive_id, id_))
    if not row or not network_archives.path(row).is_file():
        raise HTTPException(404, 'Daily archive not found')
    return FileResponse(network_archives.path(row), media_type='application/gzip',
                        filename=network_archives.path(row).name)


@router.put('')
def save(id_: str, body: Settings, user=Depends(admin)):
    host(id_)
    if store.DEMO:
        raise HTTPException(400, 'Live network recording is disabled in demo mode')
    previous, revision = config(id_)
    probe_fields = ('enabled', 'interval', 'targets', 'router_probes', 'wan_device', 'client_mac', 'latency_limit_ms', 'telemetry_seconds')
    if not revision or any(previous[key] != getattr(body, key) for key in probe_fields):
        revision = uuid.uuid4().hex
    store.execute('INSERT OR REPLACE INTO network_settings VALUES (?,?,?)',
                  (id_, body.model_dump_json(), revision))
    return {'ok': True}


def samples(id_, start, end, limit=15000):
    result, retained = [], 0
    # Fetch one capped payload at a time; never fetchall a whole call window.
    with store.db() as con:
        cursor = con.execute('''SELECT id,measured,kind,target,
            CASE WHEN length(CAST(payload AS BLOB))<=1048576 THEN payload END AS payload
            FROM network_samples WHERE server_id=? AND measured>=? AND measured<=?
            ORDER BY measured,id LIMIT ?''', (id_,start,end,limit+1))
        for r in cursor:
            if len(result)>=limit or r['payload'] is None:
                return result, True
            row = {'id':r['id'], 'at':r['measured'], 'kind':r['kind'], 'target':r['target'], **json.loads(r['payload'])}
            if row['kind']=='telemetry':
                row['metrics'] = resources.telemetry(row.get('metrics',{}))
                row['evidence_scope'] = 'Diagnostic counters; repeated inventory omitted. Raw recording retention is configured separately.'
            retained += resources.size(row)
            if retained > resources.budget()*2:
                return result, True
            result.append(row)
    return result, False


@router.get('/samples')
@resources.bounded
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
@resources.bounded
def export(id_: str, event_id: str, user=Depends(authenticated)):
    server = host(id_)
    try:
        event = store.evidence_row('network_events','archive', 'id=? AND server_id=?', (event_id, id_))
    except resources.BudgetExceeded:
        return resources.saved_incident_download(id_,event_id)
    if not event:
        raise HTTPException(404, 'Incident not found')
    bundle = incident_bundle(server, event)
    return Response(json.dumps(bundle, allow_nan=False), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="harbour-incident-{event_id}.json"'})


def incident_bundle(server, event):
    """One evidence source for downloads and analysis, including sealed windows."""
    from . import call_tests
    id_ = server['id']
    settings, _ = config(id_)
    if event['archive'] and len(event['archive'].encode())>resources.budget():
        raise resources.BudgetExceeded()
    if event['archive']:
        bundle = json.loads(event['archive'])
    else:
        rows, truncated = samples(id_, event['measured']-120, event['measured']+300, 60000)
        bundle = {'samples': rows, 'truncated': truncated, 'settings': settings,
                  'traffic_tests': call_tests.context(id_, event['measured']-120, event['measured']+300),
                  'device': json.loads(server['snapshot']).get('metrics', {}).get('openwrt', {}).get('board')}
    bundle.update(schema=1, incident={k: event[k] for k in ('id', 'measured', 'label', 'automatic', 'complete')},
                  server_name=server['name'], server_id=id_, exported_at=time.time(),
                  scope='Recorder-to-target ICMP and router telemetry; not FaceTime media measurements')
    bundle['incident']['trigger'] = json.loads(event['trigger']) if event.get('trigger') else None
    if 'runs' not in bundle:
        bundle['runs'] = run_metadata(id_, bundle['samples'])
    return bundle


def run_metadata(id_, rows):
    identities = {row.get('run_id') for row in rows}
    return [{**r, 'config': json.loads(r['config'])} for r in
            store.rows('SELECT id,started,config,observer FROM network_runs WHERE server_id=?', (id_,)) if r['id'] in identities]


def persist(id_, rows, status, signature, revision, run=None, through=0):
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
        if run is not None:
            con.execute('INSERT OR IGNORE INTO network_runs(id,server_id,started,config,observer) VALUES (?,?,?,?,?)',
                        (run['id'], id_, run['started'], json.dumps(run['config']), run['observer']))
            committed = con.execute('SELECT written_through FROM network_runs WHERE id=? AND server_id=?',
                                    (run['id'], id_)).fetchone()
            if committed is None:
                return False
            if through <= committed['written_through']:
                rows = []
        values = []
        for r in rows:
            payload = json.dumps({k:v for k,v in r.items() if k not in ('at','kind','target')}, allow_nan=False)
            values.append((id_, r['at'], r['kind'], r.get('target', ''), payload, len(payload)))
        con.executemany('INSERT INTO network_samples(server_id,measured,kind,target,payload,payload_bytes) VALUES (?,?,?,?,?,?)', values)
        if run is not None:
            con.execute('UPDATE network_runs SET written_through=max(written_through,?) WHERE id=?', (through, run['id']))
            # Commit incident detection with the batch, so a writer dying before
            # acknowledgement cannot duplicate either samples or incident markers.
            from .incidents import qualifies, trigger_evidence
            bad = [r for r in rows if qualifies(r, run['config']['latency_limit_ms'])]
            if bad:
                measured = min(r['at'] for r in bad)
                previous = con.execute('SELECT max(measured) FROM network_events WHERE server_id=? AND automatic=1', (id_,)).fetchone()[0]
                if previous is None or measured-previous >= 300:
                    trigger = trigger_evidence(min(bad, key=lambda r: r['at']), run['config'])
                    con.execute('INSERT INTO network_events(id,server_id,measured,label,automatic,trigger) VALUES (?,?,?,?,1,?)',
                                (uuid.uuid4().hex, id_, measured, 'Probe degradation — inspect correlated evidence', json.dumps(trigger)))
        con.execute('INSERT OR REPLACE INTO network_status VALUES (?,?,?)', (id_, time.time(), json.dumps(status)))
    return True


def prune_incidents(id_, settings, stop=None):
    """Rank small metadata outside write transactions; delete one window at a time."""
    if not (settings['incident_max_count'] or settings['incident_max_mib']):
        return
    events = store.rows('''SELECT id,complete,archive_bytes FROM network_events
        WHERE server_id=? ORDER BY measured DESC,id DESC''', (id_,))
    total = 0
    for position, event in enumerate(events, 1):
        if stop and stop.is_set():
            return
        size = event['archive_bytes']
        if size is None:
            # Old installations have no cached size. Read one archive at a time
            # without a writer lock, then checkpoint its size in a short write.
            # Do not sort/window the archive column: SQLite may spool gigabytes.
            row = store.one('''SELECT complete,coalesce(length(CAST(archive AS BLOB)),0) AS bytes
                FROM network_events WHERE id=?''', (event['id'],))
            if row is None:
                continue
            size, event['complete'] = row['bytes'], row['complete']
            store.execute('''UPDATE network_events SET archive_bytes=?
                WHERE id=? AND archive_bytes IS NULL AND complete=?''',
                          (size, event['id'], event['complete']))
        total += size
        excess = (settings['incident_max_count'] and position > settings['incident_max_count']
                  or settings['incident_max_mib'] and total > settings['incident_max_mib']*1024*1024)
        if excess:
            # Recheck archive protection and sealing state after the read. Each
            # commit releases the writer for probes, call tests and web requests.
            store.execute('''DELETE FROM network_events WHERE id=? AND complete=? AND archive_bytes=?
                AND (?=0 OR archived_version=complete)''',
                          (event['id'], event['complete'], size, int(settings['archive_enabled'])))


def maintain(id_, stop=None):
    from . import call_tests, network_archives
    from .network_runtime import read_status
    now = time.time()
    server = store.one('SELECT snapshot FROM servers WHERE id=?', (id_,))
    if not server:
        return
    settings, _ = config(id_)
    runtime = read_status(id_)
    # A queued reading can still belong to an otherwise finished incident.
    pending = runtime.get('writer_stalled') or runtime.get('oldest_unsaved_seconds', 0) > resources.flush_seconds()+2
    events = [] if pending else store.rows('SELECT * FROM network_events WHERE server_id=? AND complete=0 AND measured<?', (id_, now-300))
    for event in events:
        rows, truncated = samples(id_, event['measured']-120, event['measured']+300, 60000)
        archive = json.dumps({'samples': rows, 'truncated': truncated, 'runs': run_metadata(id_, rows),
                              'traffic_tests': call_tests.context(id_, event['measured']-120, event['measured']+300),
                              'settings': config(id_)[0], 'device': json.loads(server['snapshot']).get('metrics', {}).get('openwrt', {}).get('board')})
        store.execute('UPDATE network_events SET archive=?,archive_bytes=?,complete=1 WHERE id=?',
                      (archive, len(archive.encode('utf-8')), event['id']))
    # Never prune an unarchived reading while daily archiving is enabled.
    # Archive failures are visible and retried; they must not erase evidence.
    network_archives.maintain(id_, settings, stop)
    protected = 'AND archived=1' if settings['archive_enabled'] else ''
    # Keep an active long call's complete window until its aggregate evidence
    # has been collected, even with a one-hour raw-retention setting.
    protected += " AND NOT EXISTS (SELECT 1 FROM network_call_tests ct WHERE ct.server_id=network_samples.server_id AND ct.status='running' AND network_samples.measured>=ct.created)"
    with store.db() as con:
        con.execute(f'DELETE FROM network_samples WHERE server_id=? AND measured<? {protected}',
                    (id_, now-settings['retention_hours']*3600))
        if settings['max_rows']:
            cutoff = con.execute('SELECT measured,id FROM network_samples WHERE server_id=? ORDER BY measured DESC,id DESC LIMIT 1 OFFSET ?',
                                 (id_, settings['max_rows'])).fetchone()
            if cutoff:
                con.execute(f'DELETE FROM network_samples WHERE server_id=? AND (measured,id)<=(?,?) {protected}', (id_, *cutoff))
    if settings['max_payload_mib']:
        # Covering index reads avoid repeatedly loading every JSON payload.
        excess = store.one('SELECT coalesce(sum(payload_bytes),0) AS n FROM network_samples WHERE server_id=?', (id_,))['n'] - settings['max_payload_mib']*1024*1024
        while excess > 0 and not (stop and stop.is_set()):
            oldest = store.rows(f'SELECT id,payload_bytes FROM network_samples WHERE server_id=? {protected} ORDER BY measured,id LIMIT 2000', (id_,))
            if not oldest:
                break
            delete = []
            for row in oldest:
                delete.append((row['id'],))
                excess -= row['payload_bytes']
                if excess <= 0:
                    break
            with store.db() as con:
                con.executemany('DELETE FROM network_samples WHERE id=?', delete)
    prune_incidents(id_, settings, stop)
    with store.db() as con:
        # Keep run context for every raw reading awaiting archive or inspection.
        con.execute('''DELETE FROM network_runs WHERE server_id=? AND started<?
          AND id NOT IN (SELECT id FROM network_runs WHERE server_id=? ORDER BY started DESC LIMIT 256)
          AND id NOT IN (SELECT DISTINCT json_extract(payload,'$.run_id') FROM network_samples
                        WHERE server_id=? AND json_extract(payload,'$.run_id') IS NOT NULL)''',
                    (id_, now-settings['retention_hours']*3600-600, id_, id_))
