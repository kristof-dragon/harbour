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

The interactive setup creates `.env`, generates the application secret, and stores a salted hash of your chosen administrator password. The start script builds and starts Docker, then removes the first-admin credentials from `.env` and sets `FIRST_RUN=False` after successful setup.

For Nginx Proxy Manager on a separate instance, choose setup option **2**. In NPM, forward **HTTP** to the Harbour host’s LAN IP and your selected port (for example, **8384**), and enable HTTPS for your configured hostname. The container’s internal port remains **8080**.

Open your configured URL and sign in. Use **Menu → Add server** to connect a Linux or macOS host with Python 3.9+. The OS is detected automatically. Choose **Docker host** (also requires Docker, Compose v2 and an SSH account with Docker access) or **Plain server** for resource monitoring. Fetch and accept its host fingerprint, then generate or import an SSH key and install it from the form using a one-time password, or copy it manually. Optional password login is available after acknowledging its warning. Use **Server settings → Edit SSH connection** to switch authentication methods or replace keys without deleting the server.

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

Administrators can choose **Small, Medium or Large** for each resource card using its **ⓘ** menu. Small shows current readings; Medium adds recent history; Large uses the same height as Medium and is 1.5× wider, with readings beside the chart and source/limit details below. Cards fit the available width on narrow screens. **Set all cards** applies one size to the server's cards. The same size controls appear in **Server settings → Resources**, with a shared size for the three load periods and a Disk card size beside Volumes. The whole settings overlay scrolls vertically.

Drag a card's grip to arrange the grid; a floating card and shadow show the move. Focus the grip and use the arrow keys for keyboard rearrangement. Sizes and order are saved per server in the database and shared across browsers. The **ⓘ** menu also edits Monitoring, Warning, Resource card and applicable warning limits; press **Save** to apply them together. The Disk menu controls the volumes currently selected for its card. Restore hidden resources or volumes in Server settings. Layout-only changes preserve warning timers and monitoring history.

For Telegram alerts, open **Menu → Notifications**, save your bot token and Chat ID, select warnings per server, and set their trigger and repeat intervals. Use **Send test message** to check delivery; a repeat interval of **0** sends once until the issue clears.

The resource overview shows current **1-, 5-, and 15-minute load averages** together with a three-line history chart. Open **Explore history → Load average** in Resource tabs to inspect them, or use the combined, side-by-side and table views. Selected load periods are recorded at the normal polling interval, with averages and peaks preserved through history consolidation. Optional warning limits are configured per period in Resources. Load history starts with the first successful poll after the update; earlier records have no load values.

**CPU usage averages the interval between successful resource polls**, using the difference in Linux CPU counters or macOS Mach CPU counters. The CPU card shows the actual averaging period, including missed polls. Harbour saves the baseline across its own restarts; the first reading after an upgrade, remote reboot, connection change or counter reset waits for the next poll while other resources remain available. CPU warnings use this interval average, and recorded CPU peaks are the highest interval averages, not momentary spikes. Existing history keeps its original readings. Monitoring overhead remains part of real CPU usage, spread across the measured interval.

To update, keep your existing `.env` and data volume:

```sh
git pull
python3 scripts/start.py
```

Back up `.env` and the `harbour-data` Docker volume together; the application secret is needed to decrypt saved credentials.
