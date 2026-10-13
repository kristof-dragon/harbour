"""Saved remote endpoint settings and bounded, authenticated HTTP log retrieval."""
import http.client
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from . import auth, network, store

router = APIRouter()
DAY = r'\d{2}-\d{2}-\d{2}'
FILE = r'[a-zA-Z0-9_-]+\.jsonl'
LIMIT = 32 * 1024 * 1024


def initialize():
    # Preserve endpoint identities/settings while removing the saved-server
    # prerequisite. server_id is retained as a legacy column/API alias.
    with store.db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS call_endpoints (
            server_id TEXT PRIMARY KEY, config TEXT NOT NULL, deployed INTEGER NOT NULL DEFAULT 0)''')
        if con.execute('PRAGMA foreign_key_list(call_endpoints)').fetchall():
            con.execute('CREATE TABLE call_endpoints_new (server_id TEXT PRIMARY KEY, config TEXT NOT NULL, deployed INTEGER NOT NULL DEFAULT 0)')
            con.execute('INSERT INTO call_endpoints_new SELECT server_id,config,deployed FROM call_endpoints')
            con.execute('DROP TABLE call_endpoints')
            con.execute('ALTER TABLE call_endpoints_new RENAME TO call_endpoints')
        columns = {r[1] for r in con.execute('PRAGMA table_info(call_endpoints)')}
        for name, kind in [('callback_hash', 'TEXT'), ('callback_expires', 'REAL'), ('callback_boot', 'TEXT')]:
            if name not in columns:
                con.execute('ALTER TABLE call_endpoints ADD COLUMN ' + name + ' ' + kind)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint: str
    port: int = Field(default=5201, ge=1024, le=65534, strict=True)
    http_port: int = Field(default_factory=lambda: 20000 + secrets.randbelow(40000), ge=1024, le=65535, strict=True)
    token: SecretStr | None = Field(default=None, min_length=32, max_length=128)
    retention_days: int = Field(default=30, ge=1, le=365, strict=True)

    @field_validator('endpoint')
    @classmethod
    def address(cls, value):
        return network.Settings(targets=[value]).targets[0]

    @field_validator('token')
    @classmethod
    def safe_token(cls, value):
        if value and not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value.get_secret_value()):
            raise ValueError('Use 32–128 letters, numbers, underscores or hyphens')
        return value

    @model_validator(mode='after')
    def ports(self):
        if self.http_port in (self.port, self.port + 1):
            raise ValueError('Log and traffic ports must differ')
        return self


def saved(id_, secret=False):
    row = store.one('SELECT * FROM call_endpoints WHERE server_id=?', (id_,))
    if not row:
        raise HTTPException(404, 'Configure the remote endpoint first')
    config = json.loads(row['config'])
    encrypted = config.pop('token_encrypted')
    config.update(id=id_, server_id=id_, deployed=bool(row['deployed']), directory='~/harbour-c-remote',
                  callback_received=bool(row.get('callback_boot')))
    if secret:
        config['token'] = store.cipher().decrypt(encrypted.encode()).decode()
    return config


@router.get('/api/call-endpoints')
def listing(user=Depends(auth.authenticated)):
    return {'endpoints': [saved(row['server_id']) for row in store.rows('SELECT server_id FROM call_endpoints')]}


@router.put('/api/servers/{id_}/call-endpoint')
def configure(id_: str, body: Settings, user=Depends(auth.admin)):
    from .app import get_server, server_lock
    if store.DEMO:
        raise HTTPException(400, 'Remote deployment is disabled in demo mode')
    server = get_server(id_)
    if server['server_type'] == 'openwrt':
        raise HTTPException(400, 'Choose a Linux Docker host; OpenWrt remains read-only')
    lock = server_lock(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Wait for the current host operation to finish')
    try:
        old = store.one('SELECT config FROM call_endpoints WHERE server_id=?', (id_,))
        token = (body.token.get_secret_value() if body.token else
                 saved(id_, True)['token'] if old else secrets.token_urlsafe(32))
        config = body.model_dump(exclude={'token'})
        config['token_encrypted'] = store.cipher().encrypt(token.encode()).decode()
        store.execute('INSERT INTO call_endpoints(server_id,config) VALUES(?,?) ON CONFLICT(server_id) DO UPDATE SET config=excluded.config,deployed=0',
                      (id_, json.dumps(config)))
        return saved(id_)
    finally:
        lock.release()


def fetch(id_, path):
    config = saved(id_, True)
    conn = http.client.HTTPConnection(config['endpoint'], config['http_port'], timeout=5)
    try:
        conn.request('GET', path, headers={'Authorization': 'Bearer ' + config['token']})
        response = conn.getresponse()
        if response.status != 200:
            raise HTTPException(502, 'Remote logs returned HTTP ' + str(response.status))
        data = bytearray()
        deadline = time.monotonic() + 15
        while len(data) <= LIMIT:
            if time.monotonic() > deadline:
                raise HTTPException(504, 'Remote log request timed out')
            chunk = response.read1(min(65536, LIMIT + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise HTTPException(502, 'Remote log response exceeded 32 MiB')
    except (OSError, http.client.HTTPException):
        raise HTTPException(502, 'Cannot reach the remote log service. Check its port, token and firewall.') from None
    finally:
        conn.close()


@router.get('/api/servers/{id_}/call-endpoint/logs')
@router.get('/api/call-endpoints/{id_}/logs')
def logs(id_: str, day: str = '', user=Depends(auth.authenticated)):
    if day and not re.fullmatch(DAY, day):
        raise HTTPException(400, 'Use a YY-MM-DD log date')
    try:
        return json.loads(fetch(id_, '/logs' + ('/' + day if day else '')))
    except (ValueError, UnicodeError):
        raise HTTPException(502, 'Remote service returned invalid log metadata') from None


@router.get('/api/servers/{id_}/call-endpoint/logs/{day}/{filename}')
@router.get('/api/call-endpoints/{id_}/logs/{day}/{filename}')
def download(id_: str, day: str, filename: str, user=Depends(auth.authenticated)):
    if not re.fullmatch(DAY, day) or not re.fullmatch(FILE, filename):
        raise HTTPException(400, 'Invalid log path')
    return Response(fetch(id_, '/logs/' + day + '/' + filename), media_type='application/x-ndjson',
                    headers={'Content-Disposition': 'attachment; filename="' + day + '-' + filename + '"',
                             'Cache-Control': 'no-store'})


class Deployment(Settings):
    username: str = Field(default='root', pattern=r'^[A-Za-z_][A-Za-z0-9_.-]{0,63}$')
    ssh_port: int = Field(default=22, ge=1, le=65535, strict=True)
    password: SecretStr = Field(min_length=1, max_length=1024)
    fingerprint: str = Field(default='', pattern=r'^(SHA256:[A-Za-z0-9+/]{43})?$')
    callback_url: str = Field(max_length=2048)

    @field_validator('callback_url')
    @classmethod
    def callback_address(cls, value):
        value = value.strip().rstrip('/')
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(ord(c) < 33 for c in value)):
            raise ValueError('Enter the Harbour base HTTP(S) address reachable from the remote container')
        _ = parsed.port
        return value


@router.post('/api/call-endpoints/deploy')
def deploy(body: Deployment, request: Request, user=Depends(auth.admin)):
    from . import collector_setup, ssh
    from .app import server_lock
    if store.DEMO:
        raise HTTPException(400, 'Remote deployment is disabled in demo mode')
    if store.one("SELECT id FROM servers WHERE host=? AND server_type='openwrt'", (body.endpoint,)):
        raise HTTPException(400, 'OpenWrt remains read-only; choose a separate Linux Docker host')
    lock = server_lock('endpoint:' + body.endpoint)
    with collector_setup.guard:
        if len(collector_setup.operations) >= 4:
            raise HTTPException(429, 'Setup is busy; try again shortly')
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Deployment to this address is already running')
        try:
            fingerprint = body.fingerprint
            if not fingerprint:
                # First-use discovery pins the key for this one connection. The
                # form also accepts an independently verified fingerprint.
                try:
                    fingerprint = ssh.probe_host_key(body.endpoint, body.ssh_port)['fingerprint']
                except Exception:
                    raise HTTPException(502, 'Cannot read the SSH host fingerprint. Check the address and SSH port.') from None
            existing = next((r for r in store.rows('SELECT server_id,config FROM call_endpoints')
                             if json.loads(r['config'])['endpoint'] == body.endpoint), None)
            id_ = existing['server_id'] if existing else uuid.uuid4().hex
            if store.one("SELECT id FROM network_call_tests WHERE status='running' AND json_extract(config,'$.endpoint_id')=?", (id_,)):
                raise HTTPException(409, 'Stop the active call test before redeploying this endpoint')
            token = body.token.get_secret_value() if body.token else secrets.token_urlsafe(32)
            callback_token = secrets.token_urlsafe(32)
            config = body.model_dump(include={'endpoint', 'port', 'http_port', 'retention_days'})
            config.update(token_encrypted=store.cipher().encrypt(token.encode()).decode(), protocol=1)
            store.execute('''INSERT INTO call_endpoints(server_id,config,deployed,callback_hash,callback_expires)
                VALUES(?,?,0,?,?) ON CONFLICT(server_id) DO UPDATE SET config=excluded.config,deployed=0,
                callback_hash=excluded.callback_hash,callback_expires=excluded.callback_expires,callback_boot=NULL''',
                (id_, json.dumps(config), hashlib.sha256(callback_token.encode()).hexdigest(), time.time()+1200))
            remote = {k: v for k, v in config.items() if k != 'token_encrypted'}
            remote.update(token=token, endpoint_id=id_, callback_token=callback_token,
                          callback_url=body.callback_url + '/api/call-endpoints/' + id_ + '/ready')
            server = {'id': id_, 'name': body.endpoint, 'host': body.endpoint, 'port': body.ssh_port,
                      'username': body.username, 'fingerprint': fingerprint}
            op = collector_setup.Operation(server, 'call-endpoint', 'install', user, lock,
                                           credential={'password': body.password.get_secret_value()}, endpoint_config=remote)
            body.password = SecretStr('')
            # The request is never persisted. Drop the framework body cache once
            # validation is complete; only the short-lived SSH operation needs it.
            request._body = b''
            collector_setup.operations[op.id] = op
        except BaseException:
            lock.release()
            raise
    return collector_setup.response(op, request, user, auth.client_ip(request))


class Ready(BaseModel):
    model_config = ConfigDict(extra='forbid')
    boot_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    protocol: int = Field(ge=1, le=1, strict=True)


@router.post('/api/call-endpoints/{id_}/ready')
def ready(id_: str, body: Ready, request: Request):
    # This machine callback uses a deployment-specific bearer challenge, never
    # a browser session, SSH password, query-string secret or permanent API key.
    supplied = request.headers.get('authorization', '')
    digest = hashlib.sha256(supplied.removeprefix('Bearer ').encode()).hexdigest()
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM call_endpoints WHERE server_id=?', (id_,)).fetchone()
        if (not row or not supplied.startswith('Bearer ') or not row['callback_hash']
                or (row['callback_expires'] or 0) < time.time()
                or not hmac.compare_digest(digest, row['callback_hash'])
                or row['callback_boot'] not in (None, body.boot_id)):
            raise HTTPException(401, 'Invalid or expired deployment confirmation')
        con.execute('UPDATE call_endpoints SET callback_boot=? WHERE server_id=?', (body.boot_id, id_))
    return {'ok': True}


def status(id_):
    try:
        data = json.loads(fetch(id_, '/status'))
        if (not isinstance(data, dict) or data.get('endpoint_id') != id_ or data.get('protocol') != 1
                or not data.get('ready') or not re.fullmatch(r'[a-f0-9]{32}', str(data.get('boot_id', '')))):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise HTTPException(502, 'The remote endpoint is not ready or needs redeployment') from None


def confirm(id_, cancel, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cancel.check()
        row = store.one('SELECT callback_boot FROM call_endpoints WHERE server_id=?', (id_,))
        if row and row['callback_boot']:
            try:
                data = status(id_)
                if data['boot_id'] == row['callback_boot']:
                    store.execute('UPDATE call_endpoints SET deployed=1 WHERE server_id=?', (id_,))
                    return data
            except HTTPException:
                pass
        time.sleep(.25)
    raise HTTPException(502, 'Docker started but confirmation is incomplete. Check the Harbour callback address and remote HTTP port, then Check connection; no SSH password is retained.')


@router.post('/api/call-endpoints/{id_}/check')
def check_endpoint(id_: str, user=Depends(auth.admin)):
    row = store.one('SELECT callback_boot FROM call_endpoints WHERE server_id=?', (id_,))
    status(id_)
    if not row or not row['callback_boot']:
        raise HTTPException(409, 'No startup callback received. Check the callback address and redeploy.')
    store.execute('UPDATE call_endpoints SET deployed=1 WHERE server_id=?', (id_,))
    return saved(id_)
