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
Each monitored router has independent ping and router-collection processes.
Separate writer and archive processes handle SQLite, incident serialization,
compression and retention. A bounded RAM buffer in the supervisor keeps encoded
readings until the writer acknowledges their committed sequence. A writer restart
retries unacknowledged batches without duplicating readings or incident markers,
even if their raw rows have already been archived and pruned.

**Memory buffer** defaults to **128 MiB per router**, adjustable from 8 to 4096 MiB
in Recording settings without restarting probes. This bounds buffered payload,
not total process memory: objects, interpreter overhead and small bounded transfer
queues are additional. Capacity is allocated as readings arrive, not reserved in
advance. Normal writes happen about once a second. A larger buffer increases
outage tolerance, not normal RTT accuracy or the normal unsaved window.

If storage stalls, probing continues until the buffer is full, after which new
readings are discarded and counted. Lowering a limit retains queued readings and
rejects arrivals until space is available. The UI shows buffer usage, oldest
unsaved age, writer delays and losses. A private Unix socket in the shared data
volume supplies live status independently of database writes. Historical buffer
health is included in incident exports as `recorder_health` rows.

Normal shutdown stops collection and allows up to 30 seconds to drain buffered
readings. Compose grants 45 seconds for shutdown. A forced stop, supervisor crash
or power failure loses unsaved RAM contents; normally about a second, potentially
more during storage trouble. Shutdown reports remaining uncommitted readings if
storage cannot recover in time. No missing interval is reconstructed.

The recorder can also run as `python3 -m harbour.network_recorder` on the same
Linux host, with the same `HARBOUR_DATA` and `HARBOUR_SECRET` as the web service.
Only one recorder may own a data directory. A native service requires a suitable
datagram ping-group permission supplied by the administrator. Compose supplies
that permission inside its isolated namespace. Unsupported/denied ICMP is a
collection error, not fabricated packet loss. Linux is the supported deployment
target for this diagnostics recorder; macOS host monitoring remains unchanged.

## Measurement interpretation

- Recorder probes retain individual sequence numbers and a two-second deadline.
  On supported Linux kernels, software receive timestamps distinguish packet
  arrival from the time the recorder reads the socket. Scheduling uses a
  monotonic clock; clock-offset changes invalidate mixed-clock RTT calculations.
  The send timestamp is still application-side, so send-call duration is recorded
  and slow sends invalidate latency. These are not hardware wire timestamps.
- Exports preserve `timing_quality`, `observed_rtt_ms`, `received_at`,
  `reader_delay_ms`, `send_lateness_ms` and `send_duration_ms`. Kernel receive
  timestamps remove delayed-reader inflation. If unavailable, healthy
  application timing is labelled `userspace`. Delayed fallback measurements or
  clock changes are `uncertain`: retained as observations, excluded from RTT
  charts and automatic latency alerts. Unanswered probes across an observation
  gap are `unobserved`, separately counted from confirmed deadline misses.
- Reads are drained before deadlines are evaluated. A reply timestamped before
  its deadline is not a timeout just because it was read late. Genuine late
  arrivals retain their timeout and late reply. Duplicates/foreign tokens are
  ignored. Missed send slots are skipped rather than replayed in catch-up bursts.
  Process isolation cannot prevent whole-host CPU starvation, suspension, kernel
  receive delays, or buffer overflow; these remain observable limitations.
- Router probes use a one-second reply wait and approximately one-second
  cadence per target over reused SSH connections. A remote-command error is a
  gap, not a timeout. Router telemetry, logs and fast recorder probes have
  separate processes, so a slow router command does not share the ping interpreter.
- Router telemetry is read approximately every second. CPU/softirq, interface
  traffic/errors and client retry/failure deltas are boot-scoped. New baselines,
  reconnects and counter resets produce unavailable rates. Actual sample
  duration is preserved. Sampling overhead is part of the router workload.
- Main charts share recorder time. Router log lines retain their source timestamp
  but are indexed by receipt time. SSH buffering can delay telemetry/log delivery;
  its duration is recorded and is not displayed as an ICMP RTT.
- Charts keep readable axes on desktop and mobile. Isolated readings appear as
  dots; lines break across missing samples. Missing CPU/interface counter
  baselines are not zero. The queue chart omits `noqueue`, `ingress` and `clsact`
  entries; the full queue readout remains available below the charts.
- ICMP RTT variation is not RTP/FaceTime jitter. These probes do not follow every
  media path or measure the other site's Wi-Fi. A timeout can reflect filtering
  or ICMP rate limiting. Client TX retries are from the router's perspective;
  they do not expose all retries made by the phone.
- Queue drops may be normal active queue management. Hardware offload can bypass
  software queue accounting. Missing driver statistics stay unavailable.
- There are no speed tests, bulk transfers, DNS tests or automatic repair actions.

## Retention and incidents

**Network → Recording settings** controls retention separately for each router.
The defaults, including after upgrading an existing entry, are:

| Storage | Default limits |
| --- | --- |
| Unsaved readings in RAM | 128 MiB encoded payload per router |
| Raw readings in the database | 7 days / 1 GiB of JSON payload; no row cap |
| Compressed daily files | 90 days / 4 GiB of finished files |
| Incident exports in the database | 200 incidents / 128 MiB of JSON payload |

The first enabled limit reached applies. Raw history can be set from 1 hour to
365 days; daily files from 1 to 3,650 days. Set a **row, count or size limit to 0**
to remove that cap. Trimming happens approximately once per minute.
SQLite pages/indexes/WAL add overhead, and allocated disk space can remain after
pruning. The UI shows a two-minute window; raw data retains its original precision.
Increasing retention cannot restore readings already deleted by an older version.

### Automatic daily files

Daily archiving is enabled by default. It runs in the independent recorder, with
the browser closed. Files live in **`HARBOUR_DATA/network-archives/`**: with the
standard Compose deployment this is `/data/network-archives/` inside the shared,
persistent `harbour-data` volume. Native deployments use the same subdirectory of
their configured data directory. No files or configuration are written to the router.

The recorder appends compressed batches throughout the day **before pruning raw
readings**, so even a one-hour raw-history setting can preserve a whole day's
evidence. After midnight **UTC** it seals one `.jsonl.gz` file per router with data;
the first day can be partial. This happens on the next maintenance sweep, or on
restart if the recorder was stopped. Pausing recording does not prevent sealing.
The current `.part` file is not offered as a finished download. Finished files
appear under **Network → Daily compressed archives** with download links, sizes
and sample counts. Retention applies to finished files; the growing current file
is additional to the archive size cap.

Archives contain newline-delimited JSON: a `header`, recording `context` records
(settings, device and run metadata), individual `sample` records and `incident`
markers, including completion updates. Each line is a complete JSON object.
Standard gzip tools/Python's `gzip` reader decompress the full stream. Compression
is lossless; its ratio depends on the observations and does not downsample them.
The archive date is the UTC day readings were copied into that file. Delayed
readings and upgrade backfills can belong to earlier measurement dates; every
sample retains its original `at` timestamp and recorder run identity. An updated
incident is identified by the same incident ID. `archive_sequence` numbers each
sample uniquely within a file; raw database IDs may be reused after pruning.
Files are local and not encrypted;
they have owner-only permissions under Harbour's private data directory.

Each append is flushed to disk before its database checkpoint is committed.
Retries discard any uncommitted tail, preventing duplicate readings after an
interrupted write. Compression and cleanup use a separate thread from recording.
On an archive failure the UI shows an error and **unarchived samples are protected
from trimming**, so database limits can be exceeded until archiving recovers.
Disabling daily archiving explicitly removes that protection; existing finished
files still follow their configured retention. Changing only retention settings
does not restart the probe workers.

A marker captures the **2 minutes before and 5 minutes after** its timestamp when
those samples exist. After five minutes the recorder archives the window before
pruning raw history. Exports made earlier contain the available samples. Automatic
markers are limited to one per five minutes and indicate probe degradation, not
a diagnosed root cause. Incident count and payload limits are configurable
separately from daily files. Large incident windows can be truncated and are
labelled accordingly; daily files archive all persisted individual samples.
Each sample has a recorder run identity; exported run metadata records the
settings that actually applied, even across setting changes or restarts.

Pausing server monitoring or disabling recording stops the diagnostics workers.
Removing/changing a server invalidates in-flight writes. Removing the entry
deletes its database diagnostics history; the recorder removes its managed local
archive files on the next maintenance sweep. It never changes the router. Ordinary Harbour
viewer accounts may inspect/export; only administrators configure recording or
add markers. Logs are filtered to network/kernel events and obvious credential
lines are omitted; incident exports can still contain local IPs, client MACs and
hostnames, so inspect them before sharing publicly. Daily files contain the same
local evidence and are excluded from Git by the repository's ignore rules.

## Validation boundary

Synthetic tests use an invented device identity and example firmware build. They
cover missing tools,
counter resets, short loss/delay, late/duplicate replies, IPv4/IPv6, cancellation,
configuration changes, persistence, permissions and browser flows. Loopback
tests validate unprivileged ICMP in an isolated Linux container. Actual router
driver output, SSH permissions and sampling overhead still require on-site
deployment validation; no live router has been accessed or altered by this work.

## Recorder validation

`tests/test_network_pipeline.py` exercises timestamp quality, clock changes,
full buffers, nonblocking IPC, commit acknowledgement retries, and stale settings.
`tests/network_process_check.py` additionally exercises actual Linux datagram
ICMP timestamps, stopped and killed child processes, live buffer status, settings
changes and shutdown. Run that integration check in a disposable container with
`--network none`; it uses loopback and documentation addresses, never a site.
