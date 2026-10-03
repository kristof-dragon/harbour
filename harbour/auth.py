"""Local authentication, proxy-aware session binding and replay-safe TOTP."""
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import secrets
import threading
import time

import pyotp
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import store

router = APIRouter(prefix="/api")
auth_lock = threading.RLock()
DUMMY = store.password_hash("nonexistent-user-placeholder")


def settings():
    return json.loads(store.one("SELECT value FROM settings WHERE key='security'")["value"])


def secure_cookie():
    return os.environ.get("HARBOUR_SECURE_COOKIE", "false") == "true"


def cookie_name():
    return "__Host-harbour_session" if secure_cookie() else "harbour_session"


def trusted_proxies():
    networks = [ipaddress.ip_network(s.strip(), strict=False) for s in
                os.environ.get("HARBOUR_TRUSTED_PROXIES", "").split(",") if s.strip()]
    if any(n.prefixlen == 0 for n in networks):
        raise ValueError("Trust only Nginx Proxy Manager's exact address or dedicated subnet; /0 is forbidden.")
    return networks


def canonical_ip(value):
    ip = ipaddress.ip_address(value.strip())
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


def client_ip(request):
    peer = request.client.host if request.client else "unknown"
    networks = trusted_proxies()
    try:
        ip = canonical_ip(peer)
    except ValueError:
        return peer  # ASGI test clients can use a non-IP identifier.
    if not any(ip in network for network in networks):
        return str(ip)  # Never trust a direct client's forwarding headers.
    forwarded = request.headers.get("x-forwarded-for", "")
    try:
        if not forwarded or len(forwarded) > 1024:
            raise ValueError()
        chain = [canonical_ip(s) for s in forwarded.split(",")]
        if len(chain) > 20:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "The trusted proxy must supply a valid X-Forwarded-For header")
    # Walk from the socket towards the client; discard only explicitly trusted hops.
    while chain and any(ip in network for network in networks):
        ip = chain.pop()
    return str(ip)


def browser_id(request):
    return hashlib.sha256(request.headers.get("user-agent", "").encode()).hexdigest()


def token_hash(request):
    return hashlib.sha256(request.cookies.get(cookie_name(), "").encode()).hexdigest()


def log(username, address, outcome, detail=""):
    store.execute("INSERT INTO auth_log (username,address,created,outcome,detail) VALUES (?,?,?,?,?)",
                  (username[:80], address, time.time(), outcome, detail))
    store.execute("DELETE FROM auth_log WHERE created<?", (time.time() - 90 * 86400,))


def check_ban(address, username=None):
    ban = store.one("SELECT until FROM ip_bans WHERE address=? AND until>?", (address, time.time()))
    if ban:
        if username is not None:
            log(username, address, "blocked", "IP ban active")
        remaining = max(1, int(ban["until"] - time.time()))
        raise HTTPException(429, "This IP is temporarily banned after five failed authentication attempts. Try again in "
                            + str((remaining + 59) // 60) + " minutes.", headers={"Retry-After": str(remaining)})


def failed(username, address, detail="Invalid credentials", status_code=401):
    with store.db() as con:
        con.execute("INSERT INTO login_attempts VALUES (?,?)", (address, time.time()))
        con.execute("DELETE FROM login_attempts WHERE created<?", (time.time() - 900,))
        failures = con.execute("SELECT COUNT(*) FROM login_attempts WHERE address=?", (address,)).fetchone()[0]
        if failures >= 5:
            con.execute("INSERT OR REPLACE INTO ip_bans VALUES (?,?)", (address, time.time() + settings()["ban_minutes"] * 60))
    log(username, address, "failure_banned" if failures >= 5 else "failure", detail)
    if failures >= 5:
        check_ban(address)
    raise HTTPException(status_code, "Authentication failed. Check your credentials and code.")


def user_view(user, csrf):
    policy = settings()
    return {"id": user["id"], "name": user["name"], "role": user["role"], "csrf": csrf,
            "demo": store.DEMO, "mfa_enabled": bool(user.get("totp_secret")),
            "idle_minutes": policy["idle_minutes"], "absolute_hours": policy["absolute_hours"]}


def start_session(user, response, request, revoke_all=False):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    now, policy = time.time(), settings()
    with store.db() as con:
        con.execute("DELETE FROM sessions WHERE expires<?", (now,))
        con.execute("DELETE FROM sessions WHERE token=?", (token_hash(request),))
        if revoke_all:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        con.execute("INSERT INTO sessions (token,user_id,csrf,expires,created,last_activity,address,browser) VALUES (?,?,?,?,?,?,?,?)",
                    (hashlib.sha256(token.encode()).hexdigest(), user["id"], csrf, now + policy["absolute_hours"] * 3600,
                     now, now, client_ip(request), browser_id(request)))
    response.set_cookie(cookie_name(), token, httponly=True, secure=secure_cookie(), samesite="strict",
                        max_age=policy["absolute_hours"] * 3600, path="/")
    return user_view(user, csrf)


def authenticated(request: Request):
    address = client_ip(request)
    check_ban(address)
    token = token_hash(request)
    session = store.one("SELECT u.id,u.name,u.role,u.totp_secret,s.csrf,s.created,s.last_activity,s.expires,s.address,s.browser "
                        "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (token,))
    if not session:
        raise HTTPException(401, "Please sign in")
    policy, now = settings(), time.time()
    reason = None
    if min(session["expires"], session["created"] + policy["absolute_hours"] * 3600) <= now:
        reason = "Your session has reached its time limit. Please sign in again."
    elif session["last_activity"] + policy["idle_minutes"] * 60 <= now:
        reason = "Your session expired after inactivity. Please sign in again."
    elif policy["bind_ip"] and session["address"] != address:
        reason = "Your IP address changed. Please sign in again."
    elif policy["bind_browser"] and not hmac.compare_digest(session["browser"], browser_id(request)):
        reason = "Your browser identity changed. Please sign in again."
    if reason:
        store.execute("DELETE FROM sessions WHERE token=?", (token,))
        log(session["name"], address, "session_revoked", reason)
        raise HTTPException(401, reason)
    if request.method not in {"GET", "HEAD"}:
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
            raise HTTPException(403, "Invalid session token; reload the page")
        store.execute("UPDATE sessions SET last_activity=? WHERE token=?", (now, token))
    return {**user_view(session, session["csrf"]), "session_token": token}


def admin(user=Depends(authenticated)):
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


def consume_otp(user_id, code):
    """Consume a TOTP time step or recovery code under a database write lock."""
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        user = con.execute("SELECT totp_secret,totp_last FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not user["totp_secret"]:
            return False
        secret = store.cipher().decrypt(user["totp_secret"].encode()).decode()
        totp = pyotp.TOTP(secret)
        now = int(time.time() // 30)
        for step in (now, now - 1, now + 1):
            if step > user["totp_last"] and hmac.compare_digest(totp.at(step * 30), code.strip()):
                con.execute("UPDATE users SET totp_last=? WHERE id=?", (step, user_id))
                return True
        digest = hashlib.sha256(code.strip().replace("-", "").lower().encode()).hexdigest()
        return con.execute("DELETE FROM recovery_codes WHERE user_id=? AND hash=?", (user_id, digest)).rowcount == 1


def recovery_codes(user_id):
    codes = [secrets.token_hex(16) for _ in range(8)]
    with store.db() as con:
        con.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
        con.executemany("INSERT INTO recovery_codes VALUES (?,?)", [(user_id, hashlib.sha256(c.encode()).hexdigest()) for c in codes])
    return ["-".join(c[i:i + 8] for i in range(0, 32, 8)) for c in codes]


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(default="", max_length=80)


@router.post("/login")
def login(body: LoginInput, request: Request, response: Response):
    address = client_ip(request)
    with auth_lock:
        check_ban(address, body.name)
        if store.one("SELECT COUNT(*) AS n FROM auth_log WHERE address=? AND created>?", (address, time.time()-60))["n"] >= 30:
            raise HTTPException(429, "Too many authentication requests. Try again in one minute.", headers={"Retry-After": "60"})
        user = store.one("SELECT * FROM users WHERE name=?", (body.name,))
        valid = store.password_ok(body.password, user["password"] if user else DUMMY)
        if not user or not valid:
            failed(body.name, address)
        if user["totp_secret"]:
            if not body.code:
                log(body.name, address, "mfa_required", "Password verified; awaiting second factor")
                raise HTTPException(401, "Enter your authenticator code or a recovery code.")
            if not consume_otp(user["id"], body.code):
                failed(body.name, address, "Invalid or previously used second factor")
        store.execute("DELETE FROM login_attempts WHERE address=?", (address,))
        log(user["name"], address, "success", "2FA verified" if user["totp_secret"] else "Password verified")
        return start_session(user, response, request)


@router.post("/demo-login")
def demo_login(request: Request, response: Response):
    if not store.DEMO:
        raise HTTPException(404)
    check_ban(client_ip(request))
    user = store.one("SELECT * FROM users WHERE role='admin' LIMIT 1")
    log(user["name"], client_ip(request), "demo", "Simulated workspace access")
    return start_session(user, response, request)


@router.get("/me")
def me(user=Depends(authenticated)):
    return {k: v for k, v in user.items() if k != "session_token"}


@router.post("/logout")
def logout(response: Response, user=Depends(authenticated)):
    store.execute("DELETE FROM sessions WHERE token=?", (user["session_token"],))
    response.delete_cookie(cookie_name(), path="/", secure=secure_cookie(), httponly=True, samesite="strict")
    return {"ok": True}


@router.post("/session/activity")
def activity(user=Depends(authenticated)):
    return {"ok": True}


@router.get("/security/me")
def my_security(user=Depends(authenticated)):
    return {"mfa_enabled": user["mfa_enabled"],
            "recovery_remaining": store.one("SELECT COUNT(*) AS n FROM recovery_codes WHERE user_id=?", (user["id"],))["n"],
            "sessions": [{"current": s["token"] == user["session_token"], "address": s["address"], "created": s["created"],
                          "last_activity": s["last_activity"]} for s in store.rows("SELECT * FROM sessions WHERE user_id=? AND expires>?", (user["id"], time.time()))],
            **settings()}


@router.delete("/security/sessions")
def revoke_other_sessions(user=Depends(authenticated)):
    store.execute("DELETE FROM sessions WHERE user_id=? AND token!=?", (user["id"], user["session_token"]))
    return {"ok": True}


class ReauthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=256)
    code: str = Field(default="", max_length=80)


def reauthenticate(body, request, user):
    address = client_ip(request)
    check_ban(address)
    record = store.one("SELECT * FROM users WHERE id=?", (user["id"],))
    if not store.password_ok(body.password, record["password"]) or (record["totp_secret"] and not consume_otp(user["id"], body.code)):
        failed(user["name"], address, "Sensitive-action authentication failed", 400)
    return record


@router.post("/security/totp/begin")
def totp_begin(body: ReauthInput, request: Request, user=Depends(authenticated)):
    if store.DEMO:
        raise HTTPException(400, "2FA enrollment is available in the production instance; demo sign-in bypasses authentication.")
    with auth_lock:
        record = reauthenticate(body, request, user)
        if record["totp_secret"]:
            raise HTTPException(409, "2FA is already enabled")
        secret = pyotp.random_base32()
        encrypted = store.cipher().encrypt(secret.encode()).decode()
        store.execute("INSERT OR REPLACE INTO mfa_pending VALUES (?,?,?,?)", (user["id"], encrypted, time.time()+600, user["session_token"]))
        uri = pyotp.TOTP(secret).provisioning_uri(name=user["name"], issuer_name="Harbour")
        qr = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
        output = io.BytesIO()
        qr.save(output)
        return {"secret": secret, "qr": "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode()}


class CodeInput(BaseModel):
    code: str = Field(min_length=1, max_length=80)


@router.post("/security/totp/confirm")
def totp_confirm(body: CodeInput, request: Request, response: Response, user=Depends(authenticated)):
    with auth_lock:
        pending = store.one("SELECT * FROM mfa_pending WHERE user_id=? AND expires>? AND session_token=?",
                            (user["id"], time.time(), user["session_token"]))
        if not pending:
            raise HTTPException(400, "Enrollment expired. Start setup again.")
        secret = store.cipher().decrypt(pending["secret"].encode()).decode()
        now = int(time.time() // 30)
        step = next((n for n in (now, now-1, now+1) if hmac.compare_digest(pyotp.TOTP(secret).at(n*30), body.code.strip())), None)
        if step is None:
            failed(user["name"], client_ip(request), "2FA enrollment code failed", 400)
        store.execute("UPDATE users SET totp_secret=?,totp_last=? WHERE id=?", (pending["secret"], step, user["id"]))
        store.execute("DELETE FROM mfa_pending WHERE user_id=?", (user["id"],))
        codes = recovery_codes(user["id"])
        record = store.one("SELECT * FROM users WHERE id=?", (user["id"],))
        log(user["name"], client_ip(request), "2fa_enabled")
        return {**start_session(record, response, request, revoke_all=True), "recovery_codes": codes}


@router.post("/security/totp/disable")
def totp_disable(body: ReauthInput, request: Request, response: Response, user=Depends(authenticated)):
    with auth_lock:
        record = reauthenticate(body, request, user)
        store.execute("UPDATE users SET totp_secret=NULL,totp_last=-1 WHERE id=?", (user["id"],))
        store.execute("DELETE FROM recovery_codes WHERE user_id=?", (user["id"],))
        record["totp_secret"] = None
        log(user["name"], client_ip(request), "2fa_disabled")
        return start_session(record, response, request, revoke_all=True)


@router.post("/security/totp/recovery")
def regenerate_codes(body: ReauthInput, request: Request, response: Response, user=Depends(authenticated)):
    with auth_lock:
        record = reauthenticate(body, request, user)
        if not record["totp_secret"]:
            raise HTTPException(400, "Enable 2FA first")
        codes = recovery_codes(user["id"])
        log(user["name"], client_ip(request), "recovery_regenerated")
        return {**start_session(record, response, request, revoke_all=True), "recovery_codes": codes}


class PasswordInput(BaseModel):
    current: str = Field(max_length=256)
    password: str = Field(min_length=12, max_length=256)
    code: str = Field(default="", max_length=80)


@router.put("/password")
def change_password(body: PasswordInput, request: Request, response: Response, user=Depends(authenticated)):
    with auth_lock:
        record = reauthenticate(ReauthInput(password=body.current, code=body.code), request, user)
        store.execute("UPDATE users SET password=? WHERE id=?", (store.password_hash(body.password), user["id"]))
        log(user["name"], client_ip(request), "password_changed")
        return start_session(record, response, request, revoke_all=True)


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idle_minutes: int = Field(ge=1, le=240)
    absolute_hours: int = Field(ge=1, le=168)
    bind_ip: bool
    bind_browser: bool
    ban_minutes: int = Field(ge=1, le=1440)
    password: str = Field(max_length=256)
    code: str = Field(default="", max_length=80)


@router.get("/security/policy")
def get_policy(user=Depends(admin)):
    return {**settings(), "secure_cookie": secure_cookie(),
            "trusted_proxies": [str(n) for n in trusted_proxies()], "origin": os.environ.get("HARBOUR_ORIGIN", "")}


@router.put("/security/policy")
def set_policy(body: PolicyInput, request: Request, response: Response, user=Depends(admin)):
    if body.idle_minutes > body.absolute_hours * 60:
        raise HTTPException(400, "Idle timeout cannot exceed the absolute session limit")
    with auth_lock:
        record = reauthenticate(ReauthInput(password=body.password, code=body.code), request, user)
        store.execute("UPDATE settings SET value=? WHERE key='security'", (json.dumps(body.model_dump(exclude={"password", "code"})),))
        log(user["name"], client_ip(request), "policy_changed", "Session and IP-ban policy updated")
        return {**start_session(record, response, request, revoke_all=True), "ok": True}


@router.get("/security/log")
def login_log(offset: int = 0, user=Depends(admin)):
    offset = max(0, min(offset, 10_000_000))
    return {"entries": store.rows("SELECT * FROM auth_log ORDER BY id DESC LIMIT 50 OFFSET ?", (offset,)),
            "total": store.one("SELECT COUNT(*) AS n FROM auth_log")["n"],
            "bans": store.rows("SELECT address,until FROM ip_bans WHERE until>? ORDER BY until DESC", (time.time(),))}


class UnbanInput(BaseModel):
    address: str = Field(max_length=128)


@router.post("/security/unban")
def unban(body: UnbanInput, request: Request, user=Depends(admin)):
    store.execute("DELETE FROM ip_bans WHERE address=?", (body.address,))
    store.execute("DELETE FROM login_attempts WHERE address=?", (body.address,))
    log(user["name"], client_ip(request), "ip_unbanned", body.address)
    return {"ok": True}
