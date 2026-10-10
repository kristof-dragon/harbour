import base64
import hashlib
import hmac
import io
import json
import socket
import shlex
import threading
import time
from pathlib import Path

import paramiko

from . import store

DISABLED_ALGORITHMS = {"pubkeys": ["ssh-rsa"], "keys": ["ssh-rsa"]}

# Fixed code only: no host, credential or browser-supplied value enters the shell.
# macOS SSH sessions omit Homebrew and Docker Desktop from PATH. Prefer installed
# Python over Apple's development-tools stub and expose Docker credential helpers.
REMOTE_ENV = '''
if [ "$(uname -s)" = Darwin ]; then
    PATH="/opt/homebrew/bin:/usr/local/bin:$PATH:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin"
    export PATH
fi
'''.strip()
REMOTE_PYTHON = '/bin/sh -c ' + shlex.quote(REMOTE_ENV + '\nexec python3 -')


def host_fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def probe_host_key(host, port=22):
    """Read the negotiated host key without authenticating or opening a channel.

    This is discovery, not independent identity verification. The administrator
    explicitly accepts the result before it is pinned during server onboarding.
    """
    if store.DEMO:
        raise RuntimeError("SSH is disabled in demo mode")
    with socket.create_connection((host, port), timeout=10) as connection:
        peer = connection.getpeername()[0]
        transport = paramiko.Transport(connection, disabled_algorithms=DISABLED_ALGORITHMS)
        try:
            transport.banner_timeout = 10
            transport.handshake_timeout = 10
            transport.start_client(timeout=12)
            key = transport.get_remote_server_key()
            return {"host": host, "port": port, "address": peer, "key_type": key.get_name(),
                    "fingerprint": host_fingerprint(key)}
        finally:
            transport.close()


class ProbeError(RuntimeError):
    def __init__(self, message, status="unknown", latency_ms=None):
        super().__init__(message)
        self.status = status
        self.latency_ms = latency_ms


class Cancelled(RuntimeError):
    """An intentionally cancelled read, never a server health failure."""


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.closer = None

    def is_set(self):
        return self.event.is_set()

    def check(self):
        if self.is_set():
            raise Cancelled('Resource check cancelled')

    def bind(self, closer):
        with self.lock:
            self.closer = closer
        if self.is_set():
            closer()
            self.check()

    def stop(self):
        self.event.set()
        with self.lock:
            closer = self.closer
        if closer:
            closer()

    def unbind(self):
        with self.lock:
            self.closer = None


class HostKeyMismatch(paramiko.SSHException):
    pass


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def missing_host_key(self, client, hostname, key):
        actual = host_fingerprint(key)
        if not hmac.compare_digest(actual, self.fingerprint):
            raise HostKeyMismatch("Host fingerprint mismatch. Verify the server identity using a trusted connection.")


def parse_key(private_key, passphrase=None):
    for kind in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return kind.from_private_key(io.StringIO(private_key), password=passphrase)
        except (paramiko.SSHException, ValueError):
            pass
    raise ValueError("Cannot read the private key. Check its format and passphrase.")


def stored_key(key_id):
    key = store.one("SELECT encrypted FROM ssh_keys WHERE id=?", (key_id,))
    if not key:
        raise ValueError("Server credential is missing")
    credential = json.loads(store.cipher().decrypt(key["encrypted"].encode()))
    return parse_key(credential["private_key"], credential.get("passphrase"))


def connect(server, cancel=None, **credential):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHostKey(server["fingerprint"]))
    try:
        if cancel:
            cancel.bind(client.close)
            cancel.check()
        client.connect(server["host"], port=server["port"], username=server["username"],
                       allow_agent=False, look_for_keys=False, timeout=10, auth_timeout=15, banner_timeout=15,
                       disabled_algorithms=DISABLED_ALGORITHMS, **credential)
        if cancel:
            cancel.check()
        return client
    except Exception:
        client.close()
        raise


class KeyInstallError(RuntimeError):
    """Safe user-facing errors; never forward raw SSH output or credentials."""


def install_key(server, key_id, password):
    if server.get('server_type') == 'openwrt':
        raise KeyInstallError('OpenWRT connections are read-only. Use an existing authorised credential.')
    if store.DEMO:
        raise KeyInstallError("Key installation is disabled in demo mode.")
    key = stored_key(key_id)
    try:
        client = connect(server, password=password)
    except HostKeyMismatch:
        raise KeyInstallError("Host fingerprint mismatch. Fetch and verify the server fingerprint before retrying.") from None
    except paramiko.AuthenticationException:
        raise KeyInstallError("SSH password sign-in failed. Check the username and password, and that the server allows password authentication.") from None
    except Exception:
        raise KeyInstallError("Could not establish an SSH connection to install the key. Check the address and port.") from None
    try:
        # Even callers omitting server_type cannot install a key on OpenWrt.
        _, target_check, _ = client.exec_command('test -f /etc/openwrt_release', timeout=10)
        check_deadline = time.monotonic() + 10
        while not target_check.channel.exit_status_ready():
            if time.monotonic() > check_deadline:
                raise KeyInstallError('Could not verify the target platform; key installation was not attempted.')
            time.sleep(.01)
        if target_check.channel.recv_exit_status() == 0:
            raise KeyInstallError('OpenWRT connections are read-only. Use an existing authorised credential.')
        target_check.channel.close()
        source = Path(__file__).with_name('remote_install_key.py').read_text()
        public = key.get_name() + ' ' + key.get_base64()
        source += '\nimport json\nprint(json.dumps(install_public_key(' + repr(public) + ')))\n'
        stdin, stdout, stderr = client.exec_command(REMOTE_PYTHON, timeout=15)
        stdin.write(source)
        stdin.flush()
        stdin.channel.shutdown_write()
        channel, output, errors = stdout.channel, bytearray(), bytearray()
        deadline = time.monotonic() + 20
        while True:
            if channel.recv_ready():
                output.extend(channel.recv(4096))
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(4096))
            if len(output) + len(errors) > 8192 or time.monotonic() > deadline:
                raise TimeoutError()
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(.01)
        if channel.recv_exit_status() != 0:
            raise RuntimeError()
        result = json.loads(output)
        if result.get('status') not in {'installed', 'present'}:
            raise RuntimeError()
    except KeyInstallError:
        raise
    except Exception:
        raise KeyInstallError("Key installation could not be confirmed. Check Python 3 and ~/.ssh permissions on the server; retrying will not duplicate the key.") from None
    finally:
        client.close()
    try:
        # A fresh connection proves the key works without retaining or reusing the password.
        with connect(server, pkey=key) as verification:
            _, reply, _ = verification.exec_command('true', timeout=10)
            deadline = time.monotonic() + 10
            while not reply.channel.exit_status_ready():
                if time.monotonic() > deadline:
                    raise TimeoutError()
                time.sleep(.01)
            if reply.channel.recv_exit_status() != 0:
                raise RuntimeError()
    except Exception:
        raise KeyInstallError("The public key is installed, but key-based command access failed. Check the server's SSH settings before saving.") from None
    return {**result, 'verified': True}


class ProgressFrames:
    def __init__(self, on_event=None):
        self.pending = bytearray()
        self.result = None
        self.on_event = on_event

    def feed(self, chunk):
        self.pending.extend(chunk)
        while b'\n' in self.pending:
            line, _, self.pending = self.pending.partition(b'\n')
            if not line.strip():
                continue
            frame = json.loads(line)
            if 'event' in frame:
                if self.on_event:
                    self.on_event(frame['event'])
            elif 'result' in frame:
                self.result = frame['result']
            else:
                raise RuntimeError('Invalid progress response')
        if len(self.pending) > 8_000_000:
            raise RuntimeError('Server response exceeded the 8 MB limit')

    def finish(self):
        if self.pending.strip():
            self.feed(b'\n')
        if not isinstance(self.result, dict):
            raise RuntimeError('SSH ended before the operation result arrived. Check the server before retrying.')
        return self.result


def request(server, payload, on_event=None, cancel=None):
    if store.DEMO:
        raise RuntimeError("SSH is disabled in demo mode")
    if server.get('server_type') == 'openwrt':
        if payload.get('operation') != 'resources':
            raise ValueError('OpenWRT supports read-only resource and network diagnostics only')
        return resource_request(server, payload, cancel)
    if payload.get('operation') == 'resources':
        return resource_request(server, payload, cancel)
    client = None
    credential = {}
    connected, latency_ms = False, None
    try:
        if cancel:
            cancel.check()
        credential = ({'password': store.cipher().decrypt(server['password_encrypted'].encode()).decode()}
                      if server.get('auth_method') == 'password' else {'pkey': stored_key(server['key_id'])})
        client = connect(server, **credential, **({'cancel': cancel} if cancel else {}))
        transport = client.get_transport()
        transport.set_keepalive(20)
        connected = True
        begin = time.monotonic()
        _, reply, _ = client.exec_command("true", timeout=10)
        while not reply.channel.exit_status_ready():
            if cancel:
                cancel.check()
            if time.monotonic() - begin > 10:
                raise TimeoutError("SSH latency check timed out")
            time.sleep(.005)
        if reply.channel.recv_exit_status() != 0:
            raise RuntimeError("SSH round-trip check failed")
        latency_ms = round((time.monotonic() - begin) * 1000, 1)
        reply.channel.close()
        source = Path(__file__).with_name("remote_probe.py").read_text()
        # repr(json) embeds only a data literal; host/user/commands never enter a shell string.
        streaming = payload.get('operation') == 'execute'
        if streaming:
            source += "\ndef emit(event):\n print(json.dumps({'event':event}), flush=True)\ntry:\n result=handle(json.loads(" + repr(json.dumps(payload)) + "), emit)\nexcept Exception as exc:\n result={'error':str(exc)}\nprint(json.dumps({'result':result}), flush=True)\n"
        else:
            source += "\ntry:\n print(json.dumps(handle(json.loads(" + repr(json.dumps(payload)) + "))))\nexcept Exception as exc:\n print(json.dumps({'error': str(exc)}))\n"
        stdin, stdout, stderr = client.exec_command(REMOTE_PYTHON, timeout=15)
        if cancel:
            cancel.check()
        stdin.write(source)
        stdin.flush()
        stdin.channel.shutdown_write()
        channel = stdout.channel
        output, errors = bytearray(), bytearray()
        frames = ProgressFrames(on_event) if streaming else None
        deadline = time.monotonic() + (900 if payload.get("updates") or payload.get("operation") == "execute" else 90)
        while True:
            if cancel:
                cancel.check()
            if channel.recv_ready():
                chunk = channel.recv(65536)
                if frames:
                    frames.feed(chunk)
                else:
                    output.extend(chunk)
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(65536))
            if len(output) + len(errors) > 8_000_000:
                raise RuntimeError("Server response exceeded the 8 MB limit")
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() > deadline:
                raise TimeoutError("SSH operation timed out. Check the host before retrying; remote work may still be running.")
            time.sleep(0.02)
        if channel.recv_exit_status() != 0:
            raise RuntimeError(errors.decode(errors="replace")[-1000:] or "Remote Python failed")
        result = frames.finish() if frames else json.loads(output)
        if "error" in result:
            raise RuntimeError(result["error"])
        result["latency_ms"] = latency_ms
        return result
    except Exception as exc:
        if cancel and cancel.is_set():
            raise Cancelled('Resource check cancelled') from None
        status = "up" if connected else "down" if isinstance(exc, (OSError, TimeoutError)) else "unknown"
        message = str(exc)
        if server.get('auth_method') == 'password' and not connected and not isinstance(exc, HostKeyMismatch):
            message = ('SSH password sign-in failed. Check the saved username/password and server authentication settings.'
                       if isinstance(exc, paramiko.AuthenticationException) else 'Could not connect over SSH with the saved password. Check the connection settings.')
        if credential.get('password'):
            message = message.replace(credential['password'], '[redacted]')
        raise ProbeError(message, status, latency_ms) from None
    finally:
        if client:
            client.close()
        if cancel:
            cancel.unbind()


resource_sessions = {}
resource_sessions_lock = threading.RLock()


class ResourceSession:
    """One pinned monitoring connection and one session-bound reader per host."""
    def __init__(self, signature):
        self.signature = signature
        self.lock = threading.Lock()
        self.client = self.channel = self.stdin = None
        self.buffer = bytearray()
        self.retired = threading.Event()

    def close(self):
        client, self.client = self.client, None
        if client:
            client.close()

    def start(self, server, cancel):
        credential = ({'password': store.cipher().decrypt(server['password_encrypted'].encode()).decode()}
                      if server.get('auth_method') == 'password' else {'pkey': stored_key(server['key_id'])})
        self.client = connect(server, **credential, **({'cancel': cancel} if cancel else {}))
        if self.retired.is_set():
            self.close()
            raise Cancelled('Monitoring connection retired')
        if cancel:
            cancel.bind(self.close); cancel.check()
        self.client.get_transport().set_keepalive(30)
        source = Path(__file__).with_name('remote_probe.py').read_bytes()
        bootstrap = "import sys;exec(compile(sys.stdin.buffer.read(%d),'<harbour-probe>','exec'));serve_resources()" % len(source)
        command = '/bin/sh -c ' + shlex.quote(REMOTE_ENV + '\nexec python3 -u -c ' + shlex.quote(bootstrap))
        self.stdin, stdout, _ = self.client.exec_command(command, timeout=15)
        self.channel = stdout.channel
        self.buffer = bytearray()
        self.stdin.write(source); self.stdin.flush()

    def exchange(self, server, payload, cancel):
        while not self.lock.acquire(timeout=.1):
            if cancel:
                cancel.check()
        try:
            if self.retired.is_set():
                raise Cancelled('Monitoring connection retired')
            if cancel:
                cancel.check(); cancel.bind(self.close)
            if not self.client or not self.client.get_transport() or not self.client.get_transport().is_active() or self.channel.closed:
                self.close(); self.start(server, cancel)
            begin = time.monotonic()
            self.stdin.write(json.dumps(payload) + '\n'); self.stdin.flush()
            latency, received = None, 0
            while time.monotonic() - begin < 90:
                if cancel:
                    cancel.check()
                if self.channel.recv_ready():
                    chunk = self.channel.recv(65536)
                    self.buffer.extend(chunk); received += len(chunk)
                    if received > 8_000_000:
                        raise RuntimeError('Resource response exceeded 8 MB')
                if self.channel.recv_stderr_ready():
                    self.channel.recv_stderr(65536)  # Never expose remote stderr/credentials.
                while b'\n' in self.buffer:
                    line, _, rest = self.buffer.partition(b'\n')
                    self.buffer = bytearray(rest)
                    result = json.loads(line)
                    if result == {'ready': True}:
                        latency = round((time.monotonic() - begin) * 1000, 1)
                        continue
                    if not isinstance(result, dict) or 'error' in result:
                        raise ProbeError('Remote resource collection failed', 'up', latency)
                    result['latency_ms'] = latency
                    return result
                if self.channel.exit_status_ready() and not self.channel.recv_ready():
                    raise RuntimeError('SSH resource reader ended; it will reconnect on the next check')
                time.sleep(.01)
            raise TimeoutError('Resource collection timed out')
        except Exception:
            self.close()
            raise
        finally:
            if cancel:
                cancel.unbind()
            self.lock.release()


def resource_request(server, payload, cancel=None):
    from . import logins, recording
    signature = logins.connection_signature(server)
    with resource_sessions_lock:
        session = resource_sessions.get(server['id'])
        if session and session.signature != signature:
            session.retired.set(); session.close(); session = None
        if session is None:
            if server.get('server_type') == 'openwrt':
                from .openwrt import Session
                session = resource_sessions[server['id']] = Session(signature)
            else:
                session = resource_sessions[server['id']] = ResourceSession(signature)
    try:
        return session.exchange(server, recording.payload(server, payload), cancel)
    except Cancelled:
        raise
    except ProbeError:
        raise
    except Exception as exc:
        if cancel and cancel.is_set():
            raise Cancelled('Resource check cancelled') from None
        if isinstance(exc, HostKeyMismatch):
            message = str(exc)
        else:
            message = 'Resource monitoring connection failed. Check SSH access and the host recorder/probe.'
        raise ProbeError(message, 'down' if isinstance(exc, (OSError, TimeoutError)) else 'unknown') from None


def close_resources(id_=None):
    with resource_sessions_lock:
        ids = list(resource_sessions) if id_ is None else [id_]
        for key in ids:
            session = resource_sessions.pop(key, None)
            if session:
                session.retired.set()
                session.close()


def sync_resources(servers):
    from .logins import connection_signature
    active = {s['id']: s for s in servers}
    with resource_sessions_lock:
        for id_, session in list(resource_sessions.items()):
            server = active.get(id_)
            if (not server or connection_signature(server) != session.signature
                    or not server['monitoring_enabled'] and not session.lock.locked()):
                close_resources(id_)
