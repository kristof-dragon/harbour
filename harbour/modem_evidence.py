"""Offline modem-only indications, durable evidence, and temporal comparisons.

No network access, guessed RF thresholds, or ISP/router attribution. All rules
consume only modem snapshots; correlation is a separate, non-causal table.
"""
import gzip
import hashlib
import heapq
import json
import math
import re
from collections import Counter

from . import store, resource_limits as resources

VERSION = 1
MAX_FINDINGS = 128
MAX_PROOFS = 64
LIMITS = [
    'These indications use only modem-reported values. They do not establish an ISP or router fault.',
    'Snapshots cannot exclude faults between polls. Collection failures describe missing evidence, not line failure.',
    'Counter changes are bracketed by observations, sometimes across the selection start; their exact occurrence time is unknown. Counter decreases are resets or discontinuities, not negative errors.',
    'Modem event times are unverified. Log entries are historical context and are not aligned to network episodes.',
    'RF power/SNR values and unknown status codes are retained without guessed units or health thresholds.',
    'No defined indication is not a healthy-line verdict. Repeated readings and overlapping reports are not independent outages.',
]


def initialize():
    store.execute('''CREATE TABLE IF NOT EXISTS report_modem_samples (
        server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
        measured REAL NOT NULL, sample_key TEXT NOT NULL, payload BLOB NOT NULL,
        PRIMARY KEY(server_id,measured,sample_key))''')


def key(sample):
    return hashlib.sha256(json.dumps(sample, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def pack_sample(sample):
    # Collector snapshots are already bounded to 32 KiB. One compression call
    # avoids thousands of tiny gzip writes on low-power Harbour hosts.
    raw = json.dumps(sample, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > min(64*1024, resources.budget()):
        raise resources.BudgetExceeded()
    return gzip.compress(raw, mtime=0)


def capture(con, id_, sample):
    """Archive sanitized readings atomically before the short history can prune."""
    from . import network_reports
    floor = con.execute('SELECT retired_before FROM report_state WHERE server_id=?', (id_,)).fetchone()
    if floor and floor[0] and sample['at'] < floor[0]:
        return
    digest = key(sample)
    if con.execute('SELECT 1 FROM report_modem_samples WHERE server_id=? AND measured=? AND sample_key=?', (id_, sample['at'], digest)).fetchone():
        return
    cursor = con.execute('INSERT OR IGNORE INTO report_modem_samples VALUES (?,?,?,?)',
                        (id_, sample['at'], digest, pack_sample(sample)))
    if cursor.rowcount:
        network_reports.mark_dirty(con, id_, sample['at'], sample['at'])


def readings(con, id_, start, end, baseline=True):
    """Merge retained raw and archived snapshots, deduplicated in constant space."""
    def source(table, packed):
        decode = resources.unpack if packed else json.loads
        # Include the preceding timestamp for counter continuity, never a future read.
        since = start
        if baseline:
            before = con.execute(f'SELECT max(measured) FROM {table} WHERE server_id=? AND measured<?', (id_, start)).fetchone()[0]
            if before is not None:
                since = before
        cursor = con.execute(f'SELECT measured,payload FROM {table} WHERE server_id=? AND measured>=? AND measured<? ORDER BY measured', (id_, since, end))
        # Each poll is serialized; sort same-timestamp collisions deterministically.
        pending, at = [], None
        for row in cursor:
            if at is not None and row['measured'] != at:
                yield from sorted(pending, key=lambda item: item[:2])
                pending = []
            sample = decode(row['payload'])
            pending.append((row['measured'], key(sample), sample))
            at = row['measured']
        yield from sorted(pending, key=lambda item: item[:2])
    last = None
    for at, digest, sample in heapq.merge(source('modem_samples', False), source('report_modem_samples', True), key=lambda item: item[:2]):
        identity = (at, digest)
        if identity != last:
            yield sample
        last = identity


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def uptime(value):
    match = re.fullmatch(r'(\d+)day\(s\)(\d+)h:(\d+)m:(\d+)s', str(value))
    if not match:
        return None  # Numeric firmware uptime has no verified unit.
    d, h, m, s = map(int, match.groups())
    return ((d*24+h)*60+m)*60+s if h < 24 and m < 60 and s < 60 else None


def values(sample, section):
    part = sample.get('sections', {}).get(section, {})
    return {f['field']: f['value'] for f in part.get('fields', [])} if part.get('state') == 'ok' else {}


def groups(sample, section, name):
    result = {}
    for path, value in values(sample, section).items():
        match = re.fullmatch(r'[^/]+/('+name+r'(?:\[\d+\])?)/([^/]+)', path)
        if match:
            result.setdefault(match[1], {})[match[2]] = value
    return result


def channels(sample):
    result = {}
    for section, name, identity in (('downstream', 'downstream', 'chid'), ('upstream', 'upstream', 'usid')):
        for row in groups(sample, section, name).values():
            if row.get(identity) and (numeric(row.get('freq')) or 0) > 0:
                channel = (section, row[identity], row['freq'])
                # Duplicate identity makes that channel ambiguous; do not compare it.
                result[channel] = row if channel not in result else None
    return result


def context(sample):
    system = values(sample, 'system')
    return (sample.get('endpoint'), sample.get('observer'), sample.get('configuration_revision'),
            system.get('GlobalSettings/SwVersion'), system.get('GlobalSettings/ConfigVenderModel'))


class Assessment:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.previous = None
        self.counts, self.signals = Counter(), Counter()
        self.findings, self.proofs, self.logs_seen = [], {}, set()
        self.omitted = 0
        self.first = self.last = None
        self.largest_gap = 0
        self.baseline = None

    def finding(self, code, title, detail, samples, *, indication=False, aligned=True):
        self.signals[code] += 1
        if len(self.findings) >= MAX_FINDINGS:
            self.omitted += 1
            return
        refs = ['modem:'+key(s) for s in samples]
        extra = set(refs)-self.proofs.keys()
        if len(self.findings) >= MAX_FINDINGS or len(self.proofs)+len(extra) > MAX_PROOFS:
            self.omitted += 1
            return
        self.proofs.update(zip(refs, samples))
        self.findings.append({'id': 'modem-finding:'+str(len(self.findings)+1), 'code': code, 'title': title,
            'detail': detail, 'indication': indication, 'start': samples[0]['at'],
            'end': samples[-1].get('received_at', samples[-1]['at']),
            'timing': 'observation_interval' if aligned else 'unverified_modem_event_time', 'modem_evidence_refs': refs})

    def add(self, sample):
        at = sample['at']
        if at < self.start:
            self.previous = sample
            self.baseline = at
            return
        if at >= self.end:
            return
        self.counts['snapshots'] += 1
        self.counts[sample.get('state', 'unavailable')] += 1
        self.counts['truncated_snapshots'] += bool(sample.get('truncated') or any(s.get('omitted_fields') for s in sample.get('sections', {}).values()))
        self.first = at if self.first is None else self.first
        self.last = at
        previous = self.previous
        self.previous = sample
        supported = uptime(values(sample, 'device').get('cm_system_info/cm_system_uptime')) is not None or any(
            row and any(k in row for k in ('PostRs', 'PreRs', 't3Timeouts', 't4Timeouts', 'IsQamLocked', 'IsFECLocked', 'IsMpegLocked')) for row in channels(sample).values())
        self.counts['recognized_snapshots'] += supported
        if sample.get('state') != 'ok':
            self.finding('collection_gap', 'Modem collection incomplete', sample.get('error') or 'One or more modem sections could not be read.', [sample])
        comparable = False
        if previous:
            gap = at-previous['at']
            self.largest_gap = max(self.largest_gap, gap)
            threshold = max(120, 2*max(sample.get('poll_interval_seconds', 60), previous.get('poll_interval_seconds', 60))+30)
            comparable = (context(sample) == context(previous) and 0 < gap <= threshold
                          and sample.get('state') in ('ok', 'partial') and previous.get('state') in ('ok', 'partial'))
            if gap > threshold:
                self.finding('observation_gap', 'Gap between modem readings', f'{gap:.1f} seconds between observations; counter comparison withheld.', [previous, sample])
            if not comparable:
                self.counts['comparison_breaks'] += 1
            self.counts['comparable_pairs'] += comparable
        old_uptime = uptime(values(previous or {}, 'device').get('cm_system_info/cm_system_uptime'))
        new_uptime = uptime(values(sample, 'device').get('cm_system_info/cm_system_uptime'))
        reboot = comparable and old_uptime is not None and new_uptime is not None and new_uptime < old_uptime
        if reboot:
            self.finding('uptime_decreased', 'Modem uptime decreased', 'Reported uptime decreased between readings; a restart or uptime reset is indicated. Its cause is unknown.', [previous, sample], indication=True)
        old_channels = channels(previous or {}) if comparable and not reboot else {}
        changes = {}
        for identity, row in channels(sample).items():
            if row is None:
                continue
            label = f'{identity[0]} channel {identity[1]} at reported frequency {identity[2]}'
            unlocked = [field for field in ('IsQamLocked', 'IsFECLocked', 'IsMpegLocked') if row.get(field) == '0']
            if unlocked:
                self.finding('channel_unlocked', 'Channel reports loss of lock', label+': '+', '.join(unlocked)+' = 0.', [sample], indication=True)
            before = old_channels.get(identity)
            if before:
                for field in ('PostRs', 'PreRs', 't3Timeouts', 't4Timeouts'):
                    # Counters are integer strings; never round large counters through float.
                    a, b = before.get(field, ''), row.get(field, '')
                    if not re.fullmatch(r'\d+', a) or not re.fullmatch(r'\d+', b):
                        continue
                    delta = int(b)-int(a)
                    if delta:
                        reset = delta < 0
                        changes.setdefault((field, reset), []).append(
                            f'{label}: {field} {a} → {b}'+('. No error delta inferred.' if reset else f' (+{delta}).'))
        for (field, reset), descriptions in changes.items():
            self.finding('counter_reset' if reset else field+'_increase',
                'Counter decreased / reset' if reset else field+' increased',
                ' '.join(descriptions), [previous, sample], indication=not reset and field != 'PreRs')
        # Deduplicate repeated log rows by content, independently of their row indexes.
        for row in groups(sample, 'events', 'eventlog').values():
            digest = key(row)
            if digest in self.logs_seen:
                continue
            if len(self.logs_seen) < 4096:
                self.logs_seen.add(digest)
            else:
                self.counts['log_dedup_limit_reached'] += 1
                continue
            if row.get('prior', '').lower() in ('warning', 'error', 'critical', 'alert', 'emergency'):
                self.finding('reported_log', 'Modem reports '+row['prior'].lower()+' log entry',
                    row.get('time', 'Time unavailable')+': '+row.get('text', '')+' Historical context; not evidence of a new fault in this window.', [sample], aligned=False)

    def finish(self):
        indicated = sum(n for code, n in self.signals.items() if code in ('uptime_decreased', 'channel_unlocked', 'PostRs_increase', 't3Timeouts_increase', 't4Timeouts_increase'))
        state = 'indications_observed' if indicated else 'insufficient_evidence' if not self.counts['recognized_snapshots'] else 'no_defined_indication'
        return {'schema': 1, 'analyzer_version': VERSION, 'basis': 'modem_reported_values_only', 'root_cause': 'not_established',
            'state': state, 'conclusion': {'indications_observed': 'Modem-reported fault indications observed.',
                'insufficient_evidence': 'Insufficient recognized modem evidence.',
                'no_defined_indication': 'No defined modem fault indication observed; line health is not established.'}[state],
            'window': {'start': self.start, 'end': self.end, 'end_exclusive': True},
            'coverage': {**dict(self.counts), 'first_observed': self.first, 'last_observed': self.last,
                'baseline_at': self.baseline, 'largest_observation_gap_seconds': self.largest_gap,
                'start_unobserved_seconds': None if self.first is None else max(0, self.first-self.start),
                'end_unobserved_seconds': None if self.last is None else max(0, self.end-self.last)},
            'signal_counts': dict(self.signals), 'findings': self.findings, 'findings_omitted': self.omitted,
            'evidence': self.proofs, 'evidence_scope': 'Selected sanitized snapshots; counts use every retained snapshot in the window.',
            'limitations': LIMITS}


def assess(id_, start, end):
    analyzer = Assessment(start, end)
    with store.db() as con:
        con.execute('BEGIN')
        for sample in readings(con, id_, start, end):
            analyzer.add(sample)
    return analyzer.finish()


def correlate(assessment, episodes):
    """Table of intersecting observation intervals, never combined attribution."""
    rows = []
    for index, episode in enumerate(episodes):
        start, end = episode.get('start', episode.get('at')), episode.get('end', episode.get('at'))
        if start is None or end is None:
            continue
        matches = [f for f in assessment['findings'] if f['indication'] and f['timing'] == 'observation_interval' and f['start'] <= end and f['end'] >= start]
        rows.append({'network_id': episode.get('id', str(index)), 'start': start, 'end': end,
            'network_observation': episode.get('conclusion') or episode.get('title') or episode.get('classification', 'Network observation'),
            'modem_finding_ids': [f['id'] for f in matches],
            'modem_observations': [f['title'] for f in matches],
            'relationship': 'Observation intervals overlap; exact fault time and cause unproven.' if matches else 'No overlapping retained modem indication; this does not exclude a modem/link fault.'})
        if len(rows) >= 512:
            break
    return {'rows': rows, 'rows_omitted': max(0, len(episodes)-len(rows)),
            'limitation': 'Temporal comparison only. Historical log times are excluded. Omitted findings and collection gaps limit comparisons.'}


def attach(report):
    report['modem'] = assess(report['server_id'], report['start'], report['end'])
    report['modem_correlation'] = correlate(report['modem'], report.get('episodes', []))
    report['modem_correlation']['network_conclusion'] = report['conclusion']
    report['modem_correlation']['network_contributors'] = [
        {k: f[k] for k in ('title', 'assessment', 'conclusion') if k in f} for f in report.get('contributors', [])]
    return report


class ExportWriter:
    def __init__(self, writer):
        self.writer, self.bytes = writer, 0

    def write(self, text):
        self.bytes += len(text.encode('utf-8'))
        if self.bytes > resources.budget()*8:
            raise resources.BudgetExceeded()
        return self.writer.write(text)


def write_export(writer, con, id_, start, end, *, archived=False):
    """Stream all retained readings into a prepared file; no 256-row truncation."""
    analyzer = Assessment(start, end)
    writer = ExportWriter(writer)
    writer.write(json.dumps({'schema': 2, 'kind': 'harbour-modem-evidence', 'server_id': id_,
        'cause': 'not_established', 'window': {'start': start, 'end': end}, 'truncated': False,
        'scope': 'All retained sanitized snapshots in the selected window; missing periods may reflect retention or collection gaps.'})[:-1]+',"samples":[')
    count = 0
    if archived:
        samples = readings(con, id_, start, end)
    else:
        samples = (json.loads(r['payload']) for r in con.execute('SELECT payload FROM modem_samples WHERE server_id=? AND measured>=? AND measured<? ORDER BY measured,id', (id_, start, end)))
    for sample in samples:
        analyzer.add(sample)
        if sample['at'] < start:
            continue
        if count:
            writer.write(',')
        json.dump(sample, writer, allow_nan=False, separators=(',', ':'))
        count += 1
    writer.write('],"sample_count":'+str(count)+',"assessment":')
    json.dump(analyzer.finish(), writer, allow_nan=False, separators=(',', ':'))
    writer.write('}')
