"""Persistent warning timers and Telegram delivery, independent of SSH polling."""
import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from . import auth, history, store

router = APIRouter(prefix='/api/notifications', dependencies=[Depends(auth.admin)])
KINDS = ('cpu', 'memory', 'disk', 'temperature', 'resource')
LABELS = dict(zip(KINDS, ('CPU', 'Memory', 'Storage', 'Temperature', 'Resources')))
guard = threading.RLock()
sending = threading.Lock()
DEFAULT = {'enabled': False, 'chat_id': '', 'token_encrypted': '', 'revision': '',
           'last_sent': None, 'last_error': '', 'next_attempt': 0, 'failures': 0}


class Rule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    server_id: str = Field(min_length=1, max_length=128)
    kind: Literal['cpu', 'memory', 'disk', 'temperature', 'resource']
    enabled: bool = False
    delay_seconds: int = Field(default=300, ge=0, le=604800)
    repeat_seconds: int = Field(default=3600, ge=0, le=2592000)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    enabled: bool = False
    chat_id: str = Field(default='', max_length=128)
    bot_token: SecretStr = Field(default=SecretStr(''), max_length=256)
    clear_token: bool = False
    rules: list[Rule] = Field(default_factory=list, max_length=2000)


def config():
    row = store.one("SELECT value FROM settings WHERE key='telegram'")
    return {**DEFAULT, **(json.loads(row['value']) if row else {})}


def write_config(con, value):
    con.execute("INSERT INTO settings VALUES ('telegram',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))


@router.get('')
def public_config():
    value = config()
    return {key: value[key] for key in ('enabled', 'chat_id', 'last_sent', 'last_error', 'next_attempt')} | {
        'token_saved': bool(value['token_encrypted']), 'demo': store.DEMO,
        'rules': store.rows('SELECT * FROM notification_rules'),
        'servers': store.rows('SELECT id,name,monitoring_enabled FROM servers ORDER BY sort_order,rowid')}


@router.put('')
def save_settings(body: Settings):
    token = body.bot_token.get_secret_value().strip()
    if token and not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,200}', token):
        raise HTTPException(422, 'Enter a valid Telegram bot token from BotFather.')
    if body.chat_id and not re.fullmatch(r'-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,63}', body.chat_id):
        raise HTTPException(422, 'Chat ID must be a numeric ID (including negative group IDs) or @channel username.')
    if token and body.clear_token:
        raise HTTPException(422, 'Choose either a replacement token or Remove saved token.')
    keys = [(r.server_id, r.kind) for r in body.rules]
    if len(set(keys)) != len(keys):
        raise HTTPException(422, 'Each server and warning type needs only one rule.')
    if any(0 < r.repeat_seconds < 60 for r in body.rules):
        raise HTTPException(422, 'Repeat intervals must be at least 60 seconds, or zero for no repeats.')
    with guard, store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        ids = {r[0] for r in con.execute('SELECT id FROM servers')}
        if any(r.server_id not in ids for r in body.rules):
            raise HTTPException(422, 'A server was removed. Reopen Notifications and try again.')
        value = config()
        encrypted = '' if body.clear_token else value['token_encrypted']
        if token:
            encrypted = store.cipher().encrypt(token.encode()).decode()
        if body.enabled and (not encrypted or not body.chat_id):
            raise HTTPException(422, 'Save a bot token and Chat ID before enabling Telegram.')
        destination_changed = (value['enabled'] != body.enabled or value['chat_id'] != body.chat_id
                               or encrypted != value['token_encrypted'])
        if destination_changed:
            con.execute('DELETE FROM notification_state')
            value.update(revision=secrets.token_hex(12), last_error='', next_attempt=0, failures=0, last_sent=None)
        value.update(enabled=body.enabled, chat_id=body.chat_id, token_encrypted=encrypted)
        old = {(r['server_id'], r['kind']): dict(r) for r in con.execute('SELECT * FROM notification_rules')}
        for rule in body.rules:
            data = rule.model_dump()
            key = (rule.server_id, rule.kind)
            if old.get(key) != data:
                con.execute('DELETE FROM notification_state WHERE server_id=? AND kind=?', key)
            con.execute('INSERT INTO notification_rules VALUES (?,?,?,?,?) ON CONFLICT(server_id,kind) DO UPDATE SET enabled=excluded.enabled,delay_seconds=excluded.delay_seconds,repeat_seconds=excluded.repeat_seconds',
                        (rule.server_id, rule.kind, rule.enabled, rule.delay_seconds, rule.repeat_seconds))
        for key in old.keys() - set(keys):
            con.execute('DELETE FROM notification_rules WHERE server_id=? AND kind=?', key)
        write_config(con, value)
    return public_config()


def invalidate(con, server_id, identities):
    """Changed warning settings require a fresh observation, per resource."""
    if not identities:
        return
    for row in con.execute('SELECT * FROM notification_state WHERE server_id=?', (server_id,)).fetchall():
        previous = json.loads(row['entities'])
        entities = {key: value for key, value in previous.items() if key not in identities}
        if entities == previous:
            continue
        if entities:
            con.execute('UPDATE notification_state SET entities=? WHERE server_id=? AND kind=?',
                        (json.dumps(entities), server_id, row['kind']))
        else:
            con.execute('DELETE FROM notification_state WHERE server_id=? AND kind=?', (server_id, row['kind']))


def observe(server, warnings, successful=True, now=None, unavailable=()):
    """Called once per resource sample. No network access on the poll worker."""
    now = time.time() if now is None else now
    with guard, store.db() as con:
        value = config()
        if not value['enabled'] or not server['monitoring_enabled']:
            con.execute('DELETE FROM notification_state WHERE server_id=?', (server['id'],))
            return
        rules = con.execute('SELECT * FROM notification_rules WHERE server_id=? AND enabled=1', (server['id'],)).fetchall()
        for rule in rules:
            key = (server['id'], rule['kind'])
            row = con.execute('SELECT * FROM notification_state WHERE server_id=? AND kind=?', key).fetchone()
            entities = json.loads(row['entities']) if row else {}
            if successful and row and now <= row['checked']:
                continue  # Ignore duplicate or out-of-order observations.
            if not successful or rule['kind'] in unavailable:
                # Break duration continuity without re-arming an already delivered issue.
                for entity in entities.values():
                    entity['since'] = None
                con.execute('UPDATE notification_state SET checked=0,entities=? WHERE server_id=? AND kind=?', (json.dumps(entities), *key))
                continue
            matches = {w['id']: w for w in warnings if w['id'].split(':', 1)[0] == rule['kind']}
            missing = {id_: {**entity, 'since': None} for id_, entity in entities.items() if id_ in unavailable}
            if not matches and not missing:
                con.execute('DELETE FROM notification_state WHERE server_id=? AND kind=?', key)
                continue
            max_gap = max(120, (server['poll_seconds'] or history.policy()['poll_seconds']) * 2 + 15)
            continuous = row and 0 < now - row['checked'] <= max_gap
            updated = missing
            for id_, warning in matches.items():
                previous = entities.get(id_, {})
                updated[id_] = {'since': previous.get('since') if continuous and previous.get('since') is not None else now,
                                'sent': previous.get('sent', False), 'title': warning['title'], 'detail': warning['detail']}
            con.execute('INSERT INTO notification_state(server_id,kind,entities,checked) VALUES (?,?,?,?) ON CONFLICT(server_id,kind) DO UPDATE SET entities=excluded.entities,checked=excluded.checked',
                        (*key, json.dumps(updated), now))


class DeliveryError(Exception):
    def __init__(self, message, retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bot credential to another host.


def send_message(value, message):
    if store.DEMO:
        return  # Isolated preview never contacts Telegram.
    try:
        token = store.cipher().decrypt(value['token_encrypted'].encode()).decode()
        payload = json.dumps({'chat_id': value['chat_id'], 'text': message,
                              'link_preview_options': {'is_disabled': True}}).encode()
        request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage',
                                         data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=10) as response:
                result = json.loads(response.read(65536))
        except urllib.error.HTTPError as error:
            try:
                with error:
                    result = json.loads(error.read(65536))
            except (ValueError, OSError):
                result = {'ok': False, 'error_code': error.code}
        if result.get('ok') is not True:
            code = result.get('error_code')
            messages = {400: 'Telegram rejected the message. Check the Chat ID and start a chat with the bot.',
                        401: 'Telegram rejected the bot token.', 403: 'The bot cannot post to this chat. Check its membership and permissions.',
                        429: 'Telegram rate limit reached; delivery will retry automatically.'}
            retry = max(3, min(86400, int(result.get('parameters', {}).get('retry_after', 60))))
            raise DeliveryError(messages.get(code, 'Telegram is unavailable; delivery will retry automatically.'), retry)
    except DeliveryError:
        raise
    except Exception:
        # urllib errors include the token-bearing URL; never persist or return them.
        raise DeliveryError('Could not reach Telegram. Check outbound HTTPS and the saved bot settings.') from None


def delivery_result(value, error=None, now=None):
    now = time.time() if now is None else now
    with store.db() as con:
        current = config()
        if current['revision'] != value['revision']:
            return
        if error:
            failures = min(current['failures'] + 1, 10)
            current.update(last_error=str(error), failures=failures,
                           next_attempt=now + max(error.retry_after, min(3600, 30 * 2 ** failures)))
        else:
            current.update(last_sent=now, last_error='', failures=0, next_attempt=now + 3)
        write_config(con, current)


def deliver_one(now=None):
    """At most one message every 3 seconds; pending conditions stay in SQLite."""
    now = time.time() if now is None else now
    if not sending.acquire(blocking=False):
        return
    try:
        with guard:
            value = config()
            if not value['enabled'] or value['next_attempt'] > now:
                return
            candidate = None
            rows = store.rows('SELECT n.*,r.delay_seconds,r.repeat_seconds,s.name,s.monitoring_enabled,s.connection_status,s.checked AS server_checked,s.poll_seconds FROM notification_state n JOIN notification_rules r USING(server_id,kind) JOIN servers s ON s.id=n.server_id WHERE r.enabled=1 ORDER BY COALESCE(n.last_sent,0),n.checked,n.server_id,n.kind')
            for row in rows:
                max_age = max(120, (row['poll_seconds'] or history.policy()['poll_seconds']) * 2 + 15)
                if not row['monitoring_enabled'] or row['connection_status'] != 'up' or not row['checked'] or now-row['checked'] > max_age:
                    continue
                entities = json.loads(row['entities'])
                due = {k: v for k, v in entities.items() if v['since'] is not None and row['checked'] - v['since'] >= row['delay_seconds']
                       and (not v['sent'] or (row['repeat_seconds'] and now-(row['last_sent'] or 0) >= row['repeat_seconds']))}
                if due:
                    candidate = row
                    # Limit UTF-16 size as well as characters, including non-ASCII names.
                    lines = [f"{row['name']} · {LABELS[row['kind']]} warning"]
                    included = {}
                    for key, entity in due.items():
                        line = f"{entity['title']}: {entity['detail']}"
                        if len(line) > 1000:
                            line = line[:999] + '…'
                        if len(('\n'.join(lines + [line])).encode('utf-16-le')) > 7800:
                            break
                        lines.append(line)
                        included[key] = entity
                    due = included
                    message = '\n'.join(lines)
                    break
            if candidate is None:
                return
        try:
            send_message(value, message)
        except DeliveryError as error:
            with guard:
                delivery_result(value, error, now)
            return
        with guard:
            delivery_result(value, now=now)
            if config()['revision'] != value['revision']:
                return
            key = (candidate['server_id'], candidate['kind'])
            current = store.one('SELECT * FROM notification_state WHERE server_id=? AND kind=?', key)
            if current:
                entities = json.loads(current['entities'])
                for id_, sent in due.items():
                    if id_ in entities and entities[id_]['since'] == sent['since']:
                        entities[id_]['sent'] = True
                store.execute('UPDATE notification_state SET entities=?,last_sent=? WHERE server_id=? AND kind=?', (json.dumps(entities), now, *key))
    finally:
        sending.release()


def delivery_loop(stop):
    while not stop.wait(3):
        try:
            deliver_one()
        except Exception:
            logging.error('Telegram notification worker failed; retrying on the next pass.')


@router.post('/test')
def test_message():
    if not sending.acquire(blocking=False):
        raise HTTPException(409, 'A Telegram message is being delivered. Try again in a moment.')
    try:
        with guard:
            value = config()
            if not value['token_encrypted'] or not value['chat_id']:
                raise HTTPException(422, 'Save a bot token and Chat ID first.')
            if value['next_attempt'] > time.time():
                raise HTTPException(429, 'Telegram delivery is cooling down. Try again later.')
        try:
            send_message(value, 'Harbour · Test notification\nTelegram delivery is working.')
        except DeliveryError as error:
            with guard:
                delivery_result(value, error)
            raise HTTPException(502, str(error)) from None
        with guard:
            delivery_result(value)
        return {'ok': True, 'simulated': store.DEMO}
    finally:
        sending.release()
