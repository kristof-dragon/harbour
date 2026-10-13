"""Standalone stdlib service: two iperf listeners and a read-only log API."""
import datetime as dt
import hmac
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DAY = re.compile(r'\d{2}-\d{2}-\d{2}')
FILE = re.compile(r'[a-zA-Z0-9_-]+\.jsonl')
MAX_FILE = 3 * 1024 * 1024


class Logs:
    def __init__(self, root, days=30, max_bytes=1024 ** 3):
        self.root, self.days, self.max_bytes = Path(root), days, max_bytes
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock, self.files = threading.RLock(), {}

    def append(self, source, event, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        record = (json.dumps({'at': now.isoformat(), **event}) + '\n').encode()
        with self.lock:
            day = self.root / now.strftime('%y-%m-%d')
            day.mkdir(mode=0o700, exist_ok=True)
            if day.is_symlink():
                raise OSError('Log day must not be a symlink')
            path = self.files.get(source)
            if path is None or path.parent != day or path.stat().st_size + len(record) > MAX_FILE:
                path = day / (source + '-' + now.strftime('%H%M%S') + '-' + secrets.token_hex(4) + '.jsonl')
                self.files[source] = path
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(record)

    def prune(self, now=None):
        now = now or time.time()
        with self.lock:
            files = []
            for day in self.root.iterdir():
                if not DAY.fullmatch(day.name) or day.is_symlink() or not day.is_dir():
                    continue
                for path in day.iterdir():
                    if FILE.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                        info = path.stat()
                        files.append((info.st_mtime, info.st_size, path))
            total = sum(size for _, size, _ in files)
            for modified, size, path in sorted(files):
                if path not in self.files.values() and (modified < now - self.days * 86400 or total > self.max_bytes):
                    path.unlink(); total -= size
            for day in self.root.iterdir():
                if DAY.fullmatch(day.name) and not day.is_symlink() and day.is_dir():
                    try:
                        day.rmdir()
                    except OSError:
                        pass

    def read(self, path):
        parts = path.split('/')
        if parts == ['', 'logs']:
            return json.dumps({'days': sorted([p.name for p in self.root.iterdir()
                if DAY.fullmatch(p.name) and p.is_dir() and not p.is_symlink()], reverse=True)}).encode(), 'application/json'
        if len(parts) not in (3, 4) or parts[:2] != ['', 'logs'] or not DAY.fullmatch(parts[2]):
            raise FileNotFoundError()
        # Directory/file descriptors prevent traversal, including symlink swaps.
        directory = os.open(self.root / parts[2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if len(parts) == 3:
                files = []
                for name in sorted(os.listdir(directory), reverse=True):
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if FILE.fullmatch(name) and stat.S_ISREG(info.st_mode):
                        files.append({'name': name, 'bytes': info.st_size})
                return json.dumps({'day': parts[2], 'files': files}).encode(), 'application/json'
            if not FILE.fullmatch(parts[3]):
                raise FileNotFoundError()
            fd = os.open(parts[3], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise FileNotFoundError()
                data = stream.read(MAX_FILE + 1)
                if len(data) > MAX_FILE:
                    raise OSError('Log file exceeds its limit')
                return data, 'application/x-ndjson'
        finally:
            os.close(directory)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Never log request paths, authorization headers or tokens.

    def do_GET(self):
        header = self.headers.get('Authorization', '')
        if not hmac.compare_digest(header.encode(), ('Bearer ' + self.server.token).encode()):
            self.send_response(401); self.send_header('WWW-Authenticate', 'Bearer'); self.send_header('Content-Length', '0'); self.end_headers()
            return
        if self.path == '/health':
            self.send_response(200 if self.server.ready() else 503)
            self.send_header('Content-Length', '0'); self.end_headers()
            return
        try:
            body, mime = self.server.logs.read(self.path)
        except OSError:
            self.send_error(404, 'Log not found'); return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers(); self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, logs, token, ready=lambda: True):
        self.logs, self.token = logs, token
        self.ready = ready
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request); return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release(); raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def main():
    config = json.loads(Path('/run/endpoint.json').read_text())
    logs = Logs('/logs', config['retention_days'])
    stop = threading.Event()
    children, lock = [], threading.Lock()
    def halt(*_):
        stop.set()
        with lock:
            for child in children:
                if child.poll() is None:
                    child.terminate()
    signal.signal(signal.SIGTERM, halt); signal.signal(signal.SIGINT, halt)
    def listen(name, port):
        while not stop.is_set():
            command = ['iperf3', '-s', '-p', str(port), '-J', '--forceflush', '--server-bitrate-limit', '10M',
                       '--server-max-duration', '330', '--rcv-timeout', '15000']
            try:
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError:
                stop.set(); return
            with lock:
                children.append(child)
                if stop.is_set():
                    child.terminate()
            try:
                logs.append(name, {'event': 'listening', 'port': port})
                while chunk := child.stdout.read1(8192):
                    logs.append(name, {'event': 'iperf_output', 'text': chunk.decode('utf-8', 'replace')})
                logs.append(name, {'event': 'exit', 'code': child.wait()})
            except Exception:
                stop.set()
            finally:
                if child.poll() is None:
                    child.kill(); child.wait()
                child.stdout.close()
                with lock:
                    children.remove(child)
            stop.wait(2)
    workers = [threading.Thread(target=listen, args=(name, port), daemon=True)
               for name, port in [('upload', 5201), ('download', 5202)]]
    def ready():
        with lock:
            return len(children) == 2 and all(child.poll() is None for child in children)
    server = Server(('0.0.0.0', 8080), logs, config['token'], ready)
    http = threading.Thread(target=server.serve_forever, daemon=True); http.start()
    for thread in workers:
        thread.start()
    try:
        while not stop.wait(30):
            logs.prune()
    finally:
        halt(); server.shutdown(); server.server_close()
        for thread in workers:
            thread.join(5)
        with lock:
            for child in children:
                child.kill()


if __name__ == '__main__':
    main()
