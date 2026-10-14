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
   investigation. Up to 12 unicast IPv4/IPv6 literals are accepted. DNS is not
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
- Optional bounded UDP call-load tests run from the Harbour host to your own endpoint.
  See [profiles, research and endpoint setup](CALL_TESTS.md). No DNS tests or
  automatic repair actions run. Continuous probes remain independent.

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

### Scheduled archive files

Daily archiving is enabled by default. It runs in the independent recorder, with
the browser closed. Files live in **`HARBOUR_DATA/network-archives/`**: with the
standard Compose deployment this is `/data/network-archives/` inside the shared,
persistent `harbour-data` volume. Native deployments use the same subdirectory of
their configured data directory. No files or configuration are written to the router.

The recorder appends compressed batches throughout the day **before pruning raw
readings**, so even a one-hour raw-history setting can preserve a whole day's
evidence. At the configurable processing time (**03:00 Europe/London** by default)
it seals one `.jsonl.gz` file per router with data; the first cycle can be partial.
This happens on the next maintenance sweep, or on
restart if the recorder was stopped. Pausing recording does not prevent sealing.
The current `.part` file is not offered as a finished download. Finished files
appear under **Archives** with download links, sizes
and sample counts. Retention runs after successful report publication and applies
to finished files; the growing current file
is additional to the archive size cap.

Archives contain newline-delimited JSON: a `header`, recording `context` records
(settings, device and run metadata), individual `sample` records and `incident`
markers, including completion updates. Each line is a complete JSON object.
Standard gzip tools/Python's `gzip` reader decompress the full stream. Compression
is lossless; its ratio depends on the observations and does not downsample them.
The archive date is the UTC date the scheduled cycle started. Delayed
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

## Incident reports and pattern analysis

Open **Incidents → Analyse** for an individual incident, or **Explore
incident patterns** to compare incidents. Viewer accounts can use both views and
download the original evidence, individual analysis JSON and pattern summaries.
Analysis runs on the Harbour server when requested; it does not contact an AI
service, run extra probes, change the router or automatically repair anything.

### What triggers an automatic incident?

While recording and server monitoring are enabled, **one recorder-originated
probe** is enough when it either:

- Misses its **two-second reply deadline** (`status=timeout`), or
- Has an RTT **strictly above** the latency limit for that recording run
  (**150 ms by default**, configurable in Recording settings).

Rows with `timing_quality=uncertain` are excluded. Unobserved probes normally
have no usable RTT and do not trigger a marker. A usable late reply can also
qualify on its RTT; its original timeout and late reply remain separate evidence
for the same attempt. The trigger covers every recorder target, including the
router reference IP when configured by using a literal IP as its SSH address.
There is no sustained-duration requirement and no requirement for several targets
to fail. Router-originated probes, CPU, queues, Wi-Fi and log messages do **not**
independently trigger incidents. Manual degradation/recovery markers are always
user observations, not automatically detected transitions.

Detection happens when a batch is durably committed. It uses the earliest
qualifying sample's original timestamp and allows at most one automatic marker
per server every **300 seconds**. Continued degradation can produce another
marker after that cooldown; these are not necessarily separate outages. Sample
storage, run progress and marker creation share one transaction, so retrying a
committed batch cannot create another marker.

New markers save the exact triggering target, source, run, sequence, observation,
RTT/timeout reason and threshold. Older markers are still supported: the report
labels triggers reconstructed from evidence and shows when the original
triggering sample or run configuration is unavailable. Changing the current
threshold does not rewrite historical run thresholds.

### What is captured?

The saved window is **120 seconds before to 300 seconds after the marker**, up to
60,000 rows. It contains the samples that actually exist, not guaranteed full
coverage. Sealing a window means its saved evidence is frozen; it does not certify
that every expected reading was collected.

| Evidence | Captured information |
| --- | --- |
| Recorder ICMP | Targets, sequence, reply/timeout/late/unobserved status, RTT, send/receive timing and timing quality, at the configured 250 ms–2 s cadence |
| Router ICMP | Optional probes to configured targets, approximately every second, with a one-second reply wait and chosen interface/routing policy |
| Router telemetry | Approximately one-second CPU/core/softirq intervals, memory/load/uptime/boot identity, interface traffic/error/drop deltas, Wi-Fi clients/retries/failures/radio occupancy, software queues, conntrack count/limit, routes, policy rules and offload settings where available |
| Router logs | Filtered network/kernel/hostapd/mt76/PPP/watchdog/memory messages, indexed by recorder receipt time with router source text preserved |
| Collection health | Explicit collection gaps, buffer backlog, dropped-before-storage counters, collector availability and process restarts |
| Context | Device information and recording-run settings/observer, plus incident metadata |

No packet payloads, DNS tests, call media, application traces, ISP internal data or
remote-site Wi-Fi measurements are captured. The chosen client MAC filters Wi-Fi
analysis; raw evidence retains all reported stations. Missing tools, unavailable
counters and absent baselines remain missing evidence.

### How the analysis works

The versioned parser consumes the same schema-1 JSON bundle as the original
incident download. It sorts readings by capture time, removes replayed samples
using producer identities (with a timestamp-scoped legacy fallback), and keeps
late replies separate from new attempts. The report includes per-path counts,
usable-reply p95/peak RTT, before/after comparisons, timeline, limitations and
sample references back to the original `samples` array.

Possible contributors require telemetry within **two seconds** of a qualifying
recorder observation **in the same identified run**. Router path comparisons also
require the same destination. Telemetry collected over more than two seconds or
covering a counter interval longer than five seconds is excluded from those
nearby-contributor rules. These are explicit triage heuristics:

| Observation | Rule |
| --- | --- |
| CPU pressure | Total CPU or any core at least 90% |
| Softirq pressure | Any core at least 50% softirq |
| Memory pressure | Memory utilisation at least 90% |
| Queue backlog | At least 64 KiB in a reported software queue; excludes noqueue/ingress/clsact |
| Interface errors/drops | Positive valid interval delta, not a cumulative lifetime count |
| Wi-Fi retries/failures | Positive TX failure delta, or retries/packets at least 25% with at least 10 packets; retries are not a loss percentage |
| Busy radio | At least 85% channel busy time |
| Conntrack pressure | Count at least 90% of maximum |

Boot-identity changes and selected log events are shown as observations in the
window. Logs are not assumed to be synchronised source-time events. Negative or
missing interval deltas are not turned into failures. Quality notes flag open or
truncated windows, missing run metadata, sparse coverage, uncertain probes,
collection gaps, slow telemetry and recorder losses. Cumulative loss counters are
only differenced within a run; an already nonzero first reading is not treated as
new loss during this incident.

Every finding includes its rule, supporting sample references and a suggested
next check. The report deliberately leaves `root_cause=not_established`: coincident
CPU load, retries or queue backlog is evidence for an investigation, not a causal
proof. ICMP filtering, rate limiting, routing differences and recorder/container
conditions remain alternatives. No unaffected control windows are compared, so
frequency here cannot establish that a symptom is unique to incidents.

### Condensed evidence briefs (0.1.31)

In **Incidents → Analyse**, expand **Export condensed evidence brief**.
Choose **Markdown** for a readable conclusion/evidence table or **JSON** for
structured ingestion, and 1, 3 (default) or 6 supporting examples per conclusion.
Generate a preview, then copy or download its exact contents. Clipboard access
can be unavailable over HTTP; in that case the preview is selected for manual copy.

Each conclusion has a stable-within-brief ID, its rule, observed values, original
sample indexes, run identity, timestamps, supporting count and next check.
Correlated findings include a nearby qualifying recorder probe; reboot findings
include both boot identities. Path totals retain before/after statistics and
usable/uncertain denominators. Trigger details, device/run context, coverage gaps,
controlled traffic-test timing and analysis limitations accompany the conclusions.
The optional review request asks an LLM to challenge each conclusion, cite its
support, consider alternatives and identify the next useful measurement.

Selection takes the first qualifying observations in time order, not a random or
representative sample. Nested matching measurements are capped at 8 items with
omission counts; log excerpts retain up to 400 characters around a matching event.
The original full incident export remains the authority. `null`/missing values
stay unknown, and root cause remains `not_established`. The brief never sends
anything to an external service and adds no runtime dependencies or background
work. Hostnames and IP/MAC addresses remain where present in the selected evidence.

Brief schema 1 includes the app/analyzer version and a reproducible SHA-256 of the
original samples array. Sample references are local to that incident. When giving
several briefs to an LLM, compare server IDs, window bounds and fingerprints:
overlapping windows or copied samples are not independent recurrence. Open windows
can change between preview generations; copy/download uses the generated preview.

### Patterns and exports for further investigation

The explorer filters all/automatic/manual markers and all-retained/24-hour/7-day/
30-day periods. Each request analyses at most **200 retained incidents**, displays
20 overviews per page and offers older/newer batches if needed. Counts and JSON
exports refer to the displayed analysis batch, with its bounds shown. Pattern
filters select incidents sharing a finding or degraded probe path. Browser times
use the viewer's timezone; JSON retains epoch timestamps.

**Download patterns (.json.gz)** prepares a completed gzip file locally on the
Harbour server, then offers it for download with its compressed file size. JSON
is encoded incrementally into the compressed file on the data volume, avoiding
a second uncompressed batch in memory or Docker's small `/tmp` RAM filesystem.
The private temporary file is removed after transfer, disconnect or process exit.
The ordinary JSON API used by the explorer is unchanged.

This download contains compact incident summaries and recurrence statistics for
the selected batch, including its filters and evidence limitations. It does not
bundle the full raw sample archives, which can be tens of megabytes per incident.
Compression is lossless; decompress the `.json.gz` file to recover the JSON.
Actual size savings depend on repetition in the evidence. Full individual
incident evidence downloads remain available separately.

Pattern results count each incident ID once, not its sample count. Overlapping
seven-minute windows on the same server form a **window group**, so overlapping
markers do not masquerade as independent recurrences. Distinct groups can still
belong to a prolonged problem. Marker counts are not outage rates, and missing or
pruned incidents are not represented. Sealed analysis summaries are cached locally
with an analyzer version and evidence digest; open windows are analysed afresh.
The small derived cache is additional to incident payload limits and is deleted
with its incident. Full reports are always derived from the original window.

Authenticated API routes (under `/api/servers/{id}/network`):

- `GET /incidents?kind=all&limit=200&offset=0`: summaries and pattern groups.
  Optional `since`/`until` epoch timestamps bound the marker time; `download=true`
  returns a compressed `.json.gz` attachment. `kind` also accepts `automatic` and `manual`.
- `GET /incidents/{event_id}`: full analysis with referenced evidence.
  `download=true` returns the structured report.
- `GET /incidents/{event_id}/brief?format=markdown&examples=3&include_review=true`:
  returns preview content, filename, media type and UTF-8 byte count. `format=json`
  selects structured output; `download=true` returns the brief as an attachment.
  Like analysis, the endpoint is available to authenticated read-only users.
- `GET /export/{event_id}`: the original complete or available incident bundle.

For offline ingestion of multiple downloaded **incident JSON exports**:

```sh
python3 -m harbour.incidents incident-one.json incident-two.json > analysis.json
```

The parser uses only Python's standard library. It returns full reports plus
cross-incident patterns, deduplicates repeated incident IDs, and rejects invalid
export schemas and oversized inputs (64 MiB/file, 60,000 rows/incident). The CLI
accepts incident JSON, **not daily `.jsonl.gz` archives**. Daily archives retain
more raw evidence but must first be reconstructed into incident windows with their
original run context. This release does not automatically recover pruned incidents
from those files. New exports include a server ID for window grouping; legacy
exports fall back to server name and should only be combined when those names
identify the same physical recorder target.

## Connection conclusions and compressed handoffs

The separate **Analysis** tab combines retained ping/router evidence with
upload and download receiver intervals from saved call tests. Select a period,
then **Analyse saved evidence** to backfill retained tests and sealed incidents.
New completed call tests save their conclusions when evidence collection finishes.
Opening the tab or downloading a handoff starts no probes, remote requests or
repairs. A completed test without collected evidence remains unavailable; collect
its evidence from **Call tests** first. Nothing is sent to an external AI service.

**Review** opens a conclusion with degradation episodes, separate directional
exposure, path comparisons, possible router contributors, baseline/load/recovery,
sender delivery and clickable measured evidence. Ping can establish an episode
without UDP impairment and vice versa. Only matching endpoint pings corroborate
UDP impairment; failures to other destinations remain separate observations.
The analysis uses original run settings and excludes uncertain/unobserved probes.
Coverage is measured from probe cadence rather than raw row counts, so clustered
replies cannot fill sampling holes. Gaps and incomplete tests cannot become a
healthy combined comparison.

Rules are recorded in each report: receiver loss above zero or jitter above
30 ms flags review; baseline/load p95 comparisons need at least ten usable
attempts per phase and a rise of at least 20 ms. Recovery also checks timeout
fraction. These are Harbour investigation thresholds, not guarantees of real
application quality. Interval joining requires saved timing uncertainty no greater
than one second. Consecutive or overlapping symptoms form sampled episodes;
their spans do not establish exact continuous outage duration. Wider uncertainty
retains receiver symptoms but prevents precise ping/router attribution.

Router pressure comparisons require the same original recorder run and timely
telemetry (sample interval at most five seconds, collection delay at most two
seconds). Each contributor carries counts near degradation and outside detected
episodes, measured examples, qualifications and a next check. Comparison periods
are not independently proven healthy. Interface drops do not prove a cable fault;
selected-client retries do not prove that Harbour traversed that Wi-Fi path.

Recurring groups require matching profile, endpoint, requested rates, packet size,
phase durations, recorder configuration, observer and recorded route/offload
context. Missing observer/routing context excludes a test from these groups.
Overlapping test windows are counted once. Totals include impaired tests, complete
comparisons with neither test impaired, incomplete comparisons and observed versus
affected receiver interval seconds. They describe retained synthetic exposure,
not a whole-day outage rate. Ping incident and call-test reports may contain the
same observations and must not be added as independent incidents.

**Download this conclusion (.json.gz)** exports one reviewed report.
**Download all conclusions in period (.json.gz)** exports up to 200 saved reports
in the selected period; the **Show** filter affects the displayed list only.
Harbour prepares the compressed file on its own data storage before transfer.
It contains:

- A readable conclusion brief and guidance for independent review in a new session.
- Each report's measured observations, possible contributors, comparison counts,
  next checks, timing uncertainty and accepted exclusions.
- Selected measured excerpts addressed by each finding's evidence references,
  original run context, rule/analyzer versions, and report SHA-256 checksums.
- Comparable-test patterns and explicit selection/retention limits.

Decompress the `.json.gz` and supply the JSON to a new analysis session (or attach
the compressed file if that session supports gzip). It is self-contained for
reviewing these conclusions; full raw exports remain separate and may be needed
for a different analysis. Device names, IP addresses and selected client IDs
remain in the evidence. No SSH credentials or endpoint access tokens are included.
Captured names/log text are evidence, never instructions. Hashes establish content
consistency, not independent authenticity. Reports preserve
`root_cause: not_established`: actual media quality, DNS/signalling/relay failures,
endpoint CPU/socket drops, exact packet bursts and one-way latency are unmeasured.

Derived conclusions have separate retention: at most 200 reports or 128 MiB
compressed per router, whichever fills first, with an 8 MiB per-report ceiling.
They survive normal raw-test/incident expiry and are removed with their server.
Up to 400 episodes and 1,200 selected evidence examples are retained per report;
counts use all eligible saved observations and omitted examples are disclosed.
Original raw retention is unchanged. Older unanalysed evidence cannot be recovered
after it expires. Large archives are not automatically reconstructed into tests.

**Review** opens a bounded overlay with **Summary**, **Episodes**, **Comparisons**
and **Evidence** tabs. The underlying page keeps its position. Summary puts the
verdict and upload/download measurements first; supporting evidence and limitations
remain expandable. Evidence links open the exact measurement inside the overlay.

The episode column's **Observation meanings** info menu explains its five values:

- **Ping and call-flow impairment**: both measurements show impairment.
- **Call-flow impairment with normal endpoint ping**: receiver impairment with
  sufficient normal endpoint-ping coverage.
- **Ping impairment with normal call flow**: ping impairment with sufficient
  normal, aligned evidence in both receiver directions.
- **Call-flow impairment; ping comparison incomplete**: receiver impairment is
  measured but ping coverage/timing cannot establish the comparison.
- **Ping impairment; call-flow comparison incomplete**: ping impairment is
  measured but call-flow coverage/timing cannot establish the comparison.

Receiver impairment means packet loss or jitter above 30 ms; ping impairment
means timeout or RTT above the recorded run threshold. Normal comparisons require
at least 80% eligible coverage and aligned receiver intervals with at most one
second of timing uncertainty. No-impairment and insufficient-comparison outcomes
apply to whole reports, not degradation episode rows. None proves a root cause.

For deployment I/O, memory and separate logical volume per monitored router,
see **Menu → Harbour footprint** and the README's storage-sizing section. Router
process I/O itself remains unavailable for SSH-only OpenWrt probes.

Authenticated endpoints under `/api/servers/{id}/network/analysis`:

- `GET ?hours=168&until=<epoch>` lists reports, pending sources and patterns.
  `prepare=true` analyses at most ten pending sources per request; the UI proceeds
  in batches and allows stopping after the current batch.
- `GET /call/{test_id}` or `/incident/{event_id}` reads a detailed conclusion;
  `download=true` returns its gzip handoff. Open incident windows cannot be exported
  as sealed conclusions.
- `GET /download?hours=168&until=<epoch>` exports saved conclusions for that period.
  This does not fetch missing evidence. Expired/changed reports during export
  return a refresh request; insufficient local storage returns HTTP 507.

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

## Scheduled reports (v0.1.42)

Archive rotation now follows Reports → Processing & retention, replacing the
fixed midnight UTC rotation described in older versions. Retention age for a
completed raw archive runs from its actual close time. Completed files are only
trimmed after scheduled report publication succeeds. See [REPORTS.md](REPORTS.md)
for calendar boundaries, late evidence, printable output and processing limits.
