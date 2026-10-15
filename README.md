# Harbour

A self-hosted dashboard for monitoring Linux and macOS hosts and managing their Docker services over SSH.

## Install

Requires Docker Engine, Docker Compose v2 with `up --wait` support, Python 3.11+ and Git on the Docker host.

```sh
git clone https://github.com/kristof-dragon/harbour.git
cd harbour
python3 scripts/setup_env.py
python3 scripts/start.py
```

On Linux, the launcher also installs a user service for **Global settings → Configure resources**. Administrators can select a resource profile, change memory and buffer limits, and choose the recording write interval from the UI. **Apply & restart** verifies both services and rolls back failed changes. See [resource controls](harbour/REPORTS.md#memory-limits-and-small-boards) for host requirements.

The interactive setup creates `.env`, generates the application secret, and stores a salted hash of your chosen administrator password. The start script builds and starts Docker, then removes the first-admin credentials from `.env` and sets `FIRST_RUN=False` after successful setup.

For Nginx Proxy Manager on a separate instance, choose setup option **2**. In NPM, forward **HTTP** to the Harbour host’s LAN IP and your selected port (for example, **8384**), and enable HTTPS for your configured hostname. The container’s internal port remains **8080**.

Open your configured URL and sign in. Use **Menu → Add server** to connect a Linux or macOS host with Python 3.9+. The OS is detected automatically. Choose **Docker host** (also requires Docker, Compose v2 and an SSH account with Docker access) or **Plain server** for resource monitoring. Fetch and accept its host fingerprint, then generate or import an SSH key and install it from the form using a one-time password, or copy it manually. Optional password login is available after acknowledging its warning. Use **Server settings → Edit SSH connection** to switch authentication methods or replace keys without deleting the server.

### The Bridge

Enable **The Bridge** in **Menu → Global settings**. Its menu is hidden while off.
The Bridge brings connected sessions, searchable/sortable/paginated login history,
identity labels and collector coverage into one settings overlay. Administrators
can edit labels, tags and notes; other signed-in users can inspect observations.
Disabling the feature keeps the existing login history and annotations.

Current sessions require an installed login collector and a fresh host check.
SSH sessions are verified against host processes. Linux hosts with readable
logind report non-SSH console/desktop user sessions; macOS uses the current
console-session registry, including locked and switched-out desktops. Both
The Bridge and each server's Logins view show these sessions. Coverage is reported
separately for SSH and console/desktop sessions. macOS desktop login times stay
unknown when the registry provides no timestamp; no duration is inferred from
process uptime. Detection uses Apple's
[console-session registry fields](https://github.com/apple-oss-distributions/xnu/blob/main/iokit/IOKit/IOKitKeysPrivate.h).
Registry formats or permissions that cannot be verified remain unavailable.
Missing collectors, paused/offline hosts, restricted process visibility and
collection gaps remain explicit coverage limits.
OpenWrt's SSH-only probes do not install this collector, so routers are excluded
from the connected list. An unpaired login event is never proof of current presence.

Update each host's **Logins → Collector setup** to login collector **version 2**
for event-driven delivery. The collector signals committed events through its
private Unix socket. Harbour opens an additional channel on the **existing pinned
SSH connection**, not another SSH login or a public listening port. A notice fetches
only login evidence and session presence; it does not trigger a resource sample.
Bursts are coalesced, pending events are drained in bounded batches, and evidence
is committed before acknowledgement. A 20-second heartbeat verifies presence even
when a close event is missing. Reconnection resumes durable replay. Older collectors
continue ordinary polling and are labelled as needing an update for live delivery.
The browser receives authenticated change notices and retains periodic refresh as
fallback. This is near-real-time observation, not a guaranteed latency or lossless
capture of events the OS never logged. Live presence checks add some read traffic
and status writes; use Harbour footprint to measure the deployment's actual cost.

A verified **SSH key fingerprint + account** matches a credential across hosts.
It does not prove a person: shared keys, agent forwarding and shared accounts can
represent several users. Failed/offered keys cannot join verified credentials.
Without a verified key, matches are explicitly weaker **host + account + source IP
+ service + method** contexts. Source ports, boot IDs and process IDs distinguish
sessions, not recurring people. Counts and first/last observations cover the
retained 90 days, not lifetime usage. Identity labels survive retention so the
same credential/context can be recognised later.

**Session/event tags** and notes work without creating an identity. **Identity tags**
follow matching past and future observations. Inspect a session to see matching
history, connection counts, source, timestamps, duration, certificate fields and
original evidence. Tagged snapshot-only observations remain reviewable after they
end; a later unambiguous matching login inherits their tags. Unknown logout times
and durations stay unknown. No automatic suspicious-activity classification is
made in this release.

Additional useful identity context already recorded where available includes
SSH key algorithm, certificate ID/serial/CA fingerprint, host/account UID,
authentication method and timestamps; optional macOS native events can include
directory and token identifiers. Future integrations could add SSH certificate
principals, managed device IDs, VPN identities, and DHCP/ARP context. MAC addresses
are useful only within the relevant local network; these future additions are not
collected by The Bridge yet.

### Harbour footprint and storage sizing

Open **Menu → Harbour footprint** as an administrator to inspect the current
deployment. The view separates:

- **Deployment components:** separate application memory, file cache and recorder buffers,
  recent disk rates and saved daily measured totals with coverage for Harbour web/workers and the
  network recorder. Dedicated Linux cgroup v2 counters include child processes.
  A process-only fallback is labelled partial; missing counters stay unavailable.
- **Load added by each server:** serialized incoming resource/login data and
  acknowledged OpenWrt network-sample bytes. These are **logical data volumes**,
  not physical disk I/O attributed to a server. Shared indexes, WAL/checkpoints,
  retention and archives prevent exact per-server physical attribution.
- **Collectors on monitored machines:** disk traffic from Linux resource/login
  collector service cgroups, separately from the deployment. The SSH probe reports
  only its own process I/O, excluding commands it starts. Existing Linux collectors
  need no reinstall when their standard cgroup paths are readable. macOS, cgroup v1,
  stopped/missing collectors and OpenWrt SSH-only router probes show unavailable
  where scoped counters cannot be read.

The sampler runs every 30 seconds; the open view refreshes every 15 seconds.
Remote measurements follow the host's monitoring interval and become unavailable
when stale, paused or reconfigured. Rates need two samples and reset after counter
resets, process/service replacement or a long gap. Daily counter deltas and the file-size baseline are checkpointed every five minutes
and at clean shutdown, surviving restarts. History covers 90 days; gaps stay explicit.
Measurements do not scan retained history. The database file-size change is
shown separately because growth is not the same as bytes written.

Daily totals are measured during the shown coverage, never extrapolated from a
startup burst. Memory limits and a low-write `sbc-1g` profile are configurable;
see [resource profiles and meter definitions](harbour/REPORTS.md#memory-limits-and-small-boards).

SQLite remains the default database. More reads/writes alone do not establish a
need for another engine: measure sustained writer contention and reduce needless
writes/batch transactions first. PostgreSQL remains a possible future deployment
option; it is not included in this release, and switching engines would not by
itself reduce SD-card wear. See [SQLite's deployment guidance](https://www.sqlite.org/whentouse.html)
and [WAL concurrency](https://www.sqlite.org/wal.html).

### Docker operation output

The operation progress dialog updates Docker pull layers and Compose container
statuses in place. Output uses terminal cursor movements, with plain text kept
in the job history (up to 60,000 characters). Closing and reopening the dialog
restores the current view. Scroll up to read earlier output without following
new lines; return to the bottom to follow again. Long lines scroll horizontally
to keep progress columns aligned, including on mobile.

### OpenWRT routers and network diagnostics

Choose **OpenWRT** as the third server type for read-only router inspection.
Harbour discovers the model, OpenWrt version, kernel and available probes without
requiring Python on the router. Use an existing authorised SSH credential.
Router configuration changes, key installation and host-collector installation
are disabled for this type.

When deployed on the on-site server, the separate `network-recorder` service
records 250 ms–2 s ICMP probes alongside approximately one-second router CPU,
interface, Wi-Fi and queue readings. Open **Network → Recording settings** to
enable recording, choose targets/WAN interface and follow a calling device.
Mark call degradation/recovery and export the surrounding raw evidence. Use
**Incidents → Analyse** for a readable evidence report, or **Explore
incident patterns** to compare retained incidents and download structured results.
Analysis runs locally and separates observations, possible contributors and
missing evidence; it does not claim a proven cause. **Export condensed evidence brief**
in the analysis overlay pairs each conclusion with measured support in Markdown
or JSON, ready to copy or download for independent review. The recorder continues
during remote-access loss and web-container restarts.
Probes, router collection, database writes and archives run in separate processes.
A configurable 128 MiB RAM buffer per router absorbs storage delays, with live
backlog/loss reporting and acknowledged writes. Linux receive timestamps separate
late collection from network latency; uncertain readings remain labelled evidence.
Retention is configurable per router. Defaults keep seven days / 1 GiB of raw
history and automatically save compressed daily files for 90 days / 4 GiB on the
on-site server. Download finished files from the Archives tab; row and size caps
can be disabled in Recording settings.
Up to 12 IP probe targets are supported. Optional **Remote call flow tests** apply
bounded, simultaneous UDP upload/download loads using FaceTime (provisional),
Teams and Zoom profiles. They retain receiver loss/jitter evidence and mark the
router timeline. See [the research, profile limits and remote Docker endpoint
setup](harbour/CALL_TESTS.md). Tests run on the Harbour host and do not reproduce
the caller’s Wi-Fi or a real vendor call.
The **Call tests** tab includes the WebUI remote installer: deploy to
`~/harbour-c-remote`, retain remote logs in `logs/YY-MM-DD` (UTC), and download
them over token-authenticated HTTP without SSH. The log port and token are set
during deployment. The test overlay accepts a one-shot SSH password, deploys
without sudo or a saved server, and waits for startup confirmation. Deployment
output stays visible until you close it or continue. **Download output** saves the
transcript; **Saved installs** (also in the main Menu) retains administrator-only
Install artifacts on Harbour indefinitely, including failed and interrupted
attempts, with credentials excluded. Tests support
up to 60 minutes and multiple named recurring schedules, with **Edit**, **Start
now**, **Pause / Resume** and per-schedule result history. Running tests show
directional throughput, loss and jitter with in-place updates every five seconds.
Redeploy older endpoints for live upload receiver readings. Analysis correlates local and
remote evidence, with a combined compressed download. The OpenWrt **Analysis** tab
adds evidence-backed conclusions across ping and directional call flows, router
associations, and comparable recurring-test totals. **Download all conclusions in
period (.json.gz)** creates a self-contained handoff with readable conclusions,
referenced measurements, timing limits, exclusions and content hashes for a new
analysis session. Individual conclusions can also be downloaded. Analysis runs
locally against saved evidence; use **Analyse saved evidence** to include older
retained tests and sealed incidents. Network, Wireless, Incidents, Call tests, Analysis, Archives and
Activity have separate tabs; Recorder and Router probe cards adapt to the screen.
OpenWRT servers also have a **Reports** tab. Harbour checkpoints compact facts
alongside each raw archive append, then publishes the previous calendar day's
evidence timeline and statistics on a configurable schedule (03:00 Europe/London
by default). Archive rotation and raw-file cleanup follow that schedule. Raw
files are removed only after report publication succeeds; the bounded live
SQLite cache is still maintained throughout the day.

Reports include incident markers, grouped probe/router observations, collection
gaps, call-test episodes, selected supporting measurements and coverage limits.
Late data revises the measurement day it belongs to. Choose a day, a calendar
week or custom dates, open the HTML report, and use browser Print to save a PDF.
No PDF engine or external analysis service is installed on the server. Missing
days remain explicitly unavailable. Full details: [scheduled evidence reports](harbour/REPORTS.md).

See [OpenWRT deployment, measurement limits and retention](harbour/OPENWRT.md).

Harbour's container uses Python 3.15.0 with exact image references and hashed
dependency locks. See [stack versions, compatibility notes and the update
procedure](harbour/DEPENDENCIES.md), including the Paramiko 5 SSH requirements.

### Resource recording and reboot tracking

Resource checks reuse a pinned SSH connection and a session-bound Python reader.
Hosts without an installed recorder are sampled only when Harbour requests a
reading. Installing **Server settings → Install recorder** automatically enables
durable local sampling for that host. Setup offers **Push to host**, **Push &
extract**, and **Push & install**. Uploads land in the SSH user's home directory;
only installation needs sudo, with a one-time password prompt and live output.
Setup uses its own SSH connection so polling continues. The same controls are
available in **Logins → Collector setup**. Harbour fetches the recorder's private
socket through the same SSH connection; no additional network port is opened.
An unavailable installed recorder produces a warning and remote-probe fallback.

Server settings now separate the probe/collection interval, local sample interval
(60 seconds by default), disk capacity interval (300 seconds) and Docker inventory
interval (300 seconds). Cached disk readings retain their measurement time and
are not repeatedly added to history. Collected batches keep their source times;
retries are deduplicated and acknowledged only after storage. Alerts use the
latest collected reading, so longer collection intervals delay alert detection.

**Boot history**, beside Uptime, records distinct host boot identities, including
boots recovered from queued samples. Restarting the recorder does not count as a
host reboot. Uptime is retained in resource history. Remote probes cannot detect
every reboot between checks.

**Remove recorder** provides host-side removal instructions. Removal retains the
local queue unless `--purge-data` is explicitly supplied and returns Harbour to
remote probing. Removing a server entry alone does not uninstall host services.
See [resource recorder installation, retention and off-boarding](harbour/RESOURCE_RECORDER.md).

### Login and authentication history

**Server → Logins** is available on both Docker and Plain server entries. Related
authentication, session-open and session-close events appear in one session row,
with the source IP, SSH key and duration when the evidence supports a match.
**Currently connected** lists SSH sessions verified by the host's latest fresh
process snapshot; its checked time follows the monitoring interval. Stale or
unavailable snapshots do not establish that someone is still connected.
Use **Raw events** or expand a session to inspect the original evidence and its
timestamps. The **Harbour key** label means that the verified credential and SSH
account match Harbour's configured monitoring connection; it does not identify
a separate person. Filter by result, user, IP or key, and optionally hide those records.

Install the permanent host collector once using **Logins → Collector setup**.
The downloadable bundle includes an installer for Linux systemd and macOS
launchd. Like resource-recorder installation, login-collector installation needs administrator
privileges. Routine probes use the existing SSH account and a restricted local
socket. Events queue locally during disconnection, and are acknowledged only
after Harbour has saved them. Harbour keeps received events for 90 days.

Linux reads the journal or an authentication log. macOS log-only coverage is
limited; the optional native helper adds OS authentication and graphical-session
events, and requires Apple-approved Endpoint Security signing and Full Disk
Access. Source failures, partial coverage and local queue losses are visible in
the tab. See [collector installation, coverage and lifecycle details](harbour/LOGIN_COLLECTOR.md).

### Browsing data tables

Click a column heading to switch between ascending and descending order, and
use the field beneath it to filter that column. Date fields select a day;
numeric comparisons such as `>=10` are supported. Choose 25, 50 or 100 rows per
page, use Previous/Next, or enter a page number. Filters apply before pagination
and remain in place during live updates. Login history, Harbour sign-ins and
saved installs query their retained records; diagnostic tables use the displayed
window or report, with existing coverage limits preserved. Containers, Storage
and Activity keep their existing controls.

OpenWrt headings keep short status notes inline. The adjacent information icons
hold measurement and retention explanations; operational warnings stay visible.

### Monitoring macOS

Both Intel and Apple silicon Macs use the same setup:

1. Enable **Remote Login** in macOS Sharing settings and allow the selected SSH account.
2. Install Python 3.9+ (for example, with Homebrew). Harbour checks the standard Apple silicon and Intel Homebrew command locations over SSH; it does not depend on your interactive shell configuration. No Python monitoring packages or root access are required.
3. Add the Mac as **Plain server** for resources, or **Docker host** for resources and Docker services. For Docker Desktop, connect as the macOS account that runs it, keep Desktop running, and select a Docker context that targets that Mac's engine. Docker and Compose must work for that account over SSH. Harbour includes Docker Desktop's standard CLI/helper paths and preserves the account's Docker context; it does not start the engine or switch contexts. See [Docker's macOS CLI and context documentation](https://docs.docker.com/desktop/setup/install/mac-permission-requirements/).

Resource cards measure the **Mac itself**, while container information comes from the selected Docker engine (typically a Linux VM). CPU, memory, storage, uptime, timezone and 1/5/15-minute load history are supported. Memory usage excludes free and inactive pages; compressed memory remains used. This is a capacity percentage, not macOS memory pressure. The writable startup volume appears as `/`; hidden system, simulator and Time Machine mounts are excluded. APFS usage includes other volumes and snapshots sharing its container, so APFS rows sharing a container must not be added together. Optional temperature, fan, battery and electrical sensors are read from Apple SMC and the macOS battery interface; see the support matrix below.

### Platform and sensor support

The same collectors run over SSH for **Docker host** and **Plain server** entries. Readings describe the SSH host. Connecting to a container or VM only reveals the hardware that environment exposes; Docker Desktop's Linux VM cannot supply the Mac's physical sensors. Connect to macOS itself to monitor the Mac.

**✅ Implemented** means Harbour has a collector for the interface, conditional on the model, driver and account permissions. It does **not** mean every machine in that family has been tested. **✅ Bench** marks readings checked on our MacBook Pro; **✅ User** records user confirmation. **◻️ Planned** means an adapter or validation remains on the roadmap. **❌ Unsupported** marks an OS with no host collector. **—** means normally absent or no separate sensor.

| Host / hardware family | CPU / SoC temperature | GPU temperature | Fan RPM | Battery | Power / voltage / current | Validation / approach |
|---|---|---|---|---|---|---|
| Linux · Intel desktops / mini PCs | ✅ `coretemp` / thermal | ✅ If GPU exports hwmon¹ | ✅ Motherboard hwmon | — | ✅ Exposed hwmon channels; readable RAPL energy² | Driver dependent |
| Linux · AMD desktops / mini PCs | ✅ `k10temp` / thermal³ | ✅ `amdgpu` hwmon¹ | ✅ Motherboard / GPU hwmon | — | ✅ Exposed hwmon channels; readable powercap energy² | Driver dependent |
| Linux · Lenovo ThinkPad | ✅ CPU / `thinkpad_acpi` | ✅ If GPU exports hwmon¹ | ✅ `thinkpad_acpi` / hwmon | ✅ `power_supply` | ✅ Exposed hwmon / battery channels² | Model dependent; no fan control |
| Linux · Dell laptops | ✅ CPU / `dell_smm` | ✅ If GPU exports hwmon¹ | ✅ `dell_smm` / hwmon | ✅ `power_supply` | ✅ Exposed hwmon / battery channels² | Supported driver models only |
| Linux · other laptops | ✅ CPU / thermal | ✅ If GPU exports hwmon¹ | ✅ If hwmon exposes tachometer | ✅ `power_supply` | ✅ Exposed hwmon / battery channels² | Firmware / driver dependent |
| Linux · rack / tower servers | ✅ CPU / hwmon | ✅ If GPU exports hwmon¹ | ✅ Local hwmon; ◻️ BMC adapter | — | ✅ Local channels; ◻️ BMC PSU readings | IPMI / Redfish adapter planned |
| Linux · Raspberry Pi 3B-series | ✅ SoC thermal | — Shared SoC | — Stock board | — Stock board | — Stock board; ✅ exposed add-on hwmon | ✅ User: board identification and monitoring confirmed |
| Linux · Pi Zero / 4 | ✅ SoC thermal | — Shared SoC | ✅ Exposed add-on tachometer | — Stock board | ✅ Exposed add-on hwmon | Generic Linux path; not bench tested |
| Linux · Raspberry Pi 5 | ✅ SoC thermal | — Shared SoC | ✅ `pwm-fan` with tachometer | — Stock board | ✅ Exposed hwmon; ◻️ `pmic_read_adc` adapter | PMIC rails are not total board / wall power |
| Linux · Rockchip / Orange Pi / Radxa | ✅ Exposed thermal zones | ✅ Exposed GPU thermal zone | ✅ Exposed tachometer | ✅ If `power_supply` battery exists | ✅ Exposed hwmon rails | Board / kernel dependent; two-wire fans lack tachometer |
| Linux · NVIDIA Jetson | ✅ Exposed thermal / hwmon | ✅ Exposed thermal / hwmon | ✅ Standard hwmon; ◻️ custom tach path | — Usually absent | ✅ Exposed INA3221 hwmon; ◻️ `tegrastats` adapter | Carrier and BSP dependent |
| Linux · other ARM boards | ✅ Exposed thermal / hwmon | ✅ If exposed | ✅ If exposed | ✅ If exposed | ✅ If exposed | No blanket ARM-board guarantee |
| macOS · M1 Max MacBook Pro | ✅ Bench: SMC die average / maximum | ✅ Bench: four SMC channels | ✅ Bench | ✅ Bench: charge, cycles, V / A | ✅ Bench: SMC system estimate / DC input | MacBookPro18,2; unprivileged account |
| macOS · other Apple silicon | ✅ Known SMC die keys, if present | ◻️ Model-specific maps | ✅ SMC, if present | ✅ macOS battery, if present | ✅ Known SMC keys, if present | Collector implemented; model validation planned |
| macOS · Intel | ✅ SMC die / proximity, if present⁴ | ✅ SMC die / proximity, if present | ✅ SMC, if present | ✅ macOS battery, if present | ✅ Known SMC keys, if present | Collector implemented; hardware validation planned |
| Windows / FreeBSD | ❌ | ❌ | ❌ | ❌ | ❌ | Host collectors not implemented; future scope |

1. AMD, Nouveau and supported Intel GPU drivers may expose Linux hwmon readings. The proprietary **NVIDIA NVML / `nvidia-smi` adapter is ◻️ planned**. A GPU without a hwmon interface is currently unavailable. NVIDIA fan percentage would be separate from measured RPM.
2. Watts, volts and amps are read only when exported. RAPL / powercap and hwmon energy counters are shown in **joules**, including their possible wrap/reset; derived RAPL watts are **◻️ planned**. Package, SoC, GPU, battery and rail readings can overlap and are never summed. APU power can include CPU and GPU. Battery power is not adapter input or wall-plug power.
3. AMD `Tdie` can be selected as the CPU temperature. `Tctl`, core and CCD readings remain separate; Harbour does not substitute a hotter auxiliary sensor for a missing package reading.
4. Intel Mac die/proximity readings remain separate sensors until a validated primary mapping is available. Apple silicon's `SMC · CPU die average` is explicitly labelled; it is an undocumented SMC aggregate, not a calibrated package-junction guarantee. No broad SMC-prefix averaging is used. Unknown keys are omitted.

The Raspberry Pi 3B-series confirmation covers the user's existing board identification and monitoring; it does not claim fan, battery or power sensors on the stock board. Other rows show interface coverage, not a tested-device inventory.

Discovered resources appear as cards in the main server pane. Open **Hardware sensors** below the cards for the full reading list and battery status. Each reading has a **History** button; **Explore history → Hardware sensors** also includes sensors seen earlier in the selected period. The selected sensor gets its own units and scale, with sample-weighted averages and peaks. Missing readings are excluded, not recorded as zero. Sensor history begins after upgrading; earlier temperature history remains accessible. CPU, memory and eligible temperature readings inherit existing warning defaults. Load, fan, battery and electrical warnings start disabled; enable them with limits in Server settings.

Collectors only read existing interfaces. They do not install software, load drivers, change permissions, enable fan control or require `sudo`. Unavailable optional sensors leave the other resource readings usable. macOS SMC support uses private, model-dependent interfaces and can change with an OS update. Linux batteries use matching full/design capacity units for capacity health; macOS only reports this ratio when raw full and design capacity are both exposed.

Interface references: [Linux hwmon ABI](https://www.kernel.org/doc/html/latest/hwmon/sysfs-interface.html), [Linux power supplies](https://www.kernel.org/doc/html/latest/power/power_supply_class.html), [ThinkPad ACPI](https://www.kernel.org/doc/html/latest/admin-guide/laptops/thinkpad-acpi.html), [Dell SMM](https://www.kernel.org/doc/html/latest/hwmon/dell-smm-hwmon.html), [Linux powercap](https://www.kernel.org/doc/html/latest/power/powercap/powercap.html), [Raspberry Pi hardware documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html), and [Stats' model-specific SMC mappings](https://github.com/exelban/stats/blob/master/Modules/Sensors/values.swift).

### Monitoring and alerts

**Server settings → Resources** lists CPU, memory, each load period and every discovered sensor alongside Volumes. The three checkboxes work independently within the monitoring requirement:

- **Monitor** records future history. Turning it off also disables Warn and Use in cards. Discovery continues so the current reading and available choices remain visible in settings; stored history is retained.
- **Warn** enables per-resource lower and/or upper limits, in that resource's units. Equality triggers a warning. Leave an individual limit blank to turn it off; enabling Warn requires at least one limit. Choose **Inherited** for the CPU, memory or temperature limits above the table, or **Custom** for individual limits. Other resources require custom limits. Low limits can flag slow fans or low battery charge; signed battery current supports negative limits.
- **Use in cards** selects the cards in the main pane; the existing CPU, memory and primary temperature summary chips follow those selections too. Each selected sensor has its own history chart. Load periods share one card and can be selected individually. Volume card selection stays in Volumes.

New discoveries default to monitored and visible in cards. Harbour remembers discovered sensors and their preferences when readings disappear; unavailable values are never replaced with zero. Changing a sensor's units/source starts a distinct configuration and history series. Warning indicators, the warning drawer and dismissals use the same per-resource limits. Optional Telegram delivery uses the existing CPU, Memory and Temp categories, plus **Resources** for load, fans, batteries and electrical sensors. Missing individual readings break their alert-duration continuity without re-arming an already delivered issue, and do not stop alerts for other available sensors. Changing warning settings cancels pending alerts for the affected resource until a fresh poll.

Administrators can choose **Small, Medium or Large** for each resource card using its **ⓘ** menu. Small shows current readings; each Small card occupies its own half-height slot. You can place a second Small card below it to match the height of one Medium or Large card, including the gap. Small cards never pair automatically. Medium adds recent history; Large uses the same height as Medium and is 1.5× wider, with readings beside the chart and source/limit details below. Cards fit the available width on narrow screens. **Set all cards** applies one size to the server's cards. The same size controls appear in **Server settings → Resources**, with a shared size for the three load periods and a Disk card size beside Volumes. The whole settings overlay scrolls vertically.

Drag a card's grip into any empty grid position; a floating card and shadow show the move, and an outline previews the drop. Occupied positions are marked red. Empty spaces remain available, and moving one card leaves the others in place. Focus the grip and use the arrow keys for keyboard rearrangement. Sizes and positions are saved per server in the database and shared across browsers. The **ⓘ** menu also edits Monitoring, Warning, Resource card and applicable warning limits; press **Save** to apply them together. The Disk menu controls the volumes currently selected for its card. Restore hidden resources or volumes in Server settings. Layout-only changes preserve warning timers and monitoring history.

For Telegram alerts, open **Menu → Notifications**, save your bot token and Chat ID, select warnings per server, and set their trigger and repeat intervals. Use **Send test message** to check delivery; a repeat interval of **0** sends once until the issue clears.

The resource overview shows current **1-, 5-, and 15-minute load averages** together with a three-line history chart. Open **Explore history → Load average** in Resource tabs to inspect them, or use the combined, side-by-side and table views. Selected load periods are recorded at the normal polling interval, with averages and peaks preserved through history consolidation. Optional warning limits are configured per period in Resources. Load history starts with the first successful poll after the update; earlier records have no load values.

**CPU usage averages the interval between successful resource samples**, using the difference in Linux CPU counters or macOS Mach CPU counters. The CPU card shows the actual averaging period, including missed polls. The recorder saves its own baseline in local mode; Harbour saves the remote-probe baseline across its restarts; the first reading after an upgrade, remote reboot, connection change or counter reset waits for the next poll while other resources remain available. CPU warnings use this interval average, and recorded CPU peaks are the highest interval averages, not momentary spikes. Existing history keeps its original readings. Monitoring overhead remains part of real CPU usage, spread across the measured interval.

To update, keep your existing `.env` and data volume:

```sh
git pull
python3 scripts/start.py
```

Back up `.env` and the `harbour-data` Docker volume together; the application secret is needed to decrypt saved credentials.
