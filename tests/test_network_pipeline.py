"""Synthetic timing, buffer, IPC and commit failures; no live site data."""
import json
import queue
import socket
import struct
import threading
import time

import pytest

from harbour import network, network_probe, network_runtime, network_storage, store
from harbour.network_buffer import Buffer, Channel, Sender
from test_harbour import client
from test_openwrt import FakeSocket, enable, response


class Clock:
    def __init__(self):
        self.now, self.wall = 10., 1000.
    def monotonic(self): return self.now
    def time(self): return self.wall
    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds


class TimestampSocket(FakeSocket):
    def setsockopt(self, *args): pass
    def recvmsg(self, *args):
        if self.incoming:
            packet, at = self.incoming.pop(0)
            sec = int(at)
            return packet, [(socket.SOL_SOCKET, 64, struct.pack('=qq', sec, round((at-sec)*1e9)))], 0, None
        raise BlockingIOError


def echo(kernel=True):
    clock, rows = Clock(), []
    probe = network_probe.Echo('192.0.2.10', rows.append, TimestampSocket if kernel else FakeSocket, clock)
    probe.timestamp_option = 64 if kernel else None
    return clock, rows, probe


def test_kernel_arrival_is_not_reader_delay_or_false_timeout():
    clock, rows, probe = echo()
    probe.send()
    probe.socket.incoming.append((response(probe.socket.sent[-1]), clock.wall+.02))
    clock.advance(3)
    probe.receive()
    assert len(rows) == 1 and rows[0]['status'] == 'reply'
    assert rows[0]['rtt_ms'] == 20 and rows[0]['observed_rtt_ms'] == 3000
    assert rows[0]['reader_delay_ms'] == 2980
    assert rows[0]['timing_quality'] == 'kernel_receive'


def test_kernel_late_reply_keeps_real_deadline_miss():
    clock, rows, probe = echo()
    probe.send()
    probe.socket.incoming.append((response(probe.socket.sent[-1]), clock.wall+2.2))
    clock.advance(3)
    probe.receive()
    assert [r['status'] for r in rows] == ['timeout', 'late_reply']
    assert rows[-1]['rtt_ms'] == 2200


def test_clock_jump_cannot_create_network_latency():
    clock, rows, probe = echo()
    probe.send()
    clock.advance(.02)
    clock.wall += 10
    probe.socket.incoming.append((response(probe.socket.sent[-1]), clock.wall))
    probe.receive()
    assert rows[0]['status'] == 'reply' and rows[0]['rtt_ms'] is None
    assert rows[0]['timing_quality'] == 'uncertain'


def test_userspace_stall_is_uncertain_but_real_unanswered_probes_timeout():
    clock, rows, probe = echo(False)
    probe.send()
    probe.socket.incoming.append(response(probe.socket.sent[-1]))
    clock.advance(.5)
    probe.receive()
    assert rows[0]['rtt_ms'] is None and rows[0]['observed_rtt_ms'] == 500
    assert rows[0]['timing_quality'] == 'uncertain'
    probe.send()
    for _ in range(202):
        clock.advance(.01)
        probe.receive()
    assert rows[-1]['status'] == 'timeout'
    probe.send()
    clock.advance(3)
    probe.receive()
    assert rows[-1]['status'] == 'unobserved'


def test_send_pause_is_reported_even_with_kernel_receive_timestamp():
    clock, rows, probe = echo()
    send = probe.socket.send
    def stalled(packet):
        clock.advance(.5)
        send(packet)
    probe.socket.send = stalled
    probe.send(scheduled=clock.now)
    probe.socket.incoming.append((response(probe.socket.sent[-1]), clock.wall+.02))
    clock.advance(.02)
    probe.receive()
    assert rows[0]['send_duration_ms'] == 500 and rows[0]['rtt_ms'] is None


def test_stall_inside_send_batch_skips_slots_instead_of_replaying(monkeypatch):
    clock, sent, output = Clock(), {}, queue.Queue(64)
    class Stop:
        def is_set(self): return clock.now >= 11.1
        def wait(self, seconds): clock.advance(seconds)
    class Probe:
        timestamp_option = None
        def __init__(self, target, emit): self.target = target
        def receive(self, **kwargs): pass
        def close(self): pass
        def send(self, **kwargs):
            first = not sent
            sent.setdefault(self.target, []).append(clock.now)
            if first:
                clock.advance(.6)
    monkeypatch.setattr(network_probe, 'time', clock)
    monkeypatch.setattr(network_probe, 'Echo', Probe)
    network_probe.run('192.0.2.1', {'targets':['192.0.2.2'], 'interval':.25}, 'example', output, Stop())
    times = sent['192.0.2.2']
    assert len(times) >= 3 and min(b-a for a,b in zip(times,times[1:])) >= .1
    rows = [json.loads(m[3]) for m in list(output.queue) if m[0]=='row']
    assert any(r.get('detail')=='Probe batch scheduling delay' and r['skipped_slots']==2 for r in rows)


def test_bounded_buffer_acknowledgements_and_live_limit_reduction():
    buffer = Buffer(100)
    assert buffer.append(b'x'*60)
    first, rows = buffer.batch()
    assert not buffer.append(b'y'*50)
    assert buffer.dropped == 1 and buffer.size == 60
    buffer.limit = 30
    assert not buffer.append(b'z')
    assert buffer.batch() == (first, rows)
    buffer.acknowledge(first)
    buffer.acknowledge(first)  # repeated acknowledgement is harmless
    assert buffer.size == 0 and buffer.append(b'z'*30)
    assert buffer.batch()[0] > first


def test_producer_full_queue_never_waits_and_reports_exact_losses():
    output = queue.Queue(1)
    sender = Sender(output, 'example-run', 'probe')
    sender.emit({'kind':'probe', 'at':1})
    for _ in range(10):
        sender.emit({'kind':'probe', 'at':2})
    output.get_nowait()
    sender.status({})
    assert output.get_nowait()[2] == 10


def test_nonblocking_ipc_large_message_and_peer_exit():
    a, b = socket.socketpair()
    left, right = Channel(a), Channel(b)
    payload = {'rows':[b'x'*2000000]}
    try:
        left.send(payload)
        result = None
        deadline = time.monotonic()+2
        while result is None and time.monotonic() < deadline:
            left.flush()
            result = right.receive()
        assert result == payload
        left.send({'rows':[b'y'*2000000]})
        left.close()  # a partial frame cannot hang the other process
        for _ in range(100):
            assert right.receive() is None
            if right.closed:
                break
        assert right.closed
    finally:
        left.close(); right.close()


def job_for(client, monkeypatch, quality='kernel_receive'):
    server, signature, revision = enable(client, monkeypatch)
    config = network.config('atlas')[0]
    at = time.time()
    row = {'kind':'probe', 'at':at, 'source':'recorder', 'target':'192.0.2.10',
           'status':'reply', 'rtt_ms':250, 'timing_quality':quality, 'run_id':'example-run'}
    return {'server_id':'atlas', 'signature':signature, 'revision':revision,
            'run':{'id':'example-run', 'started':at, 'config':config, 'observer':'example-host'},
            'rows':[json.dumps(row).encode()], 'through':1, 'status':{}}


def test_commit_before_writer_crash_retry_is_exactly_once_even_after_pruning(client, monkeypatch):
    job = job_for(client, monkeypatch)
    assert network_storage.commit(job)
    assert network_storage.commit(job)  # commit succeeded, acknowledgement was lost
    assert len(store.rows('SELECT * FROM network_samples')) == 2
    assert len(store.rows('SELECT * FROM network_events')) == 1
    store.execute('DELETE FROM network_samples')
    assert network_storage.commit(job)
    assert not store.rows('SELECT * FROM network_samples')
    assert len(store.rows('SELECT * FROM network_events')) == 1


def test_uncertain_latency_does_not_trigger_but_confirmed_timeout_does(client, monkeypatch):
    job = job_for(client, monkeypatch, 'uncertain')
    assert network_storage.commit(job)
    assert not store.rows('SELECT * FROM network_events')
    row = json.loads(job['rows'][0])
    row.update(status='timeout', rtt_ms=None, timing_quality='deadline')
    job.update(through=2, rows=[json.dumps(row).encode()])
    assert network_storage.commit(job)
    assert len(store.rows('SELECT * FROM network_events')) == 1


def test_stale_batch_rejected_and_buffer_size_does_not_restart(client, monkeypatch):
    job = job_for(client, monkeypatch)
    settings = network.config('atlas')[0]
    assert settings['buffer_mib'] == 128
    settings['buffer_mib'] = 256
    assert client.put('/api/servers/atlas/network', json=settings).status_code == 200
    assert network.config('atlas')[1] == job['revision']
    settings['interval'] = 1
    assert client.put('/api/servers/atlas/network', json=settings).status_code == 200
    assert not network_storage.commit(job)
    assert not store.rows('SELECT * FROM network_samples')


@pytest.mark.parametrize('value', [0, 7, 4097, 128.5, True])
def test_buffer_bounds(value):
    with pytest.raises(ValueError):
        network.Settings(buffer_mib=value)


def test_runtime_health_is_available_without_database_writes(monkeypatch, tmp_path):
    # A short socket path also exercises native deployments with long DATA paths.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='harbour-health-', dir='/tmp') as directory:
        monkeypatch.setattr(network_runtime, 'path', lambda: directory+'/status.sock')
        status = network_runtime.StatusServer()
        try:
            status.snapshot = {'example':{'buffer_bytes':123, 'writer_stalled':True,
                'recorder_online':True, 'runtime_updated':time.time()}}
            monkeypatch.setattr(store, 'db', lambda: pytest.fail('health tried disk access'))
            assert network_runtime.read_status('example')['buffer_bytes'] == 123
            assert network_runtime.read_status('missing') == {}
            status.snapshot['example']['runtime_updated'] -= 10
            assert network_runtime.read_status('example')['recorder_online'] is False
        finally:
            status.close()
