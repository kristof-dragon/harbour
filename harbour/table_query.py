"""Bounded, parameterized filtering and ordering for retained data tables."""
import json
import math
import re

from fastapi import HTTPException


def options(filters, columns, sort, order, page, size):
    if sort not in columns or order not in ('asc', 'desc') or page < 1 or size not in (25, 50, 100):
        raise HTTPException(422, 'Invalid table options')
    try:
        values = json.loads(filters)
        if not isinstance(values, dict) or len(values) > len(columns):
            raise ValueError()
        for key, value in values.items():
            if key not in columns:
                raise ValueError()
            if isinstance(value, list):
                if columns[key] != 'date' or len(value) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in value) or value[0] > value[1]:
                    raise ValueError()
            elif not isinstance(value, str) or len(value) > 128:
                raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Invalid column filters')
    return values


def select(con, source, args, columns, *, filters='{}', sort='time', order='desc', page=1, size=25):
    values = options(filters, columns, sort, order, page, size)
    clauses, params = [], list(args)
    for key, value in values.items():
        if value == '':
            continue
        if isinstance(value, list):
            clauses.append(f'"{key}">=? AND "{key}"<?'); params.extend(value)
        elif columns[key] == 'number':
            match = re.fullmatch(r'\s*(<=|>=|<|>|=)?\s*(-?\d+(?:\.\d+)?)\s*', value)
            if not match:
                raise HTTPException(422, 'Use a number, optionally with <, <=, > or >=')
            clauses.append(f'"{key}" {match[1] or "="} ?'); params.append(float(match[2]))
        else:
            clauses.append(f'instr(lower(coalesce("{key}",\'\')),lower(?))>0'); params.append(value)
    query = 'WITH records AS ('+source+') SELECT * FROM records'
    if clauses:
        query += ' WHERE '+' AND '.join('('+c+')' for c in clauses)
    total = con.execute('SELECT count(*) FROM ('+query+')', params).fetchone()[0]
    page = min(page, max(1, (total+size-1)//size))
    offset = (page-1)*size
    # Names and directions above are allowlisted, values always bound. NULLs stay last.
    ordered = query+f' ORDER BY "{sort}" IS NULL,"{sort}" COLLATE NOCASE {order},id {order} LIMIT ? OFFSET ?'
    rows = con.execute(ordered, (*params, size, offset)).fetchall()
    return rows, dict(page=page, size=size, total=total, pages=max(1, (total+size-1)//size))
