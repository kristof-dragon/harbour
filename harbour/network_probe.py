"""Small ICMP-only child process. No database, SSH, exports or compression."""
import ipaddress
import secrets
import socket
import struct
import sys
import time


def checksum(data):
    if len(data) % 2:
        data += b'\0'
    value = sum(struct.unpack('!%dH' % (len(data)//2), data))
    value = (value & 65535) + (value >> 16)
    value = (value & 65535) + (value >> 16)
    return (~value) & 65535


class Echo:
    def __init__(self, target, emit, socket_factory=socket.socket, clock=time):
        self.target, self.emit, self.clock = target, emit, clock
        self.pending, self.expired = {}, {}
        self.sequence, self.prefix = 0, secrets.token_bytes(8)
        self.version = ipaddress.ip_address(target).version
        self.family = socket.AF_INET if self.version == 4 else socket.AF_INET6
        self.type = 8 if self.version == 4 else 128
        self.socket = socket_factory(self.family, socket.SOCK_DGRAM,
                                     socket.IPPROTO_ICMP if self.version == 4 else socket.IPPROTO_ICMPV6)
        self.timestamp_option = None
        self.last_receive = self.clock.monotonic()
        try:
            self.socket.connect((target, 0))
            self.socket.setblocking(False)
            if sys.platform.startswith('linux') and hasattr(self.socket, 'recvmsg'):
                # Linux's time64 ABI works on both 32- and 64-bit hosts. Older
                # kernels fall back explicitly to application timing.
                try:
                    option = getattr(socket, 'SO_TIMESTAMPNS_NEW', 64)
                    self.socket.setsockopt(socket.SOL_SOCKET, option, 1)
                    self.timestamp_option = option
                except OSError:
                    pass
        except Exception:
            self.socket.close()
            raise

    def close(self):
        self.socket.close()

    def send(self, now=None, wall=None, scheduled=None):
        self.sequence += 1
        token = self.prefix + struct.pack('!Q', self.sequence)
        header = struct.pack('!BBHHH', self.type, 0, 0, 0, self.sequence & 65535)
        packet = header + token
        if self.version == 4:
            packet = struct.pack('!BBHHH', self.type, 0, checksum(packet), 0,
                                 self.sequence & 65535) + token
        live = now is None
        now = self.clock.monotonic() if live else now
        wall = self.clock.time() if wall is None else wall
        self.socket.send(packet)
        duration = max(0, self.clock.monotonic()-now) if live else 0
        self.pending[token] = dict(sent=now, wall=wall, sequence=self.sequence,
                                   send_duration_ms=duration*1000,
                                   send_lateness_ms=max(0, now-scheduled)*1000 if scheduled is not None else 0,
                                   observation_gap=False)

    def row(self, attempt, **values):
        return {'at': attempt['wall'], 'kind': 'probe', 'target': self.target,
                'source': 'recorder', 'sequence': attempt['sequence'], 'family': self.version,
                'send_lateness_ms': round(attempt['send_lateness_ms'], 3),
                'send_duration_ms': round(attempt['send_duration_ms'], 3), **values}

    def receive(self, now=None, loop_gap=False):
        live = now is None
        now = self.clock.monotonic() if live else now
        loop_gap = loop_gap or (live and now-self.last_receive > .06)
        self.last_receive = now
        if loop_gap:
            for attempt in self.pending.values():
                attempt['observation_gap'] = True
        for _ in range(64):
            received = None
            try:
                if self.timestamp_option is not None:
                    data, ancillary, flags, _ = self.socket.recvmsg(65535, 128)
                    if not flags & getattr(socket, 'MSG_CTRUNC', 8):
                        for level, kind, value in ancillary:
                            if level == socket.SOL_SOCKET and kind == self.timestamp_option and len(value) >= 16:
                                seconds, nanoseconds = struct.unpack('=qq', value[:16])
                                if 0 <= nanoseconds < 1_000_000_000:
                                    received = seconds + nanoseconds/1e9
                else:
                    data = self.socket.recv(65535)
            except (BlockingIOError, OSError):
                break
            read_mono = self.clock.monotonic() if live else now
            read_wall = self.clock.time()
            if self.version == 4 and data and data[0] >> 4 == 4:
                data = data[(data[0] & 15)*4:]
            if len(data) != 24 or data[0] != (0 if self.version == 4 else 129):
                continue
            token = data[8:]
            attempt = self.pending.pop(token, None)
            expired = attempt is None
            if expired:
                attempt = self.expired.pop(token, None)
            if attempt is None:
                continue
            observed = max(0, read_mono-attempt['sent'])
            reliable_send = attempt['send_duration_ms'] <= 20
            kernel_valid = (received is not None and reliable_send
                            and abs((read_wall-read_mono)-(attempt['wall']-attempt['sent'])) <= .01
                            and attempt['wall'] <= received <= read_wall+.001)
            healthy_reader = not attempt['observation_gap'] and read_mono-now <= .05
            if kernel_valid:
                rtt, quality = received-attempt['wall'], 'kernel_receive'
            elif self.timestamp_option is None and healthy_reader and reliable_send:
                rtt, quality = observed, 'userspace'
            else:
                rtt, quality = None, 'uncertain'
            # An on-time kernel timestamp remains an on-time reply even if the
            # process only reads it after the deadline. Drain before expiring.
            late = rtt is not None and rtt >= 2
            if late and not expired:
                self.emit(self.row(attempt, status='timeout', rtt_ms=None,
                                   deadline_seconds=2, timing_quality=quality))
            self.emit(self.row(attempt, status='late_reply' if late or expired else 'reply',
                               rtt_ms=round(rtt*1000, 3) if rtt is not None else None,
                               observed_rtt_ms=round(observed*1000, 3), timing_quality=quality,
                               received_at=received if kernel_valid else None,
                               reader_delay_ms=round(max(0, read_wall-received)*1000, 3) if kernel_valid else None))
        now = self.clock.monotonic() if live else now
        for token, attempt in list(self.pending.items()):
            if now-attempt['sent'] >= 2:
                self.pending.pop(token)
                self.expired[token] = attempt
                uncertain = attempt['observation_gap'] or attempt['send_duration_ms'] > 20
                self.emit(self.row(attempt, status='unobserved' if uncertain else 'timeout',
                                   rtt_ms=None, deadline_seconds=2,
                                   timing_quality='uncertain' if uncertain else 'deadline'))
        self.expired = {k:v for k,v in self.expired.items() if now-v['sent'] < 10}


def run(host, settings, run_id, output, stop):
    from .network_buffer import Sender
    sender = Sender(output, run_id, 'probe')
    probes, errors = {}, {}
    targets = list(settings['targets'])
    try:
        address = str(ipaddress.ip_address(host))
        if address not in targets:
            targets.insert(0, address)
    except ValueError:
        errors['LAN reference'] = 'Use the router LAN IP as its SSH address to enable the LAN reference probe'
    retry_at, next_at, last_poll, last_status = 0, time.monotonic(), time.monotonic(), 0
    try:
        while not stop.is_set():
            now = time.monotonic()
            lag = max(0, now-last_poll-.01)
            if lag > .05:
                sender.emit({'at':time.time(), 'kind':'gap', 'detail':'Probe reader scheduling delay',
                             'reader_lag_ms':round(lag*1000, 3)})
            if now >= retry_at:
                for target in targets:
                    if target not in probes:
                        try:
                            probes[target] = Echo(target, sender.emit)
                            errors.pop(target, None)
                        except OSError as exc:
                            message = ('ICMP unavailable: permit this service group to use datagram ping sockets'
                                       if exc.errno in (1,13) else 'ICMP socket unavailable for this address family or route')
                            errors[target] = message
                            sender.emit({'at':time.time(), 'kind':'gap', 'target':target, 'detail':message})
                retry_at = now+30
            for probe in probes.values():
                probe.receive(loop_gap=lag > .05)
            # Refresh the time after reading: never use a stale loop timestamp.
            last_poll = time.monotonic()
            if last_poll >= next_at:
                skipped = int((last_poll-next_at)/settings['interval'])
                if skipped:
                    sender.emit({'at':time.time(), 'kind':'gap', 'detail':'Probe scheduler delay', 'skipped_slots':skipped})
                scheduled = next_at+skipped*settings['interval']
                for target, probe in list(probes.items()):
                    try:
                        probe.send(scheduled=scheduled)
                    except OSError:
                        sender.emit({'at':time.time(), 'kind':'gap', 'target':target,
                                     'detail':'Local probe send failed; no packet-loss claim'})
                        probe.close(); probes.pop(target)
                # Skip slots missed inside the send loop too; never catch up.
                next_at = scheduled+settings['interval']
                finished = time.monotonic()
                if finished >= next_at:
                    skipped = int((finished-next_at)/settings['interval'])+1
                    sender.emit({'at':time.time(), 'kind':'gap', 'detail':'Probe batch scheduling delay',
                                 'skipped_slots':skipped, 'batch_duration_ms':round((finished-last_poll)*1000, 3)})
                    next_at += skipped*settings['interval']
            if last_poll-last_status >= 1:
                sender.status({'probe_errors':errors, 'timestamp_modes':{
                    target:'kernel_receive' if probe.timestamp_option is not None else 'userspace'
                    for target, probe in probes.items()}})
                last_status = last_poll
            stop.wait(.01)
    finally:
        for probe in probes.values():
            probe.close()
        sender.finish()
