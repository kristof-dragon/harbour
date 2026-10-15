"""Small durable UTC daily counter ledger; one checkpoint per five minutes.

Counter replacement never erases history or counts a reset as negative traffic.
Only intervals bracketed by compatible counters count as observed coverage.
"""
import json
import sqlite3
import time
from . import store

DAYS = 90
CHECKPOINT_SECONDS = 300
path = None
rows = {}
dirty = set()
last_save = 0
error = None


def reset():
    global path, last_save, error
    path, last_save, error = None, 0, None
    rows.clear(); dirty.clear()


def load():
    global path, last_save, error
    target = store.DATA/'footprint.sqlite3'
    if path == target:
        return None
    reset()
    path = target
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE IF NOT EXISTS daily(day INTEGER, key TEXT, payload TEXT, PRIMARY KEY(day,key))')
        con.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, payload TEXT)')
        for day, key, payload in con.execute('SELECT day,key,payload FROM daily WHERE day>=?', (int(time.time()//86400-DAYS)*86400,)):
            rows[(day,key)] = json.loads(payload)
        state = con.execute('SELECT payload FROM state WHERE id=1').fetchone()
    restored = json.loads(state[0]) if state else None
    last_save = (restored or {}).get('saved_at') or time.time()
    return restored


def add(key, old, new):
    a, b = old.get('sampled_at'), new.get('sampled_at')
    seconds = new['clock']-old['clock']
    # Wall-clock corrections cannot assign bytes reliably to calendar days.
    if not isinstance(a, (float,int)) or not isinstance(b, (float,int)) or b<=a or abs(b-a-seconds)>2:
        return
    day = int(a//86400)*86400
    while day < b:
        duration = max(0, min(b, day+86400)-max(a, day))
        if duration:
            index = (day,key)
            row = rows.setdefault(index, {'read_bytes':0., 'write_bytes':0., 'read_seconds':0., 'write_seconds':0., 'intervals':0})
            for name in ('read','write'):
                field = name+'_bytes'
                if new.get(field) is not None and old.get(field) is not None:
                    row[field] += (new[field]-old[field])*duration/(b-a)
                    row[name+'_seconds'] += duration
            row['intervals'] += 1
            dirty.add(index)
        day += 86400


def save(state, force=False):
    global last_save, error
    if path is None or not force and time.time()-last_save<CHECKPOINT_SECONDS:
        return
    cutoff = int(time.time()//86400-DAYS)*86400
    try:
        with sqlite3.connect(path, timeout=2) as con:
            for day,key in dirty:
                con.execute('INSERT OR REPLACE INTO daily VALUES (?,?,?)', (day,key,json.dumps(rows[(day,key)],separators=(',',':'))))
            con.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (json.dumps({**state,'saved_at':time.time()},separators=(',',':')),))
            con.execute('DELETE FROM daily WHERE day<?', (cutoff,))
        for index in list(rows):
            if index[0]<cutoff:
                del rows[index]
        dirty.clear()
        last_save, error = time.time(), None
    except (OSError, sqlite3.Error):
        error = 'Footprint history could not be saved; recent measurements may be lost on restart.'


def summary(keys=('web','recorder')):
    result=[]
    for day in sorted({d for d,k in rows if k in keys}, reverse=True):
        parts = [rows.get((day,key), {}) for key in keys]
        # Deployment coverage requires every non-overlapping component.
        result.append({'day':day, **{name+'_bytes':sum(p.get(name+'_bytes',0) for p in parts) for name in ('read','write')},
                       **{name+'_seconds':min(p.get(name+'_seconds',0) for p in parts) for name in ('read','write')}})
    return {'timezone':'UTC','days':result,'retention_days':DAYS,'checkpoint_seconds':CHECKPOINT_SECONDS,
            'last_saved':last_save or None,'error':error,
            'note':'Measured counter deltas only. Missing intervals are excluded; midnight-crossing intervals are apportioned by elapsed time. A crash can lose up to five minutes of uncheckpointed history.'}
