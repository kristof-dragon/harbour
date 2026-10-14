"""Collector notices over a second channel of the existing pinned SSH transport."""
import json
import shlex
import threading
import time
from pathlib import Path

from . import store

listeners = {}
guard = threading.RLock()


class Listener:
    def __init__(self, session, server):
        self.session, self.server = session, dict(server)
        self.stop = threading.Event()
        self.channel = None
        self.last_notice = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name='bridge-'+server['id'])

    def close(self):
        self.stop.set()
        if self.channel:
            self.channel.close()

    def current(self, row):
        from . import logins
        return bool(row and not self.stop.is_set() and not self.session.retired.is_set()
                    and row['monitoring_enabled'] and logins.connection_signature(dict(row)) == self.session.signature)

    def fetch(self):
        from . import bridge, footprint, logins
        # Normal monitoring and notifications serialize on the same resource reader.
        # ACKs remain transactional in logins.collect; replay after a failure is safe.
        for _ in range(10):
            row = store.one('SELECT * FROM servers WHERE id=?', (self.server['id'],))
            if not self.current(row) or not bridge.enabled():
                return
            request = logins.payload(row['id'], self.session.signature)
            request['operation'] = 'logins'
            result = self.session.exchange(row, request, None)
            if self.stop.is_set():
                return
            logins.collect(row, result.get('logins'), self.session.signature, self.current)
            footprint.ingest(row['id'], result, self.session.signature, login_only=True)
            batch = result.get('logins') or {}
            if batch.get('pending', 0) <= len(batch.get('events', [])):
                return

    def run(self):
        from . import bridge, ssh
        try:
            client = self.session.client
            if not client:
                return
            source = Path(__file__).with_name('remote_probe.py').read_bytes()
            bootstrap = "import sys;exec(compile(sys.stdin.buffer.read(%d),'<harbour-login-notices>','exec'));serve_login_notifications()" % len(source)
            command = '/bin/sh -c '+shlex.quote(ssh.REMOTE_ENV+'\nexec python3 -u -c '+shlex.quote(bootstrap))
            stdin, stdout, _ = client.exec_command(command, timeout=15)
            self.channel = stdout.channel
            stdin.write(source); stdin.flush()
            buffer, last_fetch = bytearray(), 0
            last_received = time.monotonic()
            while not self.stop.is_set() and not self.session.retired.is_set():
                if time.monotonic()-last_received > 50:
                    return  # A silent channel is retried by ordinary monitoring.
                if self.channel.recv_ready():
                    noticed = False
                    buffer.extend(self.channel.recv(4096))
                    if len(buffer) > 8192:
                        raise ValueError('Oversized collector notification')
                    while b'\n' in buffer:
                        line, _, rest = buffer.partition(b'\n'); buffer = bytearray(rest)
                        notice = json.loads(line)
                        if not isinstance(notice, dict) or notice.get('kind') != 'login-change':
                            raise ValueError('Unsupported collector notification')
                        noticed = True
                    if noticed:
                        self.last_notice = time.time()
                        last_received = time.monotonic()
                        # Several queued revisions need only one fetch of the latest batch.
                        if self.stop.wait(max(0, 1-(time.monotonic()-last_fetch))):
                            return
                        if not bridge.enabled():
                            return
                        self.fetch(); last_fetch = time.monotonic()
                if self.channel.recv_stderr_ready():
                    self.channel.recv_stderr(4096)
                if self.channel.exit_status_ready() and not self.channel.recv_ready():
                    return
                self.stop.wait(.1)
        except Exception:
            # Ordinary host polling is the fallback; reconnect on its next successful response.
            pass
        finally:
            self.close()
            with guard:
                if listeners.get(self.server['id']) is self:
                    listeners.pop(self.server['id'], None)
            bridge.publish()


def ensure(session, server, status):
    from . import bridge
    if not server.get('monitoring_enabled') or status.get('notifications') is not True or not bridge.enabled():
        return
    with guard:
        old = listeners.get(server['id'])
        if old and old.session is session and not old.stop.is_set():
            return
        if old:
            old.close()
        listener = listeners[server['id']] = Listener(session, server)
        listener.thread.start()


def close_session(session):
    with guard:
        for listener in list(listeners.values()):
            if listener.session is session:
                listener.close()


def close_all():
    with guard:
        for listener in list(listeners.values()):
            listener.close()


def mode(server_id, status):
    with guard:
        listener = listeners.get(server_id)
        if listener and not listener.stop.is_set() and time.time()-listener.last_notice < 45:
            return 'live'
    return 'polling' if status.get('notifications') else 'collector update needed' if status.get('state') == 'connected' else 'unavailable'
