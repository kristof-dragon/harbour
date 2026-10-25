"""Bounded Compal CH7465/ONE web client. No router SSH or configuration writes.

Protocol verified against the device-served login.html and CommonAPI.js:
setter 15 authenticates with SHA256(password); getters 1/2/10/11/13 read status.
Firmware-specific XML values retain their names/units until verified on hardware.
"""
import hashlib
import http.client
from html.parser import HTMLParser
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

SECTIONS = {'system': 1, 'device': 2, 'downstream': 10, 'upstream': 11, 'events': 13}
MAX_RESPONSE = 128 * 1024
MAX_FIELDS = 384


class ModemError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AuthenticationError(ModemError):
    def __init__(self):
        super().__init__('authentication_required', 'Modem login was rejected or requires attention. Polling is paused; check the credentials and test again.')


class LoginForm(HTMLParser):
    """Inspect known inputs only; never execute modem JavaScript or follow actions."""
    def __init__(self):
        super().__init__()
        self.inputs = {}
        self.duplicate = False

    def handle_starttag(self, tag, attrs):
        if tag != 'input':
            return
        attrs = dict(attrs)
        name = attrs.get('name')
        if name in ('loginUsername', 'loginPassword'):
            self.duplicate |= name in self.inputs
            self.inputs[name] = attrs


def login_identity(page, username):
    """Blank usernames use a firmware default, or no username on password-only forms."""
    form = LoginForm()
    form.feed(page.decode('utf-8', 'replace'))
    password = form.inputs.get('loginPassword', {})
    if (form.duplicate or password.get('type', '').lower() != 'password'
            or not all(part in page for part in (b'HashPassword', b'sha256'))
            or not re.search(br'''["']fun["']\s*:\s*15\b''', page)
            or not re.search(br'''["']Password["']\s*:\s*HashPassword\s*\(''', page)):
        raise ModemError('unsupported', 'The modem login protocol does not match the supported Compal ONE firmware.')
    field = form.inputs.get('loginUsername')
    if field is None:
        # A different firmware that still refers to a missing username is not a
        # supported password-only form. Do not guess its authentication payload.
        if b'loginUsername' in page or re.search(br'''["']Username["']\s*:''', page):
            raise ModemError('unsupported', 'The modem login form could not be identified safely.')
        return None, 'password_only'
    value = username or (field.get('value') or '').strip()
    if not value:
        raise ModemError('username_required', 'This modem requires a username and provides no default. Enter its username and test again.')
    if len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ModemError('unsupported', 'The modem login form supplied an unsupported username.')
    return value, 'configured_username' if username else 'modem_default'


def endpoint(value):
    """Literal RFC1918 addresses only. No DNS, userinfo, proxy or redirect targets."""
    try:
        if any(ord(c) < 33 for c in value):
            raise ValueError()
        u = urllib.parse.urlsplit(value)
        ip = ipaddress.IPv4Address(u.hostname or '')
        private = any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        if not private or u.scheme not in ('http', 'https') or u.username is not None or u.password is not None or u.query or u.fragment or u.path not in ('', '/') or (u.port is not None and not 1 <= u.port <= 65535):
            raise ValueError()
        port = u.port if u.port != (443 if u.scheme == 'https' else 80) else None
        return f'{u.scheme}://{ip}' + (f':{port}' if port is not None else '')
    except (ValueError, TypeError):
        raise ValueError('Use an http:// or https:// private IPv4 address with an optional port, without a path or credentials.') from None


def xml_document(data):
    if len(data) > MAX_RESPONSE or b'\x00' in data or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
        raise ModemError('invalid_response', 'Modem returned unsupported or oversized XML.')
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise ModemError('invalid_response', 'Modem returned no usable XML status.') from None
    elements = list(root.iter())
    if len(elements) > 4096:
        raise ModemError('invalid_response', 'Modem XML exceeded the field limit.')
    for e in elements:
        tag = e.tag.rsplit('}', 1)[-1].lower()
        if tag in ('accessdenied', 'lockedout') and (e.text or '').strip().lower() in ('1', 'true', 'yes'):
            raise AuthenticationError()
    if root.tag.rsplit('}', 1)[-1].lower() == 'html':
        raise AuthenticationError()
    if root.tag.lower() in ('error', 'fault'):
        raise ModemError('invalid_response', 'Modem returned an error instead of status values.')
    return root


def fields(root, secrets=()):
    """Preserve modem-supplied values, not guessed scaling or fabricated zeroes."""
    result, omitted = [], 0
    def visit(node, path, depth):
        nonlocal omitted
        name = node.tag.rsplit('}', 1)[-1]
        if re.search(r'password|passwd|secret|token|cookie|nonce|serial|mac.?addr|ssid|private.?key|^sid$|username', name, re.I):
            return
        if depth > 12 or len(result) >= MAX_FIELDS:
            omitted += 1
            return
        children = list(node)
        if children:
            counts = {}
            totals = {}
            for child in children:
                totals[child.tag] = totals.get(child.tag, 0) + 1
            for child in children:
                counts[child.tag] = counts.get(child.tag, 0) + 1
                part = child.tag.rsplit('}', 1)[-1]
                if totals[child.tag] > 1:
                    part += '[' + str(counts[child.tag]) + ']'
                visit(child, path + '/' + part, depth + 1)
        elif node.text and node.text.strip():
            value = node.text.strip()
            if re.search(r'password|passwd|secret|token|private.?key|\bSID\s*=', value, re.I):
                value = '[Sensitive value omitted]'
            for secret in secrets:
                if secret:
                    value = value.replace(secret, '[redacted]')
                    path = path.replace(secret, '[redacted]')
            result.append({'field': path[:240], 'value': value[:400]})
            if len(value) > 400:
                omitted += 1
    visit(root, root.tag.rsplit('}', 1)[-1], 0)
    return {'fields': result, 'omitted_fields': omitted}


class Client:
    def __init__(self, url, username, password):
        self.url, self.username, self.password = endpoint(url), username, password
        self.sid = None
        self.login_mode = None

    def request(self, path, values=None, deadline=None):
        if path not in ('/common_page/login.html', '/js/CommonAPI.js', '/xml/getter.xml', '/xml/setter.xml'):
            raise ValueError('Unsupported modem request')
        if path == '/xml/setter.xml' and (not values or values.get('fun') != 15):
            raise ValueError('Only authentication is permitted on the modem setter')
        if path == '/xml/getter.xml' and (not values or values.get('fun') not in SECTIONS.values()):
            raise ValueError('Unsupported modem getter')
        remaining = min(4, deadline-time.monotonic()) if deadline else 4
        if remaining <= 0:
            raise ModemError('timeout', 'Modem collection exceeded its time limit.')
        url = urllib.parse.urlsplit(self.url)
        connection = (http.client.HTTPSConnection(url.hostname, url.port, timeout=remaining, context=ssl.create_default_context())
                      if url.scheme == 'https' else http.client.HTTPConnection(url.hostname, url.port, timeout=remaining))
        headers = {'User-Agent': 'Harbour-Modem/1', 'Accept': 'text/xml, text/html',
                   'Referer': self.url+'/', 'Origin': self.url, 'X-Requested-With': 'XMLHttpRequest',
                   'Connection': 'close'}
        if self.sid:
            headers['Cookie'] = 'SID=' + self.sid
        data = None
        if values is not None:
            data = urllib.parse.urlencode(values).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
        expired, finished = threading.Event(), threading.Event()
        response_socket = None
        def abort():
            if finished.is_set():
                return
            expired.set()
            sock = response_socket or connection.sock
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        # Socket idle timeouts alone do not bound slow header/body trickles.
        timer = threading.Timer(remaining, abort)
        timer.daemon = True
        timer.start()
        try:
            connection.request('POST' if data is not None else 'GET', path, data, headers)
            response = connection.getresponse()
            # HTTP/1.0 and Connection: close can detach connection.sock here.
            response_socket = connection.sock or getattr(getattr(response.fp, 'raw', None), '_sock', None)
            if response.status in (401, 403) or (300 <= response.status < 400 and path.startswith('/xml/')):
                raise AuthenticationError()
            if response.status != 200:
                raise ModemError('http_error', 'Modem did not return a successful response. Redirects are not followed.')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ModemError('invalid_response', 'Compressed modem responses are not supported.')
            # Check the overall deadline between bounded reads, including slow responses.
            chunks, size = [], 0
            while True:
                # A complete Content-Length response can close its socket in read1.
                # Do not change its timeout after all response bytes were received.
                if response.isclosed():
                    break
                if deadline and time.monotonic() >= deadline:
                    raise ModemError('timeout', 'Modem collection exceeded its time limit.')
                if response_socket and deadline:
                    response_socket.settimeout(max(.01, min(4, deadline-time.monotonic())))
                part = response.read1(min(8192, MAX_RESPONSE + 1 - size))
                if not part:
                    break
                chunks.append(part)
                size += len(part)
                if size > MAX_RESPONSE:
                    raise ModemError('invalid_response', 'Modem response exceeded the size limit.')
            if expired.is_set():
                raise ModemError('timeout', 'Modem request timed out; the cause is unknown.')
            return b''.join(chunks)
        except (TimeoutError, socket.timeout):
            raise ModemError('timeout', 'Modem request timed out; the cause is unknown.') from None
        except (OSError, http.client.HTTPException):
            raise ModemError('unreachable', 'Modem could not be read from this Harbour host; the cause is unknown.') from None
        finally:
            finished.set()
            timer.cancel()
            timer.join()
            connection.close()

    def login(self, deadline):
        page = self.request('/common_page/login.html', deadline=deadline)
        username, mode = login_identity(page, self.username)
        values = {'fun': 15, 'Password': hashlib.sha256(self.password.encode()).hexdigest()}
        if username is not None:
            values['Username'] = username
        response = self.request('/xml/setter.xml', values, deadline).decode('utf-8', 'replace')
        if re.search(r'incorrect|passwordneedstochange|KDGchangePW|KDGsuperUser|unsuccessful|not\s+successful', response, re.I):
            raise AuthenticationError()
        sid = re.search(r'\bSID=([A-Za-z0-9_-]{1,256})(?:\s|$|[;<])', response)
        if not re.search(r'\bsuccessful\b', response, re.I) or not sid:
            raise AuthenticationError()
        self.sid = sid[1]
        self.login_mode = mode

    def poll(self):
        deadline, started = time.monotonic()+25, time.time()
        if not self.sid:
            self.login(deadline)
        sections = {}
        redactions = (self.password, hashlib.sha256(self.password.encode()).hexdigest(), self.sid)
        for name, function in SECTIONS.items():
            at = time.time()
            try:
                root = xml_document(self.request('/xml/getter.xml', {'fun': function}, deadline))
                content = fields(root, redactions)
                sections[name] = {'observed_at': at, 'received_at': time.time(), 'state': 'ok' if content['fields'] else 'unavailable', **content}
            except AuthenticationError:
                self.sid = None
                raise
            except ModemError as exc:
                sections[name] = {'observed_at': at, 'received_at': time.time(), 'state': 'unavailable', 'error': str(exc), 'fields': []}
        usable = sum(s['state'] == 'ok' for s in sections.values())
        return {'at': started, 'received_at': time.time(), 'state': 'ok' if usable == len(SECTIONS) else 'partial' if usable else 'unavailable',
                'authentication': {'state': 'authenticated', 'mode': self.login_mode},
                'sections': sections, 'timestamp_basis': 'Harbour request/receipt time; modem event times are preserved as reported',
                'cause': 'not_established'}
