"""Read-only router collection, isolated from ping timing and disk writes."""
import ipaddress
import re
import shlex
import threading
import time

from . import openwrt, ssh
from .network_buffer import Sender


class Collector:
    def __init__(self, server, settings, run_id, output, stop):
        self.server, self.settings, self.stop = server, settings, stop
        self.sender = Sender(output, run_id, 'router')
        self.lock = threading.Lock()
        self.status = {'state':'starting', 'read_only':True}
        self.cancellations, self.threads = [], []
        self.start_thread(self.telemetry)
        self.start_thread(self.logs)
        if settings['router_probes']:
            self.start_thread(self.router_probes)

    def emit(self, row):
        self.sender.emit(row)

    def capability(self, name):
        with self.lock:
            return self.status.get('capabilities', {}).get(name, False)

    def start_thread(self, fn, *args):
        thread = threading.Thread(target=fn, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

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

    def router_probes(self):
        session, cancel = openwrt.Session(), ssh.Cancellation()
        self.cancellations.append(cancel)
        command = probe_command(self.settings)
        try:
            while not self.stop.is_set():
                if not self.capability('router_ping'):
                    self.stop.wait(1)
                    continue
                started, wall = time.monotonic(), time.time()
                try:
                    text = session.command(self.server, command, cancel, timeout=3)
                    for row in probe_results(text, self.settings, wall):
                        self.emit(row)
                except ssh.Cancelled:
                    break
                except Exception:
                    self.emit({'at': wall, 'kind': 'gap', 'source': 'router',
                               'detail': 'Router probe collection failed'})
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

    def close(self):
        self.stop.set()
        for cancel in self.cancellations:
            cancel.stop()
        for thread in self.threads:
            thread.join(10)
        self.sender.finish()


def run(server, settings, run_id, output, stop):
    collector = Collector(server, settings, run_id, output, stop)
    try:
        while not stop.wait(1):
            with collector.lock:
                status = dict(collector.status)
            collector.sender.status(status)
    finally:
        collector.close()


def probe_command(settings):
    """One persistent SSH connection, concurrent bounded pings, no remote files."""
    commands = []
    for target in settings['targets']:
        ping = ('ping -6' if ipaddress.ip_address(target).version == 6 else 'ping') + ' -n -c 1 -W 1 '
        if settings['wan_device']:
            ping += '-I ' + shlex.quote(settings['wan_device']) + ' '
        ping += shlex.quote(target)
        # Each short result is emitted in one printf after its ping completes;
        # completion order is immaterial and cannot mislabel another target.
        commands.append('(output=$(' + ping + ' 2>&1); result=$?; '
                        + "printf '\\n@@HARBOUR_PING:%s\\n%s\\nHARBOUR_PING_EXIT:%s\\n' "
                        + shlex.quote(target) + ' "$output" "$result") &')
    return '\n'.join(commands) + '\nwait'


def probe_results(text, settings, wall):
    parts = re.split(r'\n@@HARBOUR_PING:([^\n]+)\n', text)
    outputs = dict(zip(parts[1::2], parts[2::2]))
    for target in settings['targets']:
        output = outputs.get(target, '')
        match = re.search(r'time([=<])([\d.]+)\s*ms', output)
        exit_code = re.search(r'HARBOUR_PING_EXIT:(\d+)', output)
        success = bool(match and exit_code and exit_code[1] == '0')
        timeout = bool(exit_code and re.search(r'0 (?:packets )?received', output))
        yield {'at': wall, 'kind': 'probe' if success or timeout else 'gap', 'target': target,
               'source': 'router', 'status': 'reply' if success else 'timeout' if timeout else 'unavailable',
               'rtt_ms': float(match[2]) if success else None,
               'rtt_upper_bound': bool(success and match[1] == '<'),
               'interface': settings['wan_device'] or 'router routing policy',
               'detail': None if success or timeout else 'Router ping unavailable; inspect capabilities and route'}
