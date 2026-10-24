# Direct modem evidence

Open an OpenWrt server’s **Modem** tab and choose **Modem settings** as an
administrator. Configure the modem address and its administration credentials,
save, then use **Test connection**. Polling is off until explicitly enabled.
The default address is `http://192.168.100.1`; the Harbour container must have a
route to it. No software, SSH work or measurement process runs on the router.
The existing OpenWrt and network recorder settings are independent.

This initial adapter targets the Compal CH7465 ONE web protocol. The login
protocol and getter numbers were checked against device-served JavaScript.
Authenticated XML schemas, field units and firmware compatibility still need
verification against the actual modem. Other modem families are not supported.
There is no default password guessing or configuration/reset operation.

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
DNS lookup are not used. Only login function 15 and read functions 1, 2, 10, 11,
13 are permitted; configuration setters are rejected by the client.

The SID remains in process memory and is reused. A rejected/expired login pauses
automatic polling until settings are saved or a manual test succeeds. Firmware
may allow only one administrator session: pause polling before interactive use.
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
The history API and JSON export include at most the latest 256 snapshots in a
requested window (default 24 hours, maximum 720). `truncated` is explicit.
`GET /api/servers/{id}/modem/history?summary=false` includes section values;
the default summary excludes them. Export includes values and revision IDs.

## Validation boundary

Automated tests use synthetic firmware responses and local HTTP fixtures. They
do not prove that a particular firmware exposes every authenticated diagnostic
field. Confirm credentials using the settings form, inspect the first readings,
and compare values/units with the modem interface before interpreting DOCSIS
power, SNR, error counters or event codes as ISP evidence.
