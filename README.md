# Harbour

A self-hosted, Docker-packaged dashboard for Linux servers and their Docker services.

## Included

- A resizable server sidebar (23% by default), with saved width, Compact / Normal / Comfortable density, light/dark theme, and responsive mobile navigation. Tap outside the mobile panel, its close button, or Escape to dismiss it.
- CPU, memory, mounted-filesystem usage and temperature sensors; OS distribution, kernel, uptime and server timezone. Disk readings include decimal GB, percentage used and available space; Linux reserved blocks can make used + available smaller than total.
- Global warning thresholds and server overrides. Disk warnings trigger on **either** percentage used **or** remaining GB. Warnings appear first in the overview, tint affected resource chips and have instant bullet-list tooltips. Active server warnings pulse red, respecting reduced-motion preferences.
- Compose projects initially collapsed; expand a project to see its services, then expand a service for version, running image ID, health, ports, mounts and controls.
- Admin-only pull, Compose up and restart actions, including multiple selected stacks/services. A command preview precedes execution; jobs are serialized per host and recorded with the actor, status and output. Partial failures are recorded.
- Separate warning/update indicators and notification menu. Update dismissal is per user and per image config digest; another digest notifies again. Active warnings clear when their cause resolves.
- Administrators manage users and hosts. Regular users can read **all** hosts and dismiss their own notices; no Docker/SSH changes. There is no per-server access control in this version.
- Dedicated Ed25519, ECDSA or RSA key generation with valid size tiers, or existing-key import. Only the public key is returned by generation. Private keys and optional passphrases are encrypted in SQLite using the application secret.
- Server renaming, SSH latency and UP / DOWN / check-failed / stale / paused states.
- Background monitoring with global/per-host intervals; SQLite history with staged resolution and retention.
- Resource-card trends and a history overlay: resource tabs, table, side-by-side charts, combined chart; selectable ranges, resolutions, averages and peaks.
- TOTP 2FA, recovery codes, idle/absolute session timeouts, IP/browser binding, five-failure IP bans and login-attempt history.
- A demo isolated from production. Demo mode blocks real SSH and credential storage.

## Start with Docker

On your Docker host, you need **Docker Engine, Docker Compose v2 with `up --wait` support, Python 3.11+ and Git**. The setup scripts use Python's standard library; no local pip install is required. Harbour runs in the container. For your internal Nginx Proxy Manager deployment, have an HTTPS hostname/certificate and the proxy's address/network ready.

```sh
git clone https://github.com/kristof-dragon/harbour.git
cd harbour
python3 scripts/setup_env.py
python3 scripts/start.py
```

The interactive setup asks for the first administrator's username/password, the host port, and your proxy settings. Choose option **1** when NPM and Harbour share a dedicated Docker network on the same host. Choose **2** when NPM connects to the Harbour host through its LAN IP and selected host port (including NPM on another machine); this option defaults to listening on all IPv4 interfaces, with an optional specific bind address. It does not require entering the host's LAN IP during setup. Option **4** is for a reverse proxy running directly on the host, outside Docker. Local HTTP is available for a loopback-only trial. The wizard creates `.env` with owner-only permissions (`600`) and refuses to overwrite an existing file.

Your password is entered privately and **only its salted scrypt hash** is written to `.env`. Each hash has a fresh random 128-bit salt. A separate 384-bit random `HARBOUR_SECRET` protects encrypted SSH keys and 2FA data. An encryption key does not need a password salt. No plaintext password is written to disk by the wizard; save your chosen password in your password manager.

**Use `python3 scripts/start.py` for the first startup.** It runs `docker compose up -d --build --wait`, checks the database's committed receipt for this exact first administrator, then atomically removes `HARBOUR_ADMIN`, `HARBOUR_ADMIN_PASSWORD_HASH` and `HARBOUR_BOOTSTRAP_ID` from `.env` and sets `FIRST_RUN=False`. It recreates the container so the bootstrap hash also disappears from its environment. `HARBOUR_SECRET` and ordinary configuration remain. The web container never gets a writable mount of your host `.env` or Docker socket.

If startup or receipt verification fails, the bootstrap fields are retained. If interrupted after cleanup, rerunning the start script finishes bringing up the container without credentials. Existing users are never overwritten, and `FIRST_RUN=False` refuses to seed an empty database. An unexpected empty volume therefore fails visibly instead of silently creating a new admin. Do not set it back to True for an existing installation; restore the correct volume instead.

A bare `docker compose up -d --build` **does start Harbour**, but cannot clean your host `.env`. If you already used it for first boot, run `python3 scripts/start.py` to complete the same verified cleanup. Once `FIRST_RUN=False`, either command is suitable for subsequent starts; the script additionally waits for health and checks that bootstrap fields are absent. It intentionally reads the generated, unquoted `KEY=value` format and ignores shell overrides of Harbour/Compose variables. Edit non-secret values in `.env` directly for configuration changes.

Open your configured HTTPS origin, or `http://localhost:<your-selected-port>` for the local trial, and sign in with the account you chose. First-run credentials are used **only for an empty database**. Use **Menu → My account** to change your password or enable 2FA later.

The container runs as UID 10001, with a read-only root filesystem, no Linux capabilities and no Docker socket mount. Data persists in the `harbour-data` named volume (prefixed by the Compose project). One process owns background polling and host locks; do not add multiple workers or replicas against this database. Keep `COMPOSE_PROJECT_NAME` unchanged after setup so the same data volume is used. Do not use `docker compose down -v` unless intentionally deleting all Harbour data.

The Compose default publishes the selected port on all IPv4 host interfaces (`0.0.0.0`), so a separate LAN proxy can reach it without a specific bind address. The wizard uses that default for option 2, and loopback for the local trial, host-local proxy and shared-Docker-network choices. `HARBOUR_BIND_ADDRESS` is optional; set it to a specific host address only when you want to restrict listening. Keep access restricted to your LAN/proxy through the host firewall and router. Use HTTPS at NPM, the exact `HARBOUR_ORIGIN`, secure cookies, and explicit trusted proxies. Forwarded client IPs are ignored from all other peers.

Docker references: [Compose startup/build and health waiting](https://docs.docker.com/reference/cli/docker/compose/up/), [Compose environment files](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

## Sidebar and appearance

Server cards show the name, then status / address / SSH latency, then resource chips. Warning, status and update filters above the list combine together; **Clear filters** restores the whole list. “Other / paused” covers pending, failed-check, stale and paused states instead of mislabelling these as offline. Update filters use the current account's undismissed update badges; dismissed updates count as none. Filtering the sidebar does not change the server currently open in the main view.

The bottom **Menu** expands/collapses, with **Add server** first for administrators. **Visuals** offers Compact, Normal and Comfortable card density and automatic system light/dark theme. Changes save immediately in this browser. The toolbar theme button selects a manual theme; enable system following again in Visuals. The version stays at the bottom left. On mobile, the menu button opens the server pane; tapping outside it, its close button or Escape dismisses it.

## Onboard a server

1. On the target Linux server, have **Python 3.9+**, **Docker CLI/Engine** and **Docker Compose v2** available to the dedicated SSH account in its noninteractive PATH. Compose definitions and their `.env` files must remain at the paths recorded in the containers’ Compose labels.
2. In Harbour, expand **Menu → Add server**. Enter the display name, hostname/IP, SSH username and port.
3. Obtain the host fingerprint through the server console or an already-trusted connection, for example:

   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
   ```

   Paste the complete `SHA256:…` fingerprint. This must match the host key negotiated by SSH. A mismatch fails closed; Harbour never automatically trusts a replacement key.
4. Generate a dedicated key in the UI, or import an existing OpenSSH/PEM private key and its passphrase. Add the displayed `restrict ssh-ed25519 … harbour` public-key line to that account’s `~/.ssh/authorized_keys` using your existing trusted administrative connection. Keep `.ssh` at mode 700 and `authorized_keys` at mode 600. Optionally add `from="<dashboard-source-IP>"` to restrict where the key can connect from.
5. Ensure the SSH account can access the intended Docker daemon without interactive sudo. Standard Docker-group membership is **effectively root access to the host**. Prefer a dedicated account and key per host; use rootless Docker where it fits your workloads. `restrict` blocks forwarding and PTYs; it does **not** restrict the key to read-only commands or reduce Docker privileges.
6. Select **Connect server**. Harbour saves the host and tests the connection in a background job. The dashboard shows any connection failure; refresh retries it. Use Server settings to rename the host, adjust its polling interval, pause checks or override thresholds. Remove/re-add it to replace connection details or its key in this version.

No persistent agent is installed on a host. The backend sends a fixed Python helper over verified SSH. Commands are constructed as argument arrays; the web UI cannot submit arbitrary shell commands. Docker control remains a privileged capability: protect the dashboard, its data volume, its application secret and all administrator accounts accordingly.

For stronger separation in a later version, an agent with mutually authenticated TLS and a narrower host-side operation policy is a sensible option. SSH is the simpler starting point and requires no new listening service.

## Update and operation behaviour

- **Pull** downloads the image for the configured tag. It does not change the running container.
- **Apply** runs `docker compose up -d`. It can recreate containers and cause downtime. Individual-service Apply uses `--no-deps`; a whole-project selection uses normal Compose dependency behaviour.
- **Restart** runs Compose restart for projects/services, or `docker restart` for standalone containers. It does not apply new images or changed configuration.
- Standalone containers can be pulled and restarted. Applying a new standalone image requires recreating it from its original deployment definition; Harbour deliberately does not guess that definition.
- Multiple stacks can be selected. If a selected target cannot support the action, the preview fails before any command runs. If one command fails after earlier commands succeeded, the job records partial output and stops; there is no automatic rollback.
- Image checks use the **existing configured tag**, comparing the running image ID against the registry’s matching-platform image config digest. They do not discover newer version tags or suggest major upgrades. Images pinned by digest are labelled **Pinned**. Local-only images, inaccessible private registries, unsupported manifest responses and registry errors show **Check failed**, never “current.” Registry authentication is the SSH account’s existing Docker CLI configuration on that host.
- “Running version” comes from the image’s `org.opencontainers.image.version` label when present; it is publisher metadata, not a query to the application. The configured image and immutable running image ID are also shown. Missing version metadata is explicitly labelled.
- Readings poll every 60 seconds by default; registry checks run every six hours and on demand. Both intervals are configurable. CPU is a 350ms sample; warning thresholds use the latest reading, with no hysteresis. Manual and background operations share a per-host lock. Slow registry checks/actions can delay readings; stale detection uses the greater of 120 seconds or twice the polling interval plus 15 seconds. Service actions require a successful reading within two minutes, prompting a manual refresh on slower polling schedules.
- Original Compose paths must be accessible. Missing labels/definitions, unresolved Compose environment variables or profiles can require correction on the host. Discovery lists existing containers, including stopped ones; never-created services/profiles cannot be inferred from container discovery.
- The standard remote command timeout is 30 seconds (45 per registry lookup, 300 per Docker operation); ordinary monitoring has a 90-second response limit; update checks and Docker actions have a 15-minute response limit. Connection setup and the latency probe add their own short timeouts. If a connection drops or the application restarts, remote work may still be running. Check the host before retrying an interrupted/failed action.

Docker references: [SSH access and daemon protection](https://docs.docker.com/engine/security/protect-access/), [Docker privileges](https://docs.docker.com/engine/security/), [Compose pull](https://docs.docker.com/reference/cli/docker/compose/pull/), [Compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/).

## Nginx Proxy Manager (internal HTTPS)

Terminate HTTPS in NPM using a certificate trusted by your browsers. Set these deployment variables before starting Harbour:

```dotenv
HARBOUR_ORIGIN=https://harbour.your-internal-domain
HARBOUR_SECURE_COOKIE=true
HARBOUR_TRUSTED_PROXIES=192.0.2.200/32
```

Replace the example origin and proxy address with your actual values. Trust the **socket peer address Harbour sees for NPM**, preferably a fixed address on a dedicated Docker network. Avoid a shared subnet; never use `0.0.0.0/0` or `::/0` (rejected at startup). Forward the original client IP in `X-Forwarded-For`. Harbour walks the chain from the trusted proxy towards the client and ignores forwarded headers from untrusted direct peers. Keep Uvicorn's `--no-proxy-headers` flag: Harbour performs this validation itself.

If NPM runs in Docker on the same host, join NPM to a dedicated private network first. The setup wizard records its existing name in `HARBOUR_PROXY_NETWORK` and selects `compose.npm.yaml` through `COMPOSE_FILE`; Harbour then joins that network automatically. In NPM, select scheme **http**, forward hostname **harbour**, forward port **8080**, and enable SSL for your internal hostname. Do not use `localhost` as NPM's upstream. The shared-network wizard option restricts Harbour's published port to loopback. The supplied loopback port binding works for a proxy on the host, not an unrelated proxy container. Do not expose the backend directly on an untrusted network. If a shared NAT makes many users appear to have the same IP, a ban affects all of them.

### Selected port and a 502 from NPM

**`HARBOUR_PORT` controls the published host port.** The application, container listener and in-container health check remain on 8080. Compose reads your choice from `.env` when starting; setup does not replace placeholders in `compose.yaml`. In `${HARBOUR_PORT:-8080}`, `8080` is only the fallback when that variable is unset or empty.

For example, with `HARBOUR_PORT=8384` and no bind override, the mapping is `0.0.0.0:8384 → container:8080`. A short Compose mapping `${HARBOUR_PORT:-8080}:8080` is also sufficient: it publishes on the host's interfaces. No particular bind IP is required. The supplied long form keeps an optional IPv4 bind restriction available. The Compose file names these separately as `published` (your host port) and `target` (container port). The start script prints Docker's actual mapping and verifies it matches the configured port. An existing container needs `up` to recreate it after port changes; `docker compose restart` does not change its mapping.

| How NPM connects | Forward hostname / IP | Forward port | Harbour bind address |
| --- | --- | --- | --- |
| Shared Docker network | `harbour` | `8080` | Loopback is fine; host port is unused by NPM |
| Through the Harbour host's LAN IP | Harbour host's actual LAN IP | Your selected port, e.g. `8384` | `0.0.0.0` (default), or that specific LAN IP |
| Proxy directly on the host, outside Docker | `127.0.0.1` | Your selected port, e.g. `8384` | `127.0.0.1` |

Use **HTTP** for NPM's upstream scheme in each case; HTTPS terminates at NPM. A loopback-only published port cannot be reached through the host's LAN IP. A separate proxy container's `127.0.0.1` points to itself. A 502 means the proxy could not complete its upstream request; do not infer the reason just from the template's fallback port.

On the Harbour Docker host, this displays only the effective published endpoint, without dumping `.env` secrets:

```sh
docker compose port harbour 8080
docker compose ps
```

For an NPM proxy that connects through the host's LAN IP, keep your existing `.env` and set `HARBOUR_PORT=8384` plus `HARBOUR_BIND_ADDRESS=0.0.0.0` (or remove that optional line). An existing `HARBOUR_BIND_ADDRESS=127.0.0.1` from the older wizard must be changed or removed. Run `python3 scripts/start.py` to recreate the container, then set NPM's upstream to **http / the Harbour host's actual LAN IP / 8384**. Do not enter `0.0.0.0` as NPM's upstream; it is only a listening address. Keep the existing `HARBOUR_SECRET`, `FIRST_RUN`, project name and data volume. Do not rerun first-admin setup to change a port. From the proxy's network, check `http://<the-Harbour-host-LAN-IP>:8384/api/health`; a healthy backend returns `{"status":"ok"}`. If NPM still reports 502, inspect its upstream/error log and confirm host firewall reachability and the HTTP scheme.

See [Docker's distinction between host and container ports](https://docs.docker.com/compose/how-tos/networking/#default-network-and-service-discovery).

**Security & sign-ins** shows the effective origin, trusted proxies, cookie mode, session settings, authentication events and active bans. Defaults: idle 30 minutes, absolute 12 hours, IP and User-Agent binding enabled, 15-minute ban after five failed password/2FA attempts within 15 minutes. Background/dashboard polling does not extend idle sessions; real UI activity sends a separate heartbeat. Browser upgrades or network changes may require a fresh sign-in.

**My account → Set up 2FA** provides a locally generated QR code/manual key and eight one-use recovery codes. Password and second factor are required to disable 2FA, replace recovery codes or change a protected password. Used TOTP time steps cannot be replayed. Password/2FA changes rotate the session and revoke other sessions. Keep host clocks synchronized. Authentication logs record attempted username, effective IP, UTC timestamp and outcome, retain 90 days, and never record passwords or codes. Log retention is separate from resource retention.

For a lockout, use trusted console access. Back up the data first. These commands run inside the existing production container; they do not create users:

```sh
docker compose exec harbour python -m harbour.recovery --unban 192.0.2.50
docker compose exec harbour python -m harbour.recovery --user admin --disable-2fa
docker compose exec harbour python -m harbour.recovery --user admin --reset-password
```

Password recovery prompts privately. Credential recovery revokes the account's sessions and logs a console recovery event. Upgrading an older database preserves users, keys, servers and thresholds, but invalidates old unbound sessions once. Back up before upgrading; old application versions should not reopen a migrated database.

References: [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), [cookie theft mitigation](https://cheatsheetseries.owasp.org/cheatsheets/Cookie_Theft_Mitigation_Cheat_Sheet.html), [multifactor authentication](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html), [Nginx trusted real-IP configuration](https://nginx.org/en/docs/http/ngx_http_realip_module.html).

## SSH key choices

These are three supported contemporary key families, not a universal security ranking. **Ed25519 is the default recommendation** for this application. Bit lengths across algorithms are not directly comparable.

| Algorithm | Normal | High | Xhigh | Excessive |
| --- | --- | --- | --- | --- |
| Ed25519 | 256 (fixed) | — | — | — |
| ECDSA | P-256 | P-384 | P-521 | — |
| RSA | 3072 | 4096 | 6144 | 8192 |

Unsupported tiers are unavailable in the UI/API. Larger RSA keys cost more CPU and generation time; 8192 is deliberately labelled Excessive, not recommended as a default. RSA keys use SHA-2 signatures; the old SHA-1 `ssh-rsa` signature algorithm is disabled even though the public-key format begins with `ssh-rsa`. Hardware-backed keys requiring a touch are unsuitable for this unattended polling flow. See [OpenSSH key generation](https://man.openbsd.org/ssh-keygen).

## Monitoring, temperatures and retention

SQLite in WAL mode is a practical lightweight choice for a single Harbour instance. Checks run on the backend whether any UI is open or any user is signed in. The scheduler checks for due hosts every five seconds, with four workers and one in-flight operation per host. Queuing, slow checks and long Docker operations can delay samples; this is not hard real-time monitoring. Pausing a host stops scheduled checks but preserves history and permits manual refresh.

**Latency is an authenticated SSH command round-trip**, measured after connection setup with a tiny `true` command. It includes SSH/channel and host scheduling overhead; it is not ICMP ping. A successful connection reports UP; connection refusal/timeout reports DOWN; authentication/fingerprint errors report CHECK FAILED rather than asserting the host is off. Old readings are labelled stale, not silently presented as a current UP result. A host may be UP while its Docker/metrics collection fails.

OS and kernel come from the Linux host, uptime from `/proc/uptime`, and timezone from its zoneinfo configuration (with a timedatectl fallback). Readable kernel hwmon/thermal sensors supply Celsius readings without requiring `lm-sensors` or root. The CPU package card, sidebar chip and history use an identified package sensor: Intel Package/Physical id, AMD Tdie, PECI Die, or x86_pkg_temp. Core/CCD readings, AMD Tctl and unrelated board/NVMe sensors are never substituted. If multiple packages are present, the first package (Package 0 first on Intel) is shown with its source label; expand to see each package and the other sensors. The selector is independent of temperature. No identified package sensor is **Unavailable**, never zero. A package reading is not a time average and can still change quickly. VPSs/VMs commonly hide sensors. The default 80°C warning is an adjustable operational threshold, **not a manufacturer's universal thermal limit**; each CPU package and other device sensor at or above the global/per-server threshold warns. Individual CPU core, CCD and control readings remain visible as separate readings without triggering CPU warnings. Other-device warnings retain their own labels and do not tint the CPU package value. Sensors exposed by drivers can include board, CPU, GPU and NVMe temperatures; devices without a readable kernel interface are not queried separately. See [Linux hwmon sensor interfaces](https://www.kernel.org/doc/html/latest/hwmon/sysfs-interface.html), [Intel coretemp](https://www.kernel.org/doc/html/latest/hwmon/coretemp.html), and [AMD k10temp](https://www.kernel.org/doc/html/latest/hwmon/k10temp.html).

Recommended defaults, all configurable under **Global settings → Configure monitoring**:

| Age | Stored resolution |
| --- | --- |
| First 7 days | 1 minute |
| Days 8–14 | 5 minutes |
| Days 15–28 | 15 minutes |
| After 28 days | 1 hour |
| Total retention | 90 days |

Use **60-second polling** for typical hosts; try **300 seconds and initial 5-minute storage** for a small VPS or large fleet. Ninety days covers recent incidents and month-to-month growth without keeping fine detail indefinitely. Choose 180–365 days when seasonal capacity trends matter; the maximum is 730 days. Image registry checks remain a separate, much slower schedule (six hours by default).

Each bucket preserves sample counts, weighted averages, peaks, per-filesystem used/free GB and minimum free space, temperatures and SSH latency. Failed checks preserve missing resource values rather than zeros. Consolidation/deletion runs on startup and hourly in bounded batches; a large existing backlog may take several sweeps. Boundary buckets may span the exact retention cutoff. SQLite reuses freed pages; its file does not automatically shrink. The settings panel reports database allocation and reusable space. For reclaiming disk space, stop the app, back up, then vacuum the database using a SQLite maintenance tool.

Older resolutions must be multiples of earlier ones (e.g. 10 → 30 → 60, not 10 → 15). Changing to finer settings cannot recreate lost detail. Card history defaults to six hours. The full overlay supports resource tabs, a complete scrollable table, side-by-side charts, and combined selected resources with separate percentage/°C axes. Display resolution can aggregate more coarsely but never invent finer data; a maximum of 2,000 display buckets bounds browser work. Times use the browser timezone, shown beside the controls; the server timezone remains visible in the overview. Disk history shows the busiest filesystem per bucket, CPU temperature the identified package sensor. Historical graphs read that sensor’s own stored averages/peaks; old aggregate maxima are never relabelled as package values. Missing identifiable package history stays blank. Underlying per-disk and per-sensor aggregates remain available through the authenticated history API.

## Local demo and development

Requires Python 3.11+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/demo.py
```

Open <http://127.0.0.1:8097> and choose **Explore the demo**. Its generated secret and sample database are under `.demo-data/`, separate from production. The demo bypass sign-in endpoint exists only with `HARBOUR_DEMO=true`, which the supplied production Compose file never sets. Do not run demo mode against a production data volume.

The frontend uses local HTML, CSS, JavaScript and system fonts; it has no external font/CDN requirements or frontend build step. The backend uses FastAPI, SQLite, Paramiko and cryptography.

```sh
.venv/bin/python -m pytest -q
node --check harbour/static/app.js
node --check harbour/static/security.js
node --check harbour/static/monitoring.js
docker build -t harbour:local .
.venv/bin/python scripts/smoke_docker.py
.venv/bin/python scripts/smoke_startup.py
```

The Docker smoke check creates and removes only its own randomly named container and volume, uses temporary generated credentials, verifies production sign-in, encrypted key generation, 2FA enrollment and one-use recovery, audit logs, monitoring settings, user permissions, persistent restart, and non-root execution with a read-only filesystem. No real remote servers are contacted by the automated test suite.

The startup smoke check uses a separate temporary Compose project to verify hashed first-admin seeding, verified `.env` cleanup, removal of the hash from the running container, sign-in after restart, and the optional NPM network configuration.

## Updating

Back up the data volume and `.env` before an upgrade, then:

```sh
git pull --ff-only
python3 scripts/start.py
```

This builds the checked-out source and uses the existing volume and application secret. Startup migrations preserve accounts and servers. Do not regenerate `.env`, rotate `HARBOUR_SECRET`, change the project name or run multiple instances against one database during an update. The current application version is defined in `harbour/__init__.py` and shown in the sidebar.

## Backups and limitations

Stop Harbour before copying its data volume so the SQLite database and WAL are consistent. Back up the volume **and `.env`/`HARBOUR_SECRET` separately and securely**. Losing the secret makes stored SSH keys unreadable; rotating it requires deliberate key migration. Backup copies grant the same level of access as the dashboard.

This version includes local scrypt accounts, optional TOTP 2FA, HttpOnly/SameSite=Strict cookies, session rotation, CSRF/origin checks, rate limiting, pinned SSH verification and encrypted secrets. It has no SSO, per-host user grants, outbound notification delivery, automatic upgrades or rollback. IP/User-Agent binding adds detection signals; it cannot prevent reuse from a matching IP and spoofed browser identity. Keep it on a trusted network and validate against a noncritical host before using it for production changes.

Local browser/API/Docker verification is not a claim of deployment validation on your servers. The real-host onboarding and registry paths need testing against your chosen Linux host, Compose files and image registries.
