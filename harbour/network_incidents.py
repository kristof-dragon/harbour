"""Authenticated incident reports and bounded recurrence exploration."""
import hashlib
import json
import sqlite3
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from . import incident_brief, incidents, network, store
from .auth import authenticated

router = APIRouter(prefix='/api/servers/{id_}/network/incidents')


def report(server, event, full=False):
    # Never freeze an open window: delayed writes and sealing change its evidence.
    cacheable = bool(event['complete'] and event['archive'])
    digest = hashlib.sha256(json.dumps([event['archive'], event.get('trigger'),
        event['id'], event['label'], event['measured'], event['automatic'], server['name']]).encode()).hexdigest() if cacheable else ''
    if cacheable and not full:
        cached = store.one('SELECT report FROM network_analysis WHERE event_id=? AND version=? AND digest=?',
                           (event['id'], incidents.VERSION, digest))
        if cached:
            return json.loads(cached['report'])
    result = incidents.analyze(network.incident_bundle(server, event))
    if cacheable:
        # An analysis racing server deletion/pruning must not resurrect history.
        try:
            store.execute('''INSERT INTO network_analysis(event_id,version,digest,report)
              SELECT id,?,?,? FROM network_events WHERE id=? AND archive=? AND complete=1
              ON CONFLICT(event_id) DO UPDATE SET version=excluded.version,digest=excluded.digest,report=excluded.report''',
                          (incidents.VERSION, digest, json.dumps(incidents.compact(result), allow_nan=False), event['id'], event['archive']))
        except sqlite3.IntegrityError:
            pass
    return result if full else incidents.compact(result)


def attachment(data, filename):
    return Response(json.dumps(data, allow_nan=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('')
def collection(id_: str, limit: int = Query(default=200, ge=1, le=200),
               offset: int = Query(default=0, ge=0, le=1000000),
               kind: Literal['all', 'automatic', 'manual'] = 'all',
               since: Optional[float] = Query(default=None, ge=0, allow_inf_nan=False),
               until: Optional[float] = Query(default=None, ge=0, allow_inf_nan=False),
               download: bool = False, user=Depends(authenticated)):
    server = network.host(id_)
    if since is not None and until is not None and since > until:
        raise HTTPException(422, 'Start time must not follow end time')
    clauses, values = ['server_id=?'], [id_]
    if kind != 'all':
        clauses.append('automatic=?')
        values.append(int(kind == 'automatic'))
    if since is not None:
        clauses.append('measured>=?')
        values.append(since)
    if until is not None:
        clauses.append('measured<=?')
        values.append(until)
    where = ' AND '.join(clauses)
    total = store.one('SELECT count(*) AS n FROM network_events WHERE '+where, values)['n']
    # Load one archive at a time: 200 large windows must not all live in RAM.
    events = store.rows('SELECT id FROM network_events WHERE '+where+' ORDER BY measured DESC,id DESC LIMIT ? OFFSET ?',
                        (*values, limit, offset))
    reports, errors = [], []
    for identity in events:
        event = store.one('SELECT * FROM network_events WHERE id=? AND server_id=?', (identity['id'], id_))
        if event is None:
            continue
        try:
            reports.append(report(server, event))
        except (ValueError, TypeError, KeyError, RecursionError):
            errors.append({'id': event['id'], 'message': 'Stored evidence could not be parsed. Download the original incident to inspect it.'})
    result = {'analysis_schema': 1, 'analyzer_version': incidents.VERSION, 'generated_at': time.time(),
              'reports': reports, 'patterns': incidents.patterns(reports), 'errors': errors,
              'total': total, 'offset': offset, 'limit': limit, 'has_more': offset+len(events) < total,
              'filters': {'kind': kind, 'since': since, 'until': until}}
    return attachment(result, 'harbour-incident-patterns.json') if download else result


@router.get('/{event_id}')
def detail(id_: str, event_id: str, download: bool = False, user=Depends(authenticated)):
    server = network.host(id_)
    event = store.one('SELECT * FROM network_events WHERE id=? AND server_id=?', (event_id, id_))
    if not event:
        raise HTTPException(404, 'Incident not found')
    try:
        result = report(server, event, full=True)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise HTTPException(422, 'Stored evidence could not be parsed; download the original incident')
    return attachment(result, 'harbour-incident-analysis.json') if download else result


@router.get('/{event_id}/brief')
def brief(id_: str, event_id: str, format: Literal['markdown', 'json'] = 'markdown',
          examples: int = Query(default=3, ge=1, le=6), include_review: bool = True,
          download: bool = False, user=Depends(authenticated)):
    server = network.host(id_)
    event = store.one('SELECT * FROM network_events WHERE id=? AND server_id=?', (event_id, id_))
    if not event:
        raise HTTPException(404, 'Incident not found')
    if examples not in (1, 3, 6):
        raise HTTPException(422, 'Choose 1, 3 or 6 evidence examples')
    try:
        result = incident_brief.export(network.incident_bundle(server, event), format, examples, include_review)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise HTTPException(422, 'Stored evidence could not be parsed; download the original incident')
    if download:
        return Response(result['content'], media_type=result['media_type'],
                        headers={'Content-Disposition': f'attachment; filename="{result["filename"]}"'})
    return result
