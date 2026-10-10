"""Small local status socket: buffer health remains visible during slow writes."""
import json
import os
import socket
import stat
import threading
import time

from . import store


def path():
    return str(store.DATA / 'network-recorder.sock')


def read_status(id_):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(.15)
            sock.connect(path())
            sock.sendall((id_+'\n').encode())
            raw = bytearray()
            while len(raw) <= 65536:
                chunk = sock.recv(8192)
                if not chunk:
                    return json.loads(raw) if raw else {}
                raw.extend(chunk)
    except (OSError, ValueError):
        pass
    return {}


class StatusServer:
    def __init__(self):
        self.snapshot = {}
        self.stop = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        destination = path()
        try:
            info = os.lstat(destination)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError('Runtime status path is not an owned socket')
            os.unlink(destination)
        except FileNotFoundError:
            pass
        self.socket.bind(destination)
        os.chmod(destination, 0o600)
        self.socket.listen(4)
        self.socket.settimeout(.1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.socket.accept()
                with connection:
                    connection.settimeout(.1)
                    request = connection.recv(256).decode().strip()
                    row = self.snapshot.get(request, {})
                    if row and time.time()-row['runtime_updated'] > 5:
                        row = {**row, 'recorder_online':False}
                    connection.sendall(json.dumps(row).encode())
            except (OSError, ValueError):
                pass

    def close(self):
        self.stop.set()
        self.thread.join(1)
        self.socket.close()
        try:
            os.unlink(path())
        except FileNotFoundError:
            pass
