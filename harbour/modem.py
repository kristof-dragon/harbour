"""Direct modem polling on the Harbour host, independently of router SSH."""
import json
import secrets
import socket
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from . import auth, network, store
from .modem_client import Client, ModemError, AuthenticationError, endpoint

router = APIRouter(prefix='/api/servers/{id_}/modem')
_locks, _guard = {}, threading.Lock()
_clients = {}
DEFAULT = {'enabled': False, 'url': 'http://192.168.100.1', 'username': '', 'interval_seconds': 60, 'retention_days': 7}
MAX_SAMPLE = 32 * 1024
MAX_HISTORY = 64 * 1024 * 1024


def initialize():
    with store.db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS modem_settings (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
          url TEXT NOT NULL UNIQUE, config TEXT NOT NULL, password_encrypted TEXT NOT NULL,
          revision TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS modem_samples (
          id INTEGER PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          revision TEXT NOT NULL, measured REAL NOT NULL, received REAL NOT NULL,
          state TEXT NOT NULL, payload TEXT NOT NULL, bytes INTEGER NOT NULL, collection_ms REAL);
        CREATE INDEX IF NOT EXISTS modem_samples_time ON modem_samples(server_id,measured,id);
        ''')


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    url: str = Field(default=DEFAULT['url'], max_length=120)
    username: str = Field(default='', max_length=80)
    password: SecretStr = Field(default=SecretStr(''), max_length=256)
    clear_password: bool = False
    interval_seconds: int = Field(default=60, ge=30, le=900, strict=True)
    retention_days: int = Field(default=7, ge=1, le=30, strict=True)

    @field_validator('url')
    @classmethod
    def valid_endpoint(cls, value):
        return endpoint(value.strip())

    @field_validator('username')
    @classmethod
    def valid_username(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError('Username contains a control character')
        return value.strip()


def lock_for(id_):
    with _guard:
        return _locks.setdefault(id_, threading.Lock())


def config(id_):
    return store.one('SELECT * FROM modem_settings WHERE server_id=?', (id_,))


def public(id_):
    row = config(id_)
    settings = json.loads(row['config']) if row else dict(DEFAULT)
    latest = store.one('SELECT payload FROM modem_samples WHERE server_id=? AND revision=? ORDER BY measured DESC,id DESC LIMIT 1', (id_, row['revision'] if row else ''))
    sample = json.loads(latest['payload']) if latest else None
    stale = bool(sample and time.time()-sample['received_at'] > max(120, settings['interval_seconds']*2))
    server = network.host(id_)
    return {'settings': settings, 'password_saved': bool(row and row['password_encrypted']),
            'blocked': bool(row and row['blocked']), 'next_attempt': row['next_attempt'] if row else None,
            'sample': sample, 'stale': stale, 'observer': socket.gethostname(), 'demo': store.DEMO,
            'server_monitoring_enabled': bool(server['monitoring_enabled']),
            'retention_max_mib': 64}


@router.get('')
def get(id_: str, user=Depends(auth.authenticated)):
    return public(id_)


@router.put('')
def save(id_: str, body: Settings, user=Depends(auth.admin)):
    network.host(id_)
    password = body.password.get_secret_value()
    if password and body.clear_password:
        raise HTTPException(422, 'Choose a replacement password or remove the saved password.')
    lock = lock_for(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'A modem check is running. Try again when it finishes.')
    try:
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            old = con.execute('SELECT * FROM modem_settings WHERE server_id=?', (id_,)).fetchone()
            previous = json.loads(old['config']) if old else {}
            encrypted = old['password_encrypted'] if old else ''
            if encrypted and (previous.get('url') != body.url or previous.get('username') != body.username) and not (password or body.clear_password):
                raise HTTPException(422, 'Re-enter or remove the saved password when changing the address or username.')
            if body.clear_password:
                encrypted = ''
            elif password:
                encrypted = store.cipher().encrypt(password.encode()).decode()
            if body.enabled and not encrypted:
                raise HTTPException(422, 'Save the modem password before enabling polling.')
            duplicate = con.execute('SELECT server_id FROM modem_settings WHERE url=? AND server_id<>?', (body.url, id_)).fetchone()
            if duplicate:
                raise HTTPException(409, 'This modem address is already configured for another router.')
            values = body.model_dump(exclude={'password', 'clear_password'})
            revision = secrets.token_hex(16)
            con.execute('INSERT INTO modem_settings(server_id,url,config,password_encrypted,revision) VALUES (?,?,?,?,?) ON CONFLICT(server_id) DO UPDATE SET url=excluded.url,config=excluded.config,password_encrypted=excluded.password_encrypted,revision=excluded.revision,next_attempt=0,blocked=0,failures=0',
                        (id_, body.url, json.dumps(values), encrypted, revision))
        cached = _clients.get(id_)
        if cached and old and cached[0] == old['revision'] and encrypted == old['password_encrypted'] and previous.get('url') == body.url and previous.get('username') == body.username:
            _clients[id_] = (revision, cached[1])
        else:
            _clients.pop(id_, None)
    finally:
        lock.release()
    return public(id_)


def bounded_sample(sample):
    """Whole snapshot cap in addition to per-response/field bounds."""
    sample = json.loads(json.dumps(sample))
    sample['truncated'] = False
    while True:
        payload = json.dumps(sample, separators=(',', ':'))
        if len(payload.encode()) <= MAX_SAMPLE:
            return payload
        candidates = [s for s in sample.get('sections', {}).values() if s.get('fields')]
        if not candidates:
            raise ValueError('Modem snapshot metadata exceeds its limit')
        section = max(candidates, key=lambda s: len(s['fields']))
        section['fields'].pop()
        section['omitted_fields'] = section.get('omitted_fields', 0)+1
        sample['truncated'] = True


def persist(id_, revision, sample, blocked=False, failures=0):
    sample = {**sample, 'configuration_revision': revision}
    payload = bounded_sample(sample)
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM modem_settings WHERE server_id=?', (id_,)).fetchone()
        if not row or row['revision'] != revision:
            return False
        settings = json.loads(row['config'])
        con.execute('INSERT INTO modem_samples(server_id,revision,measured,received,state,payload,bytes,collection_ms) VALUES (?,?,?,?,?,?,?,?)',
                    (id_, revision, sample['at'], sample['received_at'], sample['state'], payload, len(payload.encode()), sample.get('collection_ms')))
        delay = min(900, settings['interval_seconds']*2**min(failures, 4))
        con.execute('UPDATE modem_settings SET next_attempt=?,blocked=?,failures=? WHERE server_id=? AND revision=?',
                    (time.time()+delay, blocked, failures, id_, revision))
        # Metadata-only bounded retention. Never sort large payloads in a write transaction.
        cutoff = time.time()-settings['retention_days']*86400
        size = con.execute('SELECT COALESCE(SUM(bytes),0) FROM modem_samples WHERE server_id=?', (id_,)).fetchone()[0]
        con.execute('DELETE FROM modem_samples WHERE id IN (SELECT id FROM modem_samples WHERE server_id=? AND (measured<? OR ?=1) ORDER BY measured,id LIMIT 256)',
                    (id_, cutoff, int(size > MAX_HISTORY)))
    return True


def poll_one(id_, manual=False):
    lock = lock_for(id_)
    if not lock.acquire(blocking=False):
        if manual:
            raise HTTPException(409, 'A modem check is already running.')
        return
    try:
        row = config(id_)
        if not row:
            raise HTTPException(400, 'Save modem settings first.')
        settings = json.loads(row['config'])
        if not manual and (row['blocked'] or row['next_attempt'] > time.time() or not settings['enabled']):
            return
        if not row['password_encrypted']:
            if manual:
                raise HTTPException(400, 'Enter the modem password first.')
            return
        if store.DEMO:
            return  # Never send demo credentials or create invented hardware samples.
        started = time.time()
        blocked, failure = False, 0
        try:
            cached = _clients.get(id_)
            if not cached or cached[0] != row['revision']:
                password = store.cipher().decrypt(row['password_encrypted'].encode()).decode()
                cached = (row['revision'], Client(settings['url'], settings['username'], password))
                _clients[id_] = cached
            sample = cached[1].poll()
            failure = 0 if sample['state'] == 'ok' else row['failures']+1
        except Exception as exc:
            # Never persist raw server responses, tokens, passwords or exception text.
            blocked = isinstance(exc, AuthenticationError) or (isinstance(exc, ModemError) and exc.code == 'username_required')
            failure = row['failures']+1
            code = exc.code if isinstance(exc, ModemError) else 'collection_failed'
            message = str(exc) if isinstance(exc, ModemError) else 'Modem collection failed; check the settings and test again.'
            sample = {'at': started, 'received_at': time.time(), 'state': 'unavailable', 'error_code': code,
                      'error': message, 'sections': {}, 'cause': 'not_established'}
            _clients.pop(id_, None)
        sample.update(observer=socket.gethostname(), source='modem', endpoint=settings['url'], adapter='compal-one', collection_ms=round((time.time()-started)*1000, 2))
        persist(id_, row['revision'], sample, blocked, failure)
    finally:
        lock.release()


@router.post('/test')
def test_connection(id_: str, user=Depends(auth.admin)):
    network.host(id_)
    poll_one(id_, manual=True)
    return public(id_)


@router.get('/history')
def history(id_: str, hours: int = Query(24, ge=1, le=720), summary: bool = Query(True), user=Depends(auth.authenticated)):
    network.host(id_)
    # 256 bounded snapshots per response; callers can export a smaller time window.
    columns = 'measured AS at,received AS received_at,state,revision AS configuration_revision,collection_ms' if summary else 'payload'
    rows = store.rows(f'SELECT {columns} FROM modem_samples WHERE server_id=? AND measured>=? ORDER BY measured DESC,id DESC LIMIT 257', (id_, time.time()-hours*3600))
    samples = list(reversed(rows[:256])) if summary else [json.loads(r['payload']) for r in reversed(rows[:256])]
    return {'samples': samples, 'truncated': len(rows)>256}


@router.get('/export')
def export(id_: str, hours: int = Query(24, ge=1, le=720), user=Depends(auth.authenticated)):
    data = history(id_, hours, False, user)
    return Response(json.dumps({'schema': 1, 'server_id': id_, 'cause': 'not_established', **data}),
                    media_type='application/json', headers={'Content-Disposition': 'attachment; filename="modem-evidence.json"'})


def run(stop):
    """One bounded independent worker; no HTTP work in the router/probe workers."""
    _clients.clear()
    while not stop.wait(1):
        if store.DEMO:
            continue
        try:
            rows = store.rows("SELECT m.server_id,m.config,m.blocked,m.next_attempt FROM modem_settings m JOIN servers s ON s.id=m.server_id WHERE s.monitoring_enabled=1 AND s.server_type='openwrt'")
            active = set()
            for row in rows:
                settings = json.loads(row['config'])
                # Manual tests also reuse sessions while automatic polling is off.
                active.add(row['server_id'])
                if stop.is_set():
                    break
                if settings['enabled'] and not row['blocked'] and row['next_attempt'] <= time.time():
                    poll_one(row['server_id'])
            for id_ in set(_clients)-active:
                _clients.pop(id_, None)
        except Exception:
            # A transient DB failure must not kill polling or log secrets.
            stop.wait(5)
    _clients.clear()
