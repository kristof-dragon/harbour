import hashlib
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from test_harbour import client
from harbour import modem, modem_client as protocol, store

BASE = '/api/servers/atlas/modem'
PASSWORD = 'fixture-only-password'
REAL_WORKER = modem.run
LOGIN_SCRIPT = b'''<script>function HashPassword(p){return sha256(p)};var Data={'fun':15,'Password':HashPassword(p)};</script>'''


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch):
    monkeypatch.setattr(modem, 'run', lambda stop: stop.wait())
    modem._clients.clear()
    yield
    modem._clients.clear()


@pytest.fixture
def router_client(client):
    store.execute("UPDATE servers SET server_type='openwrt' WHERE id='atlas'")
    return client


def settings(**changes):
    return {**modem.DEFAULT, 'username': 'admin', 'password': PASSWORD, **changes}


def sample(state='ok'):
    return {'at': time.time(), 'received_at': time.time(), 'state': state,
            'sections': {'device': {'state': 'ok', 'fields': [{'field': 'status/uptime', 'value': '123'}]}}, 'cause': 'not_established'}


def test_encrypted_keep_remove_and_credentials_never_returned(router_client):
    c = router_client
    assert c.get(BASE).json()['settings']['enabled'] is False
    result = c.put(BASE, json=settings()).json()
    assert result['password_saved'] and PASSWORD not in json.dumps(result)
    row = modem.config('atlas')
    assert PASSWORD not in json.dumps(row)
    assert store.cipher().decrypt(row['password_encrypted'].encode()).decode() == PASSWORD
    assert c.put(BASE, json=settings(password='', interval_seconds=90)).status_code == 200
    assert modem.config('atlas')['password_encrypted'] == row['password_encrypted']
    assert c.put(BASE, json=settings(password='', url='http://192.168.0.1')).status_code == 422
    assert c.put(BASE, json=settings(password='', username='other')).status_code == 422
    assert c.put(BASE, json=settings(password='', clear_password=True, enabled=True)).status_code == 422
    assert c.put(BASE, json=settings(password='', clear_password=True)).json()['password_saved'] is False
    assert c.post(BASE+'/test').status_code == 400
    assert PASSWORD not in c.put(BASE, json=settings(password=PASSWORD*100)).text


def test_optional_username_can_save_enable_and_test(router_client, monkeypatch):
    c, calls = router_client, []
    class Fake:
        def __init__(self, url, username, password):
            calls.append((username, password))
        def poll(self):
            return sample()
    monkeypatch.setattr(modem, 'Client', Fake)
    monkeypatch.setattr(store, 'DEMO', False)
    for enabled in (False, True):
        saved = c.put(BASE, json=settings(username='', enabled=enabled))
        assert saved.status_code == 200
        result = c.post(BASE+'/test').json()
        assert result['sample']['state'] == 'ok'
        assert result['settings']['username'] == ''
        assert result['settings']['enabled'] is enabled
        assert PASSWORD not in json.dumps(result)
    assert calls == [('', PASSWORD), ('', PASSWORD)]


def test_required_username_blocks_worker_without_exposing_credentials(router_client, monkeypatch):
    c = router_client
    class Fake:
        def __init__(self, *args):
            pass
        def poll(self):
            raise protocol.ModemError('username_required', 'This modem requires a username.')
    monkeypatch.setattr(modem, 'Client', Fake)
    monkeypatch.setattr(store, 'DEMO', False)
    c.put(BASE, json=settings(username='', enabled=True))
    result = c.post(BASE+'/test').json()
    assert result['blocked']
    assert result['sample']['error_code'] == 'username_required'
    assert PASSWORD not in json.dumps(result)


@pytest.mark.parametrize('url', ['http://example.com', 'http://127.0.0.1', 'http://169.254.169.254', 'http://8.8.8.8', 'file:///tmp/x', 'http://[::1]', 'http://user:pass@192.168.1.1', 'http://192.168.1.1/path', 'http://192.168.1.1?next=x', 'http://192.168.1.1#x', 'http://192.168.1.1:0', 'http://192.168.1.1:65536', 'http://192.168.\n1.1'])
def test_target_validation(url):
    with pytest.raises(ValueError):
        protocol.endpoint(url)


def test_permissions_csrf_demo_and_unique_target(router_client, monkeypatch):
    c = router_client
    assert c.put(BASE, json=settings()).status_code == 200
    monkeypatch.setattr(modem, 'Client', lambda *a: pytest.fail('Demo contacted modem'))
    assert c.post(BASE+'/test').json()['sample'] is None
    store.execute("UPDATE servers SET server_type='openwrt' WHERE id='beacon'")
    # Use a real second demo server rather than depending on its name.
    other = store.one("SELECT id FROM servers WHERE id<>'atlas'")['id']
    store.execute("UPDATE servers SET server_type='openwrt' WHERE id=?", (other,))
    assert c.put('/api/servers/'+other+'/modem', json=settings()).status_code == 409
    csrf = c.headers.pop('X-CSRF-Token')
    assert c.post(BASE+'/test').status_code == 403
    c.headers['X-CSRF-Token'] = csrf
    c.post('/api/users', json={'name': 'reader', 'password': 'reader-password-strong', 'role': 'user'})
    login = c.post('/api/login', json={'name': 'reader', 'password': 'reader-password-strong'}).json()
    c.headers['X-CSRF-Token'] = login['csrf']
    assert c.get(BASE).status_code == 200
    assert c.get(BASE+'/export').status_code == 200
    assert c.put(BASE, json=settings()).status_code == 403
    assert c.post(BASE+'/test').status_code == 403
    c.cookies.clear()
    assert c.get(BASE).status_code == 401


@pytest.mark.parametrize('enabled', [False, True])
def test_poll_session_reuse_gap_backoff_block_and_recovery(router_client, monkeypatch, enabled):
    c, calls = router_client, []
    class Fake:
        failure = None
        def __init__(self, *args):
            calls.append('login')
        def poll(self):
            calls.append('poll')
            if self.failure:
                raise self.failure
            return sample()
    monkeypatch.setattr(modem, 'Client', Fake)
    monkeypatch.setattr(store, 'DEMO', False)
    assert c.put(BASE, json=settings(enabled=enabled)).status_code == 200
    assert c.post(BASE+'/test').json()['sample']['state'] == 'ok'
    assert c.post(BASE+'/test').status_code == 200
    assert calls == ['login', 'poll', 'poll']
    assert c.put(BASE, json=settings(enabled=enabled, password='', interval_seconds=120)).status_code == 200
    assert c.get(BASE).json()['sample'] is None  # New settings epoch.
    assert c.post(BASE+'/test').status_code == 200
    assert calls.count('login') == 1  # Cadence changes retain the session.
    Fake.failure = protocol.AuthenticationError()
    d = c.post(BASE+'/test').json()
    assert d['blocked'] and d['sample']['state'] == 'unavailable'
    assert d['sample']['cause'] == 'not_established'
    before = list(calls)
    modem.poll_one('atlas')
    assert calls == before  # Rejected credentials are not retried by the worker.
    Fake.failure = None
    assert c.post(BASE+'/test').json()['blocked'] is False
    Fake.failure = RuntimeError('sensitive-response '+PASSWORD)
    result = c.post(BASE+'/test')
    assert PASSWORD not in result.text
    assert result.json()['sample']['error_code'] == 'collection_failed'
    row = modem.config('atlas')
    assert row['next_attempt'] > time.time()+200
    modem.poll_one('atlas')
    assert calls[-1] == 'poll'
    evidence = c.get(BASE+'/export').json()
    assert PASSWORD not in json.dumps(evidence)
    assert all(s['configuration_revision'] for s in evidence['samples'])
    assert len({s['configuration_revision'] for s in evidence['samples']}) == 2
    assert 'sections' not in c.get(BASE+'/history').json()['samples'][0]


def test_nonoverlap_and_epoch_stale_history_retention(router_client, monkeypatch):
    c = router_client
    c.put(BASE, json=settings())
    lock = modem.lock_for('atlas')
    with lock:
        assert c.post(BASE+'/test').status_code == 409
        assert c.put(BASE, json=settings()).status_code == 409
        assert modem.poll_one('atlas') is None
    revision = modem.config('atlas')['revision']
    old = sample()
    old['received_at'] -= 300
    old['at'] -= 300
    assert modem.persist('atlas', revision, old)
    assert c.get(BASE).json()['stale']
    assert not modem.persist('atlas', 'obsolete', sample())
    with store.db() as con:
        for _ in range(260):
            s = sample()
            payload = json.dumps(s)
            con.execute('INSERT INTO modem_samples(server_id,revision,measured,received,state,payload,bytes) VALUES (?,?,?,?,?,?,?)', ('atlas', revision, s['at'], s['received_at'], 'ok', payload, len(payload)))
    assert c.get(BASE+'/export').json()['truncated']
    assert len(c.get(BASE+'/export').json()['samples']) == 256
    monkeypatch.setattr(modem, 'MAX_HISTORY', 1)
    modem.persist('atlas', revision, sample())
    assert store.one('SELECT COUNT(*) AS n FROM modem_samples')['n'] == 6
    store.execute('DELETE FROM servers WHERE id=?', ('atlas',))
    assert store.one('SELECT COUNT(*) AS n FROM modem_samples')['n'] == 0


def test_worker_honours_pause_backoff_and_demo(router_client, monkeypatch):
    # Exercise the real worker without starting real timers or network requests.
    router_client.put(BASE, json=settings(enabled=True))
    calls = []
    monkeypatch.setattr(modem, 'poll_one', calls.append)
    class OneIteration:
        waited = False
        def wait(self, seconds):
            previous, self.waited = self.waited, True
            return previous
        def is_set(self):
            return False
    monkeypatch.setattr(store, 'DEMO', False)
    REAL_WORKER(OneIteration())
    assert calls == ['atlas']
    calls.clear()
    store.execute('UPDATE modem_settings SET next_attempt=?', (time.time()+900,))
    REAL_WORKER(OneIteration())
    store.execute('UPDATE modem_settings SET next_attempt=0,blocked=1')
    REAL_WORKER(OneIteration())
    store.execute('UPDATE modem_settings SET blocked=0')
    store.execute("UPDATE servers SET monitoring_enabled=0 WHERE id='atlas'")
    REAL_WORKER(OneIteration())
    store.execute("UPDATE servers SET monitoring_enabled=1 WHERE id='atlas'")
    monkeypatch.setattr(store, 'DEMO', True)
    REAL_WORKER(OneIteration())
    assert calls == []


def test_snapshot_and_xml_bounds_redaction():
    xml = b'<status><Password>pw</Password><SID>cookie</SID><cm_serial_number>serial</cm_serial_number><channel><power>123</power></channel><channel><power>456</power></channel><event>secret-token</event><value>fixture-only-password</value></status>'
    values = protocol.fields(protocol.xml_document(xml), (PASSWORD,))
    text = json.dumps(values)
    assert PASSWORD not in text and 'serial' not in text and 'cookie' not in text
    assert 'status/channel[1]/power' in text and '123' in text
    for invalid in [b'<!DOCTYPE a [<!ENTITY x "evil">]><a>&x;</a>', '<!DOCTYPE a [<!ENTITY x "evil">]><a>&x;</a>'.encode('utf-16'), b'<a>', b'x'*(protocol.MAX_RESPONSE+1), b'<error>bad</error>']:
        with pytest.raises(protocol.ModemError):
            protocol.xml_document(invalid)
    with pytest.raises(protocol.AuthenticationError):
        protocol.xml_document(b'<status><AccessDenied>1</AccessDenied></status>')
    with pytest.raises(protocol.AuthenticationError):
        protocol.xml_document(b'<html/>')
    root = protocol.xml_document(('<s>'+('<value>'+'x'*200+'</value>')*400+'</s>').encode())
    assert len(protocol.fields(root)['fields']) == protocol.MAX_FIELDS
    assert protocol.fields(root)['omitted_fields'] == 16
    rows = [{'field': 'field', 'value': 'x'*400} for _ in range(384)]
    big = sample()
    big['sections'] = {name: {'fields': list(rows)} for name in protocol.SECTIONS}
    payload = modem.bounded_sample(big)
    assert len(payload.encode()) <= modem.MAX_SAMPLE
    assert json.loads(payload)['truncated']


@pytest.fixture
def firmware(monkeypatch):
    state = {'requests': [], 'login': b'successful;SID=fixture-session', 'bad_section': None, 'redirect': False, 'oversize': False, 'slow': False,
             'cookie': 'sessionToken=fixture-preauth; Path=/; HttpOnly',
             'page': b'<input name="loginUsername" value="admin"><input name="loginPassword" type="password">'+LOGIN_SCRIPT}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            state['requests'].append((self.path, None, self.headers.get('Cookie')))
            if state['slow']:
                try:
                    for byte in b'HTTP/1.0 200 OK\r\n':
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_response(302 if state['redirect'] else 200)
            self.send_header('Location', 'http://192.168.1.2/steal')
            self.send_header('Content-Length', str(len(state['page'])))
            self.send_header('Set-Cookie', state['cookie'])
            self.end_headers()
            self.wfile.write(state['page'])
        def do_POST(self):
            data = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
            state['requests'].append((self.path, data, self.headers.get('Cookie')))
            previous_token = state.get('token', 'fixture-preauth')
            if 'sessionToken='+previous_token not in (self.headers.get('Cookie') or ''):
                self.close_connection = True
                return
            self.send_response(200)
            if state.get('rotate_cookie') and self.path.endswith('getter.xml'):
                state['token'] = 'fixture-rotated'
                self.send_header('Set-Cookie', 'sessionToken=fixture-rotated; Path=/; HttpOnly')
            self.end_headers()
            response = state['login'] if self.path.endswith('setter.xml') else ('<status><power>123</power><value>'+previous_token+' '+state.get('token', previous_token)+'</value></status>').encode()
            if data.get('fun') == [str(state['bad_section'])]:
                response = b'malformed XML'
            if state['oversize']:
                response = b'x'*(protocol.MAX_RESPONSE+1)
            self.wfile.write(response)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = http.client.HTTPConnection
    monkeypatch.setattr(protocol.http.client, 'HTTPConnection', lambda host, port, timeout: original('127.0.0.1', server.server_port, timeout=timeout))
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def test_actual_http_protocol_session_and_partial_read(firmware):
    c = protocol.Client('http://192.168.100.1', 'admin', PASSWORD)
    assert c.poll()['state'] == 'ok'
    login = firmware['requests'][1]
    assert login[1] == {'fun': ['15'], 'Username': ['admin'], 'Password': [hashlib.sha256(PASSWORD.encode()).hexdigest()]}
    assert login[2] == 'sessionToken=fixture-preauth'
    assert len(firmware['requests']) == 7
    assert all(req[2] == 'sessionToken=fixture-preauth; SID=fixture-session' for req in firmware['requests'][2:])
    firmware['bad_section'] = 10
    result = c.poll()
    assert len(firmware['requests']) == 12
    assert result['state'] == 'partial'
    assert result['sections']['downstream']['state'] == 'unavailable'
    assert result['sections']['upstream']['state'] == 'ok'
    assert 'fixture-preauth' not in json.dumps(result)
    for function in (16, 100, '15'):
        with pytest.raises(ValueError):
            c.request('/xml/setter.xml', {'fun': function})
    with pytest.raises(ValueError):
        c.request('/xml/getter.xml', {'fun': 99})


@pytest.mark.parametrize('cookie', [
    'sessionToken=fixture-preauth; Domain=other.example; Path=/',
    'sessionToken=fixture-preauth; Path=/unrelated',
    'sessionToken=fixture-preauth; Secure; Path=/',
    'sessionToken=fixture-preauth; Max-Age=0; Path=/',
    'unrelated=fixture-preauth; Path=/',
])
def test_session_cookie_scope(firmware, cookie):
    firmware['cookie'] = cookie
    c = protocol.Client('http://192.168.100.1', '', PASSWORD)
    c.request('/common_page/login.html')
    assert c.session_token is None


@pytest.mark.parametrize('value', ['x'*257, 'bad value'])
def test_session_cookie_bounded_before_credentials_sent(firmware, value):
    firmware['cookie'] = 'sessionToken="'+value+'"; Path=/'
    with pytest.raises(protocol.ModemError):
        protocol.Client('http://192.168.100.1', '', PASSWORD).poll()
    assert len(firmware['requests']) == 1


def test_rotating_session_cookie_reused_and_both_values_redacted(firmware):
    firmware['rotate_cookie'] = True
    result = protocol.Client('http://192.168.100.1', '', PASSWORD).poll()
    assert result['state'] == 'ok'
    assert 'fixture-preauth' not in json.dumps(result)
    assert 'fixture-rotated' not in json.dumps(result)
    assert firmware['requests'][3][2] == 'sessionToken=fixture-rotated; SID=fixture-session'


@pytest.mark.parametrize('username,default,expected,mode', [
    ('', 'admin', 'admin', 'modem_default'),
    ('', 'operator', 'operator', 'modem_default'),
    ('custom', 'admin', 'custom', 'configured_username'),
    ('custom', '', 'custom', 'configured_username'),
])
def test_login_uses_configured_or_actual_form_default(firmware, username, default, expected, mode):
    firmware['page'] = firmware['page'].replace(b'value="admin"', ('value="'+default+'"').encode())
    result = protocol.Client('http://192.168.100.1', username, PASSWORD).poll()
    assert firmware['requests'][1][1]['Username'] == [expected]
    assert result['authentication'] == {'state': 'authenticated', 'mode': mode}


@pytest.mark.parametrize('username', ['', 'unused'])
def test_password_only_omits_username(firmware, username):
    firmware['page'] = firmware['page'].replace(b'<input name="loginUsername" value="admin">', b'')
    result = protocol.Client('http://192.168.100.1', username, PASSWORD).poll()
    assert 'Username' not in firmware['requests'][1][1]
    assert result['authentication']['mode'] == 'password_only'


@pytest.mark.parametrize('page,code', [
    (b'<input name="loginUsername"><input name="loginPassword" type="password">'+LOGIN_SCRIPT, 'username_required'),
    (b'loginUsername loginPassword HashPassword sha256', 'unsupported'),
    (b'<input name="password" type="password">', 'unsupported'),
    (b'<input name="loginPassword" type="password">HashPassword sha256 loginUsername', 'unsupported'),
    (b'<input name="loginPassword" type="password"><input name="loginPassword" type="password">HashPassword sha256', 'unsupported'),
    (b'<input name="loginPassword" type="password">'+LOGIN_SCRIPT.replace(b"'fun':15", b"'fun':99"), 'unsupported'),
])
def test_missing_username_or_unknown_form_never_submits_password(firmware, page, code):
    firmware['page'] = page
    with pytest.raises(protocol.ModemError) as exc:
        protocol.Client('http://192.168.100.1', '', PASSWORD).poll()
    assert exc.value.code == code
    assert len(firmware['requests']) == 1


@pytest.mark.parametrize('answer', [b'idloginrightincorrect', b'KDGchangePW', b'unsuccessful SID=x', b'not successful SID=x', b'successful', b'successful SID=bad%0d%0a'])
def test_login_rejection_does_not_read_status(firmware, answer):
    firmware['login'] = answer
    with pytest.raises(protocol.AuthenticationError):
        protocol.Client('http://192.168.100.1', 'admin', PASSWORD).poll()
    assert len(firmware['requests']) == 2


def test_redirects_and_oversized_responses_never_followed(firmware):
    c = protocol.Client('http://192.168.100.1', 'admin', PASSWORD)
    firmware['redirect'] = True
    with pytest.raises(protocol.ModemError, match='Redirects'):
        c.poll()
    assert len(firmware['requests']) == 1
    firmware['redirect'] = False
    firmware['oversize'] = True
    with pytest.raises(protocol.ModemError, match='size limit'):
        c.poll()


def test_slow_headers_obey_wall_clock_deadline(firmware):
    firmware['slow'] = True
    started = time.monotonic()
    with pytest.raises(protocol.ModemError):
        protocol.Client('http://192.168.100.1', 'admin', PASSWORD).request('/common_page/login.html', deadline=started+.12)
    assert time.monotonic()-started < .5
