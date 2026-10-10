"""Bounded producer messages and acknowledged, byte-limited RAM history."""
import collections
import json
import pickle
import queue
import secrets
import socket
import struct
import threading
import time

MAX_MESSAGE = 1024*1024
IPC_SLOTS = 16
BATCH_BYTES = 1024*1024
BATCH_ROWS = 256


class Channel:
    """Framed, nonblocking socketpair IPC; partial writes survive peer exits.

    Pickle is confined to inherited private socketpairs between our processes.
    No listening socket or external input uses this protocol.
    """
    def __init__(self, sock):
        self.socket = sock
        sock.setblocking(False)
        self.incoming, self.outgoing = bytearray(), b''
        self.offset, self.closed = 0, False

    def send(self, message):
        if self.outgoing:
            raise RuntimeError('IPC message already pending')
        payload = pickle.dumps(message, protocol=4)
        if len(payload) > 16*MAX_MESSAGE:
            raise ValueError('IPC message exceeded limit')
        self.outgoing = struct.pack('!I', len(payload))+payload
        self.offset = 0
        self.flush()

    def flush(self):
        if self.outgoing and not self.closed:
            try:
                self.offset += self.socket.send(memoryview(self.outgoing)[self.offset:self.offset+65536])
                if self.offset == len(self.outgoing):
                    self.outgoing, self.offset = b'', 0
            except BlockingIOError:
                pass
            except OSError:
                self.closed = True

    def receive(self):
        if not self.closed:
            try:
                part = self.socket.recv(65536)
                if part:
                    self.incoming.extend(part)
                else:
                    self.closed = True
            except BlockingIOError:
                pass
            except OSError:
                self.closed = True
        if len(self.incoming) >= 4:
            size = struct.unpack('!I', self.incoming[:4])[0]
            if size > 16*MAX_MESSAGE:
                raise ValueError('IPC message exceeded limit')
            if len(self.incoming) >= size+4:
                payload = bytes(self.incoming[4:size+4])
                del self.incoming[:size+4]
                return pickle.loads(payload)
        return None

    def close(self):
        self.socket.close()
        self.closed = True


class Delivery:
    """Only small, bounded IPC serialization shares the producer interpreter."""
    def __init__(self, sock):
        self.channel = Channel(sock)
        self.queue = queue.Queue(IPC_SLOTS)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def put_nowait(self, message):
        if self.channel.closed:
            raise queue.Full
        self.queue.put_nowait(message)

    def put(self, message, timeout):
        self.queue.put(message, timeout=timeout)

    def run(self):
        while not self.channel.closed:
            if not self.channel.outgoing:
                try:
                    self.channel.send(self.queue.get(timeout=.01))
                except queue.Empty:
                    if self.stopping.is_set():
                        break
            self.channel.flush()
            if self.channel.outgoing:
                time.sleep(.002)

    def close(self):
        self.stopping.set()
        self.thread.join(3)
        self.channel.close()


class Sender:
    def __init__(self, output, run_id, source):
        self.output = Delivery(output) if isinstance(output, socket.socket) else output
        self.run_id, self.source = run_id, source
        self.prefix, self.sequence, self.dropped = secrets.token_hex(8), 0, 0
        self.lock = threading.Lock()

    def emit(self, row):
        with self.lock:
            self.sequence += 1
            identity = f'{self.run_id}:{self.source}:{self.prefix}:{self.sequence}'
        payload = json.dumps({**row, 'run_id':self.run_id, 'sample_key':identity},
                             allow_nan=False, separators=(',', ':')).encode()
        with self.lock:
            try:
                if len(payload) > MAX_MESSAGE:
                    raise queue.Full
                self.output.put_nowait(('row', self.source, self.dropped, payload))
            except queue.Full:
                self.dropped += 1

    def status(self, status):
        with self.lock:
            try:
                self.output.put_nowait(('status', self.source, self.dropped, status))
            except queue.Full:
                pass

    def finish(self):
        # The supervisor continues draining while stopping producers.
        try:
            self.output.put(('done', self.source, self.dropped, {}), timeout=1)
        except queue.Full:
            pass
        if isinstance(self.output, Delivery):
            self.output.close()


class Buffer:
    def __init__(self, limit):
        self.limit, self.size, self.dropped = limit, 0, 0
        self.rows = collections.deque()
        self.sequence = 0

    def append(self, payload):
        # Drop the newest arrival when full: outstanding batches remain stable
        # until the writer acknowledges them. Producers never wait for storage.
        if len(payload) > self.limit-self.size:
            self.dropped += 1
            return False
        self.sequence += 1
        self.rows.append((self.sequence, time.monotonic(), payload))
        self.size += len(payload)
        return True

    def batch(self):
        size, data, through = 0, [], 0
        for sequence, _, payload in self.rows:
            if data and (size+len(payload) > BATCH_BYTES or len(data) >= BATCH_ROWS):
                break
            data.append(payload)
            size += len(payload)
            through = sequence
        return through, data

    def acknowledge(self, through):
        while self.rows and self.rows[0][0] <= through:
            self.size -= len(self.rows.popleft()[2])

    def status(self):
        return {'buffer_bytes':self.size, 'buffer_limit_bytes':self.limit,
                'buffer_samples':len(self.rows), 'buffer_dropped':self.dropped,
                'oldest_unsaved_seconds':round(max(0, time.monotonic()-self.rows[0][1]), 1) if self.rows else 0}
