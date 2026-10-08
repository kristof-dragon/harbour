"""Sent over pinned SSH; uses Python 3 on Linux/macOS, plus Docker when enabled.

No shell expansion and no arbitrary commands from the browser. This module is
also imported locally to construct the exact same operation preview.
"""
import json
import datetime
import os
import re
import subprocess
import codecs
import selectors
import signal
import sys
import time
from pathlib import Path


def run(argv, timeout=30, cwd=None):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Command failed")[-4000:])
    return result.stdout


def run_stream(argv, emit, timeout=300, cwd=None):
    """Forward bounded output chunks while the command is still running."""
    output = ''
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True, bufsize=0) as process, selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline, heartbeat = time.monotonic() + timeout, time.monotonic()
        try:
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise TimeoutError('Command timed out. Check the server before retrying.')
                for key, _ in selector.select(.1):
                    chunk = os.read(key.fd, 4096)
                    text = decoder.decode(chunk, final=not chunk)
                    if text:
                        output = (output + text)[-60000:]
                        emit({'kind': 'output', 'text': text})
                    if not chunk:
                        selector.unregister(key.fileobj)
                if time.monotonic() - heartbeat >= 1:
                    emit({'kind': 'heartbeat'})
                    heartbeat = time.monotonic()
            if process.wait(timeout=max(.1, deadline-time.monotonic())):
                raise RuntimeError(output[-4000:] or 'Command failed')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return output


def image_version(labels):
    for key in ('org.opencontainers.image.version', 'org.label-schema.version', 'version'):
        value = labels.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None


def compare_versions(running, candidate):
    """Return candidate precedence, or None for labels without a known ordering.

    Accept numeric releases (including calendar versions), common prereleases
    and LinuxServer packaging revisions. Never sort arbitrary hashes as releases.
    """
    if not isinstance(running, str) or not isinstance(candidate, str):
        return None
    running, candidate = running.strip(), candidate.strip()
    if not running or not candidate:
        return None
    if running == candidate:
        return 0
    pattern = (r'[vV]?(\d+(?:\.\d+)*)'
               r'(?:[-.]?(dev|alpha|a|beta|b|rc|pre|preview)[.-]?(\d+)?)?'
               r'(?:-r(\d+))?(?:-ls(\d+))?(?:\+[0-9A-Za-z.-]+)?')
    parsed = [re.fullmatch(pattern, value) for value in (running, candidate)]
    if not all(parsed):
        return None
    width = max(len(match[1].split('.')) for match in parsed)
    def key(match):
        release = tuple(int(n) for n in match[1].split('.'))
        stage = {'dev': -1, 'alpha': 0, 'a': 0, 'beta': 1, 'b': 1,
                 'pre': 2, 'preview': 2, 'rc': 2, None: 3}[match[2]]
        return release + (0,) * (width-len(release)), stage, int(match[3] or 0), int(match[4] or 0), int(match[5] or 0)
    old, new = map(key, parsed)
    return (new > old) - (new < old)


def image_matches_update(service, update):
    # Classic Docker exposes the config ID; the containerd store exposes an
    # index/image ID and the actual container's platform manifest separately.
    manifest = service.get('image_manifest_digest')
    if manifest and update.get('manifest_digest'):
        return manifest == update['manifest_digest']
    return bool(update.get('digest')) and service.get('image_id') == update['digest']


def classify_update(service, update):
    """Apply the version policy to fresh checks and previously stored checks."""
    if update.get('status') not in {'available', 'current', 'unverified'}:
        return dict(update)
    result = {k: v for k, v in update.items() if k not in {'reason', 'error'}}
    if image_matches_update(service, update):
        result.update(status='current', reason='The running image matches the checked image.')
        return result
    precedence = compare_versions(service.get('version'), update.get('version'))
    if precedence == 1:
        result.update(status='available', reason='A newer version is available for the configured image tag.')
    elif precedence == 0:
        result.update(status='current', reason='The version is unchanged. Image rebuilds do not trigger update notifications.')
    elif precedence == -1:
        result.update(status='current', reason='The checked version is older than the running version.')
    else:
        result.update(status='unverified', reason='A newer version could not be verified from the image labels. A different digest alone is not an update.')
    return result


def available_version(image, manifest, config_digest):
    """Read labels from the exact remote manifest, never a tag that may have moved."""
    entries = manifest if isinstance(manifest, list) else [manifest]
    candidates = []
    for entry in entries:
        data = entry.get('SchemaV2Manifest') or entry.get('OCIManifest') or entry
        if data.get('config', {}).get('digest') != config_digest:
            continue
        version = image_version(data.get('annotations') or {})
        if version:
            return version
        digest = entry.get('Descriptor', {}).get('digest')
        if digest and re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            candidates.append(digest)
    if len(set(candidates)) != 1:
        return None
    repository = image.split('@')[0]
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    try:
        # Optional Buildx uses the host's existing Docker registry credentials.
        # This fetches configuration metadata only; no image layers are pulled.
        config = json.loads(run(['docker', 'buildx', 'imagetools', 'inspect', '--format', '{{json .Image}}',
                                 repository + '@' + candidates[0]], timeout=20))
        return image_version((config.get('config') or {}).get('Labels') or {})
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None  # A missing label/plugin never converts a valid update check to failure.


def inventory():
    ids = run(["docker", "ps", "-aq", "--no-trunc"]).split()
    containers = []
    for start in range(0, len(ids), 50):
        containers.extend(json.loads(run(["docker", "inspect", *ids[start:start + 50]])))
    images = {}
    # Read the container's platform from its immutable image, never a mutable
    # tag or the host's default platform within a multi-platform index.
    def image_key(container):
        platform = (container.get('ImageManifestDescriptor') or {}).get('platform') or {}
        selected = '/'.join(platform[k] for k in ('os', 'architecture', 'variant') if platform.get(k))
        return container['Image'], selected
    for ref, platform in {image_key(c) for c in containers}:
        try:
            argv = ['docker', 'image', 'inspect', ref] + (['--platform', platform] if platform else [])
            images[ref, platform] = json.loads(run(argv))[0]
        except RuntimeError:
            images[ref, platform] = {}
            if platform:
                # Older Docker clients may not expose --platform. Accept the
                # legacy inspection only if it describes the requested variant.
                try:
                    legacy = json.loads(run(['docker', 'image', 'inspect', ref]))[0]
                    actual = '/'.join(legacy.get(k, '') for k in ('Os', 'Architecture', 'Variant')).rstrip('/')
                    if actual == platform:
                        images[ref, platform] = legacy
                except RuntimeError:
                    pass
    services = []
    for c in containers:
        labels = c["Config"].get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        image = images[image_key(c)]
        manifest = c.get('ImageManifestDescriptor') or {}
        platform = manifest.get('platform') or {"os": image.get("Os", "linux"), "architecture": image.get("Architecture"), "variant": image.get("Variant", "")}
        image_labels = (image.get("Config") or {}).get("Labels") or {}
        ports = []
        for port, bindings in (c["NetworkSettings"].get("Ports") or {}).items():
            ports.extend((b.get("HostIp", "") + ":" + b["HostPort"] + " → " + port) for b in (bindings or []))
        name = labels.get("com.docker.compose.service", c["Name"].lstrip("/"))
        services.append({
            "id": c["Id"], "name": name, "container": c["Name"].lstrip("/"),
            "project": project, "image": c["Config"]["Image"], "image_id": c["Image"],
            "image_manifest_digest": manifest.get('digest'),
            "version": image_version(image_labels) or image_version(manifest.get('annotations') or {}),
            "state": c["State"]["Status"], "health": c["State"].get("Health", {}).get("Status"),
            "started": c["State"].get("StartedAt"), "ports": ports,
            "restart_policy": c["HostConfig"].get("RestartPolicy", {}).get("Name"),
            "mounts": [{"destination": m["Destination"], "type": m["Type"], "rw": m["RW"]} for m in c.get("Mounts", [])],
            "working_dir": labels.get("com.docker.compose.project.working_dir"),
            "config_files": labels.get("com.docker.compose.project.config_files", "").split(",") if project else [],
            "platform": {k: platform.get(k, '') for k in ('os', 'architecture', 'variant')},
            "update": {"status": "unchecked"},
        })
    return services


def metrics(docker=True):
    if sys.platform == 'darwin':
        return macos_metrics(docker)
    if sys.platform.startswith('linux'):
        return linux_metrics(docker)
    raise RuntimeError('Unsupported host OS: Harbour monitors Linux and macOS hosts.')


def linux_metrics(docker=True):
    with open("/proc/stat") as f:
        # guest/guest_nice are already included in user/nice; only use the
        # first eight counters. Harbour keeps the baseline between polls.
        values = [int(v) for v in f.readline().split()[1:9]]
        boot_time = next((line.split()[1] for line in f if line.startswith('btime ')), None)
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            boot_id = f.read().strip()
    except OSError:
        boot_id = None
    boot_id = boot_id or ('btime:' + boot_time if boot_time else None)
    cores = os.cpu_count()
    counters = {'values': values, 'boot_id': boot_id, 'uptime': uptime, 'cores': cores}
    try:
        load = os.getloadavg()
    except OSError:
        load = (None, None, None)
    with open("/proc/meminfo") as f:
        mem = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in f}
    total = mem["MemTotal"]
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    disks = []
    seen = set()
    with open("/proc/mounts") as f:
        mounts = [line.split()[:3] for line in f]
    for device, escaped_mount, fs in mounts:
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), escaped_mount)
        if mount != "/" and (fs not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "nfs", "nfs4", "cifs"} or mount.startswith(("/var/lib/docker/", "/snap/"))):
            continue
        try:
            stat = os.statvfs(mount)
            identity = mount
            if identity in seen or not stat.f_blocks:
                continue
            seen.add(identity)
            capacity = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
            disks.append({"mount": mount, "total": capacity, "used": used, "free": free,
                          "percent": round(100 * used / max(used + free, 1), 1)})
        except OSError:
            continue
    os_name = "Linux"
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.strip().split("=", 1)[1].strip('"')
    except OSError:
        pass
    return {"cpu": None, "cpu_counters": counters,
            **dict(zip(("load1", "load5", "load15"), load)),
            "cores": cores, "memory": {"total": total, "used": total - available,
            "percent": round(100 * (total - available) / total, 1)}, "disks": disks,
            "uptime": uptime, "os": os_name, "kernel": os.uname().release,
            "temperature": temperatures(), **linux_hardware(), "timezone": timezone_info(),
            "docker": run(["docker", "version", "--format", "{{.Server.Version}}"]).strip() if docker else None}


def macos_cpu_ticks():
    """Read cumulative Mach CPU ticks once, without sampling delays or dependencies."""
    import ctypes
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    lib.mach_host_self.argtypes = []
    lib.mach_host_self.restype = ctypes.c_uint
    lib.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                    ctypes.POINTER(ctypes.c_uint)]
    lib.host_statistics.restype = ctypes.c_int
    lib.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
    lib.mach_port_deallocate.restype = ctypes.c_int
    ticks, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
    host = lib.mach_host_self()
    try:
        # HOST_CPU_LOAD_INFO: user, system, idle, nice (32-bit unsigned ticks).
        status = lib.host_statistics(host, 3, ticks, ctypes.byref(count))
        if status or count.value != 4:
            raise OSError('macOS CPU counters are unavailable')
        return list(ticks)
    finally:
        task = ctypes.c_uint.in_dll(lib, 'mach_task_self_').value
        lib.mach_port_deallocate(task, host)


def macos_memory(total, output):
    # vm_stat reports the actual page size: 4 KiB on Intel, often 16 KiB on ARM.
    page_size = re.search(r'page size of (\d+) bytes', output)
    pages = {name.strip(): int(value) for name, value in
             re.findall(r'^([^:\n]+):\s*(\d+)\.', output, re.MULTILINE)}
    if (total <= 0 or not page_size or int(page_size[1]) <= 0
            or not {'Pages free', 'Pages inactive'} <= pages.keys()):
        raise RuntimeError('macOS memory statistics are unavailable')
    # Free and inactive pages are reclaimable. Compressed memory stays counted
    # as used; this is a capacity percentage, not Activity Monitor's pressure.
    available = min(total, (pages['Pages free'] + pages['Pages inactive']) * int(page_size[1]))
    used = total - available
    return {'total': total, 'used': used, 'percent': round(100 * used / total, 1)}


def macos_disks():
    mounts = []
    for line in run(['/sbin/mount'], timeout=5).splitlines():
        match = re.match(r'^.+? on (.+) \(([^, )]+)(?:, (.*))?\)$', line)
        if match:
            mounts.append((match[1], match[2], set((match[3] or '').split(', '))))
    data = '/System/Volumes/Data'
    has_data = any(mount == data and fs == 'apfs' for mount, fs, _ in mounts)
    disks, seen = [], set()
    for path, fs, flags in mounts:
        # Show the writable startup volume as /, not its tiny sealed snapshot.
        mount = '/' if path == data else path
        if path == '/' and has_data:
            continue
        if mount != '/' and (fs not in {'apfs', 'hfs', 'exfat', 'msdos', 'ntfs', 'ufs', 'zfs', 'nfs', 'smbfs', 'afpfs', 'webdav', 'osxfuse', 'macfuse'}
                or path.startswith(('/System/Volumes/', '/Volumes/.timemachine/', '/Library/Developer/CoreSimulator/'))
                or 'nobrowse' in flags):
            continue
        if mount in seen:
            continue
        try:
            stat = os.statvfs(path)
        except OSError:
            continue
        if not stat.f_blocks:
            continue
        seen.add(mount)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        # APFS capacity/free space belongs to the shared container; include
        # sibling volumes and snapshots in used space so fullness is meaningful.
        used = total - free if fs == 'apfs' else (stat.f_blocks - stat.f_bfree) * stat.f_frsize
        disks.append({'mount': mount, 'total': total, 'used': used, 'free': free,
                      'percent': round(100 * used / max(used + free, 1), 1)})
    return disks


def macos_metrics(docker=True):
    boot = run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], timeout=5)
    match = re.search(r'sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)', boot)
    if not match:
        raise RuntimeError('macOS boot time is unavailable')
    uptime = max(0, time.time() - int(match[1]) - int(match[2]) / 1_000_000)
    boot_id = 'darwin:btime:' + match[1] + ':' + match[2]
    try:
        session = run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], timeout=5).strip()
        if session:
            boot_id = 'darwin:' + session
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass
    cores, counters = os.cpu_count(), None
    try:
        user, system, idle, nice = macos_cpu_ticks()
        # Normalize to the existing user/nice/system/idle/iowait/irq/softirq/steal
        # schema. A wrap or reset rebaselines safely in harbour.cpu.
        counters = {'source': 'darwin', 'values': [user, nice, system, idle, 0, 0, 0, 0],
                    'boot_id': boot_id, 'uptime': uptime, 'cores': cores}
    except (OSError, AttributeError, ValueError):
        pass  # Keep other resources usable when CPU statistics are unavailable.
    total = int(run(['/usr/sbin/sysctl', '-n', 'hw.memsize'], timeout=5).strip())
    memory = macos_memory(total, run(['/usr/bin/vm_stat'], timeout=5))
    try:
        version = run(['/usr/bin/sw_vers', '-productVersion'], timeout=5).strip()
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        version = ''
    try:
        load = os.getloadavg()
    except OSError:
        load = (None, None, None)
    return {'cpu': None, 'cpu_counters': counters, 'cores': cores, 'memory': memory,
            **dict(zip(('load1', 'load5', 'load15'), load)), 'disks': macos_disks(),
            'uptime': uptime, 'os': ('macOS ' + version).strip(), 'kernel': os.uname().release,
            **macos_hardware(), 'timezone': timezone_info(),
            'docker': run(['docker', 'version', '--format', '{{.Server.Version}}']).strip() if docker else None}


def temperature_kind(sensor):
    """Classify actual package/die readings, never infer one from a hot core."""
    # The legacy cpu_package category also contains explicitly identified SoC
    # and die aggregate channels. Preserve the exact source label in the UI.
    if sensor['id'] == 'smc:TCMb' and sensor['label'] == 'SMC · CPU die average':
        return 'cpu_package'
    chip, _, label = sensor["label"].partition(" · ")
    chip, label = chip.lower(), label.lower()
    if (chip == "coretemp" and re.fullmatch(r"(?:package|physical) id \d+", label)
            or chip in {"k10temp", "zenpower"} and label == "tdie"
            or chip.startswith("peci_cputemp") and label == "die"
            or chip in {"x86_pkg_temp", "cpu_thermal", "cpu-thermal", "soc_thermal", "soc-thermal", "package_thermal", "package-thermal", "bcm2835_thermal"}
            or sensor["id"] == "demo:cpu" and sensor["label"] == "CPU package"):
        return "cpu_package"
    if sensor.get("cpu") or chip in {"coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal"} or "cpu" in chip or re.fullmatch(r"(?:bigcore\d*|littlecore)[_-]thermal", chip):
        return "cpu_auxiliary"
    return "other"


def package_order(sensor):
    # Package 0 before Package 1, etc. Never choose based on temperature.
    label = sensor["label"].lower()
    index = re.search(r"(?:package|physical) id (\d+)", label)
    return (0 if label.startswith("coretemp · ") else 1 if temperature_kind(sensor) == "cpu_package" and not label.startswith(("soc_thermal", "soc-thermal")) else 2, int(index[1]) if index else 0, label, sensor["id"])


def temperature_summary(sensors):
    sensors = [{**s, "kind": temperature_kind(s)} for s in sensors]
    packages = sorted((s for s in sensors if s["kind"] == "cpu_package"), key=package_order)
    package = packages[0] if packages else None
    return {"sensors": sensors, "package": package["celsius"] if package else None,
            "package_sensor_id": package["id"] if package else None,
            "package_label": package["label"] if package else None, "package_count": len(packages)}


def temperatures(root="/sys"):
    """Read existing kernel sensor interfaces only; never load drivers or write sysfs."""
    sensors = []
    def read(path):
        try:
            return path.read_text().strip()
        except OSError:
            return ""
    for hw in sorted(Path(root).glob("class/hwmon/hwmon*")):
        chip = read(hw / "name") or hw.name
        # These legacy chips report voltages in temp*_input, not Celsius.
        if chip in {"asb100", "w83781d", "w83782d", "w83783s"}:
            continue
        for path in sorted(hw.glob("temp*_input")):
            stem = path.name.removesuffix("_input")
            if read(hw / (stem + "_fault")) == "1" or read(hw / (stem + "_enable")) == "0":
                continue
            try:
                value = float(read(path)) / 1000
                if not -40 <= value <= 180:
                    continue
                label = read(hw / (stem + "_label")) or stem
                cpu = chip in {"coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "peci_cputemp"} or "cpu" in chip.lower()
                sensors.append({"id": chip + ":" + hw.name + ":" + stem, "label": chip + " · " + label, "celsius": round(value, 1), "cpu": cpu})
            except ValueError:
                continue
    # Thermal zones are independent of hwmon discovery (notably on ARM).
    # A zone can also be exposed via hwmon; retain just one copy of that type.
    chips = {s["label"].split(" · ")[0].lower().replace("-", "_") for s in sensors}
    for zone in sorted(Path(root).glob("class/thermal/thermal_zone*")):
        try:
            value = float(read(zone / "temp")) / 1000
            label = read(zone / "type") or zone.name
            if label.lower().replace("-", "_") in chips:
                continue
            # Intel exposes the same package through coretemp and this zone.
            if label == "x86_pkg_temp" and any(s["label"].startswith("coretemp · ") and temperature_kind(s) == "cpu_package" for s in sensors):
                continue
            if -40 <= value <= 180:
                sensors.append({"id": zone.name, "label": label, "celsius": round(value, 1), "cpu": "cpu" in label.lower() or "x86_pkg" in label.lower()})
        except ValueError:
            continue
    return temperature_summary(sensors)


def sensor_text(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ''


def sensor_number(value):
    import math
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def hardware_reading(identity, label, value, unit, source):
    return {'id': identity, 'label': label, 'value': round(value, 4), 'unit': unit, 'source': source}


def linux_hardware(root='/sys'):
    """Optional read-only kernel interfaces; missing permissions never fail a poll."""
    readings, batteries = [], []
    root = Path(root)
    # Units from the Linux hwmon ABI. Keep each channel separate: rails and
    # package/SoC power often overlap and must never be added into a total.
    specs = [('fan', 'input', 1, 'RPM'), ('in', 'input', 1000, 'V'),
             ('curr', 'input', 1000, 'A'), ('power', 'input', 1e6, 'W'),
             ('power', 'average', 1e6, 'W'), ('energy', 'input', 1e6, 'J')]
    for hw in sorted(root.glob('class/hwmon/hwmon*')):
        chip = sensor_text(hw / 'name') or hw.name
        # hwmon indices can change after reboot. Use the physical device path
        # where available, stripping only the dynamically allocated hwmon tail.
        physical = re.sub(r'/hwmon/hwmon\d+$', '', str(hw.resolve()))
        try:
            physical = str(Path(physical).relative_to(root.resolve()))
        except ValueError:
            pass
        for prefix, attribute, divisor, unit in specs:
            for path in sorted(hw.glob(prefix + '*_' + attribute)):
                if not re.fullmatch(prefix + r'\d+_' + attribute, path.name):
                    continue
                stem = path.name.rsplit('_', 1)[0]
                if (sensor_text(hw / (stem + '_fault')) == '1'
                        or sensor_text(hw / (stem + '_enable')) == '0'):
                    continue
                value = sensor_number(sensor_text(path))
                if value is None or unit in ('RPM', 'J') and value < 0:
                    continue
                label = sensor_text(hw / (stem + '_label')) or stem
                suffix = ' (average)' if attribute == 'average' else ''
                readings.append(hardware_reading('hwmon:' + physical + ':' + chip + ':' + path.name,
                    chip + ' · ' + label + suffix, value / divisor, unit, 'Linux hwmon'))
    by_label = {}
    for reading in readings:
        by_label.setdefault(reading['label'], []).append(reading)
    for group in by_label.values():
        if len(group) < 2:
            continue
        devices = [r['id'].rsplit(':', 2)[0].removeprefix('hwmon:') for r in group]
        short = [Path(device).name for device in devices]
        for reading, device in zip(group, short if len(set(short)) == len(short) else devices):
            reading['label'] += ' [' + device + ']'
    for device in sorted(root.glob('class/power_supply/*')):
        if sensor_text(device / 'type') != 'Battery' or sensor_text(device / 'present') == '0':
            continue
        name = device.name
        batteries.append({'name': name, 'status': sensor_text(device / 'status') or 'Unknown',
                          'condition': sensor_text(device / 'health') or 'Unknown'})
        specs = [('capacity', 1, '%', 'charge'), ('cycle_count', 1, 'cycles', 'cycles'),
                 ('voltage_now', 1e6, 'V', 'voltage'), ('current_now', 1e6, 'A', 'current'),
                 ('power_now', 1e6, 'W', 'battery power'), ('temp', 10, '°C', 'temperature'),
                 ('energy_now', 1e6, 'Wh', 'stored energy'), ('energy_full', 1e6, 'Wh', 'full energy'),
                 ('energy_full_design', 1e6, 'Wh', 'design energy'),
                 ('charge_now', 1e6, 'Ah', 'stored charge'), ('charge_full', 1e6, 'Ah', 'full charge'),
                 ('charge_full_design', 1e6, 'Ah', 'design charge')]
        values = {}
        for attribute, divisor, unit, label in specs:
            value = sensor_number(sensor_text(device / attribute))
            if value is None or (attribute == 'capacity' and not 0 <= value <= 100):
                continue
            if unit in ('cycles', 'Wh', 'Ah') and value < 0 or unit == '°C' and not -40 <= value / divisor <= 100:
                continue
            values[attribute] = value
            readings.append(hardware_reading('battery:' + name + ':' + attribute,
                name + ' · ' + label, value / divisor, unit, 'Linux battery'))
        for basis in ('energy', 'charge'):
            full, design = (values.get(basis + suffix) for suffix in ('_full', '_full_design'))
            if full is not None and design is not None and full >= 0 and design > 0:
                readings.append(hardware_reading('battery:' + name + ':capacity_health',
                    name + ' · capacity health', full / design * 100, '%', 'Full / design capacity'))
                break
    # Expose RAPL energy as energy, never mislabel a wrapping cumulative counter
    # as watts. Interval power needs a separately validated sampling strategy.
    seen = set()
    for path in sorted(root.glob('class/powercap/*/energy_uj')):
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        value = sensor_number(sensor_text(path))
        if value is not None and value >= 0:
            name = sensor_text(path.parent / 'name') or path.parent.name
            readings.append(hardware_reading('powercap:' + path.parent.name + ':energy',
                name + ' · accumulated energy', value / 1e6, 'J', 'Linux powercap (wrapping counter)'))
    return {'hardware': readings, 'batteries': batteries}


def smc_decode(type_, data):
    import struct
    if type_ == 'flt ' and len(data) == 4:
        value = struct.unpack('<f', data)[0]
    elif type_ == 'sp78' and len(data) == 2:
        value = int.from_bytes(data, 'big', signed=True) / 256
    elif type_ == 'fpe2' and len(data) == 2:
        value = int.from_bytes(data, 'big') / 4
    elif type_ in {'ui8 ', 'ui16', 'ui32', 'ui64', 'si8 ', 'si16', 'si32', 'si64'}:
        width = {'8 ': 1, '16': 2, '32': 4, '64': 8}[type_[2:]]
        if len(data) != width:
            return None
        value = int.from_bytes(data, 'big', signed=type_.startswith('si'))
    else:
        return None
    return sensor_number(value)


class MacSMC:
    """Small read-only AppleSMC client. No key enumeration or write operation."""
    def __init__(self):
        import ctypes as C
        self.C = C
        u8, u16, u32 = C.c_uint8, C.c_uint16, C.c_uint32
        class Version(C.Structure):
            _fields_ = [('major', u8), ('minor', u8), ('build', u8), ('reserved', u8), ('release', u16)]
        class Limit(C.Structure):
            _fields_ = [('version', u16), ('length', u16), ('cpu', u32), ('gpu', u32), ('memory', u32)]
        class Info(C.Structure):
            _fields_ = [('size', u32), ('type', u32), ('attributes', u8)]
        class Request(C.Structure):
            _fields_ = [('key', u32), ('version', Version), ('limit', Limit), ('info', Info),
                        ('result', u8), ('status', u8), ('command', u8), ('index', u32), ('data', u8 * 32)]
        self.Request = Request
        io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        libc = C.CDLL('/usr/lib/libSystem.B.dylib')
        def bind(name, ret, args):
            fn = getattr(io, name)
            fn.restype, fn.argtypes = ret, args
            return fn
        matching = bind('IOServiceMatching', C.c_void_p, [C.c_char_p])
        services = bind('IOServiceGetMatchingServices', C.c_int, [u32, C.c_void_p, C.POINTER(u32)])
        next_ = bind('IOIteratorNext', u32, [u32])
        name_ = bind('IORegistryEntryGetName', C.c_int, [u32, C.c_char_p])
        release = bind('IOObjectRelease', C.c_int, [u32])
        open_ = bind('IOServiceOpen', C.c_int, [u32, u32, u32, C.POINTER(u32)])
        self.close = bind('IOServiceClose', C.c_int, [u32])
        self.call = bind('IOConnectCallStructMethod', C.c_int,
                         [u32, u32, C.c_void_p, C.c_size_t, C.c_void_p, C.POINTER(C.c_size_t)])
        iterator, self.conn = u32(), u32()
        if services(0, matching(b'AppleSMC'), C.byref(iterator)):
            raise OSError('SMC services unavailable')
        try:
            while True:
                device = next_(iterator)
                if not device:
                    break
                try:
                    name = C.create_string_buffer(128)
                    name_(device, name)
                    if name.value not in (b'AppleSMCKeysEndpoint', b'AppleSMC'):
                        continue
                    if not open_(device, u32.in_dll(libc, 'mach_task_self_').value, 0, C.byref(self.conn)) and self.conn.value:
                        break
                finally:
                    release(device)
        finally:
            release(iterator)
        if not self.conn.value:
            raise OSError('SMC endpoint unavailable')

    def query(self, request):
        if request.command not in (5, 9):
            raise ValueError('Only sensor reads are supported')
        C = self.C
        out, size = self.Request(), C.c_size_t(C.sizeof(self.Request))
        error = self.call(self.conn, 2, C.byref(request), C.sizeof(request), C.byref(out), C.byref(size))
        if error or out.result or size.value != C.sizeof(out):
            raise OSError('SMC key unavailable')
        return out

    def read(self, key):
        code = int.from_bytes(key.encode('ascii'), 'big')
        info = self.query(self.Request(key=code, command=9)).info
        if not 0 < info.size <= 32:
            return None
        data = bytes(self.query(self.Request(key=code, info=info, command=5)).data[:info.size])
        type_ = int(info.type).to_bytes(4, 'big').decode('ascii')
        return smc_decode(type_, data)


def macos_smc(chip=''):
    temps, readings = [], []
    client = MacSMC()
    try:
        def read(key):
            try:
                return sensor_number(client.read(key))
            except (OSError, ValueError, UnicodeError):
                return None
        # SMC mappings are model-dependent and undocumented by Apple. Only
        # known channels are named; unknown channels are not guessed by prefix.
        temperature_keys = [('TCMb', 'SMC · CPU die average', True),
                            ('TCMz', 'SMC · CPU die maximum', True),
                            ('TB0T', 'SMC · Battery', False)]
        if 'Intel' in chip:
            temperature_keys += [('TC0P', 'SMC · CPU proximity', True), ('TC0D', 'SMC · CPU die', True),
                                 ('TG0P', 'SMC · GPU proximity', False), ('TG0D', 'SMC · GPU die', False)]
        if chip == 'Apple M1 Max':
            temperature_keys += [(key, 'SMC · GPU sensor ' + str(i + 1), False)
                                 for i, key in enumerate(('Tg05', 'Tg0D', 'Tg0L', 'Tg0T'))]
        for key, label, cpu in temperature_keys:
            value = read(key)
            if value is not None and -40 <= value <= 180:
                temps.append({'id': 'smc:' + key, 'label': label, 'celsius': round(value, 1), 'cpu': cpu})
        count = read('FNum')
        for i in range(int(count) if count is not None and count.is_integer() and 0 <= count <= 16 else 0):
            key = 'F' + format(i, 'X') + 'Ac'
            value = read(key)
            if value is not None and 0 <= value <= 100000:
                readings.append(hardware_reading('smc:' + key, 'Fan ' + str(i + 1), value, 'RPM', 'Apple SMC'))
        for key, label, unit in [('PSTR', 'System power estimate', 'W'), ('PDTR', 'DC input power', 'W'),
                                 ('VD0R', 'DC input voltage', 'V'), ('ID0R', 'DC input current', 'A')]:
            value = read(key)
            if value is not None and 0 <= value <= 10000:
                readings.append(hardware_reading('smc:' + key, label, value, unit, 'Apple SMC · ' + key))
    finally:
        client.close(client.conn)
    return temps, readings


def macos_battery():
    import plistlib
    from xml.parsers.expat import ExpatError
    raw = run(['/usr/sbin/ioreg', '-a', '-r', '-c', 'AppleSmartBattery'], timeout=3)
    try:
        data = plistlib.loads(raw.encode())
    except (ValueError, ExpatError, OverflowError):
        return [], []
    readings, batteries = [], []
    for index, device in enumerate(data if isinstance(data, list) else []):
        if not isinstance(device, dict) or not device.get('BatteryInstalled'):
            continue
        name = 'Battery ' + str(index + 1)
        status = ('Charging' if device.get('IsCharging') else 'Full' if device.get('FullyCharged')
                  else 'External power' if device.get('ExternalConnected') else 'Discharging')
        batteries.append({'name': name, 'status': status, 'condition': 'Unknown'})
        def add(field, value, unit, label):
            value = sensor_number(value)
            if value is not None:
                readings.append(hardware_reading('battery:mac:' + str(index) + ':' + field,
                    name + ' · ' + label, value, unit, 'macOS battery'))
        current, maximum = (sensor_number(device.get(k)) for k in ('CurrentCapacity', 'MaxCapacity'))
        if current is not None and maximum is not None and maximum > 0 and 0 <= current <= maximum:
            add('capacity', current / maximum * 100, '%', 'charge')
        cycles = sensor_number(device.get('CycleCount'))
        if cycles is not None and cycles >= 0:
            add('cycles', cycles, 'cycles', 'cycles')
        voltage = sensor_number(device.get('Voltage'))
        if voltage is not None and 0 < voltage < 100000:
            add('voltage', voltage / 1000, 'V', 'voltage')
        raw_current = device.get('Amperage')
        if type(raw_current) is int:
            # ioreg may serialize signed milliamps as unsigned 64-bit values.
            if 2 ** 63 <= raw_current < 2 ** 64:
                raw_current -= 2 ** 64
            if abs(raw_current) < 100000:
                add('current', raw_current / 1000, 'A', 'current (+ charging)')
                if voltage is not None and 0 < voltage < 100000:
                    add('power', voltage * raw_current / 1e6, 'W', 'battery power (+ charging)')
        # Modern macOS MaxCapacity is often a percentage. Only compare raw
        # capacity fields with a known matching mAh design field.
        full, design = (sensor_number(device.get(k)) for k in ('AppleRawMaxCapacity', 'DesignCapacity'))
        if full is not None and design is not None and full >= 0 and design > 0:
            add('health', full / design * 100, '%', 'capacity health')
    return readings, batteries


def macos_hardware():
    temps, readings, batteries = [], [], []
    try:
        chip = run(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'], timeout=3).strip()
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        chip = ''
    try:
        temps, readings = macos_smc(chip)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        battery_readings, batteries = macos_battery()
        readings.extend(battery_readings)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        pass
    return {'temperature': temperature_summary(temps), 'hardware': readings, 'batteries': batteries}


def timezone_info():
    name = None
    resolved = os.path.realpath("/etc/localtime")
    if "/zoneinfo/" in resolved:
        name = resolved.split("/zoneinfo/", 1)[1]
    if not name:
        try:
            with open("/etc/timezone") as file:
                name = file.read().strip() or None
        except OSError:
            pass
    if not name:
        try:
            name = run(["timedatectl", "show", "-p", "Timezone", "--value"], timeout=3).strip() or None
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
    # datetime uses the host's local timezone, independent of the dashboard's TZ.
    local = datetime.datetime.now().astimezone()
    offset = local.strftime("%z")
    return {"name": name, "abbreviation": local.tzname(), "offset": offset[:3] + ":" + offset[3:],
            "local_time": local.isoformat(timespec="seconds")}


def remote_image_identity(manifest, platform):
    """Keep both digests of the selected platform; indexes aren't config IDs."""
    entries = manifest if isinstance(manifest, list) else [manifest]
    matches = []
    for entry in entries:
        descriptor = entry.get("Descriptor", {})
        remote_platform = descriptor.get("platform", {})
        if remote_platform:
            if any(remote_platform.get(k, "") != platform.get(k, "") for k in ("os", "architecture")):
                continue
            if platform.get("variant") and remote_platform.get("variant", "") != platform["variant"]:
                continue
        data = entry.get("SchemaV2Manifest") or entry.get("OCIManifest") or entry
        digest = data.get("config", {}).get("digest")
        if digest:
            matches.append((digest, descriptor.get('digest')))
    configs = {config for config, _ in matches}
    manifests = {digest for _, digest in matches}
    if len(configs) != 1:
        return None
    return {'digest': next(iter(configs)), 'manifest_digest': next(iter(manifests)) if len(manifests) == 1 else None}


def remote_config_digest(manifest, platform):
    identity = remote_image_identity(manifest, platform)
    return identity['digest'] if identity else None


def check_updates(services):
    cache = {}
    for s in services:
        image = s["image"]
        if "@sha256:" in image or image.startswith("sha256:"):
            s["update"] = {"status": "pinned", "checked": time.time()}
            continue
        key = (image, json.dumps(s["platform"], sort_keys=True))
        if key not in cache:
            try:
                raw = json.loads(run(["docker", "manifest", "inspect", "--verbose", image], timeout=45))
                identity = remote_image_identity(raw, s["platform"])
                cache[key] = {**identity, "version": s.get('version') if image_matches_update(s, identity) else available_version(image, raw, identity['digest'])} if identity else {"error": "Registry did not return an unambiguous image for this platform."}
            except Exception as exc:
                cache[key] = {"error": str(exc)[:400]}
        result = cache[key]
        s["update"] = classify_update(s, {**result, "checked": time.time(), "status": "unknown" if "error" in result else 'available'})
    return services


def plan(services, action, targets):
    if action == 'prune':
        if targets:
            raise ValueError('System prune applies to the whole server, not selected services')
        return [{'argv': ['docker', 'system', 'prune', '--force'], 'cwd': None, 'label': 'Docker system prune'}]
    if action == "pull_up":
        # Validate both phases first; finish every pull before any apply.
        apply = plan(services, "up", targets)
        return plan(services, "pull", targets) + apply
    if action not in {"pull", "up", "restart", "start", "stop"}:
        raise ValueError("Unsupported action")
    if not targets or len(targets) > 200:
        raise ValueError("Select between 1 and 200 targets")
    groups = {}
    for s in services:
        if s["project"]:
            groups.setdefault(s["project"], []).append(s)
    selected = {}
    whole_groups = set()
    for target in targets:
        if target.startswith("group:"):
            project = target[6:]
            if project not in groups:
                raise ValueError("The Compose project no longer exists; refresh first")
            whole_groups.add(project)
            for s in groups[project]:
                selected[s["id"]] = s
        else:
            match = next((s for s in services if s["id"] == target), None)
            if not match:
                raise ValueError("A container changed; refresh before trying again")
            selected[match["id"]] = match
    commands = []
    for project in sorted({s["project"] for s in selected.values() if s["project"]}):
        members = [s for s in selected.values() if s["project"] == project]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project) or any(
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", s["name"]) for s in members
        ):
            raise ValueError("Invalid Compose project or service metadata")
        sample = members[0]
        cwd, files = sample.get("working_dir"), sample.get("config_files", [])
        if not cwd or not os.path.isabs(cwd) or not files or any(not os.path.isabs(f) for f in files):
            raise ValueError("Compose paths are missing for " + project + ". Restore its original Compose files on the host.")
        if any((s.get("working_dir"), s.get("config_files")) != (cwd, files) for s in members):
            raise ValueError("Inconsistent Compose metadata; reconcile the project on the host first")
        argv = ["docker", "compose", "--project-directory", cwd, "-p", project]
        for file in files:
            argv.extend(["-f", file])
        argv += {"pull": ["pull"], "up": ["up", "-d"], "restart": ["restart"], "start": ["start"], "stop": ["stop"]}[action]
        if project not in whole_groups:
            argv += ["--no-deps"] if action in {"up", "restart"} else []
            argv += sorted({s["name"] for s in members})
        commands.append({"argv": argv, "cwd": cwd, "label": project})
    for s in selected.values():
        if s["project"]:
            continue
        if action == "up":
            raise ValueError("Apply requires a Compose project; standalone containers must be recreated using their original deployment definition")
        argv = ["docker", "pull", s["image"]] if action == "pull" else ["docker", action, s["id"]]
        commands.append({"argv": argv, "cwd": None, "label": s["container"]})
    return commands


def login_events(acknowledgement=None):
    """Bounded local exchange; never needs sudo or an extra SSH connection."""
    import socket
    def exchange(ack):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            deadline = time.monotonic() + 3
            connection.connect('/var/run/harbour-logins/collector.sock')
            connection.sendall(json.dumps({'operation': 'exchange', 'acknowledgement': ack}).encode() + b'\n')
            data = bytearray()
            while not data.endswith(b'\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Collector exchange timed out')
                connection.settimeout(remaining)
                chunk = connection.recv(65536)
                if not chunk:
                    raise ValueError('Collector response was incomplete')
                data.extend(chunk)
                if len(data) > 1500000:
                    raise ValueError('Collector response exceeded the size limit')
            return json.loads(data)
    try:
        result = exchange(acknowledgement)
        if result.get('error') == 'Invalid acknowledgement' and acknowledgement:
            result = exchange(None)
            result['ack_warning'] = 'Previous acknowledgement was rejected; collector may have been replaced.'
        if result.get('error'):
            raise ValueError(result['error'])
        return result
    except FileNotFoundError:
        return {'state': 'not_installed', 'detail': 'Install the Harbour login collector on this host to record authentication events.'}
    except PermissionError:
        return {'state': 'permission_denied', 'detail': 'This SSH account cannot read the login collector socket. Check the installed reader account.'}
    except Exception as exc:
        return {'state': 'error', 'detail': str(exc)[:500]}


def handle(request, emit=None):
    operation = request["operation"]
    if operation == 'resources':
        # No Docker commands: resource polling must also work during daemon operations.
        return {'metrics': metrics(docker=False), 'logins': login_events(request.get('logins_ack'))}
    docker = request.get("server_type", "docker") == "docker"
    if not docker and operation != "snapshot":
        raise ValueError("Docker operations are disabled for plain servers")
    if operation == 'execute' and emit:
        emit({'kind': 'phase', 'phase': 'inventory', 'label': 'Reading Docker container inventory'})
    services = inventory() if docker else []
    if operation == "snapshot":
        if request.get("updates"):
            services = check_updates(services)
        if request.get('resources') is False:
            return {'services': services, 'docker': run(['docker', 'version', '--format', '{{.Server.Version}}']).strip() if docker else None}
        return {"metrics": metrics(docker=docker), "services": services}
    if operation == "execute":
        targets = list(request['targets'])
        # Compose may recreate a queued target. Its project/service identity and
        # exact command must still match the preview before anything is run.
        for index, target in enumerate(targets):
            ref = request.get('target_refs', {}).get(target)
            if ref and not any(s['id'] == target for s in services):
                matches = [s for s in services if s.get('project') == ref['project'] and s['name'] == ref['name']]
                if len(matches) != 1:
                    raise ValueError('Queued Compose service changed; refresh and confirm a new plan')
                targets[index] = matches[0]['id']
        commands = plan(services, request["action"], targets)
        if commands != request["expected"]:
            raise ValueError("Server state changed after the preview. Refresh and confirm a new plan.")
        completed = []
        for index, command in enumerate(commands):
            try:
                if emit:
                    verb = next((v for v in ('pull', 'up', 'restart', 'start', 'stop', 'prune') if v in command['argv']), request['action'])
                    emit({'kind': 'step', 'completed': index, 'total': len(commands), 'label': verb + ' · ' + command['label']})
                    output = run_stream(command['argv'], emit, timeout=300, cwd=command['cwd'])
                    emit({'kind': 'completed', 'completed': index+1, 'total': len(commands)})
                else:
                    output = run(command["argv"], timeout=300, cwd=command["cwd"])
                completed.append(command["label"] + ":\n" + output[-12000:])
            except Exception as exc:
                return {"ok": False, "output": "\n".join(completed + [command["label"] + ": " + str(exc)])[-60000:], "completed": len(completed)}
        return {"ok": True, "output": "\n".join(completed)[-60000:], "completed": len(completed)}
    raise ValueError("Unsupported operation")
