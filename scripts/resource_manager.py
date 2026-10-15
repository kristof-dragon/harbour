#!/usr/bin/env python3
"""Host-only resource controller. No HTTP, shell input, or general Docker API."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time

if __package__:
    from . import resource_profile, start
else:
    import resource_profile, start
from harbour import resource_config

SERVICES = ('harbour', 'network-recorder')


def command(args, timeout=20):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('HARBOUR_', 'COMPOSE_')) and k != 'FIRST_RUN'}
    return subprocess.run(args, cwd=start.ROOT, env=env, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout).stdout


def compose(*args, timeout=20):
    return command(['docker', 'compose', '--project-directory', str(start.ROOT),
                    '--env-file', str(start.ROOT / '.env'), *args], timeout)


def saved():
    _, values = start.read_env(start.ROOT / '.env')
    if values['FIRST_RUN'].lower() != 'false':
        raise ValueError('Complete first-run setup before changing resource settings.')
    return resource_config.from_env(values)


def inspect():
    identities = compose('ps', '-a', '-q', *SERVICES).split()
    if not identities or any(not re.fullmatch('[a-f0-9]{12,64}', i) for i in identities):
        return [], None
    containers = json.loads(command(['docker', 'inspect', *identities]))
    parts, configs = [], []
    for container in containers:
        service = container['Config']['Labels'].get('com.docker.compose.service')
        if service not in SERVICES:
            continue
        # Filter inside the host process. Credentials never leave this function.
        env = dict(v.split('=', 1) for v in container['Config']['Env'] if v.startswith('HARBOUR_') and '=' in v)
        cfg = {k: env.get(k) for k in ('HARBOUR_RESOURCE_PROFILE', *('HARBOUR_'+f.upper() for f in resource_config.FIELDS[2:]))}
        state = container['State']
        parts.append(dict(service=service, id=container['Id'], memory_mib=container['HostConfig']['Memory']//1048576,
                          swap_mib=container['HostConfig']['MemorySwap']//1048576,
                          running=bool(state.get('Running')), oom=bool(state.get('OOMKilled')),
                          healthy=state.get('Health', {}).get('Status', 'healthy') == 'healthy'))
        configs.append(cfg)
    parts.sort(key=lambda p: SERVICES.index(p['service']))
    active = None
    if len(parts) == 2 and {p['service'] for p in parts} == set(SERVICES) and configs[0] == configs[1]:
        try:
            active = resource_config.from_env({**{k: v for k, v in configs[0].items() if v is not None},
                'HARBOUR_WEB_MEMORY_MIB': parts[0]['memory_mib'], 'HARBOUR_RECORDER_MEMORY_MIB': parts[1]['memory_mib']})
        except ValueError:
            pass
    return parts, active


def host_memory():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) // 1024
    return None


def verified(parts, actual, expected):
    return actual == expected and len(parts) == 2 and all(
        p['running'] and p['healthy'] and not p['oom'] and p['swap_mib'] == p['memory_mib'] for p in parts)


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


class Manager:
    def __init__(self, directory):
        self.directory = directory
        self.guard = threading.RLock()
        self.operation = None
        path = directory / 'state.json'
        if path.exists():
            with path.open('rb') as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError('Resource state is too large')
            self.operation = json.loads(raw)
            if self.operation.get('status') == 'applying':
                self.record(status='failed', message='The resource controller restarted during this change. Check the active values before retrying.')

    def record(self, **values):
        with self.guard:
            self.operation = {**(self.operation or {}), **values, 'updated_at': time.time()}
            atomic_json(self.directory / 'state.json', self.operation)

    def status(self):
        settings = saved()
        parts, active = inspect()
        revision = hashlib.sha256(json.dumps([settings, parts, active], sort_keys=True).encode()).hexdigest()
        return dict(available=True, saved=settings, active=active, components=parts, revision=revision,
                    host_memory_mib=host_memory(), applied=verified(parts, active, settings), operation=self.operation)

    def handle(self, request):
        if request == {'action': 'status'}:
            return self.status()
        if not isinstance(request, dict) or set(request) != {'action', 'settings', 'revision', 'request_id'} or request['action'] != 'apply':
            raise ValueError('Unsupported resource request')
        cfg = resource_config.validate(request['settings'])
        identity = request['request_id']
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9-]{36}', identity):
            raise ValueError('Invalid request identifier')
        with self.guard:
            if self.operation and identity == self.operation['request_id']:
                if cfg != self.operation['settings']:
                    raise ValueError('This request identifier already belongs to another change.')
                return {'operation': self.operation}
            if self.operation and self.operation['status'] == 'applying':
                return dict(error='A resource change is already in progress.', code=409)
            lock = os.open(start.ROOT / '.env.setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return dict(error='Harbour is being updated. Try again after startup finishes.', code=409)
                current = self.status()
                if request['revision'] != current['revision']:
                    return dict(error='Resource settings changed elsewhere. Reload this panel before applying.', code=409)
                if current['host_memory_mib'] and cfg['web_memory_mib'] + cfg['recorder_memory_mib'] > current['host_memory_mib'] - 128:
                    raise ValueError('Combined memory limits must leave at least 128 MiB for this host. Choose smaller limits.')
                self.operation = None
                self.record(request_id=identity, status='applying', settings=cfg,
                            message='Saving settings and restarting Harbour…')
                thread = threading.Thread(target=self.apply, args=(cfg, current['saved'], lock), daemon=False)
                thread.start()
                lock = None  # The worker holds the startup lock through validation/rollback.
                return {'operation': self.operation}
            finally:
                if lock is not None: os.close(lock)

    def write(self, cfg):
        resource_profile.apply(start.ROOT / '.env', cfg['profile'], {k.upper(): cfg[k] for k in resource_config.FIELDS})

    def restart(self):
        # No build, pull, user-supplied service names, environment, or commands.
        compose('up', '-d', '--no-build', '--pull', 'never', '--force-recreate', '--wait', '--wait-timeout', '120', *SERVICES, timeout=240)

    def apply(self, cfg, previous, lock):
        changed = False
        try:
            # Give the accepted response time to reach the browser before stopping web.
            time.sleep(1)
            self.write(cfg)
            changed = True
            self.restart()
            if not verified(*inspect(), cfg):
                raise RuntimeError('Resource readback did not match')
            self.record(status='succeeded', message='Applied. Both services are running with the requested limits.')
        except Exception:
            rolled_back = False
            if changed:
                try:
                    self.write(previous)
                    self.restart()
                    rolled_back = verified(*inspect(), previous)
                except Exception:
                    pass
            self.record(status='failed', rolled_back=rolled_back,
                message='The change failed. Previous settings were restored and verified.' if rolled_back else
                'The change could not be verified. Check the active values; the deployment may need administrator attention.')
        finally:
            os.close(lock)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        # Standard Linux Docker runs Harbour as 10001. Host owner/root already
        # control this deployment. Other local users cannot operate this socket.
        if uid not in (10001, os.getuid(), 0):
            return
        try:
            raw = self.rfile.readline(4097)
            if len(raw) > 4096 or not raw.endswith(b'\n'):
                raise ValueError('Resource request is too large or incomplete')
            result = self.server.manager.handle(json.loads(raw))
        except (ValueError, TypeError) as error:
            result = dict(error=str(error), code=400)
        except Exception:
            result = dict(error='The host could not read or apply resource settings. Current limits have not been confirmed.', code=503)
        self.wfile.write(json.dumps(result).encode()+b'\n')


def prepare():
    directory = start.ROOT / '.harbour-control'
    if directory.is_symlink():
        raise RuntimeError('Resource control directory must not be a symlink')
    directory.mkdir(mode=0o755, exist_ok=True)
    os.chmod(directory, 0o755)
    return directory


def install():
    """Called by the host launcher, never by the container or HTTP API."""
    if sys.platform != 'linux':
        print('UI resource controls require a Linux host with a user service manager.')
        return
    # Keep the helper alive after SSH logout and across host reboots.
    username = command(['id', '-un']).strip()
    if command(['loginctl', 'show-user', username, '-p', 'Linger', '--value']).strip() != 'yes':
        try:
            command(['loginctl', 'enable-linger', username])
        except subprocess.CalledProcessError:
            command(['sudo', '-n', 'loginctl', 'enable-linger', username])
    unit = Path.home() / '.config/systemd/user/harbour-resources.service'
    unit.parent.mkdir(parents=True, exist_ok=True)
    # Systemd quotes, specifiers and environment expansion are handled explicitly.
    def quote(value):
        return '"'+str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')+'"'
    unit.write_text('[Unit]\nDescription=Harbour resource settings controller\n\n[Service]\n'
        'Type=simple\nExecStart=:'+quote(sys.executable)+' '+quote(start.ROOT / 'scripts/resource_manager.py')+'\n'
        'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=300\nKillMode=mixed\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')
    command(['systemctl', '--user', 'daemon-reload'])
    command(['systemctl', '--user', 'enable', 'harbour-resources.service'])
    command(['systemctl', '--user', 'restart', 'harbour-resources.service'])
    print('Resource settings are available in Harbour → Global settings.')


def main():
    directory = prepare()
    fd = os.open(directory / 'manager.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = directory / 'resources.sock'
    path.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(path), Handler) as server:
        server.manager = Manager(directory)
        os.chmod(path, 0o666)  # Peer credentials authorize each connection.
        server.serve_forever()


if __name__ == '__main__':
    main()
