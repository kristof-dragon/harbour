"""Install the Harbour login collector on the monitored host (Python 3.9+).

Run --dry-run first to inspect the service definition. Installation needs root;
the collector socket is readable only by --reader. SSH configuration is unchanged.
"""
import argparse
import os
import plistlib
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = 'one.harbour.logins'
INSTALL = Path('/usr/local/libexec/harbour-logins')


def systemd_quote(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def definition(platform, python, reader, auth_file=None, endpoint_helper=False, max_events=20000):
    args = [python, '-I', '-S', str(INSTALL / 'login_agent.py'), '--reader', reader, '--max-events', str(max_events)]
    if auth_file:
        args += ['--auth-file', auth_file]
    if endpoint_helper:
        args += ['--endpoint-helper', str(INSTALL / 'login-events')]
    if platform == 'darwin':
        return Path('/Library/LaunchDaemons/' + LABEL + '.plist'), plistlib.dumps({
            'Label': LABEL, 'ProgramArguments': args, 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 10, 'ProcessType': 'Background', 'UserName': 'root', 'Umask': 63,
        })
    unit = '''[Unit]
Description=Harbour authentication event collector
After=systemd-journald.service

[Service]
Type=simple
ExecStart=%s
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/harbour-logins /run/harbour-logins
StateDirectory=harbour-logins
StateDirectoryMode=0700
RuntimeDirectory=harbour-logins
RuntimeDirectoryMode=0755
RestrictAddressFamilies=AF_UNIX

[Install]
WantedBy=multi-user.target
''' % ' '.join(systemd_quote(arg) for arg in args)
    return Path('/etc/systemd/system/harbour-logins.service'), unit.encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reader', required=True, help='Existing Harbour SSH username')
    parser.add_argument('--python', default=sys.executable, help='Trusted Python 3.9+ interpreter for the service')
    parser.add_argument('--auth-file', help='Linux authentication log if journald is unavailable')
    parser.add_argument('--endpoint-helper', help='Signed, entitled macOS helper binary to install')
    parser.add_argument('--max-events', type=int, default=20000)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if sys.platform not in ('linux', 'darwin') or sys.version_info < (3, 9):
        parser.error('Python 3.9+ on Linux or macOS is required')
    if not 1000 <= args.max_events <= 1000000:
        parser.error('--max-events must be between 1000 and 1000000')
    try:
        pwd.getpwnam(args.reader)
    except KeyError:
        parser.error('The reader account does not exist')
    python = str(Path(args.python).resolve(strict=True))
    if any(c in python + args.reader + (args.auth_file or '') for c in '\n\r\x00'):
        parser.error('Invalid service argument')
    subprocess.run([python, '-I', '-S', '-c', 'import sys; assert sys.version_info >= (3,9)'], check=True)
    if args.auth_file and (sys.platform != 'linux' or not Path(args.auth_file).is_file()):
        parser.error('--auth-file must be an existing Linux authentication log')
    helper = Path(args.endpoint_helper).resolve(strict=True) if args.endpoint_helper else None
    if helper:
        if sys.platform != 'darwin':
            parser.error('--endpoint-helper is macOS only')
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(helper)], check=True)
    target, data = definition(sys.platform, python, args.reader, args.auth_file, bool(helper), args.max_events)
    print('Service:', target)
    print(data.decode())
    if args.dry_run:
        return
    if os.geteuid() != 0:
        parser.error('Run installation with sudo; --dry-run does not need root')
    source = Path(__file__).resolve().with_name('login_agent.py')
    if not source.is_file():
        parser.error('Keep login_agent.py alongside this installer')
    # Compile before touching an existing installation.
    compile(source.read_text(), str(source), 'exec')
    INSTALL.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chown(INSTALL, 0, 0)
    os.chmod(INSTALL, 0o755)
    # Stage files before stopping an existing service; leave the durable queue intact.
    staged = INSTALL / 'login_agent.py.new'
    staged.write_bytes(source.read_bytes())
    os.chown(staged, 0, 0); os.chmod(staged, 0o644)
    if helper:
        shutil.copyfile(helper, INSTALL / 'login-events.new')
        os.chown(INSTALL / 'login-events.new', 0, 0)
        os.chmod(INSTALL / 'login-events.new', 0o755)
    old_service = target.exists()
    if old_service:
        shutil.copyfile(target, target.with_suffix(target.suffix + '.previous'))
    if sys.platform == 'linux':
        if old_service:
            subprocess.run(['systemctl', 'stop', 'harbour-logins.service'], check=True)
    elif old_service:
        subprocess.run(['launchctl', 'bootout', 'system/' + LABEL], check=False, capture_output=True)
    if (INSTALL / 'login_agent.py').exists():
        shutil.copyfile(INSTALL / 'login_agent.py', INSTALL / 'login_agent.py.previous')
    staged.replace(INSTALL / 'login_agent.py')
    if helper:
        (INSTALL / 'login-events.new').replace(INSTALL / 'login-events')
    target.write_bytes(data)
    os.chown(target, 0, 0); os.chmod(target, 0o644)
    if sys.platform == 'linux':
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', 'harbour-logins.service'], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'harbour-logins.service'], check=True)
    else:
        subprocess.run(['launchctl', 'bootstrap', 'system', str(target)], check=True)
    print('Installed. Harbour will discover the collector on its next resource check.')
    print('For rejected SSH keys and detailed attempts, configure LogLevel VERBOSE in sshd and validate with sshd -t.')
    if sys.platform == 'darwin':
        print('Log-only coverage is limited. The native helper needs its Endpoint Security entitlement and Full Disk Access.')


if __name__ == '__main__':
    main()
