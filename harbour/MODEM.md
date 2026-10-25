# Direct modem evidence

Open an OpenWrt server’s **Modem** tab and choose **Modem settings** as an
administrator. Configure the modem address and its administration credentials,
then use **Test connection** inside the settings overlay. It checks the entered
address and credentials without saving settings, changing polling state, or
adding test readings to the overview/history. The result stays in the overlay;
edited values remain available to save or discard. **Save** is a separate action
and does not run a connection test. Polling is off until explicitly enabled.
The address field's **ⓘ** help lists common modem/gateway IP addresses.
The default address is `http://192.168.100.1`; the Harbour container must have a
route to it. No software, SSH work or measurement process runs on the router.
The existing OpenWrt and network recorder settings are independent.
Collection status and the five diagnostic sections appear in one row of chips;
select a section to open or close its values in a bounded panel. The row scrolls
horizontally on narrow screens. Read status describes collection, not line health.

This initial adapter targets the Compal CH7465 ONE web protocol. The login
protocol and getter numbers were checked against device-served JavaScript.
Authenticated XML schemas, field units and firmware compatibility still need
verification against the actual modem. Other modem families are not supported.
There is no default password guessing or configuration/reset operation.

Username is optional. On supported Compal forms Harbour uses an explicitly
configured username, or the default supplied by the modem's actual login form.
On supported password-only forms it omits the username from authentication.
If a username is required but the form provides no default, the test asks for
one without submitting the password. This is firmware-dependent, not inferred
from the sticker or model alone. Other authentication protocols remain unsupported.
The test distinguishes successful authentication from readable diagnostics;
neither establishes that the line is healthy.

## Credentials and access

Credentials are administrator-only to modify, encrypted with Harbour’s existing
Fernet application key, and never returned by the API. A blank password retains
the saved value; removal is explicit. Changing the address or username requires
re-entering or removing the saved password. All signed-in Harbour users can read
the collected evidence. Retain `HARBOUR_SECRET` securely with database backups.

Access Harbour over HTTPS. Encryption at rest does not encrypt the modem’s LAN
HTTP connection: its SHA256 password hash and SID cookie are sensitive. HTTPS
modem URLs require a trusted certificate; certificate verification is not disabled.
Targets must be literal RFC1918 IPv4 addresses. Redirects, environment proxies and
DNS lookup are not used. Only login function 15, logout function 16 with this
client's own SID, and read functions 1, 2, 10, 11, 13 are permitted;
configuration setters and forced logout of another session are rejected.

The pre-login `sessionToken` cookie and authenticated SID remain in process memory
and are reused only for the configured modem. The token is bounded, never returned
to Harbour's browser, and redacted from collected values. An expired established
session gets one reauthentication attempt within the collection deadline. A
rejected login pauses automatic polling until settings are saved (or the legacy
saved-settings test API succeeds). Overlay tests do not change that state. Firmware
may allow only one administrator session: disable polling and save before interactive use.
Disabling polling, replacing connection credentials, removing the monitored host,
and orderly worker shutdown release Harbour's owned session on a best-effort basis.
An abrupt process exit can leave it active until the modem's session timeout.
No attempt is made to evict an existing administrator or bypass a lockout.

## Collection and limits

A separate Harbour worker polls sequentially, normally every 60 seconds
(30–900 configurable), with no overlapping test/background collection for a modem.
The next interval starts after a completed attempt. Failures back off to 15
minutes. The read cycle has a 25-second deadline, 4-second socket timeouts,
128 KiB per response, 384 fields per section and a 32 KiB stored snapshot cap.
Five status requests are made per established-session cycle; initial login adds
two requests. Host monitoring pause also suspends background modem polling.
Demo mode never contacts hardware. No additional runtime dependency is required.

`POST /api/servers/{id}/modem/test-settings` checks draft connection settings.
It requires an administrator and CSRF protection, uses the same per-server lock
as saved collections, and returns only a transient summary. A blank password
uses the saved secret only when address and username are unchanged. Enter a
password when testing another address or username; it is not stored. Existing
configured sessions are reused for matching saved credentials, and a new successful
test with saved credentials supplies the poller's in-memory session. Drafts with
different credentials release an owned session on that modem before testing, then
log out their temporary session. A failed logout is reported without exposing its
response; another test may need to wait for the modem session to expire. Settings
and history remain unchanged. Testing while polling is disabled can retain a
saved-credential session for subsequent tests or enabling polling. The legacy
`POST .../modem/test` remains available for saved-settings collection clients;
it is no longer used by the browser UI.

System/device values, downstream/upstream channels and event logs are stored
with their reported field names. Units and counter scaling are not inferred.
Secret/identity fields and known credential values are omitted or redacted;
bounded event text may still contain operational network information.
Each section records request and receipt times using the Harbour clock. Modem
event timestamps remain unchanged; they may use a different clock or timezone.
Each settings change starts a new evidence revision.

Collection success is not a line-health verdict. A timeout, invalid response,
authentication failure, paused monitor or missing sample does not establish an
ISP or router outage. Partial reads retain usable sections. After a restart,
the new process must log in again; a failed attempt cannot turn old data current.
The UI labels old readings stale and never fills missing periods with zeroes.

History is persisted in Harbour SQLite with a default seven-day retention
(1–30 days configurable) and 64 MiB payload cap per modem. Bounded cleanup runs
on collections; paused monitors retain their existing data until collection
resumes. Settings and samples are removed when their parent server is deleted.
The history API remains limited to the latest 256 snapshots for interactive
browsing, with an explicit `truncated` flag. The evidence JSON export streams
**all retained snapshots** in the requested window (default 24 hours, maximum
720), including archived modem evidence. An optional `until` Unix timestamp
fixes the end of the window. It includes coverage, a modem-only assessment,
values and revision IDs. A preceding baseline can appear in referenced evidence,
but is excluded from the window's sample count. There is no 256-reading export cap.

Each sanitized snapshot is also compressed into the report evidence store in the
same transaction as collection. Legacy snapshots are captured before short-history
pruning. This archive follows the existing daily-report age and storage policy;
it is included in that policy's byte budget. It is retired with complete days,
so retaining modem evidence does not bypass the report storage limits. A paused
report processor can retain staging evidence until successful publication, just
as it does for network facts. No second hardware poll is performed.

## Independent indications and analysis handoff

The Analysis review has a **Modem** section. Its assessment consumes only recorded
modem values. Network conclusions and contributor rules are unchanged. A separate
table intersects the modem observation intervals with network episodes; overlap
is a review aid, not additional root-cause attribution.

Supported Compal fields provide these conservative rules:

- A decrease in the explicitly formatted `cm_system_uptime` indicates a restart
  or uptime reset; its cause is unknown.
- `IsQamLocked`, `IsFECLocked` or `IsMpegLocked` equal to `0` on an identified,
  populated downstream channel is a modem-reported unlock indication.
- Increases in `PostRs`, `t3Timeouts` or `t4Timeouts` are indications. `PreRs`
  increases are context, without an invented error rate or severity threshold.
  Counters are matched by direction, channel ID and frequency. Cumulative
  nonzero values alone do not indicate a fault in the selected window.
- Counter decreases are reset/discontinuity context. A settings, observer,
  endpoint or firmware change, a known uptime reset, or an excessive collection
  gap breaks counter comparisons. Unsupported units are never guessed.
- Warning/error/critical/alert/emergency log entries are deduplicated by content
  and shown as historical context. Their unverified device timestamps do not
  participate in automatic temporal correlation.

No defined indication is **not** a healthy-line verdict. Every assessment carries
coverage, missing/partial/truncated readings, first/last observations and method
limits. At most 128 findings and 64 supporting snapshots are retained in each
assessment; counts cover all processed observation intervals (channel changes within an interval are grouped) and omitted examples are
explicit. Correlation tables retain at most 512 rows with an omitted count.

Daily reports preserve the assessment and its selected evidence. Connection
analysis downloads include the independent assessments, correlation tables and
a separate `modem_evidence` section containing all retained modem snapshots in
the selected period. Each modem assessment owns its evidence namespace;
`modem_evidence_refs` resolve inside its `evidence` object. Baseline evidence is
included when used, even if just before the selection. Older reports can lack
modem evidence; expired readings cannot be reconstructed.

Exports are prepared locally and streamed to disk, with an explicit failure
rather than clipping if the modem section exceeds eight times the configured
processing byte budget. Files remain self-contained for offline/LLM review;
credentials, login cookies and tokens are never sourced from settings for export.
Operational addresses and bounded event text remain part of the evidence.

## Validation boundary

Automated tests use synthetic firmware responses and local HTTP fixtures. They
do not prove that a particular firmware exposes every authenticated diagnostic
field. Confirm credentials using the settings form, inspect the first readings,
and compare values/units with the modem interface before interpreting DOCSIS
power, SNR, error counters or event codes as ISP evidence.

Session lifecycle handling was cross-checked with the
[DOCSight CH7465 driver](https://github.com/itsDNNS/docsight/blob/main/app/drivers/ch7465.py).
Harbour uses its own bounded client and encrypted credential/history integration;
DOCSight is not an installed runtime dependency. Single-session HTTP fixtures
cover shared test/poll authentication, temporary-session logout, expiration,
rejected reauthentication, and orderly worker cleanup.

## Address examples

The help lists examples, not a scan or a supported-device catalogue. Check the
device label and ISP documentation, and distinguish the modem from any separate
router. Common examples are `192.168.100.1`, `192.168.0.1`, `192.168.1.1`,
`192.168.1.254` and `10.0.0.1`. Sources:

- [NETGEAR cable modem access](https://kb.netgear.com/000059644/How-do-I-log-in-to-my-NETGEAR-cable-modem)
- [Virgin Media modem/router mode addresses](https://www.virginmedia.com/help/broadband/reboot-or-reset-hub)
- [TP-Link device addresses](https://www.tp-link.com/us/support/faq/1170/)
- [AT&T gateway access](https://www.att.com/support/article/u-verse-high-speed-internet/KM1206322/)
- [Xfinity gateway Admin Tool](https://www.xfinity.com/support/articles/admin-tool-access)
