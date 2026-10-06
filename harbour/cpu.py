"""CPU utilisation from consecutive, boot-scoped normalized host counters."""
import math


def valid(sample):
    if not isinstance(sample, dict) or not isinstance(sample.get('boot_id'), str) or not sample['boot_id']:
        return False
    values = sample.get('values')
    return (sample.get('source', 'linux') in ('linux', 'darwin')
            and isinstance(values, list) and len(values) == 8
            and all(type(value) is int and value >= 0 for value in values)
            and type(sample.get('cores')) is int and sample['cores'] > 0
            and type(sample.get('uptime')) in (int, float)
            and math.isfinite(sample['uptime']) and sample['uptime'] >= 0)


def usage(previous, current):
    """Return (percent, elapsed seconds), or a missing reading requiring a baseline."""
    if not valid(previous) or not valid(current):
        return None, None
    if (previous['boot_id'] != current['boot_id'] or previous['cores'] != current['cores']
            or previous.get('source', 'linux') != current.get('source', 'linux')):
        return None, None
    elapsed = current['uptime'] - previous['uptime']
    delta = [new - old for new, old in zip(current['values'], previous['values'])]
    total = sum(delta)
    # Counters can reset (and Linux iowait can decrease). Rebaseline instead
    # of clamping an invalid interval into a misleading usage percentage.
    if elapsed <= 0 or total <= 0 or any(value < 0 for value in delta):
        return None, None
    idle = delta[3] + delta[4]
    return round(100 * (total - idle) / total, 1), round(elapsed, 2)
