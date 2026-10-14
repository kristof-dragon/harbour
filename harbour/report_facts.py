"""Small, deterministic measurement facts. Raw inputs never become instructions.

Counters are additive; coverage is a union of observed one-second bins. Selected
excerpts support review, not reconstruction of discarded raw measurements.
"""
import bisect
import copy
import hashlib
import json
from collections import Counter

from . import connection_analysis as analysis, incidents

BINS = [1, 2, 5, 10, 20, 50, 100, 150, 250, 500, 1000, 2000, 5000, 10000]
COUNTS = ('attempts', 'usable', 'replies', 'timeouts', 'high', 'uncertain', 'late')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def excerpt(row, archive_id, ordinal):
    evidence = analysis.Evidence()
    sample = dict(row, _ref=ordinal)
    ref = evidence.sample(sample)
    result = evidence.rows[ref]
    result.pop('source_sample_index', None)
    result['provenance'] = {'archive_id': archive_id, 'archive_sequence': ordinal,
                            'sample_sha256': digest(row)}
    return result


def compact(samples, events, settings, runs, archive_id, first_sequence):
    minutes, markers, seen = {}, {}, set()
    configs = {r['id']: r.get('config', {}) for r in runs}
    rejected = 0
    for ordinal, row in enumerate(samples, first_sequence):
        if not isinstance(row, dict) or not incidents.finite(row.get('at')):
            rejected += 1
            continue
        identity = row.get('sample_key')
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        at = row['at']; bucket = int(at//60)*60
        minute = minutes.setdefault(bucket, {'at': bucket, 'kinds': {}, 'paths': {}, 'signals': {}, 'events': [], 'telemetry': {}})
        kind = str(row.get('kind', 'unknown'))
        minute['kinds'][kind] = minute['kinds'].get(kind, 0)+1
        cfg = configs.get(row.get('run_id'), settings)
        if kind == 'probe':
            source, target = str(row.get('source', 'unknown')), str(row.get('target', 'unknown'))
            threshold = incidents.threshold(cfg)
            key = json.dumps([source, target, threshold])
            path = minute['paths'].setdefault(key, {'source': source, 'target': target, 'threshold_ms': threshold,
                **dict.fromkeys(COUNTS, 0), 'sum_ms': 0, 'max_ms': None, 'histogram': [0]*(len(BINS)+1),
                'observed_bins': 0, 'first': at, 'last': at, 'bad_first': None, 'bad_last': None, 'evidence': []})
            path['first'], path['last'] = min(path['first'], at), max(path['last'], at)
            status = row.get('status')
            if status == 'late_reply':
                path['late'] += 1
                continue
            path['attempts'] += 1
            uncertain = row.get('timing_quality') == 'uncertain' or status not in ('reply', 'timeout')
            rtt = row.get('rtt_ms')
            if status == 'reply' and not analysis.valid(rtt):
                uncertain = True
            if uncertain:
                path['uncertain'] += 1
                continue
            path['usable'] += 1
            path['observed_bins'] |= 1 << int(at-bucket)
            bad = status == 'timeout'
            if bad:
                path['timeouts'] += 1
            else:
                path['replies'] += 1
                path['sum_ms'] += rtt
                path['max_ms'] = max(path['max_ms'] or 0, rtt)
                path['histogram'][bisect.bisect_left(BINS, rtt)] += 1
                if threshold is not None and rtt > threshold:
                    path['high'] += 1
                    bad = True
            if bad:
                path['bad_first'] = min(path['bad_first'] if path['bad_first'] is not None else at, at)
                path['bad_last'] = max(path['bad_last'] if path['bad_last'] is not None else at, at)
                # First and latest observed impairment, bounded independently of counts.
                value = excerpt(row, archive_id, ordinal)
                if len(path['evidence']) < 2:
                    path['evidence'].append(value)
                else:
                    path['evidence'][-1] = value
        elif kind == 'telemetry':
            metrics = incidents.obj(row.get('metrics')); ow = incidents.obj(metrics.get('openwrt'))
            values = {'cpu_percent': metrics.get('cpu'), 'memory_percent': incidents.obj(metrics.get('memory')).get('percent')}
            for name, field in (('core_cpu_percent', 'cpu_percent'), ('softirq_percent', 'softirq_percent')):
                available = [x for x in incidents.obj(ow.get(field)).values() if analysis.valid(x)]
                values[name] = max(available) if available else None
            for name, value in values.items():
                if not analysis.valid(value):
                    continue
                agg = minute['telemetry'].setdefault(name, {'count': 0, 'sum': 0, 'min': value, 'max': value})
                agg.update(count=agg['count']+1, sum=agg['sum']+value, min=min(agg['min'], value), max=max(agg['max'], value))
            for signal in analysis.telemetry_signals(row, cfg):
                item = minute['signals'].setdefault(signal, {'count': 0, 'first': at, 'last': at, 'evidence': []})
                item['count'] += 1; item['first'] = min(item['first'], at); item['last'] = max(item['last'], at)
                if not item['evidence']:
                    item['evidence'] = [excerpt(row, archive_id, ordinal)]
        elif kind in ('gap', 'log'):
            minute['events'].append({'kind': kind, 'at': at, 'evidence': excerpt(row, archive_id, ordinal)})
    for row in events:
        if incidents.finite(row.get('measured')):
            marker = {k: row.get(k) for k in ('id', 'measured', 'label', 'automatic', 'complete', 'trigger')}
            if isinstance(marker['trigger'], str):
                marker['trigger'] = json.loads(marker['trigger'])
            marker['archive_id'] = archive_id
            markers[row['id']] = marker
    return {'minutes': list(minutes.values()), 'markers': list(markers.values()), 'invalid_samples': rejected}


def merge_minute(target, incoming):
    target['kinds'] = dict(Counter(target['kinds'])+Counter(incoming['kinds']))
    target['events'].extend(incoming['events'])
    for key, value in incoming['paths'].items():
        if key not in target['paths']:
            target['paths'][key] = copy.deepcopy(value)
            continue
        p = target['paths'][key]
        for k in (*COUNTS, 'sum_ms'):
            p[k] += value[k]
        p['observed_bins'] |= value['observed_bins']
        p['histogram'] = [a+b for a, b in zip(p['histogram'], value['histogram'])]
        for k in ('first', 'bad_first'):
            vals = [v for v in (p[k], value[k]) if v is not None]; p[k] = min(vals) if vals else None
        for k in ('last', 'bad_last', 'max_ms'):
            vals = [v for v in (p[k], value[k]) if v is not None]; p[k] = max(vals) if vals else None
        p['evidence'] = (p['evidence']+value['evidence'])[:1]+(p['evidence']+value['evidence'])[-1:] if p['evidence'] or value['evidence'] else []
    for key, value in incoming['signals'].items():
        if key not in target['signals']:
            target['signals'][key] = copy.deepcopy(value)
        else:
            s = target['signals'][key]; s['count'] += value['count']
            s['first'] = min(s['first'], value['first']); s['last'] = max(s['last'], value['last'])
    for key, value in incoming['telemetry'].items():
        if key not in target['telemetry']:
            target['telemetry'][key] = copy.deepcopy(value)
        else:
            s = target['telemetry'][key]
            s.update(count=s['count']+value['count'], sum=s['sum']+value['sum'], min=min(s['min'], value['min']), max=max(s['max'], value['max']))


def path_totals(minutes):
    totals = {}
    for minute in minutes:
        for key, p in minute['paths'].items():
            total = totals.setdefault(key, {'source': p['source'], 'target': p['target'], 'threshold_ms': p['threshold_ms'],
                **dict.fromkeys(COUNTS, 0), 'sum_ms': 0, 'max_ms': None, 'observed_seconds': 0,
                'histogram': [0]*(len(BINS)+1)})
            for field in (*COUNTS, 'sum_ms'):
                total[field] += p[field]
            total['observed_seconds'] += p['observed_bins'].bit_count()
            total['histogram'] = [a+b for a, b in zip(total['histogram'], p['histogram'])]
            values = [v for v in (total['max_ms'], p['max_ms']) if v is not None]
            total['max_ms'] = max(values) if values else None
    return list(totals.values())


def timeline(minutes, markers):
    items = []
    for marker in markers:
        items.append({'id': 'marker:'+marker['id'], 'at': marker['measured'], 'end': marker['measured'],
            'kind': 'Incident marker', 'title': marker['label'], 'detail': 'Automatic trigger' if marker['automatic'] else 'User marker',
            'evidence': [marker['trigger']] if marker.get('trigger') else ([{'marker_id': marker['id'], 'measured': marker['measured'], 'label': marker['label']}] if not marker['automatic'] else []), 'provenance': {'archive_id': marker['archive_id']},
            'quality': ([] if marker.get('trigger') else ['Original trigger excerpt unavailable.']) if marker['automatic'] else ['User-reported marker; this is not an independently measured impairment.']})
    for minute in minutes:
        for key, p in minute['paths'].items():
            if not (p['timeouts'] or p['high']):
                continue
            items.append({'id': 'probe:'+str(minute['at'])+':'+key, 'at': p['bad_first'], 'end': p['bad_last'],
                'kind': 'Probe observations', 'title': p['source']+' → '+p['target'],
                'group': 'probe:'+key, 'bucket': minute['at'], 'counts': {'timeouts':p['timeouts'], 'slow':p['high'], 'usable':p['usable']},
                'detail': f"{p['timeouts']} timeouts; {p['high']} replies above {p['threshold_ms']} ms; {p['usable']} usable attempts in this minute.",
                'evidence': p['evidence'], 'quality': ['Grouped observations; the first-to-last span is not a measured outage duration.']})
        for key, signal in minute['signals'].items():
            items.append({'id': 'signal:'+str(minute['at'])+':'+key, 'at': signal['first'], 'end': signal['last'],
                'kind': 'Router observation', 'title': analysis.SIGNALS[key][0],
                'group': 'signal:'+key, 'bucket': minute['at'], 'counts': {'matched_readings':signal['count']},
                'detail': str(signal['count'])+' readings matched the documented review threshold.',
                'evidence': signal['evidence'], 'quality': ['An observed router condition does not establish a cause.']})
        for index, event in enumerate(minute['events']):
            sample = event['evidence']
            items.append({'id': 'event:'+str(minute['at'])+':'+str(index), 'at': event['at'], 'end': event['at'],
                'kind': 'Collection gap' if event['kind']=='gap' else 'Router log',
                'title': sample.get('detail') or sample.get('line') or event['kind'], 'detail': '',
                'evidence': [sample], 'quality': ['Log timestamps reflect collection unless the excerpt states otherwise.'] if event['kind']=='log' else []})
    grouped, active = [], {}
    for item in sorted(items, key=lambda item:(item['at'],item['id'])):
        key = item.get('group'); previous = active.get(key) if key else None
        if previous and item['bucket'] == previous['bucket']+60:
            previous['bucket'] = item['bucket']; previous['end'] = max(previous['end'],item['end'])
            previous['counts'] = dict(Counter(previous['counts'])+Counter(item['counts']))
            previous['grouped_minutes'] = previous.get('grouped_minutes',1)+1
            selected = previous['evidence']+item['evidence']
            previous['evidence'] = selected[:1]+selected[-1:]
            previous['detail'] = '; '.join(str(v)+' '+k.replace('_',' ') for k,v in previous['counts'].items())+f" across {previous['grouped_minutes']} adjacent minute groups. Individual observations may be separated by normal readings."
        else:
            grouped.append(item)
            if key:
                active[key] = item
    return grouped
