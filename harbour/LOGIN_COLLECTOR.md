# Harbour login collector

The host runs a permanent, event-driven collector. Harbour fetches a bounded batch
over its normal SSH resource probe and stores the events in **Server → Logins**.
No extra TCP port, remote package dependency, or new SSH connection is needed.
The Python collector uses only the standard library and Python 3.9 or newer.

## Installation on each monitored host

In **Logins → Collector setup**, use **Push to host** to upload the bundle into
the saved SSH user's home directory, **Push & extract** to also unpack it into a
new folder there, or **Push & install** to run the installer with sudo. Each push
uses unique archive/folder names. Upload and extraction do not require sudo;
installation registers a root service and does require administrator access.

The dialog streams paths, progress, installer output, and the result over a
separate pinned SSH connection while normal polling continues. Enter a sudo
password only when the remote script requests one; it is used for that prompt
and never saved or logged. Root and passwordless sudo need no prompt. Closing
the dialog/stopping setup closes its connection; completed steps and uploaded
files remain. Check the host after an interrupted installation. Prompts time out
after two minutes and setup after fifteen minutes. Hosts whose sudo policy
requires a TTY or extra authentication should use manual installation.

Alternatively, download **Download collector bundle**, copy it to the host, and
extract it, or use the files in this repository's `harbour/` directory. Replace
`harbour` below with the SSH account configured in Harbour:

```sh
python3 collector_install.py --reader harbour --dry-run
sudo python3 collector_install.py --reader harbour
```

Run this on the actual Linux/macOS host, not inside Harbour's container or the
Docker Desktop VM. The installer registers a root systemd service on Linux or a
root LaunchDaemon on macOS. It copies the script to
`/usr/local/libexec/harbour-logins/`. Use a trusted, permanently installed Python;
Linux's service cannot access an interpreter under `/home` or `/root`.
`--python /usr/bin/python3` selects another interpreter.

The collector's root-owned database is `/var/lib/harbour-logins/events.db` on
Linux and `/Library/Application Support/HarbourLogins/events.db` on macOS.
The socket `/var/run/harbour-logins/collector.sock` is mode 0600, owned by the
designated reader. That account can read and acknowledge the feed, but cannot
write event records or execute commands through the socket. Other root processes
can access or alter the collector, as with ordinary system logs.

For Linux without journald, choose an existing authentication log:

```sh
sudo python3 collector_install.py --reader harbour --auth-file /var/log/auth.log
```

The first start begins collecting new records. Journal cursors/file offsets are
persisted with events; log rotation and expired cursors are handled and reported.
No arbitrary historical logs are imported during installation.

## SSH configuration and validation

Modern OpenSSH normally records successful public-key fingerprints at INFO.
Set `LogLevel VERBOSE` in the host's effective SSH server configuration to improve
coverage of rejected public keys and early authentication failures. The installer
does not modify SSH configuration, PAM, authentication methods, or log privacy.

Validate with `sudo /usr/sbin/sshd -t`; inspect the effective configuration using
`sudo /usr/sbin/sshd -T` (and `-C` when Match blocks apply), then apply it using the
host's SSH service manager. Keep an existing administrative session open when
changing SSH configuration. DEBUG logging is unnecessary for this collector.

Verify controlled successful and failed logins from a second connection. In
Harbour, check the method, username, source and key against that connection.
Confirm pending events drain after two resource probes. No password/private-key
material is read; evidence is the bounded authentication log message itself.

## Coverage

| Source | Supported records | Limits |
|---|---|---|
| Linux journal | OpenSSH accepted/failed/partial authentication, invalid users, rejection/disconnect; supported PAM authentication failures and session open/close | The application must log the event. Root-emitted journal records from the selected services are accepted. Nonstandard SSH/PAM services need parser/source additions. |
| Linux authentication file | Same parser over syslog records, with rotation handling | File must receive authentication logs. ISO timestamps and local syslog timestamps are supported; absent timestamps remain unknown. |
| macOS unified log | OpenSSH records matching the same parser; recognizable authentication/session records when emitted | Partial coverage. Privacy redaction and OS retention can remove fields/events; GUI login and all failed attempts are not guaranteed. No logging profile is silently installed. |
| Optional native macOS helper | OpenSSH connection login/logout, `/usr/bin/login`, LoginWindow sessions/lock/unlock, Open Directory/Touch ID/token/Auto Unlock, Screen Sharing | macOS 13+ and Endpoint Security permissions required. Native SSH events have no key fingerprint; separate SSH log records provide it. |

The table displays **source events**, not a count of unique humans or connections.
One login can generate authentication, PAM session and native event records.
These remain separate, labelled evidence: we do not guess that similar timestamps
belong to one session. SSH multiplexing can create several channels behind one
authenticated connection. Source PID and graphical session IDs are retained.
Duration is shown only for an unambiguous start/end pair with matching source,
boot, service, user and graphical-session ID (or PAM process ID). Missing,
overlapping or clock-reversed pairs remain unknown.

An attempted username and an offered/rejected key are unverified claims.
A successful key fingerprint identifies a credential, not a physical device.
Successful verified rows matching Harbour's configured SSH username and public key are labelled
**Harbour key** and can be filtered; another client using the same credential
receives the same label. Rejected offers of that key remain visible when the
filter is enabled. Password-authenticated monitoring cannot be identified
reliably and is not hidden. Unknown fields stay unknown.

Application-specific web/database logins, preboot/FileVault authentication,
process command auditing, network packets and biometric data are outside scope.

## Optional native macOS helper

This is a small, notify-only Endpoint Security client, not an `eslogger` wrapper.
It subscribes only to authentication/session notifications. It does not subscribe
to file/process execution events or block authentication.

Building needs Apple's command-line development tools and a macOS 13+ SDK:

```sh
xcrun clang -fobjc-arc -fblocks -mmacosx-version-min=13.0 \
  -framework Foundation -lEndpointSecurity -lbsm login_events.m -o login-events
```

Normal deployment also needs **Apple approval** for
`com.apple.developer.endpoint-security.client`, appropriate Developer ID signing
and provisioning, and **Full Disk Access** granted to the installed helper.
The included entitlements plist declares the permission; it does not grant it.
Follow Apple's signing/provisioning and distribution requirements for your
approved developer account. Do not disable SIP to bypass these requirements.

After signing the built helper using your approved identity/profile:

```sh
sudo python3 collector_install.py --reader harbour --endpoint-helper ./login-events
```

Grant Full Disk Access to
`/usr/local/libexec/harbour-logins/login-events`, using System Settings or your MDM
policy. Confirm **endpoint-security · listening** in Harbour before relying on
native coverage. A compiled, unsigned binary cannot establish this subscription.
Missing permissions, helper exits and native sequence-number gaps are visible.
Native events during downtime cannot be replayed. Unified-log backfill recovers
only retained records; it cannot prove that every historical event was retained.
Each collector/helper restart is therefore a visible potential coverage gap.

Optional token events include public-key hash/token identifier and sometimes a
Kerberos principal; Screen Sharing can expose a viewer Apple ID. None is a
universal remote-device fingerprint. Apple documents cases where Screen Sharing
events/source addresses are unavailable.

## Delivery, retention and resource bounds

- The host queue uses a persistent collector identity and monotonically increasing
  sequence numbers. It survives restarts and upgrades.
- Each probe fetches up to 200 pending records. Harbour commits the entire batch
  and its acknowledgement token in one transaction. A retry inserts no duplicate
  events. A failed/invalid batch does not advance the acknowledgement.
- The **next normal probe** delivers the saved acknowledgement and retrieves the
  next batch. The host stamps the acknowledgement only then. No event is marked
  acknowledged merely because it was read. Harbour's collected/seen timestamp is
  independent of the event, capture and host-acknowledgement times.
- A probe cancelled by pause/removal/connection change cannot commit a stale
  batch. Pausing Harbour does not stop the host collector.
- Acknowledged local records expire after 24 hours. The default queue cap is
  20,000 events (`--max-events` accepts 1,000–1,000,000). Old acknowledged records
  are removed first; losing unacknowledged records increments a persistent loss
  counter displayed prominently in Harbour. This is a bounded queue, not an
  unlimited offline archive. At the default 60-second poll, the maximum drain
  rate is approximately 200 events/minute per host.
- Harbour retains received events for 90 days, measured from receipt so delayed
  batches are not immediately expired. Server deletion removes its stored events.
- No hardware-independent RAM/CPU guarantee is claimed. Python, SQLite, the OS
  reader and optional native helper contribute to memory. Measure idle and burst
  load on your actual smallest host before setting a fleet resource budget.

The collector reads events as they arrive. It does not repeatedly scan all logs.
Log rotation, restart, transport interruption, clock changes, queue overflow and
missing native permissions must be considered when interpreting an empty view.

## Operations and removal

Linux status: `sudo systemctl status harbour-logins.service`.
macOS status: `sudo launchctl print system/one.harbour.logins`.
Re-run the installer to upgrade or change the reader account; the queue remains.
The previous script and service definition are retained as `.previous` files.

To stop collection without deleting evidence:

```sh
# Linux
sudo systemctl disable --now harbour-logins.service
# macOS
sudo launchctl bootout system/one.harbour.logins
```

Remove the service definition and installed script directory only after stopping
the service. Remove the database separately only when its retained evidence is
no longer needed. Remote installation is available through Collector setup;
removal remains a manual host-side operation.

## Validation scope

Automated tests exercise parsing, durable delivery/replay, bounded queues, source
failures, permissions, API isolation, migrations and the browser view. Native
helper compilation is checked on macOS. End-to-end deployment on a signed,
Full-Disk-Access-approved Mac and real Linux authentication sources requires a
controlled host test; synthetic fixtures are not claimed as live coverage.
