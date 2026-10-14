"""Explicit, bounded synthetic UDP loads run on the on-site Harbour host.

iperf3 processes are separate from the diagnostics recorder. No router command
is executed. Receiver statistics remain separate from ICMP RTT and real media.
"""
import json
import math
import os
import selectors
import shutil
import subprocess
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import call_profiles, logins, network, store
from .auth import admin, authenticated

router = APIRouter(prefix='/api/servers/{id_}/network/call-tests')
guard = threading.RLock()
active = {}
OUTPUT_LIMIT = 24 * 1024 * 1024


def initialize(recover=True):
    with store.db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS network_call_tests (
          id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          created REAL NOT NULL, finished REAL, status TEXT NOT NULL, phase TEXT NOT NULL,
          config TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', summary TEXT NOT NULL DEFAULT '{}', error TEXT,
          event_id TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS network_one_call_test
          ON network_call_tests((1)) WHERE status='running';
        CREATE TABLE IF NOT EXISTS call_test_schedules (
          id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          name TEXT NOT NULL, config TEXT NOT NULL, enabled INTEGER NOT NULL, interval_seconds INTEGER NOT NULL,
          next_run REAL, last_run REAL, last_test TEXT, last_outcome TEXT);
        ''')
        con.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in con.execute('PRAGMA table_info(call_test_schedules)')}
        if 'id' not in columns:
            con.execute('ALTER TABLE call_test_schedules RENAME TO call_test_schedules_legacy')
            con.execute('''CREATE TABLE call_test_schedules (
                id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                name TEXT NOT NULL, config TEXT NOT NULL, enabled INTEGER NOT NULL, interval_seconds INTEGER NOT NULL,
                next_run REAL, last_run REAL, last_test TEXT, last_outcome TEXT)''')
            for row in con.execute('SELECT * FROM call_test_schedules_legacy').fetchall():
                schedule_id = uuid.uuid4().hex
                con.execute('INSERT INTO call_test_schedules VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (schedule_id, row['server_id'], 'Recurring call test', row['config'], row['enabled'],
                             row['interval_seconds'], row['next_run'], row['last_run'], row['last_test'], row['last_outcome']))
                # Preserve attribution for all retained runs of the former single schedule.
                con.execute("""UPDATE network_call_tests SET config=json_set(config,'$.schedule_id',?,'$.schedule_name',?)
                    WHERE server_id=? AND json_extract(config,'$.source')='schedule'""",
                    (schedule_id, 'Recurring call test', row['server_id']))
            con.execute('DROP TABLE call_test_schedules_legacy')
        if 'live' not in {row[1] for row in con.execute('PRAGMA table_info(network_call_tests)')}:
            con.execute("ALTER TABLE network_call_tests ADD COLUMN live TEXT NOT NULL DEFAULT '{}'")
        con.execute('CREATE INDEX IF NOT EXISTS call_schedules_server ON call_test_schedules(server_id)')
        # The supported deployment has one web process. Never resume traffic
        # automatically after a restart; the recorder has an independent lifetime.
        if recover:
            con.execute("UPDATE network_call_tests SET status='interrupted',phase='finished',finished=?,error=? WHERE status='running'",
                        (time.time(), 'Harbour restarted; this test was not resumed'))
            con.execute("UPDATE call_test_schedules SET next_run=?+interval_seconds,last_outcome='Missed while Harbour was offline; next run scheduled' WHERE enabled=1 AND next_run<=?",
                        (time.time(), time.time()))
    from . import call_evidence
    call_evidence.initialize()


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint: str
    endpoint_id: str | None = Field(default=None, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    port: int = Field(default=5201, ge=1024, le=65534, strict=True)
    profile: str = 'facetime'
    upload_kbps: int = Field(default=2000, ge=64, le=8000, strict=True)
    download_kbps: int = Field(default=2000, ge=64, le=8000, strict=True)
    packet_bytes: int = Field(default=1200, ge=128, le=1400, strict=True)
    duration: int = Field(default=180, ge=10, le=3600, strict=True)
    baseline: int = Field(default=60, ge=0, le=120, strict=True)
    recovery: int = Field(default=60, ge=0, le=120, strict=True)

    @model_validator(mode='before')
    @classmethod
    def preset_rates(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            preset = call_profiles.profile(data.get('profile', 'facetime'))
            if preset:
                for key in ('upload_kbps', 'download_kbps'):
                    data.setdefault(key, preset[key])
        return data

    @field_validator('endpoint')
    @classmethod
    def address(cls, value):
        # Numeric, unicast data only: no shell syntax, URL or DNS rebinding.
        return network.Settings(targets=[value]).targets[0]

    @field_validator('profile')
    @classmethod
    def known_profile(cls, value):
        if call_profiles.profile(value) is None:
            raise ValueError('Choose a supported call profile')
        return value


def public(row, full=False):
    result = dict(row)
    result.pop('summary', None)
    result['config'] = json.loads(row['config'])
    result['config'].pop('connection_signature', None)
    result['result'] = json.loads(row['result'])
    result['live'] = json.loads(row.get('live', '{}'))
    if not full:
        result['result'].pop('partial_output', None)
        for direction in result['result'].get('directions', {}).values():
            direction.pop('raw', None)
            direction.pop('intervals', None)
    return result


TEST_COLUMNS = 'id,server_id,created,finished,status,phase,config,summary AS result,error,event_id,live'


def listing(id_, schedule_id=None):
    clause, args = (" AND json_extract(config,'$.schedule_id')=?", (id_, schedule_id)) if schedule_id else ('', (id_,))
    rows = store.rows('SELECT ' + TEST_COLUMNS + ' FROM network_call_tests WHERE server_id=?' + clause +
                      ' ORDER BY created DESC,id DESC', args)
    running = store.one("SELECT " + TEST_COLUMNS + " FROM network_call_tests WHERE server_id=? AND status='running'", (id_,))
    saved = schedules(id_)
    return {'profiles': [call_profiles.profile(p['id']) for p in call_profiles.PROFILES],
            'available': bool(shutil.which('iperf3')), 'tests': [public(r) for r in rows],
            'running': public(running) if running else None, 'schedules': saved,
            'schedule': saved[0] if len(saved) == 1 else None}


def schedules(id_):
    rows = store.rows('SELECT * FROM call_test_schedules WHERE server_id=? ORDER BY rowid', (id_,))
    for row in rows:
        row['config'] = json.loads(row['config'])
        row['enabled'] = bool(row['enabled'])
        last = store.one('SELECT status,phase FROM network_call_tests WHERE id=?', (row['last_test'],)) if row['last_test'] else None
        row['last_status'] = (last['phase'] if last['status'] == 'running' else last['status']) if last else None
    return rows


def schedule(id_, schedule_id=None):
    return next((r for r in schedules(id_) if schedule_id is None or r['id'] == schedule_id), None)


def require_schedule(id_, schedule_id):
    network.host(id_)
    row = schedule(id_, schedule_id)
    if not row:
        raise HTTPException(404, 'Recurring test not found')
    return row


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='Recurring call test', min_length=1, max_length=80)
    enabled: bool
    interval_minutes: int = Field(default=120, ge=1, le=10080, strict=True)
    first_run: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    test: Settings

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Enter a name for this recurring test')
        return value

    @model_validator(mode='after')
    def enough_time(self):
        if self.interval_minutes * 60 < self.test.duration + self.test.baseline + self.test.recovery + 30:
            raise ValueError('The interval must allow the full test, quiet periods and at least 30 seconds for collection')
        if not self.test.endpoint_id:
            raise ValueError('Choose a managed remote endpoint for recurring tests')
        return self


def write_schedule(id_, body, schedule_id=None):
    from . import call_endpoint
    network.host(id_)
    if store.DEMO:
        raise HTTPException(400, 'Scheduled tests are disabled in demo mode')
    endpoint = call_endpoint.saved(body.test.endpoint_id)
    if body.enabled and (not endpoint['deployed'] or endpoint['endpoint'] != body.test.endpoint or endpoint['port'] != body.test.port):
        raise HTTPException(400, 'Choose a confirmed remote endpoint with matching traffic ports')
    with guard:
        previous = require_schedule(id_, schedule_id) if schedule_id else None
        interval = body.interval_minutes * 60
        next_run = (body.first_run if body.first_run is not None else
                    previous['next_run'] if previous and previous['enabled'] and previous['interval_seconds'] == interval
                    else time.time() + interval) if body.enabled else None
        if previous:
            store.execute("""UPDATE call_test_schedules SET name=?,config=?,enabled=?,interval_seconds=?,next_run=?
                WHERE id=? AND server_id=?""", (body.name, body.test.model_dump_json(), int(body.enabled), interval, next_run, schedule_id, id_))
        else:
            schedule_id = uuid.uuid4().hex
            store.execute("""INSERT INTO call_test_schedules(id,server_id,name,config,enabled,interval_seconds,next_run)
                VALUES(?,?,?,?,?,?,?)""", (schedule_id, id_, body.name, body.test.model_dump_json(), int(body.enabled), interval, next_run))
    return schedule(id_, schedule_id)


@router.get('/schedules')
def get_schedules(id_: str, user=Depends(authenticated)):
    network.host(id_)
    return {'schedules': schedules(id_)}


@router.post('/schedules')
def create_schedule(id_: str, body: ScheduleInput, user=Depends(admin)):
    return write_schedule(id_, body)


@router.put('/schedules/{schedule_id}')
def edit_schedule(id_: str, schedule_id: str, body: ScheduleInput, user=Depends(admin)):
    return write_schedule(id_, body, schedule_id)


@router.delete('/schedules/{schedule_id}')
def delete_schedule(id_: str, schedule_id: str, user=Depends(admin)):
    with guard:
        require_schedule(id_, schedule_id)
        store.execute('DELETE FROM call_test_schedules WHERE id=? AND server_id=?', (schedule_id, id_))
    return {'ok': True}


@router.post('/schedules/{schedule_id}/start')
def start_schedule_now(id_: str, schedule_id: str, user=Depends(admin)):
    with guard:
        row = require_schedule(id_, schedule_id)
        response = start_test(id_, Settings(**row['config']), source='schedule-now', schedule_row=row)
        store.execute('UPDATE call_test_schedules SET last_run=?,last_test=?,last_outcome=? WHERE id=?',
                      (time.time(), response['id'], 'Started manually', schedule_id))
        # Starting now does not alter the recurring cadence or enable a paused schedule.
        return response


# Compatibility for an old browser during upgrade. Never blanket-edit/delete multiple schedules.
@router.put('/schedule')
def save_schedule(id_: str, body: ScheduleInput, user=Depends(admin)):
    with guard:
        rows = schedules(id_)
        if len(rows) > 1:
            raise HTTPException(409, 'Choose the individual recurring test to edit')
        return write_schedule(id_, body, rows[0]['id'] if rows else None)


@router.delete('/schedule')
def remove_schedule(id_: str, user=Depends(admin)):
    network.host(id_)
    with guard:
        rows = schedules(id_)
        if len(rows) > 1:
            raise HTTPException(409, 'Choose the individual recurring test to remove')
        if rows:
            store.execute('DELETE FROM call_test_schedules WHERE id=?', (rows[0]['id'],))
    return {'ok': True}


def run_due(now=None, shutdown=None):
    if store.DEMO:
        return
    now = now if now is not None else time.time()
    due = store.rows('SELECT id,server_id FROM call_test_schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run,id', (now,))
    for item in due:
        if shutdown and shutdown.is_set():
            return
        with guard:
            row = schedule(item['server_id'], item['id'])
            if not row or not row['enabled'] or row['next_run'] > now:
                continue
            if store.one("SELECT id FROM network_call_tests WHERE status='running'"):
                if row['last_outcome'] != 'Waiting for the current test to finish':
                    store.execute('UPDATE call_test_schedules SET last_outcome=? WHERE id=?', ('Waiting for the current test to finish', row['id']))
                continue
            # At most one due invocation per schedule. Skip missed interval slots,
            # and let other schedules wait without mixing their traffic loads.
            following = row['next_run'] + (int((now-row['next_run'])//row['interval_seconds'])+1)*row['interval_seconds']
            store.execute('UPDATE call_test_schedules SET next_run=? WHERE id=?', (following, row['id']))
            try:
                response = start_test(item['server_id'], Settings(**row['config']), source='schedule', shutdown=shutdown, schedule_row=row)
                store.execute('UPDATE call_test_schedules SET last_run=?,last_test=?,last_outcome=? WHERE id=?',
                              (now, response['id'], 'Started', row['id']))
            except HTTPException as exc:
                store.execute('UPDATE call_test_schedules SET last_outcome=? WHERE id=?', ('Skipped: '+str(exc.detail), row['id']))
            except Exception:
                store.execute('UPDATE call_test_schedules SET last_outcome=? WHERE id=?',
                              ('Skipped: test could not be started; check recording and endpoint settings', row['id']))


def schedule_loop(stop):
    while not stop.wait(5):
        try:
            run_due(shutdown=stop)
        except Exception:
            # A transient database error must not kill future scheduling. No
            # request bodies, tokens or SSH credentials enter scheduler logs.
            continue


def context(id_, start, end):
    rows = store.rows('''SELECT id,server_id,created,finished,status,phase,config,summary AS result,error,event_id
        FROM network_call_tests WHERE server_id=? AND created<=? AND COALESCE(finished,?)>=?
        ORDER BY created LIMIT 20''', (id_, end, time.time(), start))
    return [public(r) for r in rows]


@router.get('')
def get(id_: str, schedule_id: str | None = Query(None, max_length=64), user=Depends(authenticated)):
    network.host(id_)
    return listing(id_, schedule_id)


@router.get('/live')
def live_results(id_: str, schedule_id: str | None = Query(None, max_length=64), user=Depends(authenticated)):
    network.host(id_)
    data = listing(id_, schedule_id)
    test = data['running']
    if test:
        for direction in ('upload', 'download'):
            reading = test['live'].get(direction)
            if reading:
                observed_at = number(reading.get('observed_at'))
                age = time.time() - observed_at if observed_at is not None else None
                # Use Harbour's clock, not the browser's potentially skewed clock.
                reading.update(age_seconds=age, stale=age is None or not 0 <= age <= 10)
    if test and test['phase'] == 'loading' and test['config'].get('remote_live'):
        from . import call_endpoint
        try:
            began = time.monotonic()
            remote = json.loads(call_endpoint.fetch(test['config']['endpoint_id'], '/live', limit=32768, timeout=2))
            local = test['live'].get('upload', {})
            observed = remote.get('directions', {}).get('upload', {})
            if (remote.get('endpoint_id') == test['config']['endpoint_id'] and local.get('cookie')
                    and observed.get('cookie') == local['cookie'] and observed.get('sender') is False):
                # The remote reports sample age on its own monotonic clock. A
                # successful HTTP request must not make an old interval fresh.
                age = number(observed.get('age_seconds'))
                if age is not None:
                    age += time.monotonic() - began
                test['live']['upload_receiver'] = {
                    **measurement(observed), 'sender': False, 'age_seconds': age,
                    'stale': age is None or age > 10, 'received_at': time.time()}
        except (HTTPException, ValueError, TypeError, AttributeError):
            test['live']['remote_error'] = 'Live remote readings unavailable'
    return data


@router.get('/{test_id}')
def download(id_: str, test_id: str, user=Depends(authenticated)):
    network.host(id_)
    row = store.one('SELECT * FROM network_call_tests WHERE id=? AND server_id=?', (test_id, id_))
    if not row:
        raise HTTPException(404, 'Test not found')
    return Response(json.dumps(public(row, True), allow_nan=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="harbour-call-test-{test_id}.json"'})


def test_row(id_, test_id, full=True):
    network.host(id_)
    row = store.one('SELECT * FROM network_call_tests WHERE id=? AND server_id=?', (test_id, id_))
    if not row:
        raise HTTPException(404, 'Test not found')
    return public(row, full)


@router.get('/{test_id}/analysis')
def analysis(id_: str, test_id: str, user=Depends(authenticated)):
    from . import call_evidence
    test = test_row(id_, test_id, full=False)
    evidence = call_evidence.read(test_id)
    return {'test': test,
            'analysis': evidence['analysis'] if evidence else None,
            'collected_at': evidence['collected_at'] if evidence else None}


@router.post('/{test_id}/collect')
def collect(id_: str, test_id: str, user=Depends(admin)):
    from . import call_evidence
    test = test_row(id_, test_id)
    if test['status'] == 'running':
        raise HTTPException(409, 'Wait for the test to finish before collecting evidence')
    evidence = call_evidence.collect(test)
    return {'analysis': evidence['analysis'], 'collected_at': evidence['collected_at']}


@router.get('/{test_id}/export')
def aggregate_export(id_: str, test_id: str, user=Depends(authenticated)):
    from . import call_evidence, network_incidents
    test = test_row(id_, test_id)
    if test['status'] == 'running':
        raise HTTPException(409, 'Wait for the test to finish before exporting')
    evidence = call_evidence.read(test_id)
    return network_incidents.compressed_attachment(
        {'schema': 1, 'kind': 'harbour-call-flow-evidence', 'exported_at': time.time(),
         'test': test, 'evidence': evidence,
         'manifest': {'local': 'test.result.directions.*.raw and evidence.local.samples',
                      'remote': 'evidence.remote.records (original JSON and remote log paths)',
                      'analysis': 'evidence.analysis', 'clock': 'test.result.clock_before and clock_after',
                      'evidence_available': evidence is not None}},
        'harbour-call-flow-' + test_id + '.json.gz')


@router.post('')
def start(id_: str, body: Settings, user=Depends(admin)):
    return start_test(id_, body)


def start_test(id_, body, source='manual', shutdown=None, schedule_row=None):
    server = network.host(id_)
    if store.DEMO:
        raise HTTPException(400, 'Traffic tests are disabled in demo mode')
    if not server['monitoring_enabled'] or not network.config(id_)[0]['enabled']:
        raise HTTPException(400, 'Enable monitoring and network recording before starting a traffic test')
    executable = shutil.which('iperf3')
    if not executable:
        raise HTTPException(400, 'Install iperf3 on the Harbour host, or rebuild the Harbour container')
    config = body.model_dump()
    if body.endpoint_id:
        from . import call_endpoint
        endpoint = call_endpoint.saved(body.endpoint_id)
        if not endpoint['deployed'] or endpoint['endpoint'] != body.endpoint or endpoint['port'] != body.port:
            raise HTTPException(400, 'Choose a confirmed remote endpoint with matching traffic ports')
        remote_status = call_endpoint.status(body.endpoint_id)
        config['remote_live'] = remote_status.get('live_metrics') is True
        if body.duration > remote_status.get('max_duration', 300):
            raise HTTPException(400, 'Redeploy the remote endpoint to support calls up to 60 minutes')
    config.update(profile_snapshot=call_profiles.profile(body.profile),
                  connection_signature=logins.connection_signature(server),
                  scope='Fixed UDP load from the Harbour host to a user-controlled endpoint; not a real call or a test of the caller Wi-Fi',
                  estimated_payload_bytes=(body.upload_kbps+body.download_kbps)*1000*body.duration//8)
    id_test = uuid.uuid4().hex
    config.update(test_id=id_test, source=source)
    if schedule_row:
        config.update(schedule_id=schedule_row['id'], schedule_name=schedule_row['name'])
    with guard:
        if shutdown and shutdown.is_set():
            raise HTTPException(409, 'Harbour is shutting down')
        with store.db() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute("SELECT 1 FROM network_call_tests WHERE status='running'").fetchone():
                raise HTTPException(409, 'A traffic test is already running; stop it before starting another')
            con.execute('INSERT INTO network_call_tests(id,server_id,created,status,phase,config) VALUES (?,?,?,\'running\',\'baseline\',?)',
                        (id_test, id_, time.time(), json.dumps(config)))
            con.execute('''DELETE FROM network_call_tests WHERE server_id=? AND status!='running' AND id NOT IN
                (SELECT id FROM (SELECT id,ROW_NUMBER() OVER (PARTITION BY COALESCE(json_extract(config,'$.schedule_id'),'') ORDER BY created DESC,id DESC) AS n FROM network_call_tests WHERE server_id=?) WHERE n<=20)''', (id_, id_))
        cancel = threading.Event()
        thread = threading.Thread(target=execute, args=(id_test, id_, config, executable, cancel), daemon=True)
        active[id_test] = (cancel, thread)
        thread.start()
    return {'id': id_test}


@router.post('/{test_id}/stop')
def cancel_test(id_: str, test_id: str, user=Depends(admin)):
    network.host(id_)
    if not store.one('SELECT id FROM network_call_tests WHERE id=? AND server_id=?', (test_id, id_)):
        raise HTTPException(404, 'Test not found')
    with guard:
        item = active.get(test_id)
        if item:
            item[0].set()
    return {'ok': True}


class Cancelled(Exception):
    pass


def valid_host(id_, config):
    server = store.one('SELECT * FROM servers WHERE id=?', (id_,))
    return bool(server and server['server_type'] == 'openwrt' and server['monitoring_enabled']
                and network.config(id_)[0]['enabled']
                and logins.connection_signature(server) == config['connection_signature'])


def command(executable, config, direction):
    reverse = direction == 'download'
    return [executable, '-c', config['endpoint'], '-p', str(config['port']+int(reverse)),
            '-u', '-b', str(config[direction+'_kbps']*1000), '-l', str(config['packet_bytes']),
            '-t', str(config['duration']), '-i', '1', '-J', '--json-stream', '--json-stream-full-output', '--get-server-output',
            '--connect-timeout', '3000'] + (['--extra-data', 'harbour:' + config['test_id'] + ':' + direction]
                                          if config.get('test_id') else []) + (['-R'] if reverse else [])


def number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def measurement(row):
    return {key: number(row.get(key)) for key in ('start', 'end', 'seconds', 'bits_per_second',
                                                 'bytes', 'packets', 'lost_packets', 'lost_percent', 'jitter_ms')}


def parse_output(raw, direction, started):
    # --json-stream-full-output emits interval events followed by the original
    # complete JSON document. Retain that document for existing evidence exports.
    text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw
    data, offset = None, 0
    decoder = json.JSONDecoder()
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        document, offset = decoder.raw_decode(text, offset)
        if isinstance(document, dict) and 'event' not in document:
            data = document
    if data is None:
        raise ValueError('No complete iperf3 result returned')
    json.dumps(data, allow_nan=False)  # Reject non-finite raw values before they enter saved results.
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError(str(data.get('error', 'Invalid iperf3 result'))[:400] if isinstance(data, dict) else 'Invalid iperf3 result')
    summary = data.get('end', {}).get('sum_received')
    # A sender's zero jitter/loss is not a receiver measurement.
    if not isinstance(summary, dict) or summary.get('sender') is not False or not number(summary.get('packets')):
        raise ValueError('No usable receiver summary returned')
    receiver = data if direction == 'download' else data.get('server_output_json', {})
    intervals = []
    for interval in receiver.get('intervals', [])[:7202]:
        row = interval.get('sum', {})
        if row.get('sender') is False and not row.get('omitted'):
            intervals.append(measurement(row))
    return {'receiver': 'on-site' if direction == 'download' else 'remote',
            'summary': measurement(summary), 'intervals': intervals,
            'intervals_available': bool(intervals), 'process_started': started,
            'interval_time_basis': 'Seconds from this direction’s iperf3 test start; process start is an approximate wall-clock anchor',
            'client_version': data.get('start', {}).get('version'), 'raw': data}


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def run_load(config, executable, check, popen=subprocess.Popen):
    """Drain both pipes concurrently with bounded output, timeout and cancellation."""
    children, output, started = {}, {}, {}
    selector = selectors.DefaultSelector()
    pending, live, published = {}, {}, 0

    def publish():
        nonlocal published
        if config.get('test_id') and time.monotonic() - published >= 5:
            store.execute("UPDATE network_call_tests SET live=? WHERE id=? AND status='running'",
                          (json.dumps(live, allow_nan=False), config['test_id']))
            published = time.monotonic()

    def observe(direction, chunk):
        pending[direction] += chunk
        while b'\n' in pending[direction]:
            line, _, pending[direction] = pending[direction].partition(b'\n')
            # Stream event documents are single-line. Pretty-printed final JSON
            # is parsed once, after EOF, from the separately bounded raw output.
            if not line.startswith(b'{"event":'):
                continue
            event = json.loads(line)
            data = event.get('data', {})
            if event.get('event') == 'start':
                live[direction] = {'cookie': data.get('cookie')}
            elif event.get('event') == 'interval' and isinstance(data.get('sum'), dict):
                row = data['sum']
                if row.get('omitted') or type(row.get('sender')) is not bool:
                    continue
                stats = measurement(row)
                if row['sender']:
                    for key in ('lost_packets', 'lost_percent', 'jitter_ms'):
                        stats[key] = None
                live[direction] = {**live.get(direction, {}), **stats, 'sender': row['sender'], 'observed_at': time.time()}
    try:
        for direction in ('upload', 'download'):
            check()
            started[direction] = time.time()
            process = popen(command(executable, config, direction), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            children[direction] = process
            output[direction] = bytearray()
            pending[direction] = b''
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, direction)
        deadline = time.monotonic()+config['duration']+15
        while selector.get_map() or any(p.poll() is None for p in children.values()):
            check()
            if time.monotonic() > deadline:
                raise ValueError('Traffic test timed out; incomplete results are not a successful test')
            for key, _ in selector.select(.1):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    output[key.data].extend(chunk)
                    if len(output[key.data]) > OUTPUT_LIMIT:
                        raise ValueError('iperf3 output exceeded the result limit')
                    observe(key.data, chunk)
            publish()
            # Stop the other direction if a peer is absent/busy or a test fails.
            if any(p.poll() not in (None, 0) for p in children.values()):
                raise ValueError('iperf3 failed; check both endpoint ports, server availability and UDP reachability')
        return {direction: {**parse_output(raw, direction, started[direction]), 'process_finished': time.time()}
                for direction, raw in output.items()}
    except Exception as exc:
        exc.directions = {}
        exc.partial_output = {d: raw.decode('utf-8', 'replace') for d, raw in output.items()}
        for direction, raw in output.items():
            try:
                exc.directions[direction] = parse_output(raw, direction, started[direction])
            except (ValueError, TypeError, AttributeError):
                pass
        raise
    finally:
        for process in children.values():
            stop_process(process)
            process.stdout.close()
        selector.close()


def execute(test_id, id_, config, executable, cancel):
    result, status, error = {'directions': {}}, 'complete', None
    next_check = 0

    def check():
        nonlocal next_check
        if cancel.is_set():
            raise Cancelled('Stopped by the user or Harbour shutdown')
        if time.monotonic() >= next_check:
            if not valid_host(id_, config):
                raise Cancelled('Monitoring stopped or the server connection changed')
            next_check = time.monotonic()+.5

    def pause(phase, seconds):
        store.execute('UPDATE network_call_tests SET phase=? WHERE id=?', (phase, test_id))
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            check()
            cancel.wait(min(.1, max(0, end-time.monotonic())))
        check()

    def summary():
        return json.dumps({**{k:v for k,v in result.items() if k != 'partial_output'}, 'directions': {name: {k: v for k, v in data.items() if k not in ('raw', 'intervals')}
                          for name, data in result['directions'].items()}}, allow_nan=False)

    try:
        pause('baseline', config['baseline'])
        if config.get('endpoint_id'):
            from . import call_evidence
            result['clock_before'] = call_evidence.synchronize(config['endpoint_id'])
            check()
        result['load_started'] = time.time()
        event_id = uuid.uuid4().hex
        with store.db() as con:
            con.execute('INSERT INTO network_events(id,server_id,measured,label,automatic) VALUES (?,?,?,?,0)',
                        (event_id, id_, result['load_started'], 'Synthetic load: '+config['profile_snapshot']['name']))
            con.execute("UPDATE network_call_tests SET phase='loading',event_id=?,result=?,summary=? WHERE id=?",
                        (event_id, json.dumps(result), summary(), test_id))
        result['directions'] = run_load(config, executable, check)
        result['load_finished'] = time.time()
        store.execute('UPDATE network_call_tests SET result=?,summary=? WHERE id=?',
                      (json.dumps(result, allow_nan=False), summary(), test_id))
        pause('recovery', config['recovery'])
    except Cancelled as exc:
        status, error = 'cancelled', str(exc)
        result['directions'].update(getattr(exc, 'directions', {}))
        result['partial_output'] = getattr(exc, 'partial_output', {})
    except Exception as exc:
        status, error = 'failed', (exc.detail if isinstance(exc, HTTPException) else str(exc)[:400])
        result['directions'].update(getattr(exc, 'directions', {}))
        result['partial_output'] = getattr(exc, 'partial_output', {})
    finally:
        try:
            ended = time.time()
            if result.get('load_started'):
                result.setdefault('load_finished', ended)
            if config.get('endpoint_id'):
                try:
                    from . import call_evidence
                    result['clock_after'] = call_evidence.synchronize(config['endpoint_id'])
                except Exception:
                    result['clock_after'] = None
            store.execute("UPDATE network_call_tests SET phase='collecting' WHERE id=?", (test_id,))
            try:
                from . import call_evidence
                row = store.one('SELECT * FROM network_call_tests WHERE id=?', (test_id,))
                if row:
                    test = public(row, True)
                    test.update(result=result, status=status, finished=ended)
                    evidence = call_evidence.collect(test)
                    result['analysis'] = call_evidence.compact(evidence['analysis'])
            except Exception:
                result['evidence_error'] = 'Evidence collection is incomplete. Open Analysis and retry collection.'
            store.execute("UPDATE network_call_tests SET status=?,phase='finished',finished=?,result=?,summary=?,error=? WHERE id=?",
                          (status, ended, json.dumps(result, allow_nan=False), summary(), error, test_id))
        finally:
            with guard:
                active.pop(test_id, None)


def close_all():
    with guard:
        items = list(active.values())
        for cancel, _ in items:
            cancel.set()
    for _, thread in items:
        thread.join(timeout=5)
