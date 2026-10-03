import copy
import json
import os
import time

import paramiko
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("HARBOUR_SECRET", "test-secret-only-" + "x" * 40)
os.environ.setdefault("HARBOUR_PASSWORD", "test-password-strong")
os.environ.setdefault("HARBOUR_DEMO", "true")

from harbour import app as module, demo, remote_probe, ssh, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "DEMO", True)
    with TestClient(module.app) as client:
        login = client.post("/api/login", json={"name": "admin", "password": "test-password-strong"})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrf"]
        yield client
    # Jobs use real threads; don't let a test change the database beneath them.
    deadline = time.monotonic() + 5
    while (any(lock.locked() for lock in [*module.locks.values(), *module.resource_locks.values()]) or module.job_queues) and time.monotonic() < deadline:
        time.sleep(.02)


def wait_job(client, id_):
    for _ in range(100):
        response = client.get("/api/jobs/" + id_).json()
        if response["status"] not in {"running", "queued"}:
            return response
        time.sleep(.03)
    pytest.fail("Job did not finish")


def test_unauthenticated_and_csrf(client):
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/servers/atlas/refresh").status_code == 403
    assert client.post("/api/demo-login", headers={"Origin": "https://evil.example"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/").headers["content-security-policy"].startswith("default-src 'self'")


def test_user_permissions_and_private_dismissal(client):
    result = client.post("/api/users", json={"name": "viewer", "password": "viewer-password-strong", "role": "user"})
    assert result.status_code == 200
    admin_id = client.get("/api/me").json()["id"]
    assert client.delete("/api/users/" + admin_id).status_code == 400
    viewer = client.post("/api/login", json={"name": "viewer", "password": "viewer-password-strong"}).json()
    client.headers["X-CSRF-Token"] = viewer["csrf"]
    assert client.get("/api/dashboard").status_code == 200
    for method, path, body in [
        ("POST", "/api/servers/atlas/refresh", None), ("POST", "/api/keys", {}),
        ("POST", "/api/servers/atlas/plan", {"action": "restart", "targets": ["group:immich"]}),
        ("PUT", "/api/thresholds", store.DEFAULTS), ("DELETE", "/api/servers/atlas", None),
        ("POST", "/api/users", {"name": "rogue", "password": "rogue-password-strong", "role": "admin"})]:
        assert client.request(method, path, json=body).status_code == 403
    assert client.post("/api/dismiss", json={"server_id": "atlas", "service_id": "immich-web"}).status_code == 200
    assert client.get("/api/dashboard").json()["servers"][0]["updates"] == 2
    login = client.post("/api/login", json={"name": "admin", "password": "test-password-strong"}).json()
    client.headers["X-CSRF-Token"] = login["csrf"]
    assert client.get("/api/dashboard").json()["servers"][0]["updates"] == 3


def test_thresholds_persist_and_either_disk_limit_warns(client):
    t = {"cpu": 99, "memory": 99, "disk": 99, "disk_free_gb": 500, "temperature": 80}
    assert client.put("/api/thresholds", json=t).status_code == 200
    server = client.get("/api/dashboard").json()["servers"][0]
    assert any(w["id"].startswith("disk:") for w in server["warnings"])
    override = {**t, "disk_free_gb": 0}
    assert client.put("/api/servers/atlas/thresholds", json=override).status_code == 200
    server = client.get("/api/dashboard").json()["servers"][0]
    assert not server["warnings"] and server["override"]
    store.initialize()
    assert json.loads(store.one("SELECT thresholds FROM servers WHERE id='atlas'")["thresholds"]) == override
    assert client.put("/api/servers/atlas/thresholds", content="null", headers={"Content-Type": "application/json"}).status_code == 200
    assert not client.get("/api/dashboard").json()["servers"][0]["override"]


def test_dismissal_renotifies_new_digest(client):
    client.post("/api/dismiss", json={"server_id": "atlas", "service_id": "immich-web"})
    snapshot = json.loads(store.one("SELECT snapshot FROM servers WHERE id='atlas'")["snapshot"])
    snapshot["services"][0]["update"]["digest"] = "sha256:" + "c" * 64
    store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
    assert client.get("/api/dashboard").json()["servers"][0]["updates"] == 3


def test_plan_execute_replay_and_pull_does_not_apply(client):
    def execute(action):
        p = client.post("/api/servers/atlas/plan", json={"action": action, "targets": ["group:immich"]})
        assert p.status_code == 200, p.text
        token = p.json()["token"]
        r = client.post("/api/servers/atlas/execute", json={"token": token})
        assert r.status_code == 200, r.text
        assert wait_job(client, r.json()["id"])["status"] == "succeeded"
        assert client.post("/api/servers/atlas/execute", json={"token": token}).status_code == 409
    execute("pull")
    assert client.get("/api/dashboard").json()["servers"][0]["updates"] == 3
    execute("up")
    assert client.get("/api/dashboard").json()["servers"][0]["updates"] == 1
    assert client.post("/api/servers/atlas/execute", json={"token": "forged"}).status_code == 400


def test_stale_data_blocks_actions_and_unknown_does_not_mean_current(client):
    store.execute("UPDATE servers SET checked=? WHERE id='atlas'", (time.time() - 121,))
    assert client.post("/api/servers/atlas/plan", json={"action": "restart", "targets": ["group:immich"]}).status_code == 409
    r = client.post("/api/servers/atlas/refresh")
    assert wait_job(client, r.json()["id"])["status"] == "succeeded"


def test_compose_command_scope_and_no_shell_interpolation():
    c = demo.service("web", "nginx:stable", "test")
    other = demo.service("db", "postgres:16", "test")
    c["working_dir"] = other["working_dir"] = "/opt/space and $(touch /tmp/never)"
    commands = remote_probe.plan([c, other], "up", [c["id"]])
    assert commands[0]["argv"][-4:] == ["up", "-d", "--no-deps", "web"]
    assert c["working_dir"] in commands[0]["argv"]
    assert remote_probe.plan([c, other], "restart", ["group:test"])[0]["argv"][-1] == "restart"
    with pytest.raises(ValueError):
        remote_probe.plan([c], "up", ["unknown"])
    with pytest.raises(ValueError):
        remote_probe.plan([c], "destroy", [c["id"]])
    with pytest.raises(ValueError):
        remote_probe.plan([demo.service("solo", "nginx")], "up", ["solo-solo"])
    c["config_files"] = ["relative.yml"]
    with pytest.raises(ValueError):
        remote_probe.plan([c], "pull", [c["id"]])


def test_manifest_platform_digest():
    manifest = [{"Descriptor": {"platform": {"os": "linux", "architecture": arch}},
                 "SchemaV2Manifest": {"config": {"digest": digest}}} for arch, digest in [("amd64", "sha256:amd"), ("arm64", "sha256:arm")]]
    assert remote_probe.remote_config_digest(manifest, {"os": "linux", "architecture": "arm64"}) == "sha256:arm"
    assert remote_probe.remote_config_digest(manifest, {"os": "linux", "architecture": "riscv64"}) is None
    assert remote_probe.remote_config_digest({"config": {"digest": "sha256:one"}}, {}) == "sha256:one"


def test_host_fingerprint_rejects_mismatch():
    key = paramiko.RSAKey.generate(2048)
    with pytest.raises(paramiko.SSHException, match="fingerprint mismatch"):
        ssh.PinnedHostKey("SHA256:" + "a" * 43).missing_host_key(None, "example", key)
    import base64, hashlib
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    ssh.PinnedHostKey(fingerprint).missing_host_key(None, "example", key)


def test_key_encryption_no_private_material_returned(client, monkeypatch):
    monkeypatch.setattr(store, "DEMO", False)
    result = client.post("/api/keys", json={})
    assert result.status_code == 200
    body = result.json()
    assert body["public_key"].startswith("ssh-ed25519 ")
    assert "private" not in result.text.lower()
    row = store.one("SELECT * FROM ssh_keys WHERE id=?", (body["id"],))
    assert "PRIVATE KEY" not in row["encrypted"]
    decrypted = json.loads(store.cipher().decrypt(row["encrypted"].encode()))
    ssh.parse_key(decrypted["private_key"])
    assert client.post("/api/demo-login").status_code == 404


def test_password_change_and_login_throttling(client):
    result = client.put("/api/password", json={"current": "test-password-strong", "password": "a-new-strong-password"})
    assert result.status_code == 200
    for _ in range(4):
        result = client.post("/api/login", json={"name": "admin", "password": "wrong"})
        assert result.status_code == 401
    assert client.post("/api/login", json={"name": "admin", "password": "wrong"}).status_code == 429
    assert client.post("/api/login", json={"name": "admin", "password": "a-new-strong-password"}).status_code == 429


def test_remote_revalidation_and_partial_failure(monkeypatch):
    services = [demo.service("web", "nginx", "one"), demo.service("db", "postgres", "two")]
    monkeypatch.setattr(remote_probe, "inventory", lambda: services)
    commands = remote_probe.plan(services, "restart", ["group:one", "group:two"])
    called = []
    def fake_run(argv, **kwargs):
        called.append(argv)
        if len(called) == 2:
            raise RuntimeError("Second project failed")
        return "done"
    monkeypatch.setattr(remote_probe, "run", fake_run)
    request = {"operation": "execute", "action": "restart", "targets": ["group:one", "group:two"], "expected": commands}
    result = remote_probe.handle(request)
    assert not result["ok"] and result["completed"] == 1 and "Second project failed" in result["output"]
    changed = copy.deepcopy(commands)
    changed[0]["cwd"] = "/unexpected"
    request["expected"] = changed
    with pytest.raises(ValueError, match="state changed"):
        remote_probe.handle(request)


def test_demo_cannot_open_production_store(client, monkeypatch):
    monkeypatch.setattr(store, "DEMO", False)
    with pytest.raises(RuntimeError, match="separate directory"):
        store.initialize()
