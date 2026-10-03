import base64
import hashlib
import hmac
import io
import json
import socket
import threading
import time
from pathlib import Path

import paramiko

from . import store

DISABLED_ALGORITHMS = {"pubkeys": ["ssh-rsa"], "keys": ["ssh-rsa"]}


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
        source = Path(__file__).with_name('remote_install_key.py').read_text()
        public = key.get_name() + ' ' + key.get_base64()
        source += '\nimport json\nprint(json.dumps(install_public_key(' + repr(public) + ')))\n'
        stdin, stdout, stderr = client.exec_command('python3 -', timeout=15)
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
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=15)
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
