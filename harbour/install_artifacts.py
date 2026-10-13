"""Local, indefinitely retained setup evidence, independent of host lifetimes."""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from . import __version__, auth, store

router = APIRouter()
MAX_OUTPUT = 1_000_000
METADATA = 'sequence,id,target_id,target_name,host,port,ssh_user,fingerprint,actor,kind,action,version,created,finished,status,bundle_sha256'


def initialize(con):
    # No foreign keys to hosts/users and no retention job: off-boarding must not
    # erase an installation's audit trail. Chunks commit before they are streamed.
    con.executescript('''
        CREATE TABLE IF NOT EXISTS install_artifacts (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
          target_id TEXT, target_name TEXT, host TEXT, port INTEGER, ssh_user TEXT,
          fingerprint TEXT, actor TEXT, kind TEXT, action TEXT, version TEXT, created REAL,
          finished REAL, status TEXT NOT NULL, bundle_sha256 TEXT,
          output_bytes INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS install_output (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          artifact_id TEXT NOT NULL REFERENCES install_artifacts(id),
          created REAL NOT NULL, text TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS install_output_artifact ON install_output(artifact_id,sequence);
    ''')


def recover():
    """Called once at process startup, before any new setup workers exist."""
    with store.db() as con:
        now = time.time()
        con.execute('''INSERT INTO install_output(artifact_id,created,text)
                       SELECT id,?,? FROM install_artifacts WHERE status='running' ''',
                    (now, '\nHarbour restarted before a final result was recorded. Remote state is unconfirmed.\n'))
        con.execute("UPDATE install_artifacts SET status='interrupted',finished=? WHERE status='running'", (now,))


class Redactor:
    """Remove known deployment secrets even when stdout splits them across frames."""
    def __init__(self):
        self.secrets = set()
        self.pending = ''

    def add(self, value):
        if isinstance(value, str) and value:
            self.secrets.add(value)

    def feed(self, text, final=False):
        text = self.pending + text
        self.pending = ''
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, '[REDACTED]')
        # Withhold only a possible secret prefix, not an entire output frame.
        keep = max((n for secret in self.secrets for n in range(1, min(len(secret), len(text) + 1))
                    if text.endswith(secret[:n])), default=0)
        if keep:
            self.pending, text = text[-keep:], text[:-keep]
        if final and self.pending:
            text += '[REDACTED]'
            self.pending = ''
        return text

    def clear(self):
        self.secrets.clear()
        self.pending = ''


def create(op, actor):
    server = op.server
    store.execute('''INSERT INTO install_artifacts
        (id,target_id,target_name,host,port,ssh_user,fingerprint,actor,kind,action,version,created,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'running')''',
        (op.id, server.get('id'), server.get('name'), server.get('host'), server.get('port'),
         server.get('username'), server.get('fingerprint'), actor, op.kind, op.action, __version__, time.time()))


def append(id_, text):
    if not text:
        return
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT output_bytes,status FROM install_artifacts WHERE id=?', (id_,)).fetchone()
        if row is None or row['status'] != 'running':
            raise RuntimeError('Install artifact is not writable')
        size = len(text.encode('utf-8'))
        if row['output_bytes'] + size > MAX_OUTPUT:
            raise RuntimeError('Setup output exceeded its limit; the preceding output is retained')
        con.execute('INSERT INTO install_output(artifact_id,created,text) VALUES (?,?,?)', (id_, time.time(), text))
        con.execute('UPDATE install_artifacts SET output_bytes=output_bytes+? WHERE id=?', (size, id_))


def finish(id_, status, message):
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        now = time.time()
        changed = con.execute("UPDATE install_artifacts SET status=?,finished=? WHERE id=? AND status='running'",
                              (status, now, id_)).rowcount
        if changed:
            con.execute('INSERT INTO install_output(artifact_id,created,text) VALUES (?,?,?)',
                        (id_, now, '\n' + message[:4000] + '\n'))


@router.get('/api/install-artifacts')
def listing(before: int | None = Query(None, ge=1), user=Depends(auth.admin)):
    rows = store.rows('SELECT ' + METADATA + ' FROM install_artifacts ' +
                      ('WHERE sequence<? ' if before else '') + 'ORDER BY sequence DESC LIMIT 51',
                      (before,) if before else ())
    return {'artifacts': rows[:50], 'next': rows[49]['sequence'] if len(rows) > 50 else None}


def read(id_):
    with store.db() as con:
        con.execute('BEGIN')
        artifact = con.execute('SELECT ' + METADATA + ' FROM install_artifacts WHERE id=?', (id_,)).fetchone()
        if not artifact:
            raise HTTPException(404, 'Install artifact not found')
        return {**dict(artifact), 'output': ''.join(row['text'] for row in con.execute(
            'SELECT text FROM install_output WHERE artifact_id=? ORDER BY sequence', (id_,)))}


@router.get('/api/install-artifacts/{id_}')
def detail(id_: str, user=Depends(auth.admin)):
    return read(id_)


@router.get('/api/install-artifacts/{id_}/download')
def download(id_: str, user=Depends(auth.admin)):
    artifact = read(id_)
    stamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat() if value else 'In progress'
    header = (f"Harbour Install artifact: {artifact['id']}\n"
              f"Target: {artifact['target_name']} ({artifact['host']}:{artifact['port']})\n"
              f"SSH account: {artifact['ssh_user']}\nHost fingerprint: {artifact['fingerprint'] or 'Not recorded'}\n"
              f"Administrator: {artifact['actor']}\n"
              f"Setup: {artifact['kind']} / {artifact['action']}\nHarbour: {artifact['version']}\n"
              f"Bundle SHA256: {artifact['bundle_sha256'] or 'Not sent'}\n"
              f"Started (UTC): {stamp(artifact['created'])}\nFinished (UTC): {stamp(artifact['finished'])}\n"
              f"Outcome: {artifact['status']}\n\n--- Captured setup output ---\n")
    # Database-generated sequence keeps filenames independent of target/user input.
    return Response(header + artifact['output'], media_type='text/plain', headers={
        'Content-Disposition': f'attachment; filename="harbour-install-{artifact["sequence"]}.txt"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
