# OpenWRT inspection and local network recording

Choose **OpenWRT** when adding the router. Use its LAN IP and an existing SSH
credential. Python is not required on the router. Harbour records the model,
board identifier, architecture, OpenWrt version/revision and kernel in its details.
Capabilities and interface names are discovered from the actual device. The
MT7621/mt76 profile covers that chipset family; it does not imply that
all optional counters exist or that the physical device has been validated.

## Read-only boundary

The adapter reads `ubus`, `/proc`, `/sys`-backed tools, selected UCI values, `iw`,
`tc` and network logs. It sends bounded ICMP probes. There are no router installs,
files, configuration writes, restarts, upgrades, wireless scans or packet-capture
mode changes. Docker operations, host collector installation and SSH key
installation are rejected for OpenWRT entries. Use credentials already authorised
on the router. Automatic incident detection records observations only.

The existing host recorder/login collector is not used on OpenWrt. Common CPU,
memory, load and boot history continue to use Harbour's existing resource views;
network history has separate storage to retain individual subsecond samples.

## Deploy on the site being investigated

The standard Compose file now includes a separate **network-recorder** service.
It shares the durable data volume and encryption secret with Harbour but has no
published port. It runs as UID/GID 10001 with all capabilities dropped; datagram
ICMP is allowed only for that group inside its container network namespace.
The router's configuration and the host's global sysctls are not changed.

Deploy Harbour on the on-site server using the usual `python3 scripts/start.py`.
The new service is built and started with the web service. No targets are probed
by the diagnostics service until recording is enabled for an OpenWRT entry.

Open **Network → Recording settings**:

1. Enable recording and choose 1 second for a baseline or 250/500 ms for an
   investigation. Up to four unicast IPv4/IPv6 literals are accepted. DNS is not
   involved. The router's LAN IP is also probed if its SSH address is an IP literal.
2. Select the WAN device for router-originated probes. The default follows the
   router's routing policy; it does not guarantee a direct WAN path. Recorder
   probes follow the server/container routing table. Check both paths before
   interpreting them as WAN evidence, particularly with policy routing/VPNs.
3. Optionally select a calling device by its current Wi-Fi MAC address. This
   filters charts; the raw evidence includes every client the driver reports.
4. Mark degradation and recovery during a call. Export an incident for analysis.

The recorder continues with the browser closed, during remote-access/WAN loss,
and through a restart of the web container alone. Restarting the recorder,
recreating the whole stack, or losing server power creates a measurement gap.
SQLite receives approximately one-second batches; a process/power failure can
lose the uncommitted batch. Storage errors retain a bounded in-memory queue and
report discarded readings. No missing interval is reconstructed.

The recorder can also run as `python3 -m harbour.network_recorder` on the same
Linux host, with the same `HARBOUR_DATA` and `HARBOUR_SECRET` as the web service.
Only one recorder may own a data directory. A native service requires a suitable
datagram ping-group permission supplied by the administrator. Compose supplies
that permission inside its isolated namespace. Unsupported/denied ICMP is a
collection error, not fabricated packet loss. Linux is the supported deployment
target for this diagnostics recorder; macOS host monitoring remains unchanged.

## Measurement interpretation

- Recorder probes are concurrent, use monotonic RTT timing and keep individual
  sequence numbers. Their reply deadline is 2 seconds. Late replies are retained
  separately from deadline misses; duplicates/foreign tokens are ignored.
- Router probes use a one-second reply wait and approximately one-second
  cadence per target over reused SSH connections. A remote-command error is a
  gap, not a timeout. Router telemetry, logs and fast recorder probes have
  independent workers, so a slow router command cannot block packet sampling.
- Router telemetry is read approximately every second. CPU/softirq, interface
  traffic/errors and client retry/failure deltas are boot-scoped. New baselines,
  reconnects and counter resets produce unavailable rates. Actual sample
  duration is preserved. Sampling overhead is part of the router workload.
- Main charts share recorder time. Router log lines retain their source timestamp
  but are indexed by receipt time. SSH buffering can delay telemetry/log delivery;
  its duration is recorded and is not displayed as an ICMP RTT.
- ICMP RTT variation is not RTP/FaceTime jitter. These probes do not follow every
  media path or measure the other site's Wi-Fi. A timeout can reflect filtering
  or ICMP rate limiting. Client TX retries are from the router's perspective;
  they do not expose all retries made by the phone.
- Queue drops may be normal active queue management. Hardware offload can bypass
  software queue accounting. Missing driver statistics stay unavailable.
- There are no speed tests, bulk transfers, DNS tests or automatic repair actions.

## Retention and incidents

Raw readings retain up to **6 hours, 200,000 rows or 64 MiB of JSON payload per
router**, whichever fills first. Trimming happens approximately once per minute.
SQLite pages/indexes/WAL add overhead, and allocated disk space can remain after
pruning. The UI shows a two-minute window; raw data retains its original precision.

A marker captures the **2 minutes before and 5 minutes after** its timestamp when
those samples exist. After five minutes the recorder archives the window before
pruning raw history. Exports made earlier contain the available samples. Automatic
markers are limited to one per five minutes and indicate probe degradation, not
a diagnosed root cause. The archive retains up to **20 incidents / 32 MiB** of
JSON per router. Large windows can be truncated and are labelled accordingly.
Each sample has a recorder run identity; exported run metadata records the
settings that actually applied, even across setting changes or restarts.

Pausing server monitoring or disabling recording stops the diagnostics workers.
Removing/changing a server invalidates in-flight writes. Removing the entry
deletes its diagnostics history and never changes the router. Ordinary Harbour
viewer accounts may inspect/export; only administrators configure recording or
add markers. Logs are filtered to network/kernel events and obvious credential
lines are omitted; incident exports can still contain local IPs, client MACs and
hostnames, so inspect them before sharing publicly.

## Validation boundary

Synthetic tests use an invented device identity and example firmware build. They
cover missing tools,
counter resets, short loss/delay, late/duplicate replies, IPv4/IPv6, cancellation,
configuration changes, persistence, permissions and browser flows. Loopback
tests validate unprivileged ICMP in an isolated Linux container. Actual router
driver output, SSH permissions and sampling overhead still require on-site
deployment validation; no live router has been accessed or altered by this work.
