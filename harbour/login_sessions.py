"""Session projections over immutable authentication evidence.

Correlation requires source, collector, boot, user and process/session identity.
An unmatched opening is never proof that a connection is still alive.
"""
import hashlib
import json
import time

from . import store


def boot_key(value):
    return str(value or '').lower().replace('-', '')


def initialize(con):
    if 'session_ref' not in {r[1] for r in con.execute('PRAGMA table_info(host_login_events)')}:
        con.execute('ALTER TABLE host_login_events ADD COLUMN session_ref INTEGER')
    con.executescript('''
        CREATE TABLE IF NOT EXISTS host_login_sessions (
          id INTEGER PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          scope TEXT NOT NULL, state TEXT NOT NULL, username TEXT, service TEXT NOT NULL,
          source_ip TEXT, key_fingerprint TEXT, harbour_key INTEGER NOT NULL DEFAULT 0,
          result TEXT NOT NULL, collected_at REAL NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS login_session_scope ON host_login_sessions(server_id,scope,id DESC);
        CREATE INDEX IF NOT EXISTS login_session_page ON host_login_sessions(server_id,id DESC);
        CREATE INDEX IF NOT EXISTS login_session_open ON host_login_sessions(server_id,state);
        CREATE INDEX IF NOT EXISTS login_session_evidence ON host_login_events(session_ref,id);
        CREATE TABLE IF NOT EXISTS host_login_session_cursor (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE, through INTEGER NOT NULL);
    ''')


def unpack(row):
    row = dict(row)
    return {**json.loads(row.pop('payload')), **row}


def project(con, server_id, limit=500):
    cursor = con.execute('SELECT through FROM host_login_session_cursor WHERE server_id=?', (server_id,)).fetchone()
    rows = con.execute('SELECT * FROM host_login_events WHERE server_id=? AND id>? ORDER BY id LIMIT ?',
                       (server_id, cursor[0] if cursor else 0, limit)).fetchall()
    for row in rows:
        correlate(con, unpack(row))
    if rows:
        con.execute('INSERT OR REPLACE INTO host_login_session_cursor VALUES (?,?)', (server_id, rows[-1]['id']))
    return len(rows)


def backfill():
    for server in store.rows('SELECT id FROM servers'):
        while True:
            with store.db() as con:
                con.execute('BEGIN IMMEDIATE')
                count = project(con, server['id'])
            if count < 500:
                break


def correlate(con, event):
    kind, at = event.get('event_type'), event.get('occurred_at')
    authentication = kind == 'authentication' and event['result'] == 'success' and event['service'] == 'ssh'
    opening, closing = kind == 'session_start', kind in ('session_end', 'disconnect')
    identity = event.get('session_id') if event.get('session_id') is not None else event.get('pid')
    if (not (authentication or opening or closing) or identity is None or at is None
            or not all(event.get(k) for k in ('boot_id', 'username', 'source'))):
        return
    scope = hashlib.sha256(json.dumps([event['collector_id'], event['source'], boot_key(event['boot_id']),
        event['service'], event['username'], 'session' if event.get('session_id') is not None else 'pid', str(identity)]).encode()).hexdigest()
    previous = con.execute('SELECT * FROM host_login_sessions WHERE server_id=? AND scope=? ORDER BY id DESC LIMIT 1',
                           (event['server_id'], scope)).fetchone()
    old = unpack(previous) if previous else None
    same = bool(old and old['state'] == 'open' and not old.get('ambiguous'))
    if authentication:
        # A delayed authentication may arrive after its PAM opening.
        same = bool(same and old.get('authenticated_at') is None and 0 <= old['started_at']-at <= 30)
    elif opening:
        same = bool(same and old.get('opened_at') is None and old.get('authenticated_at') is not None
                    and 0 <= at-old['authenticated_at'] <= 60)
    elif closing:
        same = bool(same and at >= old['started_at'])
        if same and event.get('session_started_at') is not None:
            same = abs(event['session_started_at']-(old.get('opened_at') or old['started_at'])) < .01
        if not same:
            return  # An orphan/ambiguous close remains visible as raw evidence.
    if not same:
        ambiguous = bool(opening and old and old['state'] == 'open' and old.get('opened_at') is not None)
        if old and old['state'] == 'open':
            old['ambiguous'] = True
            old['state'] = 'unknown'
            save(con, old)
        session = {k: v for k, v in event.items() if k not in ('payload', 'evidence', 'session_ref')}
        session.update(record_type='session', scope=scope, state='open', started_at=at,
                       authenticated_at=None, opened_at=None, ended_at=None, event_count=0,
                       ambiguous=ambiguous)
    else:
        session = old
    if authentication:
        for key in ('method', 'source_ip', 'source_port', 'key_fingerprint', 'key_algorithm', 'credential_verified',
                    'certificate_id', 'certificate_serial', 'ca_fingerprint', 'harbour_key'):
            if event.get(key) is not None:
                session[key] = event[key]
        session['authenticated_at'] = at
        session['started_at'] = min(session['started_at'], at)
        session['result'] = 'success'
    elif opening:
        session['opened_at'] = at
    else:
        session.update(state='closed', ended_at=at)
        session['duration_seconds'] = at-(session.get('opened_at') or session['started_at'])
    session['event_count'] += 1
    session['collected_at'] = max(session['collected_at'], event['collected_at'])
    save(con, session)
    con.execute('UPDATE host_login_events SET session_ref=? WHERE id=?', (session['id'], event['id']))


def save(con, session):
    con.execute('''INSERT INTO host_login_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET state=excluded.state,source_ip=excluded.source_ip,
        key_fingerprint=excluded.key_fingerprint,harbour_key=excluded.harbour_key,result=excluded.result,
        collected_at=excluded.collected_at,payload=excluded.payload''',
        (session['id'], session['server_id'], session['scope'], session['state'], session.get('username'),
         session['service'], session.get('source_ip'), session.get('key_fingerprint'), session.get('harbour_key', 0),
         session['result'], session['collected_at'], json.dumps(session)))


def presence(server, status):
    snapshot = status.get('current_sessions') or {}
    fresh = bool(status.get('fetched_at') and time.time()-status['fetched_at'] <= max(90, (server.get('poll_seconds') or 60)*2))
    fresh = fresh and bool(server['monitoring_enabled']) and not server.get('error')
    available = fresh and snapshot.get('state') == 'ok'
    return {**snapshot, 'available': bool(available), 'fresh': bool(fresh),
            'fetched_at': status.get('fetched_at'), 'sessions': snapshot.get('sessions', [])}


def matches(session, observed, snapshot):
    return (session['service'] == 'ssh' and not session.get('ambiguous')
            and boot_key(session.get('boot_id')) == boot_key(snapshot.get('boot_id'))
            and str(session.get('pid')) == str(observed.get('pid'))
            and session.get('username') == observed.get('username')
            and observed['process_started_at'] <= session['started_at']+1
            and session['started_at'] <= snapshot['checked_at'])


def decorate(session, snapshot):
    session = dict(session)
    if session['state'] == 'closed':
        session['session_status'] = 'closed'
    elif session.get('ambiguous') or not snapshot['available'] or session['service'] != 'ssh':
        session['session_status'] = 'unconfirmed'
    elif any(matches(session, observed, snapshot) for observed in snapshot['sessions']):
        session['session_status'] = 'active'
        session['confirmed_at'] = snapshot['checked_at']
    elif session['started_at'] <= snapshot['checked_at']:
        session['session_status'] = 'ended'
    else:
        session['session_status'] = 'unconfirmed'
    return session


def current(server_id, snapshot):
    if not snapshot['available']:
        return []
    rows = store.rows("SELECT * FROM host_login_sessions WHERE server_id=? AND state='open' ORDER BY id DESC LIMIT 5000", (server_id,))
    sessions, result = [unpack(row) for row in rows], []
    for observed in snapshot['sessions']:
        candidates = [s for s in sessions if matches(s, observed, snapshot)]
        if len(candidates) == 1:
            result.append(decorate(candidates[0], snapshot))
        else:
            result.append(dict(record_type='session', id='live:'+str(observed['pid']), service='ssh',
                username=observed['username'], pid=observed['pid'], started_at=None, method=None,
                session_status='active', confirmed_at=snapshot['checked_at'], event_count=0,
                source_ip=None, key_fingerprint=None, harbour_key=0))
    return result


def listing(server_id, before, outcome, search, hide_harbour, snapshot):
    where, args = ['server_id=?'], [server_id]
    if before:
        where.append('id<?'); args.append(before)
    if outcome == 'success':
        where.append("result='success'")
    elif outcome == 'failure':
        where.append("result IN ('failure','invalid_user','rejected')")
    elif outcome == 'sessions':
        where.append("record_type='session' OR result IN ('opened','closed','disconnected','locked','unlocked')")
    elif outcome == 'collector':
        where.append("service='collector'")
    if hide_harbour:
        where.append('harbour_key=0')
    if search:
        where.append("instr(lower(coalesce(username,'')||' '||coalesce(source_ip,'')||' '||coalesce(key_fingerprint,'')),lower(?))>0")
        args.append(search)
    rows = store.rows('''SELECT * FROM (
        SELECT id,server_id,username,service,source_ip,key_fingerprint,harbour_key,result,'session' AS record_type
        FROM host_login_sessions UNION ALL
        SELECT id,server_id,username,service,source_ip,key_fingerprint,harbour_key,result,'event' AS record_type
        FROM host_login_events WHERE session_ref IS NULL)
        WHERE ''' + ' AND '.join('('+clause+')' for clause in where) + ' ORDER BY id DESC LIMIT 101', args)
    result = []
    with store.db() as con:
        for row in rows[:100]:
            if row['record_type'] == 'session':
                session = unpack(con.execute('SELECT * FROM host_login_sessions WHERE id=?', (row['id'],)).fetchone())
                session = decorate(session, snapshot)
                session['evidence_events'] = [unpack(e) for e in con.execute(
                    'SELECT * FROM host_login_events WHERE session_ref=? ORDER BY id LIMIT 20', (row['id'],))]
                result.append(session)
            else:
                result.append(unpack(con.execute('SELECT * FROM host_login_events WHERE id=?', (row['id'],)).fetchone()))
    return result, result[-1]['id'] if len(rows) > 100 else None


TABLE_COLUMNS = dict(time='date', duration='number', status='text', username='text', service='text', source='text', key='text')


def table_listing(server_id, snapshot, *, view, outcome, search, hide_harbour, **options):
    from . import table_query
    active = [s['id'] for s in current(server_id, snapshot) if s.get('event_count')]
    ids = ','.join('?' for _ in active) or 'NULL'
    available, checked = int(snapshot['available']), snapshot.get('checked_at') or 0
    session_status = f"""CASE WHEN state='closed' THEN 'closed'
        WHEN json_extract(payload,'$.ambiguous') OR NOT ? OR service!='ssh' THEN 'unconfirmed'
        WHEN id IN ({ids}) THEN 'connected'
        WHEN json_extract(payload,'$.started_at')<=? THEN 'ended' ELSE 'unconfirmed' END"""
    session_source = f'''SELECT *,json_extract(payload,'$.started_at') AS time,
        CASE WHEN state='closed' THEN json_extract(payload,'$.duration_seconds')
             WHEN id IN ({ids}) AND ? THEN max(0,?-coalesce(json_extract(payload,'$.opened_at'),json_extract(payload,'$.started_at'))) END AS duration,
        {session_status} AS status,'session' AS record_type FROM host_login_sessions WHERE server_id=?'''
    session_args = [*active, available, time.time(), available, *active, checked, server_id]
    raw_source = '''SELECT *,coalesce(occurred_at,captured_at) AS time,
        json_extract(payload,'$.duration_seconds') AS duration,result AS status,'event' AS record_type
        FROM host_login_events WHERE server_id=?'''+(' AND session_ref IS NULL' if view == 'sessions' else '')
    fields = "id,server_id,username,service,source_ip,key_fingerprint,harbour_key,result,payload,time,duration,status,record_type"
    source = ('SELECT '+fields+' FROM ('+session_source+') UNION ALL ' if view == 'sessions' else '')+'SELECT '+fields+' FROM ('+raw_source+')'
    args = (session_args if view == 'sessions' else [])+[server_id]
    where = ['1=1']
    if hide_harbour:
        where.append('harbour_key=0')
    if outcome == 'success':
        where.append("result='success'")
    elif outcome == 'failure':
        where.append("result IN ('failure','invalid_user','rejected')")
    elif outcome == 'sessions':
        where.append("(record_type='session' OR result IN ('opened','closed','disconnected','locked','unlocked'))")
    elif outcome == 'collector':
        where.append("service='collector'")
    if search:
        where.append("instr(lower(coalesce(username,'')||' '||coalesce(source_ip,'')||' '||coalesce(key_fingerprint,'')),lower(?))>0")
        args.append(search)
    source = '''SELECT id,time,duration,status,username,
        service||' '||coalesce(json_extract(payload,'$.method'),'') AS service,
        coalesce(source_ip,'')||' '||coalesce(json_extract(payload,'$.source_port'),'') AS source,
        key_fingerprint AS key,record_type FROM ('''+source+') WHERE '+' AND '.join(where)
    result = []
    with store.db() as con:
        con.execute('BEGIN')
        rows, metadata = table_query.select(con, source, args, TABLE_COLUMNS, **options)
        for row in rows:
            table = 'host_login_sessions' if row['record_type'] == 'session' else 'host_login_events'
            record = unpack(con.execute('SELECT * FROM '+table+' WHERE id=?', (row['id'],)).fetchone())
            if row['record_type'] == 'session':
                record = decorate(record, snapshot)
                record['evidence_events'] = [unpack(e) for e in con.execute('SELECT * FROM host_login_events WHERE session_ref=? ORDER BY id LIMIT 20', (row['id'],))]
            result.append(record)
    return result, metadata
