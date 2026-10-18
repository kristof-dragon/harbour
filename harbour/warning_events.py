"""Durable warning episodes, independent of Telegram and UI sessions."""
import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from . import resources, store, volumes
from .auth import authenticated

router = APIRouter(prefix='/api/warning-events')


def initialize(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS warning_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          server_name TEXT NOT NULL, warning_key TEXT NOT NULL, source TEXT NOT NULL,
          title TEXT NOT NULL, detail TEXT NOT NULL, definition TEXT NOT NULL,
          started_at REAL NOT NULL, last_seen REAL NOT NULL, detected_at REAL NOT NULL,
          closed_at REAL, close_reason TEXT,
          initial_values TEXT NOT NULL, latest_values TEXT NOT NULL, extreme_values TEXT NOT NULL,
          observation_gap INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS warning_event_open
          ON warning_events(server_id,warning_key) WHERE closed_at IS NULL;
        CREATE INDEX IF NOT EXISTS warning_event_server ON warning_events(server_id,id DESC);
        CREATE INDEX IF NOT EXISTS warning_event_closed ON warning_events(closed_at);
        CREATE TABLE IF NOT EXISTS warning_observations (
          server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          warning_key TEXT NOT NULL, sampled_at REAL NOT NULL,
          PRIMARY KEY(server_id,warning_key));
        CREATE TABLE IF NOT EXISTS warning_config_epochs (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
          changed_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS warning_acknowledgements (
          event_id INTEGER NOT NULL REFERENCES warning_events(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          acknowledged_at REAL NOT NULL, PRIMARY KEY(user_id,event_id));
    ''')
    con.execute("INSERT OR IGNORE INTO settings VALUES ('warning_history_started_at',?)", (str(time.time()),))


def observe(con, server, key, source, sampled_at, active, title='', detail='', definition=None, values=None, received_at=None):
    """None means unknown; only an observed healthy value resolves an episode.

    Caller holds the same write transaction as the accepted sample / recorder ack.
    Watermarks prevent duplicates and late data from reopening a recovered warning.
    """
    received_at = time.time() if received_at is None else received_at
    started = float(con.execute("SELECT value FROM settings WHERE key='warning_history_started_at'").fetchone()[0])
    if source == 'resource':
        epoch = con.execute('SELECT changed_at FROM warning_config_epochs WHERE server_id=?', (server['id'],)).fetchone()
        if epoch:
            started = max(started, epoch[0])
    if not server['monitoring_enabled'] or not resources.finite(sampled_at) or sampled_at < started or sampled_at > received_at + 300:
        return
    cursor = con.execute('SELECT sampled_at FROM warning_observations WHERE server_id=? AND warning_key=?', (server['id'], key)).fetchone()
    if cursor and sampled_at <= cursor[0]:
        return
    con.execute('INSERT INTO warning_observations VALUES (?,?,?) ON CONFLICT(server_id,warning_key) DO UPDATE SET sampled_at=excluded.sampled_at', (server['id'], key, sampled_at))
    event = con.execute('SELECT * FROM warning_events WHERE server_id=? AND warning_key=? AND closed_at IS NULL', (server['id'], key)).fetchone()
    interval = max(server.get('poll_seconds') or 60, server.get('record_seconds') or 60,
                   (server.get('disk_seconds') or 60) if key.startswith('disk:') else 0)
    if event and sampled_at - event['last_seen'] > max(120, interval * 2 + 15):
        con.execute('UPDATE warning_events SET observation_gap=1 WHERE id=?', (event['id'],))
    if active is None:
        if event:
            con.execute('UPDATE warning_events SET observation_gap=1 WHERE id=?', (event['id'],))
        return
    if not active:
        if event:
            con.execute("UPDATE warning_events SET closed_at=?,close_reason='recovered' WHERE id=?", (sampled_at, event['id']))
        return
    definition = json.dumps(definition or {}, sort_keys=True)
    values = values or {}
    if event and event['definition'] != definition:
        con.execute("UPDATE warning_events SET closed_at=?,close_reason='condition_changed' WHERE id=?", (sampled_at, event['id']))
        event = None
    if event is None:
        con.execute('INSERT INTO warning_events (server_id,server_name,warning_key,source,title,detail,definition,started_at,last_seen,detected_at,initial_values,latest_values,extreme_values) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (server['id'], server['name'], key, source, title[:300], detail[:2000], definition, sampled_at, sampled_at, received_at, *([json.dumps(values)] * 3)))
    else:
        extreme = json.loads(event['extreme_values'])
        side = json.loads(definition).get('side')
        for field, value in values.items():
            if resources.finite(value):
                combine = min if field == 'free_gb' or side == 'low' else max
                extreme[field] = combine(extreme.get(field, value), value)
        gap = event['observation_gap'] or sampled_at - event['last_seen'] > max(120, interval * 2 + 15)
        con.execute('UPDATE warning_events SET last_seen=?,detail=?,latest_values=?,extreme_values=?,observation_gap=? WHERE id=?',
                    (sampled_at, detail[:2000], json.dumps(values), json.dumps(extreme), int(gap), event['id']))


def observe_resources(con, server, metrics, thresholds, received_at):
    sampled_at = metrics.get('measured_at') or received_at
    for row in resources.decorate(server, metrics, thresholds):
        if not row['monitor'] or not row['warn']:
            continue
        warning = resources.warnings([row])
        detail = warning[0]['detail'] if warning else ''
        observe(con, server, row['id'], 'resource', sampled_at, row['warning'] if row['present'] else None,
                warning[0]['title'] if warning else row['label'], detail,
                {'unit': row['unit'], 'low': row['low'], 'high': row['high'], 'side': row['limit_side']},
                {'value': row['value']} if row['present'] else {}, received_at)
    # Disk values may be cached while CPU/sensors continue sampling.
    if metrics.get('disks_sampled') is False and not metrics.get('disk_measured_at'):
        return
    disk_at = metrics.get('disk_measured_at') or sampled_at
    seen = set()
    for disk in volumes.decorate(server, metrics.get('disks', []), thresholds):
        if not disk['monitor'] or not disk['warn']:
            continue
        present = disk['present']
        seen.add('disk:' + disk['mount'])
        observe(con, server, 'disk:' + disk['mount'], 'resource', disk_at, disk['warning'] if present else None,
                'Disk space · ' + disk['mount'],
                f"{disk['percent']:.1f}% used · {volumes.capacity(disk['free'])} free" if present else '',
                {'unit': '%', 'high': thresholds['disk'], 'free_low': thresholds['disk_free_gb']},
                {'value': disk['percent'], 'free_gb': disk['free'] / 1e9} if present else {}, received_at)
    for event in con.execute("SELECT warning_key FROM warning_events WHERE server_id=? AND source='resource' AND warning_key LIKE 'disk:%' AND closed_at IS NULL", (server['id'],)).fetchall():
        if event[0] not in seen:
            observe(con, server, event[0], 'resource', disk_at, None, received_at=received_at)


def observe_inventory(con, server, snapshot, at, error=None):
    observe(con, server, 'docker', 'inventory', at, bool(error), 'Docker inventory check failed', error or '')
    if error:
        con.execute("UPDATE warning_events SET observation_gap=1 WHERE server_id=? AND source='service' AND closed_at IS NULL", (server['id'],))
        return
    services = snapshot.get('services', [])
    keys = set()
    for service in services:
        key = 'service:' + service['id']; keys.add(key)
        issue = service['state'] if service['state'] in {'restarting', 'dead'} else 'unhealthy' if service['state'] == 'running' and service.get('health') == 'unhealthy' else None
        observe(con, server, key, 'service', at, bool(issue), service['name'] + ' needs attention', issue or '')
    removed = [r[0] for r in con.execute("SELECT warning_key FROM warning_events WHERE server_id=? AND source='service' AND closed_at IS NULL", (server['id'],)) if r[0] not in keys]
    invalidate(con, server['id'], removed, 'resource_removed', at)


def observe_connection(con, server, at, error=None):
    observe(con, server, 'connection', 'connection', at, bool(error), 'Monitoring connection failed', error or '')
    if error:
        con.execute("UPDATE warning_events SET observation_gap=1 WHERE server_id=? AND source IN ('resource','recorder') AND closed_at IS NULL", (server['id'],))


def observe_recorder(con, server, delivery, at):
    observe(con, server, 'recorder', 'recorder', at, delivery.get('mode') == 'fallback', 'Local recorder unavailable', delivery.get('detail') or 'Using remote probes until the recorder is available.')
    observe(con, server, 'recorder-loss', 'recorder', at, bool(delivery.get('dropped')), 'Recorder history gap', str(delivery.get('dropped', 0)) + ' uncollected samples were removed from the bounded local queue.')


def configuration_changed(con, server_id, at=None):
    # Also covers sensors not yet discovered when inherited limits change.
    at = time.time() if at is None else at
    con.execute('INSERT INTO warning_config_epochs VALUES (?,?) ON CONFLICT(server_id) DO UPDATE SET changed_at=MAX(changed_at,excluded.changed_at)', (server_id, at))


def invalidate(con, server_id, keys=None, reason='settings_changed', at=None):
    """Close changed conditions without claiming the monitored value recovered."""
    at = time.time() if at is None else at
    all_keys = keys is None
    if keys is None:
        keys = {r[0] for r in con.execute('SELECT warning_key FROM warning_observations WHERE server_id=?', (server_id,))}
    if (all_keys or keys) and reason != 'resource_removed':
        configuration_changed(con, server_id, at)
    for key in keys:
        con.execute('UPDATE warning_events SET closed_at=MAX(?,last_seen),close_reason=? WHERE server_id=? AND warning_key=? AND closed_at IS NULL', (at, reason, server_id, key))
        con.execute('INSERT INTO warning_observations VALUES (?,?,?) ON CONFLICT(server_id,warning_key) DO UPDATE SET sampled_at=MAX(sampled_at,excluded.sampled_at)', (server_id, key, at))


def live_for_user(server_id, user_id):
    return {r['warning_key']: r for r in store.rows('SELECT e.id,e.warning_key,a.acknowledged_at FROM warning_events e LEFT JOIN warning_acknowledgements a ON a.event_id=e.id AND a.user_id=? WHERE e.server_id=? AND e.closed_at IS NULL', (user_id, server_id))}


def summary(user_id):
    row = store.one('SELECT COUNT(*) AS unacknowledged,MAX(e.id) AS latest_id FROM warning_events e WHERE NOT EXISTS (SELECT 1 FROM warning_acknowledgements a WHERE a.event_id=e.id AND a.user_id=?)', (user_id,))
    return {**row, 'started_at': float(store.one("SELECT value FROM settings WHERE key='warning_history_started_at'")['value'])}


def acknowledge_all(con, user_id):
    return con.execute('INSERT OR IGNORE INTO warning_acknowledgements SELECT id,?,? FROM warning_events', (user_id, time.time())).rowcount


def prune(now=None):
    days = json.loads(store.one("SELECT value FROM settings WHERE key='monitoring'")['value'])['retention_days']
    with store.db() as con:
        # Bounded, indexed sweep; active warnings are never aged out.
        con.execute('DELETE FROM warning_events WHERE id IN (SELECT id FROM warning_events WHERE closed_at<? ORDER BY closed_at,id LIMIT 500)', ((time.time() if now is None else now) - days * 86400,))


@router.get('')
def listing(user=Depends(authenticated), server_id: str = Query(default='', max_length=100),
            status: Literal['all', 'active', 'resolved', 'ended'] = 'all',
            acknowledgement: Literal['all', 'unacknowledged', 'acknowledged'] = 'all',
            search: str = Query(default='', max_length=200), before: int | None = Query(default=None, ge=1),
            limit: int = Query(default=50, ge=1, le=100)):
    conditions, args = [], [user['id']]
    if server_id:
        conditions.append('e.server_id=?'); args.append(server_id)
    if status == 'active':
        conditions.append('e.closed_at IS NULL')
    elif status == 'resolved':
        conditions.append("e.close_reason='recovered'")
    elif status == 'ended':
        conditions.append("e.closed_at IS NOT NULL AND e.close_reason<>'recovered'")
    if acknowledgement != 'all':
        conditions.append('a.acknowledged_at IS ' + ('NULL' if acknowledgement == 'unacknowledged' else 'NOT NULL'))
    if search:
        conditions.append("(e.title LIKE ? ESCAPE '\\' OR e.server_name LIKE ? ESCAPE '\\' OR e.detail LIKE ? ESCAPE '\\')")
        term = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        args.extend([term] * 3)
    if before is not None:
        conditions.append('e.id<?'); args.append(before)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = store.rows('SELECT e.*,a.acknowledged_at FROM warning_events e LEFT JOIN warning_acknowledgements a ON a.event_id=e.id AND a.user_id=?' + where + ' ORDER BY e.id DESC LIMIT ?', (*args, limit + 1))
    more = len(rows) > limit; rows = rows[:limit]
    for row in rows:
        for key in ('definition', 'initial_values', 'latest_values', 'extreme_values'):
            row[key] = json.loads(row[key])
        row['status'] = 'active' if row['closed_at'] is None else 'resolved' if row['close_reason'] == 'recovered' else 'ended'
    days = json.loads(store.one("SELECT value FROM settings WHERE key='monitoring'")['value'])['retention_days']
    return {'events': rows, 'next_before': rows[-1]['id'] if more else None, 'retention_days': days, **summary(user['id'])}


class AcknowledgementInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    acknowledged: bool = True


@router.put('/{event_id}/acknowledgement')
def acknowledge(event_id: int, body: AcknowledgementInput, user=Depends(authenticated)):
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        if not con.execute('SELECT 1 FROM warning_events WHERE id=?', (event_id,)).fetchone():
            raise HTTPException(404, 'Warning event no longer exists')
        if body.acknowledged:
            con.execute('INSERT OR IGNORE INTO warning_acknowledgements VALUES (?,?,?)', (event_id, user['id'], time.time()))
        else:
            con.execute('DELETE FROM warning_acknowledgements WHERE event_id=? AND user_id=?', (event_id, user['id']))
    return {'ok': True}
