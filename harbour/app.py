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

from . import __version__, auth, cpu, demo, history, notifications, polling, remote_probe, resources, ssh, store, volumes
from .auth import authenticated, admin

pool = ThreadPoolExecutor(max_workers=4)
resource_workers = None
inventory_attempts = {}
scheduler_guard = threading.RLock()
scheduler_wake = polling.changed
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
            service['update'] = remote_probe.classify_update(service, update) if update else service['update']
        elif update.get('status') in {'available', 'current', 'unverified'} and remote_probe.image_matches_update(service, update):
            # The checked platform image is now running, on either image store.
            service['update'] = remote_probe.classify_update(service, {**update, 'version': service.get('version')})


def resource_lock(id_):
    with locks_guard:
        return resource_locks.setdefault(id_, threading.Lock())


def refresh_server(id_, updates=False):
    # Manual refresh shares the same resource lock as the dedicated worker.
    server = get_server(id_)
    lock = resource_lock(id_)
    lock.acquire()
    poll_resources(server, lock, raise_errors=True)
    refresh_inventory(id_, updates)


def refresh_inventory(id_, updates=False):
    """Merge Docker inventory without overwriting independently sampled resources."""
    server = get_server(id_)
    try:
        if server['server_type'] == 'plain':
            result = {'services': [], 'docker': None}
        elif store.DEMO:
            result = json.loads(server['snapshot'])
            result['docker'] = result.get('metrics', {}).get('docker')
            if updates:
                for service in result.get('services', []):
                    service['update']['checked'] = time.time()
        else:
            result = ssh.request(server, {'operation': 'snapshot', 'updates': updates,
                                         'server_type': server['server_type'], 'resources': False})
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM servers WHERE id=?', (id_,)).fetchone()
            if not row or connection_signature(dict(row)) != connection_signature(server):
                return
            snapshot = json.loads(row['snapshot'])
            services = result.get('services', [])
            if not updates:
                preserve_update_checks(services, snapshot.get('services', []))
            snapshot['services'] = services
            snapshot['docker'] = result.get('docker')
            if snapshot.get('metrics'):
                snapshot['metrics']['docker'] = result.get('docker')
            snapshot.pop('docker_error', None)
            con.execute('UPDATE servers SET snapshot=?,update_checked=? WHERE id=?',
                        (json.dumps(snapshot), time.time() if updates else row['update_checked'], id_))
    except Exception as exc:
        # A Docker/registry failure must not mark a responding host down or insert
        # a missing resource sample while its dedicated worker is healthy.
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM servers WHERE id=?', (id_,)).fetchone()
            if row and connection_signature(dict(row)) == connection_signature(server):
                snapshot = json.loads(row['snapshot'])
                snapshot['docker_error'] = str(exc)[:1000]
                con.execute('UPDATE servers SET snapshot=? WHERE id=?', (json.dumps(snapshot), id_))
        raise
    clear_resolved_warning_dismissals(id_)


def poll_one(server, lock):
    try:
        refresh_inventory(server['id'], not server.get('_details_only') and time.time() - server['update_checked'] > history.policy()['update_check_hours']*3600)
    except Exception:
        pass  # Persisted on the server and surfaced in the dashboard.
    finally:
        with scheduler_guard:
            inventory_attempts[server['id']] = time.time()
        lock.release()
        dispatch_queued(server['id'])


def poll_resources(server, lock, cancel=None, background=False, raise_errors=False, release_lock=True):
    """One resource reading; no Docker calls and no shared network worker pool."""
    id_ = server['id']
    def valid(row):
        return row and connection_signature(dict(row)) == connection_signature(server) and (not background or row['monitoring_enabled']) and not (cancel and cancel.is_set())
    try:
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM servers WHERE id=?', (id_,)).fetchone()
            if not valid(row):
                return
            con.execute('UPDATE servers SET last_attempt=? WHERE id=?', (time.time(), id_))
        if store.DEMO:
            result = json.loads(server['snapshot'])
            result['latency_ms'] = server['latency_ms']
            if result.get('metrics'):
                result['metrics']['timezone'] = demo.demo_timezone(id_)
        else:
            result = ssh.request(server, {'operation': 'resources'}, **({'cancel': cancel} if cancel else {}))
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM servers WHERE id=?', (id_,)).fetchone()
            if not valid(row):
                return
            snapshot = json.loads(row['snapshot'])
            metrics = result['metrics']
            if 'cpu_counters' in metrics:
                counters = metrics.pop('cpu_counters')
                baseline = snapshot.get('_cpu_baseline', {})
                signature = connection_signature(dict(row))
                previous = baseline.get('counters') if baseline.get('connection') == signature else None
                metrics['cpu'], metrics['cpu_sample_seconds'] = cpu.usage(previous, counters)
                snapshot['_cpu_baseline'] = {'connection': signature, 'counters': counters if cpu.valid(counters) else None}
            else:
                # Synthetic/legacy percentage readings cannot seed a counter interval.
                snapshot.pop('_cpu_baseline', None)
            metrics['docker'] = snapshot.get('docker', snapshot.get('metrics', {}).get('docker')) if row['server_type'] == 'docker' else None
            con.execute('UPDATE servers SET resource_catalog=? WHERE id=?',
                        (json.dumps(resources.catalog(dict(row), metrics)), id_))
            snapshot['metrics'] = metrics
            if row['server_type'] == 'plain':
                snapshot['services'] = []
            snapshot['latency_ms'] = result.get('latency_ms')
            snapshot['history'] = (snapshot.get('history', []) + [metrics['cpu']])[-48:]
            now = time.time()
            con.execute("UPDATE servers SET snapshot=?,error=NULL,checked=?,latency_ms=?,connection_status='up' WHERE id=?",
                        (json.dumps(snapshot), now, result.get('latency_ms'), id_))
            history.record(id_, metrics, result.get('latency_ms'), up=True, now=now, connection=con)
        clear_resolved_warning_dismissals(id_)
        observe_notifications(id_)
    except ssh.Cancelled:
        pass
    except Exception as exc:
        status, latency = getattr(exc, 'status', 'unknown'), getattr(exc, 'latency_ms', None)
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM servers WHERE id=?', (id_,)).fetchone()
            if not valid(row):
                return
            con.execute('UPDATE servers SET error=?,connection_status=?,latency_ms=? WHERE id=?', (str(exc)[:1000], status, latency, id_))
            history.record(id_, latency_ms=latency, up=status == 'up', connection=con)
        observe_notifications(id_, successful=False)
        if raise_errors:
            raise
    finally:
        if release_lock:
            lock.release()


def run_resource_worker(id_, cancel):
    lock = resource_lock(id_)
    if not lock.acquire(blocking=False):
        return  # A manual refresh is already collecting this host's reading.
    try:
        server = store.one('SELECT * FROM servers WHERE id=?', (id_,))
        if server:
            poll_resources(server, lock, cancel=cancel, background=True, release_lock=False)
    finally:
        lock.release()


def poll_due():
    global resource_workers
    with scheduler_guard:
        if stop.is_set():
            return
        if resource_workers is None:
            resource_workers = polling.ServerWorkers(run_resource_worker)
        servers = store.rows('SELECT * FROM servers')
        interval = history.policy()['poll_seconds']
        resource_workers.sync({s['id']: (connection_signature(s), bool(s['monitoring_enabled']), s['poll_seconds'] or interval) for s in servers})
        for id_ in inventory_attempts.keys() - {s['id'] for s in servers}:
            inventory_attempts.pop(id_, None)
        for server in servers:
            due = time.time() - (server['last_attempt'] or 0) >= (server['poll_seconds'] or interval)
            if server['monitoring_enabled'] and due:
                resource_workers.request(server['id'])
    # Never hold the scheduler lock while dispatching Docker jobs: their completion
    # path also updates scheduling state before releasing the per-server lock.
    with queue_guard:
        for id_ in list(job_queues):
            dispatch_queued(id_)
    for server in servers:
        with queue_guard:
            details_due = server['id'] in detail_refreshes
        with scheduler_guard:
            last = inventory_attempts.get(server['id'], server['last_attempt'] or 0)
        inventory_due = (server['server_type'] == 'docker' and server['monitoring_enabled']
                         and time.time() - last >= (server['poll_seconds'] or interval))
        if inventory_due or details_due:
            lock = server_lock(server['id'])
            if not lock.acquire(blocking=False):
                continue
            with queue_guard:
                server['_details_only'] = server['id'] in detail_refreshes
                detail_refreshes.discard(server['id'])
            with scheduler_guard:
                inventory_attempts[server['id']] = time.time()
            try:
                pool.submit(poll_one, server, lock)
            except Exception:
                if server['_details_only']:
                    with queue_guard:
                        detail_refreshes.add(server['id'])
                lock.release()
                raise


def poll_loop():
    while not stop.is_set():
        scheduler_wake.wait(5)
        scheduler_wake.clear()
        if stop.is_set():
            break
        try:
            poll_due()
        except Exception:
            import logging
            logging.exception('Monitoring scheduler failed; retrying on the next pass')


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
    global resource_workers
    auth.trusted_proxies()
    store.initialize()
    with queue_guard:
        detail_refreshes.clear()
    if store.DEMO:
        demo.seed()
        demo.seed_history()
    stop.clear()
    scheduler_wake.clear()
    with scheduler_guard:
        inventory_attempts.clear()
        resource_workers = polling.ServerWorkers(run_resource_worker)
    thread = threading.Thread(target=poll_loop, daemon=True)
    thread.start()
    retention = threading.Thread(target=retention_loop, daemon=True)
    retention.start()
    telegram = threading.Thread(target=notifications.delivery_loop, args=(stop,), daemon=True)
    telegram.start()
    try:
        yield
    finally:
        stop.set()
        scheduler_wake.set()
        thread.join()
        resource_workers.close()
        resource_workers = None
        retention.join(timeout=15)
        telegram.join(timeout=15)


app = FastAPI(title="Harbour", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth.router)
app.include_router(history.router)
app.include_router(notifications.router)


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
    if server["server_type"] == "docker" and snapshot.get("docker_error") and not server['error']:
        warnings.append({"id": "docker", "title": "Docker inventory check failed", "detail": snapshot["docker_error"]})
    interval = server["poll_seconds"] or history.policy()["poll_seconds"]
    stale = not server["checked"] or time.time() - server["checked"] > max(120, interval*2 + 15)
    if server["error"]:
        warnings.append({"id": "connection", "title": "Server down" if server["connection_status"] == "down" else "Monitoring check failed", "detail": server["error"]})
    elif stale and server["monitoring_enabled"]:
        warnings.append({"id": "stale", "title": "Waiting for fresh data", "detail": "Readings are older than the configured monitoring window."})
    metrics = snapshot.get("metrics", {})
    if metrics:
        metrics["temperature"] = remote_probe.temperature_summary(metrics.get("temperature", {}).get("sensors", []))
        metrics['resources'] = resources.decorate(server, metrics, threshold)
        warnings.extend(resources.warnings(metrics['resources']))
        metrics["disks"] = volumes.decorate(server, metrics["disks"], threshold)
        for disk in metrics["disks"]:
            if disk["warning"]:
                warnings.append({"id": "disk:" + disk["mount"], "title": "Disk space · " + disk["mount"], "detail": f"{disk['percent']:.1f}% used · {volumes.capacity(disk['free'])} free"})
    for s in services:
        # Docker can retain the last health result after a container stops.
        issue = s["state"] if s["state"] in {"restarting", "dead"} else (
            "unhealthy" if s["state"] == "running" and s.get("health") == "unhealthy" else None)
        if issue:
            warnings.append({"id": "service:" + s["id"], "title": s["name"] + " needs attention", "detail": issue})
    return warnings, stale


def observe_notifications(server_id, successful=True):
    try:
        server = get_server(server_id)
        if successful and not server['checked']:
            return
        snapshot = json.loads(server['snapshot'])
        warnings = server_warnings(server, snapshot, thresholds_for(server))[0] if successful else []
        unavailable = resources.unavailable(server, snapshot.get('metrics', {}), thresholds_for(server))
        notifications.observe(server, warnings, successful, now=server['checked'] if successful else None,
                              unavailable=unavailable)
    except Exception:
        # Notification storage must never turn a successful resource poll into a failure.
        import logging
        logging.error('Could not record Telegram warning state for server %s', server_id)


def warning_notification(warning):
    # Reading values can change while the same condition remains active.
    reason = warning['detail'] if warning['id'] == 'connection' or warning['id'].startswith('service:') else None
    digest = hashlib.sha256(json.dumps([warning['title'], reason]).encode()).hexdigest()
    return 'warning:' + warning['id'], digest


def clear_resolved_warning_dismissals(server_id, warnings=None):
    records = store.rows("SELECT user_id,service_id,digest FROM dismissals WHERE server_id=? AND service_id LIKE 'warning:%'", (server_id,))
    if not records:
        return
    server = store.one('SELECT * FROM servers WHERE id=?', (server_id,))
    if not server:
        return
    snapshot = json.loads(server['snapshot'])
    if warnings is None:
        warnings, _ = server_warnings(server, snapshot, thresholds_for(server))
    active = {warning_notification(w) for w in warnings}
    unavailable = resources.unavailable(server, snapshot.get('metrics', {}), thresholds_for(server))
    expired = [(r['user_id'], server_id, r['service_id'], r['digest']) for r in records
               if (r['service_id'], r['digest']) not in active
               and r['service_id'].removeprefix('warning:') not in unavailable]
    if expired:
        with store.db() as con:
            con.executemany('DELETE FROM dismissals WHERE user_id=? AND server_id=? AND service_id=? AND digest=?', expired)


def public_server(server, user):
    snapshot = json.loads(server["snapshot"])
    snapshot.pop('_cpu_baseline', None)
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
        # Reclassify cached checks too, so an upgrade immediately clears old
        # same-version badges without waiting for the next registry interval.
        s['update'] = remote_probe.classify_update(s, s.get('update', {}))
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
            "poll_override": server["poll_seconds"], "last_attempt": server["last_attempt"], "server_type": server["server_type"],
            "card_layout": resources.card_layout(server)}


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


class ResourceOption(Input):
    id: str = Field(min_length=1, max_length=4096)
    monitor: bool
    warn: bool
    card: bool
    limit_mode: Literal['default', 'custom'] = 'default'
    low: float | None = Field(default=None, allow_inf_nan=False)
    high: float | None = Field(default=None, allow_inf_nan=False)


def resource_config(server, values, thresholds):
    config = resources.preferences(server)
    if values is None:
        return config
    known = resources.catalog(server)
    identities = [v.id for v in values]
    if len(identities) != len(set(identities)) or any(k not in known for k in identities):
        raise HTTPException(400, 'Choose from the discovered resources')
    for value in values:
        if not value.monitor and (value.warn or value.card):
            raise HTTPException(400, 'Enable monitoring before warnings or card display')
        data = value.model_dump(exclude={'id'})
        if value.limit_mode == 'default':
            data.update(low=None, high=None)
        effective = resources.options({**server, 'resource_settings': json.dumps({value.id: data})}, known[value.id], thresholds)
        if effective['low'] is not None and effective['high'] is not None and effective['low'] >= effective['high']:
            raise HTTPException(400, 'The low warning limit must be less than the high limit')
        if value.warn and effective['low'] is None and effective['high'] is None:
            raise HTTPException(400, 'Set at least one warning limit for ' + known[value.id]['label'])
        config[value.id] = data
    return config


class CardPositionInput(Input):
    x: float = Field(ge=0, le=1)
    row: int = Field(ge=0, le=4096, strict=True)


class CardLayoutInput(Input):
    default_size: Literal['small', 'medium', 'large'] | None = None
    sizes: dict[str, Literal['small', 'medium', 'large']] = Field(default_factory=dict, max_length=2004)
    order: list[str] | None = Field(default=None, max_length=2004)
    positions: dict[str, CardPositionInput] = Field(default_factory=dict, max_length=2004)
    reset_sizes: bool = False


def card_layout_config(server, value):
    layout = resources.card_layout(server)
    if value is None:
        return layout
    known = resources.card_ids(server)
    if (set(value.sizes) | set(value.positions)) - known or value.order is not None and (
            set(value.order) - known or len(set(value.order)) != len(value.order)):
        raise HTTPException(400, 'Choose from the discovered resource cards')
    if value.default_size is not None:
        layout['default_size'] = value.default_size
    layout['sizes'] = {**({} if value.reset_sizes else layout['sizes']), **value.sizes}
    layout['positions'] = {**layout['positions'], **{key: pos.model_dump() for key, pos in value.positions.items()}}
    if value.order is not None:
        # Retain hidden/missing identities when only visible cards are reordered.
        layout['order'] = value.order + [key for key in layout['order'] if key not in value.order]
    return layout


def changed_resource_warnings(server, settings, thresholds):
    old_thresholds = thresholds_for(server)
    return {identity for identity, resource in resources.catalog(server).items()
            if {k: v for k, v in resources.options(server, resource, old_thresholds).items() if k != 'card'} !=
               {k: v for k, v in resources.options(server, resource, thresholds, settings).items() if k != 'card'}}


def changed_volume_warnings(server, settings, thresholds):
    old = thresholds_for(server)
    limits_changed = any(old[key] != thresholds[key] for key in ('disk', 'disk_free_gb'))
    mounts = set(settings) | {d['mount'] for d in json.loads(server['snapshot']).get('metrics', {}).get('disks', [])}
    updated = {**server, 'volume_settings': json.dumps(settings)}
    return {'disk:' + mount for mount in mounts if limits_changed or any(
        volumes.options(server, mount)[key] != volumes.options(updated, mount)[key]
        for key in ('monitor', 'warn'))}


class CardPreferencesInput(Input):
    layout: CardLayoutInput | None = None
    resources: list[ResourceOption] | None = Field(default=None, max_length=2000)
    volumes: list[VolumeOption] = Field(default_factory=list, max_length=500)


@app.patch('/api/servers/{id_}/cards')
def save_card_preferences(id_: str, body: CardPreferencesInput, user=Depends(admin)):
    lock = server_lock(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Wait for the current server operation to finish, then save again')
    try:
        server = get_server(id_)
        layout = card_layout_config(server, body.layout)
        choices = resource_config(server, body.resources, thresholds_for(server))
        disks = volume_config(server, body.volumes)
        changed = changed_resource_warnings(server, choices, thresholds_for(server))
        changed |= changed_volume_warnings(server, disks, thresholds_for(server))
        with notifications.guard, store.db() as con:
            con.execute('UPDATE servers SET card_layout=?,resource_settings=?,volume_settings=? WHERE id=?',
                        (json.dumps(layout), json.dumps(choices), json.dumps(disks), id_))
            notifications.invalidate(con, id_, changed)
    finally:
        lock.release()
    return {'ok': True, 'card_layout': layout}


class ServerSettingsInput(ServerTypeInput):
    name: str = Field(min_length=1, max_length=80)
    thresholds: Thresholds | None
    volumes: list[VolumeOption] = Field(max_length=500)
    resources: list[ResourceOption] | None = Field(default=None, max_length=2000)
    card_layout: CardLayoutInput | None = None
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
        next_thresholds = thresholds_for({**server, 'thresholds': body.thresholds.model_dump_json() if body.thresholds else None})
        resource_settings = resource_config(server, body.resources, next_thresholds)
        layout = card_layout_config(server, body.card_layout)
        known = resources.catalog(server)
        changed = changed_resource_warnings(server, resource_settings, next_thresholds)
        changed |= changed_volume_warnings(server, config, next_thresholds)
        # One transaction prevents a rejected setting from leaving a partial save.
        with notifications.guard, store.db() as con:
            con.execute('UPDATE servers SET name=?,thresholds=?,volume_settings=?,resource_settings=?,resource_catalog=?,monitoring_enabled=?,poll_seconds=?,server_type=?,card_layout=? WHERE id=?',
                        (body.name, body.thresholds.model_dump_json() if body.thresholds else None,
                         json.dumps(config), json.dumps(resource_settings), json.dumps(known), int(body.enabled), body.poll_seconds, body.server_type, json.dumps(layout), id_))
            notifications.invalidate(con, id_, changed)
            if body.enabled and not server['monitoring_enabled']:
                con.execute('UPDATE servers SET last_attempt=NULL WHERE id=?', (id_,))
            if server['server_type'] != body.server_type:
                snapshot = json.loads(server['snapshot'])
                snapshot['services'] = []
                snapshot.get('metrics', {})['docker'] = None
                con.execute("UPDATE servers SET snapshot=?,error=NULL,checked=NULL,last_attempt=NULL,update_checked=0,connection_status='pending' WHERE id=?",
                            (json.dumps(snapshot), id_))
    finally:
        lock.release()
    scheduler_wake.set()
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
    scheduler_wake.set()
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
    username: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
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
    scheduler_wake.set()
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
    scheduler_wake.set()
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
    scheduler_wake.set()
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
