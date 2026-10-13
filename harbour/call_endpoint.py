"""Saved remote endpoint settings and bounded, authenticated HTTP log retrieval."""
import http.client
import json
import re
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from . import auth, network, store

router = APIRouter()
DAY = r'\d{2}-\d{2}-\d{2}'
FILE = r'[a-zA-Z0-9_-]+\.jsonl'
LIMIT = 4 * 1024 * 1024


def initialize():
    store.execute('''CREATE TABLE IF NOT EXISTS call_endpoints (
        server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
        config TEXT NOT NULL, deployed INTEGER NOT NULL DEFAULT 0)''')


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
    config.update(server_id=id_, deployed=bool(row['deployed']), directory='~/harbour-c-remote')
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
        raise HTTPException(502, 'Remote log response exceeded 4 MiB')
    except (OSError, http.client.HTTPException):
        raise HTTPException(502, 'Cannot reach the remote log service. Check its port, token and firewall.') from None
    finally:
        conn.close()


@router.get('/api/servers/{id_}/call-endpoint/logs')
def logs(id_: str, day: str = '', user=Depends(auth.authenticated)):
    if day and not re.fullmatch(DAY, day):
        raise HTTPException(400, 'Use a YY-MM-DD log date')
    try:
        return json.loads(fetch(id_, '/logs' + ('/' + day if day else '')))
    except (ValueError, UnicodeError):
        raise HTTPException(502, 'Remote service returned invalid log metadata') from None


@router.get('/api/servers/{id_}/call-endpoint/logs/{day}/{filename}')
def download(id_: str, day: str, filename: str, user=Depends(auth.authenticated)):
    if not re.fullmatch(DAY, day) or not re.fullmatch(FILE, filename):
        raise HTTPException(400, 'Invalid log path')
    return Response(fetch(id_, '/logs/' + day + '/' + filename), media_type='application/x-ndjson',
                    headers={'Content-Disposition': 'attachment; filename="' + day + '-' + filename + '"',
                             'Cache-Control': 'no-store'})
