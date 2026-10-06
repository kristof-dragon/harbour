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

Resource cards measure the **Mac itself**, while container information comes from the selected Docker engine (typically a Linux VM). CPU, memory, storage, uptime, timezone and 1/5/15-minute load history are supported. Memory usage excludes free and inactive pages; compressed memory remains used. This is a capacity percentage, not macOS memory pressure. The writable startup volume appears as `/`; hidden system, simulator and Time Machine mounts are excluded. APFS usage includes other volumes and snapshots sharing its container, so APFS rows sharing a container must not be added together. Temperature is unavailable on macOS with the built-in collector and does not generate temperature warnings.

### Monitoring and alerts

For Telegram alerts, open **Menu → Notifications**, save your bot token and Chat ID, select warnings per server, and set their trigger and repeat intervals. Use **Send test message** to check delivery; a repeat interval of **0** sends once until the issue clears.

The resource overview shows current **1-, 5-, and 15-minute load averages** together with a three-line history chart. Open **Explore history → Load average** in Resource tabs to inspect them, or use the combined, side-by-side and table views. Load is recorded at the normal polling interval, with averages and peaks preserved through history consolidation; it does not trigger warnings. Load history starts with the first successful poll after the update; earlier records have no load values.

**CPU usage averages the interval between successful resource polls**, using the difference in Linux CPU counters or macOS Mach CPU counters. The CPU card shows the actual averaging period, including missed polls. Harbour saves the baseline across its own restarts; the first reading after an upgrade, remote reboot, connection change or counter reset waits for the next poll while other resources remain available. CPU warnings use this interval average, and recorded CPU peaks are the highest interval averages, not momentary spikes. Existing history keeps its original readings. Monitoring overhead remains part of real CPU usage, spread across the measured interval.

To update, keep your existing `.env` and data volume:

```sh
git pull
python3 scripts/start.py
```

Back up `.env` and the `harbour-data` Docker volume together; the application secret is needed to decrypt saved credentials.
