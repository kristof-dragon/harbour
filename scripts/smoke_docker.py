"""Smoke-test the built image with disposable credentials, container and volume."""
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import http.cookiejar
import pyotp

name = "harbour-smoke-" + secrets.token_hex(4)
volume = name + "-data"
env = {**os.environ, "HARBOUR_SECRET": secrets.token_urlsafe(48), "HARBOUR_PASSWORD": secrets.token_urlsafe(24)}
password = env["HARBOUR_PASSWORD"]
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
csrf = ""


def docker(*args):
    return subprocess.check_output(["docker", *args], env=env, text=True).strip()


def request(path, body=None, method=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-CSRF-Token": csrf}, method=method)
    with opener.open(req, timeout=10) as res:
        return json.load(res)


def healthy():
    error = None
    for _ in range(50):
        try:
            assert request("/api/health")["status"] == "ok"
            return
        except Exception as exc:
            error = exc
            time.sleep(.2)
    print(docker("logs", name))
    raise RuntimeError("Container did not become healthy: " + str(error))


try:
    docker("run", "-d", "--name", name, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
           "--tmpfs", "/tmp:size=32m,noexec,nosuid", "-v", volume + ":/data", "-p", "127.0.0.1::8080",
           "-e", "HARBOUR_DATA=/data", "-e", "HARBOUR_SECRET", "-e", "HARBOUR_PASSWORD", "harbour:local")
    binding = docker("port", name, "8080/tcp")
    base = "http://" + binding
    healthy()
    config = request("/api/config")
    assert config["demo"] is False and config["version"]
    result = request("/api/login", {"name": "admin", "password": password})
    csrf = result["csrf"]
    assert result["role"] == "admin"
    assert request("/api/dashboard")["servers"] == []
    values = {"cpu": 80, "memory": 90, "disk": 82, "disk_free_gb": 8, "temperature": 78}
    request("/api/thresholds", values, "PUT")
    key = request("/api/keys", {})
    assert key["public_key"].startswith("ssh-ed25519") and "private" not in key
    request("/api/users", {"name": "viewer", "password": password, "role": "user"})
    policy = request("/api/monitoring")
    monitored = {k: policy[k] for k in ("poll_seconds", "update_check_hours", "retention_days", "week1_minutes", "week2_minutes", "weeks3_4_minutes", "older_minutes")}
    monitored["retention_days"] = 180
    request("/api/monitoring", monitored, "PUT")
    setup = request("/api/security/totp/begin", {"password": password})
    confirmed = request("/api/security/totp/confirm", {"code": pyotp.TOTP(setup["secret"]).now()})
    csrf = confirmed["csrf"]
    assert confirmed["mfa_enabled"] and len(confirmed["recovery_codes"]) == 8
    # Restart verifies durable settings, users, encrypted keys, 2FA and sessions.
    docker("restart", name)
    base = "http://" + docker("port", name, "8080/tcp")
    healthy()
    assert request("/api/dashboard")["thresholds"] == values
    assert len(request("/api/users")) == 2
    assert request("/api/monitoring")["retention_days"] == 180
    assert request("/api/security/me")["mfa_enabled"]
    assert any(entry["outcome"] == "2fa_enabled" for entry in request("/api/security/log")["entries"])
    result = request("/api/login", {"name": "admin", "password": password, "code": confirmed["recovery_codes"][0]})
    csrf = result["csrf"]
    assert request("/api/security/me")["recovery_remaining"] == 7
    result = request("/api/login", {"name": "viewer", "password": password})
    csrf = result["csrf"]
    try:
        request("/api/thresholds", values, "PUT")
        raise AssertionError("Reader unexpectedly allowed to change thresholds")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    assert docker("exec", name, "id", "-u") == "10001"
    print("PASS: production bootstrap, health, sign-in, key generation, 2FA and recovery, security audit, monitoring policy, permissions, durable restart, non-root/read-only container")
finally:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
