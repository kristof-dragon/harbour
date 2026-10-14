"""Host authentication history, separate from Harbour's own sign-in audit."""
import base64
import hashlib
import json
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from . import login_sessions, store
from .auth import authenticated, admin

router = APIRouter()
MAX_BATCH = 200


def connection_signature(server):
    values = {k: server[k] for k in ('host', 'port', 'username', 'fingerprint', 'auth_method', 'key_id', 'password_encrypted', 'server_type')}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@router.get('/api/login-collector/download')
def collector_download(user=Depends(admin)):
    from .collector_bundle import build
    from fastapi.responses import Response
    return Response(build('logins'), media_type='application/zip', headers={
        'Content-Disposition': 'attachment; filename="harbour-login-collector.zip"', 'Cache-Control': 'no-store'})


def initialize(con):
    con.executescript('''
      CREATE TABLE IF NOT EXISTS host_login_state (
        server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
        connection TEXT NOT NULL, collector_id TEXT, acknowledgement TEXT,
        status TEXT NOT NULL DEFAULT '{}', fetched_at REAL);
      CREATE TABLE IF NOT EXISTS host_login_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
        collector_id TEXT NOT NULL, seq INTEGER NOT NULL, source_event_id TEXT NOT NULL, occurred_at REAL,
        captured_at REAL NOT NULL, collected_at REAL NOT NULL, acknowledged_at REAL,
        username TEXT, service TEXT NOT NULL, result TEXT NOT NULL,
        source_ip TEXT, key_fingerprint TEXT, harbour_key INTEGER NOT NULL DEFAULT 0,
        payload TEXT NOT NULL, UNIQUE(server_id,collector_id,seq), UNIQUE(server_id,collector_id,source_event_id));
      CREATE INDEX IF NOT EXISTS host_login_page ON host_login_events(server_id,id DESC);
      CREATE INDEX IF NOT EXISTS host_login_retention ON host_login_events(collected_at);
    ''')
    login_sessions.initialize(con)


def payload(server_id, signature):
    row = store.one('SELECT * FROM host_login_state WHERE server_id=?', (server_id,))
    from . import bridge
    request = {'operation': 'resources'}
    if bridge.enabled():
        request['login_os_sessions'] = True
    if row and row['connection'] == signature and row['acknowledgement']:
        request['logins_ack'] = json.loads(row['acknowledgement'])
    return request


def finite(value, optional=False):
    if optional and value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid event timestamp')
    return value


def validate(batch):
    if not isinstance(batch, dict) or len(json.dumps(batch, allow_nan=False)) > 1_500_000:
        raise ValueError('Invalid login batch')
    if batch.get('protocol') != 1:
        raise ValueError('Unsupported collector protocol')
    identity = batch.get('collector_id')
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
        raise ValueError('Invalid collector identity')
    events = batch.get('events')
    if not isinstance(events, list) or len(events) > MAX_BATCH:
        raise ValueError('Invalid login event count')
    previous = 0
    for event in events:
        if not isinstance(event, dict) or len(json.dumps(event)) > 8192:
            raise ValueError('Invalid login event')
        seq = event.get('seq')
        if type(seq) is not int or not previous < seq < 2**63:
            raise ValueError('Invalid event sequence')
        previous = seq
        event_identity = event.get('event_id')
        if not isinstance(event_identity, str) or len(event_identity) != 64 or any(c not in '0123456789abcdef' for c in event_identity):
            raise ValueError('Invalid source event identity')
        if any(value is not None and type(value) not in (str, int, float, bool) for value in event.values()):
            raise ValueError('Invalid event field type')
        finite(event.get('captured_at'))
        finite(event.get('occurred_at'), optional=True)
        for key in ('username', 'service', 'result', 'source_ip', 'key_fingerprint'):
            value = event.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise ValueError('Invalid event ' + key)
        if not event.get('service') or not event.get('result'):
            raise ValueError('Missing event classification')
    ack = batch.get('acknowledgement')
    if events:
        if not isinstance(ack, dict) or ack.get('collector_id') != identity or ack.get('through') != previous or not isinstance(ack.get('token'), str) or len(ack['token']) != 64:
            raise ValueError('Invalid batch acknowledgement')
    elif ack is not None:
        raise ValueError('Unexpected acknowledgement')
    if type(batch.get('acked_through')) is not int or not 0 <= batch['acked_through'] < 2**63:
        raise ValueError('Invalid acknowledgement sequence')
    finite(batch.get('acknowledged_at'), optional=True)
    if not isinstance(batch.get('status'), dict):
        raise ValueError('Invalid collector status')
    for key in ('pending', 'dropped'):
        if type(batch.get(key)) is not int or not 0 <= batch[key] < 2**63:
            raise ValueError('Invalid collector counter')
    status = batch['status']
    if not isinstance(status.get('sources', {}), dict) or len(status.get('sources', {})) > 16:
        raise ValueError('Invalid collector sources')
    for source in status.get('sources', {}).values():
        if not isinstance(source, dict) or source.get('state') not in ('starting','listening','partial','error','gap'):
            raise ValueError('Invalid source state')
        if not isinstance(source.get('detail', ''), str):
            raise ValueError('Invalid source detail')
    for key in ('limitations', 'coverage'):
        value = status.get(key, [])
        if not isinstance(value, list) or len(value) > 16 or any(not isinstance(v, str) or len(v) > 500 for v in value):
            raise ValueError('Invalid coverage information')
    return batch


def key_fingerprint(server):
    if server.get('auth_method') == 'password' or not server.get('key_id'):
        return None
    key = store.one('SELECT public FROM ssh_keys WHERE id=?', (server['key_id'],))
    try:
        raw = base64.b64decode(key['public'].split()[1], validate=True)
        return 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
    except (TypeError, IndexError, ValueError):
        return None


def validate_presence(value):
    if not isinstance(value, dict) or value.get('state') != 'ok':
        return {'state': 'unavailable', 'detail': 'Current SSH sessions have not been verified by the host.'}
    finite(value.get('checked_at'))
    if not isinstance(value.get('boot_id'), str) or not 1 <= len(value['boot_id']) <= 128:
        raise ValueError('Invalid session snapshot boot identity')
    rows = value.get('sessions')
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError('Invalid session snapshot')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not str(row.get('pid', '')).isdigit():
            raise ValueError('Invalid session process')
        if not isinstance(row.get('username'), str) or not 1 <= len(row['username']) <= 512:
            raise ValueError('Invalid session user')
        pid = str(row['pid'])
        if len(pid) > 20 or int(pid) <= 0 or pid in seen:
            raise ValueError('Invalid session process identity')
        seen.add(pid)
        finite(row.get('process_started_at'))
        if row['process_started_at'] > value['checked_at']:
            raise ValueError('Invalid session process time')
        result.append(dict(pid=pid, username=row['username'], process_started_at=row['process_started_at']))
    return dict(state='ok', checked_at=value['checked_at'], boot_id=value['boot_id'], sessions=result, scope='ssh')


def validate_os_presence(value):
    unavailable = dict(state='unavailable', detail='Console/desktop presence has not been verified.')
    try:
        if not isinstance(value, dict) or value.get('state') != 'ok':
            return unavailable
        finite(value.get('checked_at'))
        if not isinstance(value.get('boot_id'), str) or not 1 <= len(value['boot_id']) <= 128:
            return unavailable
        scope = value.get('scope', 'linux-logind')
        sources = {'linux-logind': 'Linux logind', 'macos-console': 'macOS console registry'}
        if scope not in sources:
            return unavailable
        rows = value.get('sessions')
        if not isinstance(rows, list) or len(rows) > 128:
            return unavailable
        seen, clean = set(), []
        fields = ('session_id', 'presence_id', 'username', 'uid', 'service', 'method', 'source_ip', 'tty', 'session_type')
        for row in rows:
            if not isinstance(row, dict) or any(row.get(k) is not None and (not isinstance(row[k], str) or len(row[k]) > 512) for k in fields):
                return unavailable
            identity = row.get('presence_id') or row.get('session_id')
            if not row.get('username') or not row.get('session_id') or not row.get('service') or identity in seen:
                return unavailable
            seen.add(identity)
            if any(row.get(k) is not None and type(row[k]) is not bool for k in ('locked', 'on_console')):
                return unavailable
            finite(row.get('started_at'), optional=True)
            if row.get('started_at') and row['started_at'] > value['checked_at']:
                return unavailable
            clean.append({**{k: row.get(k) for k in fields}, 'started_at': row.get('started_at'),
                'presence_source': sources[scope], 'result': 'opened',
                'locked': row.get('locked'), 'on_console': row.get('on_console')})
        return dict(state='ok', checked_at=value['checked_at'], boot_id=value['boot_id'], sessions=clean, scope=scope, source=sources[scope])
    except (ValueError, TypeError):
        return unavailable


def collect(server, batch, signature, is_current):
    """Persist the entire batch and its next-probe acknowledgement atomically.

    is_current runs inside the write transaction to reject removed/reconfigured
    hosts and cancelled workers. Invalid batches never advance acknowledgements.
    """
    if batch is None:
        return
    error = None
    try:
        if batch.get('state'):
            status = {'state': batch['state'], 'detail': str(batch.get('detail', ''))[:500]}
        else:
            validate(batch)
            status = dict(batch['status'], state='connected', pending=batch.get('pending', 0),
                          dropped=batch.get('dropped', 0), version=batch.get('version'),
                          last_drop_at=batch.get('last_drop_at'), ack_warning=batch.get('ack_warning'))
    except (ValueError, TypeError, AttributeError) as exc:
        error = str(exc)
        status = dict(state='error', detail='Login batch rejected: ' + error)
    fingerprint = key_fingerprint(server)
    try:
        status['current_sessions'] = validate_presence(batch.get('current_sessions'))
    except (ValueError, TypeError, AttributeError):
        status['current_sessions'] = {'state': 'unavailable', 'detail': 'Host session information was invalid.'}
    status['os_sessions'] = validate_os_presence(batch.get('os_sessions'))
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM servers WHERE id=?', (server['id'],)).fetchone()
        if not is_current(row):
            return
        previous = con.execute('SELECT * FROM host_login_state WHERE server_id=?', (server['id'],)).fetchone()
        now = time.time()
        if previous and previous['connection'] == signature:
            old = json.loads(previous['status'])
            # A slower ordinary poll must not replace a more recent live snapshot.
            if (old.get('current_sessions', {}).get('checked_at', 0) > status['current_sessions'].get('checked_at', float('inf'))):
                status['current_sessions'] = old['current_sessions']
            if old.get('os_sessions', {}).get('checked_at', 0) > status['os_sessions'].get('checked_at', float('inf')):
                status['os_sessions'] = old['os_sessions']
        if error or batch.get('state'):
            # Keep the pending ACK through outages, but never carry it to another connection.
            con.execute('''INSERT INTO host_login_state(server_id,connection,status,fetched_at) VALUES (?,?,?,?)
                ON CONFLICT(server_id) DO UPDATE SET status=excluded.status,fetched_at=excluded.fetched_at,
                acknowledgement=CASE WHEN connection=excluded.connection THEN acknowledgement ELSE NULL END,
                connection=excluded.connection''', (server['id'], signature, json.dumps(status), now))
            return
        if previous and previous['collector_id'] and previous['collector_id'] != batch['collector_id']:
            status['detail'] = 'Collector identity changed; earlier events remain in history.'
        for event in batch['events']:
            own = bool(fingerprint and event.get('result') == 'success' and event.get('credential_verified') is True and
                       event.get('username') == server['username'] and event.get('key_fingerprint') == fingerprint)
            con.execute('''INSERT OR IGNORE INTO host_login_events
                (server_id,collector_id,seq,source_event_id,occurred_at,captured_at,collected_at,username,service,result,source_ip,key_fingerprint,harbour_key,payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (server['id'], batch['collector_id'], event['seq'], event['event_id'], event.get('occurred_at'),
                event['captured_at'], now, event.get('username'), event['service'], event['result'], event.get('source_ip'),
                event.get('key_fingerprint'), int(own), json.dumps(event)))
        login_sessions.project(con, server['id'])
        from . import bridge
        bridge.reconcile(con, server['id'])
        if batch['acknowledged_at'] is not None:
            con.execute('''UPDATE host_login_events SET acknowledged_at=? WHERE server_id=? AND collector_id=?
                AND seq<=? AND acknowledged_at IS NULL''', (batch['acknowledged_at'], server['id'], batch['collector_id'], batch['acked_through']))
        con.execute('''INSERT INTO host_login_state(server_id,connection,collector_id,acknowledgement,status,fetched_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(server_id) DO UPDATE SET connection=excluded.connection,
            collector_id=excluded.collector_id,acknowledgement=excluded.acknowledgement,status=excluded.status,fetched_at=excluded.fetched_at''',
            (server['id'], signature, batch['collector_id'], json.dumps(batch['acknowledgement']) if batch['acknowledgement'] else None,
             json.dumps(status), now))
    from . import bridge
    bridge.publish()


def summary(server):
    row = store.one('SELECT connection,status,fetched_at FROM host_login_state WHERE server_id=?', (server['id'],))
    if row and row['connection'] != connection_signature(server):
        return {'state': 'pending', 'detail': 'Connection changed; awaiting the collector on this connection. Earlier history is retained.'}
    return {**json.loads(row['status']), 'fetched_at': row['fetched_at']} if row else {'state': 'pending'}


def compact():
    store.execute('DELETE FROM host_login_events WHERE collected_at<?', (time.time() - 90*86400,))
    store.execute('DELETE FROM host_login_sessions WHERE collected_at<?', (time.time() - 90*86400,))
    from . import bridge
    bridge.compact()


def seed_demo():
    if not store.DEMO:
        return
    for server in store.rows('SELECT * FROM servers'):
        if store.one('SELECT id FROM host_login_events WHERE server_id=?', (server['id'],)):
            continue
        now = time.time()
        rows = [dict(seq=1, occurred_at=now-3600, captured_at=now-3600, service='ssh', result='success',
                     event_type='authentication', username='example-user', method='publickey', source_ip='192.0.2.24', source_port=52144,
                     key_algorithm='ED25519', key_fingerprint='SHA256:DemoCredentialNotARealKey', credential_verified=True,
                     source='demo', evidence='Simulated successful SSH public-key authentication.'),
                dict(seq=2, occurred_at=now-600, captured_at=now-600, service='ssh', result='failure',
                     event_type='authentication', username='admin', method='password', source_ip='198.51.100.42', source_port=41992,
                     source='demo', evidence='Simulated failed SSH authentication.'),
                dict(seq=3, occurred_at=now-120, captured_at=now-120, service='login', result='opened',
                     event_type='session_start', username='example-user', uid=501, method='pam', source='demo')]
        for row in rows:
            row['event_id'] = hashlib.sha256((server['id'] + ':' + str(row['seq'])).encode()).hexdigest()
        batch = dict(protocol=1, version='demo', collector_id=hashlib.sha256(server['id'].encode()).hexdigest()[:32],
                     events=rows, acknowledgement=None, acked_through=3, acknowledged_at=now-30, pending=0, dropped=0,
                     status=dict(platform='demo', sources={'simulated': {'state':'listening','detail':'Demo events only'}},
                                 limitations=['Simulated records; no collector is installed or contacted.']))
        batch['acknowledgement'] = dict(collector_id=batch['collector_id'], through=3, token='0'*64)
        collect(server, batch, connection_signature(server), lambda row: row is not None)
        store.execute('UPDATE host_login_state SET acknowledgement=NULL WHERE server_id=?', (server['id'],))


@router.get('/api/servers/{server_id}/logins')
def events(server_id: str, before: int = Query(default=0, ge=0),
           outcome: str = Query(default='all', pattern='^(all|success|failure|sessions|collector)$'),
           search: str = Query(default='', max_length=128), hide_harbour: bool = False,
           view: str = Query(default='events', pattern='^(events|sessions)$'), user=Depends(authenticated)):
    server = store.one('SELECT * FROM servers WHERE id=?', (server_id,))
    if not server:
        raise HTTPException(404, 'Server not found')
    status = summary(server)
    presence = login_sessions.presence(server, status)
    current = login_sessions.current(server_id, presence)
    if view == 'sessions':
        result, next_before = login_sessions.listing(server_id, before, outcome, search, hide_harbour, presence)
        return dict(events=result, next_before=next_before, status=status, paused=not server['monitoring_enabled'],
                    host_checked_at=server['checked'], host_error=bool(server['error']), retention_days=90,
                    current_sessions=current, session_presence={k:v for k,v in presence.items() if k != 'sessions'})
    where, args = ['server_id=?'], [server_id]
    if before:
        where.append('id<?'); args.append(before)
    if outcome == 'success':
        where.append("result='success'")
    elif outcome == 'failure':
        where.append("result IN ('failure','invalid_user','rejected')")
    elif outcome == 'sessions':
        where.append("result IN ('opened','closed','disconnected','locked','unlocked')")
    elif outcome == 'collector':
        where.append("service='collector'")
    if hide_harbour:
        where.append('harbour_key=0')
    if search:
        where.append("(instr(lower(coalesce(username,'')),lower(?))>0 OR instr(lower(coalesce(source_ip,'')),lower(?))>0 OR instr(lower(coalesce(key_fingerprint,'')),lower(?))>0)")
        args.extend([search]*3)
    rows = store.rows('SELECT * FROM host_login_events WHERE ' + ' AND '.join(where) + ' ORDER BY id DESC LIMIT 101', args)
    result = []
    for row in rows[:100]:
        event = json.loads(row.pop('payload'))
        result.append({**event, **row})
    return dict(events=result, next_before=result[-1]['id'] if len(rows)>100 else None,
                status=status, paused=not server['monitoring_enabled'],
                host_checked_at=server['checked'], host_error=bool(server['error']), retention_days=90,
                current_sessions=current, session_presence={k:v for k,v in presence.items() if k != 'sessions'})


@router.get('/api/servers/{server_id}/logins/table')
def event_table(server_id: str, page: int = Query(1, ge=1, le=1000000), size: int = Query(25),
                sort: str = 'time', order: str = 'desc', filters: str = Query('{}', max_length=3000),
                view: str = Query('sessions', pattern='^(events|sessions)$'),
                outcome: str = Query('all', pattern='^(all|success|failure|sessions|collector)$'),
                search: str = Query('', max_length=128), hide_harbour: bool = False, user=Depends(authenticated)):
    server = store.one('SELECT * FROM servers WHERE id=?', (server_id,))
    if not server:
        raise HTTPException(404, 'Server not found')
    status = summary(server)
    presence = login_sessions.presence(server, status)
    records, pagination = login_sessions.table_listing(server_id, presence, view=view, outcome=outcome,
        search=search, hide_harbour=hide_harbour, page=page, size=size, sort=sort, order=order, filters=filters)
    current = login_sessions.current(server_id,presence)
    from . import bridge
    if bridge.enabled():
        records = bridge.enrich(records)
        current = bridge.current_for_server(server, presence)
        current.extend(bridge.current_os_for_server(server, status, presence))
        presence = {**presence, 'os_available': bridge.os_presence_available(status, presence),
            'os_checked_at': status.get('os_sessions', {}).get('checked_at'),
            'os_source': status.get('os_sessions', {}).get('source')}
    return dict(events=records, pagination=pagination, status=status, paused=not server['monitoring_enabled'],
        host_checked_at=server['checked'], host_error=bool(server['error']), retention_days=90,
        current_sessions=current,
        session_presence={k:v for k,v in presence.items() if k != 'sessions'})
