"""Fleet login observation. Labels describe credentials/context, never proven people."""
import asyncio
import hashlib
import json
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import auth, login_sessions, logins, store, table_query

router = APIRouter()
changed = threading.Condition()
revision = 0
subscribers = set()


def initialize(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS bridge_profiles (
          id TEXT PRIMARY KEY, match_key TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
          tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS bridge_snapshots (
          ref TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          payload TEXT NOT NULL, collected_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS bridge_annotations (
          ref TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
    ''')
    con.execute("INSERT OR IGNORE INTO settings VALUES ('bridge',?)", (json.dumps({'enabled': False}),))
    expression = MATCH_SQL.replace('r.', '')
    con.execute('CREATE INDEX IF NOT EXISTS bridge_session_match ON host_login_sessions ('+expression+')')
    con.execute('CREATE INDEX IF NOT EXISTS bridge_event_match ON host_login_events ('+expression+') WHERE session_ref IS NULL')
    con.execute('CREATE INDEX IF NOT EXISTS bridge_snapshot_server ON bridge_snapshots(server_id)')


def enabled():
    row = store.one("SELECT value FROM settings WHERE key='bridge'")
    return bool(row and json.loads(row['value']).get('enabled'))


def require_enabled(user=Depends(auth.authenticated)):
    if not enabled():
        raise HTTPException(404, 'The Bridge is disabled in Global settings')
    return user


def publish():
    global revision
    with changed:
        revision += 1
        targets = list(subscribers)
    for loop, event in targets:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # The disconnected subscriber's event loop has already closed.


class Toggle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.put('/api/bridge/settings')
def configure(body: Toggle, user=Depends(auth.admin)):
    store.execute("UPDATE settings SET value=? WHERE key='bridge'", (json.dumps(body.model_dump()),))
    if not body.enabled:
        from . import login_live
        login_live.close_all()
    else:
        from . import ssh
        # Start on already-open authenticated transports; never create another login.
        with ssh.resource_sessions_lock:
            sessions = list(ssh.resource_sessions.items())
        from . import login_live
        for server_id, session in sessions:
            server = store.one('SELECT * FROM servers WHERE id=?', (server_id,))
            if server and isinstance(session, ssh.ResourceSession):
                login_live.ensure(session, server, logins.summary(server))
    publish()
    return body.model_dump()


@router.get('/api/bridge/stream')
async def stream(request: Request, user=Depends(require_enabled)):
    async def updates():
        previous = -1
        event = asyncio.Event()
        subscriber = (asyncio.get_running_loop(), event)
        with changed:
            subscribers.add(subscriber)
        try:
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(auth.authenticated, request)
                    if not await asyncio.to_thread(enabled):
                        yield 'event: disabled\ndata: {}\n\n'
                        return
                except HTTPException:
                    yield 'event: expired\ndata: {}\n\n'
                    return
                with changed:
                    current = revision
                yield ('event: changed\ndata: '+json.dumps({'revision': current})+'\n\n') if current != previous else ': heartbeat\n\n'
                previous = current
                # A bounded, coalescing signal; idle browsers consume no waiting threads.
                try:
                    await asyncio.wait_for(event.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
                event.clear()
                await asyncio.sleep(.25)
        finally:
            with changed:
                subscribers.discard(subscriber)
    return StreamingResponse(updates(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


def match_key(record):
    # Failed/offered keys are deliberately segregated from verified credentials.
    if record.get('credential_verified') is True and record.get('key_fingerprint') and record.get('result') == 'success':
        parts = ['credential', record.get('username'), record['key_fingerprint']]
    else:
        basis = 'attempt' if record.get('result') in ('failure', 'invalid_user', 'rejected') else 'context'
        parts = [basis, record['server_id'], record.get('username'), record.get('source_ip'), record.get('service'), record.get('method')]
    return json.dumps(parts, separators=(',', ':'), ensure_ascii=False)


MATCH_SQL = """CASE WHEN json_extract(r.payload,'$.credential_verified')=1 AND r.key_fingerprint IS NOT NULL AND r.result='success'
    THEN json_array('credential',r.username,r.key_fingerprint)
    ELSE json_array(CASE WHEN r.result IN ('failure','invalid_user','rejected') THEN 'attempt' ELSE 'context' END,
        r.server_id,r.username,r.source_ip,r.service,json_extract(r.payload,'$.method')) END"""
BASE = f"""SELECT r.*,s.name AS server_name,s.host AS server_host,{MATCH_SQL} AS match_key FROM (
    SELECT 'session:'||id AS ref,id,server_id,username,service,source_ip,key_fingerprint,result,payload,collected_at,
      'session' AS kind,json_extract(payload,'$.started_at') AS time FROM host_login_sessions
    UNION ALL SELECT 'event:'||id,id,server_id,username,service,source_ip,key_fingerprint,result,payload,collected_at,
      'event',coalesce(occurred_at,captured_at) FROM host_login_events WHERE session_ref IS NULL
    UNION ALL SELECT ref,ref,server_id,json_extract(payload,'$.username'),json_extract(payload,'$.service'),
      json_extract(payload,'$.source_ip'),NULL,'observed',payload,collected_at,'observation',
      coalesce(json_extract(payload,'$.started_at'),json_extract(payload,'$.confirmed_at')) FROM bridge_snapshots
    ) r JOIN servers s ON s.id=r.server_id"""
SOURCE = f"""SELECT r.*,p.id AS profile_id,coalesce(p.label,'') AS label,coalesce(p.tags,'[]') AS identity_tags,
    coalesce(a.tags,'[]') AS tags,coalesce(a.notes,'') AS notes,
    coalesce(r.source_ip,'') AS source,coalesce(r.key_fingerprint,'') AS key,
    json_extract(r.payload,'$.duration_seconds') AS duration,
    coalesce(p.label,'')||' '||coalesce(p.tags,'[]')||' '||coalesce(a.tags,'[]') AS identity
    FROM ({BASE}) r LEFT JOIN bridge_profiles p ON p.match_key=r.match_key
    LEFT JOIN bridge_annotations a ON a.ref=r.ref"""
COLUMNS = dict(time='date', server_name='text', username='text', service='text', result='text', source='text', key='text', identity='text', duration='number')


def decorate(record, con=None, snapshots=None):
    if con is None:
        with store.db() as connection:
            return decorate(record, connection, {})
    item = dict(record)
    if 'payload' in item:
        item = {**json.loads(item.pop('payload')), **item}
    item.setdefault('ref', ('session:' if item.get('record_type') == 'session' else 'event:')+str(item['id']))
    item['match_key'] = match_key(item)
    profile = con.execute('SELECT * FROM bridge_profiles WHERE match_key=?', (item['match_key'],)).fetchone()
    annotation = con.execute('SELECT * FROM bridge_annotations WHERE ref=?', (item['ref'],)).fetchone()
    item.update(profile_id=profile['id'] if profile else None, label=profile['label'] if profile else '',
        identity_tags=json.loads(profile['tags']) if profile else [],
        tags=json.loads(annotation['tags']) if annotation else [], notes=annotation['notes'] if annotation else '',
        match_basis=json.loads(item['match_key'])[0])
    if item.get('record_type') == 'session' or item.get('kind') == 'session':
        if item.get('state'):
            if item['server_id'] not in snapshots:
                row = con.execute('SELECT * FROM servers WHERE id=?', (item['server_id'],)).fetchone()
                server = dict(row) if row else None
                snapshots[item['server_id']] = login_sessions.presence(server, logins.summary(server)) if server else None
            if snapshots[item['server_id']] is not None:
                item = login_sessions.decorate(item, snapshots[item['server_id']])
    return item


def enrich(records):
    with store.db() as con:
        snapshots = {}
        result = []
        for record in records:
            item = decorate(record, con, snapshots)
            if item.get('evidence_events'):
                item['evidence_events'] = [decorate(e, con, snapshots) for e in item['evidence_events']]
            result.append(item)
        return result


def current_for_server(server, snapshot):
    records = []
    for item in login_sessions.current(server['id'], snapshot):
        item.update(server_id=server['id'], server_name=server['name'])
        if not item.get('event_count'):
            observed = next(x for x in snapshot['sessions'] if str(x['pid']) == str(item['pid']))
            key = json.dumps([server['id'], snapshot['boot_id'], item['pid'], observed['process_started_at']])
            item['ref'] = 'live:'+hashlib.sha256(key.encode()).hexdigest()
            item['process_started_at'] = observed['process_started_at']
            item['boot_id'] = snapshot['boot_id']
        records.append(item)
    return enrich(records)


def fleet():
    from . import login_live
    active, coverage = [], []
    for server in store.rows('SELECT * FROM servers ORDER BY name'):
        status = logins.summary(server)
        snapshot = login_sessions.presence(server, status)
        installed = status.get('state') == 'connected'
        coverage.append(dict(id=server['id'], name=server['name'], state=status.get('state'),
            verified=bool(installed and snapshot['available']), checked_at=snapshot.get('checked_at'),
            mode=login_live.mode(server['id'], status), paused=not server['monitoring_enabled'],
            sources=status.get('sources', {}), dropped=status.get('dropped', 0),
            detail=status.get('detail'), limitations=status.get('limitations', []),
            os_sessions=status.get('os_sessions', {}).get('state', 'unavailable')))
        if not installed:
            continue
        active.extend(current_for_server(server, snapshot))
        os_snapshot = status.get('os_sessions', {})
        if snapshot['fresh'] and os_snapshot.get('state') == 'ok':
            for item in os_snapshot.get('sessions', []):
                item = {**item, 'server_id': server['id'], 'server_name': server['name'], 'session_status': 'active',
                        'record_type': 'session', 'event_count': 0, 'confirmed_at': os_snapshot['checked_at']}
                key = json.dumps([server['id'], os_snapshot['boot_id'], item['session_id'], item.get('started_at')])
                item['id'] = item['ref'] = 'live:'+hashlib.sha256(key.encode()).hexdigest()
                active.append(decorate(item))
    return active, coverage


@router.get('/api/bridge')
def overview(user=Depends(require_enabled)):
    active, coverage = fleet()
    return dict(active=active, coverage=coverage, checked_at=time.time(), retention_days=90)


@router.get('/api/bridge/history')
def history(page: int = Query(1, ge=1, le=1000000), size: int = 25, sort: str = 'time', order: str = 'desc',
            filters: str = Query('{}', max_length=3000), profile: str = '', ref: str = '', user=Depends(require_enabled)):
    source, args = SOURCE, []
    if profile:
        candidate = store.one('SELECT match_key FROM bridge_profiles WHERE id=?', (profile,))
        if not candidate:
            raise HTTPException(404, 'Identity not found')
        source = 'SELECT * FROM ('+source+') WHERE match_key=?'; args = [candidate['match_key']]
    elif ref:
        source = 'SELECT * FROM ('+source+') WHERE match_key=?'; args = [match_key(resolve(ref))]
    with store.db() as con:
        rows, pagination = table_query.select(con, source, args, COLUMNS, filters=filters, page=page, size=size, sort=sort, order=order)
    return dict(records=enrich(rows), pagination=pagination)


def resolve(ref):
    if ref.startswith('live:'):
        item = next((r for r in fleet()[0] if r['ref'] == ref), None)
        if not item:
            item = store.one('SELECT * FROM ('+SOURCE+') WHERE ref=?', (ref,))
            if item:
                item = {**item, 'session_status': 'unconfirmed'}
    else:
        item = store.one('SELECT * FROM ('+SOURCE+') WHERE ref=?', (ref,))
        if not item and ref.startswith('event:') and ref[6:].isdigit():
            item = store.one('SELECT e.*,s.name AS server_name FROM host_login_events e JOIN servers s ON s.id=e.server_id WHERE e.id=?', (int(ref[6:]),))
            if item:
                item['ref'] = ref
    if not item:
        raise HTTPException(404, 'Session or event is no longer available')
    return decorate(item)


@router.get('/api/bridge/records/{ref}')
def detail(ref: str, user=Depends(require_enabled)):
    item = resolve(ref)
    stats = store.one('''SELECT count(*) AS observations,sum(kind='session') AS connections,
        min(time) AS first_seen,max(time) AS last_seen,count(DISTINCT server_id) AS servers,
        count(DISTINCT source_ip) AS addresses FROM ('''+BASE+') WHERE match_key=?', (item['match_key'],))
    evidence = store.rows('SELECT * FROM host_login_events WHERE session_ref=? ORDER BY id LIMIT 100', (item['id'],)) if item.get('event_count') else []
    profile = store.one('SELECT * FROM bridge_profiles WHERE match_key=?', (item['match_key'],))
    prior_tags = store.rows('SELECT a.tags FROM bridge_annotations a JOIN ('+BASE+') r ON r.ref=a.ref WHERE r.match_key=? AND r.ref!=? LIMIT 500', (item['match_key'], item['ref']))
    related_tags = sorted({tag for row in prior_tags for tag in json.loads(row['tags'])})
    return dict(record=item, stats=stats, related_tags=related_tags, evidence=[login_sessions.unpack(r) for r in evidence],
        profile_notes=profile['notes'] if profile else '', retention_days=90)


class Annotation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tags: list[str] = Field(default_factory=list, max_length=20)
    notes: str = Field(default='', max_length=2000)

    @field_validator('tags')
    @classmethod
    def clean_tags(cls, tags):
        cleaned = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
        if any(len(t) > 40 or any(ord(c) < 32 for c in t) for t in cleaned):
            raise ValueError('Tags must be at most 40 characters without control characters')
        return cleaned


class Profile(Annotation):
    label: str = Field(min_length=1, max_length=80)

    @field_validator('label')
    @classmethod
    def clean_label(cls, value):
        if not value.strip():
            raise ValueError('A label is required')
        return value.strip()


@router.put('/api/bridge/records/{ref}/annotation')
def annotate(ref: str, body: Annotation, user=Depends(auth.admin), feature=Depends(require_enabled)):
    item = resolve(ref)
    remember_snapshot(item)
    store.execute('''INSERT INTO bridge_annotations VALUES (?,?,?,?,?) ON CONFLICT(ref) DO UPDATE
        SET tags=excluded.tags,notes=excluded.notes,updated_at=excluded.updated_at''',
        (ref, item['server_id'], json.dumps(body.tags), body.notes, time.time()))
    publish()
    return {'ok': True}


@router.put('/api/bridge/records/{ref}/identity')
def identify(ref: str, body: Profile, user=Depends(auth.admin), feature=Depends(require_enabled)):
    item = resolve(ref)
    if not item.get('username') or item.get('service') == 'collector':
        raise HTTPException(422, 'This event has no account to label')
    remember_snapshot(item)
    store.execute('''INSERT INTO bridge_profiles VALUES (?,?,?,?,?,?) ON CONFLICT(match_key) DO UPDATE
        SET label=excluded.label,tags=excluded.tags,notes=excluded.notes,updated_at=excluded.updated_at''',
        (secrets.token_hex(16), match_key(item), body.label, json.dumps(body.tags), body.notes, time.time()))
    publish()
    return {'ok': True}


@router.get('/api/bridge/identities')
def identities(user=Depends(require_enabled)):
    # Profiles survive evidence retention, so a credential can be recognised later.
    return dict(profiles=[{**r, 'tags': json.loads(r['tags']), 'basis': json.loads(r['match_key'])[0],
        'username': json.loads(r['match_key'])[1 if json.loads(r['match_key'])[0] == 'credential' else 2]}
        for r in store.rows('SELECT * FROM bridge_profiles ORDER BY label LIMIT 10000')])


def compact():
    store.execute('DELETE FROM bridge_snapshots WHERE collected_at<?', (time.time()-90*86400,))
    store.execute('''DELETE FROM bridge_annotations WHERE updated_at<? AND ref NOT IN
        (SELECT 'session:'||id FROM host_login_sessions UNION ALL SELECT 'event:'||id FROM host_login_events)''',
        (time.time()-90*86400,))


@router.delete('/api/bridge/identities/{profile_id}')
def remove_identity(profile_id: str, user=Depends(auth.admin), feature=Depends(require_enabled)):
    store.execute('DELETE FROM bridge_profiles WHERE id=?', (profile_id,))
    publish()
    return {'ok': True}


def remember_snapshot(item):
    if not item['ref'].startswith('live:'):
        return
    saved = {**item, 'session_status': 'unconfirmed'}
    # Keep tagged snapshot-only observations reviewable after the process disappears.
    store.execute('INSERT OR IGNORE INTO bridge_snapshots VALUES (?,?,?,?)',
        (item['ref'], item['server_id'], json.dumps(saved), time.time()))


def reconcile(con, server_id):
    """Move snapshot annotations only when later login evidence unambiguously matches."""
    saved = con.execute('SELECT * FROM bridge_snapshots WHERE server_id=? LIMIT 500', (server_id,)).fetchall()
    if not saved:
        return
    for row in saved:
        observed = json.loads(row['payload'])
        if not observed.get('pid') or not observed.get('boot_id') or not observed.get('process_started_at'):
            continue
        candidates = [login_sessions.unpack(s) for s in con.execute(
            "SELECT * FROM host_login_sessions WHERE server_id=? AND username=? AND CAST(json_extract(payload,'$.pid') AS TEXT)=? ORDER BY id DESC LIMIT 10",
            (server_id, observed['username'], str(observed['pid'])))]
        candidates = [s for s in candidates if login_sessions.matches(s, observed,
            dict(boot_id=observed['boot_id'], checked_at=observed['confirmed_at']))]
        if len(candidates) != 1:
            continue
        ref = 'session:'+str(candidates[0]['id'])
        old = con.execute('SELECT * FROM bridge_annotations WHERE ref=?', (row['ref'],)).fetchone()
        existing = con.execute('SELECT * FROM bridge_annotations WHERE ref=?', (ref,)).fetchone()
        if old:
            tags = list(dict.fromkeys(json.loads(old['tags'])+(json.loads(existing['tags']) if existing else [])))
            notes = '\n'.join(x for x in [existing['notes'] if existing else '', old['notes']] if x)
            con.execute('INSERT OR REPLACE INTO bridge_annotations VALUES (?,?,?,?,?)',
                (ref, server_id, json.dumps(tags), notes, time.time()))
            con.execute('DELETE FROM bridge_annotations WHERE ref=?', (row['ref'],))
        con.execute('DELETE FROM bridge_snapshots WHERE ref=?', (row['ref'],))


@router.get('/api/bridge/identities/{profile_id}')
def identity_detail(profile_id: str, user=Depends(require_enabled)):
    profile = store.one('SELECT * FROM bridge_profiles WHERE id=?', (profile_id,))
    if not profile:
        raise HTTPException(404, 'Identity not found')
    return {**profile, 'tags': json.loads(profile['tags']), 'match': json.loads(profile['match_key'])}


@router.put('/api/bridge/identities/{profile_id}')
def edit_identity(profile_id: str, body: Profile, user=Depends(auth.admin), feature=Depends(require_enabled)):
    if not store.one('SELECT id FROM bridge_profiles WHERE id=?', (profile_id,)):
        raise HTTPException(404, 'Identity not found')
    store.execute('UPDATE bridge_profiles SET label=?,tags=?,notes=?,updated_at=? WHERE id=?',
        (body.label, json.dumps(body.tags), body.notes, time.time(), profile_id))
    publish()
    return {'ok': True}
