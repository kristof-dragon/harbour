"""Standalone, standard-library authentication collector. Installed on the SSH host.

No TCP listener, command execution API, passwords or private-key collection.
The local socket is restricted to the configured Harbour SSH account.
"""
import argparse
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import pwd
import re
import secrets
import signal
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

VERSION = '1'
SOCKET = '/var/run/harbour-logins/collector.sock'
MAX_BATCH = 200
MAX_LINE = 32768
SERVICES = ('sshd', 'sshd-session', 'sshd-auth', 'login', 'sudo', 'su', 'gdm-password', 'lightdm', 'sddm', 'loginwindow', 'opendirectoryd')


def clean(value, limit=512):
    return ''.join(c for c in str(value or '') if c >= ' ' and c != '\x7f')[:limit]


def address(value):
    try:
        return str(ipaddress.ip_address(value))
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def boot_identity():
    try:
        if sys.platform == 'linux':
            return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def parse_auth(message, service='sshd', occurred_at=None, pid=None):
    """Parse evidence, not assumptions: missing/redacted fields remain null.

    Multiple authentication attempts on one connection remain separate events.
    PAM session-open records are not another successful authentication.
    """
    message = clean(message, 2048)
    if service not in SERVICES:
        return None
    event = dict(occurred_at=occurred_at, service='ssh' if service.startswith('sshd') else service,
                 result=None, event_type='authentication', username=None, uid=None,
                 method=None, source_ip=None, source_port=None, key_fingerprint=None,
                 key_algorithm=None, pid=pid, evidence=message)
    match = re.search(r'\b(Accepted|Failed|Partial) (\S+) for (invalid user )?(.*?) from (\S+) port (\d+)\b', message)
    if match:
        result, method, invalid, user, ip, port = match.groups()
        event.update(result={'Accepted': 'success', 'Failed': 'failure', 'Partial': 'partial'}[result],
                     method=method, username=None if user == '<private>' else user,
                     invalid_user=bool(invalid), source_ip=address(ip), source_port=int(port))
        key = re.search(r'ssh2: ([\w@.+-]+) (SHA256:[A-Za-z0-9+/=]+|MD5:[a-fA-F0-9:]+)', message)
        if key:
            event.update(key_algorithm=key[1], key_fingerprint=key[2],
                         credential_verified=result in ('Accepted', 'Partial'))
        cert = re.search(r' ID (.*?) \(serial (\d+)\) CA (\S+) (SHA256:[A-Za-z0-9+/=]+|MD5:[a-fA-F0-9:]+)', message)
        if cert:
            event.update(certificate_id=cert[1], certificate_serial=cert[2], ca_algorithm=cert[3], ca_fingerprint=cert[4])
        return event
    match = re.search(r'\bInvalid user (.*?) from (\S+)(?: port (\d+))?', message)
    if match:
        event.update(result='invalid_user', username=match[1], source_ip=address(match[2]),
                     source_port=int(match[3]) if match[3] else None, invalid_user=True)
        return event
    match = re.search(r'\b(?:Disconnected from|Disconnecting|Connection closed by|Received disconnect from) (?:authenticating user |invalid user |user )?(?:(\S+) )?(\S+) port (\d+)', message)
    if match and service.startswith('sshd'):
        event.update(event_type='disconnect', result='disconnected', username=match[1],
                     source_ip=address(match[2]), source_port=int(match[3]))
        return event
    if 'authentication failure' in message or 'Failed to authenticate' in message:
        user = re.search(r'\buser=(\S+)', message)
        ip = re.search(r'\brhost=(\S+)', message)
        event.update(result='failure', method='pam' if 'pam_' in message else 'unknown',
                     username=user[1] if user else None, source_ip=address(ip[1]) if ip else None)
        return event
    match = re.search(r'session (opened|closed) for user ([^\s(]+)(?:\(uid=(\d+)\))?', message)
    if match:
        event.update(event_type='session_start' if match[1] == 'opened' else 'session_end',
                     result='opened' if match[1] == 'opened' else 'closed', username=match[2],
                     uid=int(match[3]) if match[3] else None, method='pam')
        return event
    # Preserve rejection evidence even when no username was sent or it is redacted.
    if service.startswith('sshd') and re.search(r'maximum authentication attempts|not allowed because|refused|Unable to negotiate|Did not receive identification|kex_exchange_identification', message):
        event.update(event_type='connection', result='rejected')
        ip = re.search(r'\bfrom (\S+)(?: port (\d+))?', message)
        if ip:
            event.update(source_ip=address(ip[1]), source_port=int(ip[2]) if ip[2] else None)
        return event
    return None


class Spool:
    def __init__(self, path, max_events=20000):
        self.path, self.max_events = str(path), max_events
        with self.db() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT UNIQUE NOT NULL,
                  captured_at REAL NOT NULL, payload TEXT NOT NULL, acknowledged_at REAL);
                CREATE INDEX IF NOT EXISTS pending ON events(acknowledged_at,seq);
                CREATE TABLE IF NOT EXISTS open_sessions (identity TEXT PRIMARY KEY, started REAL, ambiguous INTEGER NOT NULL DEFAULT 0);
            ''')
            for key, value in [('collector_id', secrets.token_hex(16)), ('secret', secrets.token_hex(32)),
                               ('dropped', 0), ('acked_through', 0), ('acknowledged_at', None)]:
                con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get(self, key, default=None):
        with self.db() as con:
            row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    @staticmethod
    def set(con, key, value):
        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def checkpoint(self, key, value):
        with self.db() as con:
            self.set(con, key, value)

    def append(self, event, source_key, checkpoint=None):
        with self.db() as con:
            con.execute('BEGIN IMMEDIATE')
            if event and not con.execute('SELECT 1 FROM events WHERE source_key=?', (source_key,)).fetchone():
                event = dict(event)
                self.correlate(con, event)
                con.execute('INSERT OR IGNORE INTO events(source_key,captured_at,payload) VALUES (?,?,?)',
                            (source_key, time.time(), json.dumps(event, ensure_ascii=True)))
            if checkpoint:
                self.set(con, *checkpoint)

    @staticmethod
    def correlate(con, event):
        kind = event.get('event_type')
        session = event.get('session_id')
        if session is None and event.get('method') == 'pam':
            session = event.get('pid')
        if (kind not in ('session_start', 'session_end') or session is None or not event.get('boot_id') or
                not event.get('username') or event.get('occurred_at') is None):
            return
        identity = json.dumps([event.get(k) for k in ('source','boot_id','service','username')] + [str(session)])
        previous = con.execute('SELECT * FROM open_sessions WHERE identity=?', (identity,)).fetchone()
        if kind == 'session_start':
            con.execute('INSERT OR REPLACE INTO open_sessions VALUES (?,?,?)', (identity, event['occurred_at'], int(previous is not None)))
            event['session_started_at'] = event['occurred_at']
        elif previous:
            elapsed = event['occurred_at'] - previous['started']
            if not previous['ambiguous'] and elapsed >= 0:
                event.update(session_started_at=previous['started'], duration_seconds=elapsed)
            con.execute('DELETE FROM open_sessions WHERE identity=?', (identity,))
        # Bound optional correlation state; unmatched/ambiguous sessions stay unknown.
        con.execute('DELETE FROM open_sessions WHERE identity IN (SELECT identity FROM open_sessions ORDER BY started DESC LIMIT -1 OFFSET 2000)')

    def maintain(self, now=None):
        now = now or time.time()
        with self.db() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('DELETE FROM events WHERE acknowledged_at IS NOT NULL AND acknowledged_at<?', (now-86400,))
            excess = max(0, con.execute('SELECT count(*) FROM events').fetchone()[0] - self.max_events)
            # Prefer removing acknowledged records before sacrificing pending evidence.
            ids = con.execute('SELECT seq,acknowledged_at FROM events ORDER BY acknowledged_at IS NULL,seq LIMIT ?', (excess,)).fetchall()
            dropped = sum(r['acknowledged_at'] is None for r in ids)
            if dropped:
                old = json.loads(con.execute("SELECT value FROM meta WHERE key='dropped'").fetchone()[0])
                self.set(con, 'dropped', old + dropped)
                self.set(con, 'last_drop_at', now)
            con.executemany('DELETE FROM events WHERE seq=?', [(r['seq'],) for r in ids])

    def token(self, collector_id, through):
        return hmac.new(self.get('secret').encode(), (collector_id + ':' + str(through)).encode(), hashlib.sha256).hexdigest()

    def exchange(self, acknowledgement=None):
        collector_id = self.get('collector_id')
        if acknowledgement:
            through = acknowledgement.get('through')
            if (acknowledgement.get('collector_id') != collector_id or type(through) is not int or through < 1 or
                    not hmac.compare_digest(str(acknowledgement.get('token', '')), self.token(collector_id, through))):
                raise ValueError('Invalid acknowledgement')
            with self.db() as con:
                con.execute('BEGIN IMMEDIATE')
                previous = json.loads(con.execute("SELECT value FROM meta WHERE key='acked_through'").fetchone()[0])
                if through > previous:
                    now = time.time()
                    con.execute('UPDATE events SET acknowledged_at=? WHERE seq<=? AND acknowledged_at IS NULL', (now, through))
                    self.set(con, 'acked_through', through)
                    self.set(con, 'acknowledged_at', now)
        with self.db() as con:
            rows = con.execute('SELECT * FROM events WHERE acknowledged_at IS NULL ORDER BY seq LIMIT ?', (MAX_BATCH,)).fetchall()
            pending = con.execute('SELECT count(*) FROM events WHERE acknowledged_at IS NULL').fetchone()[0]
        batch = [{**json.loads(r['payload']), 'seq': r['seq'], 'captured_at': r['captured_at'],
                  'event_id': hashlib.sha256(r['source_key'].encode()).hexdigest()} for r in rows]
        through = rows[-1]['seq'] if rows else None
        return dict(protocol=1, collector_id=collector_id, version=VERSION, events=batch, pending=pending,
                    acknowledgement=dict(collector_id=collector_id, through=through, token=self.token(collector_id, through)) if through else None,
                    acked_through=self.get('acked_through'), acknowledged_at=self.get('acknowledged_at'),
                    dropped=self.get('dropped'), last_drop_at=self.get('last_drop_at'))


class Collector:
    def __init__(self, spool, platform=None, endpoint_helper=None, auth_file=None):
        self.spool, self.platform = spool, platform or sys.platform
        self.endpoint_helper, self.auth_file = endpoint_helper, auth_file
        self.stop = threading.Event()
        self.sources, self.processes, self.threads = {}, [], []
        self.guard = threading.Lock()
        self.last_errors = {}
        self.started_at = time.time()
        self.boot_id = boot_identity()

    def state(self, name, state, detail=''):
        with self.guard:
            previous = self.sources.get(name)
            self.sources[name] = dict(state=state, detail=clean(detail, 500), checked_at=time.time())
        problem = self.last_errors.get(name)
        if state in ('error', 'gap') and (not problem or problem[0] != detail or time.time()-problem[1] > 300):
            self.last_errors[name] = (detail, time.time())
            self.spool.append(dict(occurred_at=time.time(), service='collector', event_type='collection_gap',
                                   result='gap', evidence=name + ': ' + clean(detail, 500)), secrets.token_hex(16))

    def status(self):
        with self.guard:
            sources = json.loads(json.dumps(self.sources))
        return dict(started_at=self.started_at, checked_at=time.time(), platform=self.platform, sources=sources,
                    coverage=['ssh', 'pam-authentication', 'pam-sessions'] if self.platform == 'linux' else
                    ['ssh-logs', 'os-authentication', 'graphical-sessions', 'screen-sharing'] if self.endpoint_helper else ['ssh-logs'],
                    limitations=(['Coverage depends on services writing authentication records.'] if self.platform == 'linux' else
                                 ['Log fields may be redacted or unavailable; SSH key details depend on sshd logging.'] +
                                 ([] if self.endpoint_helper else ['Native macOS authentication events are not enabled.'])))

    def start(self):
        self.spool.append(dict(occurred_at=self.started_at, service='collector', event_type='collector_start', result='started',
                               evidence='Collector started. Replay depends on retained source logs; native events during downtime cannot be recovered.'), secrets.token_hex(16))
        targets = [(self.follow_file, 'auth-file')] if self.auth_file else [(self.follow_journal, 'journal')] if self.platform == 'linux' else [(self.follow_macos, 'unified-log'), (self.follow_macos_replay, 'unified-replay')]
        if self.platform == 'darwin' and self.endpoint_helper:
            targets.append((self.follow_endpoint, 'endpoint-security'))
        for fn, name in targets:
            self.state(name, 'starting')
            thread = threading.Thread(target=fn, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stop.set()
        with self.guard:
            processes = list(self.processes)
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for thread in self.threads:
            thread.join(timeout=5)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()

    def lines(self, argv, on_started=None):
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env={**os.environ, 'LC_ALL': 'C'})
        with self.guard:
            self.processes.append(process)
        try:
            if on_started:
                on_started()
            while not self.stop.is_set():
                line = process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    while line and not line.endswith(b'\n'):
                        line = process.stdout.readline(MAX_LINE + 1)
                    yield '{"collector_error":"Source record exceeded the size limit"}'
                    continue
                yield line.decode('utf-8', 'replace').strip()
            if process.poll() is None:
                process.terminate()
            if process.wait(timeout=3) and not self.stop.is_set():
                raise RuntimeError('Event source exited unsuccessfully')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            with self.guard:
                self.processes.remove(process)

    def journal_record(self, item):
        cursor = item.get('__CURSOR')
        if not cursor:
            return
        event = None
        if item.get('_UID') == '0':
            event = parse_auth(item.get('MESSAGE', ''), item.get('SYSLOG_IDENTIFIER', ''),
                               int(item.get('__REALTIME_TIMESTAMP', 0))/1e6 or None, item.get('SYSLOG_PID', item.get('_PID')))
        if event:
            event.update(source='journal', boot_id=item.get('_BOOT_ID'))
        self.spool.append(event, 'journal:' + cursor, ('journal_cursor', cursor))

    def follow_journal(self):
        while not self.stop.is_set():
            cursor = self.spool.get('journal_cursor')
            argv = ['/usr/bin/journalctl', '--follow', '--no-tail', '--no-pager', '--output=json']
            argv += ['--after-cursor', cursor] if cursor else ['--since', datetime.datetime.fromtimestamp(self.spool.get('journal_resume', self.started_at)).isoformat(sep=' ')]
            argv += ['SYSLOG_IDENTIFIER=' + name for name in SERVICES]
            try:
                self.state('journal', 'listening')
                for line in self.lines(argv):
                    try:
                        item = json.loads(line)
                    except ValueError:
                        if 'cursor' in line.lower() and ('failed' in line.lower() or 'seek' in line.lower()):
                            self.spool.checkpoint('journal_cursor', None)
                            self.spool.checkpoint('journal_resume', time.time()-300)
                            self.state('journal', 'gap', 'Saved journal cursor expired; source history cannot be fully recovered.')
                        continue
                    if 'collector_error' in item:
                        self.state('journal', 'gap', item['collector_error'])
                    else:
                        self.journal_record(item)
                if not self.stop.is_set():
                    raise RuntimeError('Journal event stream stopped')
            except Exception as exc:
                self.state('journal', 'error', str(exc))
            self.stop.wait(5)

    def follow_file(self):
        path = Path(self.auth_file)
        while not self.stop.is_set():
            try:
                with path.open('rb') as source:
                    stat = os.fstat(source.fileno())
                    identity = '%s:%s' % (stat.st_dev, stat.st_ino)
                    previous = self.spool.get('file_cursor')
                    generation = secrets.token_hex(8)
                    if previous and previous['identity'] == identity and previous['offset'] <= stat.st_size:
                        source.seek(previous['offset'])
                        generation = previous.get('generation', generation)
                    elif previous:
                        self.state('auth-file', 'gap', 'Authentication log rotated or truncated while collector was stopped.')
                    else:
                        source.seek(0, 2)  # Begin at installation, not arbitrary historical logs.
                    self.spool.checkpoint('file_cursor', dict(identity=identity, generation=generation, offset=source.tell()))
                    self.state('auth-file', 'listening')
                    while not self.stop.is_set():
                        offset = source.tell()
                        line = source.readline(MAX_LINE + 1)
                        if len(line) > MAX_LINE:
                            while line and not line.endswith(b'\n'):
                                line = source.readline(MAX_LINE + 1)
                            self.state('auth-file', 'gap', 'Oversized authentication record')
                            self.spool.checkpoint('file_cursor', dict(identity=identity, generation=generation, offset=source.tell()))
                            continue
                        if not line or not line.endswith(b'\n'):
                            source.seek(offset)
                            stat = path.stat()
                            if (stat.st_dev, stat.st_ino) != (os.fstat(source.fileno()).st_dev, os.fstat(source.fileno()).st_ino) or stat.st_size < offset:
                                break
                            self.stop.wait(.5)
                            continue
                        text = line.decode('utf-8', 'replace')
                        match = re.search(r'\b([\w-]+)\[(\d+)\]: (.*)', text)
                        moment = timestamp(text.split(' ', 1)[0])
                        if moment is None:
                            try:
                                now = datetime.datetime.now()
                                parsed = datetime.datetime.strptime(str(now.year) + ' ' + text[:15], '%Y %b %d %H:%M:%S')
                                if parsed > now + datetime.timedelta(days=1):
                                    parsed = parsed.replace(year=now.year-1)
                                moment = parsed.timestamp()
                            except ValueError:
                                pass
                        event = parse_auth(match[3], match[1], moment, match[2]) if match else None
                        if event:
                            event.update(source='auth-file', boot_id=self.boot_id)
                        self.spool.append(event, 'file:' + identity + ':' + generation + ':' + str(offset),
                                          ('file_cursor', dict(identity=identity, generation=generation, offset=source.tell())))
            except Exception as exc:
                self.state('auth-file', 'error', str(exc))
                self.stop.wait(5)

    def mac_record(self, item):
        if not isinstance(item, dict) or 'eventMessage' not in item:
            return
        service = Path(item.get('processImagePath') or '').name
        moment = timestamp(item.get('timestamp'))
        event = parse_auth(item['eventMessage'], service, moment, item.get('processID'))
        if event:
            event.update(source='unified-log', boot_id=item.get('bootUUID'))
        # stable source identity across stream/replay; equal messages at different native timestamps survive.
        key = hashlib.sha256(json.dumps([item.get(k) for k in ('bootUUID', 'timestamp', 'machTimestamp', 'processID', 'threadID', 'eventMessage')]).encode()).hexdigest()
        last = max(moment or 0, self.spool.get('mac_timestamp', self.started_at))
        self.spool.append(event, 'mac:' + key, ('mac_timestamp', last))

    def mac_predicate(self):
        return ' OR '.join('process == "' + service + '"' for service in SERVICES)

    def follow_macos(self):
        while not self.stop.is_set():
            try:
                self.state('unified-log', 'partial', 'SSH log coverage only; OS retention and privacy redaction can omit details.')
                for line in self.lines(['/usr/bin/log', 'stream', '--style', 'ndjson', '--level', 'info', '--predicate', self.mac_predicate()]):
                    try:
                        item = json.loads(line)
                        if 'collector_error' in item:
                            self.state('unified-log', 'gap', item['collector_error'])
                        else:
                            self.mac_record(item)
                    except ValueError:
                        pass
                if not self.stop.is_set():
                    raise RuntimeError('Unified log stream stopped')
            except Exception as exc:
                self.state('unified-log', 'error', str(exc))
            self.stop.wait(5)

    def follow_macos_replay(self):
        # Independent durable recovery boundary: live events cannot move it past
        # a backlog that has not been committed. Short overlapping windows also
        # close the subscribe/backfill race. Only selected authentication sources
        # in this incremental window are queried, not the whole log history.
        while not self.stop.is_set():
            try:
                start = self.spool.get('mac_replay_from')
                if start is None:
                    start = self.started_at - 5
                    self.spool.checkpoint('mac_replay_from', start)
                end = int(time.time()) - 1
                if end < start:
                    self.state('unified-replay', 'gap', 'Host clock moved backwards; recovery window reset.')
                    start = end - 60
                for line in self.lines(['/usr/bin/log', 'show', '--style', 'ndjson', '--info',
                                        '--start', datetime.datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M:%S'),
                                        '--end', datetime.datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M:%S'),
                                        '--predicate', self.mac_predicate()]):
                    try:
                        item = json.loads(line)
                        if 'collector_error' in item:
                            self.state('unified-replay', 'gap', item['collector_error'])
                        else:
                            self.mac_record(item)
                    except ValueError:
                        pass
                if not self.stop.is_set():
                    self.spool.checkpoint('mac_replay_from', end - 5)
                    self.state('unified-replay', 'partial', 'Incremental backfill checked; OS retention cannot guarantee complete history.')
            except Exception as exc:
                self.state('unified-replay', 'error', str(exc))
            self.stop.wait(60)

    def follow_endpoint(self):
        while not self.stop.is_set():
            try:
                for line in self.lines([self.endpoint_helper]):
                    item = json.loads(line)
                    if item.get('ready'):
                        self.state('endpoint-security', 'listening')
                        self.spool.append(dict(occurred_at=time.time(), service='collector', event_type='collection_gap', result='gap',
                                               evidence='Native event subscription started; events while it was stopped cannot be replayed.'), secrets.token_hex(16))
                    elif item.get('collector_error'):
                        self.state('endpoint-security', 'gap', item['collector_error'])
                    elif item.get('event_type'):
                        item.update(source='endpoint-security', boot_id=self.boot_id)
                        self.spool.append(item, 'es:' + secrets.token_hex(16))
                if not self.stop.is_set():
                    raise RuntimeError('Native event stream stopped')
            except Exception as exc:
                self.state('endpoint-security', 'error', str(exc))
            self.stop.wait(5)


def serve(collector, socket_path=SOCKET, reader_uid=None):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            try:
                request = json.loads(self.rfile.readline(4097))
                if request.get('operation') != 'exchange':
                    raise ValueError('Unknown operation')
                result = collector.spool.exchange(request.get('acknowledgement'))
                result['status'] = collector.status()
            except Exception as exc:
                result = {'error': clean(str(exc), 300)}
            self.wfile.write(json.dumps(result, ensure_ascii=True).encode() + b'\n')
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if reader_uid is not None:
        stat = path.parent.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise PermissionError('Collector socket directory must be owned by root and not writable by other accounts')
        # The reader needs traversal despite the daemon's restrictive umask.
        os.chmod(path.parent, 0o755)
    if path.exists():
        path.unlink()
    server = socketserver.UnixStreamServer(str(path), Handler)
    os.chmod(path, 0o600)
    if reader_uid is not None:
        os.chown(path, reader_uid, -1)
    server.timeout = 1
    try:
        while not collector.stop.is_set():
            server.handle_request()
            collector.spool.maintain()
    finally:
        server.server_close()
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reader', required=True, help='Harbour SSH account allowed to read and acknowledge events')
    parser.add_argument('--data', default='/var/lib/harbour-logins' if sys.platform == 'linux' else '/Library/Application Support/HarbourLogins')
    parser.add_argument('--auth-file', help='Linux syslog authentication file instead of the journal')
    parser.add_argument('--endpoint-helper', help='Signed and entitled native macOS event helper')
    parser.add_argument('--max-events', type=int, default=20000)
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform not in ('linux', 'darwin'):
        parser.error('Run as root on Linux or macOS')
    if not 1000 <= args.max_events <= 1000000:
        parser.error('--max-events must be between 1000 and 1000000')
    uid = pwd.getpwnam(args.reader).pw_uid
    os.umask(0o077)
    path = Path(args.data)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    # An advisory lock prevents two services sharing a queue/socket after upgrades.
    import fcntl
    lock = (path / 'collector.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    collector = Collector(Spool(path / 'events.db', args.max_events), endpoint_helper=args.endpoint_helper, auth_file=args.auth_file)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: collector.stop.set())
    collector.start()
    try:
        serve(collector, reader_uid=uid)
    finally:
        collector.close()
        lock.close()


if __name__ == '__main__':
    main()
