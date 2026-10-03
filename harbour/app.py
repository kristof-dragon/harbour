import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from . import __version__, auth, demo, history, remote_probe, ssh, store, volumes
from .auth import authenticated, admin

pool = ThreadPoolExecutor(max_workers=4)
resource_pool = ThreadPoolExecutor(max_workers=4)
stop = threading.Event()
locks = {}
locks_guard = threading.Lock()
queue_guard = threading.RLock()
job_queues = {}
detail_refreshes = set()
resource_locks = {}
STATIC = Path(__file__).with_name("static")


def server_lock(id_):
    with locks_guard:
        return locks.setdefault(id_, threading.Lock())


def get_server(id_):
    server = store.one("SELECT * FROM servers WHERE id=?", (id_,))
    if not server:
        raise HTTPException(404, "Server not found")
    return server


def preserve_update_checks(services, previous):
    """Reconcile local image IDs with the last registry check, including recreations."""
    old = {s['id']: s for s in previous}
    identities = {}
    for service in previous:
        identity = (service.get('project'), service['name'] if service.get('project') else service['container'])
        identities.setdefault(identity, []).append(service)
    for service in services:
        identity = (service.get('project'), service['name'] if service.get('project') else service['container'])
        matches = identities.get(identity, [])
        prior = old.get(service['id']) or (matches[0] if len(matches) == 1 else {})
        if prior.get('image') != service['image'] or prior.get('platform') != service.get('platform'):
            continue
        update = prior.get('update', {})
        if prior.get('image_id') == service['image_id']:
            service['update'] = dict(update) if update else service['update']
        elif update.get('status') in {'available', 'current'} and update.get('digest') and update['digest'] == service['image_id']:
            # The previously available config digest is now the running image.
            service['update'] = {k: v for k, v in update.items() if k != 'error'}
            service['update'].update(status='current', version=service.get('version'))


def refresh_server(id_, updates=False):
    server = get_server(id_)
    store.execute("UPDATE servers SET last_attempt=? WHERE id=?", (time.time(), id_))
    try:
        previous = json.loads(server["snapshot"])
        if store.DEMO:
            snapshot = previous
            if snapshot.get("metrics"):
                snapshot["metrics"]["timezone"] = demo.demo_timezone(id_)
            snapshot["latency_ms"] = {"atlas": 1.8, "luna": 2.4, "edge": 18.6, "backup": 3.2}.get(id_, 2)
            if updates:
                for service in snapshot.get("services", []):
                    service["update"]["checked"] = time.time()
        else:
            snapshot = ssh.request(server, {"operation": "snapshot", "updates": updates and server["server_type"] == "docker", "server_type": server["server_type"]})
            if not updates:
                preserve_update_checks(snapshot['services'], previous.get('services', []))
            snapshot["history"] = (previous.get("history", []) + [snapshot["metrics"]["cpu"]])[-48:]
        if server["server_type"] == "plain":
            snapshot["services"] = []
            snapshot.get("metrics", {})["docker"] = None
        store.execute("UPDATE servers SET snapshot=?, error=NULL, checked=?, update_checked=?,latency_ms=?,connection_status='up' WHERE id=?",
                      (json.dumps(snapshot), time.time(), time.time() if updates else server["update_checked"], snapshot.get("latency_ms"), id_))
    except Exception as exc:
        status = getattr(exc, "status", "unknown")
        latency = getattr(exc, "latency_ms", None)
        store.execute("UPDATE servers SET error=?,connection_status=?,latency_ms=? WHERE id=?", (str(exc)[:1000], status, latency, id_))
        history.record(id_, latency_ms=latency, up=status == "up")
        clear_resolved_warning_dismissals(id_)
        raise
    history.record(id_, snapshot.get("metrics"), snapshot.get("latency_ms"), up=True)
    clear_resolved_warning_dismissals(id_)


def poll_one(server, lock):
    try:
        refresh_server(server["id"], not server.get('_details_only') and time.time() - server["update_checked"] > history.policy()["update_check_hours"]*3600)
    except Exception:
        pass  # Persisted on the server and surfaced in the dashboard.
    finally:
        lock.release()
        dispatch_queued(server['id'])


def poll_resources(server, lock):
    """Continue lightweight readings while Docker holds the operation lock."""
    id_ = server['id']
    try:
        store.execute('UPDATE servers SET last_attempt=? WHERE id=?', (time.time(), id_))
        if store.DEMO:
            result = json.loads(get_server(id_)['snapshot'])
            result['latency_ms'] = server['latency_ms']
        else:
            result = ssh.request(server, {'operation': 'resources'})
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM servers WHERE id=?', (id_,)).fetchone()
            if not row or connection_signature(dict(row)) != connection_signature(server):
                return
            snapshot = json.loads(row['snapshot'])
            metrics = result['metrics']
            metrics['docker'] = snapshot.get('metrics', {}).get('docker')
            snapshot['metrics'] = metrics
            snapshot['latency_ms'] = result.get('latency_ms')
            snapshot['history'] = (snapshot.get('history', []) + [metrics['cpu']])[-48:]
            con.execute("UPDATE servers SET snapshot=?,checked=?,latency_ms=?,connection_status='up' WHERE id=?",
                        (json.dumps(snapshot), time.time(), result.get('latency_ms'), id_))
        history.record(id_, metrics, result.get('latency_ms'), up=True)
        clear_resolved_warning_dismissals(id_)
    except Exception as exc:
        status, latency = getattr(exc, 'status', 'unknown'), getattr(exc, 'latency_ms', None)
        history.record(id_, latency_ms=latency, up=status == 'up')
        store.execute('UPDATE servers SET error=?,connection_status=?,latency_ms=? WHERE id=?', (str(exc)[:1000], status, latency, id_))
    finally:
        lock.release()


def poll_due():
    with queue_guard:
        for id_ in list(job_queues):
            dispatch_queued(id_)
    interval = history.policy()["poll_seconds"]
    for server in store.rows("SELECT * FROM servers"):
        resources_due = server['monitoring_enabled'] and time.time() - (server["last_attempt"] or 0) >= (server["poll_seconds"] or interval)
        with queue_guard:
            details_due = server['id'] in detail_refreshes
        if resources_due or details_due:
            # Submit only once per host; don't grow an unbounded queue during slow registry calls.
            lock = server_lock(server["id"])
            if lock.acquire(blocking=False):
                with queue_guard:
                    server['_details_only'] = server['id'] in detail_refreshes
                    detail_refreshes.discard(server['id'])
                try:
                    pool.submit(poll_one, server, lock)
                except Exception:
                    if server['_details_only']:
                        with queue_guard:
                            detail_refreshes.add(server['id'])
                    lock.release()
                    raise
            elif resources_due:
                with locks_guard:
                    resource_lock = resource_locks.setdefault(server['id'], threading.Lock())
                if resource_lock.acquire(blocking=False):
                    try:
                        resource_pool.submit(poll_resources, server, resource_lock)
                    except Exception:
                        resource_lock.release()
                        raise


def poll_loop():
    while not stop.wait(5):
        poll_due()


def retention_loop():
    while not stop.is_set():
        try:
            history.compact()
            store.execute("DELETE FROM auth_log WHERE created<?", (time.time()-90*86400,))
            store.execute("DELETE FROM mfa_pending WHERE expires<?", (time.time(),))
            store.execute("DELETE FROM ip_bans WHERE until<?", (time.time(),))
            store.execute("DELETE FROM login_attempts WHERE created<?", (time.time()-900,))
            store.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        except Exception:
            import logging
            logging.exception("History retention sweep failed")
        if stop.wait(3600):
            break


@asynccontextmanager
async def lifespan(app):
    auth.trusted_proxies()
    store.initialize()
    with queue_guard:
        detail_refreshes.clear()
    if store.DEMO:
        demo.seed()
        demo.seed_history()
    stop.clear()
    thread = threading.Thread(target=poll_loop, daemon=True)
    thread.start()
    retention = threading.Thread(target=retention_loop, daemon=True)
    retention.start()
    yield
    stop.set()
    thread.join(timeout=1)
    retention.join(timeout=15)


app = FastAPI(title="Harbour", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth.router)
app.include_router(history.router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Do not echo submitted passwords/private keys in validation responses.
    return JSONResponse({'detail': [{k: error[k] for k in ('loc', 'msg', 'type')} for error in exc.errors()]}, status_code=422)


@app.middleware("http")
async def security(request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected = os.environ.get("HARBOUR_ORIGIN") or str(request.base_url).rstrip("/")
        origin = request.headers.get("origin")
        if origin and origin != expected:
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        if int(request.headers.get("content-length", "0")) > 100_000:
            return JSONResponse({"detail": "Request is too large"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if auth.secure_cookie():
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Credentials(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return {"demo": store.DEMO, "version": __version__}


def thresholds_for(server):
    global_ = json.loads(store.one("SELECT value FROM settings WHERE key='thresholds'")["value"])
    return {**store.DEFAULTS, **global_, **(json.loads(server["thresholds"]) if server["thresholds"] else {})}


def server_warnings(server, snapshot, threshold):
    services = snapshot.get("services", []) if server["server_type"] == "docker" else []
    warnings = []
    interval = server["poll_seconds"] or history.policy()["poll_seconds"]
    stale = not server["checked"] or time.time() - server["checked"] > max(120, interval*2 + 15)
    if server["error"]:
        warnings.append({"id": "connection", "title": "Server down" if server["connection_status"] == "down" else "Monitoring check failed", "detail": server["error"]})
    elif stale and server["monitoring_enabled"]:
        warnings.append({"id": "stale", "title": "Waiting for fresh data", "detail": "Readings are older than the configured monitoring window."})
    metrics = snapshot.get("metrics", {})
    if metrics:
        metrics["temperature"] = remote_probe.temperature_summary(metrics.get("temperature", {}).get("sensors", []))
        for key, value in (("cpu", metrics["cpu"]), ("memory", metrics["memory"]["percent"])):
            if value >= threshold[key]:
                warnings.append({"id": key, "title": key.capitalize() + " usage is high", "detail": f"{value:.1f}% used · threshold {threshold[key]}%"})
        metrics["disks"] = volumes.decorate(server, metrics["disks"], threshold)
        for disk in metrics["disks"]:
            if disk["warning"]:
                warnings.append({"id": "disk:" + disk["mount"], "title": "Disk space · " + disk["mount"], "detail": f"{disk['percent']:.1f}% used · {volumes.capacity(disk['free'])} free"})
        for sensor in metrics.get("temperature", {}).get("sensors", []):
            if sensor["kind"] != "cpu_auxiliary" and sensor["celsius"] >= threshold["temperature"]:
                warnings.append({"id": "temperature:" + sensor["id"], "title": "Temperature · " + sensor["label"],
                                 "kind": sensor["kind"],
                                 "detail": f"{sensor['celsius']:.1f}°C · threshold {threshold['temperature']}°C"})
    for s in services:
        # Docker can retain the last health result after a container stops.
        issue = s["state"] if s["state"] in {"restarting", "dead"} else (
            "unhealthy" if s["state"] == "running" and s.get("health") == "unhealthy" else None)
        if issue:
            warnings.append({"id": "service:" + s["id"], "title": s["name"] + " needs attention", "detail": issue})
    return warnings, stale


def warning_notification(warning):
    # Reading values can change while the same condition remains active.
    reason = warning['detail'] if warning['id'] == 'connection' or warning['id'].startswith('service:') else None
    digest = hashlib.sha256(json.dumps([warning['title'], reason]).encode()).hexdigest()
    return 'warning:' + warning['id'], digest


def clear_resolved_warning_dismissals(server_id, warnings=None):
    records = store.rows("SELECT user_id,service_id,digest FROM dismissals WHERE server_id=? AND service_id LIKE 'warning:%'", (server_id,))
    if not records:
        return
    if warnings is None:
        server = store.one('SELECT * FROM servers WHERE id=?', (server_id,))
        if not server:
            return
        warnings, _ = server_warnings(server, json.loads(server['snapshot']), thresholds_for(server))
    active = {warning_notification(w) for w in warnings}
    expired = [(r['user_id'], server_id, r['service_id'], r['digest']) for r in records if (r['service_id'], r['digest']) not in active]
    if expired:
        with store.db() as con:
            con.executemany('DELETE FROM dismissals WHERE user_id=? AND server_id=? AND service_id=? AND digest=?', expired)


def public_server(server, user):
    snapshot = json.loads(server["snapshot"])
    threshold = thresholds_for(server)
    dismissed = {(d["service_id"], d["digest"]) for d in store.rows("SELECT service_id,digest FROM dismissals WHERE user_id=? AND server_id=?", (user["id"], server["id"]))}
    services = snapshot.get("services", []) if server["server_type"] == "docker" else []
    snapshot["services"] = services
    warnings, stale = server_warnings(server, snapshot, threshold)
    interval = server["poll_seconds"] or history.policy()["poll_seconds"]
    clear_resolved_warning_dismissals(server['id'], warnings)
    for warning in warnings:
        warning['dismissed'] = warning_notification(warning) in dismissed
    for s in services:
        key = (s["project"] + "/" + s["name"]) if s["project"] else s["container"]
        s["notification_key"] = key
        s["dismissed"] = (key, s.get("update", {}).get("digest")) in dismissed
    return {"id": server["id"], "name": server["name"], "host": server["host"], "port": server["port"],
            "username": server["username"], "fingerprint": server["fingerprint"], "checked": server["checked"],
            "update_checked": server["update_checked"], "error": server["error"], "stale": stale,
            "thresholds": threshold, "override": bool(server["thresholds"]), "warnings": warnings,
            "updates": sum(s.get("update", {}).get("status") == "available" and not s["dismissed"] for s in services),
            **snapshot, "latency_ms": server["latency_ms"], "connection_status": server["connection_status"],
            "monitoring_enabled": bool(server["monitoring_enabled"]), "poll_seconds": interval,
            "poll_override": server["poll_seconds"], "last_attempt": server["last_attempt"], "server_type": server["server_type"]}


@app.get("/api/dashboard")
def dashboard(user=Depends(authenticated)):
    jobs = store.rows("SELECT id,server_id,server_name,actor,action,status,created,finished,progress,target_names,targets FROM jobs WHERE status IN ('queued','running') OR id IN (SELECT id FROM jobs ORDER BY created DESC,rowid DESC LIMIT 50) ORDER BY created DESC,rowid DESC")
    pending = {}
    for job in reversed(jobs):
        if job['status'] in {'queued', 'running'}:
            pending.setdefault(job['server_id'], []).append(job)
    for job in jobs:
        job['progress'] = json.loads(job['progress'])
        job['target_names'] = stored_target_names(job)
        queue_details(job, pending.get(job['server_id'], []))
    return {"servers": [public_server(s, user) for s in store.rows("SELECT * FROM servers ORDER BY sort_order,rowid")],
            "thresholds": {**store.DEFAULTS, **json.loads(store.one("SELECT value FROM settings WHERE key='thresholds'")["value"])}, "jobs": jobs}


class Thresholds(Input):
    cpu: float = Field(ge=1, le=100)
    memory: float = Field(ge=1, le=100)
    disk: float = Field(ge=1, le=100)
    disk_free_gb: float = Field(ge=0, le=1_000_000)
    temperature: float = Field(default=80, ge=1, le=180)


@app.put("/api/thresholds")
def global_thresholds(body: Thresholds, user=Depends(admin)):
    store.execute("UPDATE settings SET value=? WHERE key='thresholds'", (body.model_dump_json(),))
    return {"ok": True}


@app.put("/api/servers/{id_}/thresholds")
def server_thresholds(id_: str, body: Thresholds | None = None, user=Depends(admin)):
    get_server(id_)
    store.execute("UPDATE servers SET thresholds=? WHERE id=?", (body.model_dump_json() if body else None, id_))
    return {"ok": True}


class VolumeOption(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    mount: str = Field(min_length=1, max_length=4096)
    monitor: bool
    warn: bool
    card: bool


class VolumeInput(Input):
    volumes: list[VolumeOption] = Field(max_length=500)


def volume_config(server, values):
    known = {d['mount'] for d in json.loads(server['snapshot']).get('metrics', {}).get('disks', [])} | set(volumes.preferences(server))
    names = [v.mount for v in values]
    if len(names) != len(set(names)) or any(name not in known for name in names):
        raise HTTPException(400, 'Choose from the discovered volumes')
    config = volumes.preferences(server)
    for value in values:
        if not value.monitor and (value.warn or value.card):
            raise HTTPException(400, 'Enable monitoring before warnings or card display')
        config[value.mount] = value.model_dump(exclude={'mount'})
    return config


@app.put('/api/servers/{id_}/volumes')
def save_volumes(id_: str, body: VolumeInput, user=Depends(admin)):
    config = volume_config(get_server(id_), body.volumes)
    store.execute('UPDATE servers SET volume_settings=? WHERE id=?', (json.dumps(config), id_))
    return {'ok': True}


class ServerTypeInput(Input):
    server_type: Literal['docker', 'plain']


class ServerSettingsInput(ServerTypeInput):
    name: str = Field(min_length=1, max_length=80)
    thresholds: Thresholds | None
    volumes: list[VolumeOption] = Field(max_length=500)
    enabled: bool
    poll_seconds: int | None = Field(default=None, ge=15, le=3600)


@app.put('/api/servers/{id_}/settings')
def save_server_settings(id_: str, body: ServerSettingsInput, user=Depends(admin)):
    lock = server_lock(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Wait for the current server operation to finish, then save again')
    try:
        server = get_server(id_)
        config = volume_config(server, body.volumes)
        # One transaction prevents a rejected setting from leaving a partial save.
        with store.db() as con:
            con.execute('UPDATE servers SET name=?,thresholds=?,volume_settings=?,monitoring_enabled=?,poll_seconds=?,server_type=? WHERE id=?',
                        (body.name, body.thresholds.model_dump_json() if body.thresholds else None,
                         json.dumps(config), int(body.enabled), body.poll_seconds, body.server_type, id_))
            if server['server_type'] != body.server_type:
                snapshot = json.loads(server['snapshot'])
                snapshot['services'] = []
                snapshot.get('metrics', {})['docker'] = None
                con.execute("UPDATE servers SET snapshot=?,error=NULL,checked=NULL,last_attempt=NULL,update_checked=0,connection_status='pending' WHERE id=?",
                            (json.dumps(snapshot), id_))
    finally:
        lock.release()
    return {'ok': True, 'name': body.name}


@app.put('/api/servers/{id_}/type')
def save_server_type(id_: str, body: ServerTypeInput, user=Depends(admin)):
    lock = server_lock(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Wait for the current server operation to finish')
    try:
        server = get_server(id_)
        if server['server_type'] != body.server_type:
            snapshot = json.loads(server['snapshot'])
            snapshot['services'] = []
            snapshot.get('metrics', {})['docker'] = None
            store.execute("UPDATE servers SET server_type=?,snapshot=?,error=NULL,checked=NULL,last_attempt=NULL,update_checked=0,connection_status='pending' WHERE id=?",
                          (body.server_type, json.dumps(snapshot), id_))
    finally:
        lock.release()
    return {'ok': True}


class ServerOrderInput(Input):
    ids: list[str] = Field(max_length=10000)


@app.put('/api/server-order')
def save_server_order(body: ServerOrderInput, user=Depends(admin)):
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        existing = {r[0] for r in con.execute('SELECT id FROM servers')}
        if len(body.ids) != len(set(body.ids)) or set(body.ids) != existing:
            raise HTTPException(409, 'The server list changed. Refresh and try again.')
        con.executemany('UPDATE servers SET sort_order=? WHERE id=?', enumerate(body.ids))
    return {'ok': True}


class KeyInput(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    private_key: str | None = Field(default=None, max_length=20000)
    passphrase: str | None = Field(default=None, max_length=256)
    algorithm: Literal["ed25519", "ecdsa", "rsa"] = "ed25519"
    tier: Literal["normal", "high", "xhigh", "excessive"] = "normal"


KEY_SIZES = {"ed25519": {"normal": 256}, "ecdsa": {"normal": 256, "high": 384, "xhigh": 521},
             "rsa": {"normal": 3072, "high": 4096, "xhigh": 6144, "excessive": 8192}}


@app.get("/api/key-options")
def key_options(user=Depends(admin)):
    return {"sizes": KEY_SIZES, "recommended": "ed25519"}


@app.post("/api/keys")
def create_key(body: KeyInput, user=Depends(admin)):
    if store.DEMO:
        raise HTTPException(400, "Keys are disabled in demo mode. Start a production instance to onboard servers.")
    if body.private_key:
        try:
            key = ssh.parse_key(body.private_key, body.passphrase)
            private = body.private_key
            public = key.get_name() + " " + key.get_base64()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    else:
        bits = KEY_SIZES[body.algorithm].get(body.tier)
        if bits is None:
            raise HTTPException(400, "This algorithm does not support that size tier")
        if body.algorithm == "ed25519":
            key = Ed25519PrivateKey.generate()
        elif body.algorithm == "ecdsa":
            key = ec.generate_private_key({256: ec.SECP256R1, 384: ec.SECP384R1, 521: ec.SECP521R1}[bits]())
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
        public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    id_ = secrets.token_hex(12)
    encrypted = store.cipher().encrypt(json.dumps({"private_key": private, "passphrase": body.passphrase}).encode()).decode()
    store.execute("INSERT INTO ssh_keys VALUES (?,?,?)", (id_, encrypted, public))
    return {"id": id_, "public_key": public + " harbour"}


class RenameInput(Input):
    name: str = Field(min_length=1, max_length=80)


@app.patch("/api/servers/{id_}")
def rename_server(id_: str, body: RenameInput, user=Depends(admin)):
    get_server(id_)
    store.execute("UPDATE servers SET name=? WHERE id=?", (body.name, id_))
    return {"ok": True, "name": body.name}


class SSHHostInput(Input):
    host: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.:%_-]{0,252}$")
    port: int = Field(default=22, ge=1, le=65535)


fingerprint_probes = threading.BoundedSemaphore(4)


@app.post("/api/ssh/fingerprint")
def probe_fingerprint(body: SSHHostInput, user=Depends(admin)):
    if store.DEMO:
        raise HTTPException(400, "Live SSH fingerprint discovery is disabled in demo mode")
    if not fingerprint_probes.acquire(blocking=False):
        raise HTTPException(429, "Fingerprint checks are busy. Please try again shortly.")
    try:
        return ssh.probe_host_key(body.host, body.port)
    except Exception as exc:
        raise HTTPException(502, "Could not retrieve the SSH host fingerprint: " + str(exc)[:300]) from exc
    finally:
        fingerprint_probes.release()


class SSHLoginInput(SSHHostInput):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    username: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,63}$")
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")

    @field_validator('host', 'username', 'fingerprint', 'name', 'key_id', mode='before', check_fields=False)
    @classmethod
    def trim_labels(cls, value):
        # Passwords must retain significant leading/trailing whitespace.
        return value.strip() if isinstance(value, str) else value


class InstallKeyInput(SSHLoginInput):
    key_id: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)


key_installations = threading.BoundedSemaphore(4)


@app.post('/api/ssh/install-key')
def install_ssh_key(body: InstallKeyInput, request: Request, user=Depends(admin)):
    if store.DEMO:
        raise HTTPException(400, 'Key installation is disabled in demo mode')
    if not store.one('SELECT 1 FROM ssh_keys WHERE id=?', (body.key_id,)):
        raise HTTPException(400, 'Generate or import a key first')
    if not key_installations.acquire(blocking=False):
        raise HTTPException(429, 'Key installations are busy. Please try again shortly.')
    target = f'{body.username}@{body.host}:{body.port}'
    try:
        result = ssh.install_key(body.model_dump(exclude={'password', 'key_id'}), body.key_id, body.password.get_secret_value())
        auth.log(user['name'], auth.client_ip(request), 'ssh_key_installed', target)
        return result
    except ssh.KeyInstallError as exc:
        auth.log(user['name'], auth.client_ip(request), 'ssh_key_install_failed', target)
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(400, 'Key installation failed. Check the SSH connection and try again.') from None
    finally:
        key_installations.release()


class ServerInput(SSHLoginInput):
    name: str = Field(min_length=1, max_length=80)
    auth_method: Literal['key', 'password'] = 'key'
    key_id: str | None = Field(default=None, min_length=1, max_length=64)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)
    password_auth_confirmed: bool = False
    server_type: Literal['docker', 'plain'] = 'docker'


def connection_credential(body, existing=None):
    if body.auth_method == 'key':
        if body.password is not None:
            raise HTTPException(400, 'Use the separate key-installation button for a one-time password')
        if not body.key_id or not store.one('SELECT 1 FROM ssh_keys WHERE id=?', (body.key_id,)):
            raise HTTPException(400, 'Generate or import a key first')
        return body.key_id, None
    if not body.password_auth_confirmed:
        raise HTTPException(400, 'Acknowledge the password-login warning first')
    if body.password is not None:
        return None, store.cipher().encrypt(body.password.get_secret_value().encode()).decode()
    if (existing and existing['auth_method'] == 'password' and existing['password_encrypted']
            and all(existing[k] == getattr(body, k) for k in ('host', 'port', 'username'))):
        return None, existing['password_encrypted']
    raise HTTPException(400, 'Enter the SSH password for password login')


@app.post("/api/servers")
def add_server(body: ServerInput, user=Depends(admin)):
    if store.DEMO:
        raise HTTPException(400, "Live servers cannot be connected in demo mode")
    key_id, encrypted_password = connection_credential(body)
    id_ = secrets.token_hex(12)
    store.execute("INSERT INTO servers (id,name,host,port,username,fingerprint,key_id,auth_method,password_encrypted,server_type,sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,(SELECT COALESCE(MAX(sort_order),0)+1 FROM servers))",
                  (id_, body.name, body.host, body.port, body.username, body.fingerprint, key_id, body.auth_method, encrypted_password, body.server_type))
    job = queue_job(id_, "refresh", [], user)
    return {"id": id_, "job": job}


@app.get('/api/servers/{id_}/connection')
def read_connection(id_: str, user=Depends(admin)):
    server = get_server(id_)
    key = store.one('SELECT id,public FROM ssh_keys WHERE id=?', (server['key_id'],))
    return {**{k: server[k] for k in ('id', 'name', 'host', 'port', 'username', 'fingerprint', 'auth_method', 'server_type')},
            'has_password': bool(server['password_encrypted']),
            'key': {'id': key['id'], 'public_key': key['public'] + ' harbour'} if key else None}


@app.put('/api/servers/{id_}/connection')
def update_connection(id_: str, body: ServerInput, request: Request, user=Depends(admin)):
    if store.DEMO:
        raise HTTPException(400, 'Live SSH connection changes are disabled in demo mode')
    lock = server_lock(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Wait for the current server operation to finish before changing its connection')
    try:
        server = get_server(id_)
        key_id, encrypted_password = connection_credential(body, server)
        with store.db() as con:
            con.execute("UPDATE servers SET name=?,host=?,port=?,username=?,fingerprint=?,key_id=?,auth_method=?,password_encrypted=?,error=NULL,checked=NULL,connection_status='pending',last_attempt=NULL,latency_ms=NULL,update_checked=0 WHERE id=?",
                        (body.name, body.host, body.port, body.username, body.fingerprint, key_id, body.auth_method, encrypted_password, id_))
            if server['key_id'] and server['key_id'] != key_id:
                con.execute('DELETE FROM ssh_keys WHERE id=? AND NOT EXISTS (SELECT 1 FROM servers WHERE key_id=?)', (server['key_id'], server['key_id']))
        auth.log(user['name'], auth.client_ip(request), 'ssh_connection_changed', f'{body.username}@{body.host}:{body.port} · {body.auth_method}')
    finally:
        lock.release()
    try:
        job = queue_job(id_, 'refresh', [], user)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        job = None  # Background monitoring may already be checking the saved connection.
    return {'id': id_, 'job': job}


@app.delete("/api/servers/{id_}")
def delete_server(id_: str, user=Depends(admin)):
    server = get_server(id_)
    lock = server_lock(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Wait for the current server operation to finish")
    try:
        store.execute("DELETE FROM servers WHERE id=?", (id_,))
        if server["key_id"]:
            store.execute("DELETE FROM ssh_keys WHERE id=? AND NOT EXISTS (SELECT 1 FROM servers WHERE key_id=?)", (server["key_id"], server["key_id"]))
    finally:
        lock.release()
    return {"ok": True}


class ActionInput(Input):
    action: Literal["pull", "up", "restart", "pull_up", "prune", "start", "stop"]
    targets: list[str] = Field(max_length=200)


def connection_signature(server):
    values = {k: server[k] for k in ('host', 'port', 'username', 'fingerprint', 'auth_method', 'key_id', 'password_encrypted', 'server_type')}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def build_plan(server, body):
    if server["server_type"] != "docker":
        raise HTTPException(400, "Docker operations are disabled for plain servers")
    # An operation can legitimately keep the latest snapshot older than two minutes.
    # Remote execution still rechecks the exact confirmed commands against live inventory.
    pending = store.one("SELECT 1 FROM jobs WHERE server_id=? AND status IN ('queued','running')", (server['id'],))
    if server["error"] or not server["checked"] or (time.time() - server["checked"] > 120 and not pending):
        raise HTTPException(409, "Refresh this server successfully before changing its services")
    try:
        return remote_probe.plan(json.loads(server["snapshot"]).get("services", []), body.action, body.targets)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/servers/{id_}/plan")
def action_plan(id_: str, body: ActionInput, user=Depends(admin)):
    server = get_server(id_)
    commands = build_plan(server, body)
    payload = {"server_id": id_, "actor": user["id"], "action": body.action, "targets": body.targets,
               "commands": commands, "connection": connection_signature(server), "expires": time.time() + 300, "nonce": secrets.token_hex(12)}
    token = store.cipher().encrypt(json.dumps(payload).encode()).decode()
    return {"token": token, "commands": [{"command": shlex.join(c["argv"]), "directory": c["cwd"], "label": c["label"]} for c in commands]}


class ExecuteInput(Input):
    token: str = Field(max_length=80000)


@app.post("/api/servers/{id_}/execute")
def execute_action(id_: str, body: ExecuteInput, user=Depends(admin)):
    try:
        payload = json.loads(store.cipher().decrypt(body.token.encode(), ttl=300))
    except Exception:
        raise HTTPException(400, "The preview expired; create a new one")
    if payload["actor"] != user["id"] or payload["server_id"] != id_ or payload["expires"] < time.time():
        raise HTTPException(403, "This preview is not valid for this account or server")
    if store.one("SELECT 1 FROM jobs WHERE id=?", (payload["nonce"],)):
        raise HTTPException(409, "This operation has already been submitted")
    server = get_server(id_)
    if payload.get('connection') != connection_signature(server):
        raise HTTPException(409, 'Server connection changed; create a new preview')
    commands = build_plan(server, ActionInput(action=payload["action"], targets=payload["targets"]))
    if commands != payload["commands"]:
        raise HTTPException(409, "Server details changed; create a new preview")
    return queue_job(id_, payload["action"], payload["targets"], user, commands, payload["nonce"], payload['connection'])


def target_names(server, action, targets):
    if action == 'prune':
        return ['Entire Docker host']
    if action in {'refresh', 'check'}:
        return ['Server resources' if action == 'refresh' else 'All container images']
    services = json.loads(server['snapshot']).get('services', []) if server else []
    names = []
    for target in targets:
        selected = [s for s in services if s['id'] == target or 'group:' + str(s.get('project')) == target]
        names.extend([(s['project'] + ' / ' if s.get('project') else '') + s.get('container', s['name']) for s in selected] or [target])
    return list(dict.fromkeys(names))


def stored_target_names(job):
    names = json.loads(job['target_names'])
    if names:
        return names
    server = store.one('SELECT snapshot FROM servers WHERE id=?', (job['server_id'],))
    return target_names(server, job['action'], json.loads(job['targets'] or '[]'))


def queue_job(id_, action, targets, user, commands=None, job_id=None, expected_connection=None):
    job_id = job_id or secrets.token_hex(12)
    with queue_guard:
        server = get_server(id_)
        if action != "refresh" and server["server_type"] != "docker":
            raise HTTPException(400, "Docker operations are disabled for plain servers")
        if expected_connection and expected_connection != connection_signature(server):
            raise HTTPException(409, 'Server connection changed; create a new preview')
        if store.one('SELECT 1 FROM jobs WHERE id=?', (job_id,)):
            raise HTTPException(409, 'This operation has already been submitted')
        if store.one("SELECT COUNT(*) AS n FROM jobs WHERE server_id=? AND status IN ('queued','running')", (id_,))['n'] >= 100:
            raise HTTPException(429, 'This server already has 100 pending tasks')
        store.execute("INSERT INTO jobs (id,server_id,server_name,actor,action,status,targets,created,target_names) VALUES (?,?,?,?,?,'queued',?,?,?)",
                      (job_id, id_, server["name"], user["name"], action, json.dumps(targets), time.time(), json.dumps(target_names(server, action, targets))))
        job_queues.setdefault(id_, deque()).append((server, action, targets, commands, job_id))
        dispatch_queued(id_)
    return {"id": job_id}


def dispatch_queued(id_):
    """Reserve one host per worker; waiting tasks never consume worker threads."""
    with queue_guard:
        pending = job_queues.get(id_)
        if not pending:
            return
        lock = server_lock(id_)
        if not lock.acquire(blocking=False):
            return
        task = pending.popleft()
        if not pending:
            job_queues.pop(id_, None)
        try:
            pool.submit(work_job, *task, lock)
        except Exception:
            lock.release()
            store.execute("UPDATE jobs SET status='failed',finished=?,output='Unable to start task worker' WHERE id=?", (time.time(), task[4]))
            raise


def queue_details(job, pending=None):
    if job['status'] == 'queued':
        if pending is None:
            pending = store.rows("SELECT id,action,status FROM jobs WHERE server_id=? AND status IN ('queued','running') ORDER BY created,rowid", (job['server_id'],))
        waiting = [item for item in pending if item['status'] == 'queued']
        job['queue_position'] = next((i+1 for i,item in enumerate(waiting) if item['id'] == job['id']), 1)
        running = next((item for item in pending if item['status'] == 'running'), None)
        job['waiting_for'] = running['action'] if running else 'server availability'
    return job


def work_job(server, action, targets, commands, job_id, lock):
    progress = {'completed': 0, 'total': len(commands or []), 'label': 'Connecting to server', 'started': time.time(), 'phase': 'connecting', 'phase_started': time.time()}
    live_output, last_saved = '', 0
    refresh_details = False

    def event_update(event):
        nonlocal live_output, last_saved
        kind = event.get('kind')
        if kind == 'phase':
            progress.update(phase=event['phase'], label=event['label'], phase_started=time.time())
        elif kind == 'step':
            progress.update({key: event[key] for key in ('completed', 'total', 'label')})
            progress.update(phase='executing', phase_started=time.time())
            live_output = (live_output + '\n' + event['label'] + '\n')[-60000:]
        elif kind == 'completed':
            progress.update(completed=event['completed'], total=event['total'])
        elif kind == 'output':
            progress['last_output'] = time.time()
            text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(event.get('text', ''))).replace('\r', '\n')
            live_output = (live_output + text)[-60000:]
        elif kind != 'heartbeat':
            return
        progress['heartbeat'] = time.time()
        now = time.monotonic()
        if kind in {'phase', 'step', 'completed'} or now-last_saved >= .5:
            store.execute('UPDATE jobs SET progress=?,output=? WHERE id=?', (json.dumps(progress), live_output, job_id))
            last_saved = now

    try:
        target_refs = {s['id']: {'project': s['project'], 'name': s['name']} for s in json.loads(server['snapshot']).get('services', []) if s['id'] in targets and s.get('project')}
        store.execute("UPDATE jobs SET status='running',progress=? WHERE id=?", (json.dumps(progress), job_id))
        latest = get_server(server['id'])
        if connection_signature(latest) != connection_signature(server):
            raise RuntimeError('Server connection or type changed while queued; create a new preview')
        server = latest
        output = ""
        status = "succeeded"
        if action in {"refresh", "check"}:
            refresh_server(server["id"], action == "check")
            output = "Demo readings refreshed." if store.DEMO else "Readings refreshed."
        elif store.DEMO:
            for index, command in enumerate(commands or []):
                event_update({'kind': 'step', 'completed': index, 'total': len(commands), 'label': command['label']})
                event_update({'kind': 'output', 'text': 'Simulating ' + shlex.join(command['argv']) + '\n'})
                time.sleep(.6)
                event_update({'kind': 'completed', 'completed': index+1, 'total': len(commands)})
            snapshot = json.loads(get_server(server["id"])["snapshot"])
            selected = {s["id"] for s in snapshot["services"] if s["id"] in targets or "group:" + str(s["project"]) in targets}
            for s in snapshot["services"]:
                if s["id"] in selected:
                    if action in {"pull", "pull_up"}:
                        s["pulled"] = True
                    if action in {"up", "pull_up"} and s.get("pulled"):
                        s["image_id"] = s["update"].get("digest", s["image_id"])
                        s['version'] = s['update'].get('version') or s.get('version')
                        s["update"]["status"] = "current"
                    if action in {"up", "pull_up", "restart", "start"}:
                        s["state"] = "running"
                    if action == 'stop':
                        s['state'] = 'exited'
            store.execute("UPDATE servers SET snapshot=?,checked=? WHERE id=?", (json.dumps(snapshot), time.time(), server["id"]))
            output = live_output + "\nSimulated operation completed. No real host was contacted."
        else:
            refresh_details = True
            result = ssh.request(server, {"operation": "execute", "action": action, "targets": targets, "target_refs": target_refs, "expected": commands}, on_event=event_update)
            output, status = result["output"], "succeeded" if result["ok"] else "failed"
        progress['label'] = 'Completed' if status == 'succeeded' else 'Failed'
        store.execute("UPDATE jobs SET status=?,output=?,finished=?,progress=? WHERE id=?", (status, output[-60000:], time.time(), json.dumps(progress), job_id))
    except Exception as exc:
        progress['label'] = 'Failed'
        store.execute("UPDATE jobs SET status='failed',output=?,finished=?,progress=? WHERE id=?", ((live_output+'\n'+str(exc))[-60000:], time.time(), json.dumps(progress), job_id))
    finally:
        if refresh_details:
            with queue_guard:
                detail_refreshes.add(server['id'])
        lock.release()
        dispatch_queued(server['id'])


@app.post("/api/servers/{id_}/refresh")
def refresh(id_: str, user=Depends(admin)):
    return queue_job(id_, "refresh", [], user)


@app.post('/api/servers/refresh-all')
def refresh_all(user=Depends(admin)):
    jobs, errors = [], []
    for server in store.rows('SELECT id,name FROM servers ORDER BY sort_order,rowid'):
        try:
            jobs.append({**queue_job(server['id'], 'refresh', [], user), 'server_id': server['id']})
        except HTTPException as exc:
            errors.append({'id': server['id'], 'name': server['name'], 'detail': exc.detail})
    return {'jobs': jobs, 'errors': errors}


@app.post("/api/servers/{id_}/check-updates")
def check_updates(id_: str, user=Depends(admin)):
    return queue_job(id_, "check", [], user)


@app.get("/api/jobs/{id_}")
def job_detail(id_: str, user=Depends(admin)):
    job = store.one("SELECT * FROM jobs WHERE id=?", (id_,))
    if not job:
        raise HTTPException(404)
    job['progress'] = json.loads(job['progress'])
    job['target_names'] = stored_target_names(job)
    return queue_details(job)


class DismissInput(Input):
    server_id: str
    service_id: str


@app.post("/api/dismiss")
def dismiss(body: DismissInput, user=Depends(authenticated)):
    server = public_server(get_server(body.server_id), user)
    service = next((s for s in server.get("services", []) if s["id"] == body.service_id), None)
    if not service or service.get("update", {}).get("status") != "available":
        raise HTTPException(400, "This update is no longer available")
    store.execute("INSERT OR IGNORE INTO dismissals VALUES (?,?,?,?)", (user["id"], body.server_id, service["notification_key"], service["update"]["digest"]))
    return {"ok": True}


@app.delete("/api/dismissals")
def restore_dismissals(user=Depends(authenticated)):
    store.execute("DELETE FROM dismissals WHERE user_id=?", (user["id"],))
    return {"ok": True}


@app.post("/api/dismiss-all")
def dismiss_all(user=Depends(authenticated)):
    records = []
    for row in store.rows('SELECT * FROM servers'):
        server = public_server(row, user)
        for warning in server['warnings']:
            if not warning['dismissed']:
                key, digest = warning_notification(warning)
                records.append((user['id'], server['id'], key, digest))
        for service in server.get('services', []):
            update = service.get('update', {})
            if update.get('status') == 'available' and update.get('digest') and not service['dismissed']:
                records.append((user['id'], server['id'], service['notification_key'], update['digest']))
    with store.db() as con:
        con.executemany('INSERT OR IGNORE INTO dismissals VALUES (?,?,?,?)', records)
    return {"ok": True, "dismissed": len(records)}


@app.get("/api/users")
def users(user=Depends(admin)):
    return store.rows("SELECT id,name,role,(totp_secret IS NOT NULL) AS mfa_enabled FROM users ORDER BY name")


class UserInput(Credentials):
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "user"]


@app.post("/api/users")
def add_user(body: UserInput, user=Depends(admin)):
    if store.one("SELECT 1 FROM users WHERE name=?", (body.name,)):
        raise HTTPException(409, "This username is already in use")
    id_ = secrets.token_hex(12)
    store.execute("INSERT INTO users (id,name,password,role) VALUES (?,?,?,?)", (id_, body.name, store.password_hash(body.password), body.role))
    return {"id": id_}


@app.delete("/api/users/{id_}")
def delete_user(id_: str, user=Depends(admin)):
    if id_ == user["id"]:
        raise HTTPException(400, "You cannot remove your own account")
    store.execute("DELETE FROM users WHERE id=?", (id_,))
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
