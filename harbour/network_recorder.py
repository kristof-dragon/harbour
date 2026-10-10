"""Independent, on-site network recording service: python -m harbour.network_recorder.

The web process is not involved in sampling. All target operations are read-only.
Linux datagram ICMP sockets need a permitted ping group, never a privileged app.
"""
import collections
import fcntl
import ipaddress
import json
import logging
import re
import secrets
import shlex
import signal
import socket
import struct
import threading
import time

from . import logins, network, openwrt, ssh, store


def checksum(data):
    if len(data) % 2:
        data += b'\0'
    value = sum(struct.unpack('!%dH' % (len(data)//2), data))
    value = (value & 65535) + (value >> 16)
    value = (value & 65535) + (value >> 16)
    return (~value) & 65535


class Echo:
    """One connected datagram ICMP socket. Payload tokens disambiguate replies."""
    def __init__(self, target, emit, socket_factory=socket.socket):
        self.target, self.emit = target, emit
        self.pending, self.expired = {}, {}
        self.sequence = 0
        self.prefix = secrets.token_bytes(8)
        version = ipaddress.ip_address(target).version
        self.family = socket.AF_INET if version == 4 else socket.AF_INET6
        self.type = 8 if version == 4 else 128
        self.socket = socket_factory(self.family, socket.SOCK_DGRAM,
                                     socket.IPPROTO_ICMP if version == 4 else socket.IPPROTO_ICMPV6)
        try:
            self.socket.connect((target, 0))
            self.socket.setblocking(False)
        except Exception:
            self.socket.close()
            raise

    def close(self):
        self.socket.close()

    def send(self, now, wall):
        self.sequence += 1
        token = self.prefix + struct.pack('!Q', self.sequence)
        header = struct.pack('!BBHHH', self.type, 0, 0, 0, self.sequence & 65535)
        packet = header + token
        if self.family == socket.AF_INET:
            packet = struct.pack('!BBHHH', self.type, 0, checksum(packet), 0, self.sequence & 65535) + token
        self.socket.send(packet)
        self.pending[token] = (now, wall, self.sequence)

    def receive(self, now):
        for _ in range(64):
            try:
                data = self.socket.recv(65535)
            except BlockingIOError:
                break
            except OSError:
                # Some stacks report asynchronous ICMP errors through recv.
                # Outstanding attempts still get their explicit deadline result.
                break
            if self.family == socket.AF_INET and data and data[0] >> 4 == 4:
                data = data[(data[0] & 15)*4:]
            if len(data) != 24 or data[0] != (0 if self.family == socket.AF_INET else 129):
                continue
            token = data[8:]
            attempt = self.pending.pop(token, None)
            late = False
            if attempt is None:
                attempt = self.expired.pop(token, None)
                late = True
            if attempt:
                sent, wall, seq = attempt
                if not late and now-sent >= 2:
                    # A stalled reader can encounter the reply before its next
                    # deadline sweep. Preserve the missed deadline in that case.
                    self.emit({'at': wall, 'kind': 'probe', 'target': self.target,
                               'source': 'recorder', 'status': 'timeout', 'sequence': seq,
                               'rtt_ms': None, 'deadline_seconds': 2})
                    late = True
                self.emit({'at': wall, 'kind': 'probe', 'target': self.target,
                           'source': 'recorder', 'status': 'late_reply' if late else 'reply',
                           'sequence': seq, 'rtt_ms': round((now-sent)*1000, 3),
                           'family': 4 if self.family == socket.AF_INET else 6})
        for token, attempt in list(self.pending.items()):
            sent, wall, seq = attempt
            if now-sent >= 2:
                self.pending.pop(token)
                self.expired[token] = attempt
                self.emit({'at': wall, 'kind': 'probe', 'target': self.target,
                           'source': 'recorder', 'status': 'timeout', 'sequence': seq,
                           'rtt_ms': None, 'deadline_seconds': 2})
        self.expired = {k:v for k,v in self.expired.items() if now-v[0] < 10}


class Worker:
    def __init__(self, server, settings, revision):
        self.server, self.settings, self.revision = server, settings, revision
        self.signature = logins.connection_signature(server)
        self.stop = threading.Event()
        self.queue, self.lock = collections.deque(), threading.Lock()
        self.dropped = 0
        self.status = {'state': 'starting', 'observer': socket.gethostname(),
                       'vantage': 'Network recorder host / container network namespace',
                       'read_only': True, 'probe_errors': {}, 'revision': revision}
        self.cancellations, self.threads = [], []
        self.last_incident = 0
        self.run_id = secrets.token_hex(8)
        store.execute('INSERT INTO network_runs VALUES (?,?,?,?,?)',
                      (self.run_id, server['id'], time.time(), json.dumps(settings), socket.gethostname()))
        self.start_thread(self.probes)
        self.start_thread(self.telemetry)
        self.start_thread(self.logs)
        if settings['router_probes']:
            for target in settings['targets']:
                self.start_thread(self.router_probe, target)

    def capability(self, name):
        with self.lock:
            return self.status.get('capabilities', {}).get(name, False)

    def start_thread(self, fn, *args):
        thread = threading.Thread(target=fn, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

    def emit(self, row):
        row['run_id'] = self.run_id
        with self.lock:
            if len(self.queue) >= 10000:
                self.queue.popleft()
                self.dropped += 1
            self.queue.append(row)

    def probes(self):
        targets = list(self.settings['targets'])
        try:
            address = str(ipaddress.ip_address(self.server['host']))
            if address not in targets:
                targets.insert(0, address)
        except ValueError:
            with self.lock:
                self.status['lan_probe'] = 'Use the router LAN IP as its SSH address to enable the LAN reference probe'
        probes = {}
        retry_at, next_at = 0, time.monotonic()
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now >= retry_at:
                    for target in targets:
                        if target not in probes:
                            try:
                                probes[target] = Echo(target, self.emit)
                                with self.lock:
                                    self.status['probe_errors'].pop(target, None)
                            except OSError as exc:
                                message = 'ICMP unavailable: permit this service group to use datagram ping sockets' if exc.errno in (1,13) else 'ICMP socket unavailable for this address family or route'
                                with self.lock:
                                    self.status['probe_errors'][target] = message
                                self.emit({'at': time.time(), 'kind': 'gap', 'target': target, 'detail': message})
                    retry_at = now+30
                for probe in probes.values():
                    probe.receive(now)
                if now >= next_at:
                    skipped = int((now-next_at)/self.settings['interval'])
                    if skipped:
                        self.emit({'at': time.time(), 'kind': 'gap', 'detail': 'Probe scheduler delay', 'skipped_slots': skipped})
                    for target, probe in list(probes.items()):
                        try:
                            probe.send(time.monotonic(), time.time())
                        except OSError:
                            self.emit({'at': time.time(), 'kind': 'gap', 'target': target, 'detail': 'Local probe send failed; no packet-loss claim'})
                            probe.close(); probes.pop(target)
                    next_at += (skipped+1)*self.settings['interval']
                self.stop.wait(.01)
        finally:
            for probe in probes.values():
                probe.close()

    def telemetry(self):
        session, cancel = openwrt.Session(), ssh.Cancellation()
        self.cancellations.append(cancel)
        previous = None
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    result = session.exchange(self.server, cancel=cancel)
                    metrics = result['metrics']
                    openwrt.interval(previous, metrics)
                    self.emit({'at': metrics['measured_at'], 'kind': 'telemetry', 'metrics': metrics,
                               'collection_ms': result['collection_ms']})
                    with self.lock:
                        self.status.update(state='recording', telemetry_error=None, device=metrics['openwrt']['board'],
                                           capabilities=metrics['openwrt']['capabilities'])
                    previous = metrics
                except ssh.Cancelled:
                    break
                except Exception:
                    previous = None
                    with self.lock:
                        self.status.update(state='partial', telemetry_error='Router telemetry unavailable; check SSH, fingerprint and read permissions')
                    self.emit({'at': time.time(), 'kind': 'gap', 'detail': 'Router telemetry unavailable'})
                    if self.stop.wait(5):
                        break
                elapsed = time.monotonic()-started
                if elapsed > 1:
                    self.emit({'at': time.time(), 'kind': 'gap', 'detail': 'Router sampling exceeded one second', 'collection_seconds': elapsed})
                self.stop.wait(max(.05, 1-elapsed))
        finally:
            session.close()

    def router_probe(self, target):
        session, cancel = openwrt.Session(), ssh.Cancellation()
        self.cancellations.append(cancel)
        version = ipaddress.ip_address(target).version
        command = ('ping -6' if version == 6 else 'ping') + ' -n -c 1 -W 1 '
        # IPs and interface names have already passed strict data validation.
        if self.settings['wan_device']:
            command += '-I ' + shlex.quote(self.settings['wan_device']) + ' '
        command += shlex.quote(target)
        # Preserve ping's exit code as data; remote shell itself succeeds.
        command += '; printf "\\nHARBOUR_PING_EXIT:%s\\n" "$?"'
        try:
            while not self.stop.is_set():
                if not self.capability('router_ping'):
                    self.stop.wait(1)
                    continue
                started, wall = time.monotonic(), time.time()
                try:
                    text = session.command(self.server, command, cancel, timeout=3)
                    match = re.search(r'time([=<])([\d.]+)\s*ms', text)
                    exit_code = re.search(r'HARBOUR_PING_EXIT:(\d+)', text)
                    success = bool(match and exit_code and exit_code[1] == '0')
                    # BusyBox's explicit packet summary distinguishes timeout
                    # from an absent tool, unsupported option or missing route.
                    timeout = bool(re.search(r'0 (?:packets )?received', text))
                    self.emit({'at': wall, 'kind': 'probe' if success or timeout else 'gap', 'target': target,
                               'source': 'router', 'status': 'reply' if success else 'timeout' if timeout else 'unavailable',
                               'rtt_ms': float(match[2]) if success else None,
                               'rtt_upper_bound': bool(success and match[1] == '<'),
                               'interface': self.settings['wan_device'] or 'router routing policy',
                               'detail': None if success or timeout else 'Router ping unavailable; inspect capabilities and route'})
                except ssh.Cancelled:
                    break
                except Exception:
                    self.emit({'at': wall, 'kind': 'gap', 'target': target, 'source': 'router', 'detail': 'Router probe collection failed'})
                    self.stop.wait(5)
                self.stop.wait(max(.05, 1-(time.monotonic()-started)))
        finally:
            session.close()

    def logs(self):
        session, cancel = openwrt.Session(), ssh.Cancellation()
        self.cancellations.append(cancel)
        try:
            while not self.stop.is_set():
                if not self.capability('logs'):
                    self.stop.wait(1)
                    continue
                channel = None
                try:
                    # Establish the same pinned, read-only connection first.
                    session.command(self.server, 'test -f /etc/openwrt_release', cancel)
                    cancel.bind(session.close)
                    _, output, _ = session.client.exec_command('logread -f -l 1', timeout=5)
                    channel, pending = output.channel, bytearray()
                    while not self.stop.is_set():
                        if channel.recv_ready():
                            pending.extend(channel.recv(16384))
                            while b'\n' in pending:
                                line, _, pending = pending.partition(b'\n')
                                line = line.decode('utf-8', errors='replace')[:2000]
                                if re.search(r'netifd|kernel:|hostapd|mt76|pppd|watchdog|out of memory', line, re.I):
                                    if re.search(r'password|private.?key|preshared|secret|token', line, re.I):
                                        line = '[Sensitive router log line omitted]'
                                    self.emit({'at': time.time(), 'kind': 'log', 'line': line,
                                               'timestamp_basis': 'recorder receipt; router source timestamp preserved in line'})
                            if len(pending)>65536:
                                pending.clear()
                                self.emit({'at': time.time(), 'kind': 'gap', 'detail':'Oversized router log line discarded'})
                        if channel.recv_stderr_ready():
                            channel.recv_stderr(16384)
                        if channel.exit_status_ready() and not channel.recv_ready():
                            raise OSError('Log stream ended')
                        self.stop.wait(.05)
                except ssh.Cancelled:
                    break
                except Exception:
                    self.emit({'at': time.time(), 'kind': 'gap', 'detail': 'Router log stream unavailable; reconnecting'})
                    self.stop.wait(10)
                finally:
                    if channel:
                        channel.close()
                    cancel.unbind()
                    session.close()
        finally:
            session.close()

    def flush(self):
        with self.lock:
            rows = list(self.queue)
            self.queue.clear()
            status = json.loads(json.dumps(self.status))
            status['dropped_before_storage'] = self.dropped
        try:
            accepted = network.persist(self.server['id'], rows, status, self.signature, self.revision)
        except Exception:
            with self.lock:
                self.queue.extendleft(reversed(rows))
                while len(self.queue) > 10000:
                    self.queue.popleft(); self.dropped += 1
            return
        if accepted:
            bad = [r for r in rows if r['kind'] == 'probe' and r.get('source') == 'recorder'
                   and (r.get('status') == 'timeout' or (r.get('rtt_ms') or 0) > self.settings['latency_limit_ms'])]
            if bad and time.time()-self.last_incident > 300:
                network.mark(self.server['id'], 'Probe degradation — inspect correlated evidence', True, bad[0]['at'])
                self.last_incident = time.time()

    def close(self):
        self.stop.set()
        for cancel in self.cancellations:
            cancel.stop()
        for thread in self.threads:
            thread.join()
        self.flush()


def maintenance(stop):
    from . import network_archives
    while not stop.is_set():
        try:
            if not store.DEMO:
                for server in store.rows("SELECT id FROM servers WHERE server_type='openwrt' OR id IN (SELECT server_id FROM network_settings)"):
                    if stop.is_set():
                        break
                    network.maintain(server['id'], stop)
                network_archives.cleanup_removed()
        except Exception:
            logging.exception('Network history maintenance failed; will retry')
        stop.wait(60)


def run(stop):
    workers = {}
    maintainer = threading.Thread(target=maintenance, args=(stop,), daemon=True)
    maintainer.start()
    try:
        while not stop.is_set():
            active = {}
            if not store.DEMO:
                for server in store.rows("SELECT * FROM servers WHERE server_type='openwrt' AND monitoring_enabled=1"):
                    settings, revision = network.config(server['id'])
                    if settings['enabled']:
                        # Revalidate stored configuration before executing probes.
                        settings = network.Settings(**settings).model_dump()
                        active[server['id']] = (server, settings, revision)
            for id_, worker in list(workers.items()):
                next_config = active.get(id_)
                if (not next_config or next_config[2] != worker.revision
                        or logins.connection_signature(next_config[0]) != worker.signature):
                    worker.close(); workers.pop(id_)
            for id_, (server, settings, revision) in active.items():
                if id_ not in workers:
                    workers[id_] = Worker(server, settings, revision)
                workers[id_].flush()
            stop.wait(1)
    finally:
        stop.set()
        for worker in workers.values():
            worker.close()
        maintainer.join()


def main():
    store.DATA.mkdir(parents=True, exist_ok=True)
    # Only one process owns the recorder per data directory. A second container
    # must not silently double probe traffic or produce duplicate incidents.
    with (store.DATA / 'network-recorder.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        # The web service owns the account/schema bootstrap. Do not initialise
        # users from this sidecar or race a first-run database migration.
        while not stop.is_set():
            try:
                store.one('SELECT server_type FROM servers LIMIT 1')
                network.initialize()
                break
            except Exception:
                stop.wait(1)
        if not stop.is_set():
            run(stop)


if __name__ == '__main__':
    main()
