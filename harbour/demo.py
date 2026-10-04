"""Synthetic fixtures. Demo mode never connects to hosts."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import remote_probe, store

GB = 1_000_000_000


def service(name, image, project=None, update=False, state="running", version=None):
    identity = (project or "solo") + "-" + name
    available = version
    if update and version:
        prefix, patch = version.rsplit('.', 1) if '.' in version else ('', version)
        available = (prefix + '.' if prefix else '') + str(int(patch) + 1)
    return {"id": identity, "name": name, "container": f"{project}-{name}-1" if project else name,
            "project": project, "image": image, "image_id": "sha256:" + "a" * 64,
            "version": version, "state": state, "health": "healthy" if state == "running" else None,
            "started": "2026-09-27T08:42:10Z", "ports": ["0.0.0.0:8080 → 80/tcp"] if name in {"web", "homepage"} else [],
            "restart_policy": "unless-stopped", "mounts": [{"destination": "/data", "type": "volume", "rw": True}],
            "working_dir": "/opt/stacks/" + project if project else None,
            "config_files": ["/opt/stacks/" + project + "/compose.yaml"] if project else [],
            "platform": {"os": "linux", "architecture": "amd64", "variant": ""},
            "update": {"status": "available" if update else "current", "digest": "sha256:" + ("b" if update else "a") * 64, "version": available, "checked": time.time()}}


def seed():
    if store.one("SELECT 1 FROM servers"):
        for server in store.rows("SELECT id,snapshot FROM servers"):
            snapshot = json.loads(server["snapshot"])
            if snapshot.get("metrics") and "timezone" not in snapshot["metrics"]:
                snapshot["metrics"]["timezone"] = demo_timezone(server["id"])
                store.execute("UPDATE servers SET snapshot=? WHERE id=?", (json.dumps(snapshot), server["id"]))
        return
    fixtures = [
        ("atlas", "Atlas", "192.0.2.10", 28.4, 46.2, 16, 186, 500, [
            service("web", "ghcr.io/immich-app/immich-server:release", "immich", True, version="v1.142.0"),
            service("machine-learning", "ghcr.io/immich-app/immich-machine-learning:release", "immich", True, version="v1.142.0"),
            service("redis", "redis:7-alpine", "immich", version="7.4.2"),
            service("database", "postgres:16", "immich", version="16.8"),
            service("proxy", "traefik:v3", "gateway", version="3.3.5"),
            service("whoami", "traefik/whoami:latest", "gateway", version="1.11.0"),
            service("homepage", "ghcr.io/gethomepage/homepage:latest", update=True, version="1.4.0"),
            service("uptime-kuma", "louislam/uptime-kuma:1", version="1.23.16"),
        ]),
        ("luna", "Luna", "192.0.2.20", 16.8, 61.7, 32, 925, 1000, [
            service("server", "nextcloud:stable", "nextcloud", version="31.0.4"),
            service("database", "mariadb:11", "nextcloud", version="11.4.5"),
            service("cache", "redis:7-alpine", "nextcloud", version="7.4.2")]),
        ("edge", "Edge · London", "edge.example.net", 9.2, 38.5, 4, 22, 80, [
            service("web", "caddy:2", "websites", True, version="2.9.1"),
            service("app", "nginx:stable", "websites", version="1.26.3")]),
        ("backup", "Backup", "192.0.2.30", 2.1, 18.4, 8, 312, 2000, [
            service("restic", "restic/restic:latest", state="exited", version="0.18.0")]),
    ]
    for id_, name, host, cpu, ram, ram_gb, used, total, services in fixtures:
        snapshot = {"metrics": {"cpu": cpu, "cores": 8 if id_ == "atlas" else 4,
                    "memory": {"percent": ram, "total": ram_gb * GB, "used": ram_gb * GB * ram / 100},
                    "disks": [{"mount": "/", "total": total * GB, "used": used * GB, "free": (total - used) * GB, "percent": used / total * 100}],
                    "uptime": 12 * 86400 + 6 * 3600, "os": "Ubuntu 24.04.3 LTS", "docker": "28.4.0", "timezone": demo_timezone(id_)},
                    "services": services, "history": [max(0, cpu + ((i * 7) % 17 - 8)) for i in range(32)]}
        store.execute("INSERT INTO servers (id,name,host,port,username,fingerprint,snapshot,checked,update_checked) VALUES (?,?,?,22,'harbour','DEMO',?,?,?)",
                      (id_, name, host, json.dumps(snapshot), time.time(), time.time()))


def demo_timezone(id_):
    name = "UTC" if id_ == "backup" else "Europe/London"
    local = datetime.now(ZoneInfo(name))
    offset = local.strftime("%z")
    return {"name": name, "abbreviation": local.tzname(), "offset": offset[:3] + ":" + offset[3:], "local_time": local.isoformat(timespec="seconds")}


def seed_history():
    from . import history
    import math
    now = time.time()
    for server in store.rows("SELECT * FROM servers"):
        snapshot = json.loads(server["snapshot"])
        metrics = snapshot.get("metrics")
        if not metrics:
            continue
        metrics.setdefault("kernel", "6.8.0-79-generic")
        for key, factor in (("load1", 1), ("load5", .9), ("load15", .8)):
            metrics.setdefault(key, round(metrics["cpu"] / 100 * metrics["cores"] * factor, 2))
        sensors = [] if server["id"] == "edge" else [{"id": "demo:cpu", "label": "CPU package", "celsius": 48.2 if server["id"] != "luna" else 57.5, "cpu": True}, {"id": "demo:nvme", "label": "NVMe composite", "celsius": 39.4, "cpu": False}]
        metrics["temperature"] = remote_probe.temperature_summary(metrics.get("temperature", {}).get("sensors", sensors))
        store.execute("UPDATE servers SET snapshot=?,connection_status='up',latency_ms=?,last_attempt=COALESCE(last_attempt,?) WHERE id=?",
                      (json.dumps(snapshot), {"atlas": 1.8, "luna": 2.4, "edge": 18.6, "backup": 3.2}.get(server["id"], 2), now, server["id"]))
        if store.one("SELECT 1 FROM resource_history WHERE server_id=? LIMIT 1", (server["id"],)):
            continue
        # Synthetic one-day history for exploring every chart mode, demo database only.
        resolution = history.policy()["week1_minutes"]*60
        with store.db() as con:
            for i in range(1440 // (resolution // 60)):
                sample = json.loads(json.dumps(metrics))
                phase = i / 13
                sample["cpu"] = max(.5, min(100, metrics["cpu"] + 10*math.sin(phase) + 4*math.sin(i/3)))
                for key, amplitude, period in (("load1", .6, 13), ("load5", .35, 18), ("load15", .15, 30)):
                    sample[key] = round(max(0, metrics[key] + amplitude * math.sin(i / period)), 2)
                sample["memory"]["percent"] = max(1, metrics["memory"]["percent"] + 3*math.sin(i/40))
                sample["memory"]["used"] = sample["memory"]["total"]*sample["memory"]["percent"]/100
                for sensor in sample["temperature"]["sensors"]:
                    sensor["celsius"] += 3*math.sin(phase)
                sample["temperature"] = remote_probe.temperature_summary(sample["temperature"]["sensors"])
                stamp = int((now - 86400 + i*resolution)//resolution*resolution)
                history.put(con, server["id"], stamp, resolution, history.sample_payload(sample, 2 + abs(math.sin(phase))*5, True))
