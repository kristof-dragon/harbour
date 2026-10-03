import base64
import hashlib
import hmac
import io
import json
import socket
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


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def missing_host_key(self, client, hostname, key):
        actual = host_fingerprint(key)
        if not hmac.compare_digest(actual, self.fingerprint):
            raise paramiko.SSHException("Host fingerprint mismatch. Verify the server identity using a trusted connection.")


def parse_key(private_key, passphrase=None):
    for kind in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return kind.from_private_key(io.StringIO(private_key), password=passphrase)
        except (paramiko.SSHException, ValueError):
            pass
    raise ValueError("Cannot read the private key. Check its format and passphrase.")


def request(server, payload):
    if store.DEMO:
        raise RuntimeError("SSH is disabled in demo mode")
    key = store.one("SELECT encrypted FROM ssh_keys WHERE id=?", (server["key_id"],))
    if not key:
        raise ValueError("Server credential is missing")
    credential = json.loads(store.cipher().decrypt(key["encrypted"].encode()))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHostKey(server["fingerprint"]))
    connected, latency_ms = False, None
    try:
        client.connect(server["host"], port=server["port"], username=server["username"],
                       pkey=parse_key(credential["private_key"], credential.get("passphrase")),
                       allow_agent=False, look_for_keys=False, timeout=10, auth_timeout=15, banner_timeout=15,
                       disabled_algorithms=DISABLED_ALGORITHMS)
        transport = client.get_transport()
        transport.set_keepalive(20)
        connected = True
        begin = time.monotonic()
        _, reply, _ = client.exec_command("true", timeout=10)
        while not reply.channel.exit_status_ready():
            if time.monotonic() - begin > 10:
                raise TimeoutError("SSH latency check timed out")
            time.sleep(.005)
        if reply.channel.recv_exit_status() != 0:
            raise RuntimeError("SSH round-trip check failed")
        latency_ms = round((time.monotonic() - begin) * 1000, 1)
        reply.channel.close()
        source = Path(__file__).with_name("remote_probe.py").read_text()
        # repr(json) embeds only a data literal; host/user/commands never enter a shell string.
        source += "\ntry:\n print(json.dumps(handle(json.loads(" + repr(json.dumps(payload)) + "))))\nexcept Exception as exc:\n print(json.dumps({'error': str(exc)}))\n"
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=15)
        stdin.write(source)
        stdin.flush()
        stdin.channel.shutdown_write()
        channel = stdout.channel
        output, errors = bytearray(), bytearray()
        deadline = time.monotonic() + (900 if payload.get("updates") or payload.get("operation") == "execute" else 90)
        while True:
            if channel.recv_ready():
                output.extend(channel.recv(65536))
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
        result = json.loads(output)
        if "error" in result:
            raise RuntimeError(result["error"])
        result["latency_ms"] = latency_ms
        return result
    except Exception as exc:
        status = "up" if connected else "down" if isinstance(exc, (OSError, TimeoutError)) else "unknown"
        raise ProbeError(str(exc), status, latency_ms) from exc
    finally:
        client.close()
