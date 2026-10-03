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

Open your configured URL and sign in. Use **Menu → Add server** to connect a Linux host with Python 3.9+, Docker and Compose v2 installed, using an SSH account with Docker access. Fetch and accept its host fingerprint, then generate or import an SSH key and install it from the form using a one-time password, or copy it manually. Optional password login is available after acknowledging its warning. Use **Server settings → Edit SSH connection** to switch authentication methods or replace keys without deleting the server.

To update, keep your existing `.env` and data volume:

```sh
git pull
python3 scripts/start.py
```

Back up `.env` and the `harbour-data` Docker volume together; the application secret is needed to decrypt saved credentials.
