import copy
import json
import threading
import time

import pytest

from test_harbour import client
from harbour import app as module, history, store


def readings():
    return json.loads(store.one('SELECT snapshot FROM servers WHERE id="atlas"')['snapshot'])['metrics']


def test_load_survives_legacy_buckets_uneven_samples_and_all_retention_tiers(client):
    store.execute('DELETE FROM resource_history')
    metrics = readings()
    now = 1800000000
    stamp = (now - 9 * 86400) // 3600 * 3600
    legacy = {key: value for key, value in history.sample_payload(metrics, up=True).items()
              if not key.startswith('load')}
    with store.db() as con:
        history.put(con, 'atlas', stamp, 60, legacy)
    for offset, values in [(60, (0, 1, 10)), (70, (2, 3, 20)), (120, (4, 5, 30))]:
        history.record('atlas', {**metrics, **dict(zip(history.LOAD_KEYS, values))}, up=True, now=stamp + offset)
    history.record('atlas', up=False, now=stamp + 180)
    data = history.series('atlas', 1, now=stamp + 240, requested_resolution=60)
    assert all(data['points'][0][key] is None for key in history.LOAD_KEYS)
    for age in (0, 10, 30):
        later = now + age * 86400
        history.compact(now=later)
        point = history.series('atlas', 1000, now=later, requested_resolution=3600)['points'][0]
        assert [point[key] for key in history.LOAD_KEYS] == [2, 3, 20]
        assert [point[key + '_peak'] for key in history.LOAD_KEYS] == [4, 5, 30]
        assert point['samples'] == 4 and point['attempts'] == 5
        before = copy.deepcopy(point)
        history.compact(now=later)
        assert history.series('atlas', 1000, now=later, requested_resolution=3600)['points'][0] == before
    assert store.one('SELECT resolution FROM resource_history')['resolution'] == 3600


@pytest.mark.parametrize('invalid', [None, -1, float('nan'), float('inf'), 'unavailable'])
def test_missing_load_does_not_dilute_valid_zero_or_other_periods(client, invalid):
    sample = history.sample_payload({**readings(), 'load1': 0, 'load5': invalid, 'load15': .125}, up=True)
    merged = history.merge(sample, history.empty())
    assert merged['load1_n'] == 1 and merged['load1_sum'] == 0
    assert merged['load5_n'] == 0
    assert merged['load15_n'] == 1 and merged['load15_sum'] == .125


def test_resource_poll_exposes_and_records_load_without_warnings(client, monkeypatch):
    store.execute('DELETE FROM resource_history')
    metrics = {**readings(), 'load1': 1000.25, 'load5': 123.45, 'load15': 12.34}
    baseline = next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] == 'atlas')['warnings']
    monkeypatch.setattr(store, 'DEMO', False)
    def request(server, payload):
        assert payload == {'operation': 'resources'}
        return {'metrics': metrics, 'latency_ms': 2}
    monkeypatch.setattr(module.ssh, 'request', request)
    lock = threading.Lock()
    lock.acquire()
    module.poll_resources(module.get_server('atlas'), lock, raise_errors=True)
    server = next(s for s in client.get('/api/dashboard').json()['servers'] if s['id'] == 'atlas')
    assert server['warnings'] == baseline
    point = client.get('/api/servers/atlas/history?hours=1').json()['points'][0]
    for key in history.LOAD_KEYS:
        assert server['metrics'][key] == point[key] == point[key + '_peak'] == metrics[key]
    # A failed poll records a gap, not fabricated zero load.
    history.record('atlas', up=False, now=time.time() + 120)
    missing = history.series('atlas', 1, now=time.time() + 121)['points'][-1]
    assert all(missing[key] is None for key in history.LOAD_KEYS)
