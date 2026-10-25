"""Scheduled, replay-safe daily evidence reports and calendar-period rollups.

The recorder checkpoints compact facts with each raw archive append. Nightly
publication works from those small facts; old archives are streamed once on
upgrade. Only the recorder maintenance process publishes or removes evidence.
"""
import datetime as dt
import gzip
import json
import logging
import time
from collections import Counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import network, report_facts as facts, store, resource_limits as resources
from .auth import admin, authenticated

router = APIRouter(prefix='/api/servers/{id_}/network/reports')
VERSION = 2
DAY = dt.timedelta(days=1)


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    timezone: str = 'Europe/London'
    process_at: str = Field(default='03:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    retention_days: int = Field(default=365, ge=7, le=3650, strict=True)
    max_mib: int = Field(default=512, ge=16, le=16384, strict=True)
    call_retention_days: int = Field(default=7, ge=1, le=365, strict=True)
    call_max_mib: int = Field(default=1024, ge=16, le=16384, strict=True)

    @field_validator('timezone')
    @classmethod
    def timezone_valid(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Choose an IANA timezone, for example Europe/London')
        return value


def initialize():
    from . import modem_evidence
    modem_evidence.initialize()
    with store.db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS report_policy (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS report_state (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
          requested INTEGER NOT NULL DEFAULT 0, last_slot REAL, last_success REAL,
          updated REAL, error TEXT, progress TEXT, retired_before REAL);
        CREATE TABLE IF NOT EXISTS report_archives (
          id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          size INTEGER NOT NULL, through INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS report_chunks (
          archive_id TEXT NOT NULL REFERENCES report_archives(id) ON DELETE CASCADE,
          part TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, payload BLOB NOT NULL,
          PRIMARY KEY(archive_id,part));
        CREATE INDEX IF NOT EXISTS report_chunk_time ON report_chunks(start,end);
        CREATE TABLE IF NOT EXISTS report_sources (
          server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          source_id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
          payload BLOB NOT NULL, verified INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(server_id,source_id));
        CREATE TABLE IF NOT EXISTS report_dirty (
          server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          day INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(server_id,day));
        CREATE TABLE IF NOT EXISTS report_days (
          server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          day TEXT NOT NULL, timezone TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
          generated REAL NOT NULL, payload BLOB NOT NULL,
          PRIMARY KEY(server_id,day,timezone));
        ''')


def policy(id_):
    row = store.one('SELECT config FROM report_policy WHERE server_id=?', (id_,))
    return Policy(**json.loads(row['config'])).model_dump() if row else Policy().model_dump()


def pack(value):
    return resources.pack(value)


def unpack(value):
    return resources.unpack(value)


def bounds(day, timezone):
    date = dt.date.fromisoformat(day) if isinstance(day, str) else day
    zone = ZoneInfo(timezone)
    return (dt.datetime.combine(date, dt.time(), zone).timestamp(),
            dt.datetime.combine(date+DAY, dt.time(), zone).timestamp())


def scheduled(date, cfg):
    """One run per local date; nonexistent clock times advance, folds run once."""
    h, m = map(int, cfg['process_at'].split(':'))
    return dt.datetime.combine(date, dt.time(h, m), ZoneInfo(cfg['timezone'])).timestamp()


def slot(now, cfg):
    date = dt.datetime.fromtimestamp(now, ZoneInfo(cfg['timezone'])).date()
    point = scheduled(date, cfg)
    return point if point <= now else scheduled(date-DAY, cfg)


def next_slot(now, cfg):
    date = dt.datetime.fromtimestamp(now, ZoneInfo(cfg['timezone'])).date()
    point = scheduled(date, cfg)
    return point if point > now else scheduled(date+DAY, cfg)


def mark_dirty(con, id_, start, end):
    # UTC partitions are translated to the current report timezone at publication.
    for day in range(int(start//86400)*86400, int(end//86400)*86400+1, 86400):
        con.execute('INSERT INTO report_dirty(server_id,day) VALUES (?,?) ON CONFLICT(server_id,day) DO UPDATE SET revision=revision+1', (id_, day))


def save_chunk(con, archive, part, compact):
    if compact['invalid_samples']:
        raise ValueError('Invalid timestamps in archive; raw evidence retained for inspection')
    times = [m['at'] for m in compact['minutes']]+[m['measured'] for m in compact['markers']]
    if not times:
        return
    start, end = min(times), max(times)+60
    con.execute('INSERT OR REPLACE INTO report_chunks VALUES (?,?,?,?,?)',
                (archive['id'], part, start, end, pack(compact)))
    mark_dirty(con, archive['server_id'], start, end)


def checkpoint(con, archive, new_size, samples, events, settings, runs):
    """Same transaction as archived=1: raw pruning cannot outrun these facts."""
    prior = con.execute('SELECT * FROM report_archives WHERE id=?', (archive['id'],)).fetchone()
    contiguous = (prior and prior['valid'] and prior['size'] == archive['size'] and prior['through'] == archive['sample_count']) or archive['size'] == 0
    con.execute('INSERT OR IGNORE INTO report_archives VALUES (?,?,?,?,0)',
                (archive['id'], archive['server_id'], 0, 0))
    compact = facts.compact(samples, events, settings, runs, archive['id'], archive['sample_count']+1)
    save_chunk(con, archive, 'append:'+str(archive['size']), compact)
    if contiguous:
        con.execute('UPDATE report_archives SET size=?,through=?,valid=1 WHERE id=?',
                    (new_size, archive['sample_count']+len(samples), archive['id']))


def ready(archive):
    row = store.one('SELECT * FROM report_archives WHERE id=?', (archive['id'],))
    return bool(row and row['valid'] and row['size'] == archive['size'] and row['through'] == archive['sample_count'])


def import_archive(archive, stop=None):
    """Stream legacy files. A CRC/read failure leaves raw evidence protected."""
    from . import network_archives
    with store.db() as con:
        con.execute('INSERT OR REPLACE INTO report_archives VALUES (?,?,?,?,0)',
                    (archive['id'], archive['server_id'], 0, 0))
        con.execute('DELETE FROM report_chunks WHERE archive_id=?', (archive['id'],))
    samples, events, runs, settings, ordinal, part, batch_size = [], [], {}, {}, 0, 0, 0
    def flush():
        nonlocal samples, events, part, batch_size
        compact = facts.compact(samples, events, settings, list(runs.values()), archive['id'], ordinal-len(samples)+1)
        with store.db() as con:
            save_chunk(con, archive, 'import:'+str(part), compact)
        samples, events, part, batch_size = [], [], part+1, 0
    if network_archives.path(archive, not archive['complete']).stat().st_size < archive['size']:
        raise ValueError('Archive is shorter than its durable checkpoint; raw evidence retained')
    import io
    class CheckpointReader:
        # An interrupted append can leave an uncommitted tail. Read only the
        # fsynced size recorded by SQLite, including on an active archive.
        def __init__(self, stream, size):
            self.stream, self.remaining = stream, size
        def read(self, count=-1):
            count = self.remaining if count < 0 else min(count, self.remaining)
            data = self.stream.read(count)
            self.remaining -= len(data)
            return data
    with network_archives.path(archive, not archive['complete']).open('rb') as raw, \
            gzip.GzipFile(fileobj=CheckpointReader(raw, archive['size'])) as compressed, \
            io.TextIOWrapper(compressed, encoding='utf-8') as stream:
        while True:
            line = stream.readline(1048577)
            if not line:
                break
            if len(line.encode())>1048576:
                raise resources.BudgetExceeded()
            if stop and stop.is_set():
                raise InterruptedError('Processing stopped; raw archive retained')
            if batch_size+len(line.encode())>resources.batch_bytes():
                flush()
            batch_size += len(line.encode())
            row = json.loads(line)
            if row.get('type') == 'context':
                flush()
                settings = row.get('settings', {})
                runs = {r['id']: r for r in row.get('runs', [])}
            elif row.get('type') == 'sample':
                ordinal += 1
                sample = {k: v for k, v in row.items() if k not in ('type', 'archive_sequence')}
                samples.append(sample)
            elif row.get('type') == 'incident':
                events.append(row)
            if len(samples)+len(events) >= 500:
                flush()
    flush()
    if ordinal != archive['sample_count']:
        raise ValueError('Archive count differs from its checkpoint')
    store.execute('UPDATE report_archives SET size=?,through=?,valid=1 WHERE id=?',
                  (archive['size'], ordinal, archive['id']))


def capture_call(report, con=None):
    """Keep complete derived reports independently of the raw test/count cache."""
    if report.get('source') != 'call':
        return
    if con is None:
        with store.db() as connection:
            capture_call(report, connection)
        return
    start, end = report['start'], report['end']
    con.execute('''INSERT INTO report_sources VALUES (?,?,?,?,?,?) ON CONFLICT(server_id,source_id)
      DO UPDATE SET start=excluded.start,end=excluded.end,payload=excluded.payload,verified=excluded.verified''',
                (report['server_id'], report['source_id'], start, end, pack(report), int(not report.get('analysis_failed'))))
    mark_dirty(con, report['server_id'], start, end)


def capture_missing_calls(id_):
    # Upgrade cached conclusions before their 200-result cache is trimmed.
    for row in store.rows("SELECT source_id FROM network_diagnostics WHERE server_id=? AND source='call'", (id_,)):
        if not store.one('SELECT 1 FROM report_sources WHERE server_id=? AND source_id=?', (id_, row['source_id'])):
            saved = store.evidence_row('network_diagnostics','payload', "server_id=? AND source='call' AND source_id=?", (id_,row['source_id']))
            capture_call(unpack(saved['payload']))
    for row in store.rows("SELECT id,created,finished,status,config,summary FROM network_call_tests WHERE server_id=? AND status!='running'", (id_,)):
        if store.one('SELECT 1 FROM report_sources WHERE server_id=? AND source_id=? AND verified=1', (id_, row['id'])):
            continue
        failed = bool(json.loads(store.one('SELECT result FROM network_call_tests WHERE id=?',(row['id'],))['result']).get('evidence_error'))
        if store.one('SELECT 1 FROM call_test_evidence WHERE test_id=?', (row['id'],)):
            try:
                from . import network_diagnostics
                report = network_diagnostics.report(network.host(id_), 'call', row['id'])
                capture_call(report)
                continue
            except Exception:
                logging.exception('Saved test analysis deferred; full test evidence protected')
                failed = True
        cfg = json.loads(row['config'])
        summary = json.loads(row['summary'])
        capture_call({'source': 'call', 'source_id': row['id'], 'server_id': id_, 'start': row['created'],
            'end': row['finished'] or row['created'], 'status': row['status'],
            'context': {k: cfg[k] for k in ('profile', 'schedule_name') if k in cfg},
            'conclusion': 'Test finished without a complete retained analysis.',
            'directions': [{'direction':k, 'summary':v.get('summary',{})} for k,v in summary.get('directions',{}).items()],
            'quality': ['Detailed call analysis was unavailable when this report was compiled.']+(['Full saved test evidence is protected from cleanup until analysis succeeds.'] if failed else []),
            'analysis_failed': failed, 'episodes': [], 'evidence': {}, 'root_cause': 'not_established'})


def compile_day(id_, day, cfg):
    start, end = bounds(day, cfg['timezone'])
    minutes, markers, manifests = {}, {}, {}
    invalid, retained = 0, 0
    with store.db() as con:
        cursor = con.execute('''SELECT c.*,a.id,a.size,a.through FROM report_chunks c
          JOIN report_archives a ON a.id=c.archive_id WHERE a.server_id=? AND a.valid=1
          AND c.start<? AND c.end>? ORDER BY c.start,c.archive_id,c.part''', (id_, end, start))
        for row in cursor:
            data = unpack(row['payload'])
            # Conservative additive allowance includes merged-away duplicate facts.
            retained += resources.size(data)
            if retained>resources.budget()*4:
                retained = resources.size(minutes)+resources.size(markers)+resources.size(data)
                if retained>resources.budget()*2:
                    raise resources.BudgetExceeded()
            manifests[row['archive_id']] = {'archive_id': row['archive_id'], 'bytes': row['size'], 'sample_count': row['through']}
            invalid += data['invalid_samples']
            for minute in data['minutes']:
                if not start <= minute['at'] < end:
                    continue
                if minute['at'] in minutes:
                    facts.merge_minute(minutes[minute['at']], minute)
                else:
                    minutes[minute['at']] = minute
            for marker in data['markers']:
                if start <= marker['measured'] < end and marker.get('complete', 0) >= markers.get(marker['id'], {}).get('complete', -1):
                    markers[marker['id']] = marker
    calls = []
    retained = resources.size(minutes)+resources.size(markers)
    with store.db() as con:
        for r in con.execute('SELECT payload FROM report_sources WHERE server_id=? AND start<? AND (end>? OR start>=?) ORDER BY start,source_id', (id_,end,start,start)):
            call = unpack(r['payload'])
            retained += resources.size(call)
            if retained>resources.budget()*2:
                raise resources.BudgetExceeded()
            calls.append(call)
    minute_rows = sorted(minutes.values(), key=lambda m: m['at'])
    kinds = dict(sum((Counter(m['kinds']) for m in minute_rows), Counter()))
    telemetry = {}
    for minute in minute_rows:
        for key, value in minute['telemetry'].items():
            if key not in telemetry:
                telemetry[key] = dict(value)
            else:
                a = telemetry[key]
                a.update(count=a['count']+value['count'], sum=a['sum']+value['sum'], min=min(a['min'],value['min']), max=max(a['max'],value['max']))
    report = {'schema': VERSION, 'server_id': id_, 'day': str(day), 'timezone': cfg['timezone'], 'start': start, 'end': end,
        'generated': time.time(), 'root_cause': 'not_established', 'paths': facts.path_totals(minute_rows),
        'timeline': facts.timeline(minute_rows, list(markers.values())), 'calls': calls, 'kinds': kinds, 'telemetry': telemetry,
        'markers': len(markers), 'minutes_with_recordings': len(minutes), 'minutes_in_day': int((end-start)/60),
        'manifests': list(manifests.values()), 'invalid_samples': invalid,
        'quality': ['Statistics describe retained measurements, not guaranteed continuous coverage.',
                    'Timeline entries overlap; their count is not a count of independent outages.',
                    'Selected evidence survives raw cleanup. Complete raw reanalysis is unavailable after raw retention expires.',
                    'Synthetic call tests do not measure real application call quality. Correlation does not establish a cause.']}
    if not minute_rows:
        report['quality'].insert(0, 'No raw recording facts were available for this day. This is missing coverage, not a healthy day.')
    state = store.one('SELECT retired_before FROM report_state WHERE server_id=?', (id_,))
    if state and state['retired_before'] and start < state['retired_before']:
        report['quality'].insert(0, 'Earlier evidence in this day was retired under the previous retention/timezone policy; this day is partial.')
    from . import modem_evidence
    report['modem'] = modem_evidence.assess(id_, start, end)
    report['modem_correlation'] = modem_evidence.correlate(report['modem'], report['timeline'])
    payload = pack(report)
    store.execute('INSERT OR REPLACE INTO report_days VALUES (?,?,?,?,?,?,?)',
                  (id_, str(day), cfg['timezone'], start, end, report['generated'], payload))
    return report


def scheduled_cleanup(id_, cfg, now):
    """Called only after all due days were committed successfully."""
    from . import network_archives
    settings, _ = network.config(id_)
    network_archives.trim(id_, settings, now, processed_only=True)
    # Whole call-test records cascade to evidence, after an independent summary exists.
    rows = store.rows('''SELECT t.id,t.created,t.status,coalesce(length(e.payload),0)+length(t.result) AS bytes
        FROM network_call_tests t LEFT JOIN call_test_evidence e ON e.test_id=t.id
        WHERE t.server_id=? ORDER BY t.created DESC,t.id''', (id_,))
    used = 0
    for row in rows:
        used += row['bytes']
        if row['status']=='running' or not (row['created'] < now-cfg['call_retention_days']*86400 or used>cfg['call_max_mib']*1048576):
            continue
        store.execute('''DELETE FROM network_call_tests WHERE id=? AND status!='running'
            AND EXISTS(SELECT 1 FROM report_sources s WHERE s.server_id=network_call_tests.server_id AND s.source_id=network_call_tests.id AND s.verified=1)''', (row['id'],))
    # Retire complete calendar days, their compact staging facts, and call excerpts together.
    store.execute('DELETE FROM report_days WHERE server_id=? AND timezone!=?', (id_, cfg['timezone']))
    rows = store.rows('SELECT day,start,end,length(payload) AS bytes FROM report_days WHERE server_id=? AND timezone=? ORDER BY day DESC', (id_, cfg['timezone']))
    local_day = dt.datetime.fromtimestamp(now, ZoneInfo(cfg['timezone'])).date()
    cutoff = bounds(local_day-dt.timedelta(days=cfg['retention_days']), cfg['timezone'])[0]
    retire_before(id_, cutoff)
    # Budget includes published reports, call conclusions and compact staging
    # facts. Retire oldest complete days together, retaining the newest report.
    for row in reversed(rows[1:]):
        if sum(stored_bytes(id_).values()) <= cfg['max_mib']*1048576:
            break
        retire_before(id_, row['end'])


def retire_before(id_, cutoff):
    with store.db() as con:
        con.execute('INSERT OR IGNORE INTO report_state(server_id) VALUES (?)', (id_,))
        con.execute('UPDATE report_state SET retired_before=max(coalesce(retired_before,0),?) WHERE server_id=?', (cutoff,id_))
        con.execute('DELETE FROM report_modem_samples WHERE server_id=? AND measured<?', (id_, cutoff))
        con.execute('DELETE FROM report_days WHERE server_id=? AND end<=?', (id_, cutoff))
        con.execute('DELETE FROM report_sources WHERE server_id=? AND end<?', (id_, cutoff))
        con.execute('''DELETE FROM report_chunks WHERE end<=? AND archive_id IN
            (SELECT id FROM report_archives WHERE server_id=? AND valid=1)''', (cutoff, id_))
        con.execute('DELETE FROM report_dirty WHERE server_id=? AND day<?', (id_, cutoff-86400))


def stored_bytes(id_):
    sizes = {}
    for name, sql in (
        ('modem_evidence', 'SELECT coalesce(sum(length(payload)),0) AS n FROM report_modem_samples WHERE server_id=?'),
        ('daily_reports', 'SELECT coalesce(sum(length(payload)),0) AS n FROM report_days WHERE server_id=?'),
        ('call_summaries', 'SELECT coalesce(sum(length(payload)),0) AS n FROM report_sources WHERE server_id=?'),
        ('compact_facts', 'SELECT coalesce(sum(length(c.payload)),0) AS n FROM report_chunks c JOIN report_archives a ON a.id=c.archive_id WHERE a.server_id=?')):
        sizes[name] = store.one(sql, (id_,))['n']
    return sizes


def run_due(id_, stop=None, now=None):
    now = time.time() if now is None else now
    cfg = policy(id_)
    if not cfg['enabled']:
        return
    state = store.one('SELECT * FROM report_state WHERE server_id=?', (id_,)) or {}
    due = slot(now, cfg)
    if not state.get('requested') and state.get('last_slot', 0) is not None and (state.get('last_slot') or 0) >= due:
        return
    store.execute('INSERT OR IGNORE INTO report_state(server_id) VALUES (?)', (id_,))
    store.execute('UPDATE report_state SET updated=?,progress=?,error=NULL WHERE server_id=?', (now, 'Preparing recording facts', id_))
    try:
        from .network_runtime import read_status
        runtime = read_status(id_)
        if runtime.get('writer_stalled') or runtime.get('oldest_unsaved_seconds', 0) > resources.flush_seconds()+2:
            raise ValueError('Waiting for buffered recordings before publishing the daily report')
        # This worker is the sole archive writer. Active legacy files can be
        # imported through their committed byte checkpoint without rotating.
        for archive in store.rows('SELECT * FROM network_archives WHERE server_id=? ORDER BY day', (id_,)):
            if not ready(archive):
                import_archive(archive, stop)
        capture_missing_calls(id_)
        zone = ZoneInfo(cfg['timezone'])
        today = dt.datetime.fromtimestamp(now if state.get('requested') else due, zone).date()
        days = {today-DAY}
        dirty = store.rows('SELECT day,revision FROM report_dirty WHERE server_id=?', (id_,))
        oldest = today-dt.timedelta(days=cfg['retention_days'])
        for row in dirty:
            for at in (row['day'], row['day']+86399):
                date = dt.datetime.fromtimestamp(at, zone).date()
                if oldest <= date < today:
                    days.add(date)
        # Byte retention may have retired dates inside the age window. A late
        # reading must not resurrect a previously retired day as a partial report.
        floor = state.get('retired_before') or 0
        days = {day for day in days if bounds(day, cfg['timezone'])[1] > floor}
        # Never claim completion while a legacy open file still has unimported history.
        if any(not ready(a) for a in store.rows('SELECT * FROM network_archives WHERE server_id=?', (id_,))):
            raise ValueError('Waiting for the scheduled archive rotation to finish legacy evidence')
        if store.one('SELECT 1 FROM network_samples WHERE server_id=? AND archived=0 LIMIT 1', (id_,)):
            # Check only readings belonging to completed calendar days. Current
            # day writes may continue throughout the job.
            cutoff = bounds(today, cfg['timezone'])[0]
            if store.one('SELECT 1 FROM network_samples WHERE server_id=? AND archived=0 AND measured<? LIMIT 1', (id_, cutoff)):
                raise ValueError('Waiting for older recordings to reach their archive checkpoint')
        for day in sorted(days):
            if stop and stop.is_set():
                raise InterruptedError()
            store.execute('UPDATE report_state SET progress=?,updated=? WHERE server_id=?', ('Compiling '+str(day), time.time(), id_))
            compile_day(id_, day, cfg)
        scheduled_cleanup(id_, cfg, now)
        with store.db() as con:
            # Dirty UTC days touching today stay pending for tomorrow's calendar report.
            boundary = bounds(today, cfg['timezone'])[0]
            for row in dirty:
                con.execute('DELETE FROM report_dirty WHERE server_id=? AND day=? AND revision=? AND day+86400<=?', (id_, row['day'], row['revision'], boundary))
            con.execute('UPDATE report_state SET last_slot=?,last_success=?,requested=0,error=NULL,progress=NULL,updated=? WHERE server_id=?', (due, now, time.time(), id_))
    except Exception as exc:
        logging.exception('Scheduled network report failed; raw archive cleanup withheld')
        message = str(exc) if isinstance(exc, (ValueError, InterruptedError)) else 'Processing failed; raw archives retained. Check recorder logs and available space.'
        store.execute('UPDATE report_state SET error=?,progress=NULL,updated=? WHERE server_id=?', (message or 'Processing interrupted; will retry.', time.time(), id_))


@router.get('')
def listing(id_: str, user=Depends(authenticated)):
    network.host(id_)
    cfg = policy(id_)
    from . import storage_usage
    return {'policy': cfg, 'state': store.one('SELECT * FROM report_state WHERE server_id=?', (id_,)),
            'storage': storage_usage.collect() if user['role']=='admin' else None,
            'stored_bytes': stored_bytes(id_),
            'budget_exceeded': sum(stored_bytes(id_).values()) > cfg['max_mib']*1048576,
            'next_run': next_slot(time.time(), cfg) if cfg['enabled'] else None,
            'days': store.rows('SELECT day,generated,length(payload) AS bytes FROM report_days WHERE server_id=? AND timezone=? ORDER BY day DESC LIMIT 366', (id_, cfg['timezone']))}


@router.put('/policy')
def save_policy(id_: str, body: Policy, user=Depends(admin)):
    network.host(id_)
    with store.db() as con:
        con.execute('INSERT OR REPLACE INTO report_policy VALUES (?,?)', (id_, body.model_dump_json()))
        con.execute('INSERT OR IGNORE INTO report_state(server_id) VALUES (?)', (id_,))
        con.execute('UPDATE report_state SET last_slot=NULL WHERE server_id=?', (id_,))
        for row in con.execute('SELECT start,end FROM report_days WHERE server_id=?', (id_,)).fetchall():
            mark_dirty(con, id_, row['start'], row['end']-1)
    return {'ok': True}


@router.post('/run')
def request_run(id_: str, user=Depends(admin)):
    network.host(id_)
    if not policy(id_)['enabled']:
        raise HTTPException(409, 'Enable scheduled processing before requesting a run')
    store.execute('INSERT INTO report_state(server_id,requested) VALUES (?,1) ON CONFLICT(server_id) DO UPDATE SET requested=1', (id_,))
    return {'ok': True, 'message': 'Queued for the recorder maintenance worker'}


def period(id_, start, end):
    cfg = policy(id_)
    if end < start or (end-start).days > 365:
        raise HTTPException(422, 'Choose an inclusive date range of at most 366 days')
    sizes = store.rows('SELECT substr(payload,-4) AS trailer FROM report_days WHERE server_id=? AND timezone=? AND day>=? AND day<=?',
                      (id_, cfg['timezone'], str(start), str(end)))
    if sum(int.from_bytes(r['trailer'], 'little') for r in sizes) > resources.budget():
        raise HTTPException(413, 'The detailed evidence for this range exceeds the configured processing budget. Choose a shorter range.')
    rows = store.rows('SELECT payload FROM report_days WHERE server_id=? AND timezone=? AND day>=? AND day<=? ORDER BY day',
                      (id_, cfg['timezone'], str(start), str(end)))
    reports = [unpack(row['payload']) for row in rows]
    expected = [str(start+dt.timedelta(days=n)) for n in range((end-start).days+1)]
    found = {r['day'] for r in reports}
    return {'start_day': str(start), 'end_day': str(end), 'timezone': cfg['timezone'], 'days': reports,
            'missing_days': [day for day in expected if day not in found]}


@router.get('/period')
def get_period(id_: str, start: dt.date, end: dt.date, format: str = Query(default='html', pattern='^(html|json)$'), user=Depends(authenticated)):
    server = network.host(id_)
    with resources.processing():
        try:
            report = period(id_, start, end)
            if format == 'json':
                from fastapi.responses import JSONResponse
                return JSONResponse(report)
            from .report_html import render
            return HTMLResponse(render(server['name'], report))
        except resources.BudgetExceeded as exc:
            raise HTTPException(413, str(exc)) from None
