"""Local, bounded correlation of call-test evidence. No external analysis service."""
import bisect
import json
import time
from collections import defaultdict

from fastapi import HTTPException

from . import call_endpoint, incidents, network, store, resource_limits as resources

MAX_BYTES = 128 * 1024 * 1024
MAX_SAMPLES = 300000
collection_lock = resources.processing_lock


def initialize():
    store.execute('''CREATE TABLE IF NOT EXISTS call_test_evidence (
        test_id TEXT PRIMARY KEY REFERENCES network_call_tests(id) ON DELETE CASCADE,
        payload BLOB NOT NULL)''')


def clock_sample(endpoint_id):
    t0, m0 = time.time(), time.monotonic()
    data = call_endpoint.status(endpoint_id)
    t1, m1 = time.time(), time.monotonic()
    remote = data.get('wall_time')
    if not incidents.finite(remote) or abs((t1-t0)-(m1-m0)) > .1:
        raise ValueError('Clock changed during synchronization')
    return {'local_before': t0, 'local_after': t1, 'remote_at': remote,
            'offset_seconds': remote-(t0+t1)/2, 'uncertainty_seconds': (m1-m0)/2,
            'boot_id': data['boot_id'], 'remote_monotonic': data.get('monotonic')}


def synchronize(endpoint_id):
    readings = [clock_sample(endpoint_id) for _ in range(3)]
    return min(readings, key=lambda r: r['uncertainty_seconds'])


def read(test_id):
    row = store.one('SELECT CASE WHEN length(payload)<=? THEN payload END AS payload FROM call_test_evidence WHERE test_id=?', (resources.budget(),test_id))
    if row and row['payload'] is None:
        raise resources.BudgetExceeded()
    return resources.unpack(row['payload']) if row else None


def compact(result):
    return {k: v for k, v in result.items() if k not in ('timeline', 'router_report')}


def correlate(test, remote):
    matches = {}
    for direction in ('upload', 'download'):
        local = test['result'].get('directions', {}).get(direction, {}).get('raw', {})
        cookie = local.get('start', {}).get('cookie')
        tag = 'harbour:' + test['id'] + ':' + direction
        candidates = [r for r in remote.get('records', []) if isinstance(r, dict)
                      and r.get('test_id') == test['id'] and r.get('direction') == direction]
        if len(candidates) == 1 and cookie and local.get('extra_data') == tag:
            raw = candidates[0].get('raw', {})
            if raw.get('extra_data') == tag and raw.get('start', {}).get('cookie') == cookie:
                matches[direction] = candidates[0]
    return matches


def analyze(test, evidence):
    result = test['result']
    matches = correlate(test, evidence['remote'])
    start, end = evidence['window']['start'], evidence['window']['end']
    began, finished = result.get('load_started'), result.get('load_finished')
    findings, quality, paths, timeline = [], [], [], []
    before, after = result.get('clock_before'), result.get('clock_after')
    clock_ok = bool(before and after and before['boot_id'] == after['boot_id']
                    and abs(before['offset_seconds']-after['offset_seconds']) <=
                    before['uncertainty_seconds']+after['uncertainty_seconds']+.1)
    if not clock_ok:
        quality.append('Remote clock alignment is unavailable or changed; session matching remains independent of clock time.')
    if len(matches) != 2:
        quality.append('Both remote directions have not been verified against local session IDs.')
    if evidence['remote'].get('error'):
        quality.append(evidence['remote']['error'])
    rows, invalid, outside, duplicates = incidents.normalized_samples(evidence['local']['samples'], start, end)
    if not rows:
        quality.append('No recorder evidence is available for this test window.')
    if evidence['local']['truncated']:
        quality.append('Recorder evidence reached its size or row limit; the export is partial.')
    if evidence['local'].get('status'):
        quality.append('Recorder status at collection is included in the export; delayed writes can leave this snapshot incomplete.')
    if any(r['kind'] == 'gap' for r in rows):
        quality.append('The recorder reported collection gaps during this window.')
    if test['status'] != 'complete':
        quality.append('The traffic test did not complete; missing measurements are not a successful result.')
    cfg = evidence['local']['settings']
    runs = {r['id']: r['config'] for r in evidence['local']['runs']}
    limits = {r['_ref']: incidents.threshold(runs.get(r.get('run_id'), cfg)) for r in rows}
    groups = defaultdict(list)
    for row in rows:
        if row['kind'] == 'probe':
            phase = ('baseline' if began is None or row['at'] < began else
                     'recovery' if finished is not None and row['at'] >= finished else 'load')
            groups[(row.get('source'), row.get('target'), phase)].append(row)
    for (source, target, phase), group in sorted(groups.items(), key=lambda x: str(x[0])):
        paths.append({'source': source, 'target': target, 'phase': phase, **incidents.probe_stats(group, limits)})
    comparisons = defaultdict(dict)
    for path in paths:
        comparisons[(path['source'], path['target'])][path['phase']] = path
    for (source, target), phases in comparisons.items():
        baseline, load = phases.get('baseline'), phases.get('load')
        if baseline and load and baseline['usable_attempts'] >= 10 and load['usable_attempts'] >= 10:
            if baseline['p95_ms'] is not None and load['p95_ms'] is not None and load['p95_ms']-baseline['p95_ms'] >= 20:
                findings.append({'kind': 'coincidence', 'text': f'{source} → {target}: p95 RTT rose from {baseline["p95_ms"]:.1f} to {load["p95_ms"]:.1f} ms during load. Queueing is a possible contributor, not a proven cause.'})
    bad_probes = [r for r in rows if incidents.qualifies(r, limits[r['_ref']])]
    bad_times = [r['at'] for r in bad_probes]
    directions = []
    for name in ('upload', 'download'):
        direction = result.get('directions', {}).get(name, {})
        summary = direction.get('summary', {})
        directions.append({'direction': name, 'receiver': direction.get('receiver'), 'summary': summary,
                           'remote_match': name in matches})
        loss, jitter = summary.get('lost_percent'), summary.get('jitter_ms')
        if incidents.finite(loss) and loss > 0:
            findings.append({'kind': 'observation', 'text': f'{name.title()} receiver measured {loss:.2f}% UDP packet loss.'})
        if incidents.finite(jitter) and jitter > 30:
            findings.append({'kind': 'observation', 'text': f'{name.title()} receiver summary jitter was {jitter:.1f} ms, above Harbour’s 30 ms review threshold.'})
        # The connection timestamp precedes actual packet flow. Bound the flow
        # start between connection time and result-observation minus measured
        # duration, retaining handshake/output delay as uncertainty.
        raw = direction.get('raw', {}) if name == 'download' else matches.get(name, {}).get('raw', {})
        timestamp = raw.get('start', {}).get('timestamp', {})
        lower = (timestamp['timemillisecs']/1000 if incidents.finite(timestamp.get('timemillisecs'))
                 else timestamp.get('timesecs'))
        interval_ends = [i.get('sum', {}).get('end') for i in raw.get('intervals', []) if isinstance(i, dict)]
        interval_ends = [x for x in interval_ends if incidents.finite(x)]
        observed = (direction.get('process_finished', finished) if name == 'download'
                    else matches.get(name, {}).get('at'))
        if not incidents.finite(lower) or not incidents.finite(observed) or not interval_ends:
            continue
        upper = observed-max(interval_ends)
        if name == 'upload':
            if not clock_ok or matches.get(name, {}).get('boot_id') != before['boot_id']:
                continue
            lower -= max(c['offset_seconds']+c['uncertainty_seconds'] for c in (before, after))
            upper -= min(c['offset_seconds']-c['uncertainty_seconds'] for c in (before, after))
        if upper < lower:
            quality.append(name.title()+' timing bounds are inconsistent; interval correlation is unavailable.')
            continue
        anchor, uncertainty = (lower+upper)/2, (upper-lower)/2+.001
        for interval_index, interval in enumerate(raw.get('intervals', [])[:7202]):
            stats = interval.get('sum', {})
            if stats.get('sender') is not False or stats.get('omitted') or not incidents.finite(stats.get('start')) or not incidents.finite(stats.get('end')):
                continue
            a, b = anchor+stats['start'], anchor+stats['end']
            left, right = bisect.bisect_left(bad_times, a-uncertainty), bisect.bisect_right(bad_times, b+uncertainty)
            affected = [p['_ref'] for p in bad_probes[left:min(right,left+100)]]
            timeline.append({'direction': name, 'start': a, 'end': b, 'uncertainty_seconds': uncertainty,
                             'interval_index': interval_index,
                             'packets': stats.get('packets'), 'lost_packets': stats.get('lost_packets'),
                             'bits_per_second': stats.get('bits_per_second'),
                             'lost_percent': stats.get('lost_percent'), 'jitter_ms': stats.get('jitter_ms'),
                             'recorder_degradation_refs': affected, 'recorder_degradation_count': right-left})
    overlaps = [r for r in timeline if incidents.finite(r.get('lost_percent')) and r['lost_percent'] > 0 and r['recorder_degradation_count']]
    jitter_spikes = [r for r in timeline if incidents.finite(r.get('jitter_ms')) and r['jitter_ms'] > 30]
    if jitter_spikes:
        findings.append({'kind': 'observation', 'text': f'{len(jitter_spikes)} receiver intervals exceeded 30 ms jitter; the worst reported {max(r["jitter_ms"] for r in jitter_spikes):.1f} ms. Short spikes can be hidden by the final summary.'})
    if overlaps:
        findings.append({'kind': 'coincidence', 'text': f'{len(overlaps)} lossy receiver intervals overlap recorder degradation within the stated timing uncertainty. This does not locate the fault.'})
    report = None
    if began is not None:
        windows = []
        cursor = start
        # Apply the established incident analyzer to consecutive 7-minute
        # windows, covering the whole hour instead of just the initial load.
        while cursor <= end:
            window_end = min(cursor+420, end)
            subset = [r for r in evidence['local']['samples'] if cursor <= r['at'] < window_end
                      or r['at'] == end == window_end]
            windows.append(incidents.analyze({'schema': 1, 'incident': {'id': test['id'], 'measured': cursor+120,
                        'label': 'Remote call flow test', 'complete': test['status'] != 'running'},
                        'samples': subset[:60000], 'settings': cfg, 'runs': evidence['local']['runs'],
                        'truncated': evidence['local']['truncated'] or len(subset)>60000, 'server_id': test['server_id'],
                        'server_name': evidence['local']['server_name'],
                        'traffic_tests': [{'result': {'load_started': began, 'load_finished': finished}}]}))
            cursor += 420
        report = {'windows': windows, 'findings': list({(f['title'],f['assessment']): f
                   for window in windows for f in window['findings']}.values()),
                  'scope': 'Consecutive incident-analysis windows covering the entire test; evidence refs address each window’s samples.'}
    complete_receivers = all(d['summary'].get('packets', 0) for d in directions)
    headline = ('Receiver evidence is incomplete.' if not complete_receivers else
                'Receiver impairment was recorded.' if jitter_spikes or any(incidents.finite(d['summary'].get('lost_percent')) and d['summary']['lost_percent'] > 0
                    or incidents.finite(d['summary'].get('jitter_ms')) and d['summary']['jitter_ms'] > 30 for d in directions) else
                'No packet loss or summary jitter above 30 ms was recorded by the two receivers.')
    return {'schema': 1, 'summary': headline, 'root_cause': 'not_established', 'findings': findings,
            'quality': quality, 'directions': directions, 'paths': paths, 'timeline': timeline,
            'clock_aligned': clock_ok, 'remote_matches': sorted(matches), 'router_report': report,
            'sample_counts': {'valid': len(rows), 'invalid': invalid, 'duplicates': duplicates, 'outside': outside},
            'method': 'Run ID + direction + iperf cookie establish identity. Clock probes estimate alignment; connection-to-result timing brackets retain timestamp, handshake and output delay uncertainty in interval comparisons. Summary jitter >30 ms and p95 RTT rise ≥20 ms are Harbour review heuristics, not vendor certification or MOS. Correlation does not establish root cause.'}


def collect(test):
    if not collection_lock.acquire(blocking=False):
        raise HTTPException(409, 'Evidence collection is busy; retry shortly')
    try:
        return _collect(test)
    finally:
        collection_lock.release()


def _collect(test):
    previous = read(test['id'])
    remote = previous['remote'] if previous else {'records': []}
    endpoint_id = test['config'].get('endpoint_id')
    if endpoint_id:
        try:
            data = json.loads(call_endpoint.fetch(endpoint_id, '/runs/' + test['id'], min(call_endpoint.LIMIT, resources.budget()//2)))
            if not isinstance(data, dict) or data.get('test_id') != test['id'] or not isinstance(data.get('records'), list) or len(data['records']) > 2:
                raise ValueError()
            for record in data['records']:
                if (not isinstance(record, dict) or record.get('test_id') != test['id']
                        or record.get('direction') not in ('upload', 'download') or not isinstance(record.get('raw'), dict)):
                    raise ValueError()
                raw = record['raw']
                if (not isinstance(raw.get('start', {}), dict) or not isinstance(raw.get('end', {}), dict)
                        or not isinstance(raw.get('intervals', []), list) or len(raw.get('intervals', [])) > 7202
                        or any(not isinstance(i, dict) or not isinstance(i.get('sum', {}), dict) for i in raw.get('intervals', []))):
                    raise ValueError()
            json.dumps(data, allow_nan=False)
            if len(data['records']) >= len(remote['records']):
                remote = data
        except (HTTPException, ValueError, UnicodeError):
            remote = {**remote, 'error': 'Remote retrieval was incomplete. Retry collection while the endpoint retains the dated logs.'}
    else:
        remote = {**remote, 'error': 'This test used an unmanaged endpoint; independent remote logs are unavailable.'}
    server = network.host(test['server_id'])
    start, end = test['created'], test['finished'] or time.time()
    rows, truncated = network.samples(test['server_id'], start, end, MAX_SAMPLES)
    if truncated:
        raise resources.BudgetExceeded()
    # Merge earlier snapshots so a retry after recorder retention never erases
    # already collected local evidence. Stable DB row IDs identify snapshots.
    indexed = {r['id']: r for r in (previous['local']['samples'] if previous else [])}
    indexed.update({r['id']: r for r in rows})
    rows, used = [], 0
    for row in sorted(indexed.values(), key=lambda r: (r['at'], r['id'])):
        used += len(json.dumps(row).encode())
        if len(rows) >= MAX_SAMPLES or used > min(MAX_BYTES, resources.budget()//2):
            raise resources.BudgetExceeded()
        rows.append(row)
    settings = previous['local']['settings'] if previous else network.config(test['server_id'])[0]
    status = store.one('SELECT updated,payload FROM network_status WHERE server_id=?', (test['server_id'],))
    evidence = {'schema': 1, 'test_id': test['id'], 'collected_at': time.time(), 'window': {'start': start, 'end': end},
                'remote': remote, 'local': {'server_name': server['name'], 'samples': rows,
                    'settings': settings, 'runs': network.run_metadata(test['server_id'], rows),
                    'status': {'updated': status['updated'], **json.loads(status['payload'])} if status else None,
                    'truncated': truncated or bool(previous and previous['local']['truncated'])}}
    if previous:
        evidence['local']['runs'] = list({r['id']: r for r in [*previous['local']['runs'], *evidence['local']['runs']]}.values())
    evidence['analysis'] = analyze(test, evidence)
    payload = resources.pack(evidence)
    store.execute('INSERT INTO call_test_evidence(test_id,payload) SELECT id,? FROM network_call_tests WHERE id=? '
                  'ON CONFLICT(test_id) DO UPDATE SET payload=excluded.payload', (payload, test['id']))
    if test.get('status') != 'running':
        # Derived conclusions survive normal raw-test retention. Analysis errors
        # must never prevent the original evidence being retained/downloaded.
        try:
            from . import network_diagnostics
            network_diagnostics.cache_call(test, evidence)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Connection analysis deferred; saved call evidence remains available')
    return evidence
