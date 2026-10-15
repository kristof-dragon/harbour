"""Deployment budgets, applied before materializing recorded evidence.

Byte budgets bound input/output, not Python heap. Compose supplies the independent
hard process-tree memory ceiling. Oversized evidence remains on disk.
"""
import gzip
import io
import json
import os
import sys
import threading
from contextlib import contextmanager

MiB = 1024 * 1024


def small():
    value = os.environ.get('HARBOUR_RESOURCE_PROFILE', 'standard')
    if value not in ('standard', 'sbc-1g'):
        raise ValueError('HARBOUR_RESOURCE_PROFILE must be standard or sbc-1g')
    return value == 'sbc-1g'


def number(name, default, minimum=1, maximum=1024):
    value = int(os.environ.get('HARBOUR_'+name, default))
    if not minimum <= value <= maximum:
        raise ValueError('HARBOUR_'+name+' is outside its supported range')
    return value


def budget():
    return number('PROCESSING_MIB', 8 if small() else 32, 2, 128)*MiB


def batch_bytes():
    return min(budget()//4, MiB)


def buffer_bytes():
    return number('BUFFER_TOTAL_MIB', 16 if small() else 128, 1, 512)*MiB


def flush_seconds():
    return number('WRITE_INTERVAL_SECONDS', 10 if small() else 1, 1, 60)


class BudgetExceeded(ValueError):
    def __init__(self):
        super().__init__('Evidence exceeds the configured processing budget. Original data is retained; choose a shorter range or increase HARBOUR_PROCESSING_MIB and the container memory limit.')


# One large evidence operation per process, including API response serialization.
processing_lock = threading.RLock()


@contextmanager
def processing():
    with processing_lock:
        yield


def unpack(payload, limit=None):
    limit = budget() if limit is None else limit
    if len(payload) > limit:
        raise BudgetExceeded()
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
        raw = stream.read(limit+1)
    if len(raw) > limit:
        raise BudgetExceeded()
    return json.loads(raw)


def pack(value, limit=None):
    limit = budget() if limit is None else limit
    output = io.BytesIO()
    used = 0
    with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=6, mtime=0, filename='') as stream:
        for part in json.JSONEncoder(allow_nan=False, separators=(',', ':')).iterencode(value):
            encoded = part.encode()
            used += len(encoded)
            if used > limit or output.tell() > limit:
                raise BudgetExceeded()
            stream.write(encoded)
    return output.getvalue()


def size(value):
    """Actual retained Python object size; no second serialized copy."""
    seen = set()
    def count(item):
        if id(item) in seen:
            return 0
        seen.add(id(item))
        result = sys.getsizeof(item)
        if isinstance(item, dict):
            result += sum(count(k)+count(v) for k,v in item.items())
        elif isinstance(item, (tuple, list)):
            result += sum(count(v) for v in item)
        return result
    return count(value)


def telemetry(metrics):
    """Keep all diagnostic counters, omit repeated inventories/configuration.

    No list clipping: all interfaces, stations and queues remain attributable.
    """
    from .connection_analysis import pick
    value = pick(metrics, 'measured_at cpu boot_id memory')
    value['openwrt'] = pick(metrics.get('openwrt', {}),
        'cpu_percent softirq_percent sample_seconds conntrack interfaces radio_stats queue_stats stations network board capabilities')
    return value


def settings(value):
    value = dict(value)
    value['telemetry_seconds'] = max(value.get('telemetry_seconds', 1), 10 if small() else 1)
    if small():
        value.update(interval=max(2, value['interval']), router_probes=False,
                     buffer_mib=min(value['buffer_mib'], buffer_bytes()//MiB))
    return value


def bounded(fn):
    """Serialize evidence work before loading; encode API responses under lock."""
    import functools
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with processing_lock:
            result = fn(*args, **kwargs)
            if isinstance(result, (dict,list)):
                from fastapi.responses import JSONResponse
                return JSONResponse(result)
            return result
    return wrapped


def saved_download(table, key, identity, filename):
    """Export a stored blob without decompressing or materializing the full row."""
    import tempfile
    from . import store
    from .network_incidents import PreparedDownload
    if (table,key) not in (('call_test_evidence','test_id'),):
        raise ValueError('Unsupported evidence source')
    file = tempfile.TemporaryFile(mode='w+b', dir=store.DATA)
    try:
        with store.db() as con:
            # One read transaction ensures all chunks belong to the same snapshot.
            con.execute('BEGIN')
            size=con.execute(f'SELECT length(payload) FROM {table} WHERE {key}=?',(identity,)).fetchone()[0]
            for offset in range(1,size+1,65536):
                file.write(con.execute(f'SELECT substr(payload,?,65536) FROM {table} WHERE {key}=?',(offset,identity)).fetchone()[0])
        return PreparedDownload(file,filename)
    except BaseException:
        file.close()
        raise


def saved_incident_download(server_id, identity):
    import tempfile
    from . import store
    from .network_incidents import PreparedDownload
    file=tempfile.TemporaryFile(mode='w+b',dir=store.DATA)
    try:
        with store.db() as con, gzip.GzipFile(fileobj=file,mode='wb',filename='',mtime=0) as output:
            con.execute('BEGIN')
            event=con.execute('SELECT id,measured,label,automatic,complete,length(CAST(archive AS BLOB)) AS bytes FROM network_events WHERE id=? AND server_id=?',(identity,server_id)).fetchone()
            if event is None or not event['bytes']:
                raise ValueError('Saved incident unavailable')
            meta={k:event[k] for k in ('id','measured','label','automatic','complete')}
            output.write(json.dumps({'schema':1,'incident':meta,'server_id':server_id}).encode()[:-1]+b',"saved_archive":')
            for offset in range(1,event['bytes']+1,65536):
                output.write(con.execute('SELECT substr(CAST(archive AS BLOB),?,65536) FROM network_events WHERE id=?',(offset,identity)).fetchone()[0])
            output.write(b'}')
        return PreparedDownload(file,'harbour-original-incident-'+identity+'.json.gz')
    except BaseException:
        file.close()
        raise
