"""Versioned, offline analysis of Harbour incident exports. No network or repair actions.

Run: python -m harbour.incidents incident-*.json > analysis.json
Evidence refs are zero-based indexes into the original bundle's samples array.
"""
import argparse
import bisect
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import sys

VERSION = 1
MAX_SAMPLES = 30000
CORRELATION_SECONDS = 2


def obj(value):
    return value if isinstance(value, dict) else {}


def items(value):
    return value if isinstance(value, list) else []


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def text(value, limit=500):
    return value[:limit] if isinstance(value, str) else ''


def threshold(settings):
    value = obj(settings).get('latency_limit_ms')
    return value if finite(value) and value > 0 else None


def qualifies(row, limit):
    """The recorder's existing trigger, shared with persistence to avoid drift."""
    return (row.get('kind') == 'probe' and row.get('source') == 'recorder'
            and row.get('timing_quality') != 'uncertain'
            and (row.get('status') == 'timeout'
                 or (finite(row.get('rtt_ms')) and limit is not None and row['rtt_ms'] > limit)))


def trigger_evidence(row, settings):
    return {**{key: row.get(key) for key in ('at', 'run_id', 'sample_key', 'source', 'target', 'sequence',
                                            'status', 'rtt_ms', 'timing_quality')},
            'latency_limit_ms': threshold(settings),
            'reason': 'timeout' if row.get('status') == 'timeout' else 'latency',
            'cooldown_seconds': 300}


def percentile(values, quantile=.95):
    return sorted(values)[max(0, math.ceil(len(values)*quantile)-1)] if values else None


def probe_stats(rows, limits):
    attempts = [r for r in rows if r.get('status') in ('reply', 'timeout', 'unobserved')]
    usable = [r for r in attempts if r.get('timing_quality') != 'uncertain' and r.get('status') != 'unobserved']
    ms = [r['rtt_ms'] for r in usable if r.get('status') == 'reply' and finite(r.get('rtt_ms')) and r['rtt_ms'] >= 0]
    timeouts = sum(r.get('status') == 'timeout' for r in usable)
    high = sum(r.get('status') == 'reply' and finite(r.get('rtt_ms')) and limits[r['_ref']] is not None
               and r['rtt_ms'] > limits[r['_ref']] for r in usable)
    return {'attempts': len(attempts), 'usable_attempts': len(usable), 'timeouts': timeouts,
            'timeout_percent': round(100*timeouts/len(usable), 2) if usable else None,
            'high_latency': high, 'uncertain': len(attempts)-len(usable),
            'late_replies': sum(r.get('status') == 'late_reply' for r in rows),
            'p95_ms': percentile(ms), 'peak_ms': max(ms) if ms else None}


def analyze(bundle):
    if not isinstance(bundle, dict) or bundle.get('schema') != 1:
        raise ValueError('Expected a Harbour schema 1 incident JSON export')
    event = obj(bundle.get('incident'))
    if not text(event.get('id')) or not finite(event.get('measured')):
        raise ValueError('Incident id and finite measurement timestamp are required')
    if not isinstance(bundle.get('samples'), list) or len(bundle['samples']) > MAX_SAMPLES:
        raise ValueError(f'An incident must contain at most {MAX_SAMPLES} samples')
    at = event['measured']
    start, end = at-120, at+300
    runs = {text(r.get('id')): obj(r.get('config')) for r in items(bundle.get('runs')) if isinstance(r, dict)}
    rows, seen = [], set()
    invalid = outside = duplicates = 0
    for index, raw in enumerate(bundle['samples']):
        if not isinstance(raw, dict) or not finite(raw.get('at')) or not text(raw.get('kind')):
            invalid += 1
            continue
        if not start <= raw['at'] <= end:
            outside += 1
            continue
        # A child process can restart (and reset ICMP sequences) within a run.
        # Producer sample keys include a process nonce. Legacy fallback also
        # needs the send timestamp; a timeout and late reply remain distinct.
        if text(raw.get('sample_key')):
            identity = 'key:'+raw['sample_key']
        elif raw.get('kind') == 'probe' and text(raw.get('run_id')) and finite(raw.get('sequence')):
            identity = json.dumps([raw['run_id'], raw.get('source'), raw.get('target'), raw['sequence'], raw['at'], raw.get('status')], sort_keys=True)
        else:
            identity = json.dumps({k: v for k, v in raw.items() if k != 'id'}, sort_keys=True)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        rows.append({**raw, '_ref': index})
    rows.sort(key=lambda r: (r['at'], r['_ref']))
    limits, missing_run = {}, 0
    for row in rows:
        cfg = runs.get(text(row.get('run_id')))
        if cfg is None:
            missing_run += 1
        limits[row['_ref']] = threshold(cfg if cfg is not None else bundle.get('settings'))
    probes = [r for r in rows if r['kind'] == 'probe']
    telemetry = [r for r in rows if r['kind'] == 'telemetry' and obj(r.get('metrics'))]
    bad = [r for r in probes if qualifies(r, limits[r['_ref']])]
    times_by_run = defaultdict(list)
    times_by_path = defaultdict(list)
    for row in bad:
        times_by_run[text(row.get('run_id'))].append(row['at'])
        times_by_path[(text(row.get('run_id')), text(row.get('target')))].append(row['at'])

    def near_bad(row, same_target=False):
        run = text(row.get('run_id'))
        if not run:
            return False
        times = times_by_path[(run, text(row.get('target')))] if same_target else times_by_run[run]
        pos = bisect.bisect_left(times, row['at']-CORRELATION_SECONDS)
        return pos < len(times) and times[pos] <= row['at']+CORRELATION_SECONDS

    paths = []
    grouped = defaultdict(list)
    for row in probes:
        grouped[(text(row.get('source')), text(row.get('target')))].append(row)
    for (source, target), observations in sorted(grouped.items()):
        paths.append({'source': source, 'target': target, **probe_stats(observations, limits),
                      'thresholds_ms': sorted({limits[r['_ref']] for r in observations if limits[r['_ref']] is not None}),
                      'before': probe_stats([r for r in observations if r['at'] < at], limits),
                      'after': probe_stats([r for r in observations if r['at'] >= at], limits)})

    findings = []

    def finding(code, title, explanation, evidence, check, category='contributor'):
        if not evidence:
            return
        findings.append({'code': code, 'title': title, 'category': category,
                         'assessment': 'Observed' if category != 'contributor' else 'Possible contributor',
                         'explanation': explanation, 'sample_count': len(evidence),
                         'first_at': evidence[0]['at'], 'last_at': evidence[-1]['at'],
                         'evidence_refs': [r['_ref'] for r in evidence[:6]], 'next_check': check})

    finding('probe_degradation', 'Recorder probe degradation',
            'Usable recorder observations crossed their run threshold or missed the two-second reply deadline. ICMP alone cannot identify an application or ISP fault.',
            bad, 'Compare the same targets from the router and recorder, and check the route each uses.', 'symptom')
    router_bad = [r for r in probes if r.get('source') == 'router' and near_bad(r, same_target=True)
                  and r.get('timing_quality') != 'uncertain' and (r.get('status') == 'timeout' or
                  (r.get('status') == 'reply' and finite(r.get('rtt_ms')) and limits[r['_ref']] is not None and r['rtt_ms'] > limits[r['_ref']]))]
    finding('router_path_degradation', 'Router probes also degraded nearby',
            'Router-originated probes to the same target degraded within two seconds of recorder degradation in the same run. A shared upstream path or target is possible; routing, filtering and ICMP limits remain alternatives.',
            router_bad, 'Confirm WAN/VPN routing and compare affected targets with unaffected targets.')
    healthy_router = [r for r in probes if r.get('source') == 'router' and near_bad(r, same_target=True)
                      and r.get('status') == 'reply' and r.get('timing_quality') != 'uncertain'
                      and finite(r.get('rtt_ms')) and limits[r['_ref']] is not None and r['rtt_ms'] <= limits[r['_ref']]]
    finding('router_reply_nearby', 'Router replies remained below threshold nearby',
            'Some router probes to the same target replied below threshold near recorder degradation. This is a path difference to investigate, not proof that the router or WAN was healthy throughout.',
            healthy_router, 'Match targets and routes; inspect the recorder LAN, container networking and host scheduling.', 'observation')

    correlated = [r for r in telemetry if near_bad(r)
                  and (not finite(r.get('collection_ms')) or r['collection_ms'] <= 2000)
                  and (not finite(obj(r['metrics'].get('openwrt')).get('sample_seconds'))
                       or r['metrics']['openwrt']['sample_seconds'] <= 5)]
    signals = defaultdict(list)
    for row in correlated:
        metrics = row['metrics']
        ow = obj(metrics.get('openwrt'))
        cpus = [v for v in obj(ow.get('cpu_percent')).values() if finite(v)]
        if finite(metrics.get('cpu')):
            cpus.append(metrics['cpu'])
        if cpus and max(cpus) >= 90:
            signals['cpu'].append(row)
        if any(finite(v) and v >= 50 for v in obj(ow.get('softirq_percent')).values()):
            signals['softirq'].append(row)
        memory = obj(metrics.get('memory')).get('percent')
        if finite(memory) and memory >= 90:
            signals['memory'].append(row)
        if any(finite(q.get('backlog_bytes')) and q['backlog_bytes'] >= 65536
               for q in items(ow.get('queue_stats')) if isinstance(q, dict) and q.get('discipline') not in ('noqueue', 'ingress', 'clsact')):
            signals['queue'].append(row)
        if any(finite(v.get(k)) and v[k] > 0 for v in obj(ow.get('interfaces')).values() if isinstance(v, dict)
               for k in ('rx_errors_delta', 'tx_errors_delta', 'rx_dropped_delta', 'tx_dropped_delta')):
            signals['interface'].append(row)
        cfg = runs.get(text(row.get('run_id')), obj(bundle.get('settings')))
        mac = text(cfg.get('client_mac')).lower()
        stations = [s for s in items(ow.get('stations')) if isinstance(s, dict) and (not mac or text(s.get('mac')).lower() == mac)]
        if any((finite(s.get('tx_failed_delta')) and s['tx_failed_delta'] > 0)
               or (finite(s.get('tx_retries_delta')) and finite(s.get('tx_packets_delta'))
                   and s['tx_packets_delta'] >= 10 and s['tx_retries_delta']/s['tx_packets_delta'] >= .25) for s in stations):
            signals['wifi'].append(row)
        if any(finite(v.get('busy_percent')) and v['busy_percent'] >= 85
               for v in obj(ow.get('radio_stats')).values() if isinstance(v, dict)):
            signals['radio'].append(row)
        conntrack = text(ow.get('conntrack')).split()
        if len(conntrack) == 2 and all(v.isdigit() and len(v) < 16 for v in conntrack):
            used, maximum = map(int, conntrack)
            if maximum > 0 and used/maximum >= .9:
                signals['conntrack'].append(row)
    rules = [
        ('cpu', 'cpu_pressure', 'High router CPU near degradation', 'Total CPU or at least one core reached 90%.', 'Compare core utilisation with traffic and offload settings; repeat under a controlled load.'),
        ('softirq', 'softirq_pressure', 'High softirq time near degradation', 'At least one CPU spent 50% or more of its interval in softirq.', 'Compare packet rate, per-core load and interface traffic.'),
        ('memory', 'memory_pressure', 'High memory use near degradation', 'Reported memory use reached 90%. This alone is not evidence of an out-of-memory kill.', 'Inspect memory trends and out-of-memory log entries.'),
        ('queue', 'queue_backlog', 'Software queue backlog near degradation', 'A software queue held at least 64 KiB. This heuristic does not establish link saturation or bufferbloat; offload may bypass accounting.', 'Check which queue is on the affected path, its traffic rate and link capacity.'),
        ('interface', 'interface_errors', 'Interface errors or drops increased nearby', 'Valid interval counters show new errors or drops on an interface. Software drops do not establish a cable fault.', 'Identify the interface in the evidence and compare hardware and switch counters.'),
        ('wifi', 'wifi_retries', 'Wi-Fi retries or failures near degradation', 'A selected client (or any reported client if none was selected) had TX failures, or at least 10 TX packets with retries/packets ≥ 25%. Retries are attempts, not a packet-loss percentage.', 'Match the affected client MAC and radio; compare signal, channel occupancy and a wired test.'),
        ('radio', 'radio_busy', 'Busy radio near degradation', 'An in-use radio reported at least 85% channel busy time. It may include this router’s own traffic.', 'Compare channel occupancy with local traffic and the affected client’s radio.'),
        ('conntrack', 'conntrack_pressure', 'Connection tracking near capacity', 'The reported connection count reached 90% of the configured maximum.', 'Inspect connection growth and table-full log entries before changing limits.'),
    ]
    for key, code, title, explanation, check in rules:
        finding(code, title, explanation+' These readings are within two seconds of a qualifying probe in the same run.', signals[key], check)
    restarts, previous = [], {}
    for row in telemetry:
        run_id = text(row.get('run_id'))
        boot = text(row['metrics'].get('boot_id'))
        if boot and previous.get(run_id) and previous[run_id] != boot:
            restarts.append(row)
        if boot:
            previous[run_id] = boot
    finding('router_reboot', 'Router boot identity changed',
            'Two telemetry readings in one recorder run show different router boot identities. This confirms a reboot in the captured window, but not its cause.',
            restarts, 'Inspect boot, power and watchdog evidence around the change.', 'observation')
    log_events = [r for r in rows if r['kind'] == 'log' and re.search(
        r'link.*(?:down|up)|deauth|disassoc|disconnect|watchdog|out of memory|oom|reset|timed? out|table full', text(r.get('line'), 2000), re.I)]
    finding('router_log_event', 'Router events in the captured window',
            'Selected log lines mention link changes, disconnections, resets or resource failures. Their timestamps are recorder receipt times; buffered logs may arrive late.',
            log_events, 'Read the original lines and source timestamps; match interfaces, clients and probe timing.', 'observation')

    gaps = [r for r in rows if r['kind'] == 'gap']
    health = [r for r in rows if r['kind'] == 'recorder_health']
    loss_increases = 0
    loss_baselines = {}
    for row in health:
        value = row.get('dropped_before_storage')
        if finite(value) and value >= 0:
            run = text(row.get('run_id'))
            if run in loss_baselines and value >= loss_baselines[run]:
                loss_increases += value-loss_baselines[run]
            loss_baselines[run] = value
    quality = []
    if not event.get('complete'):
        quality.append('Window is still collecting or has not been sealed; this analysis can change.')
    if bundle.get('truncated'):
        quality.append('The incident export was truncated; observations outside the saved subset are unknown.')
    if not rows:
        quality.append('No valid samples are available in this incident window.')
    if gaps:
        quality.append(f'{len(gaps)} explicit collection gaps; these are not network timeouts.')
    uncertain = sum(p['uncertain'] for p in paths)
    if uncertain:
        quality.append(f'{uncertain} uncertain or unobserved probes are excluded from RTT and timeout statistics.')
    if not telemetry:
        quality.append('No router telemetry is available to test router-level contributors.')
    if not any(r.get('source') == 'router' for r in probes):
        quality.append('No router-originated probes are available for path comparison.')
    if missing_run:
        quality.append('Some run metadata is missing; export settings are a fallback and may not be the settings at capture time.')
    if any(limits[r['_ref']] is None for r in probes):
        quality.append('Some latency thresholds are unavailable; only deadline misses can be evaluated for those probes.')
    if invalid or outside:
        quality.append(f'{invalid} malformed and {outside} out-of-window samples were excluded.')
    delayed = [r for r in telemetry if (finite(r.get('collection_ms')) and r['collection_ms'] > 2000)
               or (finite(obj(r['metrics'].get('openwrt')).get('sample_seconds')) and r['metrics']['openwrt']['sample_seconds'] > 5)]
    if delayed:
        quality.append(f'{len(delayed)} slow or long-interval telemetry readings were excluded from nearby-contributor rules.')
    if loss_increases:
        quality.append(f'{int(loss_increases)} additional readings were dropped before storage within observed health intervals.')
    elif any(finite(r.get('dropped_before_storage')) and r['dropped_before_storage'] > 0 for r in health):
        quality.append('Recorder reports earlier dropped readings; the window does not locate when those losses occurred.')
    if any((finite(r.get('oldest_unsaved_seconds')) and r['oldest_unsaved_seconds'] > 2)
           or r.get('probe_online') is False or r.get('router_collector_online') is False for r in health):
        quality.append('Recorder health shows storage delay or a collector outage in the window.')

    candidates = [r for r in bad if abs(r['at']-at) < .001] if event.get('automatic') else []
    recorded_trigger = obj(event.get('trigger'))
    trigger = {'mode': 'automatic' if event.get('automatic') else 'manual',
               'verification': 'recorded' if recorded_trigger else 'reconstructed' if event.get('automatic') and candidates else 'unavailable' if event.get('automatic') else 'user marker',
               'evidence': recorded_trigger or (trigger_evidence(candidates[0], {'latency_limit_ms': limits[candidates[0]['_ref']]}) if candidates else None),
               'evidence_refs': [r['_ref'] for r in candidates[:6]]}
    if event.get('automatic') and not candidates:
        quality.append('The triggering probe is absent from the available samples; its absence does not invalidate the marker.')
    elif event.get('automatic') and not recorded_trigger:
        quality.append('Legacy trigger reconstructed from samples; exact trigger metadata was not saved by the older recorder.')
    if rows:
        for source in ('recorder', 'router'):
            for path in [p for p in paths if p['source'] == source]:
                matching = grouped[(source, path['target'])]
                path['first_at'], path['last_at'] = matching[0]['at'], matching[-1]['at']
                # Coverage holes are separate from ICMP timeouts, and never filled.
                holes = 0
                for left, right in zip(matching, matching[1:]):
                    cfg = runs.get(text(left.get('run_id')), obj(bundle.get('settings')))
                    interval = cfg.get('interval') if source == 'recorder' else 1
                    if finite(interval) and right['at']-left['at'] > max(3*interval, 3):
                        holes += 1
                path['sampling_holes'] = holes
        if any(p.get('sampling_holes') for p in paths):
            quality.append('Probe paths contain sampling holes; missing probes are not counted as losses.')
        if rows[0]['at'] > start+5 or rows[-1]['at'] < end-5:
            quality.append('Samples do not span the full requested seven-minute window. Sealed does not mean complete coverage.')
    contributor_count = sum(f['category'] == 'contributor' for f in findings)
    target_count = len({r['target'] for r in bad if isinstance(r.get('target'), str)})
    summary = (f'{len(bad)} qualifying recorder observation'+('s' if len(bad) != 1 else '')+
               f' across {target_count} target'+('s' if target_count != 1 else '')+'. '
               +(f'{contributor_count} possible contributor'+('s' if contributor_count != 1 else '')+' found.'
                 if contributor_count else 'No correlated contributor was identified.')
               if bad else 'No qualifying recorder degradation is present in the available evidence.')
    summary += ' Root cause is not established.'
    timeline_rows = sorted({r['_ref']: r for r in [*bad, *gaps, *log_events, *restarts]}.values(), key=lambda r: (r['at'], r['_ref']))
    refs = {ref for f in findings for ref in f['evidence_refs']} | {r['_ref'] for r in timeline_rows[:100]} | set(trigger['evidence_refs'])
    return {'analysis_schema': 1, 'analyzer_version': VERSION,
            'incident': {k: event.get(k) for k in ('id', 'measured', 'label', 'automatic', 'complete')},
            'server_name': text(bundle.get('server_name')), 'server_id': text(bundle.get('server_id')), 'summary': summary,
            'root_cause': 'not_established', 'trigger': trigger, 'paths': paths, 'findings': findings,
            'quality': quality, 'coverage': {'requested_start': start, 'requested_end': end,
                'first_at': rows[0]['at'] if rows else None, 'last_at': rows[-1]['at'] if rows else None,
                'samples': len(rows), 'kinds': dict(Counter(r['kind'] for r in rows)),
                'duplicates_removed': duplicates, 'invalid_samples': invalid, 'outside_window': outside,
                'dropped_in_observed_intervals': loss_increases},
            'timeline': [{'at': r['at'], 'kind': r['kind'], 'ref': r['_ref'],
                          'description': text(r.get('line') or r.get('detail') or f'{text(r.get("source"))} → {text(r.get("target"))}: {text(r.get("status"))}', 2000)} for r in timeline_rows[:100]],
            'timeline_total': len(timeline_rows),
            'evidence': [{'ref': r['_ref'], 'sample': {k: v for k, v in r.items() if k != '_ref'}} for r in rows if r['_ref'] in refs],
            'method': {'correlation_seconds': CORRELATION_SECONDS, 'evidence_refs': 'Zero-based index in the original samples array',
                       'thresholds': {'cpu_percent': 90, 'softirq_percent': 50, 'memory_percent': 90,
                                      'queue_backlog_bytes': 65536, 'radio_busy_percent': 85,
                                      'wifi_retries_per_packet': .25, 'wifi_min_packets': 10, 'conntrack_percent': 90},
                       'scope': 'Local, deterministic evidence triage; temporal correlation does not prove causation. No external AI service is used.'}}


def compact(report):
    return {k: v for k, v in report.items() if k not in ('evidence', 'timeline')}


def patterns(reports):
    """Count markers and non-overlapping window clusters, never sample counts."""
    unique = {}
    for report in reports:
        key = report['incident']['id']
        unique[key] = report
    ordered = sorted(unique.values(), key=lambda r: r['incident']['measured'])
    clusters, ends, membership = Counter(), {}, {}
    for report in ordered:
        server = report.get('server_id') or report.get('server_name', '')
        window = report['coverage']
        if server not in ends or window['requested_start'] > ends[server]:
            clusters[server] += 1
        ends[server] = max(ends.get(server, window['requested_end']), window['requested_end'])
        membership[id(report)] = (server, clusters[server])
    groups = defaultdict(list)
    titles = {}
    for report in ordered:
        for f in report['findings']:
            groups['finding:'+f['code']].append(report)
            titles['finding:'+f['code']] = f['title']
        for path in report['paths']:
            if path['timeouts'] or path['high_latency']:
                key = 'path:'+path['source']+':'+path['target']
                groups[key].append(report)
                titles[key] = f'{path["source"]} → {path["target"]} degradation'
    result = []
    for key, matches in groups.items():
        result.append({'key': key, 'title': titles[key], 'incidents': len(matches),
                       'window_clusters': len({membership[id(r)] for r in matches}),
                       'incident_ids': [r['incident']['id'] for r in matches],
                       'first_at': matches[0]['incident']['measured'], 'last_at': matches[-1]['incident']['measured']})
    return {'incident_count': len(ordered), 'window_clusters': sum(clusters.values()),
            'patterns': sorted(result, key=lambda p: (-p['incidents'], p['key'])),
            'note': 'Counts describe retained markers, not outage rates. Overlapping seven-minute windows are grouped to avoid treating shared evidence as independent recurrence. Clusters can still belong to one prolonged issue. No unaffected control windows were analysed.'}


def read_bundle(file):
    """Bound file ingestion before decoding; reject nonstandard/nonfinite JSON."""
    def parse_number(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Non-finite JSON number')
        return number

    with Path(file).open('rb') as stream:
        data = stream.read(64*1024*1024+1)
    if len(data) > 64*1024*1024:
        raise ValueError('File exceeds 64 MiB')
    return json.loads(data, parse_float=parse_number, parse_constant=parse_number)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    args = parser.parse_args()
    reports = []
    for file in args.files:
        try:
            reports.append(analyze(read_bundle(file)))
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            parser.exit(2, f'{file}: {exc}\n')
    json.dump({'reports': reports, 'patterns': patterns(reports)}, sys.stdout, allow_nan=False, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
