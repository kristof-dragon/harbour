"""Weighted SQLite time-series buckets with staged, incremental retention."""
import json
import math
import threading
import time
from contextlib import nullcontext

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import polling, remote_probe, resources, store, volumes
from .auth import admin, authenticated

router = APIRouter(prefix="/api")
housekeeping_lock = threading.Lock()
LOAD_KEYS = ("load1", "load5", "load15")


def policy():
    return json.loads(store.one("SELECT value FROM settings WHERE key='monitoring'")["value"])


def empty():
    return {"attempts": 0, "up": 0, "n": 0, "cpu_n": 0, "cpu_sum": 0, "cpu_max": 0,
            "cpu_duration_n": 0, "cpu_seconds_sum": 0, "cpu_weighted_sum": 0,
            "uptime_n": 0, "uptime_sum": 0, "uptime_max": 0,
            **{key + suffix: 0 for key in LOAD_KEYS for suffix in ("_n", "_sum", "_max")},
            "memory_n": 0, "memory_sum": 0, "memory_max": 0, "memory_used_sum": 0, "memory_total_sum": 0,
            "latency_n": 0, "latency_sum": 0, "latency_max": 0, "disks": {},
            "temperature_n": 0, "temperature_sum": 0, "temperature_max": -273.15, "sensors": {}, "hardware": {}}


def merge(left, right):
    # Legacy buckets had a CPU reading for every resource sample. New first
    # polls and reset intervals have other metrics but no CPU percentage.
    left, right = ({**empty(), **data, 'cpu_n': data.get('cpu_n', data.get('n', 0)), 'memory_n': data.get('memory_n', data.get('n', 0))} for data in (left, right))
    value = dict(left)
    for key in ("sample_first_min", "sample_last_max"):
        if key in right and key not in value:
            value[key] = right[key]
            left[key] = right[key]
        if key in value and key not in right:
            right[key] = left[key]
    for key in value:
        if key == "sample_first_min":
            value[key] = min(left.get(key, float("inf")), right.get(key, float("inf")))
            continue
        if key in {"disks", "sensors", "hardware"}:
            continue
        value[key] = max(left[key], right[key]) if key.endswith("_max") else left[key] + right[key]
    for collection in ("disks", "sensors", "hardware"):
        value[collection] = {k: dict(v) for k, v in left[collection].items()}
        for identity, item in right[collection].items():
            if identity not in value[collection]:
                value[collection][identity] = dict(item)
            else:
                target = value[collection][identity]
                for key in item:
                    target[key] = item[key] if key not in target or isinstance(item[key], str) else min(target[key], item[key]) if key.endswith("_min") else (
                        max(target[key], item[key]) if key.endswith("_max") else target[key] + item[key])
    return value


def sample_payload(metrics=None, latency_ms=None, up=False, attempt=True):
    sample = empty()
    sample.update(attempts=int(attempt), up=int(up and attempt))
    if latency_ms is not None:
        sample.update(latency_n=1, latency_sum=latency_ms, latency_max=latency_ms)
    if metrics:
        sample.update(n=1)
        memory = metrics.get('memory')
        if memory and resources.finite(memory.get('percent')):
            sample.update(memory_n=1, memory_sum=memory['percent'], memory_max=memory['percent'],
                          memory_used_sum=memory['used'], memory_total_sum=memory['total'])
        value = metrics.get('cpu')
        if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100:
            sample.update(cpu_n=1, cpu_sum=value, cpu_max=value)
            duration = metrics.get('cpu_sample_seconds')
            if resources.finite(duration) and duration > 0:
                sample.update(cpu_duration_n=1, cpu_seconds_sum=duration, cpu_weighted_sum=value * duration)
        uptime = metrics.get('uptime')
        if resources.finite(uptime) and uptime >= 0:
            sample.update(uptime_n=1, uptime_sum=uptime, uptime_max=uptime)
        for key in LOAD_KEYS:
            value = metrics.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                sample.update({key + "_n": 1, key + "_sum": value, key + "_max": value})
        for disk in metrics["disks"] if metrics.get('disks_sampled', True) else []:
            sample["disks"][disk["mount"]] = {"n": 1, "percent_sum": disk["percent"], "percent_max": disk["percent"],
                "used_sum": disk["used"], "used_max": disk["used"], "total_sum": disk["total"],
                "free_sum": disk["free"], "free_min": disk["free"]}
        temp = remote_probe.temperature_summary(metrics.get("temperature", {}).get("sensors", []))
        if temp["package"] is not None:
            sample.update(temperature_n=1, temperature_sum=temp["package"], temperature_max=temp["package"])
        for sensor in temp.get("sensors", []):
            sample["sensors"][sensor["id"]] = {"label": sensor["label"], "n": 1, "sum": sensor["celsius"], "celsius_max": sensor["celsius"]}
        for sensor in metrics.get('hardware', []):
            value = sensor.get('value')
            if type(value) not in (int, float) or not math.isfinite(value):
                continue
            # Unit/source changes start separate series, never mixed averages.
            identity = json.dumps([sensor['id'], sensor['unit'], sensor['source']], ensure_ascii=False, separators=(',', ':'))
            sample['hardware'][identity] = {**{k: sensor[k] for k in ('id', 'label', 'unit', 'source')},
                                            'n': 1, 'sum': value, 'value_max': value}
    return sample


def put(con, server_id, bucket, resolution, payload):
    row = con.execute("SELECT payload FROM resource_history WHERE server_id=? AND bucket=? AND resolution=?",
                      (server_id, bucket, resolution)).fetchone()
    if row:
        payload = merge(json.loads(row[0]), payload)
    con.execute("INSERT OR REPLACE INTO resource_history VALUES (?,?,?,?)", (server_id, bucket, resolution, json.dumps(payload)))


def record(server_id, metrics=None, latency_ms=None, up=False, now=None, connection=None, attempt=True):
    now = time.time() if now is None else now
    resolution = policy()["week1_minutes"] * 60
    bucket = int(now // resolution * resolution)
    with (nullcontext(connection) if connection is not None else store.db()) as con:
        if connection is None:
            con.execute("BEGIN IMMEDIATE")
        server = con.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if not server:
            return
        if metrics and server:
            metrics = resources.filter_history(dict(server), metrics)
            metrics = {**metrics, "disks": [d for d in metrics["disks"] if volumes.options(dict(server), d["mount"])["monitor"]]}
        payload = sample_payload(metrics, latency_ms, up, attempt)
        if metrics:
            payload.update(sample_first_min=now, sample_last_max=now)
            for collection in ('disks', 'sensors', 'hardware'):
                for sensor in payload[collection].values():
                    sensor.update(sample_first_min=now, sample_last_max=now)
        put(con, server_id, bucket, resolution, payload)


def compact(now=None, limit=12000):
    """Coarsen oldest tiers first. Delete inputs and merge outputs atomically."""
    if not housekeeping_lock.acquire(blocking=False):
        return
    try:
        now = time.time() if now is None else now
        p = policy()
        with store.db() as con:
            con.execute("DELETE FROM resource_history WHERE bucket + resolution <= ?", (now-p["retention_days"]*86400,))
        for age_days, minutes in ((28, p["older_minutes"]), (14, p["weeks3_4_minutes"]), (7, p["week2_minutes"])):
            resolution = minutes * 60
            # Process only complete target buckets beyond this tier's age boundary.
            boundary = int((now-age_days*86400)//resolution*resolution)
            with store.db() as con:
                con.execute("BEGIN IMMEDIATE")
                rows = con.execute("SELECT * FROM resource_history WHERE bucket+resolution<=? AND resolution<? ORDER BY bucket LIMIT ?",
                                   (boundary, resolution, limit)).fetchall()
                grouped = {}
                for row in rows:
                    key = (row["server_id"], int(row["bucket"]//resolution*resolution))
                    grouped[key] = merge(grouped.get(key, empty()), json.loads(row["payload"]))
                for (server_id, bucket), payload in grouped.items():
                    put(con, server_id, bucket, resolution, payload)
                con.executemany("DELETE FROM resource_history WHERE server_id=? AND bucket=? AND resolution=?",
                                [(r["server_id"], r["bucket"], r["resolution"]) for r in rows])
        with store.db() as con:
            con.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        housekeeping_lock.release()


def series(server_id, hours, now=None, requested_resolution=0, disk_mount=None):
    now = time.time() if now is None else now
    p = policy()
    hours = min(hours, p["retention_days"] * 24)
    since = now - hours * 3600
    rows = store.rows("SELECT bucket,resolution,payload FROM resource_history WHERE server_id=? AND bucket+resolution>? AND bucket<=? ORDER BY bucket",
                      (server_id, since, now))
    multiple = math.lcm(*(r["resolution"] for r in rows)) if rows else p["week1_minutes"]*60
    required = max(requested_resolution or hours*3600/240, hours*3600/2000, multiple)
    choices = [60, 300, 600, 900, 1800, 3600, 10800, 21600, 43200, 86400, 172800, 604800]
    resolution = next((n for n in choices if n >= required and n % multiple == 0), int(math.ceil(required/multiple)*multiple))
    grouped = {}
    for row in rows:
        bucket = int(row["bucket"]//resolution*resolution)
        grouped[bucket] = merge(grouped.get(bucket, empty()), json.loads(row["payload"]))
    # Read the package's own aggregates, including legacy buckets. The old
    # temperature_sum/max fields mixed unrelated sensors and must not be relabelled.
    known = {key: {"id": key, **s} for data in grouped.values() for key, s in data["sensors"].items()
             if remote_probe.temperature_kind({"id": key, **s}) == "cpu_package"}
    server = store.one("SELECT * FROM servers WHERE id=?", (server_id,))
    current = json.loads(server["snapshot"]).get("metrics", {}).get("temperature", {}) if server else {}
    selected = remote_probe.temperature_summary(current.get("sensors", []))["package_sensor_id"]
    if not selected and known:
        selected = min(known.values(), key=remote_probe.package_order)["id"]
    points = []
    for bucket, data in sorted(grouped.items()):
        n, ln, cn, mn = data["n"], data["latency_n"], data["cpu_n"], data["memory_n"]
        package = data["sensors"].get(selected)
        disks = [{"mount": mount, "percent": d["percent_sum"]/d["n"], "peak": d["percent_max"],
                  "used_gb": d["used_sum"]/d["n"]/1e9, "free_gb": d["free_sum"]/d["n"]/1e9,
                  "sample_first": d.get('sample_first_min'), "sample_last": d.get('sample_last_max'),
                  "min_free_gb": d["free_min"]/1e9} for mount, d in data["disks"].items()]
        card_disks = [d for d in disks if d["mount"] == disk_mount] if disk_mount else [d for d in disks if server and volumes.options(server, d["mount"])["monitor"] and volumes.options(server, d["mount"])["card"]]
        average_cpu = (data['cpu_weighted_sum'] / data['cpu_seconds_sum'] if data['cpu_duration_n'] == cn and data['cpu_seconds_sum'] > 0
                       else data['cpu_sum'] / cn if cn else None)
        points.append({"time": bucket, "sample_first": data.get("sample_first_min"), "sample_last": data.get("sample_last_max"), "cpu": average_cpu, "cpu_peak": data["cpu_max"] if cn else None,
                       "uptime": data['uptime_sum'] / data['uptime_n'] if data['uptime_n'] else None,
                       "uptime_peak": data['uptime_max'] if data['uptime_n'] else None,
                       **{key: data[key + "_sum"]/data[key + "_n"] if data[key + "_n"] else None for key in LOAD_KEYS},
                       **{key + "_peak": data[key + "_max"] if data[key + "_n"] else None for key in LOAD_KEYS},
                       "memory": data["memory_sum"]/mn if mn else None, "memory_peak": data["memory_max"] if mn else None,
                       "disk": max((d["percent"] for d in card_disks), default=None), "disk_peak": max((d["peak"] for d in card_disks), default=None),
                       "disk_sample_first": min((d['sample_first'] for d in card_disks if d['sample_first'] is not None), default=None),
                       "disk_sample_last": max((d['sample_last'] for d in card_disks if d['sample_last'] is not None), default=None),
                       "disks": disks, "latency_ms": data["latency_sum"]/ln if ln else None,
                       "temperature": package["sum"]/package["n"] if package else None,
                       "temperature_peak": package["celsius_max"] if package else None,
                       "sensors": [{"id": key, "label": d["label"], "celsius": d["sum"]/d["n"], "peak": d["celsius_max"], "sample_first": d.get("sample_first_min"), "sample_last": d.get("sample_last_max")} for key, d in data["sensors"].items()],
                       "hardware": [{**{k: d[k] for k in ('id', 'label', 'unit', 'source')},
                                     'value': d['sum']/d['n'], 'peak': d['value_max'], 'samples': d['n'],
                                     'sample_first': d.get('sample_first_min'), 'sample_last': d.get('sample_last_max')}
                                    for d in data['hardware'].values()],
                       "up_percent": 100*data["up"]/data["attempts"] if data["attempts"] else None,
                       "attempts": data["attempts"], "samples": n})
    return {"points": points, "resolution_seconds": resolution, "from": since, "to": now, "hours": hours,
            "retention_days": p["retention_days"], "demo": store.DEMO, "requested_resolution": requested_resolution,
            "temperature_source": known.get(selected, {}).get("label"), "temperature_sensor_id": selected,
            "poll_seconds": (server["poll_seconds"] or p["poll_seconds"]) if server else p["poll_seconds"],
            "sample_seconds": (server['record_seconds'] if json.loads(server['snapshot']).get('recording', {}).get('mode') == 'local'
                               else server['poll_seconds'] or p['poll_seconds']) if server else p['poll_seconds'],
            "disk_seconds": server['disk_seconds'] if server else 300,
            "disk_mount": disk_mount, "disk_mounts": sorted({mount for data in grouped.values() for mount in data["disks"]})}


class MonitoringPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_seconds: int = Field(ge=15, le=3600)
    update_check_hours: int = Field(ge=1, le=168)
    retention_days: int = Field(ge=1, le=730)
    week1_minutes: int
    week2_minutes: int
    weeks3_4_minutes: int
    older_minutes: int


@router.get("/monitoring")
def get_policy(user=Depends(admin)):
    count = store.one("SELECT COUNT(*) AS n, MIN(bucket) AS oldest FROM resource_history")
    with store.db() as con:
        allocated = con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
        free = con.execute("PRAGMA freelist_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
    return {**policy(), "history_rows": count["n"], "oldest": count["oldest"], "db_bytes": allocated, "reusable_bytes": free}


@router.put("/monitoring")
def save_policy(body: MonitoringPolicy, user=Depends(admin)):
    values = body.model_dump()
    fields = ["week1_minutes", "week2_minutes", "weeks3_4_minutes", "older_minutes"]
    choices = [{1, 5, 10, 15}, {5, 15, 30, 60}, {15, 30, 60}, {60, 180, 360}]
    sizes = [values[k] for k in fields]
    if any(n not in options for n, options in zip(sizes, choices)) or any(b < a or b % a for a, b in zip(sizes, sizes[1:])):
        raise HTTPException(400, "Older tiers must use equal or coarser resolutions that are multiples of the previous tier")
    store.execute("UPDATE settings SET value=? WHERE key='monitoring'", (json.dumps(values),))
    polling.changed.set()
    # Retention applies on the next hourly sweep; no large synchronous deletion in a request.
    return {"ok": True}


class ServerMonitoring(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    poll_seconds: int | None = Field(default=None, ge=15, le=3600)


@router.put("/servers/{id_}/monitoring")
def save_server_monitoring(id_: str, body: ServerMonitoring, user=Depends(admin)):
    server = store.one("SELECT monitoring_enabled FROM servers WHERE id=?", (id_,))
    if not server:
        raise HTTPException(404, "Server not found")
    with store.db() as con:
        con.execute("UPDATE servers SET monitoring_enabled=?,poll_seconds=? WHERE id=?", (int(body.enabled), body.poll_seconds, id_))
        if body.enabled and not server["monitoring_enabled"]:
            con.execute("UPDATE servers SET last_attempt=NULL WHERE id=?", (id_,))
    polling.changed.set()
    return {"ok": True}


@router.get("/servers/{id_}/history")
def history(id_: str, hours: float = 6, resolution: int = 0, disk: str | None = None, user=Depends(authenticated)):
    if not math.isfinite(hours) or not 1 <= hours <= 17520:
        raise HTTPException(400, "Choose between 1 and 17520 hours")
    if not store.one("SELECT 1 FROM servers WHERE id=?", (id_,)):
        raise HTTPException(404, "Server not found")
    if resolution not in {0,60,300,600,900,1800,3600,10800,21600,43200,86400,604800}:
        raise HTTPException(400, "Unsupported display resolution")
    return series(id_, hours, requested_resolution=resolution, disk_mount=disk)
