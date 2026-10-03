"""One resource worker per host; at most one pending or active check each."""
import logging
import math
import threading
import time

from .ssh import Cancellation, Cancelled

changed = threading.Event()


class ServerWorker:
    def __init__(self, server_id, run):
        self.server_id, self.run = server_id, run
        self.condition = threading.Condition()
        self.signature = None
        self.enabled = False
        self.interval = 60
        self.closed = self.pending = self.active = False
        self.cancel = None
        self.started = None
        self.next_at = 0
        self.thread = threading.Thread(target=self.loop, name='resources-' + server_id, daemon=True)
        self.thread.start()

    def configure(self, signature, enabled, interval):
        cancel = None
        with self.condition:
            changed = self.signature != signature or self.enabled != enabled
            if changed:
                self.pending = False
                self.next_at = 0
                cancel = self.cancel
            elif self.interval != interval and self.started is not None:
                self.next_at = self.started + interval
            self.signature, self.enabled, self.interval = signature, enabled, interval
            self.condition.notify_all()
        if cancel:
            cancel.stop()

    def request(self):
        with self.condition:
            if self.closed or not self.enabled or self.pending or self.active or time.monotonic() < self.next_at:
                return False
            self.pending = True
            self.condition.notify()
            return True

    def close(self):
        with self.condition:
            self.closed, self.pending = True, False
            cancel = self.cancel
            self.condition.notify_all()
        if cancel:
            cancel.stop()

    def loop(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending)
                if self.closed:
                    return
                self.pending, self.active = False, True
                signature = self.signature
                self.cancel = cancel = Cancellation()
                self.started = time.monotonic()
            try:
                self.run(self.server_id, cancel)
            except Cancelled:
                pass
            except Exception:
                logging.exception('Resource worker failed for server %s', self.server_id)
            finally:
                with self.condition:
                    # Skip elapsed slots instead of queueing catch-up checks.
                    if self.signature == signature and not cancel.is_set():
                        elapsed = max(0, time.monotonic() - self.started)
                        self.next_at = self.started + (math.floor(elapsed / self.interval) + 1) * self.interval
                    self.active, self.cancel = False, None
                    self.condition.notify_all()


class ServerWorkers:
    def __init__(self, run):
        self.run = run
        self.lock = threading.RLock()
        self.workers = {}
        self.retired = []
        self.closed = False

    def sync(self, configurations):
        """Configurations map IDs to (connection signature, enabled, interval)."""
        with self.lock:
            if self.closed:
                return
            self.retired = [w for w in self.retired if w.thread.is_alive()]
            for id_ in self.workers.keys() - configurations.keys():
                worker = self.workers.pop(id_)
                worker.close()
                self.retired.append(worker)
            for id_, settings in configurations.items():
                if id_ not in self.workers:
                    self.workers[id_] = ServerWorker(id_, self.run)
                self.workers[id_].configure(*settings)

    def request(self, id_):
        with self.lock:
            worker = self.workers.get(id_)
            return bool(worker and not self.closed and worker.request())

    def close(self):
        with self.lock:
            self.closed = True
            workers = [*self.workers.values(), *self.retired]
            for worker in workers:
                worker.close()
        # Cancellation closes active SSH clients. Join before the data store can
        # be replaced/reopened by another lifespan; no abandoned poll threads.
        for worker in workers:
            worker.thread.join()
        with self.lock:
            self.workers.clear()
            self.retired.clear()
