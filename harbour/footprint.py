"""Scoped current measurements and a checkpointed daily counter ledger."""
import json
import math
import threading
import time

from fastapi import APIRouter, Depends
from . import store, footprint_history as history, resource_limits as resources
from .auth import admin
from .remote_probe import harbour_self_footprint

router = APIRouter(prefix='/api/footprint')
lock = threading.RLock()
inputs, remote, tracks = {}, {}, {}
latest = None
storage_baseline = None


def restore():
    global storage_baseline
    try:
        state = history.load()
    except (OSError,ValueError,history.sqlite3.Error):
        history.error = 'Footprint history could not be loaded. Current measurements remain available.'
        state = None
    if state:
        tracks.update(state.get('tracks', {}))
        inputs.update(state.get('inputs', {}))
        storage_baseline = state.get('storage_baseline')


def checkpoint(force=False):
    history.save({'tracks':tracks,'inputs':inputs,'storage_baseline':storage_baseline}, force)


def finite(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def sanitize(sample):
    sample = sample if isinstance(sample, dict) else {}
    clean = {k: sample.get(k) for k in ('scope', 'identity', 'sampled_at', 'clock', 'memory_bytes', 'cache_bytes',
                                       'read_bytes', 'write_bytes', 'detail', 'io_available', 'app_bytes', 'kernel_bytes', 'dirty_cache_bytes', 'writeback_bytes', 'memory_limit_bytes', 'memory_peak_bytes')}
    for k in ('memory_bytes', 'cache_bytes', 'read_bytes', 'write_bytes', 'clock', 'sampled_at', 'app_bytes', 'kernel_bytes', 'dirty_cache_bytes', 'writeback_bytes', 'memory_limit_bytes', 'memory_peak_bytes'):
        if not finite(clean[k]):
            clean[k] = None
    for k in ('scope', 'identity', 'detail'):
        clean[k] = str(clean[k])[:500] if clean[k] is not None else None
    clean['io_available'] = clean['io_available'] is True
    return clean


def observe(key, sample):
    """Reset on replacement/reset/coverage changes; missing data is never zero."""
    clean = sanitize(sample)
    clean.update(window_seconds=None, read_per_second=None, write_per_second=None,
                 observed_read_bytes=None, observed_write_bytes=None, recent_read_per_second=None, recent_write_per_second=None)
    if not isinstance(clean['identity'], str) or len(clean['identity']) > 300 or clean['clock'] is None:
        tracks.pop(key, None)
        return clean
    previous = tracks.get(key)
    changed = (not previous or previous['last']['identity'] != clean['identity'] or
               not 0 <= clean['clock']-previous['last']['clock'] <= 600)
    if previous and not changed:
        changed = any((clean[k] is None) != (previous['last'][k] is None) or
                      (clean[k] is not None and clean[k] < previous['last'][k])
                      for k in ('read_bytes', 'write_bytes'))
    if changed:
        tracks[key] = {'first': dict(clean), 'last': dict(clean)}
        return clean
    first, last = previous['first'], previous['last']
    if clean['clock'] == last['clock']:
        return dict(last)
    if clean['clock']-first['clock'] > 86400:
        first = previous['first'] = dict(last)
    elapsed, recent = clean['clock']-first['clock'], clean['clock']-last['clock']
    history.add(key, last, clean)
    clean['recent_window_seconds'] = recent
    clean['window_seconds'] = elapsed
    for name in ('read', 'write'):
        key_bytes = name+'_bytes'
        if clean[key_bytes] is not None and first[key_bytes] is not None:
            delta = clean[key_bytes]-first[key_bytes]
            clean['observed_'+key_bytes] = delta
            clean[name+'_per_second'] = delta/elapsed if elapsed >= 1 else None
        if clean[key_bytes] is not None and last[key_bytes] is not None and recent >= 1:
            clean['recent_'+name+'_per_second'] = (clean[key_bytes]-last[key_bytes])/recent
    previous['last'] = dict(clean)
    return clean


def ingest(server_id, result, signature, login_only=False):
    # Serialized received data is a useful attribution signal, not disk writes.
    size = len(json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode())
    with lock:
        restore()
        if (inputs.get(server_id, {}).get('signature') != signature or
                time.monotonic()<inputs[server_id]['started']):
            inputs[server_id] = {'bytes': 0, 'samples': 0, 'started': time.monotonic(), 'signature': signature}
        entry = inputs[server_id]
        entry['bytes'] += size
        entry['samples'] += 1
        value = result.get('harbour_footprint')
        if isinstance(value, dict):
            remote[server_id] = {'signature': signature, 'received_at': time.time(),
                                 'samples': {k: sanitize(value.get(k)) for k in ('probe', 'resources', 'logins')}}
        elif not login_only:
            remote.pop(server_id, None)


def storage():
    global storage_baseline
    files = {}
    for name in ('harbour.db', 'harbour.db-wal', 'harbour.db-shm'):
        try:
            info = (store.DATA/name).stat()
            files[name] = {'bytes': info.st_size, 'allocated_bytes': info.st_blocks*512}
        except FileNotFoundError:
            files[name] = {'bytes': 0, 'allocated_bytes': 0}
        except OSError:
            files[name] = {'bytes': None, 'allocated_bytes': None}
    size = sum(f['bytes'] for f in files.values()) if all(f['bytes'] is not None for f in files.values()) else None
    now = time.time()
    if storage_baseline is None and size is not None:
        storage_baseline = (now, size)
    from . import storage_usage
    return {'files': files, 'bytes': size, 'volume': storage_usage.collect(), 'since': storage_baseline[0] if storage_baseline else None,
            'net_change_bytes': size-storage_baseline[1] if storage_baseline and size is not None else None}


def collect():
    from .network_runtime import read_status
    from .logins import connection_signature
    from .history import policy
    servers = store.rows('SELECT id,name,host,port,username,fingerprint,auth_method,key_id,password_encrypted,server_type,poll_seconds,monitoring_enabled FROM servers')
    default_poll = policy()['poll_seconds']
    recorder = read_status('__footprint__')
    now, clock = time.time(), time.monotonic()
    with lock:
        restore()
        active_keys = {'web', 'recorder', 'deployment'}
        components = [{'name': 'Harbour web and workers', **observe('web', harbour_self_footprint())}]
        components.append({'name': 'Network recorder', **observe('recorder', recorder.get('process', {}))})
        seen = set()
        for component in components:
            key = component.get('identity')
            component['overlaps'] = bool(key and key in seen)
            if key:
                seen.add(key)
        rows = []
        for server in servers:
            sid = server['id']
            entry = inputs.get(sid)
            ingress = None
            if entry and clock>=entry['started'] and server['monitoring_enabled'] and entry['signature'] == connection_signature(server):
                elapsed = clock-entry['started']
                ingress = {'bytes': entry['bytes'], 'batches': entry['samples'], 'window_seconds': elapsed,
                           'per_second': entry['bytes']/elapsed if elapsed >= 30 else None}
                key = 'incoming:'+sid; active_keys.add(key)
                observed = observe(key, {'identity':entry['signature'], 'clock':clock, 'sampled_at':now, 'write_bytes':entry['bytes'], 'scope':'logical'})
                ingress['per_second'] = observed.get('recent_write_per_second')
            n = recorder.get('servers', {}).get(sid, {})
            if now-(n.get('runtime_updated') or 0) > 5 or n.get('signature') != connection_signature(server) or not server['monitoring_enabled']:
                n = {}
            key = 'network:'+sid; active_keys.add(key)
            network = observe(key, {'identity': n.get('run_id'), 'clock': clock,
                'sampled_at': n.get('runtime_updated'), 'write_bytes': n.get('committed_bytes'), 'scope': 'logical'}) if n else None
            source = remote.get(sid, {})
            fresh = (source.get('signature') == connection_signature(server) and now-source.get('received_at', 0) <= max(120, (server['poll_seconds'] or default_poll)*2+15)
                     and bool(server['monitoring_enabled']))
            collectors = []
            for name, label in [('probe', 'SSH probe'), ('resources', 'Resource recorder'), ('logins', 'Login collector')]:
                key = sid+':'+name; active_keys.add(key)
                sample = observe(key, source.get('samples', {}).get(name, {}) if fresh else {})
                collectors.append({'name': label, 'fresh': fresh, **sample})
            buffers = {k:n.get(k) for k in ('buffer_bytes','buffer_limit_bytes','buffer_dropped','oldest_unsaved_seconds')}
            rows.append({'id': sid, 'buffer':buffers, 'name': server['name'], 'server_type': server['server_type'],
                         'incoming': ingress, 'network': network, 'collectors': collectors})
        for mapping in (inputs, remote):
            for key in set(mapping)-{s['id'] for s in servers}:
                mapping.pop(key, None)
        for key in set(tracks)-active_keys:
            tracks.pop(key, None)
        usable = [c for c in components if not c['overlaps']]
        complete = all(c.get('scope') == 'cgroup' and c['memory_bytes'] is not None and c['write_per_second'] is not None and c['read_per_second'] is not None for c in usable)
        total = {field: sum(c[field] for c in usable) if all(c[field] is not None for c in usable) else None
                 for field in ('memory_bytes', 'cache_bytes', 'read_per_second', 'write_per_second')}
        total['complete'] = complete
        for field in ('app_bytes','kernel_bytes','dirty_cache_bytes','writeback_bytes','memory_limit_bytes'):
            total[field] = sum(c[field] for c in usable) if all(c[field] is not None for c in usable) else None
        for name in ('read','write'):
            total['recent_'+name+'_per_second'] = sum(c['recent_'+name+'_per_second'] for c in usable) if all(c['recent_'+name+'_per_second'] is not None for c in usable) else None
        if all(c['identity'] and c['scope']=='cgroup' and c['read_bytes'] is not None and c['write_bytes'] is not None for c in usable):
            observe('deployment', {'identity':'|'.join(c['identity'] for c in usable), 'clock':clock, 'sampled_at':now,
                'read_bytes':sum(c['read_bytes'] for c in usable),'write_bytes':sum(c['write_bytes'] for c in usable),'scope':'cgroup'})
        else:
            tracks.pop('deployment',None)
        total['buffer_bytes'] = sum(s['buffer']['buffer_bytes'] or 0 for s in rows) if recorder.get('process') else None
        total['buffer_limit_bytes'] = resources.buffer_bytes()
        for component,key in zip(components, ('web','recorder')):
            component['history'] = history.summary((key,))
        for server in rows:
            server['network_history'] = history.summary(('network:'+server['id'],))
            server['incoming_history'] = history.summary(('incoming:'+server['id'],))
        checkpoint()
        processing = store.one('SELECT count(*) AS n FROM report_state WHERE progress IS NOT NULL')
        return {'measured_at': now, 'history':history.summary(('deployment',)),
                'resource_profile':'sbc-1g' if resources.small() else 'standard',
                'processing_active':bool(processing and processing['n']), 'processing_budget_bytes':resources.budget(), 'components': components, 'total': total, 'servers': rows, 'storage': storage(),
                'method': 'OS counters for Harbour web/workers and the network recorder. Cgroup memory includes file cache; process fallback reports RSS only. Recent rates use the latest compatible sample interval. UTC daily counter deltas and their observed coverage are saved every five minutes and survive restarts. Counter resets and missing intervals are excluded.',
                'limits': ['Disk counters exclude unrelated host services, external Docker logging and device-internal flash write amplification.',
                           'Per-server incoming/committed data is logical volume. Shared database reads, indexes, WAL, checkpoints, compaction and archives cannot be assigned as physical I/O to one server.',
                           'Daily totals are measured during the displayed coverage, not extrapolated. Maintenance and imports are included; short recent rates do not predict a typical day or SD-card lifetime.',
                           'Collector counters require readable Linux cgroup v2 service counters. Missing or stale counters are unavailable, not zero. OpenWrt SSH-only router process I/O is unavailable.']}


def reset():
    global latest, storage_baseline
    with lock:
        inputs.clear(); remote.clear(); tracks.clear()
        history.reset()
        latest = storage_baseline = None


def run(stop):
    global latest
    while not stop.is_set():
        try:
            value = collect()
            with lock:
                latest = value
        except Exception:
            # Diagnostics must never stop monitoring or create a noisy error loop.
            pass
        stop.wait(30)
    with lock:
        checkpoint(force=True)


@router.get('')
def report(user=Depends(admin)):
    with lock:
        value = latest
    value = dict(value or collect())
    value['stale'] = time.time()-value['measured_at'] > 90
    return value
