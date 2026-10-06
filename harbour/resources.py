"""Discovered resource identities, per-host preferences and warning evaluation."""
import hashlib
import json
import math

from . import remote_probe


def preferences(server):
    return json.loads(server.get('resource_settings') or '{}')


def hardware_id(sensor):
    identity = json.dumps([sensor['id'], sensor['unit'], sensor['source']], ensure_ascii=False, separators=(',', ':'))
    return 'resource:' + hashlib.sha256(identity.encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def discover(metrics):
    if not metrics:
        return []
    result = [dict(id='cpu', label='CPU', unit='%', source='Host CPU', group='cpu', value=metrics.get('cpu')),
              dict(id='memory', label='Memory', unit='%', source='Host memory', group='memory',
                   value=(metrics.get('memory') or {}).get('percent'))]
    for key, minutes in [('load1', 1), ('load5', 5), ('load15', 15)]:
        if key in metrics:
            result.append(dict(id='resource:' + key, label=f'Load · {minutes} min', unit='',
                               source='System load', group='load', metric=key, value=metrics[key]))
    for sensor in metrics.get('temperature', {}).get('sensors', []):
        result.append(dict(id='temperature:' + sensor['id'], sensor_id=sensor['id'], label=sensor['label'],
                           unit='°C', source='Temperature sensor', group='temperature',
                           kind=remote_probe.temperature_kind(sensor), value=sensor['celsius']))
    for sensor in metrics.get('hardware', []):
        result.append(dict(id=hardware_id(sensor), sensor_id=sensor['id'], label=sensor['label'],
                           unit=sensor['unit'], source=sensor['source'], group='hardware', value=sensor['value']))
    return result


def catalog(server, metrics=None):
    known = json.loads(server.get('resource_catalog') or '{}')
    previous = json.loads(server.get('snapshot') or '{}').get('metrics', {})
    discovered = discover(previous)
    if metrics is not None:
        discovered.extend(discover(metrics))
    for resource in discovered:
        known[resource['id']] = {k: v for k, v in resource.items() if k != 'value'}
    return known


def options(server, resource, thresholds, settings=None):
    settings = preferences(server) if settings is None else settings
    group = resource['group']
    legacy_warn = group in ('cpu', 'memory') or group == 'temperature' and resource.get('kind') != 'cpu_auxiliary'
    default_high = thresholds.get(group) if group in ('cpu', 'memory', 'temperature') else None
    result = {'monitor': True, 'warn': legacy_warn, 'card': True, 'limit_mode': 'default',
              'low': None, 'high': default_high, **settings.get(resource['id'], {})}
    if result['limit_mode'] == 'default':
        result.update(low=None, high=default_high)
    return result


def decorate(server, metrics, thresholds):
    current = {r['id']: r for r in discover(metrics)}
    rows, settings = [], preferences(server)
    for identity, metadata in catalog(server, metrics).items():
        resource = {**metadata, 'value': current.get(identity, {}).get('value')}
        config = options(server, resource, thresholds, settings)
        value = resource['value']
        present = finite(value)
        if not present:
            resource['value'] = None
        low = present and config['low'] is not None and value <= config['low']
        high = present and config['high'] is not None and value >= config['high']
        rows.append({**resource, **config, 'present': present,
                     'warning': bool(config['monitor'] and config['warn'] and (low or high)),
                     'limit_side': 'low' if low else 'high' if high else None})
    return rows


def warnings(rows):
    result = []
    for row in rows:
        if not row['warning']:
            continue
        side = row['limit_side']
        title = ('Temperature · ' if row['group'] == 'temperature' else '') + row['label']
        if row['group'] in ('cpu', 'memory') and side == 'high':
            title += ' usage is high'
        detail = f"{row['value']:g} {row['unit']} · {'at or below' if side == 'low' else 'at or above'} {row[side]:g} {row['unit']}"
        result.append({'id': row['id'], 'title': title, 'detail': detail,
                       **({'kind': row['kind']} if row['group'] == 'temperature' else {})})
    return result


def unavailable(server, metrics, thresholds):
    return {r['id'] for r in decorate(server, metrics, thresholds)
            if r['monitor'] and r['warn'] and not r['present']}


def filter_history(server, metrics):
    # Discovery stays active even when recording is disabled. Preserve raw
    # snapshots and CPU baselines; only exclude future samples from history.
    config = preferences(server)
    enabled = lambda identity: config.get(identity, {}).get('monitor', True)
    result = {**metrics}
    if not enabled('cpu'):
        result['cpu'] = None
    if not enabled('memory'):
        result['memory'] = None
    for key in ('load1', 'load5', 'load15'):
        if not enabled('resource:' + key):
            result[key] = None
    result['temperature'] = remote_probe.temperature_summary([
        s for s in metrics.get('temperature', {}).get('sensors', []) if enabled('temperature:' + s['id'])])
    result['hardware'] = [s for s in metrics.get('hardware', []) if enabled(hardware_id(s))]
    return result
