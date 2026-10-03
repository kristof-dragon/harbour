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
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, auth, demo, history, remote_probe, ssh, store
from .auth import authenticated, admin

pool = ThreadPoolExecutor(max_workers=4)
stop = threading.Event()
locks = {}
locks_guard = threading.Lock()
STATIC = Path(__file__).with_name("static")


def server_lock(id_):
    with locks_guard:
        return locks.setdefault(id_, threading.Lock())


def get_server(id_):
    server = store.one("SELECT * FROM servers WHERE id=?", (id_,))
    if not server:
        raise HTTPException(404, "Server not found")
    return server


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
            snapshot = ssh.request(server, {"operation": "snapshot", "updates": updates})
            if not updates:
                old = {s["id"]: s for s in previous.get("services", [])}
                for s in snapshot["services"]:
                    prior = old.get(s["id"], {})
                    if prior.get("image_id") == s["image_id"]:
                        s["update"] = prior["update"]
            snapshot["history"] = (previous.get("history", []) + [snapshot["metrics"]["cpu"]])[-48:]
        store.execute("UPDATE servers SET snapshot=?, error=NULL, checked=?, update_checked=?,latency_ms=?,connection_status='up' WHERE id=?",
                      (json.dumps(snapshot), time.time(), time.time() if updates else server["update_checked"], snapshot.get("latency_ms"), id_))
    except Exception as exc:
        status = getattr(exc, "status", "unknown")
        latency = getattr(exc, "latency_ms", None)
        store.execute("UPDATE servers SET error=?,connection_status=?,latency_ms=? WHERE id=?", (str(exc)[:1000], status, latency, id_))
        history.record(id_, latency_ms=latency, up=status == "up")
        raise
    history.record(id_, snapshot.get("metrics"), snapshot.get("latency_ms"), up=True)


def poll_one(server, lock):
    try:
        refresh_server(server["id"], time.time() - server["update_checked"] > history.policy()["update_check_hours"]*3600)
    except Exception:
        pass  # Persisted on the server and surfaced in the dashboard.
    finally:
        lock.release()


def poll_due():
    interval = history.policy()["poll_seconds"]
    for server in store.rows("SELECT * FROM servers WHERE monitoring_enabled=1"):
        if time.time() - (server["last_attempt"] or 0) >= (server["poll_seconds"] or interval):
            # Submit only once per host; don't grow an unbounded queue during slow registry calls.
            lock = server_lock(server["id"])
            if lock.acquire(blocking=False):
                try:
                    pool.submit(poll_one, server, lock)
                except Exception:
                    lock.release()
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


def public_server(server, user):
    snapshot = json.loads(server["snapshot"])
    threshold = thresholds_for(server)
    dismissed = {(d["service_id"], d["digest"]) for d in store.rows("SELECT service_id,digest FROM dismissals WHERE user_id=? AND server_id=?", (user["id"], server["id"]))}
    services = snapshot.get("services", [])
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
        for disk in metrics["disks"]:
            if disk["percent"] >= threshold["disk"] or disk["free"] / 1e9 <= threshold["disk_free_gb"]:
                warnings.append({"id": "disk:" + disk["mount"], "title": "Disk space · " + disk["mount"], "detail": f"{disk['percent']:.1f}% used · {disk['free'] / 1e9:.1f} GB free"})
        for sensor in metrics.get("temperature", {}).get("sensors", []):
            if sensor["kind"] != "cpu_auxiliary" and sensor["celsius"] >= threshold["temperature"]:
                warnings.append({"id": "temperature:" + sensor["id"], "title": "Temperature · " + sensor["label"],
                                 "kind": sensor["kind"],
                                 "detail": f"{sensor['celsius']:.1f}°C · threshold {threshold['temperature']}°C"})
    for s in services:
        key = (s["project"] + "/" + s["name"]) if s["project"] else s["container"]
        s["notification_key"] = key
        s["dismissed"] = (key, s.get("update", {}).get("digest")) in dismissed
        if s.get("health") == "unhealthy" or s["state"] in {"restarting", "dead"}:
            warnings.append({"id": "service:" + s["id"], "title": s["name"] + " needs attention", "detail": s.get("health") or s["state"]})
    return {"id": server["id"], "name": server["name"], "host": server["host"], "port": server["port"],
            "username": server["username"], "fingerprint": server["fingerprint"], "checked": server["checked"],
            "update_checked": server["update_checked"], "error": server["error"], "stale": stale,
            "thresholds": threshold, "override": bool(server["thresholds"]), "warnings": warnings,
            "updates": sum(s.get("update", {}).get("status") == "available" and not s["dismissed"] for s in services),
            **snapshot, "latency_ms": server["latency_ms"], "connection_status": server["connection_status"],
            "monitoring_enabled": bool(server["monitoring_enabled"]), "poll_seconds": interval,
            "poll_override": server["poll_seconds"], "last_attempt": server["last_attempt"]}


@app.get("/api/dashboard")
def dashboard(user=Depends(authenticated)):
    jobs = store.rows("SELECT id,server_id,server_name,actor,action,status,created,finished FROM jobs ORDER BY created DESC LIMIT 50")
    return {"servers": [public_server(s, user) for s in store.rows("SELECT * FROM servers ORDER BY rowid")],
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


class ServerInput(Input):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.:%_-]{0,252}$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,63}$")
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    key_id: str = Field(min_length=1, max_length=64)


@app.post("/api/servers")
def add_server(body: ServerInput, user=Depends(admin)):
    if store.DEMO:
        raise HTTPException(400, "Live servers cannot be connected in demo mode")
    if not store.one("SELECT 1 FROM ssh_keys WHERE id=?", (body.key_id,)):
        raise HTTPException(400, "Generate or import a key first")
    id_ = secrets.token_hex(12)
    store.execute("INSERT INTO servers (id,name,host,port,username,fingerprint,key_id) VALUES (?,?,?,?,?,?,?)",
                  (id_, body.name, body.host, body.port, body.username, body.fingerprint, body.key_id))
    job = queue_job(id_, "refresh", [], user)
    return {"id": id_, "job": job}


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
    action: Literal["pull", "up", "restart"]
    targets: list[str] = Field(min_length=1, max_length=200)


def build_plan(server, body):
    if server["error"] or not server["checked"] or time.time() - server["checked"] > 120:
        raise HTTPException(409, "Refresh this server successfully before changing its services")
    try:
        return remote_probe.plan(json.loads(server["snapshot"]).get("services", []), body.action, body.targets)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/servers/{id_}/plan")
def action_plan(id_: str, body: ActionInput, user=Depends(admin)):
    commands = build_plan(get_server(id_), body)
    payload = {"server_id": id_, "actor": user["id"], "action": body.action, "targets": body.targets,
               "commands": commands, "expires": time.time() + 300, "nonce": secrets.token_hex(12)}
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
    commands = build_plan(get_server(id_), ActionInput(action=payload["action"], targets=payload["targets"]))
    if commands != payload["commands"]:
        raise HTTPException(409, "Server details changed; create a new preview")
    return queue_job(id_, payload["action"], payload["targets"], user, commands, payload["nonce"])


def queue_job(id_, action, targets, user, commands=None, job_id=None):
    server = get_server(id_)
    lock = server_lock(id_)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "This server already has an operation in progress")
    job_id = job_id or secrets.token_hex(12)
    try:
        store.execute("INSERT INTO jobs (id,server_id,server_name,actor,action,status,targets,created) VALUES (?,?,?,?,?,'queued',?,?)",
                      (job_id, id_, server["name"], user["name"], action, json.dumps(targets), time.time()))
        pool.submit(work_job, server, action, targets, commands, job_id, lock)
    except Exception:
        lock.release()
        raise
    return {"id": job_id}


def work_job(server, action, targets, commands, job_id, lock):
    try:
        store.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        output = ""
        status = "succeeded"
        if action in {"refresh", "check"}:
            refresh_server(server["id"], action == "check")
            output = "Demo readings refreshed." if store.DEMO else "Readings refreshed."
        elif store.DEMO:
            time.sleep(0.6)
            snapshot = json.loads(get_server(server["id"])["snapshot"])
            selected = {s["id"] for s in snapshot["services"] if s["id"] in targets or "group:" + str(s["project"]) in targets}
            for s in snapshot["services"]:
                if s["id"] in selected:
                    if action == "pull":
                        s["pulled"] = True
                    if action == "up" and s.get("pulled"):
                        s["image_id"] = s["update"].get("digest", s["image_id"])
                        s["update"]["status"] = "current"
                    if action in {"up", "restart"}:
                        s["state"] = "running"
            store.execute("UPDATE servers SET snapshot=?,checked=? WHERE id=?", (json.dumps(snapshot), time.time(), server["id"]))
            output = "Simulated operation completed. No real host was contacted."
        else:
            result = ssh.request(server, {"operation": "execute", "action": action, "targets": targets, "expected": commands})
            output, status = result["output"], "succeeded" if result["ok"] else "failed"
            try:
                refresh_server(server["id"], action == "up")
            except Exception as exc:
                output += "\nFollow-up monitoring failed: " + str(exc)
        store.execute("UPDATE jobs SET status=?,output=?,finished=? WHERE id=?", (status, output[-60000:], time.time(), job_id))
    except Exception as exc:
        store.execute("UPDATE jobs SET status='failed',output=?,finished=? WHERE id=?", (str(exc)[-60000:], time.time(), job_id))
    finally:
        lock.release()


@app.post("/api/servers/{id_}/refresh")
def refresh(id_: str, user=Depends(admin)):
    return queue_job(id_, "refresh", [], user)


@app.post("/api/servers/{id_}/check-updates")
def check_updates(id_: str, user=Depends(admin)):
    return queue_job(id_, "check", [], user)


@app.get("/api/jobs/{id_}")
def job_detail(id_: str, user=Depends(admin)):
    job = store.one("SELECT * FROM jobs WHERE id=?", (id_,))
    if not job:
        raise HTTPException(404)
    return job


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
