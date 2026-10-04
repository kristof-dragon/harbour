# Harbour

A self-hosted dashboard for monitoring Linux servers and managing their Docker services over SSH.

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

Open your configured URL and sign in. Use **Menu → Add server** to connect a Linux host with Python 3.9+. Choose **Docker host** (also requires Docker, Compose v2 and an SSH account with Docker access) or **Plain server** for resource monitoring. Fetch and accept its host fingerprint, then generate or import an SSH key and install it from the form using a one-time password, or copy it manually. Optional password login is available after acknowledging its warning. Use **Server settings → Edit SSH connection** to switch authentication methods or replace keys without deleting the server.

For Telegram alerts, open **Menu → Notifications**, save your bot token and Chat ID, select warnings per server, and set their trigger and repeat intervals. Use **Send test message** to check delivery; a repeat interval of **0** sends once until the issue clears.

The resource overview shows current **1-, 5-, and 15-minute load averages** together with a three-line history chart. Open **Explore history → Load average** in Resource tabs to inspect them, or use the combined, side-by-side and table views. Load is recorded at the normal polling interval, with averages and peaks preserved through history consolidation; it does not trigger warnings. Load history starts with the first successful poll after the update; earlier records have no load values.

**CPU usage averages the interval between successful resource polls**, using the difference in Linux CPU counters. The CPU card shows the actual averaging period, including missed polls. Harbour saves the baseline across its own restarts; the first reading after an upgrade, remote reboot, connection change or counter reset waits for the next poll while other resources remain available. CPU warnings use this interval average, and recorded CPU peaks are the highest interval averages, not momentary spikes. Existing history keeps its original readings. Monitoring overhead remains part of real CPU usage, spread across the measured interval.

To update, keep your existing `.env` and data volume:

```sh
git pull
python3 scripts/start.py
```

Back up `.env` and the `harbour-data` Docker volume together; the application secret is needed to decrypt saved credentials.
