import io
import json
import threading
import urllib.error

import pytest

from test_harbour import client
from harbour import app as module, notifications as alerts, store

TOKEN = '123456789:' + 'a' * 35


@pytest.fixture(autouse=True)
def isolated_workers(monkeypatch):
    monkeypatch.setattr(alerts, 'delivery_loop', lambda stop: None)
    monkeypatch.setattr(module, 'poll_loop', lambda: None)


def settings(client, rules=None, **extra):
    body = {'enabled': True, 'bot_token': TOKEN, 'chat_id': '-1001234567890', 'rules': rules or [
        {'server_id': 'atlas', 'kind': 'cpu', 'enabled': True, 'delay_seconds': 120, 'repeat_seconds': 300}]}
    body.update(extra)
    result = client.put('/api/notifications', json=body)
    assert result.status_code == 200, result.text
    return result.json()


def observe(at, ids=('cpu',), **extra):
    store.execute("UPDATE servers SET checked=?,connection_status='up' WHERE id='atlas'", (at,))
    warnings = [{'id': id_, 'title': 'CPU usage is high' if id_ == 'cpu' else id_, 'detail': '99.0% used · threshold 85%'} for id_ in ids]
    alerts.observe(module.get_server('atlas'), warnings, now=at, **extra)


def test_config_encrypts_token_preserves_blank_and_rejects_bad_values(client):
    result = settings(client)
    assert result['token_saved'] and TOKEN not in json.dumps(result)
    stored = store.one("SELECT value FROM settings WHERE key='telegram'")['value']
    assert TOKEN not in stored
    assert store.cipher().decrypt(json.loads(stored)['token_encrypted'].encode()).decode() == TOKEN
    settings(client, bot_token='')
    store.initialize()
    assert client.get('/api/notifications').json()['token_saved']
    for patch in [{'bot_token': 'bad-token-secret'}, {'chat_id': 'https://example.org/'}, {'bot_token': TOKEN, 'clear_token': True},
                  {'rules': [{'server_id': 'missing', 'kind': 'cpu'}]},
                  {'rules': [{'server_id': 'atlas', 'kind': 'cpu', 'repeat_seconds': 1}]},
                  {'rules': [{'server_id': 'atlas', 'kind': 'cpu', 'delay_seconds': -1}]},
                  {'rules': [{'server_id': 'atlas', 'kind': 'cpu'}] * 2}]:
        response = client.put('/api/notifications', json={'enabled': True, 'chat_id': '12345', **patch})
        assert response.status_code == 422
        assert 'bad-token-secret' not in response.text and TOKEN not in response.text
    result = settings(client, bot_token='', clear_token=True, enabled=False)
    assert not result['token_saved']
    assert client.post('/api/notifications/test').status_code == 422


def test_admin_csrf_and_read_only_boundary(client):
    client.headers.pop('X-CSRF-Token')
    assert client.put('/api/notifications', json={}).status_code == 403
    assert client.post('/api/notifications/test').status_code == 403
    login = client.post('/api/login', json={'name': 'admin', 'password': 'test-password-strong'}).json()
    client.headers['X-CSRF-Token'] = login['csrf']
    client.post('/api/users', json={'name': 'reader', 'password': 'reader-password-long', 'role': 'user'})
    reader = client.post('/api/login', json={'name': 'reader', 'password': 'reader-password-long'}).json()
    client.headers['X-CSRF-Token'] = reader['csrf']
    assert client.get('/api/notifications').status_code == 403
    assert client.put('/api/notifications', json={}).status_code == 403
    assert client.post('/api/notifications/test').status_code == 403
    client.cookies.clear()
    assert client.get('/api/notifications').status_code == 401


def test_delays_repeats_fresh_readings_recovery_and_restart(client, monkeypatch):
    settings(client)
    messages = []
    monkeypatch.setattr(alerts, 'send_message', lambda value, text: messages.append(text))
    observe(1000); alerts.deliver_one(1119)
    assert not messages
    observe(1060); observe(1120); alerts.deliver_one(1120)
    assert len(messages) == 1 and 'Atlas' in messages[0] and '99.0%' in messages[0]
    store.initialize()  # timer and sent state survive restarts
    observe(1180); alerts.deliver_one(1180)
    assert len(messages) == 1
    alerts.deliver_one(1420)  # old readings never send a repeat
    assert len(messages) == 1
    for at in [1240, 1300, 1360, 1420]: observe(at)
    alerts.deliver_one(1420)
    assert len(messages) == 2
    observe(1480, ids=())
    assert not store.rows('SELECT * FROM notification_state')
    observe(1540); observe(1600); observe(1660); alerts.deliver_one(1660)
    assert len(messages) == 3


def test_entity_duration_does_not_transfer_between_disks_and_zero_repeat(client, monkeypatch):
    settings(client, rules=[{'server_id': 'atlas', 'kind': 'disk', 'enabled': True, 'delay_seconds': 60, 'repeat_seconds': 0}])
    messages = []
    monkeypatch.setattr(alerts, 'send_message', lambda value, text: messages.append(text))
    observe(1000, ['disk:/']); observe(1060, ['disk:/home']); alerts.deliver_one(1060)
    assert not messages
    observe(1120, ['disk:/home', 'disk:/log']); alerts.deliver_one(1120)
    assert len(messages) == 1 and 'disk:/home' in messages[-1] and 'disk:/log' not in messages[-1]
    observe(1180, ['disk:/home', 'disk:/log']); alerts.deliver_one(1180)
    assert len(messages) == 2 and 'disk:/home' not in messages[-1]
    for at in range(1240, 1900, 60): observe(at, ['disk:/home', 'disk:/log']); alerts.deliver_one(at)
    assert len(messages) == 2


def test_failed_poll_breaks_duration_without_rearming_sent_warning(client, monkeypatch):
    settings(client, rules=[{'server_id': 'atlas', 'kind': 'cpu', 'enabled': True, 'delay_seconds': 60, 'repeat_seconds': 0}])
    sent = []
    monkeypatch.setattr(alerts, 'send_message', lambda value, text: sent.append(text))
    observe(1000); observe(1060, successful=False); observe(1120); alerts.deliver_one(1120)
    assert not sent
    observe(1180); alerts.deliver_one(1180)
    assert len(sent) == 1
    observe(1240, successful=False); alerts.deliver_one(1240)
    observe(1300); observe(1360); alerts.deliver_one(1360)
    assert len(sent) == 1


def test_rule_edits_disable_pause_and_removal_cancel_pending_delivery(client, monkeypatch):
    settings(client)
    sent = []
    monkeypatch.setattr(alerts, 'send_message', lambda value, text: sent.append(text))
    observe(1000); observe(1060); observe(1120)
    # An identical save retains active timers.
    settings(client, bot_token='')
    assert store.rows('SELECT * FROM notification_state')
    store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    alerts.deliver_one(1120)
    assert not sent
    observe(1120)
    assert not store.rows('SELECT * FROM notification_state')
    store.execute("UPDATE servers SET monitoring_enabled=1 WHERE id='atlas'")
    observe(1180)
    settings(client, bot_token='', rules=[{'server_id':'atlas','kind':'cpu','enabled':False}])
    alerts.deliver_one(1300)
    assert not sent and not store.rows('SELECT * FROM notification_state')
    settings(client, bot_token=''); observe(1400)
    settings(client, enabled=False, bot_token='')
    assert not store.rows('SELECT * FROM notification_state')
    settings(client, bot_token=''); observe(1500)
    store.execute("DELETE FROM servers WHERE id='atlas'")
    assert not store.rows('SELECT * FROM notification_state') and not store.rows('SELECT * FROM notification_rules')


def test_retries_use_backoff_and_latest_value_without_blocking_monitoring(client, monkeypatch):
    settings(client, rules=[{'server_id':'atlas','kind':'cpu','enabled':True,'delay_seconds':0,'repeat_seconds':60}])
    calls = []
    def failing(value, text):
        calls.append(text)
        raise alerts.DeliveryError('Rate limited', 90)
    monkeypatch.setattr(alerts, 'send_message', failing)
    observe(1000); alerts.deliver_one(1000)
    assert alerts.config()['next_attempt'] == 1090
    observe(1060); alerts.deliver_one(1060)
    assert len(calls) == 1
    observe(1120); alerts.deliver_one(1120)
    assert alerts.config()['next_attempt'] == 1240
    monkeypatch.setattr(alerts, 'send_message', lambda value, text: calls.append(text))
    observe(1180); observe(1240); alerts.deliver_one(1240)
    assert len(calls) == 3 and alerts.config()['last_error'] == ''


def test_slow_delivery_does_not_block_observation_or_revive_recovered_warning(client, monkeypatch):
    settings(client, rules=[{'server_id':'atlas','kind':'cpu','enabled':True,'delay_seconds':0,'repeat_seconds':0}])
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(alerts, 'send_message', lambda value, text: (started.set(), release.wait(3)))
    observe(1000)
    worker = threading.Thread(target=alerts.deliver_one, args=(1000,)); worker.start()
    try:
        assert started.wait(1)
        observe(1060, [])
        assert not store.rows('SELECT * FROM notification_state')
    finally:
        release.set(); worker.join(2)
    assert not worker.is_alive() and not store.rows('SELECT * FROM notification_state')


def test_background_integration_independent_of_dismissals_and_docker_lock(client):
    settings(client, rules=[{'server_id':'atlas','kind':kind,'enabled':True,'delay_seconds':0,'repeat_seconds':0} for kind in alerts.KINDS])
    snapshot = json.loads(module.get_server('atlas')['snapshot'])
    snapshot['metrics']['cpu'] = 99
    snapshot['metrics']['memory']['percent'] = 99
    snapshot['metrics']['disks'][0]['percent'] = 99
    snapshot['metrics']['temperature']['sensors'][0]['celsius'] = 100
    store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
    module.refresh_server('atlas')
    assert {r['kind'] for r in store.rows('SELECT * FROM notification_state')} == set(alerts.KINDS)
    client.post('/api/dismiss-all')
    before = store.rows('SELECT * FROM notification_state')
    assert len(before) == 4
    snapshot['metrics']['cpu'] = 10
    store.execute("UPDATE servers SET snapshot=? WHERE id='atlas'", (json.dumps(snapshot),))
    lock = threading.Lock(); lock.acquire()
    module.poll_resources(module.get_server('atlas'), lock)
    assert not lock.locked()
    assert 'cpu' not in {r['kind'] for r in store.rows('SELECT * FROM notification_state')}


def test_demo_never_contacts_telegram_and_test_is_explicit(client, monkeypatch):
    monkeypatch.setattr(alerts.urllib.request, 'build_opener', lambda *args: pytest.fail('Demo contacted Telegram'))
    settings(client)
    assert alerts.config()['last_sent'] is None
    result = client.post('/api/notifications/test')
    assert result.status_code == 200 and result.json()['simulated']
    assert client.post('/api/notifications/test').status_code == 429


def test_telegram_protocol_redacts_errors_and_honors_retry_after(client, monkeypatch):
    settings(client)
    monkeypatch.setattr(store, 'DEMO', False)
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert timeout == 10
            return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
    monkeypatch.setattr(alerts.urllib.request, 'build_opener', lambda *args: Opener())
    alerts.send_message(alerts.config(), 'Atlas · CPU 99%')
    assert seen[0].full_url == 'https://api.telegram.org/bot'+TOKEN+'/sendMessage'
    assert json.loads(seen[0].data)['text'] == 'Atlas · CPU 99%'
    assert 'parse_mode' not in json.loads(seen[0].data)
    class Limited:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, TOKEN, {}, io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":250}}'))
    monkeypatch.setattr(alerts.urllib.request, 'build_opener', lambda *args: Limited())
    with pytest.raises(alerts.DeliveryError) as error:
        alerts.send_message(alerts.config(), 'Test')
    assert error.value.retry_after == 250 and TOKEN not in str(error.value)
    class Broken:
        def open(self, request, timeout):
            raise OSError(request.full_url)
    monkeypatch.setattr(alerts.urllib.request, 'build_opener', lambda *args: Broken())
    result = client.post('/api/notifications/test')
    assert result.status_code == 502 and TOKEN not in result.text
    assert TOKEN not in json.dumps(alerts.public_config())
    assert alerts.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.org') is None


def test_rules_are_per_server_and_kind_and_duplicate_samples_keep_duration(client, monkeypatch):
    settings(client, rules=[{'server_id':'atlas','kind':'cpu','enabled':True,'delay_seconds':60,'repeat_seconds':0},
                           {'server_id':'atlas','kind':'memory','enabled':False},
                           {'server_id':'luna','kind':'memory','enabled':True,'delay_seconds':0,'repeat_seconds':0}])
    sent = []
    monkeypatch.setattr(alerts, 'send_message', lambda value, text: sent.append(text))
    observe(1000, ['cpu', 'memory', 'service:test']); observe(1030, ['cpu', 'memory']); observe(1030, ['cpu'])
    observe(1060, ['cpu']); alerts.deliver_one(1060)
    assert len(sent) == 1 and 'Atlas' in sent[-1] and 'Memory' not in sent[-1]
    lunar = module.get_server('luna')
    alerts.observe(lunar, [{'id':'cpu','title':'CPU','detail':'99%'}, {'id':'memory','title':'Memory','detail':'98% used'}], now=1100)
    alerts.deliver_one(1100)
    assert len(sent) == 2 and 'Luna' in sent[-1] and '98% used' in sent[-1] and 'CPU' not in sent[-1]
