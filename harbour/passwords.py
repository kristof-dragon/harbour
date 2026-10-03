"""Password storage shared by the app and dependency-free setup wizard."""
import hashlib
import hmac
import re
import secrets


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ":" + value


def valid_password_hash(value):
    return bool(re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{128}", value))


def password_ok(password, stored):
    return valid_password_hash(stored) and hmac.compare_digest(password_hash(password, stored.split(":")[0]), stored)
