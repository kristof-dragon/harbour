"""Sent over pinned SSH; requires only Python 3 and Docker on the Linux host.

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
import time
from pathlib import Path


def run(argv, timeout=30, cwd=None):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
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
    for image in {c["Image"] for c in containers}:
        try:
            images[image] = json.loads(run(["docker", "image", "inspect", image]))[0]
        except RuntimeError:
            images[image] = {}
    services = []
    for c in containers:
        labels = c["Config"].get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        image = images[c["Image"]]
        image_labels = (image.get("Config") or {}).get("Labels") or {}
        ports = []
        for port, bindings in (c["NetworkSettings"].get("Ports") or {}).items():
            ports.extend((b.get("HostIp", "") + ":" + b["HostPort"] + " → " + port) for b in (bindings or []))
        name = labels.get("com.docker.compose.service", c["Name"].lstrip("/"))
        services.append({
            "id": c["Id"], "name": name, "container": c["Name"].lstrip("/"),
            "project": project, "image": c["Config"]["Image"], "image_id": c["Image"],
            "version": image_version(image_labels),
            "state": c["State"]["Status"], "health": c["State"].get("Health", {}).get("Status"),
            "started": c["State"].get("StartedAt"), "ports": ports,
            "restart_policy": c["HostConfig"].get("RestartPolicy", {}).get("Name"),
            "mounts": [{"destination": m["Destination"], "type": m["Type"], "rw": m["RW"]} for m in c.get("Mounts", [])],
            "working_dir": labels.get("com.docker.compose.project.working_dir"),
            "config_files": labels.get("com.docker.compose.project.config_files", "").split(",") if project else [],
            "platform": {"os": image.get("Os", "linux"), "architecture": image.get("Architecture"), "variant": image.get("Variant", "")},
            "update": {"status": "unchecked"},
        })
    return services


def metrics(docker=True):
    def cpu():
        with open("/proc/stat") as f:
            values = [int(v) for v in f.readline().split()[1:9]]
        return sum(values), values[3] + values[4]
    first = cpu()
    time.sleep(0.35)
    second = cpu()
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
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    os_name = "Linux"
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.strip().split("=", 1)[1].strip('"')
    except OSError:
        pass
    return {"cpu": round(100 * (1 - (second[1] - first[1]) / max(second[0] - first[0], 1)), 1),
            "cores": os.cpu_count(), "memory": {"total": total, "used": total - available,
            "percent": round(100 * (total - available) / total, 1)}, "disks": disks,
            "uptime": uptime, "os": os_name, "kernel": os.uname().release,
            "temperature": temperatures(), "timezone": timezone_info(),
            "docker": run(["docker", "version", "--format", "{{.Server.Version}}"]).strip() if docker else None}


def temperature_kind(sensor):
    """Classify actual package/die readings, never infer one from a hot core."""
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


def remote_config_digest(manifest, platform):
    """Compare the platform image config digest, not an index to a child digest."""
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
            matches.append(digest)
    return matches[0] if len(set(matches)) == 1 else None


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
                digest = remote_config_digest(raw, s["platform"])
                cache[key] = {"digest": digest, "version": available_version(image, raw, digest) if digest != s["image_id"] else s.get("version")} if digest else {"error": "Registry did not return an unambiguous image for this platform."}
            except Exception as exc:
                cache[key] = {"error": str(exc)[:400]}
        result = cache[key]
        s["update"] = {**result, "checked": time.time(), "status": "unknown" if "error" in result else (
            "available" if result["digest"] != s["image_id"] else "current")}
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


def handle(request, emit=None):
    operation = request["operation"]
    if operation == 'resources':
        # No Docker commands: resource polling must also work during daemon operations.
        return {'metrics': metrics(docker=False)}
    docker = request.get("server_type", "docker") == "docker"
    if not docker and operation != "snapshot":
        raise ValueError("Docker operations are disabled for plain servers")
    if operation == 'execute' and emit:
        emit({'kind': 'phase', 'phase': 'inventory', 'label': 'Reading Docker container inventory'})
    services = inventory() if docker else []
    if operation == "snapshot":
        if request.get("updates"):
            services = check_updates(services)
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
