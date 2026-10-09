"""Fixed SSH bootstrap: receive a bundle in the login user's home and install it.

Only standard-library Python 3.9+ is needed. JSON lines on stdout are progress;
stdin carries the length-delimited bundle followed by one-time sudo replies.
"""
import codecs
import hashlib
import json
import os
import pwd
import secrets
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def emit(kind, **values):
    print(json.dumps({'kind': kind, **values}), flush=True)


def output(text):
    emit('output', text=text)


def receive(config, stream):
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    fd, name = tempfile.mkstemp(prefix=config['prefix'] + '-', suffix='.zip', dir=home)
    archive = Path(name)
    remaining, digest = config['size'], hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as target:
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise RuntimeError('Upload interrupted')
                target.write(chunk); digest.update(chunk); remaining -= len(chunk)
                emit('progress', received=config['size'] - remaining, total=config['size'])
        if digest.hexdigest() != config['sha256']:
            raise RuntimeError('Bundle verification failed')
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    output('Uploaded: ' + str(archive) + '\n')
    if config['action'] == 'push':
        return archive, None
    folder = Path(tempfile.mkdtemp(prefix=config['prefix'] + '-', dir=home))
    # Only the expected flat, regular files may be extracted. Never overwrite a
    # previous upload or follow existing files/symlinks in the home directory.
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if (len(entries) != len(config['files']) or {i.filename for i in entries} != set(config['files'])
                or sum(i.file_size for i in entries) > 8_000_000):
            raise RuntimeError('Unexpected bundle contents')
        for entry in entries:
            if '/' in entry.filename or '\\' in entry.filename or entry.filename in ('.', '..'):
                raise RuntimeError('Unsafe bundle path')
            with (folder / entry.filename).open('xb') as target:
                target.write(bundle.read(entry))
            (folder / entry.filename).chmod(0o600)
    output('Extracted: ' + str(folder) + '\n')
    return archive, folder


class SudoOutput:
    """Recognize a prompt even when SSH/process reads split it across chunks."""
    def __init__(self, marker, prompt):
        self.marker, self.prompt, self.pending = marker, prompt, b''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def feed(self, chunk, final=False):
        self.pending += chunk
        while self.marker in self.pending:
            before, _, self.pending = self.pending.partition(self.marker)
            output(self.decoder.decode(before))
            self.prompt()
        keep = 0
        if not final:
            for size in range(1, min(len(self.pending), len(self.marker) - 1) + 1):
                if self.pending.endswith(self.marker[:size]):
                    keep = size
        text, self.pending = (self.pending[:-keep], self.pending[-keep:]) if keep else (self.pending, b'')
        if text or final:
            output(self.decoder.decode(text, final=final))


def run_installer(command, stream):
    marker = ('HARBOUR_SUDO_' + secrets.token_hex(16) + ':').encode()
    if os.geteuid() != 0:
        sudo = shutil.which('sudo')
        if not sudo:
            raise RuntimeError('sudo is not installed. Install manually using an administrator account.')
        # A pipe cannot echo the password. sudo handles NOPASSWD, cached tickets,
        # rejection/retry, and the configured target-password policy itself.
        command = [sudo, '-S', '-p', marker.decode(), '--', *command]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            selector.register(stream, selectors.EVENT_READ, 'input')
            def prompt():
                emit('password', message='Enter the password requested by sudo on this host.')
                with selectors.DefaultSelector() as waiter:
                    waiter.register(stream, selectors.EVENT_READ)
                    if not waiter.select(120):
                        raise RuntimeError('Sudo password prompt timed out')
                reply = json.loads(stream.readline(8192))
                password = reply.get('password')
                if (not isinstance(password, str) or not 1 <= len(password) <= 1024
                        or any(c in password for c in '\n\r\x00')):
                    raise RuntimeError('Invalid sudo reply')
                process.stdin.write((password + '\n').encode()); process.stdin.flush()
                del password, reply
            parser = SudoOutput(marker, prompt)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                for key, _ in selector.select(.25):
                    if key.data == 'input':
                        # Outside a password prompt, EOF/cancel is the only
                        # permitted input. Stop the remote subprocess as well.
                        raise RuntimeError('Setup connection closed')
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if chunk:
                        parser.feed(chunk)
                    else:
                        parser.feed(b'', final=True)
                        return process.wait(timeout=10)
            raise RuntimeError('Installer timed out; check the host service before retrying')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        process.stdin.close(); process.stdout.close()


def main():
    if sys.version_info < (3, 9) or sys.platform not in ('linux', 'darwin'):
        raise RuntimeError('Python 3.9+ on Linux or macOS is required')
    stream = sys.stdin.buffer
    config = json.loads(stream.readline(65536))
    archive, folder = receive(config, stream)
    if folder:
        command = [sys.executable, '-I', '-S', '-u', str(folder / config['installer']), '--reader', config['reader']]
        if config['kind'] == 'resources':
            command += ['--interval', str(config['interval'])]
        output('Install command: ' + shlex.join(['sudo', *command]) + '\n')
        if config['action'] == 'install':
            output('Installing the boot service…\n')
            code = run_installer(command, stream)
            if code:
                emit('result', ok=False, message='Installer exited with status ' + str(code) + '. Review the output before retrying.')
                return
    emit('result', ok=True, message={'push': 'Bundle uploaded.', 'extract': 'Bundle uploaded and extracted.',
                                   'install': 'Collector installed. Harbour will discover it on the next resource check.'}[config['action']],
         archive=str(archive), directory=str(folder) if folder else None)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('result', ok=False, message=str(exc))
        sys.exit(1)
