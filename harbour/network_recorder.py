"""On-site supervisor: isolated probes, router collection, writer and archives.

All router operations remain read-only. The supervisor owns acknowledged RAM
buffers; slow storage and large exports never run in the probe interpreter.
"""
import fcntl
import json
import logging
import multiprocessing
import secrets
import signal
import socket
import threading
import time

from . import logins, network, store
from .network_buffer import Buffer, Channel
from .network_probe import Echo, checksum  # retained as the public probe helpers


def maintenance(stop, data_directory=None, demo=None):
    from pathlib import Path
    from . import network_archives
    if data_directory is not None:
        store.DATA, store.DEMO = Path(data_directory), demo
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


class Producer:
    def __init__(self, context, target, args):
        self.stop = context.Event()
        local, remote = socket.socketpair()
        self.channel = Channel(local)
        self.process = context.Process(target=target, args=(*args, remote, self.stop), daemon=True)
        self.done, self.started, self.closing = False, time.monotonic(), None
        try:
            self.process.start()
        finally:
            remote.close()

    def messages(self):
        for _ in range(64):
            message = self.channel.receive()
            if message is not None:
                if message[0] == 'done':
                    self.done = True
                yield message
            elif not self.channel.incoming or self.channel.closed:
                break

    def request_stop(self):
        if self.closing is None:
            self.closing = time.monotonic()
            self.stop.set()

    def reap(self):
        if self.closing is not None and time.monotonic()-self.closing > 12 and self.process.is_alive():
            self.process.terminate()
        if not self.process.is_alive():
            self.process.join()
            self.channel.close()
            return True
        return False


class Worker:
    def __init__(self, context, server, settings, revision):
        self.context, self.server, self.settings, self.revision = context, server, settings, revision
        self.signature = logins.connection_signature(server)
        self.run_id = secrets.token_hex(16)
        self.run = {'id':self.run_id, 'started':time.time(), 'config':settings, 'observer':socket.gethostname()}
        self.buffer = Buffer(settings['buffer_mib']*1024*1024)
        self.status = {'state':'starting', 'observer':self.run['observer'], 'read_only':True,
                       'run_id':self.run_id, 'probe_errors':{}}
        self.losses, self.sources, self.source_status, self.producers = {}, {}, {}, {}
        self.retiring, self.restarts, self.writer_error = False, 0, False
        self.restart_at = {}
        self.start('probe'); self.start('router')

    def start(self, source):
        from . import network_collector, network_probe
        target, args = ((network_probe.run, (self.server['host'], self.settings, self.run_id))
                        if source == 'probe' else (network_collector.run, (self.server, self.settings, self.run_id)))
        self.producers[source] = Producer(self.context, target, args)
        self.sources[source] = time.monotonic()
        self.losses.setdefault(source, 0)
        self.source_status[source] = {}

    def gap(self, detail, **values):
        self.buffer.append(json.dumps({'at':time.time(), 'kind':'gap', 'run_id':self.run_id,
                                       'detail':detail, **values}, separators=(',', ':')).encode())

    def poll(self):
        for source, producer in list(self.producers.items()):
            for kind, _, dropped, data in producer.messages():
                old = self.source_status[source].get('dropped', 0)
                if dropped > old:
                    self.losses[source] += dropped-old
                    self.gap('Producer transfer buffer overflow', source=source, samples_lost=dropped-old)
                self.source_status[source]['dropped'] = dropped
                self.sources[source] = time.monotonic()
                if kind == 'row':
                    self.buffer.append(data)
                elif kind == 'status':
                    self.status.update(data)
            if not producer.process.is_alive():
                if not producer.done or not self.retiring:
                    self.gap('Recording process exited; untransferred readings may be missing', source=source)
                producer.reap()
                self.producers.pop(source)
                if not self.retiring:
                    self.restarts += 1
                    self.restart_at[source] = time.monotonic()+min(30, 2**min(self.restarts, 5))
            elif self.retiring:
                producer.reap()
        for source, when in list(self.restart_at.items()):
            if not self.retiring and time.monotonic() >= when:
                self.start(source)
                self.restart_at.pop(source)

    def health(self):
        now = time.monotonic()
        return {**self.status, **self.buffer.status(), 'process_restarts':self.restarts,
                'probe_online':'probe' in self.producers and now-self.sources.get('probe', 0) < 5,
                'router_collector_online':'router' in self.producers and now-self.sources.get('router', 0) < 5,
                'dropped_before_storage':self.buffer.dropped+sum(self.losses.values()),
                'writer_error':self.writer_error}

    def job(self):
        through, rows = self.buffer.batch()
        return {'server_id':self.server['id'], 'signature':self.signature, 'revision':self.revision,
                'run':self.run, 'through':through, 'rows':rows, 'status':self.health()}

    def retire(self):
        self.retiring = True
        for producer in self.producers.values():
            producer.request_stop()


class Pipeline:
    def __init__(self, context=None):
        self.context = context or multiprocessing.get_context('spawn')
        self.workers, self.active = {}, {}
        self.writer = self.writer_channel = None
        self.writer_stop = self.context.Event()
        self.maintenance_stop = self.context.Event()
        self.archiver = None
        self.inflight, self.next_write, self.cursor = None, 0, 0
        self.shutting_down = False
        self.config_stop = threading.Event()
        self.config_pending = None
        self.start_writer()
        self.start_archiver()
        self.config_thread = threading.Thread(target=self.watch_configuration, daemon=True)
        self.config_thread.start()

    def watch_configuration(self):
        from .network_storage import configuration
        while not self.config_stop.is_set():
            try:
                self.config_pending = configuration()
            except Exception:
                logging.exception('Network configuration read failed; retaining previous configuration')
            self.config_stop.wait(1)

    def start_writer(self):
        from .network_storage import run as write
        local, remote = socket.socketpair()
        self.writer_channel = Channel(local)
        self.writer_stop = self.context.Event()
        self.writer = self.context.Process(target=write, args=(remote, self.writer_stop, str(store.DATA), store.DEMO), daemon=True)
        try:
            self.writer.start()
        finally:
            remote.close()
        self.inflight, self.next_write = None, 0

    def start_archiver(self):
        self.archiver = self.context.Process(target=maintenance,
            args=(self.maintenance_stop, str(store.DATA), store.DEMO), daemon=True)
        self.archiver.start()

    def configure(self, active):
        if active is None or self.shutting_down:
            return
        for id_, worker in list(self.active.items()):
            candidate = active.get(id_)
            if (candidate is None or candidate[2] != worker.revision
                    or logins.connection_signature(candidate[0]) != worker.signature):
                worker.retire()
                self.active.pop(id_)
            else:
                worker.settings = candidate[1]
                worker.buffer.limit = candidate[1]['buffer_mib']*1024*1024
        for id_, (server, settings, revision) in active.items():
            if id_ not in self.active:
                worker = Worker(self.context, server, settings, revision)
                self.workers[worker.run_id] = worker
                self.active[id_] = worker

    def tick(self):
        pending, self.config_pending = self.config_pending, None
        if pending is not None:
            self.configure(pending)
        for worker in list(self.workers.values()):
            worker.poll()
        if not self.writer.is_alive():
            self.writer.join()
            self.writer_channel.close()
            for worker in self.workers.values():
                worker.writer_error = True
            self.start_writer()
        if not self.shutting_down and not self.archiver.is_alive():
            self.archiver.join()
            self.start_archiver()
        self.writer_channel.flush()
        response = self.writer_channel.receive()
        if response is not None:
            for run_id, through in response['ack']:
                if run_id in self.workers:
                    self.workers[run_id].buffer.acknowledge(through)
                    self.workers[run_id].writer_error = False
            for run_id in response['errors']:
                if run_id in self.workers:
                    self.workers[run_id].writer_error = True
            self.inflight = None
            # Drain a backlog promptly, but keep normal flushes to one per second.
            backlog = any(len(w.buffer.rows) > 256 or w.buffer.size > 1024*1024 for w in self.workers.values())
            cadence = max(.05, 1/max(1, (len(self.workers)+3)//4))
            self.next_write = time.monotonic()+(.05 if backlog or self.shutting_down else cadence)
        for run_id, worker in list(self.workers.items()):
            if worker.retiring and not worker.producers and not worker.buffer.rows:
                self.workers.pop(run_id)
        if self.inflight is None and time.monotonic() >= self.next_write:
            candidates = list(self.workers.values())
            # Bound serialization/IPC per request even with many monitored routers.
            jobs = []
            for n in range(min(4, len(candidates))):
                jobs.append(candidates[(self.cursor+n) % len(candidates)].job())
            self.cursor += len(jobs)
            self.writer_channel.send({'jobs':jobs})
            self.inflight = time.monotonic()

    def health(self):
        stalled = self.inflight is not None and time.monotonic()-self.inflight > 5
        # Exclude settings and credentials from the web-facing runtime snapshot.
        return {id_:{**worker.health(), 'recorder_online':True, 'runtime_updated':time.time(),
                     'writer_stalled':stalled} for id_, worker in self.active.items()}

    def close(self):
        self.shutting_down = True
        self.config_stop.set()
        self.maintenance_stop.set()
        for worker in self.workers.values():
            worker.retire()
        deadline = time.monotonic()+30
        while self.workers and time.monotonic() < deadline:
            self.tick()
            time.sleep(.01)
        lost = sum(len(worker.buffer.rows) for worker in self.workers.values())
        if lost:
            logging.error('Recorder shutdown could not commit %d buffered readings', lost)
        self.writer_stop.set()
        for worker in self.workers.values():
            for producer in worker.producers.values():
                producer.process.terminate()
                producer.process.join(2)
                producer.channel.close()
        for process in (self.writer, self.archiver):
            process.join(2)
            if process.is_alive():
                process.terminate(); process.join(2)
        self.writer_channel.close()
        self.config_thread.join(2)


def run(stop):
    from .network_runtime import StatusServer
    pipeline, status = Pipeline(), None
    try:
        try:
            status = StatusServer()
        except OSError:
            logging.exception('Live buffer status unavailable; database status remains available')
        while not stop.is_set():
            pipeline.tick()
            if status:
                status.snapshot = pipeline.health()
            stop.wait(.01)
    finally:
        pipeline.close()
        if status:
            status.close()


def main():
    store.DATA.mkdir(parents=True, exist_ok=True)
    with (store.DATA / 'network-recorder.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
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
    logging.basicConfig(level=logging.INFO)
    main()
