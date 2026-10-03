import json

from test_harbour import client
from harbour import app as module, store


def dashboard(client):
    return client.get('/api/dashboard').json()['servers']


def test_dismiss_all_is_private_persistent_and_keeps_active_warnings(client):
    before = dashboard(client)
    expected = sum(len(s['warnings']) + s['updates'] for s in before)
    assert expected > 0 and any(s['warnings'] for s in before)
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-long', 'role': 'user'})
    result = client.post('/api/dismiss-all')
    assert result.status_code == 200 and result.json()['dismissed'] == expected
    store.initialize()
    after = dashboard(client)
    assert sum(len(s['warnings']) for s in after) == sum(len(s['warnings']) for s in before)
    assert all(w['dismissed'] for s in after for w in s['warnings'])
    assert all(s['updates'] == 0 for s in after)
    assert client.post('/api/dismiss-all').json()['dismissed'] == 0
    login = client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-long'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    reader = dashboard(client)
    assert not any(w['dismissed'] for s in reader for w in s['warnings'])
    assert sum(s['updates'] for s in reader) == sum(s['updates'] for s in before)
    assert client.post('/api/dismiss-all').status_code == 200
    assert client.delete('/api/dismissals').status_code == 200
    assert not any(w['dismissed'] for s in dashboard(client) for w in s['warnings'])
    assert sum(s['updates'] for s in dashboard(client)) > 0
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/dismiss-all').status_code == 403
    client.cookies.clear()
    assert client.post('/api/dismiss-all').status_code in {401, 403}


def test_warning_reappears_after_background_recovery_and_updates_after_new_digest(client):
    def cpu(value):
        snapshot = json.loads(module.get_server('atlas')['snapshot'])
        snapshot['metrics']['cpu'] = value
        store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
        module.refresh_server('atlas')

    def warning():
        return next(w for s in dashboard(client) if s['id'] == 'atlas' for w in s['warnings'] if w['id'] == 'cpu')

    cpu(95)
    client.post('/api/dismiss-all')
    cpu(99)
    assert warning()['dismissed']
    # No dashboard request between recovery and recurrence: background polling must clear it.
    cpu(10)
    cpu(96)
    assert not warning()['dismissed']
    snapshot = json.loads(module.get_server('atlas')['snapshot'])
    snapshot['services'][0]['update']['digest'] = 'sha256:' + 'f' * 64
    store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
    server = next(s for s in dashboard(client) if s['id'] == 'atlas')
    assert not server['services'][0]['dismissed'] and server['updates'] == 1


def test_resource_poll_clears_warning_dismissal_while_docker_busy(client):
    snapshot = json.loads(module.get_server('atlas')['snapshot'])
    snapshot['metrics']['cpu'] = 99
    store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
    client.post('/api/dismiss-all')
    for value in [10, 99]:
        snapshot['metrics']['cpu'] = value
        store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
        lock = module.threading.Lock()
        lock.acquire()
        module.poll_resources(module.get_server('atlas'), lock)
    server = next(s for s in dashboard(client) if s['id'] == 'atlas')
    assert not next(w for w in server['warnings'] if w['id'] == 'cpu')['dismissed']
