import base64
import copy
import io
import json
import os
import plistlib
import socket
import threading
import tempfile
import time
import zipfile

import pytest

from test_harbour import client
from harbour import app as module, collector_install, login_agent, logins, remote_probe, ssh, store


KEY = 'SHA256:abcdefgh1234'


def event(n=1):
    return dict(occurred_at=1700000000+n, service='ssh', result='success', event_type='authentication',
                username='example-user', method='publickey', source_ip='2001:db8::42', source_port=54422,
                key_fingerprint=KEY, key_algorithm='ED25519', evidence='test event')


def batch(tmp_path, n=1):
    spool = login_agent.Spool(tmp_path/'agent.sqlite')
    for i in range(n):
        spool.append(event(i), str(i))
    result = spool.exchange()
    result['status'] = login_agent.Collector(spool, platform='linux').status()
    return spool, result


def clear():
    store.execute('DELETE FROM host_login_events')
    store.execute('DELETE FROM host_login_state')


def collect(result, id_='atlas', valid=lambda r: r is not None):
    server = module.get_server(id_)
    logins.collect(server, result, module.connection_signature(server), valid)


@pytest.mark.parametrize('message,expected', [
    ('Accepted publickey for example-user from 2001:db8::1 port 12345 ssh2: ED25519 '+KEY,
     dict(result='success', method='publickey', username='example-user', source_ip='2001:db8::1', source_port=12345, key_fingerprint=KEY, credential_verified=True)),
    ('Failed publickey for invalid user guest from 192.0.2.1 port 123 ssh2: RSA '+KEY,
     dict(result='failure', invalid_user=True, credential_verified=False, username='guest', key_algorithm='RSA')),
    ('Partial publickey for example-user from 192.0.2.1 port 123 ssh2: ED25519 '+KEY,
     dict(result='partial', credential_verified=True)),
    ('Failed password for user.with.dots from 192.0.2.1 port 123 ssh2', dict(method='password', username='user.with.dots', key_fingerprint=None)),
    ('Accepted keyboard-interactive/pam for example-user from 192.0.2.1 port 123 ssh2', dict(method='keyboard-interactive/pam')),
    ('Invalid user guest from 192.0.2.1 port 123', dict(result='invalid_user', username='guest')),
    ('Disconnected from user example-user 192.0.2.1 port 123', dict(event_type='disconnect', username='example-user', source_ip='192.0.2.1')),
    ('Connection closed by 192.0.2.1 port 123 [preauth]', dict(event_type='disconnect', username=None, source_ip='192.0.2.1')),
    ('pam_unix(sshd:session): session opened for user example-user(uid=1000) by (uid=0)', dict(event_type='session_start', result='opened', uid=1000)),
    ('pam_unix(sshd:session): session closed for user example-user', dict(result='closed')),
    ('pam_unix(sshd:auth): authentication failure; logname= uid=0 rhost=192.0.2.1 user=example-user', dict(result='failure', username='example-user', source_ip='192.0.2.1')),
    ('Accepted publickey for <private> from <private> port 123 ssh2: <private>', dict(username=None, source_ip=None, key_fingerprint=None)),
    ('Unable to negotiate with 192.0.2.1 port 123: no matching cipher found', dict(result='rejected', username=None)),
])
def test_auth_parser(message, expected):
    actual = login_agent.parse_auth(message)
    assert actual is not None
    for k, v in expected.items():
        assert actual[k] == v


def test_certificate_fields_and_unknown_records():
    actual = login_agent.parse_auth('Accepted publickey for example-user from 192.0.2.1 port 123 ssh2: ED25519-CERT '+KEY+' ID workstation (serial 82) CA ED25519 SHA256:ca123')
    assert (actual['certificate_id'], actual['certificate_serial'], actual['ca_fingerprint']) == ('workstation', '82', 'SHA256:ca123')
    assert login_agent.parse_auth('Unrelated text') is None
    assert login_agent.parse_auth('Accepted password for root from 192.0.2.1 port 123 ssh2', 'unknown') is None


def test_durable_batch_retry_ack_and_new_arrivals(tmp_path):
    spool, first = batch(tmp_path, 3)
    # Reading never marks seen. Retry after process restart returns identical IDs.
    assert spool.exchange()['events'] == first['events']
    reopened = login_agent.Spool(tmp_path/'agent.sqlite')
    assert reopened.exchange()['collector_id'] == first['collector_id']
    reopened.append(event(4), '4')
    after = reopened.exchange(first['acknowledgement'])
    assert [e['seq'] for e in after['events']] == [4]
    assert after['acked_through'] == 3 and after['acknowledged_at'] > 0
    # Losing ACK response is harmless; replaying an older ACK never advances/re-stamps it.
    duplicate = reopened.exchange(first['acknowledgement'])
    assert duplicate['acknowledged_at'] == after['acknowledged_at']
    assert duplicate['events'] == after['events']
    with pytest.raises(ValueError):
        reopened.exchange({**first['acknowledgement'], 'through': 9999})
    with pytest.raises(ValueError):
        reopened.exchange({**first['acknowledgement'], 'collector_id': '0'*32})


def test_bounded_queue_records_unacknowledged_loss(tmp_path):
    spool, first = batch(tmp_path, 5)
    spool.max_events = 3
    spool.maintain()
    result = spool.exchange()
    assert result['dropped'] == 2 and [e['seq'] for e in result['events']] == [3,4,5]
    spool.exchange(result['acknowledgement'])
    spool.append(event(6), '6')
    spool.maintain()
    assert spool.exchange()['dropped'] == 2  # Acknowledged records removed first.
    spool.maintain(time.time()+86401)
    assert len(spool.exchange()['events']) == 1


def test_journal_cursor_and_source_provenance(tmp_path):
    spool = login_agent.Spool(tmp_path/'agent.sqlite')
    collector = login_agent.Collector(spool, platform='linux')
    record = {'__CURSOR':'one', '_UID':'0', '_PID':'88','SYSLOG_IDENTIFIER':'sshd-auth',
              '__REALTIME_TIMESTAMP':'1700000000000000', '_BOOT_ID':'boot',
              'MESSAGE':'Accepted publickey for example-user from 192.0.2.1 port 100 ssh2: ED25519 '+KEY}
    collector.journal_record(record); collector.journal_record(record)
    assert len(spool.exchange()['events']) == 1 and spool.get('journal_cursor') == 'one'
    collector.journal_record({**record, '__CURSOR':'two', '_UID':'1000'})
    assert len(spool.exchange()['events']) == 1 and spool.get('journal_cursor') == 'two'


def test_macos_replay_deduplicates_and_preserves_unknown_time(tmp_path):
    spool = login_agent.Spool(tmp_path/'agent.sqlite')
    collector = login_agent.Collector(spool, platform='darwin')
    line = dict(eventMessage='Failed password for example-user from 192.0.2.2 port 200 ssh2',
                processImagePath='/usr/libexec/sshd-session',processID=82,threadID=17,
                timestamp='2026-10-08 01:02:03.456789+0100',machTimestamp=12345,bootUUID='boot')
    collector.mac_record(line); collector.mac_record(line)
    assert len(spool.exchange()['events']) == 1
    assert spool.exchange()['events'][0]['occurred_at'] == login_agent.timestamp(line['timestamp'])
    assert collector.status()['coverage'] == ['ssh-logs']
    collector.state('unified-log','error','permission denied')
    assert collector.status()['sources']['unified-log']['state'] == 'error'


def test_local_socket_roundtrip_and_no_command_interface(tmp_path):
    spool, first = batch(tmp_path)
    collector = login_agent.Collector(spool, platform='linux')
    short = tempfile.TemporaryDirectory(prefix='hbl-',dir='/tmp')
    path = short.name+'/c.sock'
    thread = threading.Thread(target=login_agent.serve, args=(collector,path))
    thread.start()
    deadline = time.time()+2
    while not os.path.exists(path) and time.time()<deadline:
        time.sleep(.01)
    def query(request):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(2); client.connect(path)
            client.sendall(json.dumps(request).encode()+b'\n')
            return json.loads(client.makefile('rb').readline())
    try:
        assert os.stat(path).st_mode & 0o777 == 0o600
        result = query({'operation':'exchange'})
        assert result['events'][0]['username'] == 'example-user'
        assert query({'operation':'execute','command':'id'})['error'] == 'Unknown operation'
        assert query({'operation':'exchange','acknowledgement':result['acknowledgement']})['events'] == []
    finally:
        collector.stop.set(); thread.join(3)
    assert not thread.is_alive() and not os.path.exists(path)
    short.cleanup()


def test_harbour_commits_before_ack_and_deduplicates(client,tmp_path):
    clear(); spool, result = batch(tmp_path, 3)
    server = module.get_server('atlas'); signature = module.connection_signature(server)
    assert logins.payload('atlas',signature) == {'operation':'resources'}
    collect(result); collect(result)
    rows = store.rows('SELECT * FROM host_login_events')
    assert len(rows) == 3 and all(r['acknowledged_at'] is None for r in rows)
    first_seen = rows[0]['collected_at']
    request = logins.payload('atlas',signature)
    assert request['logins_ack'] == result['acknowledgement']
    response = spool.exchange(request['logins_ack']); response['status'] = {}
    collect(response)
    rows = store.rows('SELECT * FROM host_login_events')
    assert rows[0]['collected_at'] == first_seen
    assert all(r['acknowledged_at'] == response['acknowledged_at'] for r in rows)
    assert logins.payload('atlas',signature) == {'operation':'resources'}


def test_invalid_batch_and_stale_connection_do_not_advance_ack(client,tmp_path):
    clear(); spool, result = batch(tmp_path, 2)
    collect(result)
    old_ack = store.one('SELECT acknowledgement FROM host_login_state')['acknowledgement']
    invalid = copy.deepcopy(result); invalid['events'][1]['seq'] = 1
    collect(invalid)
    assert store.one('SELECT acknowledgement FROM host_login_state')['acknowledgement'] == old_ack
    assert logins.summary(module.get_server('atlas'))['state'] == 'error'
    clear()
    collect(result, valid=lambda _:False)
    assert store.rows('SELECT * FROM host_login_events') == []
    collect(result)
    store.execute("UPDATE servers SET host='new.invalid' WHERE id='atlas'")
    signature = module.connection_signature(module.get_server('atlas'))
    assert logins.payload('atlas',signature) == {'operation':'resources'}
    assert logins.summary(module.get_server('atlas'))['state'] == 'pending'


def test_failed_database_transaction_never_queues_ack(client,tmp_path,monkeypatch):
    clear(); spool,result = batch(tmp_path,2)
    with store.db() as con:
        con.execute("CREATE TRIGGER fail_login BEFORE INSERT ON host_login_events WHEN NEW.seq=2 BEGIN SELECT RAISE(ABORT,'disk fixture'); END")
    with pytest.raises(Exception,match='disk fixture'):
        collect(result)
    assert store.rows('SELECT * FROM host_login_events') == []
    assert store.rows('SELECT * FROM host_login_state') == []
    assert len(spool.exchange()['events']) == 2


def test_resource_poll_piggybacks_acks_without_affecting_health(client,tmp_path,monkeypatch):
    clear(); spool,result = batch(tmp_path)
    metrics=json.loads(module.get_server('atlas')['snapshot'])['metrics']; calls=[]
    def request(server,payload,**kwargs):
        calls.append(payload)
        value=spool.exchange(payload.get('logins_ack')); value['status']={}
        return {'metrics':copy.deepcopy(metrics),'logins':value}
    monkeypatch.setattr(store,'DEMO',False); monkeypatch.setattr(ssh,'request',request)
    for _ in range(2):
        lock=module.resource_lock('atlas'); lock.acquire()
        module.poll_resources(module.get_server('atlas'),lock)
    assert calls[0]=={'operation':'resources'} and 'logins_ack' in calls[1]
    assert module.get_server('atlas')['connection_status']=='up'
    assert store.one('SELECT acknowledged_at FROM host_login_events')['acknowledged_at']
    monkeypatch.setattr(ssh,'request',lambda *a,**k:{'metrics':copy.deepcopy(metrics),'logins':{'state':'error','detail':'Collector stopped'}})
    lock=module.resource_lock('atlas');lock.acquire();module.poll_resources(module.get_server('atlas'),lock)
    assert module.get_server('atlas')['error'] is None
    assert logins.summary(module.get_server('atlas'))['state']=='error'


def test_api_pagination_filter_isolation_delete_and_download(client,tmp_path):
    clear(); spool,result=batch(tmp_path,105); collect(result)
    page=client.get('/api/servers/atlas/logins').json()
    assert len(page['events'])==100 and page['next_before']
    assert 'acknowledgement' not in page['status'] and 'token' not in json.dumps(page)
    older=client.get('/api/servers/atlas/logins',params={'before':page['next_before']}).json()
    assert len(older['events'])==5
    assert client.get('/api/servers/atlas/logins?search=2001:db8').json()['events']
    assert client.get('/api/servers/atlas/logins?outcome=failure').json()['events']==[]
    assert client.get('/api/servers/luna/logins').json()['events']==[]
    assert client.get('/api/servers/absent/logins').status_code==404
    assert client.get('/api/servers/atlas/logins?outcome=bad').status_code==422
    data=client.get('/api/login-collector/download');assert data.status_code==200
    with zipfile.ZipFile(io.BytesIO(data.content)) as archive:
        assert 'LOGIN_COLLECTOR.md' in archive.namelist()
        assert 'login_events.m' in archive.namelist()
    assert client.delete('/api/servers/atlas').status_code==200
    assert not store.rows('SELECT * FROM host_login_events')


def test_login_history_requires_auth_and_installer_requires_admin(client):
    store.execute("UPDATE users SET role='user'")
    assert client.get('/api/servers/atlas/logins').status_code==200
    assert client.get('/api/login-collector/download').status_code==403
    client.cookies.clear()
    assert client.get('/api/servers/atlas/logins').status_code==401


def test_service_definitions_no_shell_and_python_isolation():
    path,body=collector_install.definition('linux','/usr/bin/python3','harbour')
    assert path.name=='harbour-logins.service'
    assert '"-I" "-S"' in body.decode() and 'RestrictAddressFamilies=AF_UNIX' in body.decode()
    path,body=collector_install.definition('darwin','/opt/homebrew/bin/python3','example-user',endpoint_helper=True)
    value=plistlib.loads(body)
    assert value['UserName']=='root' and value['ProgramArguments'][1:3]==['-I','-S']
    assert value['ProgramArguments'][-1]=='/usr/local/libexec/harbour-logins/login-events'
    assert collector_install.systemd_quote('a%b"c')=='"a%%b\\"c"'


def eventually(predicate):
    deadline=time.time()+4
    while not predicate() and time.time()<deadline:
        time.sleep(.02)
    assert predicate()


def test_auth_file_rotation_truncation_and_offline_recovery(tmp_path):
    path=tmp_path/'auth.log';path.write_text('old history\n')
    spool=login_agent.Spool(tmp_path/'agent.sqlite')
    def start():
        collector=login_agent.Collector(spool,platform='linux',auth_file=str(path))
        worker=threading.Thread(target=collector.follow_file);worker.start()
        return collector,worker
    def append(user):
        with path.open('a') as f:
            f.write('2026-10-08T01:02:03+01:00 host sshd[77]: Accepted password for '+user+' from 192.0.2.1 port 200 ssh2\n')
    def users():
        return [e.get('username') for e in spool.exchange()['events'] if e['service']=='ssh']
    collector,worker=start()
    try:
        eventually(lambda:spool.get('file_cursor') is not None)
        append('first');eventually(lambda:'first' in users())
        path.rename(tmp_path/'auth.log.1');path.write_text('')
        append('rotated');eventually(lambda:'rotated' in users())
        path.write_text('')
        eventually(lambda:spool.get('file_cursor')['offset']==0)
        append('truncated');eventually(lambda:'truncated' in users())
    finally:
        collector.stop.set();worker.join(3)
    append('offline')
    collector,worker=start()
    try:
        eventually(lambda:'offline' in users())
        assert users()==['first','rotated','truncated','offline']
    finally:
        collector.stop.set();worker.join(3)


def test_macos_failed_backfill_does_not_advance_durable_boundary(tmp_path,monkeypatch):
    spool=login_agent.Spool(tmp_path/'agent.sqlite')
    collector=login_agent.Collector(spool,platform='darwin')
    spool.checkpoint('mac_replay_from',1700000000)
    def failed(argv):
        # A live stream can already have newer records. It cannot advance replay.
        spool.checkpoint('mac_timestamp',1800000000)
        collector.stop.set()
        raise RuntimeError('log show interrupted')
        yield
    monkeypatch.setattr(collector,'lines',failed)
    collector.follow_macos_replay()
    assert spool.get('mac_replay_from')==1700000000
    assert collector.status()['sources']['unified-replay']['state']=='error'


def test_monitoring_key_label_requires_matching_username_and_key(client,tmp_path,monkeypatch):
    clear();spool,result=batch(tmp_path)
    monkeypatch.setattr(store,'DEMO',False)
    key=client.post('/api/keys',json={}).json()
    store.execute("UPDATE servers SET key_id=? WHERE id='atlas'",(key['id'],))
    server=module.get_server('atlas')
    result['events'][0].update(username=server['username'],key_fingerprint=logins.key_fingerprint(server),credential_verified=True)
    collect(result)
    assert client.get('/api/servers/atlas/logins').json()['events'][0]['harbour_key']==1
    assert client.get('/api/servers/atlas/logins?hide_harbour=true').json()['events']==[]
    store.execute("UPDATE servers SET auth_method='password' WHERE id='atlas'")
    assert logins.key_fingerprint(module.get_server('atlas')) is None


def test_history_retention_is_based_on_receipt_and_upgrade_is_idempotent(client,tmp_path):
    clear();_,result=batch(tmp_path);collect(result)
    logins.compact()
    assert len(store.rows('SELECT * FROM host_login_events'))==1  # Old event newly received.
    store.initialize()
    assert len(store.rows('SELECT * FROM host_login_events'))==1
    store.execute('UPDATE host_login_events SET collected_at=?',(time.time()-91*86400,))
    logins.compact()
    assert not store.rows('SELECT * FROM host_login_events')


def test_duration_only_for_unambiguous_sessions_in_same_boot(tmp_path):
    spool=login_agent.Spool(tmp_path/'agent.sqlite')
    start=dict(event(),source='journal',boot_id='boot-one',pid=12,method='pam',event_type='session_start',result='opened',occurred_at=100)
    end={**start,'event_type':'session_end','result':'closed','occurred_at':160}
    spool.append(start,'start');spool.append(start,'start');spool.append(end,'end')
    assert spool.exchange()['events'][-1]['duration_seconds']==60
    spool.append(start,'start2');spool.append(start,'overlap');spool.append(end,'end2')
    assert 'duration_seconds' not in spool.exchange()['events'][-1]
    spool.append(start,'start3');spool.append({**end,'boot_id':'boot-two'},'end3')
    assert 'duration_seconds' not in spool.exchange()['events'][-1]


def test_replay_after_host_retention_does_not_duplicate_harbour_history(client,tmp_path):
    clear();spool,result=batch(tmp_path)
    collect(result)
    spool.exchange(result['acknowledgement'])
    spool.maintain(time.time()+86401)
    spool.append(event(0),'0')  # Same native source record, newer transport sequence.
    replay=spool.exchange();replay['status']={}
    assert replay['events'][0]['seq']!=result['events'][0]['seq']
    collect(replay)
    assert len(store.rows('SELECT * FROM host_login_events'))==1
    assert logins.payload('atlas',module.connection_signature(module.get_server('atlas')))['logins_ack']['through']==replay['events'][0]['seq']
