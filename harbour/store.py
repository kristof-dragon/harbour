"""Small, durable SQLite store. Every connection is scoped to one operation."""
import base64
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from .passwords import password_hash, password_ok, valid_password_hash

DATA = Path(os.environ.get("HARBOUR_DATA", "data"))
DEMO = os.environ.get("HARBOUR_DEMO") == "true"
DEFAULTS = {"cpu": 85, "memory": 85, "disk": 85, "disk_free_gb": 10, "temperature": 80}
SECURITY_DEFAULTS = {"idle_minutes": 30, "absolute_hours": 12, "bind_ip": True,
                     "bind_browser": True, "ban_minutes": 15}
MONITORING_DEFAULTS = {"poll_seconds": 60, "update_check_hours": 6, "retention_days": 90,
                       "week1_minutes": 1, "week2_minutes": 5, "weeks3_4_minutes": 15,
                       "older_minutes": 60}


@contextmanager
def db():
    con = sqlite3.connect(DATA / "harbour.db", timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def rows(sql, args=()):
    with db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(sql, args=()):
    results = rows(sql, args)
    return results[0] if results else None


def execute(sql, args=()):
    with db() as con:
        return con.execute(sql, args).lastrowid


def cipher():
    secret = os.environ.get("HARBOUR_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("HARBOUR_SECRET must contain at least 32 characters.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def initialize():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    cipher()
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('admin','user')));
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
          csrf TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ssh_keys (
          id TEXT PRIMARY KEY, encrypted TEXT NOT NULL, public TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS servers (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL,
          username TEXT NOT NULL, fingerprint TEXT NOT NULL, key_id TEXT REFERENCES ssh_keys(id),
          thresholds TEXT, snapshot TEXT NOT NULL DEFAULT '{}', error TEXT,
          checked REAL, update_checked REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dismissals (
          user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
          server_id TEXT REFERENCES servers(id) ON DELETE CASCADE,
          service_id TEXT, digest TEXT,
          PRIMARY KEY (user_id,server_id,service_id,digest));
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, server_id TEXT, server_name TEXT, actor TEXT,
          action TEXT, status TEXT, targets TEXT, output TEXT DEFAULT '',
          created REAL, finished REAL);
        CREATE TABLE IF NOT EXISTS login_attempts (address TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS auth_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, address TEXT NOT NULL,
          created REAL NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS auth_log_address_time ON auth_log(address,created);
        CREATE TABLE IF NOT EXISTS ip_bans (address TEXT PRIMARY KEY, until REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS mfa_pending (
          user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          secret TEXT NOT NULL, expires REAL NOT NULL, session_token TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recovery_codes (
          user_id TEXT REFERENCES users(id) ON DELETE CASCADE, hash TEXT NOT NULL,
          PRIMARY KEY(user_id,hash));
        CREATE TABLE IF NOT EXISTS resource_history (
          server_id TEXT REFERENCES servers(id) ON DELETE CASCADE, bucket INTEGER NOT NULL,
          resolution INTEGER NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(server_id,bucket,resolution));
        CREATE INDEX IF NOT EXISTS history_time ON resource_history(bucket);
        CREATE TABLE IF NOT EXISTS notification_rules (
          server_id TEXT REFERENCES servers(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
          delay_seconds INTEGER NOT NULL DEFAULT 300,
          repeat_seconds INTEGER NOT NULL DEFAULT 3600,
          PRIMARY KEY(server_id,kind));
        CREATE TABLE IF NOT EXISTS notification_state (
          server_id TEXT, kind TEXT, entities TEXT NOT NULL DEFAULT '{}',
          checked REAL NOT NULL DEFAULT 0, last_sent REAL,
          PRIMARY KEY(server_id,kind),
          FOREIGN KEY(server_id,kind) REFERENCES notification_rules(server_id,kind) ON DELETE CASCADE);
        """)
        def add_columns(table, columns):
            existing = {r[1] for r in con.execute("PRAGMA table_info(" + table + ")")}
            for name, definition in columns.items():
                if name not in existing:
                    con.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
            return existing
        add_columns("users", {"totp_secret": "TEXT", "totp_last": "INTEGER NOT NULL DEFAULT -1"})
        add_columns("jobs", {"progress": "TEXT NOT NULL DEFAULT '{}'", "target_names": "TEXT NOT NULL DEFAULT '[]'"})
        add_columns("servers", {"poll_seconds": "INTEGER", "monitoring_enabled": "INTEGER NOT NULL DEFAULT 1",
            "last_attempt": "REAL", "latency_ms": "REAL", "connection_status": "TEXT NOT NULL DEFAULT 'pending'",
            "auth_method": "TEXT NOT NULL DEFAULT 'key'", "password_encrypted": "TEXT", "server_type": "TEXT NOT NULL DEFAULT 'docker'",
            "volume_settings": "TEXT NOT NULL DEFAULT '{}'", "sort_order": "INTEGER NOT NULL DEFAULT 0"})
        old_sessions = add_columns("sessions", {"created": "REAL NOT NULL DEFAULT 0",
            "last_activity": "REAL NOT NULL DEFAULT 0", "address": "TEXT NOT NULL DEFAULT ''",
            "browser": "TEXT NOT NULL DEFAULT ''"})
        if "address" not in old_sessions:
            con.execute("DELETE FROM sessions")  # Older sessions have no binding; require a fresh sign-in.
        mode = "demo" if DEMO else "production"
        saved_mode = con.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
        if saved_mode and saved_mode[0] != mode:
            raise RuntimeError("This data directory belongs to " + saved_mode[0] + "; use a separate directory for " + mode + ".")
        con.execute("INSERT OR IGNORE INTO settings VALUES ('mode',?)", (mode,))
        con.execute("INSERT OR IGNORE INTO settings VALUES ('thresholds',?)", (json.dumps(DEFAULTS),))
        con.execute("INSERT OR IGNORE INTO settings VALUES ('security',?)", (json.dumps(SECURITY_DEFAULTS),))
        con.execute("INSERT OR IGNORE INTO settings VALUES ('monitoring',?)", (json.dumps(MONITORING_DEFAULTS),))
        if not con.execute("SELECT 1 FROM users").fetchone():
            first_run = os.environ.get("FIRST_RUN", "True").lower()
            if first_run != "true":
                raise RuntimeError("No accounts exist and FIRST_RUN is not True. Restore the data volume or run setup for a new installation.")
            encoded = os.environ.get("HARBOUR_ADMIN_PASSWORD_HASH", "")
            if encoded:
                if not valid_password_hash(encoded):
                    raise RuntimeError("HARBOUR_ADMIN_PASSWORD_HASH is not a valid salted scrypt hash.")
            else:
                # Compatibility for the isolated demo and existing deployments.
                password = os.environ.get("HARBOUR_PASSWORD", "")
                if len(password) < 12:
                    raise RuntimeError("Run scripts/setup_env.py to seed the first administrator, or set HARBOUR_PASSWORD to at least 12 characters.")
                encoded = password_hash(password)
            user_id = secrets.token_hex(12)
            name = os.environ.get("HARBOUR_ADMIN", "admin")
            if not name or len(name) > 80:
                raise RuntimeError("The first administrator needs a username of 1–80 characters.")
            con.execute("INSERT INTO users (id,name,password,role) VALUES (?,?,?,?)", (
                user_id, name, encoded, "admin"))
            bootstrap_id = os.environ.get("HARBOUR_BOOTSTRAP_ID", "")
            if bootstrap_id:
                con.execute("INSERT INTO settings VALUES ('bootstrap',?)", (json.dumps({
                    "id": bootstrap_id, "user_id": user_id, "name": name}),))
        con.execute("UPDATE jobs SET status='interrupted', output=output || '\nApplication restarted. Verify the server before retrying.' WHERE status IN ('queued','running')")
    os.chmod(DATA / "harbour.db", 0o600)
