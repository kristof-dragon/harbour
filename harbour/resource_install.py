"""Install or remove Harbour's resource recorder on the monitored host."""
import argparse
import json
import os
import plistlib
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = 'one.harbour.resources'
INSTALL = Path('/usr/local/libexec/harbour-resources')
RUNTIME = Path('/var/run/harbour-resources')
FILES = ('resource_agent.py', 'remote_probe.py', 'cpu.py', 'resource_install.py')


def state_path(platform):
    return Path('/Library/Application Support/HarbourResources' if platform == 'darwin' else '/var/lib/harbour-resources')


def service_path(platform):
    return Path('/Library/LaunchDaemons/' + LABEL + '.plist' if platform == 'darwin' else '/etc/systemd/system/harbour-resources.service')


def definition(platform, python, reader, interval=60):
    socket_path = state_path(platform) / 'recorder.sock' if platform == 'darwin' else RUNTIME / 'recorder.sock'
    args = [python, '-I', '-S', str(INSTALL / 'resource_agent.py'), '--state', str(state_path(platform)),
            '--socket', str(socket_path), '--interval', str(interval)]
    if platform == 'darwin':
        return plistlib.dumps({'Label': LABEL, 'ProgramArguments': args, 'UserName': reader,
                              'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                              'ProcessType': 'Background', 'Umask': 63})
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    return ('''[Unit]
Description=Harbour resource recorder
After=local-fs.target

[Service]
Type=simple
User=%s
ExecStart=%s
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
StateDirectory=harbour-resources
StateDirectoryMode=0700
RuntimeDirectory=harbour-resources
RuntimeDirectoryMode=0700
ReadWritePaths=/var/lib/harbour-resources /run/harbour-resources
RestrictAddressFamilies=AF_UNIX

[Install]
WantedBy=multi-user.target
''' % (quote(reader), ' '.join(quote(arg) for arg in args))).encode()


def stop_service(platform, target):
    if platform == 'linux':
        subprocess.run(['systemctl', 'disable', '--now', 'harbour-resources.service'], check=True)
    else:
        result = subprocess.run(['launchctl', 'print', 'system/' + LABEL], capture_output=True)
        if result.returncode == 0:
            subprocess.run(['launchctl', 'bootout', 'system/' + LABEL], check=True)


def uninstall(platform, purge=False, dry_run=False):
    target, state = service_path(platform), state_path(platform)
    print('Remove service:', target)
    print('Remove program:', INSTALL)
    print(('Delete' if purge else 'Retain') + ' recorded data:', state)
    if dry_run:
        return
    if os.geteuid() != 0:
        raise PermissionError('Run removal with sudo')
    # Fixed installation paths only. Never follow a replaced directory symlink.
    for path in (INSTALL, RUNTIME, state):
        if path.is_symlink():
            raise RuntimeError('Refusing a symlink at ' + str(path))
    if target.exists():
        stop_service(platform, target)
        target.unlink()
        if platform == 'linux':
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for name in (*FILES, 'installed.json'):
        (INSTALL / name).unlink(missing_ok=True)
    if INSTALL.exists():
        # Unknown files are deliberately retained.
        try:
            INSTALL.rmdir()
        except OSError:
            pass
    (RUNTIME / 'recorder.sock').unlink(missing_ok=True)
    (state / 'recorder.sock').unlink(missing_ok=True)
    if purge and state.exists():
        shutil.rmtree(state)
    print('Recorder removed. Harbour will use remote probes on its next check.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reader', help='Existing Harbour SSH account; recorder runs as this user')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--purge-data', action='store_true', help='With --uninstall, also delete the local resource queue')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if sys.platform not in ('linux', 'darwin') or sys.version_info < (3, 9):
        parser.error('Python 3.9+ on Linux or macOS is required')
    if args.uninstall:
        uninstall(sys.platform, args.purge_data, args.dry_run)
        return
    if args.purge_data:
        parser.error('--purge-data requires --uninstall')
    if not args.reader or not 15 <= args.interval <= 3600:
        parser.error('--reader is required; interval must be 15–3600 seconds')
    account = pwd.getpwnam(args.reader)
    python = str(Path(args.python).resolve(strict=True))
    if any(c in python + args.reader for c in '\n\r\x00'):
        parser.error('Invalid service argument')
    subprocess.run([python, '-I', '-S', '-c', 'import sys,sqlite3; assert sys.version_info >= (3,9)'], check=True)
    data = definition(sys.platform, python, args.reader, args.interval)
    print(data.decode())
    if args.dry_run:
        return
    if os.geteuid() != 0:
        parser.error('Run installation with sudo; the recorder itself runs as --reader')
    source = Path(__file__).resolve().parent
    contents = {name: (source / name).read_bytes() for name in FILES}
    for name, body in contents.items():
        compile(body, name, 'exec')
    target, state = service_path(sys.platform), state_path(sys.platform)
    for path in (INSTALL, state, RUNTIME):
        if path.is_symlink():
            parser.error('Refusing a symlink at ' + str(path))
    if (state / 'resources.db').exists() and state.stat().st_uid != account.pw_uid:
        parser.error('Existing recorder data belongs to a different reader; uninstall and explicitly purge before changing reader')
    if target.exists():
        stop_service(sys.platform, target)
    INSTALL.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chown(INSTALL, 0, 0); os.chmod(INSTALL, 0o755)
    for name, body in contents.items():
        stage = INSTALL / (name + '.new')
        stage.unlink(missing_ok=True)
        stage.write_bytes(body); os.chown(stage, 0, 0); os.chmod(stage, 0o644)
        stage.replace(INSTALL / name)
    for path in (state, RUNTIME):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path, account.pw_uid, account.pw_gid); os.chmod(path, 0o700)
    marker = INSTALL / 'installed.json'
    marker.unlink(missing_ok=True)
    marker.write_text(json.dumps({'reader': args.reader, 'protocol': 1}))
    os.chmod(marker, 0o644)
    target.write_bytes(data); os.chown(target, 0, 0); os.chmod(target, 0o644)
    if sys.platform == 'linux':
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', 'harbour-resources.service'], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'harbour-resources.service'], check=True)
    else:
        subprocess.run(['launchctl', 'bootstrap', 'system', str(target)], check=True)
    print('Installed. Harbour automatically selects this recorder on its next check.')


if __name__ == '__main__':
    main()
