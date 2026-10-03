"""Per-mount monitoring preferences shared by live readings and history."""
import json


def preferences(server):
    return json.loads(server.get('volume_settings') or '{}')


def options(server, mount):
    return {'monitor': True, 'warn': True, 'card': mount == '/', **preferences(server).get(mount, {})}


def decorate(server, disks, thresholds):
    result = []
    for disk in disks:
        config = options(server, disk['mount'])
        warning = config['monitor'] and config['warn'] and (disk['percent'] >= thresholds['disk'] or disk['free']/1e9 <= thresholds['disk_free_gb'])
        result.append({**disk, **config, 'warning': bool(warning), 'present': True})
    for mount, config in preferences(server).items():
        if not any(d['mount'] == mount for d in result):
            result.append({'mount': mount, **options(server, mount), 'present': False, 'warning': False})
    return result


def capacity(value):
    for unit, scale in [('TB', 1e12), ('GB', 1e9), ('MB', 1e6), ('KB', 1e3)]:
        if value >= scale:
            return f'{value / scale:.1f} {unit}'
    return f'{value:g} B'
