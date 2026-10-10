"""Linux process integration check; run in an isolated --network none container.

Uses only loopback and documentation addresses, with a temporary database.
The router collector deliberately has no SSH server to reach.
"""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import time

os.environ['HARBOUR_SECRET'] = 'synthetic-process-test-secret-'+'x'*32
os.environ['HARBOUR_PASSWORD'] = 'synthetic-process-test-password'
os.environ['FIRST_RUN'] = 'true'
os.environ['HARBOUR_DEMO'] = 'false'

from harbour import network, network_probe, network_recorder, network_runtime, store


def pump(pipeline, duration, status=None):
    end = time.monotonic()+duration
    while time.monotonic() < end:
        pipeline.tick()
        if status:
            status.snapshot = pipeline.health()
        time.sleep(.005)


def until(pipeline, predicate, seconds=10, status=None):
    end = time.monotonic()+seconds
    while not predicate() and time.monotonic() < end:
        pump(pipeline, .02, status)
    assert predicate(), 'Timed out waiting for process progress'


def check():
    # Exercise real, unprivileged Linux datagram ping timestamp ancillary data.
    rows = []
    probe = network_probe.Echo('127.0.0.1', rows.append)
    try:
        assert probe.timestamp_option is not None
        probe.send()
        time.sleep(.6)
        probe.receive()
        assert len(rows) == 1 and rows[0]['status'] == 'reply'
        assert rows[0]['rtt_ms'] < 50 and rows[0]['observed_rtt_ms'] >= 590
        assert rows[0]['reader_delay_ms'] >= 550
    finally:
        probe.close()
    print('PASS: delayed reader preserves actual Linux kernel reply time', flush=True)

    with tempfile.TemporaryDirectory(prefix='harbour-process-') as directory:
        store.DATA, store.DEMO = Path(directory), False
        store.initialize(); network.initialize()
        store.execute('''INSERT INTO servers(id,name,host,port,username,fingerprint,server_type)
                         VALUES ('example','Example router','127.0.0.1',9,'example','EXAMPLE','openwrt')''')
        config = {**network.DEFAULTS, 'enabled':True, 'interval':.25,
                  'targets':['192.0.2.10'], 'router_probes':False, 'archive_enabled':False}
        store.execute('INSERT INTO network_settings VALUES (?,?,?)', ('example',json.dumps(config),'example-revision'))
        pipeline, status = network_recorder.Pipeline(), network_runtime.StatusServer()
        stopped = []
        try:
            until(pipeline, lambda:bool(pipeline.active), status=status)
            worker = pipeline.active['example']
            until(pipeline, lambda:worker.buffer.sequence >= 8, status=status)
            run_id = worker.run_id
            assert worker.buffer.limit == 128*1024*1024
            processes = [pipeline.writer, pipeline.archiver, *[p.process for p in worker.producers.values()]]
            assert len({p.pid for p in processes}) == 4
            # Freeze the archiver: sampling and disk commits must continue.
            os.kill(pipeline.archiver.pid, signal.SIGSTOP); stopped.append(pipeline.archiver.pid)
            old = store.one('SELECT written_through FROM network_runs WHERE id=?', (run_id,))['written_through']
            pump(pipeline, 2, status)
            assert store.one('SELECT written_through FROM network_runs WHERE id=?', (run_id,))['written_through'] > old
            os.kill(pipeline.archiver.pid, signal.SIGCONT); stopped.remove(pipeline.archiver.pid)
            print('PASS: stalled archives and unavailable SSH do not stop probes or writes', flush=True)

            until(pipeline, lambda:pipeline.inflight is None, status=status)
            os.kill(pipeline.writer.pid, signal.SIGSTOP); stopped.append(pipeline.writer.pid)
            old = worker.buffer.sequence
            pump(pipeline, 6.5, status)
            live = network_runtime.read_status('example')
            assert worker.buffer.sequence-old >= 20
            assert live['writer_stalled'] and live['buffer_samples'] >= 20
            assert live['oldest_unsaved_seconds'] >= 5 and not live['dropped_before_storage']
            old_writer = pipeline.writer.pid
            os.kill(old_writer, signal.SIGKILL); stopped.remove(old_writer)
            until(pipeline, lambda:pipeline.writer.pid != old_writer, status=status)
            until(pipeline, lambda:worker.buffer.status()['oldest_unsaved_seconds'] < 2, status=status)
            print('PASS: stalled writer buffers in RAM, exposes health, restarts and drains', flush=True)

            pid = worker.producers['probe'].process.pid
            config['buffer_mib'] = 256
            store.execute('UPDATE network_settings SET config=? WHERE server_id=?', (json.dumps(config),'example'))
            until(pipeline, lambda:worker.buffer.limit == 256*1024*1024, status=status)
            assert worker.producers['probe'].process.pid == pid
            os.kill(pid, signal.SIGSTOP); stopped.append(pid)
            pump(pipeline, 1, status)
            os.kill(pid, signal.SIGCONT); stopped.remove(pid)
            pump(pipeline, 2, status)
            samples, _ = network.samples('example', time.time()-120, time.time())
            assert any(r['kind']=='gap' and r.get('reader_lag_ms',0) >= 500 for r in samples)
            replies = [r for r in samples if r['kind']=='probe' and r['target']=='127.0.0.1']
            assert replies and all(r['status']=='reply' and r.get('rtt_ms',0) < 100 for r in replies)
            assert not store.rows('SELECT * FROM network_events')
            print('PASS: live buffer resizing keeps probes; reader pause becomes a gap, not false latency', flush=True)

            os.kill(pid, signal.SIGKILL)
            until(pipeline, lambda:'probe' in worker.producers and worker.producers['probe'].process.pid != pid, status=status)
            pump(pipeline, 1, status)
            assert worker.restarts == 1
            # A recording change still retires producers while storage is stopped.
            until(pipeline, lambda:pipeline.inflight is None, status=status)
            writer_pid = pipeline.writer.pid
            os.kill(writer_pid, signal.SIGSTOP); stopped.append(writer_pid)
            config['enabled'] = False
            store.execute('UPDATE network_settings SET config=? WHERE server_id=?', (json.dumps(config),'example'))
            until(pipeline, lambda:not pipeline.active, status=status)
            os.kill(writer_pid, signal.SIGCONT); stopped.remove(writer_pid)
            print('PASS: child restart and pause work independently of storage', flush=True)
        finally:
            for pid in stopped:
                try: os.kill(pid, signal.SIGCONT)
                except ProcessLookupError: pass
            pipeline.close(); status.close()
        assert not multiprocessing.active_children()
        print('PASS: shutdown reaps every child process', flush=True)
        config['enabled'] = True
        store.execute('UPDATE network_settings SET config=? WHERE server_id=?', (json.dumps(config),'example'))
        pipeline = network_recorder.Pipeline()
        try:
            until(pipeline, lambda:bool(pipeline.active))
            worker = pipeline.active['example']
            until(pipeline, lambda:worker.buffer.sequence >= 6 and bool(worker.buffer.rows))
        finally:
            pipeline.close()
        committed = store.one('SELECT written_through FROM network_runs WHERE id=?', (worker.run_id,))
        assert committed['written_through'] == worker.buffer.sequence
        assert not worker.buffer.rows and not multiprocessing.active_children()
        print('PASS: normal shutdown drains all transferred readings before exit', flush=True)


if __name__ == '__main__':
    check()
