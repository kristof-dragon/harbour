"""WebUI-delivered Linux Docker installer. Host Python remains stdlib-only."""
import argparse
import json
import os
import pwd
import shutil
import socket
import subprocess
from pathlib import Path

FILES = ('Dockerfile', 'compose.yaml', 'build-iperf.sh', 'call_endpoint_service.py')


def install(reader, source):
    if not shutil.which('docker'):
        raise RuntimeError('Install Docker Engine and its Compose plugin on this Linux host first.')
    user = pwd.getpwnam(reader)
    runtime_uid, runtime_gid = (user.pw_uid, user.pw_gid) if user.pw_uid else (10001, 10001)
    root = Path(user.pw_dir) / 'harbour-c-remote'
    if root.is_symlink():
        raise RuntimeError('Deployment folder must not be a symlink')
    root.mkdir(mode=0o700, exist_ok=True)
    marker = root / '.harbour-endpoint'
    if any(root.iterdir()) and (marker.is_symlink() or not marker.is_file()):
        raise RuntimeError('The deployment folder is not a Harbour endpoint; move its existing files first.')
    config = json.loads((source / 'endpoint.json').read_text())
    managed = (*FILES, 'endpoint.json', '.env', '.harbour-endpoint', '.dockerignore')
    for name in (*managed, 'logs'):
        if (root / name).is_symlink():
            raise RuntimeError('Deployment files must not be symlinks')
    compose = ['docker', 'compose', '--project-name', 'harbour-c-remote', '--project-directory', str(root)]
    subprocess.run(['docker', 'compose', 'version'], check=True)
    # An existing service owns its published ports during an update. For a fresh
    # install check availability before building; Docker rechecks on startup.
    if not marker.exists():
        for port, protocols in [(config['http_port'], [socket.SOCK_STREAM]),
                                (config['port'], [socket.SOCK_STREAM, socket.SOCK_DGRAM]),
                                (config['port'] + 1, [socket.SOCK_STREAM, socket.SOCK_DGRAM])]:
            for protocol in protocols:
                with socket.socket(socket.AF_INET, protocol) as probe:
                    try:
                        probe.bind(('0.0.0.0', port))
                    except OSError:
                        raise RuntimeError('Remote port ' + str(port) + ' is unavailable. Choose another port in the deployment form.') from None
    def write(name, data):
        temporary = root / ('.' + name + '.next')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(temporary, 0o600)
        os.chown(temporary, user.pw_uid, user.pw_gid)
        temporary.replace(root / name)
    # Keep one protected previous configuration for failed-upgrade recovery.
    previous = root / 'previous'
    if previous.is_symlink():
        raise RuntimeError('Backup folder must not be a symlink')
    previous.mkdir(mode=0o700, exist_ok=True)
    for name in managed:
        if (previous / name).is_symlink():
            raise RuntimeError('Backup files must not be symlinks')
        if (root / name).is_file():
            shutil.copyfile(root / name, previous / name)
            os.chmod(previous / name, 0o600)
    for name in FILES:
        write(name, (source / name).read_bytes())
    write('endpoint.json', json.dumps(config).encode())
    write('.env', ('TRAFFIC_PORT=%d\nDOWNLOAD_PORT=%d\nLOG_PORT=%d\nREMOTE_UID=%d\nREMOTE_GID=%d\n'
                   % (config['port'], config['port'] + 1, config['http_port'], runtime_uid, runtime_gid)).encode())
    write('.dockerignore', b'**\n!Dockerfile\n!build-iperf.sh\n!call_endpoint_service.py\n')
    write('.harbour-endpoint', b'1\n')
    logs = root / 'logs'; logs.mkdir(mode=0o700, exist_ok=True)
    for path in (root, logs):
        os.chown(path, user.pw_uid, user.pw_gid)
    for path in (logs, root / 'endpoint.json'):
        os.chown(path, runtime_uid, runtime_gid)
    print('Building remote call endpoint in ' + str(root), flush=True)
    subprocess.run([*compose, 'config', '--quiet'], check=True)
    subprocess.run([*compose, 'up', '-d', '--build', '--wait', '--wait-timeout', '45'], check=True)
    print('Endpoint ready. Logs: ~/harbour-c-remote/logs/YY-MM-DD (UTC). HTTP port: ' + str(config['http_port']), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--reader', required=True)
    args = parser.parse_args()
    try:
        install(args.reader, Path(__file__).resolve().parent)
    except Exception as exc:
        # No configuration or token is printed, even for subprocess failures.
        message = (str(exc) if isinstance(exc, RuntimeError) else
                   'Check Docker, port availability and folder permissions (' + type(exc).__name__ + ').')
        print('Endpoint installation failed. ' + message, flush=True)
        raise SystemExit(1)
