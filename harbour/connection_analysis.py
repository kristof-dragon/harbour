"""Deterministic, offline ping/UDP conclusions. No probes, repairs or model calls."""
import bisect
import hashlib
import json
import math
from collections import Counter, defaultdict

from . import incidents

VERSION = 3
RULES = {'jitter_ms': 30, 'rtt_rise_ms': 20, 'max_alignment_seconds': 1,
         'minimum_coverage': .8, 'sender_shortfall_fraction': .9,
         'cpu_percent': 90, 'softirq_percent': 50, 'memory_percent': 90,
         'queue_bytes': 65536, 'radio_busy_percent': 85, 'conntrack_fraction': .9}
EXCLUSIONS = [
    'The synthetic UDP test originates at Harbour. It does not establish the affected phone/laptop or Wi-Fi media experience.',
    'DNS, application signalling, media relays, codec/FEC behaviour and actual call quality are not measured.',
    'Remote endpoint CPU, scheduling and socket drops are not collected; receiver loss does not establish an ISP fault.',
    'One-second receiver aggregates cannot reconstruct exact packet-loss bursts, media freezes or one-way latency. Jitter is a receiver estimator.',
    'Router-originated and forwarded traffic can use different routes, queues and policies. Same destination does not prove an identical path.',
    'Temporal association and repeated symptoms do not prove causation. No automatic repair is performed.',
]
LABELS = {'both': 'Ping and call-flow impairment', 'udp_only': 'Call-flow impairment with normal endpoint ping',
          'ping_only': 'Ping impairment with normal call flow', 'udp_unconfirmed': 'Call-flow impairment; ping comparison incomplete',
          'ping_unconfirmed': 'Ping impairment; call-flow comparison incomplete',
          'neither': 'Neither test observed impairment', 'insufficient': 'Insufficient comparison evidence'}
SIGNALS = {'cpu': ('Router CPU pressure', 'Compare core load, traffic and forwarding/offload configuration.'),
           'softirq': ('Router softirq pressure', 'Compare packet rate and per-core networking work.'),
           'memory': ('Router memory pressure', 'Check memory trends and original out-of-memory logs.'),
           'queue': ('Software queue backlog', 'Compare delay, traffic and recovery. Queue drops alone are not a fault.'),
           'interface': ('Interface errors or drops', 'Identify the interface and compare hardware/switch counters; software drops do not prove a cable fault.'),
           'wifi': ('Selected Wi-Fi client retries/failures', 'Repeat from the affected client and a wired reference. This is association, not proof that Harbour traversed that radio.'),
           'radio': ('Busy radio', 'Match the affected client and radio; channel busy time can include this router’s own traffic.'),
           'conntrack': ('Connection tracking pressure', 'Compare table growth and connection-setup failures.'),
           'reboot': ('Router reboot observed', 'Compare boot, watchdog and power evidence; a changed boot ID does not identify its cause.'),
           'log': ('Router link/disconnection events', 'Inspect source log time and interface/client. Receipt time cannot establish exact ordering.')}


def pick(value, keys):
    return {k: value[k] for k in keys.split() if isinstance(value, dict) and k in value}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def valid(value):
    return incidents.finite(value) and value >= 0


def union_seconds(ranges):
    total, edge = 0, None
    for a, b in sorted(ranges):
        if b <= a:
            continue
        total += b-max(a, edge if edge is not None else a) if edge is None or b > edge else 0
        edge = max(edge if edge is not None else b, b)
    return max(0, total)


class Evidence:
    def __init__(self, limit=1200):
        self.rows = {}
        self.omitted = 0
        self.limit = limit

    def add(self, key, value):
        if key in self.rows:
            return key
        if self.limit is not None and len(self.rows) >= self.limit:
            self.omitted += 1
            return None
        self.rows[key] = value
        return key

    def sample(self, row):
        # Bounded measured excerpts, never a saved SSH connection or token.
        excerpt = pick(row, 'at kind source target status rtt_ms timing_quality run_id sample_key collection_ms detail reader_delay_ms send_lateness_ms send_duration_ms dropped_before_storage')
        if row.get('kind') == 'telemetry':
            m = incidents.obj(row.get('metrics'))
            ow = incidents.obj(m.get('openwrt'))
            excerpt['metrics'] = pick(m, 'cpu boot_id memory')
            excerpt['metrics']['openwrt'] = pick(ow, 'cpu_percent softirq_percent sample_seconds conntrack')
            for key in ('interfaces', 'radio_stats'):
                excerpt['metrics']['openwrt'][key] = dict(list(incidents.obj(ow.get(key)).items())[:12])
            for key in ('queue_stats', 'stations'):
                excerpt['metrics']['openwrt'][key] = incidents.items(ow.get(key))[:12]
        if row.get('kind') == 'log':
            excerpt['line'] = str(row.get('line', ''))[:600]
        excerpt['source_sample_index'] = row['_ref']
        return self.add('sample:'+str(row['_ref']), excerpt)


def telemetry_signals(row, config):
    m = incidents.obj(row.get('metrics'))
    ow = incidents.obj(m.get('openwrt'))
    result = set()
    if any(valid(n) and n >= 90 for n in [m.get('cpu'), *incidents.obj(ow.get('cpu_percent')).values()]):
        result.add('cpu')
    if any(valid(n) and n >= 50 for n in incidents.obj(ow.get('softirq_percent')).values()):
        result.add('softirq')
    if valid(incidents.obj(m.get('memory')).get('percent')) and m['memory']['percent'] >= 90:
        result.add('memory')
    if any(valid(q.get('backlog_bytes')) and q['backlog_bytes'] >= 65536
           for q in incidents.items(ow.get('queue_stats')) if isinstance(q, dict) and q.get('discipline') not in ('noqueue', 'ingress', 'clsact')):
        result.add('queue')
    if any(valid(v.get(k)) and v[k] > 0 for v in incidents.obj(ow.get('interfaces')).values() if isinstance(v, dict)
           for k in ('rx_errors_delta', 'tx_errors_delta', 'rx_dropped_delta', 'tx_dropped_delta')):
        result.add('interface')
    mac = str(config.get('client_mac', '')).lower()
    if mac and any((valid(s.get('tx_failed_delta')) and s['tx_failed_delta'] > 0) or
                   (valid(s.get('tx_packets_delta')) and s['tx_packets_delta'] >= 10 and valid(s.get('tx_retries_delta'))
                    and s['tx_retries_delta']/s['tx_packets_delta'] >= .25)
                   for s in incidents.items(ow.get('stations')) if isinstance(s, dict) and str(s.get('mac', '')).lower() == mac):
        result.add('wifi')
    if any(valid(v.get('busy_percent')) and v['busy_percent'] >= 85 for v in incidents.obj(ow.get('radio_stats')).values() if isinstance(v, dict)):
        result.add('radio')
    conn = str(ow.get('conntrack', '')).split()
    if len(conn) == 2 and all(v.isdigit() and len(v) < 16 for v in conn) and int(conn[1]) > 0 and int(conn[0])/int(conn[1]) >= .9:
        result.add('conntrack')
    return result


def healthy_probe(row, config):
    limit = incidents.threshold(config)
    return (limit is not None and row.get('status') == 'reply' and row.get('timing_quality') != 'uncertain'
            and valid(row.get('rtt_ms')) and row['rtt_ms'] <= limit)


def probe_coverage(rows, start, end, configs, *, healthy=False):
    """Cadence-covered time, so clustered or duplicate replies cannot fill holes."""
    ranges = []
    for row in rows:
        config = configs.get(row.get('run_id'))
        if not config or row.get('timing_quality') == 'uncertain':
            continue
        if healthy and not healthy_probe(row, config):
            continue
        if row.get('status') not in ('reply', 'timeout'):
            continue
        if row.get('status') == 'reply' and not valid(row.get('rtt_ms')):
            continue
        cadence = 1 if row.get('source') == 'router' else config.get('interval')
        if valid(cadence) and .1 <= cadence <= 2:
            ranges.append((max(start, row['at']), min(end, row['at']+cadence)))
    return union_seconds(ranges)


def impaired(stats):
    return ((valid(stats.get('lost_percent')) and stats['lost_percent'] > 0)
            or (valid(stats.get('lost_packets')) and stats['lost_packets'] > 0)
            or (valid(stats.get('jitter_ms')) and stats['jitter_ms'] > RULES['jitter_ms']))


def measured(stats):
    return (valid(stats.get('packets')) and stats['packets'] > 0 and valid(stats.get('lost_percent'))
            and valid(stats.get('jitter_ms')))


def receiver_intervals(test, evidence):
    """Retain unaligned receiver measurements without inventing absolute times."""
    from .call_evidence import correlate
    matches = correlate(test, evidence.get('remote', {}))
    anchored = {(i['direction'], i.get('interval_index')): i for i in evidence.get('analysis', {}).get('timeline', [])}
    result = []
    for name in ('upload', 'download'):
        d = test.get('result', {}).get('directions', {}).get(name, {})
        raw = d.get('raw', {}) if name == 'download' else matches.get(name, {}).get('raw', d.get('raw', {}).get('server_output_json', {}))
        for index, interval in enumerate(raw.get('intervals', [])[:7202]):
            stats = interval.get('sum', {})
            if stats.get('sender') is not False or stats.get('omitted') or not valid(stats.get('start')) or not valid(stats.get('end')) or stats['end'] <= stats['start']:
                continue
            absolute = anchored.get((name, index), {})
            aligned = bool(absolute and valid(absolute.get('start')) and valid(absolute.get('end'))
                           and absolute['end'] > absolute['start'] and valid(absolute.get('uncertainty_seconds'))
                           and absolute['uncertainty_seconds'] <= RULES['max_alignment_seconds'])
            item = {**pick(stats, 'packets lost_packets lost_percent jitter_ms bits_per_second'), 'direction': name,
                    'relative_start': stats['start'], 'relative_end': stats['end'],
                    'start': absolute.get('start'), 'end': absolute.get('end'),
                    'uncertainty_seconds': absolute.get('uncertainty_seconds'), 'aligned': aligned,
                    'interval_index': index, 'impaired': impaired(stats), 'measured': measured(stats)}
            result.append(item)
    return result, matches


def analyze(source, identity, server, start, end, samples, runs, settings, *, test=None, evidence=None, quality=None):
    """Analyze one saved window; all returned sample refs address its input array."""
    rows, invalid, outside, duplicates = incidents.normalized_samples(samples, start, end)
    proof, issues = Evidence(), list(quality or [])
    configs = {r['id']: r.get('config', {}) for r in runs if r.get('id')}
    cfg = lambda r: configs.get(r.get('run_id'), settings)
    probes = [r for r in rows if r.get('kind') == 'probe']
    probe_groups = defaultdict(list)
    for row in probes:
        probe_groups[(row.get('source'), row.get('target'))].append(row)
    probe_summary = []
    limits = {r['_ref']: incidents.threshold(cfg(r)) for r in probes}
    for (origin, target), group in sorted(probe_groups.items(), key=lambda item: str(item[0])):
        stats = {'source': origin, 'target': target, **incidents.probe_stats(group, limits),
                 'latency_thresholds_ms': sorted({limits[r['_ref']] for r in group if limits[r['_ref']] is not None}),
                 'method': 'All deduplicated original probe observations in this saved window; timeout percentages exclude uncertain/unobserved attempts.'}
        ref = proof.add('probe-summary:'+str(len(probe_summary)), stats)
        examples = [group[0], group[-1]]
        examples += [r for r in group if incidents.qualifies(r, limits[r['_ref']])][:1]
        refs = ([ref] if ref else [])+[ref for row in examples if (ref := proof.sample(row))]
        probe_summary.append({**stats, 'evidence_refs': list(dict.fromkeys(refs))})
    bad = [r for r in probes if incidents.qualifies(r, incidents.threshold(cfg(r)))]
    probe_times = [r['at'] for r in probes]
    gaps = [r for r in rows if r.get('kind') == 'gap']
    bad_ranges = []
    for index, r in enumerate(bad):
        cadence = cfg(r).get('interval', 1)
        cadence = cadence if valid(cadence) and .1 <= cadence <= 2 else 1
        bad_ranges.append({'start': r['at'], 'end': min(end, r['at']+cadence), 'ping': True,
                           'run_id': r.get('run_id'), 'ref': proof.sample(r) if index < 300 else None, 'target': r.get('target'), 'sample': r})
    proof.omitted += max(0, len(bad)-300)
    intervals, matches = receiver_intervals(test, evidence) if test else ([], {})
    endpoint = test.get('config', {}).get('endpoint') if test else None
    receiver_examples = 0
    for i in intervals:
        if i['aligned'] and not (start <= i['start'] < i['end'] <= end):
            i['aligned'] = False
        if i['impaired']:
            i['ref'] = proof.add('receiver:'+i['direction']+':'+str(i['interval_index']), dict(i)) if receiver_examples < 300 else None
            proof.omitted += int(receiver_examples >= 300)
            receiver_examples += 1
            if i['aligned']:
                bad_ranges.append({'start': i['start'], 'end': i['end'], 'ping': False,
                                   'direction': i['direction'], 'ref': i['ref'], 'uncertainty': i['uncertainty_seconds'], 'interval': i})
    episodes = []
    for r in sorted(bad_ranges, key=lambda r: (r['start'], r['end'])):
        if not episodes or r['start'] > episodes[-1]['end']+.001:
            episodes.append({'id': len(episodes)+1, 'start': r['start'], 'end': r['end'], 'members': []})
        episodes[-1]['end'] = max(episodes[-1]['end'], r['end'])
        episodes[-1]['members'].append(r)
    interval_by_direction = {d: sorted([i for i in intervals if i['direction'] == d and i['aligned']], key=lambda i: i['start']) for d in ('upload', 'download')}
    interval_times = {d: [i['start'] for i in values] for d, values in interval_by_direction.items()}
    interval_widths = {d: max((i['end']-i['start'] for i in values), default=0) for d, values in interval_by_direction.items()}
    for episode in episodes:
        a, b = episode['start'], episode['end']
        near = probes[bisect.bisect_left(probe_times, a-2):bisect.bisect_left(probe_times, b)]
        relevant = [r for r in near if r.get('source') == 'recorder' and (not endpoint or r.get('target') == endpoint)]
        run_ids = {r.get('run_id') for r in near if r.get('run_id') in configs}
        episode['run_ids'] = sorted(run_ids)
        episode['directions'] = sorted({r['direction'] for r in episode['members'] if not r['ping']})
        episode['ping_targets'] = sorted({r.get('target', '') for r in episode['members'] if r['ping']})
        ping_normal = bool(endpoint and relevant and len(run_ids) == 1 and
                           probe_coverage(relevant, a, b, configs, healthy=True) >= (b-a)*.8
                           and all(healthy_probe(r, cfg(r)) for r in relevant if r['at'] >= a)
                           and not any(a <= g['at'] <= b for g in gaps))
        udp_normal = bool(test)
        normal_refs = []
        for d, values in interval_by_direction.items():
            right = bisect.bisect_left(interval_times[d], b)
            left = bisect.bisect_left(interval_times[d], a-interval_widths[d])
            overlap = [i for i in values[left:right] if i['end'] > a]
            covered = union_seconds([(max(a, i['start']), min(b, i['end'])) for i in overlap if i['measured'] and not i['impaired']])
            if covered < .8*(b-a) or not overlap or any(i['impaired'] for i in overlap):
                udp_normal = False
            normal_refs.extend(overlap[:2])
        # Same-endpoint corroboration is distinct from unrelated target failures.
        ping_bad = any(r['ping'] and (not endpoint or r.get('target') == endpoint) for r in episode['members'])
        udp_bad = bool(episode['directions'])
        classification = ('both' if ping_bad and udp_bad else 'udp_only' if udp_bad and ping_normal else
                          'udp_unconfirmed' if udp_bad else 'ping_only' if udp_normal else 'ping_unconfirmed')
        episode.update(classification=classification, conclusion=LABELS[classification],
                       sampled_span_seconds=round(b-a, 3), uncertainty_seconds=max((r.get('uncertainty', 0) for r in episode['members']), default=0),
                       evidence_refs=list(dict.fromkeys(r['ref'] for r in episode['members'] if r.get('ref')))[:8],
                       observations=len(episode['members']), assessment='Observed',
                       limitation='Span describes sampled observations, not exact continuous outage duration.')
        if ping_normal:
            episode['evidence_refs'] += [ref for r in relevant[:3] if (ref := proof.sample(r))]
        if udp_normal:
            episode['evidence_refs'] += [ref for i in normal_refs if (ref := proof.add('receiver:'+i['direction']+':'+str(i['interval_index']), dict(i)))]
        # Every identified episode retains at least one actual observation even
        # after the general example budget is reached. Daily reports must not
        # silently lose later episodes from a long-running test.
        if not any(member.get('ref') for member in episode['members']):
            member = episode['members'][0]
            selected = Evidence(limit=None)
            if member['ping']:
                ref = selected.sample(member['sample'])
            else:
                value = member['interval']
                ref = selected.add('receiver:'+value['direction']+':'+str(value['interval_index']), dict(value))
            proof.rows.update(selected.rows)
            episode['evidence_refs'].append(ref)
        del episode['members']
    directions = []
    for d in ('upload', 'download') if test else ():
        values = [i for i in intervals if i['direction'] == d]
        summary = test['result'].get('directions', {}).get(d, {}).get('summary', {})
        summary_ref = proof.add('summary:'+d, {'direction': d, **pick(summary, 'packets lost_packets lost_percent jitter_ms bits_per_second seconds')})
        directions.append({'direction': d, 'summary': pick(summary, 'packets lost_packets lost_percent jitter_ms bits_per_second'),
                           'remote_match': d in matches, 'receiver_intervals': len(values),
                           'measured_intervals': sum(i['measured'] for i in values), 'unaligned_intervals': sum(not i['aligned'] for i in values),
                           'impaired_intervals': sum(i['impaired'] for i in values),
                           'affected_seconds': union_seconds([(i['relative_start'], i['relative_end']) for i in values if i['impaired'] and i['measured']]),
                           'unquantified_impaired_intervals': sum(i['impaired'] and not i['measured'] for i in values),
                           'observed_seconds': union_seconds([(i['relative_start'], i['relative_end']) for i in values if i['measured']]),
                           'peak_interval_jitter_ms': max((i['jitter_ms'] for i in values if valid(i.get('jitter_ms'))), default=None),
                           'impairment_observed': impaired(summary) or any(i['impaired'] for i in values),
                           'evidence_refs': ([summary_ref] if summary_ref else [])+[i['ref'] for i in values if i.get('ref')][:3]})
        if any(i['impaired'] and not i['aligned'] for i in values):
            issues.append(d.title()+' receiver impairment exists, but timing uncertainty prevents joining those intervals to router/ping episodes.')
    contributors, path_findings = correlate_local(rows, probes, episodes, configs, endpoint, proof)
    phases, delivery = compare_load(test, evidence, matches, proof) if test else ([], [])
    if gaps:
        issues.append(f'{len(gaps)} collection gaps; missing readings are not loss or healthy observations.')
        for r in gaps[:3]:
            proof.sample(r)
    if any(r.get('timing_quality') == 'uncertain' or r.get('status') == 'unobserved' for r in probes):
        issues.append('Uncertain or unobserved probes were excluded from healthy/degraded decisions.')
    if any(incidents.threshold(cfg(r)) is None for r in probes):
        issues.append('Some original latency thresholds are missing; only explicit timeouts establish ping impairment.')
    health = [r for r in rows if r['kind'] == 'health']
    if any((valid(r.get('dropped_before_storage')) and r['dropped_before_storage'] > 0) or
           (valid(r.get('oldest_unsaved_seconds')) and r['oldest_unsaved_seconds'] > 2) or
           r.get('probe_online') is False or r.get('router_collector_online') is False for r in health):
        issues.append('Recorder health reports dropped/delayed readings or a collector outage; missing evidence is unknown.')
        for r in health[:3]:
            proof.sample(r)
    if any(r.get('run_id') not in configs for r in rows):
        issues.append('Some observations lack their original run context; cross-source attribution is restricted.')
    if test and test.get('status') != 'complete':
        issues.append('This call-flow test did not complete. Available measurements remain observations, not a passed test.')
    if test and any(d['receiver_intervals'] == 0 or d['unaligned_intervals'] for d in directions):
        issues.append('Receiver interval timing/coverage is incomplete; whole-test summaries cannot supply missing interval evidence.')
    if invalid or outside:
        issues.append(f'{invalid} malformed and {outside} out-of-window samples excluded.')
    load_start = test['result'].get('load_started') if test else None
    load_end = test['result'].get('load_finished') if test else None
    endpoint_probes = [r for r in probes if r.get('source') == 'recorder' and endpoint and r.get('target') == endpoint]
    ping_coverage = (probe_coverage(endpoint_probes, load_start, load_end, configs)/(load_end-load_start)
                     if valid(load_start) and valid(load_end) and load_end > load_start else 0)
    if test and ping_coverage < .8:
        issues.append('Endpoint ping covers less than 80% of the loading period; a healthy combined comparison is unavailable.')
    symptoms = bool(bad or any(d['impairment_observed'] for d in directions))
    complete = bool(test and test.get('status') == 'complete' and not issues and endpoint and directions
                    and all(d['observed_seconds'] >= test['config'].get('duration', math.inf)*.8 for d in directions)
                    and ping_coverage >= .8)
    conclusion = ('Impairment was observed. ' + ', '.join(sorted({LABELS[e['classification']] for e in episodes})) if episodes else
                  'Receiver impairment was observed; precise ping/router correlation is unavailable.' if symptoms else
                  'Neither test observed impairment in the covered periods.' if complete else
                  'Available evidence does not establish a healthy combined test.')
    context = pick(test.get('config', {}), 'profile profile_snapshot endpoint port upload_kbps download_kbps packet_bytes duration baseline recovery schedule_id schedule_name') if test else {}
    observers = [pick(r, 'id started observer config') for r in runs]
    # Only non-secret run context is stored, not arbitrary caller configuration.
    for r in observers:
        r['config'] = pick(r.get('config', {}), 'interval targets router_probes wan_device client_mac latency_limit_ms')
        r['observer'] = pick(r.get('observer', {}), 'host hostname platform boot_id') if isinstance(r.get('observer'), dict) else str(r.get('observer', ''))[:200]
    cohort_basis = {'analyzer_version': VERSION, **pick(context, 'profile profile_snapshot endpoint port upload_kbps download_kbps packet_bytes duration baseline recovery'),
                    'recorder_contexts': sorted({json.dumps(r['config'], sort_keys=True) for r in observers}),
                    'observers': sorted({json.dumps(r['observer'], sort_keys=True) for r in observers})}
    route_snapshots = {}
    for row in rows:
        ow = incidents.obj(incidents.obj(row.get('metrics')).get('openwrt'))
        if row.get('kind') == 'telemetry' and ow.get('routes'):
            snapshot = pick(ow, 'routes rules software_offload hardware_offload')
            route_snapshots[digest(snapshot)] = snapshot
    route_contexts = sorted(route_snapshots)
    for key in route_contexts[:8]:
        proof.add('route:'+key, {k: str(v)[:8000] if v is not None else None for k, v in route_snapshots[key].items()})
    if len(route_contexts) > 8:
        issues.append('Routing context changed more than eight times; route evidence examples are limited to eight snapshots.')
    cohort_basis['route_contexts'] = route_contexts
    cohort_known = bool(test and route_contexts and observers and all(r['observer'] and r['config'] for r in observers))
    if test and not cohort_known:
        issues.append('Observer or routing context is missing; this report is excluded from comparable recurring-test groups.')
    return {'schema': 1, 'analyzer_version': VERSION, 'id': source+':'+identity, 'source': source, 'source_id': identity,
            'server_id': server['id'], 'server_name': server['name'], 'start': start, 'end': end,
            'status': test.get('status') if test else 'saved', 'context': context, 'run_context': observers,
            'conclusion': conclusion, 'root_cause': 'not_established', 'symptoms_observed': symptoms,
            'comparison_complete': complete, 'endpoint_ping_coverage': ping_coverage, 'quality': sorted(set(issues)), 'exclusions': EXCLUSIONS,
            'rules': RULES, 'episodes': episodes, 'episode_count': len(episodes), 'episodes_omitted': 0,
            'directions': directions, 'probe_summary': probe_summary, 'contributors': contributors, 'path_findings': path_findings,
            'load_response': phases, 'delivery': delivery, 'cohort': digest(cohort_basis) if cohort_known else None,
            'cohort_basis': cohort_basis if test else None, 'evidence': proof.rows,
            'evidence_selection': {'method': 'Selected measured examples; counts use all eligible observations. Examples are not exhaustive.',
                                   'omitted_examples': proof.omitted, 'sample_count': len(rows), 'invalid': invalid, 'outside': outside, 'duplicates': duplicates},
            'source_samples_sha256': digest(samples)}


def correlate_local(rows, probes, episodes, configs, endpoint, proof):
    totals = {k: Counter() for k in SIGNALS}
    examples = {k: {'affected': [], 'control': []} for k in SIGNALS}
    starts = [e['start'] for e in episodes]
    ends = [e['end'] for e in episodes]
    previous_boot, events = {}, []
    for row in rows:
        run = row.get('run_id')
        if row.get('kind') == 'log':
            import re
            if re.search(r'link.*(?:down|up)|deauth|disassoc|watchdog|out of memory|reset|table full', str(row.get('line', '')), re.I):
                events.append(('log', row))
            continue
        if row.get('kind') != 'telemetry' or run not in configs:
            continue
        m = incidents.obj(row.get('metrics'))
        ow = incidents.obj(m.get('openwrt'))
        boot = m.get('boot_id')
        if boot and previous_boot.get(run) and previous_boot[run]['metrics']['boot_id'] != boot:
            events.append(('reboot', row, previous_boot[run]))
        if boot:
            previous_boot[run] = row
        seconds, delay = ow.get('sample_seconds'), row.get('collection_ms')
        if not valid(seconds) or seconds > 5 or not valid(delay) or delay > 2000:
            continue
        left = bisect.bisect_left(ends, row['at']-seconds-1)
        right = bisect.bisect_right(starts, row['at']+1)
        nearby = episodes[left:right]
        # Never associate across unidentified or conflicting recorder runs.
        associated = any(e['run_ids'] == [run] for e in nearby)
        if nearby and not associated:
            continue
        group = 'affected' if associated else 'control'
        config = configs[run]
        selected = [s for s in incidents.items(ow.get('stations')) if isinstance(s, dict) and config.get('client_mac') and str(s.get('mac', '')).lower() == config['client_mac'].lower()]
        available = {'cpu': valid(m.get('cpu')) or any(valid(v) for v in incidents.obj(ow.get('cpu_percent')).values()),
                     'softirq': any(valid(v) for v in incidents.obj(ow.get('softirq_percent')).values()),
                     'memory': valid(incidents.obj(m.get('memory')).get('percent')),
                     'queue': any(valid(q.get('backlog_bytes')) for q in incidents.items(ow.get('queue_stats')) if isinstance(q, dict) and q.get('discipline') not in ('noqueue', 'ingress', 'clsact')),
                     'interface': any(valid(v.get(k)) for v in incidents.obj(ow.get('interfaces')).values() if isinstance(v, dict)
                                      for k in ('rx_errors_delta', 'tx_errors_delta', 'rx_dropped_delta', 'tx_dropped_delta')),
                     'wifi': any(valid(s.get('tx_failed_delta')) or (valid(s.get('tx_packets_delta')) and s['tx_packets_delta'] >= 10 and valid(s.get('tx_retries_delta'))) for s in selected),
                     'radio': any(valid(v.get('busy_percent')) for v in incidents.obj(ow.get('radio_stats')).values() if isinstance(v, dict)),
                     'conntrack': len(str(ow.get('conntrack', '')).split()) == 2 and all(n.isdigit() for n in str(ow.get('conntrack', '')).split())}
        signals = telemetry_signals(row, config)
        for key, present in available.items():
            if not present:
                continue
            totals[key][group+'_total'] += 1
            if key in signals:
                totals[key][group+'_hits'] += 1
                if len(examples[key][group]) < 3:
                    examples[key][group].append(row)
    findings = []
    for key, n in totals.items():
        if not n['affected_hits']:
            continue
        affected = n['affected_hits']/n['affected_total']
        control = n['control_hits']/n['control_total'] if n['control_total'] else None
        refs = [ref for r in examples[key]['affected']+examples[key]['control'] if (ref := proof.sample(r))]
        findings.append({'code': key, 'title': SIGNALS[key][0], 'assessment': 'Possible contributor',
                         'conclusion': f"Observed in {n['affected_hits']} / {n['affected_total']} eligible readings near degradation"
                         +(f" and {n['control_hits']} / {n['control_total']} comparison readings." if control is not None else '; no eligible comparison readings.'),
                         'support': 'More frequent near degradation' if control is not None and affected > control else
                                    'Also present outside degradation' if control is not None else 'No control comparison',
                         'counts': dict(n), 'affected_fraction': affected, 'control_fraction': control,
                         'evidence_refs': refs, 'next_check': SIGNALS[key][1],
                         'limitations': 'Comparison periods are outside detected episodes, not independently proven healthy. Association is not causation.'})
    for key in ('reboot', 'log'):
        found = [event for event in events if event[0] == key]
        if found:
            findings.append({'code': key, 'title': SIGNALS[key][0], 'assessment': 'Observed',
                             'conclusion': f'{len(found)} observations in the saved window.', 'counts': {'observations': len(found)},
                             'evidence_refs': [ref for event in found[:3] for r in event[1:] if (ref := proof.sample(r))], 'next_check': SIGNALS[key][1],
                             'limitations': 'These events do not independently establish the cause of network impairment.'})
    paths = []
    times = [r['at'] for r in probes]
    gap_times = [r['at'] for r in rows if r['kind'] == 'gap']
    for ep in episodes[:400]:
        values = probes[bisect.bisect_left(times, ep['start']-2):bisect.bisect_left(times, ep['end'])]
        grouped = defaultdict(list)
        for row in values:
            grouped[(row.get('source'), row.get('target'))].append(row)
        states = []
        for (origin, target), group in grouped.items():
            matched = [r for r in group if r.get('run_id') in configs]
            degraded = [r for r in matched if r['at'] >= ep['start'] and r.get('timing_quality') != 'uncertain' and
                        (r.get('status') == 'timeout' or valid(r.get('rtt_ms')) and incidents.threshold(configs[r['run_id']]) is not None
                         and r['rtt_ms'] > incidents.threshold(configs[r['run_id']]))]
            normal = (len({r['run_id'] for r in matched}) == 1 and
                      probe_coverage(matched, ep['start'], ep['end'], configs, healthy=True) >= (ep['end']-ep['start'])*.8
                      and bisect.bisect_left(gap_times, ep['end']) == bisect.bisect_left(gap_times, ep['start']))
            state = 'degraded' if degraded else 'normal' if matched and normal else 'unknown'
            states.append({'source': origin, 'target': target, 'state': state, 'observations': len(group),
                           'evidence_refs': [ref for r in (degraded or matched)[:2] if (ref := proof.sample(r))]})
        by_path = {(p['source'], p['target']): p['state'] for p in states}
        targets = {p['target'] for p in states if p['source'] == 'recorder' and p['state'] == 'degraded'}
        explanations = []
        for target in sorted(targets):
            if by_path.get(('router', target)) == 'degraded':
                explanations.append(f'Both observers degraded to {target}; a shared path or destination is possible.')
            elif by_path.get(('router', target)) == 'normal':
                explanations.append(f'Router replies to {target} remained normal in sampled periods; inspect Harbour, LAN and forwarded-path differences.')
        if len(targets) > 1:
            explanations.append('Several destinations degraded together; investigate their shared path, without assuming they are independent routes.')
        if ep['directions'] and endpoint and targets and endpoint not in targets:
            explanations.append('The bad pings were to other destinations; they do not corroborate impairment to the UDP endpoint.')
        if states:
            paths.append({'episode_id': ep['id'], 'paths': states, 'conclusions': explanations,
                          'assessment': 'Possible fault area', 'limitation': 'Observed path differences do not locate a faulty hop or exclude router forwarding problems.'})
    return findings, paths


def compare_load(test, evidence, matches, proof):
    result, config = test['result'], test['config']
    phases = []
    grouped = defaultdict(dict)
    for p in evidence.get('analysis', {}).get('paths', []):
        grouped[(p['source'], p['target'])][p['phase']] = p
    for (source, target), group in grouped.items():
        values = {phase: pick(p, 'attempts usable_attempts timeouts timeout_percent p95_ms peak_ms') for phase, p in group.items()}
        baseline, load, recovery = (group.get(k, {}) for k in ('baseline', 'load', 'recovery'))
        qualified = all(p.get('usable_attempts', 0) >= 10 and valid(p.get('p95_ms')) for p in (baseline, load))
        rise = load['p95_ms']-baseline['p95_ms'] if qualified else None
        recovered = (recovery['p95_ms'] <= baseline['p95_ms']+RULES['rtt_rise_ms']
                     and (recovery.get('timeout_percent') or 0) <= (baseline.get('timeout_percent') or 0)
                     if qualified and recovery.get('usable_attempts', 0) >= 10 and valid(recovery.get('p95_ms')) else None)
        message = ('Delay rose under load and returned near baseline afterwards; queueing is a possible contributor.' if rise is not None and rise >= RULES['rtt_rise_ms'] and recovered else
                   'Delay rose under load; recovery was absent or not established.' if rise is not None and rise >= RULES['rtt_rise_ms'] else
                   'No p95 increase of at least 20 ms was observed.' if qualified else 'Insufficient baseline/load observations.')
        ref = proof.add('phase:'+str(len(phases)), {'source': source, 'target': target, 'phases': values,
                        'load_started': result.get('load_started'), 'load_finished': result.get('load_finished'),
                        'method': 'All eligible original probe observations in each phase; p95 uses usable replies, so timeouts are reported separately.'})
        phases.append({'source': source, 'target': target, 'phases': values, 'p95_rise_ms': rise,
                       'recovered': recovered, 'conclusion': message, 'evidence_refs': [ref] if ref else []})
    delivery = []
    for name in ('upload', 'download'):
        raw = result.get('directions', {}).get(name, {}).get('raw', {}) if name == 'upload' else matches.get(name, {}).get('raw', {})
        sent = raw.get('end', {}).get('sum_sent', {})
        received = result.get('directions', {}).get(name, {}).get('summary', {})
        requested = config.get(name+'_kbps')
        sender_bps = sent.get('bits_per_second')
        if sent.get('sender') is not True or not valid(sender_bps) or not valid(requested) or requested <= 0:
            delivery.append({'direction': name, 'conclusion': 'Matching sender evidence is unavailable.', 'assessment': 'Insufficient evidence', 'evidence_refs': []})
            continue
        shortfall = sender_bps < requested*1000*RULES['sender_shortfall_fraction']
        conclusion = ('Sender generated less than 90% of the requested rate; low receiver throughput alone cannot be attributed to network loss.' if shortfall else
                      'Sender sustained at least 90% of the requested rate; assess receiver-reported loss separately.')
        ref = proof.add('sender:'+name, {'direction': name, 'requested_bits_per_second': requested*1000,
                                        'sender': pick(sent, 'bits_per_second bytes packets seconds sender'),
                                        'receiver': pick(received, 'bits_per_second bytes packets lost_packets lost_percent seconds')})
        delivery.append({'direction': name, 'conclusion': conclusion, 'assessment': 'Observed',
                         'sender_shortfall': shortfall, 'sender_bits_per_second': sender_bps,
                         'requested_bits_per_second': requested*1000, 'evidence_refs': [ref] if ref else []})
    return phases, delivery


def analyze_call(test, evidence, server):
    local = evidence['local']
    quality = list(evidence.get('analysis', {}).get('quality', []))
    # A collection status snapshot is context, not by itself an evidence failure.
    quality = [q for q in quality if not q.startswith('Recorder status at collection')]
    report = analyze('call', test['id'], server, evidence['window']['start'], evidence['window']['end'],
                     local['samples'], local.get('runs', []), local.get('settings', {}),
                     test=test, evidence=evidence, quality=quality)
    report['selection_time'] = test.get('created', report['start'])
    return report


def analyze_incident(bundle, server):
    old = incidents.analyze(bundle)
    report = analyze('incident', bundle['incident']['id'], server, old['coverage']['requested_start'], old['coverage']['requested_end'],
                     bundle['samples'], bundle.get('runs', []), bundle.get('settings', {}), quality=old['quality'])
    report['label'] = bundle['incident'].get('label', 'Ping incident')
    report['status'] = 'sealed' if bundle['incident'].get('complete') else 'collecting'
    report['selection_time'] = bundle['incident']['measured']
    report['quality'].append('This incident report contains ping/router evidence; use overlapping call reports for verified receiver measurements.')
    return report


def patterns(reports):
    """Matched call-test comparisons with exposure denominators, never marker counts."""
    groups = defaultdict(list)
    for r in reports:
        if r.get('source') == 'call' and r.get('cohort'):
            groups[r['cohort']].append(r)
    result = []
    for cohort, rows in groups.items():
        unique, end = [], None
        for r in sorted(rows, key=lambda r: r['start']):
            if end is None or r['start'] >= end:
                unique.append(r)
                end = r['end']
        healthy = sum(r['comparison_complete'] and not r['symptoms_observed'] for r in unique)
        impaired_count = sum(r['symptoms_observed'] for r in unique)
        directions = {d: {'observed_seconds': sum(x['observed_seconds'] for r in unique for x in r.get('directions', []) if x['direction'] == d),
                          'affected_seconds': sum(x['affected_seconds'] for r in unique for x in r.get('directions', []) if x['direction'] == d)} for d in ('upload', 'download')}
        result.append({'cohort': cohort, 'context': rows[0]['context'], 'tests': len(unique), 'impaired_tests': impaired_count,
                       'healthy_comparisons': healthy, 'incomplete_comparisons': len(unique)-healthy-impaired_count,
                       'overlapping_tests_excluded': len(rows)-len(unique), 'directions': directions,
                       'report_ids': [r['id'] for r in unique],
                       'conclusion': f'{impaired_count} of {len(unique)} comparable retained tests recorded impairment; {healthy} had complete comparisons with neither test impaired.',
                       'limitations': 'Retained test exposure only, not an outage rate. Fixed schedules and missing/expired tests can bias time-of-day comparisons.'})
    return result
