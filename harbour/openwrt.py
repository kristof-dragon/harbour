"""Read-only OpenWrt collector. No Python, files or configuration changes on target."""
import json
import re
import shlex
import threading
import time

from . import cpu, ssh, store

# This script is deliberately fixed. Only discovered interface names (quoted by
# the shell) are passed to read-only tools. Never source UCI or release files.
SCRIPT = r'''
section() { printf '\n@@HARBOUR:%s\n' "$1"; }
[ -f /etc/openwrt_release ] || exit 42
section board
ubus call system board
section uptime
cat /proc/uptime
section boot
cat /proc/sys/kernel/random/boot_id
section cpu
cat /proc/stat
section memory
cat /proc/meminfo
section load
cat /proc/loadavg
section interfaces
cat /proc/net/dev
section network
ubus call network.interface dump
section tools
for tool in iw tc ethtool ping logread; do command -v "$tool"; done
section offload
uci -q get 'firewall.@defaults[0].flow_offloading'
section hardware_offload
uci -q get 'firewall.@defaults[0].flow_offloading_hw'
section wireless
if command -v iw >/dev/null 2>&1; then
    iw dev
    for iface in $(iw dev 2>/dev/null | awk '$1 == "Interface" {print $2}'); do
        section "station:$iface"
        iw dev "$iface" station dump
        section "survey:$iface"
        iw dev "$iface" survey dump
    done
fi
section queues
if command -v tc >/dev/null 2>&1; then tc -s qdisc show; fi
section conntrack
cat /proc/sys/net/netfilter/nf_conntrack_count
cat /proc/sys/net/netfilter/nf_conntrack_max
section routes
ip route show
ip -6 route show
section rules
ip rule show
ip -6 rule show
section end
'''.strip()


def sections(output):
    result = {}
    for part in re.split(r'^@@HARBOUR:', output, flags=re.M)[1:]:
        key, _, value = part.partition('\n')
        result[key.strip()] = value.strip()
    return result


def number(value, default=None):
    try:
        return float(str(value).split()[0])
    except (ValueError, IndexError):
        return default


def json_value(value, default):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def parse_stations(text, interface):
    result = []
    for block in re.split(r'^Station ', text, flags=re.M)[1:]:
        address = block.split()[0].lower()
        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', address):
            continue
        fields = dict(re.findall(r'^\s+([^:\n]+):\s*([^\n]*)', block, re.M))
        row = {'mac': address, 'interface': interface}
        for key, label in [('signal', 'signal_dbm'), ('signal avg', 'signal_avg_dbm'),
                           ('tx bitrate', 'tx_mbps'), ('rx bitrate', 'rx_mbps'),
                           ('tx retries', 'tx_retries'), ('tx failed', 'tx_failed'),
                           ('tx packets', 'tx_packets'), ('rx packets', 'rx_packets'),
                           ('tx bytes', 'tx_bytes'), ('rx bytes', 'rx_bytes'),
                           ('connected time', 'connected_seconds')]:
            row[label] = number(fields.get(key, ''))
        result.append(row)
    return result


def queue_stats(text):
    result = []
    for block in re.split(r'^qdisc ', text, flags=re.M)[1:]:
        header = block.splitlines()[0]
        dev = re.search(r'\bdev (\S+)', header)
        backlog = re.search(r'backlog (\d+(?:\.\d+)?)([KMGkmg]?)b (\d+)p', block)
        dropped = re.search(r'\bdropped (\d+)', block)
        result.append({'device': dev[1] if dev else None, 'discipline': header.split()[0],
                       'backlog_bytes': float(backlog[1]) * {'':1,'k':1000,'m':1000000,'g':1000000000}[backlog[2].lower()] if backlog else None,
                       'backlog_packets': int(backlog[3]) if backlog else None,
                       'dropped': int(dropped[1]) if dropped else None})
    return result


def survey_stats(text):
    for block in re.split(r'Survey data from ', text)[1:]:
        if '[in use]' not in block:
            continue
        fields = dict(re.findall(r'^\s*([^:\n]+):\s*([^\n]+)', block, re.M))
        return {key: number(fields.get(name, '')) for key, name in
                [('frequency_mhz','frequency'), ('active_ms','channel active time'), ('busy_ms','channel busy time'),
                 ('noise_dbm','noise'), ('rx_ms','channel receive time'), ('tx_ms','channel transmit time')]}
    return {}


def parse(output, measured_at=None):
    data = sections(output)
    board = json_value(data.get('board'), {})
    release = board.get('release', {})
    if not board or release.get('distribution') != 'OpenWrt' or 'end' not in data:
        raise ValueError('The target did not return a complete OpenWrt reading')
    uptime = number(data.get('uptime', ''))
    if uptime is None:
        raise ValueError('OpenWrt uptime is unavailable')
    cpus = {}
    for line in data.get('cpu', '').splitlines():
        fields = line.split()
        if fields and re.fullmatch(r'cpu\d*', fields[0]) and len(fields) >= 9:
            cpus[fields[0]] = [int(v) for v in fields[1:9]]
    memory = {k: int(v) * 1024 for k, v in re.findall(r'^(\w+):\s+(\d+) kB', data.get('memory', ''), re.M)}
    total, available = memory.get('MemTotal'), memory.get('MemAvailable')
    used = total - available if total and available is not None else None
    interfaces = {}
    for name, values in re.findall(r'^\s*([^:\s]+):\s*(.+)$', data.get('interfaces', ''), re.M):
        fields = values.split()
        if len(fields) >= 16 and all(v.isdigit() for v in fields):
            interfaces[name] = dict(zip(('rx_bytes', 'rx_packets', 'rx_errors', 'rx_dropped',
                                         'tx_bytes', 'tx_packets', 'tx_errors', 'tx_dropped'),
                                        [int(fields[i]) for i in (0, 1, 2, 3, 8, 9, 10, 11)]))
    stations = [row for key, value in data.items() if key.startswith('station:')
                for row in parse_stations(value, key.split(':', 1)[1])]
    tools = [line.rsplit('/', 1)[-1] for line in data.get('tools', '').splitlines() if line.startswith('/')]
    network = json_value(data.get('network'), {}).get('interface', [])
    profile = 'mt7621-mt76' if 'mt7621' in str(board).lower() else 'openwrt-generic'
    # Capability discovery determines tests, never the model name alone.
    capabilities = {'wireless': 'iw' in tools, 'queues': 'tc' in tools,
                    'ethernet_details': 'ethtool' in tools, 'router_ping': 'ping' in tools,
                    'logs': 'logread' in tools, 'interfaces': bool(interfaces),
                    'client_retries': any(s['tx_retries'] is not None for s in stations)}
    test_plan = [{'name': name, 'available': available, 'method': method}
                 for name, available, method in [
                     ('CPU and softirq intervals', bool(cpus), '/proc/stat counter differences'),
                     ('Interface traffic and errors', bool(interfaces), '/proc/net/dev counter differences'),
                     ('Wi-Fi client and radio statistics', capabilities['wireless'], 'iw station / survey reads'),
                     ('Software queue backlog', capabilities['queues'], 'tc statistics; offload coverage may be partial'),
                     ('Router-originated ICMP', capabilities['router_ping'], 'Bounded ping, selected interface'),
                     ('Router network events', capabilities['logs'], 'Filtered logread stream')]]
    load = data.get('load', '').split()
    boot = data.get('boot', '')
    core_count = sum(name != 'cpu' for name in cpus) or None
    counters = {'boot_id': boot, 'uptime': uptime, 'cores': core_count,
                'source': 'linux', 'values': cpus.get('cpu')}
    return {'measured_at': measured_at or time.time(), 'hostname': board.get('hostname'),
            'os': release.get('description', 'OpenWrt'), 'kernel': board.get('kernel'),
            'model': board.get('model'), 'architecture': board.get('system'), 'boot_id': boot,
            'uptime': uptime, 'cpu': None, 'cpu_counters': counters, 'cpu_model': board.get('system'),
            'cpu_count': core_count, 'cores': core_count, 'memory': {'total': total, 'used': used,
                'percent': round(used / total * 100, 1) if used is not None else None},
            'disks': [], 'hardware': [], 'temperature': {'sensors': [], 'package': None},
            'docker': None, 'timezone': None,
            **{f'load{n}': number(load[i]) if len(load) > i else None for i, n in enumerate((1, 5, 15))},
            'openwrt': {'board': board, 'profile': profile, 'read_only': True,
                        'capabilities': capabilities, 'test_plan': test_plan, 'tools': tools, 'cpus': cpus,
                        'interfaces': interfaces, 'network': network, 'stations': stations,
                        'wireless': data.get('wireless', ''),
                        'surveys': {k.split(':', 1)[1]: v for k, v in data.items() if k.startswith('survey:')},
                        'radio_stats': {k.split(':', 1)[1]: survey_stats(v) for k, v in data.items() if k.startswith('survey:')},
                        'queues': data.get('queues', ''), 'queue_stats': queue_stats(data.get('queues','')), 'routes': data.get('routes', ''),
                        'rules': data.get('rules', ''), 'conntrack': data.get('conntrack', ''),
                        'software_offload': data.get('offload') or None,
                        'hardware_offload': data.get('hardware_offload') or None}}


def interval(previous, current):
    """Derive rates only within a continuous boot; unknown/reset counters stay gaps."""
    result = current['openwrt']
    current['cpu'], current['cpu_sample_seconds'] = cpu.usage(
        previous.get('cpu_counters') if previous else None, current['cpu_counters'])
    result['cpu_percent'], result['softirq_percent'] = {}, {}
    elapsed = current['uptime'] - previous['uptime'] if previous and current['boot_id'] and previous['boot_id'] == current['boot_id'] else 0
    elapsed = max(0, elapsed)
    before = previous['openwrt'] if elapsed > 0 else {}
    for name, row in result.get('radio_stats', {}).items():
        old = before.get('radio_stats', {}).get(name, {})
        active, busy = None, None
        if old.get('frequency_mhz') == row.get('frequency_mhz'):
            if old.get('active_ms') is not None and row.get('active_ms') is not None:
                active = row['active_ms'] - old['active_ms']
            if old.get('busy_ms') is not None and row.get('busy_ms') is not None:
                busy = row['busy_ms'] - old['busy_ms']
        row['busy_percent'] = 100*busy/active if active and active > 0 and busy is not None and 0 <= busy <= active else None
    for name, values in result['cpus'].items():
        old = before.get('cpus', {}).get(name)
        delta = [b-a for a, b in zip(old, values)] if old else []
        valid = len(delta) == 8 and min(delta) >= 0 and sum(delta) > 0
        result['cpu_percent'][name] = round(100 * (sum(delta)-delta[3]-delta[4])/sum(delta), 1) if valid else None
        result['softirq_percent'][name] = round(100 * delta[6]/sum(delta), 1) if valid else None
    for name, row in result['interfaces'].items():
        old = before.get('interfaces', {}).get(name, {})
        for key in list(row):
            diff = row[key] - old[key] if key in old else -1
            row[key + '_delta'] = diff if diff >= 0 else None
            if key.endswith('_bytes'):
                row[key.replace('_bytes', '_mbps')] = diff*8/elapsed/1e6 if diff >= 0 and elapsed else None
    old_stations = {(s['interface'], s['mac']): s for s in before.get('stations', [])}
    for row in result['stations']:
        old = old_stations.get((row['interface'], row['mac']), {})
        if (row.get('connected_seconds') is not None and old.get('connected_seconds') is not None
                and row['connected_seconds'] < old['connected_seconds']):
            old = {}
        for key in ('tx_retries', 'tx_failed', 'tx_packets', 'rx_packets'):
            diff = row[key] - old[key] if row.get(key) is not None and old.get(key) is not None else -1
            row[key + '_delta'] = diff if diff >= 0 else None
    result['sample_seconds'] = elapsed or None
    return current


class Session:
    def __init__(self, signature=None):
        self.signature = signature
        self.lock = threading.Lock()
        self.retired = threading.Event()
        self.client = None

    def close(self):
        client, self.client = self.client, None
        if client:
            client.close()

    def command(self, server, command, cancel=None, timeout=8):
        if self.retired.is_set():
            raise ssh.Cancelled('OpenWrt connection retired')
        if not self.client or not self.client.get_transport() or not self.client.get_transport().is_active():
            self.close()
            credential = ({'password': store.cipher().decrypt(server['password_encrypted'].encode()).decode()}
                          if server.get('auth_method') == 'password' else {'pkey': ssh.stored_key(server['key_id'])})
            self.client = ssh.connect(server, cancel=cancel, **credential)
            self.client.get_transport().set_keepalive(20)
        if cancel:
            cancel.bind(self.close); cancel.check()
        channel = None
        try:
            _, output, _ = self.client.exec_command(command, timeout=timeout)
            channel = output.channel
            data = bytearray()
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if cancel:
                    cancel.check()
                if channel.recv_ready():
                    data.extend(channel.recv(65536))
                    if len(data) > 1_000_000:
                        raise ValueError('OpenWrt reading exceeded limit')
                if channel.recv_stderr_ready():
                    channel.recv_stderr(65536)
                if channel.exit_status_ready() and not channel.recv_ready():
                    if channel.recv_exit_status():
                        raise ValueError('OpenWrt read-only command unavailable')
                    return data.decode('utf-8', errors='replace')
                time.sleep(.005)
            raise TimeoutError('OpenWrt reading timed out')
        except Exception:
            self.close()
            raise
        finally:
            if channel:
                channel.close()
            if cancel:
                cancel.unbind()

    def exchange(self, server, payload=None, cancel=None):
        while not self.lock.acquire(timeout=.1):
            if cancel:
                cancel.check()
        try:
            begin = time.monotonic()
            output = self.command(server, '/bin/sh -c ' + shlex.quote(SCRIPT), cancel)
            return {'metrics': parse(output), 'services': [],
                    'collection_ms': round((time.monotonic()-begin)*1000, 1),
                    'latency_ms': None, 'recording': {'mode': 'openwrt', 'state': 'read-only'}}
        finally:
            self.lock.release()
