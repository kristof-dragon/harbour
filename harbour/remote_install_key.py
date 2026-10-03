"""Fixed helper sent over pinned SSH; runs as the selected SSH account."""
import fcntl
import os
from pathlib import Path
import re
import shlex
import stat


def install_public_key(public_key, home=None):
    if not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,2}", public_key):
        raise ValueError("Invalid public key")
    key_type, key_data = public_key.split()
    directory = Path(home) if home is not None else Path.home()
    home_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir('.ssh', 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        ssh_fd = os.open('.ssh', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
        try:
            if os.fstat(ssh_fd).st_uid != os.geteuid():
                raise ValueError("SSH directory must belong to this account")
            fd = os.open('authorized_keys', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=ssh_fd)
            with os.fdopen(fd, 'r+b') as output:
                info = os.fstat(output.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                    raise ValueError("Unsafe authorized_keys file")
                fcntl.flock(output.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                content = output.read(2_000_001)
                if len(content) > 2_000_000:
                    raise ValueError("authorized_keys is too large")
                os.fchmod(ssh_fd, 0o700)
                os.fchmod(output.fileno(), 0o600)
                for line in content.decode('utf-8', errors='replace').splitlines():
                    try:
                        fields = shlex.split(line, comments=True)
                    except ValueError:
                        continue
                    if fields[:2] == [key_type, key_data] or fields[1:3] == [key_type, key_data]:
                        return {'status': 'present'}
                output.seek(0, os.SEEK_END)
                output.write((b'\n' if content and not content.endswith(b'\n') else b'') +
                             ('restrict ' + public_key + ' harbour\n').encode())
                output.flush()
                os.fsync(output.fileno())
                return {'status': 'installed'}
        finally:
            os.close(ssh_fd)
    finally:
        os.close(home_fd)
