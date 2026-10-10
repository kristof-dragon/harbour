"""Durable, incremental gzip archives, sealed once per UTC day.

Only the recorder's archive process writes these files. SQLite checkpoints
the fsynced file length together with the archived flags. After an interrupted
append, retry truncates the uncommitted tail before writing it again. Multiple
gzip members form one standard gzip stream; readers need only gzip + JSONL.
"""
import datetime
import gzip
import json
import logging
import os
import re
import time
import uuid

from . import store

BATCH_SIZE = 500
MAX_BATCHES = 40
FILE_NAME = re.compile(r'network-\d{4}-\d{2}-\d{2}-[a-f0-9]{32}\.jsonl\.gz(?:\.part)?$')


def initialize():
    with store.db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS network_archives (
          id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
          day INTEGER NOT NULL, size INTEGER NOT NULL DEFAULT 0,
          sample_count INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
          updated REAL NOT NULL, UNIQUE(server_id,day));
        CREATE TABLE IF NOT EXISTS network_archive_status (
          server_id TEXT PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
          updated REAL NOT NULL, error TEXT);
        ''')


def root():
    return store.DATA / 'network-archives'


def path(row, partial=False):
    if not re.fullmatch('[a-f0-9]{32}', row['id']):
        raise ValueError('Invalid archive identity')
    day = datetime.datetime.fromtimestamp(row['day'], datetime.timezone.utc).date()
    return root() / f"network-{day}-{row['id']}.jsonl.gz{'.part' if partial else ''}"


def sync_directory():
    fd = os.open(root(), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def line(stream, value):
    stream.write((json.dumps(value, allow_nan=False, separators=(',', ':'))+'\n').encode())


def append(row, readings, events, settings, server, now):
    from . import network
    root().mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = path(row, partial=True)
    fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+b') as raw:
        if raw.seek(0, os.SEEK_END) < row['size']:
            raise OSError('Archive is shorter than its committed checkpoint')
        raw.truncate(row['size'])
        raw.seek(row['size'])
        with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6, mtime=0, filename='') as compressed:
            if row['size'] == 0:
                line(compressed, {'type': 'header', 'schema': 1, 'archive_id': row['id'],
                     'server_name': server['name'], 'day_start_utc': row['day'],
                     'day_end_utc': row['day']+86400, 'day_basis': 'archive receipt time',
                     'scope': 'Recorder-to-target ICMP and router telemetry; not FaceTime media measurements',
                     'note': 'Original measurement times are preserved; delayed/backfilled readings may precede this day.'})
            samples = [{'id': r['id'], 'at': r['measured'], 'kind': r['kind'], 'target': r['target'],
                        **json.loads(r['payload'])} for r in readings]
            line(compressed, {'type': 'context', 'archived_at': now, 'settings': settings,
                 'device': json.loads(server['snapshot']).get('metrics', {}).get('openwrt', {}).get('board'),
                 'runs': network.run_metadata(server['id'], samples)})
            for ordinal, sample in enumerate(samples, row['sample_count']+1):
                # SQLite can reuse raw row IDs after pruning an empty table.
                line(compressed, {'type': 'sample', **sample, 'archive_sequence': ordinal})
            for event in events:
                line(compressed, {'type': 'incident', **event})
        raw.flush()
        os.fsync(raw.fileno())
        size = raw.tell()
    sync_directory()
    # A crash before this transaction commits leaves only an uncommitted tail.
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        result = con.execute('''UPDATE network_archives SET size=?,sample_count=sample_count+?,updated=?
          WHERE id=? AND size=? AND complete=0''', (size, len(readings), now, row['id'], row['size']))
        if result.rowcount != 1:
            raise RuntimeError('Archive checkpoint changed or server was removed')
        con.executemany('UPDATE network_samples SET archived=1 WHERE id=?', [(r['id'],) for r in readings])
        con.executemany('UPDATE network_events SET archived_version=? WHERE id=?', [(e['complete'], e['id']) for e in events])


def seal(row):
    partial, destination = path(row, True), path(row)
    if row['size'] == 0:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        store.execute('DELETE FROM network_archives WHERE id=?', (row['id'],))
        return
    if partial.exists():
        with partial.open('r+b') as stream:
            if stream.seek(0, os.SEEK_END) < row['size']:
                raise OSError('Archive is shorter than its committed checkpoint')
            stream.truncate(row['size'])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, destination)
        sync_directory()
    # Handles a crash after rename but before the completion transaction.
    if not destination.is_file() or destination.stat().st_size != row['size']:
        raise OSError('Daily archive file is missing or incomplete')
    store.execute('UPDATE network_archives SET complete=1 WHERE id=?', (row['id'],))


def trim(id_, settings, today):
    kept = 0
    for row in store.rows('SELECT * FROM network_archives WHERE server_id=? AND complete=1 ORDER BY day DESC', (id_,)):
        kept += row['size']
        expired = row['day'] < today-settings['archive_retention_days']*86400
        oversized = settings['archive_max_mib'] and kept > settings['archive_max_mib']*1024*1024
        if expired or oversized:
            path(row).unlink(missing_ok=True)
            store.execute('DELETE FROM network_archives WHERE id=?', (row['id'],))


def maintain(id_, settings, stop=None):
    """Archive before retention; leave pending rows protected on any failure."""
    now = time.time()
    today = int(now//86400)*86400
    try:
        # Work continues for paused routers, so their last daily file is sealed.
        for row in store.rows('SELECT * FROM network_archives WHERE server_id=? AND complete=0 AND day<?', (id_, today)):
            seal(row)
        server = store.one('SELECT id,name,snapshot FROM servers WHERE id=?', (id_,))
        if not server:
            return
        if settings['archive_enabled']:
            for _ in range(MAX_BATCHES):
                if stop and stop.is_set():
                    break
                readings = store.rows('''SELECT id,measured,kind,target,payload FROM network_samples
                  WHERE server_id=? AND archived=0 ORDER BY id LIMIT ?''', (id_, BATCH_SIZE))
                events = store.rows('''SELECT id,measured,label,automatic,complete FROM network_events
                  WHERE server_id=? AND archived_version!=complete ORDER BY measured,id LIMIT ?''', (id_, BATCH_SIZE))
                if not readings and not events:
                    break
                row = store.one('SELECT * FROM network_archives WHERE server_id=? AND day=?', (id_, today))
                if not row:
                    store.execute('INSERT INTO network_archives(id,server_id,day,updated) VALUES (?,?,?,?)',
                                  (uuid.uuid4().hex, id_, today, now))
                    row = store.one('SELECT * FROM network_archives WHERE server_id=? AND day=?', (id_, today))
                append(row, readings, events, settings, server, now)
        trim(id_, settings, today)
        store.execute('INSERT OR REPLACE INTO network_archive_status VALUES (?,?,NULL)', (id_, now))
    except Exception:
        logging.exception('Network daily archiving failed')
        # Avoid leaking OS paths or arbitrary exception content into the UI.
        if store.one('SELECT id FROM servers WHERE id=?', (id_,)):
            message = 'Daily archiving failed. Check recorder logs and available disk space.'
            if settings['archive_enabled']:
                message += ' Unarchived readings are protected from cleanup.'
            store.execute('INSERT OR REPLACE INTO network_archive_status VALUES (?,?,?)',
                          (id_, now, message))


def cleanup_removed():
    """Remove only managed archive files whose server/record has been deleted."""
    if not root().is_dir():
        return
    known = set()
    for row in store.rows('SELECT id,day FROM network_archives'):
        known.update((path(row).name, path(row, True).name))
    for file in root().iterdir():
        if FILE_NAME.fullmatch(file.name) and file.name not in known:
            file.unlink(missing_ok=True)


def summary(id_):
    rows = store.rows('SELECT * FROM network_archives WHERE server_id=? ORDER BY day DESC LIMIT 100', (id_,))
    totals = store.one('SELECT count(*) AS files,coalesce(sum(size),0) AS bytes FROM network_archives WHERE server_id=?', (id_,))
    status = store.one('SELECT updated,error FROM network_archive_status WHERE server_id=?', (id_,)) or {}
    return {'directory': str(root()), 'files': [{**r, 'filename': path(r).name} for r in rows],
            'file_count': totals['files'], 'bytes': totals['bytes'], **status}
