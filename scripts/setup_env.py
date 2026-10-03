#!/usr/bin/env python3
"""Create a private, first-run .env without third-party dependencies."""
import getpass
import ipaddress
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harbour.passwords import password_hash


def username(value):
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", value):
        raise ValueError("Use 1–80 letters, digits, dots, underscores, @ or hyphens.")
    return value


def origin(value, https=True):
    parsed = urlsplit(value)
    if (parsed.scheme != ("https" if https else "http") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)
            or not re.fullmatch(r"[A-Za-z0-9.:/\[\]_-]+", value)):
        raise ValueError("Enter an exact %s origin, with no path, query or credentials." % ("HTTPS" if https else "HTTP"))
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return value.rstrip("/")


def proxies(value):
    networks = [ipaddress.ip_network(v.strip(), strict=False) for v in value.split(",") if v.strip()]
    if not networks or any(n.prefixlen == 0 for n in networks):
        raise ValueError("Use the proxy's exact IP or dedicated CIDR; do not trust all addresses.")
    return ",".join(str(n) for n in networks)


def network_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Enter an existing Docker network name using letters, digits, _, . or -.")
    return value


def bind_address(value):
    address = ipaddress.IPv4Address(value)
    if address.is_unspecified or address.is_multicast:
        raise ValueError("Use loopback or this machine's specific internal IPv4 address.")
    return str(address)


def port(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return str(number)


def deployment_mode(value):
    if value not in ("1", "2", "3"):
        raise ValueError("Choose 1, 2 or 3.")
    return value


def ask(label, default=None, validate=lambda v: v):
    while True:
        value = input(label + (f" [{default}]" if default is not None else "") + ": ").strip()
        try:
            return validate(value or default or "")
        except ValueError as error:
            print(error)


def admin_password():
    while True:
        value = getpass.getpass("First admin password (at least 12 characters; use a password manager): ")
        if len(value) < 12 or len(value) > 1024:
            print("Use between 12 and 1,024 characters.")
        elif getpass.getpass("Confirm password: ") != value:
            print("Passwords did not match. Try again.")
        else:
            return password_hash(value)


def write_env(path, values):
    # O_EXCL also refuses symlinks and protects an existing encryption secret.
    text = "# Private Harbour configuration. Keep this file out of source control.\n"
    text += "# Bootstrap values are removed by scripts/start.py after verified startup.\n"
    text += "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def main():
    path = ROOT / ".env"
    if path.exists() or path.is_symlink():
        raise RuntimeError(".env already exists. Edit its non-secret settings directly; never regenerate HARBOUR_SECRET for an existing database.")
    print("Harbour setup — secrets stay on this machine. Nothing is sent to GitHub.")
    print("1. Nginx Proxy Manager in Docker on this host (recommended)")
    print("2. HTTPS reverse proxy on the host or another internal machine")
    print("3. Local HTTP only, for an initial trial")
    mode = ask("Deployment", "1", deployment_mode)
    values = {"COMPOSE_PROJECT_NAME": "harbour", "FIRST_RUN": "True"}
    values["HARBOUR_ADMIN"] = ask("First admin username", "admin", username)
    values["HARBOUR_ADMIN_PASSWORD_HASH"] = admin_password()
    values["HARBOUR_BOOTSTRAP_ID"] = secrets.token_hex(16)
    values["HARBOUR_SECRET"] = secrets.token_urlsafe(48)
    values["HARBOUR_PORT"] = ask("Host port", "8080", port)
    values["HARBOUR_BIND_ADDRESS"] = "127.0.0.1"
    if mode == "3":
        values["HARBOUR_ORIGIN"] = "http://localhost:" + values["HARBOUR_PORT"]
        values["HARBOUR_SECURE_COOKIE"] = "false"
        values["HARBOUR_TRUSTED_PROXIES"] = ""
    else:
        values["HARBOUR_ORIGIN"] = ask("Public-facing internal HTTPS origin (e.g. https://harbour.home.example)", validate=origin)
        values["HARBOUR_SECURE_COOKIE"] = "true"
        print("Use NPM's IP as seen by Harbour; preferably a fixed IP on a dedicated proxy network.")
        values["HARBOUR_TRUSTED_PROXIES"] = ask("Trusted proxy IP(s) or dedicated CIDR(s), comma-separated", validate=proxies)
        if mode == "1":
            values["HARBOUR_PROXY_NETWORK"] = ask("Existing dedicated Docker network already joined by NPM", validate=network_name)
            values["COMPOSE_FILE"] = "compose.yaml:compose.npm.yaml"
        else:
            values["HARBOUR_BIND_ADDRESS"] = ask("Listen IPv4 (loopback for host proxy; local LAN IP for remote proxy)", "127.0.0.1", bind_address)
    write_env(path, values)
    print("Created .env with owner-only permissions (600). It contains a salted scrypt password hash, never the plaintext password.")
    print("Save your chosen password in your password manager. Keep a separate secure backup of HARBOUR_SECRET.")
    if mode == "1":
        print("In NPM: forward HTTP to harbour:8080 on the chosen network; enable HTTPS and forward X-Forwarded-For.")
    print("Next: python3 scripts/start.py")
    print("This builds/starts Docker, verifies the seeded admin, removes bootstrap fields, sets FIRST_RUN=False, and recreates the container without the bootstrap hash.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, EOFError, KeyboardInterrupt) as error:
        print("Setup stopped:", str(error) or "cancelled", file=sys.stderr)
        sys.exit(1)
