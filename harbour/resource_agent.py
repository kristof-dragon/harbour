"""Unprivileged, standard-library resource recorder; private local socket only."""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager

try:
    from . import cpu, remote_probe
except ImportError:
    # The installer owns this directory; isolated Python excludes user packages.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cpu
    import remote_probe

MAX_BYTES = 64 * 1024 * 1024
BATCH_BYTES = 2 * 1024 * 1024


class Spool:
    def __init__(self, path, max_samples=20000):
        self.path, self.max_samples = path, max_samples
        self.lock = threading.RLock()
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
            ''')
            for key, value in [('id', secrets.token_hex(16)), ('secret', secrets.token_hex(32)), ('dropped', 0)]:
                con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (key, json.dumps(value)))
            if self.get(con, 'bytes') is None:
                size = con.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM samples').fetchone()[0]
                self.set(con, 'bytes', size)
            if self.get(con, 'count') is None:
                self.set(con, 'count', con.execute('SELECT COUNT(*) FROM samples').fetchone()[0])

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=FULL')
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def get(con, key, default=None):
        row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set(con, key, value):
        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value, allow_nan=False)))

    def add(self, metrics):
        with self.lock, self.connect() as con:
            previous = self.get(con, 'baseline')
            metrics = dict(metrics)
            counters = metrics.get('cpu_counters')
            metrics['cpu'], metrics['cpu_sample_seconds'] = cpu.usage(previous, counters)
            self.set(con, 'baseline', counters if cpu.valid(counters) else None)
            encoded = json.dumps(metrics, allow_nan=False)
            if len(encoded.encode()) > 500000:
                raise ValueError('Resource sample exceeds 500 KB')
            seq = con.execute('INSERT INTO samples(payload) VALUES (?)', (encoded,)).lastrowid
            self.set(con, 'latest', metrics)
            self.set(con, 'latest_seq', seq)
            count = self.get(con, 'count', 0) + 1
            size = self.get(con, 'bytes', 0) + len(encoded.encode())
            dropped = 0
            if count > self.max_samples or size > MAX_BYTES:
                for old_seq, length in con.execute('SELECT seq,length(CAST(payload AS BLOB)) FROM samples ORDER BY seq'):
                    if count <= self.max_samples and size <= MAX_BYTES:
                        break
                    con.execute('DELETE FROM samples WHERE seq=?', (old_seq,))
                    count -= 1; size -= length; dropped += 1
                self.set(con, 'dropped', self.get(con, 'dropped', 0) + dropped)
            self.set(con, 'bytes', size)
            self.set(con, 'count', count)
            return seq

    def exchange(self, acknowledgement=None):
        with self.lock, self.connect() as con:
            identity, secret = self.get(con, 'id'), self.get(con, 'secret')
            def token(through):
                return hmac.new(secret.encode(), (identity + ':' + str(through)).encode(), hashlib.sha256).hexdigest()
            rejected = False
            if acknowledgement:
                through = acknowledgement.get('through')
                if (acknowledgement.get('recorder_id') == identity and type(through) is int and through > 0
                        and hmac.compare_digest(str(acknowledgement.get('token', '')), token(through))):
                    count, removed = con.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM samples WHERE seq<=?', (through,)).fetchone()
                    con.execute('DELETE FROM samples WHERE seq<=?', (through,))
                    self.set(con, 'bytes', max(0, self.get(con, 'bytes', 0) - removed))
                    self.set(con, 'count', max(0, self.get(con, 'count', 0) - count))
                else:
                    rejected = True
            rows, used = [], 0
            for seq, payload in con.execute('SELECT seq,payload FROM samples ORDER BY seq LIMIT 200'):
                used += len(payload.encode())
                if rows and used > BATCH_BYTES:
                    break
                rows.append({'seq': seq, 'metrics': json.loads(payload)})
            through = rows[-1]['seq'] if rows else None
            return {'protocol': 1, 'recorder_id': identity, 'samples': rows,
                    'latest': self.get(con, 'latest'), 'latest_seq': self.get(con, 'latest_seq', 0),
                    'pending': self.get(con, 'count', 0),
                    'dropped': self.get(con, 'dropped', 0), 'ack_rejected': rejected,
                    'acknowledgement': {'recorder_id': identity, 'through': through, 'token': token(through)} if through else None}


def settings(values):
    result = {}
    for key, default in [('sample_seconds', 60), ('disk_seconds', 300)]:
        value = values.get(key, default)
        if type(value) is not int or not 15 <= value <= 3600:
            raise ValueError('Invalid recorder interval')
        result[key] = value
    result['memory'] = values.get('memory', True) is not False
    for key in ('excluded_disks', 'excluded_temperatures', 'excluded_hardware'):
        value = values.get(key, [])
        if not isinstance(value, list) or len(value) > 2000 or any(not isinstance(v, str) or len(v) > 4096 for v in value):
            raise ValueError('Invalid collection selection')
        result[key] = value
    return result


class Recorder:
    def __init__(self, spool, interval=60):
        self.spool, self.stop, self.wake = spool, threading.Event(), threading.Event()
        with spool.connect() as con:
            self.options = settings(spool.get(con, 'settings', {'sample_seconds': interval}))
        self.sampler, self.error = remote_probe.ResourceSampler(), None

    def configure(self, values):
        options = settings(values)
        if options != self.options:
            with self.spool.lock, self.spool.connect() as con:
                self.spool.set(con, 'settings', options)
            self.options = options
            self.wake.set()

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.spool.add(self.sampler.sample(self.options))
                self.error = None
            except Exception as exc:
                self.error = str(exc)[:300]
            interval = self.options['sample_seconds']
            elapsed = time.monotonic() - started
            delay = (int(elapsed // interval) + 1) * interval - elapsed
            self.wake.wait(delay)
            self.wake.clear()

    def exchange(self, request):
        if 'settings' in request:
            self.configure(request['settings'])
        result = self.spool.exchange(request.get('acknowledgement'))
        result.update(state='error' if self.error else 'recording', sample_seconds=self.options['sample_seconds'],
                      detail=self.error, disk_seconds=self.options['disk_seconds'])
        return result


def serve(recorder, path):
    path = Path(path)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path)); os.chmod(path, 0o600)
        listener.listen(4); listener.settimeout(1)
        try:
            while not recorder.stop.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(5)
                    try:
                        with client.makefile('rb') as stream:
                            line = stream.readline(262145)
                        if not line.endswith(b'\n') or len(line) > 262144:
                            raise ValueError('Invalid request size')
                        result = recorder.exchange(json.loads(line))
                        client.sendall(json.dumps(result, allow_nan=False).encode() + b'\n')
                    except (OSError, ValueError, TypeError, AttributeError):
                        try:
                            client.sendall(b'{"error":"Recorder exchange failed"}\n')
                        except OSError:
                            pass
        finally:
            path.unlink(missing_ok=True)


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--socket', default='/var/run/harbour-resources/recorder.sock')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--max-samples', type=int, default=20000)
    args = parser.parse_args()
    if not 15 <= args.interval <= 3600 or not 100 <= args.max_samples <= 100000:
        parser.error('Interval must be 15–3600 seconds; queue must hold 100–100000 samples')
    os.umask(0o077)
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'recorder.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recorder = Recorder(Spool(state / 'resources.db', args.max_samples), args.interval)
        def stop(*_):
            recorder.stop.set(); recorder.wake.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        worker = threading.Thread(target=recorder.run, daemon=True)
        worker.start()
        try:
            serve(recorder, args.socket)
        finally:
            stop(); worker.join(timeout=15)


if __name__ == '__main__':
    main()
