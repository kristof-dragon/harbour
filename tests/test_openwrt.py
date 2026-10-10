# Synthetic device identity and documentation addresses; no real site data.
import copy
import json
import socket
import struct
import subprocess
import threading
import time

import pytest

from harbour import app, logins, network, network_recorder as recorder, openwrt, ssh, store
from test_harbour import client


def fixture_reading(uptime=100, ticks=100, retries=10, boot='boot-one'):
    fields = {
        'board': json.dumps({'model':'Example MT7621 router','board_name':'example,mt7621-router',
            'system':'MediaTek MT7621','kernel':'6.6.0',
            'release':{'distribution':'OpenWrt','version':'24.10.0','revision':'r00000-example',
                       'description':'OpenWrt 24.10.0 r00000-example'}}),
        'uptime':f'{uptime} 70', 'boot':boot,
        'cpu':f'cpu {ticks} 0 0 {ticks*2} 0 0 {ticks} 0\ncpu0 {ticks} 0 0 {ticks*2} 0 0 {ticks} 0',
        'memory':'MemTotal: 256000 kB\nMemAvailable: 200000 kB', 'load':'0.2 0.3 0.4 1/50 100',
        'interfaces':f' wan: {ticks*1000} 10 0 0 0 0 0 0 {ticks*500} 20 0 0 0 0 0 0',
        'tools':'/usr/sbin/iw\n/usr/sbin/tc\n/bin/ping\n/sbin/logread',
        'network':'{"interface":[{"interface":"wan","l3_device":"wan","proto":"dhcp"}]}',
        'station:phy1-ap0':f'Station aa:bb:cc:dd:ee:ff (on phy1-ap0)\n\tsignal: -53 dBm\n\ttx retries: {retries}\n\ttx failed: 1\n\ttx packets: 200\n\ttx bitrate: 433.3 MBit/s VHT-MCS 9\n\tconnected time: {uptime} seconds',
        'survey:phy1-ap0':f'Survey data from phy1-ap0\n frequency: 5180 MHz [in use]\n noise: -94 dBm\n channel active time: {uptime*1000} ms\n channel busy time: {uptime*200} ms',
        'queues':'qdisc fq_codel 0: dev wan root refcnt 2\n Sent 100 bytes 2 pkt (dropped 1, overlimits 0 requeues 0)\n backlog 2Kb 2p',
        'end':''}
    return '\n'.join('@@HARBOUR:'+key+'\n'+value for key,value in fields.items())


def test_model_build_capabilities_and_read_only_profile():
    m = openwrt.parse(fixture_reading(), 1000)
    assert m['model']=='Example MT7621 router'
    assert m['openwrt']['board']['release']['revision']=='r00000-example'
    assert m['openwrt']['profile']=='mt7621-mt76'
    assert m['openwrt']['capabilities']['client_retries']
    assert not m['openwrt']['capabilities']['ethernet_details']
    assert m['openwrt']['stations'][0]['rx_mbps'] is None
    assert m['openwrt']['queue_stats'][0]['backlog_bytes']==2000
    assert m['cores']==1 and m['cpu'] is None
    assert m['disks']==[] and m['docker'] is None
    subprocess.run(['/bin/sh','-n'], input=openwrt.SCRIPT, text=True, check=True)
    assert all(word not in openwrt.SCRIPT for word in ('uci set','uci commit','reboot','ifdown','opkg','apk add','sysupgrade','python'))


def test_counter_deltas_reset_and_missing_capabilities():
    a,b = openwrt.parse(fixture_reading()),openwrt.parse(fixture_reading(101,110,13))
    openwrt.interval(a,b)
    assert b['cpu']==50
    assert b['openwrt']['softirq_percent']['cpu0']==25
    assert b['openwrt']['interfaces']['wan']['rx_mbps']==.08
    assert b['openwrt']['stations'][0]['tx_retries_delta']==3
    assert b['openwrt']['radio_stats']['phy1-ap0']['busy_percent']==20
    c=openwrt.parse(fixture_reading(5,1,1,'new-boot'))
    openwrt.interval(b,c)
    assert c['cpu'] is None and c['openwrt']['interfaces']['wan']['rx_mbps'] is None
    assert c['openwrt']['stations'][0]['tx_retries_delta'] is None
    no_tools=fixture_reading().replace('/usr/sbin/iw','').replace('/usr/sbin/tc','')
    assert not openwrt.parse(no_tools)['openwrt']['capabilities']['wireless']
    no_cpu=fixture_reading().replace('cpu 100 0 0 200 0 0 100 0\ncpu0 100 0 0 200 0 0 100 0','')
    unavailable=openwrt.parse(no_cpu)
    openwrt.interval(a,unavailable)
    assert unavailable['cores'] is None and unavailable['cpu'] is None
    no_boot_a,no_boot_b=openwrt.parse(fixture_reading(100,100,10,'')),openwrt.parse(fixture_reading(101,110,13,''))
    openwrt.interval(no_boot_a,no_boot_b)
    assert no_boot_b['openwrt']['interfaces']['wan']['rx_mbps'] is None
    with pytest.raises(ValueError):openwrt.parse(fixture_reading().replace('OpenWrt','SomethingElse'))
    with pytest.raises(ValueError):openwrt.parse(fixture_reading().replace('@@HARBOUR:end',''))


class FakeSocket:
    def __init__(self,*args):self.sent=[];self.incoming=[];self.closed=False
    def connect(self,address):self.address=address
    def setblocking(self,value):pass
    def send(self,packet):self.sent.append(packet)
    def recv(self,_):
        if self.incoming:return self.incoming.pop(0)
        raise BlockingIOError()
    def close(self):self.closed=True


def response(packet,kind=0):
    return bytes([kind,0])+packet[2:]


def test_echo_concurrent_attempts_timeout_late_and_spoof():
    rows=[];probe=recorder.Echo('1.1.1.1',rows.append,FakeSocket)
    probe.send(10,100);probe.send(10.25,100.25)
    assert recorder.checksum(probe.socket.sent[0])==0
    probe.socket.incoming=[response(probe.socket.sent[1]),b'\0'*24]
    probe.receive(10.3)
    assert rows[0]['sequence']==2 and rows[0]['rtt_ms']==50
    probe.receive(12.01)
    assert rows[-1]['status']=='timeout' and rows[-1]['sequence']==1
    probe.socket.incoming=[response(probe.socket.sent[0])]
    probe.receive(12.5)
    assert rows[-1]['status']=='late_reply' and rows[-1]['at']==100
    probe.socket.incoming=[response(probe.socket.sent[0])]
    probe.receive(13)
    assert len(rows)==3
    probe.send(14,104)
    probe.socket.incoming=[response(probe.socket.sent[-1])]
    probe.receive(16.1)
    assert [r['status'] for r in rows[-2:]]==['timeout','late_reply']
    probe.close();assert probe.socket.closed


def test_ipv6_echo_and_socket_setup_failure():
    rows=[];probe=recorder.Echo('2606:4700:4700::1111',rows.append,FakeSocket)
    probe.send(1,100);assert probe.socket.sent[0][0]==128
    probe.socket.incoming=[response(probe.socket.sent[0],129)];probe.receive(1.01)
    assert rows[0]['family']==6
    probe.close()
    class Denied(FakeSocket):
        def connect(self,_):raise PermissionError()
    with pytest.raises(PermissionError):recorder.Echo('1.1.1.1',rows.append,Denied)


@pytest.mark.parametrize('value',['1.1.1.1; reboot','example.com','127.0.0.1','224.0.0.1','0.0.0.0','fe80::1%eth0'])
def test_targets_are_validated_ip_data(value):
    with pytest.raises(ValueError):network.Settings(targets=[value])


def test_openwrt_api_read_only_collection_and_persisted_identity(client,monkeypatch):
    assert client.put('/api/servers/atlas/type',json={'server_type':'openwrt'}).status_code==200
    monkeypatch.setattr(store,'DEMO',False)
    operations=[]
    def collect(server,payload,**kwargs):
        operations.append(payload['operation'])
        return {'metrics':openwrt.parse(fixture_reading()),'services':[],'recording':{'mode':'openwrt'}}
    monkeypatch.setattr(ssh,'request',collect)
    app.refresh_server('atlas')
    assert operations==['resources']
    result=client.get('/api/dashboard').json()['servers'][0]
    assert result['metrics']['model']=='Example MT7621 router' and result['services']==[]
    for path,body in [('/plan',{'action':'restart','targets':[]}),('/collector/resources',{'action':'install'}),('/collector/logins',{'action':'push'})]:
        assert client.post('/api/servers/atlas'+path,json=body).status_code==400
    assert client.get('/api/servers/atlas/network').status_code==200
    assert client.get('/api/servers/boreal/network').status_code in (400,404)


def enable(client,monkeypatch):
    client.put('/api/servers/atlas/type',json={'server_type':'openwrt'})
    monkeypatch.setattr(store,'DEMO',False)
    assert client.put('/api/servers/atlas/network',json={'enabled':True,'interval':.25,'targets':['1.1.1.1']}).status_code==200
    server=store.one('SELECT * FROM servers WHERE id="atlas"')
    return server,logins.connection_signature(server),network.config('atlas')[1]


def test_fast_history_preserves_samples_and_rejects_stale_writes(client,monkeypatch):
    server,signature,revision=enable(client,monkeypatch)
    now=time.time()
    rows=[{'at':now+i*.25,'kind':'probe','target':'1.1.1.1','source':'recorder',
           'sequence':i,'status':'timeout' if i==1 else 'reply','rtt_ms':None if i==1 else 20} for i in range(3)]
    assert network.persist('atlas',rows,{'state':'recording'},signature,revision)
    assert len(network.samples('atlas',now-1,now+1)[0])==3
    assert not network.persist('atlas',rows,{},signature,'outdated')
    store.execute('UPDATE servers SET monitoring_enabled=0 WHERE id="atlas"')
    assert not network.persist('atlas',rows,{},signature,revision)
    store.execute('UPDATE servers SET monitoring_enabled=1,host="changed" WHERE id="atlas"')
    assert not network.persist('atlas',rows,{},signature,revision)


def test_incident_archive_survives_raw_pruning_and_export(client,monkeypatch):
    server,signature,revision=enable(client,monkeypatch)
    at=time.time()-400
    row={'at':at,'kind':'probe','target':'1.1.1.1','status':'timeout','source':'recorder','rtt_ms':None}
    network.persist('atlas',[row],{},signature,revision)
    event=network.mark('atlas','Video degraded',measured=at)
    network.maintain('atlas')
    store.execute('DELETE FROM network_samples')
    response=client.get('/api/servers/atlas/network/export/'+event)
    assert response.status_code==200 and response.json()['samples'][0]['status']=='timeout'
    assert response.json()['incident']['complete']==1
    assert 'password' not in response.text and 'private_key' not in response.text


def test_router_type_never_uses_python_or_exec_operations(client,monkeypatch):
    monkeypatch.setattr(store,'DEMO',False)
    server=store.one('SELECT * FROM servers WHERE id="atlas"');server['server_type']='openwrt'
    monkeypatch.setattr(openwrt.Session,'exchange',lambda self,*args:{'metrics':{'model':'test'}})
    monkeypatch.setattr(ssh.ResourceSession,'start',lambda *args:pytest.fail('Python collector invoked'))
    assert ssh.request(server,{'operation':'resources'})['metrics']['model']=='test'
    with pytest.raises(ValueError):ssh.request(server,{'operation':'execute','action':'restart'})
    with pytest.raises(ssh.KeyInstallError):ssh.install_key(server,'unused','unused')
    ssh.close_resources(server['id'])


def test_network_permissions_and_invalid_settings(client,monkeypatch):
    enable(client,monkeypatch)
    assert client.put('/api/servers/atlas/network',json={'interval':.01}).status_code==422
    assert client.put('/api/servers/atlas/network',json={'wan_device':'wan;reboot'}).status_code==422
    client.post('/api/users',json={'name':'reader','password':'reader-password-strong','role':'user'})
    response=client.post('/api/login',json={'name':'reader','password':'reader-password-strong'}).json()
    client.headers['X-CSRF-Token']=response['csrf']
    assert client.get('/api/servers/atlas/network').status_code==200
    assert client.put('/api/servers/atlas/network',json={'enabled':True}).status_code==403
    assert client.post('/api/servers/atlas/network/markers',json={'label':'test'}).status_code==403


def test_slow_router_collection_shutdown_cancels(client, monkeypatch):
    from harbour.network_collector import Collector
    import queue
    server, _, revision = enable(client, monkeypatch)
    started = threading.Event()
    def slow_router(self, server, payload=None, cancel=None):
        started.set()
        while not cancel.is_set():
            time.sleep(.005)
        raise ssh.Cancelled()
    monkeypatch.setattr(openwrt.Session, 'exchange', slow_router)
    settings = network.config('atlas')[0]
    settings['router_probes'] = False
    collector = Collector(server, settings, 'example-run', queue.Queue(16), threading.Event())
    try:
        assert started.wait(1)
    finally:
        collector.close()
    assert not any(t.is_alive() for t in collector.threads)


def test_recorder_restart_keeps_rows_and_distinct_run_configuration(client, monkeypatch):
    from harbour.network_storage import commit
    server, signature, revision = enable(client, monkeypatch)
    monkeypatch.setattr(recorder.Worker, 'start', lambda *args: None)
    settings = network.config('atlas')[0]
    for kind in ('probe', 'gap'):
        worker = recorder.Worker(None, server, settings, revision)
        worker.buffer.append(json.dumps({'at':time.time(), 'kind':kind, 'run_id':worker.run_id,
            'target':'192.0.2.10', 'status':'reply', 'rtt_ms':4}).encode())
        assert commit(worker.job())
        worker.retire()
    rows, _ = network.samples('atlas', time.time()-5, time.time())
    assert len([r for r in rows if r['kind'] != 'recorder_health']) == 2
    assert len({r['run_id'] for r in rows}) == 2
    assert len(network.run_metadata('atlas', rows)) == 2
    network.initialize()
    assert len(network.samples('atlas', time.time()-5, time.time())[0]) == 4


def test_storage_limits_keep_latest_raw_samples(client,monkeypatch):
    server,signature,revision=enable(client,monkeypatch)
    settings=network.config('atlas')[0]
    settings['max_rows']=3
    assert client.put('/api/servers/atlas/network',json=settings).status_code==200
    now=time.time()
    rows=[{'at':now-10+i,'kind':'probe','target':'1.1.1.1','status':'reply','rtt_ms':i} for i in range(10)]
    network.persist('atlas',rows,{},signature,revision)
    network.maintain('atlas')
    assert [r['rtt_ms'] for r in network.samples('atlas',now-20,now)[0]]==[7,8,9]


def test_pinned_openwrt_ssh_session_reused_and_output_is_bounded(client,monkeypatch):
    """Actual loopback SSH transport; target returns a recorded OpenWrt fixture."""
    import paramiko
    monkeypatch.setattr(store,'DEMO',False)
    key=paramiko.ECDSAKey.generate()
    calls,transports,channels,threads=[],[],[],[]
    stop=threading.Event()
    class Host(paramiko.ServerInterface):
        def check_auth_password(self,*args):return paramiko.AUTH_SUCCESSFUL
        def check_channel_request(self,kind,id_):return paramiko.OPEN_SUCCEEDED
        def check_channel_exec_request(self,channel,command):
            calls.append(command);channels.append(channel)
            def reply():
                channel.sendall(fixture_reading().encode())
                channel.send_exit_status(0)
                channel.shutdown_write()
            t=threading.Thread(target=reply);t.start();threads.append(t)
            return True
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.1)
        def accept():
            while not stop.is_set():
                try:connection,_=listener.accept()
                except TimeoutError:continue
                transport=paramiko.Transport(connection);transports.append(transport)
                transport.add_server_key(key);transport.start_server(server=Host())
        thread=threading.Thread(target=accept);thread.start()
        server={'host':'127.0.0.1','port':listener.getsockname()[1],'username':'root',
                'fingerprint':ssh.host_fingerprint(key),'auth_method':'password',
                'password_encrypted':store.cipher().encrypt(b'test').decode()}
        session=openwrt.Session()
        try:
            for _ in range(2):assert session.exchange(server)['metrics']['model']=='Example MT7621 router'
            assert len(transports)==1 and len(calls)==2
            assert all(command.startswith(b'/bin/sh -c ') and b'python' not in command for command in calls)
        finally:
            session.close();stop.set();thread.join(2)
            for transport in transports:transport.close()
            for t in threads:t.join(2)
