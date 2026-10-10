"""Short-lived, administrator-only setup streams, independent of polling SSH."""
import asyncio
import hashlib
import json
import queue
import secrets
import shlex
import threading
import time
from pathlib import Path
from typing import Literal

import paramiko
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from . import auth, collector_bundle, logins, ssh, store

router = APIRouter()
operations = {}
guard = threading.RLock()
MAX_OUTPUT = 1_000_000


class SetupInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['push', 'extract', 'install']


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator('password')
    @classmethod
    def one_line(cls, value):
        if any(c in value.get_secret_value() for c in '\n\r\x00'):
            raise ValueError('The sudo password must be a single line')
        return value


class Operation:
    def __init__(self, server, kind, action, user, lock):
        self.id = secrets.token_urlsafe(24)
        self.server, self.kind, self.action = server, kind, action
        self.session, self.lock = user['session_token'], lock
        self.cancel = ssh.Cancellation()
        self.events, self.replies = queue.Queue(maxsize=512), queue.Queue(maxsize=1)
        self.prompt = None
        self.state_lock = threading.Lock()
        self.done = threading.Event()
        self.started = False
        self.deadline = time.monotonic() + 900

    def emit(self, event):
        self.cancel.check()
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.cancel.stop()
            raise RuntimeError('Progress reader is too slow') from None

    def password(self):
        with self.state_lock:
            self.prompt = secrets.token_hex(16)
            prompt = self.prompt
        self.emit({'kind': 'password', 'prompt': prompt,
                   'message': 'Sudo needs a password on ' + self.server['name'] + '. It is used once and is not saved.'})
        deadline = min(self.deadline, time.monotonic() + 120)
        try:
            while time.monotonic() < deadline:
                self.cancel.check()
                try:
                    return self.replies.get(timeout=.1)
                except queue.Empty:
                    pass
            raise TimeoutError('Sudo password prompt timed out')
        finally:
            with self.state_lock:
                self.prompt = None


def transfer(op):
    """Always open a fresh pinned client; never borrow ResourceSession or its lock."""
    server, client = op.server, None
    data = collector_bundle.build(op.kind)
    prefix, installer, files = collector_bundle.BUNDLES[op.kind]
    config = {'kind': op.kind, 'action': op.action, 'prefix': prefix, 'installer': installer, 'files': files,
              'reader': server['username'], 'interval': server.get('record_seconds') or 60,
              'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    try:
        credential = ({'password': store.cipher().decrypt(server['password_encrypted'].encode()).decode()}
                      if server.get('auth_method') == 'password' else {'pkey': ssh.stored_key(server['key_id'])})
        op.emit({'kind': 'output', 'text': 'Opening a separate SSH connection…\n'})
        client = ssh.connect(server, cancel=op.cancel, **credential)
        credential.clear()
        client.get_transport().set_keepalive(20)
        source = Path(__file__).with_name('remote_collector_setup.py').read_bytes()
        bootstrap = "import sys;exec(compile(sys.stdin.buffer.read(%d),'<harbour-setup>','exec'))" % len(source)
        command = '/bin/sh -c ' + shlex.quote(ssh.REMOTE_ENV + '\nexec python3 -u -c ' + shlex.quote(bootstrap))
        stdin, stdout, _ = client.exec_command(command, timeout=15)
        channel = stdout.channel
        stdin.write(source)
        stdin.write((json.dumps(config) + '\n').encode())
        stdin.write(data); stdin.flush()
        pending, received, result = bytearray(), 0, None
        checked = 0
        while time.monotonic() < op.deadline:
            op.cancel.check()
            if time.monotonic() - checked >= 1:
                current = store.one('SELECT * FROM servers WHERE id=?', (server['id'],))
                if not current or logins.connection_signature(current) != logins.connection_signature(server):
                    raise ssh.Cancelled('Host connection changed')
                checked = time.monotonic()
            if channel.recv_ready():
                chunk = channel.recv(16384); received += len(chunk); pending.extend(chunk)
                if received > MAX_OUTPUT:
                    raise RuntimeError('Setup output exceeded its limit')
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n'); pending = bytearray(rest)
                    event = json.loads(line)
                    if event.get('kind') == 'password':
                        password = op.password()
                        current = store.one('SELECT * FROM servers WHERE id=?', (server['id'],))
                        if not current or logins.connection_signature(current) != logins.connection_signature(server):
                            raise ssh.Cancelled('Host connection changed')
                        stdin.write(json.dumps({'password': password}) + '\n'); stdin.flush()
                        del password
                    elif event.get('kind') == 'result':
                        result = event
                    elif event.get('kind') in ('output', 'progress'):
                        op.emit(event)
                    else:
                        raise RuntimeError('Invalid setup response')
            if channel.recv_stderr_ready():
                # Bootstrap/SSH errors are deliberately not surfaced verbatim.
                received += len(channel.recv_stderr(16384))
                if received > MAX_OUTPUT:
                    raise RuntimeError('Setup output exceeded its limit')
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                code = channel.recv_exit_status()
                if pending.strip() or not result:
                    raise RuntimeError('Setup ended without a result')
                if code and result.get('ok'):
                    raise RuntimeError('Setup process failed')
                return result
            time.sleep(.02)
        raise TimeoutError('Setup timed out')
    finally:
        if client:
            client.close()
        op.cancel.unbind()


def worker(op, username, address):
    try:
        result = transfer(op)
        op.emit(result)
        auth.log(username, address, 'collector_setup_succeeded' if result.get('ok') else 'collector_setup_failed',
                 op.server['id'] + ' · ' + op.kind + ' · ' + op.action)
    except Exception as exc:
        message = ('Host fingerprint mismatch. Verify the server identity before retrying.' if isinstance(exc, ssh.HostKeyMismatch)
                   else 'SSH sign-in failed. Check the saved connection credentials.' if isinstance(exc, paramiko.AuthenticationException)
                   else 'Setup or its sudo prompt timed out. Check the host before retrying; completed steps are retained.' if isinstance(exc, TimeoutError)
                   else 'Setup stopped. Check SSH access, Python 3.9+, and the host service before retrying; completed steps are retained.')
        if not op.cancel.is_set():
            try:
                op.emit({'kind': 'result', 'ok': False, 'message': message})
            except Exception:
                pass
        auth.log(username, address, 'collector_setup_stopped', op.server['id'] + ' · ' + op.kind + ' · ' + op.action)
    finally:
        with op.state_lock:
            op.prompt = None
            while not op.replies.empty():
                op.replies.get_nowait()
        op.done.set()
        op.lock.release()
        with guard:
            operations.pop(op.id, None)


def close_all():
    with guard:
        active = list(operations.values())
    for op in active:
        op.cancel.stop()
    deadline = time.monotonic() + 20
    for op in active:
        op.done.wait(max(0, deadline - time.monotonic()))


class SetupResponse(StreamingResponse):
    """Release reservations even if the client leaves before body iteration."""
    def __init__(self, op, content):
        self.op = op
        super().__init__(content, media_type='application/x-ndjson',
                         headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.op.cancel.stop()
            if not self.op.started:
                self.op.lock.release()
                self.op.done.set()
                with guard:
                    operations.pop(self.op.id, None)


@router.post('/api/servers/{id_}/collector/{kind}')
def start(id_: str, kind: Literal['resources', 'logins'], body: SetupInput, request: Request, user=Depends(auth.admin)):
    from .app import get_server, server_lock
    if get_server(id_)['server_type'] == 'openwrt':
        raise HTTPException(400, 'OpenWRT is read-only. Router collector installation is disabled.')
    if store.DEMO:
        raise HTTPException(400, 'Collector push and installation are disabled in demo mode')
    lock = server_lock(id_)
    with guard:
        if len(operations) >= 4:
            raise HTTPException(429, 'Collector setup is busy. Try again shortly.')
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Wait for the current host operation to finish')
        try:
            server = get_server(id_)
            if server['server_type'] == 'openwrt':
                raise HTTPException(400, 'OpenWRT is read-only. Router collector installation is disabled.')
            op = Operation(server, kind, body.action, user, lock)
            address = auth.client_ip(request)
            operations[op.id] = op
        except BaseException:
            lock.release()
            raise

    async def stream():
        try:
            yield json.dumps({'kind': 'started', 'id': op.id}) + '\n'
            thread = threading.Thread(target=worker, args=(op, user['name'], address), daemon=True)
            thread.start(); op.started = True
            checked = time.monotonic()
            while not op.done.is_set() or not op.events.empty():
                if await request.is_disconnected():
                    break
                if time.monotonic() - checked >= 5:
                    # Revoked/expired sessions must not keep privileged work alive.
                    auth.admin(auth.authenticated(request))
                    checked = time.monotonic()
                    yield json.dumps({'kind': 'heartbeat'}) + '\n'
                try:
                    event = op.events.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(.05)
                    continue
                yield json.dumps(event) + '\n'
        finally:
            op.cancel.stop()

    return SetupResponse(op, stream())


def owned(id_, user):
    with guard:
        op = operations.get(id_)
    if not op or op.session != user['session_token']:
        raise HTTPException(404, 'Setup operation not found')
    return op


@router.post('/api/collector-operations/{id_}/password')
def password(id_: str, body: PasswordInput, user=Depends(auth.admin)):
    op = owned(id_, user)
    with op.state_lock:
        if not op.prompt or not secrets.compare_digest(op.prompt, body.prompt):
            raise HTTPException(409, 'That sudo prompt has ended')
        op.replies.put_nowait(body.password.get_secret_value())
        op.prompt = None  # Each challenge can be answered only once.
    return {'ok': True}


@router.post('/api/collector-operations/{id_}/cancel')
def cancel(id_: str, user=Depends(auth.admin)):
    owned(id_, user).cancel.stop()
    return {'ok': True}
