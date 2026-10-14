"""Cached size accounting for the data volume; no payload scans or writes."""
import os
import shutil
import stat
import time

from . import store

cached = None


def collect(force=False):
    global cached
    now = time.monotonic()
    if not force and cached and cached[0] == str(store.DATA) and now-cached[1] < 60:
        return cached[2]
    categories = {key: {'bytes': 0, 'allocated_bytes': 0, 'files': 0} for key in ('database','recordings','recovery','temporary','other')}
    complete = True
    def unreadable(_):
        nonlocal complete
        complete = False
    for directory, dirs, files in os.walk(store.DATA, followlinks=False, onerror=unreadable):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(directory,d))]
        for name in files:
            path = os.path.join(directory,name)
            try:
                s = os.stat(path, follow_symlinks=False)
                if not stat.S_ISREG(s.st_mode):
                    continue
                relative = os.path.relpath(path, store.DATA).split(os.sep)
                category = ('database' if len(relative)==1 and name in ('harbour.db','harbour.db-wal','harbour.db-shm')
                    else {'network-archives':'recordings','recovery':'recovery','sqlite-tmp':'temporary'}.get(relative[0],'other'))
                value = categories[category]
                value['bytes'] += s.st_size; value['allocated_bytes'] += s.st_blocks*512; value['files'] += 1
            except OSError:
                complete = False
    disk = shutil.disk_usage(store.DATA)
    reusable = None
    try:
        with store.db() as con:
            reusable = con.execute('PRAGMA freelist_count').fetchone()[0]*con.execute('PRAGMA page_size').fetchone()[0]
    except Exception:
        pass
    result = {'measured_at': time.time(), 'complete': complete, 'categories': categories,
              'bytes': sum(c['bytes'] for c in categories.values()),
              'allocated_bytes': sum(c['allocated_bytes'] for c in categories.values()),
              'reusable_database_bytes': reusable,
              'filesystem': {'total': disk.total, 'used': disk.used, 'free': disk.free},
              'pressure': 'critical' if disk.free < 1024**3 else 'warning' if disk.free < 5*1024**3 else 'normal',
              'scope': 'Harbour data volume, including recordings and recovery copies. Container images and external Docker logs are outside this total. Database reusable space is included, not extra.'}
    cached = (str(store.DATA), now, result)
    return result
