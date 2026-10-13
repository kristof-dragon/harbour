# Video-call survey research and implementation guide

Research checked 13 October 2026; implementation released in Harbour **0.1.30**.
These tests run from the **on-site Harbour installation**; nothing is installed
on, or changed in, the OpenWrt router. This file collates the findings, source
references, implementation decisions and deployment procedure.

## Findings and decisions

- **Increase the target limit to 12.** The extra destinations help compare the
  local router, remote test endpoint and independent Internet paths. The limit
  is a configurable ceiling, not a requirement to populate every slot. Probing
  and storage grow with the number of enabled targets; use the smallest useful
  set and check overhead on the actual router. The implementation shares one
  SSH connection for router pings and expands the incident evidence row limit.
- **Use a remote endpoint for bounded two-way UDP tests.** This provides a
  repeatable load while the recorder observes changes. A Docker endpoint is
  included, with separate listeners so upload and download rates can differ.
- **Provide distinct application presets, with their evidence visible.** Teams
  and Zoom publish different bandwidth guidance for individual and group calls.
  FaceTime has a provisional, editable preset. None is a captured or complete
  reproduction of that application's traffic.
- **Follow the layered approach demonstrated by the professional tools below.**
  Combine path and radio evidence, controlled transport tests, and actual media
  experience. Harbour implements the controlled transport layer and correlates
  router evidence. Testing a wired Harbour host cannot establish the quality of
  a phone's Wi-Fi connection or certify an application call.

These are Harbour's engineering decisions based on the cited documentation,
not an industry certification standard or an exhaustive market survey.

## What professional tools actually test

| Layer | Documented practice | Implication for Harbour |
| --- | --- | --- |
| Wi-Fi and access path | NetAlly combines UDP throughput, upload/download loss and jitter with the connected BSSID, signal, SNR and retries. | Test from the affected Wi-Fi client as well as from a wired reference. A wired server cannot validate the phone's radio path. |
| Controlled transport | NetAlly supports an iperf endpoint; its performance peers separate directions, whereas reflectors report round trips. | Keep upload/download receiver measurements separate. Retain ICMP RTT as its own measurement. |
| Synthetic media | ThousandEyes RTP tests select codec, DSCP and jitter-buffer settings and report loss, discards, latency, packet-delay variation and an estimated voice MOS. | A bitrate preset is useful but is not a codec model. Do not invent video MOS from ping or UDP jitter. |
| Actual application experience | Teams provides per-call quality statistics and CQD alongside synthetic network assessment. WebRTC statistics include media-specific counters such as frozen frames, concealment and jitter-buffer delay. | Correlate synthetic results with real calls. A controlled WebRTC test could add a client-side media check later. |

Sources: [NetAlly UDP survey application note](https://www.netally.com/wp-content/uploads/2021/04/AirCheckG2-iPerf-App-Note-2021.pdf),
[NetAlly peers and reflectors](https://www.netally.com/knowledgebase/performance-testing/),
[ThousandEyes voice tests](https://docs.thousandeyes.com/product-documentation/tests/voice-tests),
[Teams quality review](https://learn.microsoft.com/en-us/microsoftteams/quality-of-experience-review-guide),
[WebRTC statistics](https://www.w3.org/TR/webrtc-stats/).

Even a professional instrument's displayed one-way latency may be an estimate:
[NetAlly documents RTT divided by two](https://www.netally.com/user-guides/etherscopenxg/Content/B_Testing-Applications/Performance/Running-Performance-Test.htm).
Harbour therefore keeps RTT labelled as RTT. Unsynchronised clocks cannot establish
accurate absolute one-way delay, and asymmetric paths need not split RTT equally.

## Implemented profiles

The numbers below are **load presets**, not measured traffic signatures or a
promise that a real application uses this rate. Directions are from the on-site
host's perspective. Rates may be edited and the actual test settings are saved.

| Profile | Upload | Download | Basis |
| --- | ---: | ---: | --- |
| FaceTime, provisional video | 2 Mbps | 2 Mbps | Harbour starting approximation; not an Apple recommendation |
| Teams, one-to-one video | 1.5 Mbps | 1.5 Mbps | Microsoft recommended video bandwidth |
| Teams, video meeting | 2.5 Mbps | 4 Mbps | Microsoft recommended meeting video bandwidth |
| Zoom, one-to-one 720p | 1.2 Mbps | 1.2 Mbps | Zoom recommended 720p bandwidth |
| Zoom, group 720p | 2.6 Mbps | 1.8 Mbps | Zoom recommended group 720p bandwidth |

[Microsoft's table](https://learn.microsoft.com/en-us/microsoftteams/prepare-network#bandwidth-requirements)
distinguishes minimum, recommended and best-performance bandwidth and notes that
actual media adapts. [Zoom's table](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0060748)
distinguishes resolutions, group calls and gallery views. Their capacity guidance
does not prescribe fixed UDP payload rates: using it as a synthetic load is our
engineering approximation, with IP/UDP/link overhead additional.

The current Apple material reviewed describes [FaceTime's encrypted SRTP and
ICE/STUN peer-to-peer establishment](https://support.apple.com/guide/security/facetime-security-seca331c55cd/web)
and [firewall requirements](https://support.apple.com/en-us/102036), but did not
provide a comparable current bitrate table. Calibrate the provisional preset
against observed traffic during representative calls on the affected devices.
A test to our endpoint also follows a different destination path from Apple,
Microsoft or Zoom services and cannot validate their signalling or media relays.

All presets currently use a common, editable **1200-byte UDP payload**, fixed-rate
pacing and default best-effort QoS. This is not an app-specific packet size.
Separate audio, frame bursts, variable encoding, adaptive congestion control,
media retransmission/FEC, DSCP policies and jitter buffers are not reproduced.

## First test procedure

1. Keep the normal recorder probes running. Include the router LAN reference,
   remote test endpoint and useful independent destinations among the 12 targets.
2. In **Call tests → Remote call flow test**, choose a profile and
   supply the remote IP and first port. Check the displayed rates and data volume.
3. Default timing is 60 seconds idle, 180 seconds under load and 60 seconds
   recovery. Compare RTT, receiver loss/jitter and router CPU, queues and retries
   across these periods. A load-start marker links the surrounding router evidence.
4. Repeat under similar conditions and compare with a real call. A successful
   wired test plus a bad Wi-Fi call narrows the investigation but does not prove
   the WAN or application innocent.

Results are receiver-reported throughput, packet loss and jitter per direction.
The final jitter value is iperf3's receiver estimator, not a claim about all-packet
peak jitter or rendered video. Per-second receiver intervals are in the export;
upload intervals require the remote server's JSON output. Missing measurements
stay unavailable. No universal pass/fail or MOS score is assigned. Published
application diagnostic thresholds are not interchangeable acceptance criteria.

Tests are explicit, one at a time across this Harbour installation, bounded to
64–8000 kbps per direction and 10–300 seconds of load. The latest 20 results per
router are retained. Exports preserve profile version/source, modified settings,
iperf3 output and direction-specific timing. Interval times are relative to that
iperf3 test; local process start is only an approximate wall-clock anchor.

Closing the browser does not stop a test. Stop test, pausing monitoring or network
recording, changing/removing the router connection, and graceful Harbour shutdown
cancel it. A restarted Harbour marks unfinished tests interrupted and never
resumes traffic automatically. A forced process/host failure can prevent result
recovery; iperf3 duration limits still bound an orphaned client process. A failed
or cancelled run is not reported as a clean test. The independent passive recorder
continues when only the web service restarts.

The incident window is two minutes before to five minutes after its marker,
bounded to 60,000 rows. Long recovery periods can extend beyond it. Export the
traffic test separately for the full test evidence. At 12 targets and 250 ms,
normal recorder/router/reference/health rows total roughly 28,000 per seven-minute
window; late replies and logs add more. Retention size caps may fill sooner with
more targets. Router pings now share one SSH connection, but still run one bounded
ping per selected target approximately every second: validate CPU overhead on the
actual router before adopting the fastest settings permanently.

## Remote Docker endpoint

Use the existing **WebUI remote installer** (Harbour 0.1.34+):

1. Add the remote **Linux** host to Harbour as a Docker or plain server, with its
   verified SSH connection. Docker Engine and the Compose plugin must already be
   available; Harbour does not install or upgrade host packages.
2. On the OpenWrt server, open **Call tests → Deploy remote endpoint** and select
   that remote host. Set its reachable IP and the first traffic port (default
   5201; the second is 5202). Choose the log HTTP port, initially a random number
   between 20000 and 59999. If the remote port is occupied, choose another.
3. Leave the token blank to generate one securely on first deployment, or supply
   32–128 URL-safe characters. A blank token on subsequent deployments preserves
   the saved token. Set retention (30 days by default).
4. Select **Save & deploy endpoint**. The existing installer streams progress
   over a separate, pinned SSH connection and asks for sudo only when needed.
   Its sudo password is never saved. Closing the dialog cancels the setup
   connection; completed deployment steps and logs remain on the host.
5. Open **Remote logs** to choose a date and download files. These requests use
   HTTP with `Authorization: Bearer …`; they do **not** open SSH. Saved endpoint
   addresses and ports are also available in the call-test form.

The deployment always lives in the SSH user's **`~/harbour-c-remote`**:

```text
harbour-c-remote/
  compose.yaml, Dockerfile, build-iperf.sh, call_endpoint_service.py
  endpoint.json, .env       # protected configuration; token is in endpoint.json
  previous/                # one protected previous deployment configuration
  logs/
    26-10-13/
      upload-HHMMSS-id.jsonl
      download-HHMMSS-id.jsonl
    26-10-14/
      ...
```

Dates use **UTC**, always `YY-MM-DD`. JSONL records have UTC timestamps and
contain listener lifecycle events plus chunks of iperf's JSON output. The
original iperf result can span consecutive output records; these are endpoint
logs, while the test export remains the parsed receiver result. Files rotate at
3 MiB and date boundaries when output arrives. The next output after midnight
is written into the new date folder. Completed files expire after the chosen
retention or when the 1 GiB total cap is reached. Cleanup runs every 30 seconds;
current writer files and data between cleanup passes can temporarily exceed the
cap. Existing logs survive container recreation and repeat deployments.

Harbour encrypts the HTTP token in its database and never sends it back in
settings, test exports or setup progress. The remote configuration and logs use
owner-only permissions. A custom token can be selected during deployment for
other HTTP clients; the generated token also remains in the protected remote
configuration. Read-only Harbour users may download logs; only administrators
can configure/deploy endpoints. The remote API provides `GET /logs`,
`GET /logs/YY-MM-DD` and `GET /logs/YY-MM-DD/name.jsonl`, all token protected.
It has no execution or upload endpoints, rejects symlinks/traversal and bounds
file size, socket wait time and concurrent connections. Tokens stay in headers,
never URLs. Plain HTTP requires a trusted LAN or VPN: it does not encrypt the
bearer token. A random port is not access control.

The bundle starts one unprivileged container containing two iperf3 listeners
and the HTTP service. It uses a read-only root filesystem, limited memory/PIDs,
no Linux capabilities and separate persistent log/config mounts. The chosen
traffic ports must allow **both TCP control and UDP data**; the log port needs
TCP. Restrict all three published ports to the intended Harbour site using the
host/cloud firewall. The installer does not change firewall rules. The HTTP
token protects logs only; iperf itself remains source-restricted. Each listener
has a 10 Mbps abort limit and rejects tests longer than 330 seconds, following
[ESnet's documented server options](https://software.es.net/iperf/invoking.html).
The remote host needs spare CPU/bandwidth, and one endpoint pair serves one site
at a time. Multiple sites need separate hosts/deployments.

The files in `deploy/call-test-endpoint` are installer templates. The WebUI
bundle adds the standalone service and installer; copying that template folder
alone is no longer a complete deployment. A failed update keeps a protected
`previous/` copy of managed configuration for administrator recovery; it does
not automatically roll back or erase logs.

Harbour launches separate upload and reverse-download clients, allowing the
asymmetric app presets. A single `--bidir` test would apply the same configured
rate to its streams. The endpoint uses `-J` and Harbour requests its results, so
the upload receiver's intervals are available after completion. No Docker API
access or privileged container is needed. [iperf3 options](https://software.es.net/iperf/invoking.html).

Harbour's container includes iperf3 after rebuilding. For native Harbour installs,
install iperf3 through the host package manager and ensure it is on the service's
PATH. The UI reports whether it is available. No automatic host package install
is performed by the app. Docker NAT/container networking remains part of the
tested path; record the actual deployment and routes when comparing results.

For an existing Harbour deployment, run in its repository directory:

```sh
git pull --ff-only
python3 scripts/start.py
```

This rebuilds and restarts Harbour, including its iperf3 client. The remote
endpoint is deployed separately. Start a test from the Call tests tab only after
that endpoint is reachable; enabling the feature does not start traffic by itself.

## Useful subsequent extensions

- **Video frame bursts:** iperf2 has an isochronous mode that varies frame sizes at
  a chosen frame rate, making it closer to video-shaped load. It is a separate,
  incompatible protocol and would need its own endpoint and parser. It still
  does not emulate FaceTime, Teams or Zoom adaptation. [iperf2 manual](https://iperf2.sourceforge.io/iperf2-user-manual.html).
- **Real media on the affected client:** a self-hosted WebRTC test page and peer
  can exercise the phone/laptop's Wi-Fi, encoder/decoder, adaptive media and jitter
  buffer using synthetic audio/video. Collect supported freeze, concealment,
  loss, jitter and candidate-path statistics; record whether traffic was relayed.
  This remains a test of our WebRTC session, not certification of another app.
- **Application-specific confirmation:** repeat FaceTime calls on the affected
  Apple device and use Teams/Zoom's real-call diagnostics where available. Keep
  these observations separate from synthetic results.

Docker gives us room to add those services later. The controlled UDP test is the
first implementation; no WebRTC, RTP codec model or vendor-call emulator is
included in this version.

## Validation for the initial implementation

Local validation passed 465 Python tests, the OpenWrt desktop/mobile browser
fixture, syntax checks and Docker image/Compose checks. The runner also completed
a ten-second simultaneous 128/256 kbps test using Linux iperf3 3.18 in a disposable
container with external networking disabled, returning receiver intervals in
both directions. Native iperf3 3.21 result formats were checked on loopback.
No remote endpoint, monitored router, real call, Wi-Fi client or WAN path was
tested or deployed. Actual-router CPU overhead and real-call correlation remain
field-validation work.

The subsequent 0.1.32 dependency upgrade pins iperf3 3.22 in both Docker images;
see [stack versions and compatibility notes](DEPENDENCIES.md). The WebUI installer includes the shared `build-iperf.sh` in each deployment bundle.

## Validation for the remote installer and layout update (0.1.34)

- Full suite on the pinned Linux Python 3.15 runtime: **480 passed, 1 skipped**
  (the platform-specific macOS launcher). After the final installer changes,
  all **37 endpoint, collector setup and call-test tests passed**, including
  the additional complete SSH bootstrap protocol fixture for this endpoint.
- OpenWrt browser checks passed at 390, 700, 900, 1200, 1440 and 2600 px:
  separate views, responsive probe cards, bounded sidebar, undimmed mobile
  drawer, WebUI deployment form, log date selection and download links.
  Existing refresh/resource and collector-install browser suites also passed.
- Both Docker images built. The endpoint was tested as an unprivileged user
  with a read-only filesystem and external networking disabled. Simultaneous
  ten-second UDP loads at 128/256 kbps returned receiver measurements in both
  directions and remote upload intervals. Local dated logs and authenticated
  HTTP downloads worked; bad tokens and unsafe paths were rejected. This caught
  and resolved a Docker file-permission issue before publication.
- Real HTTP tests cover token authentication and Harbour's log proxy; filesystem
  tests cover UTC rollover, file rotation, retention and symlinks. Installer
  tests preserve existing logs/configuration and exercise the separate setup
  connection, sudo secrecy, cancellation and OpenWrt installation refusal.
- Production Harbour container smoke checks passed for authentication, encrypted
  credentials, permissions, persistence and non-root/read-only operation.

The remote installer is ready to run from the WebUI. No user-owned remote host,
router, firewall or real-call path was changed or tested during this update.

## Reference index

All links below are primary vendor, project or standards sources reviewed for
this work. The review date is above; it is not a claim that the sources were
published or updated on that date. Inline links connect individual findings to
their sources; this index keeps the research together for later review.

| Source | Finding used and limits |
| --- | --- |
| [NetAlly: AirCheck G2 iPerf application note](https://www.netally.com/wp-content/uploads/2021/04/AirCheckG2-iPerf-App-Note-2021.pdf) | UDP testing alongside Wi-Fi connection and radio measurements; supports comparing transport results with the affected client's radio path. |
| [NetAlly: performance testing](https://www.netally.com/knowledgebase/performance-testing/) | Peer and reflector test approaches; directional evidence differs from round-trip evidence. |
| [NetAlly: running a performance test](https://www.netally.com/user-guides/etherscopenxg/Content/B_Testing-Applications/Performance/Running-Performance-Test.htm) | Its documented one-way latency estimate uses RTT divided by two; this does not establish equal delay in each direction. |
| [ThousandEyes: voice tests](https://docs.thousandeyes.com/product-documentation/tests/voice-tests) | RTP tests with codec, DSCP and jitter-buffer settings, including voice MOS. This is evidence for a richer media model, not a basis for assigning video MOS to iperf3 results. |
| [Microsoft: prepare your organization's network for Teams](https://learn.microsoft.com/en-us/microsoftteams/prepare-network#bandwidth-requirements) | Recommended video rates for one-to-one and meeting profiles, distinct from minimum and best-performance rates. Capacity guidance is not a fixed packet signature. |
| [Microsoft: quality of experience review guide](https://learn.microsoft.com/en-us/microsoftteams/quality-of-experience-review-guide) | Real-call quality investigation and network evidence; supports correlating synthetic tests with actual application experience. |
| [Microsoft: Teams Network Assessment Tool](https://www.microsoft.com/en-us/download/details.aspx?id=103017) | Vendor-specific synthetic network assessment against Teams infrastructure. Harbour's private endpoint does not exercise those service paths. |
| [Zoom: desktop system requirements](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0060748) | Different recommended bandwidth for 720p one-to-one and group video; other resolutions and layouts differ. |
| [Apple: FaceTime security](https://support.apple.com/guide/security/facetime-security-seca331c55cd/web) | SRTP and connection establishment; no comparable fixed video bitrate table in this source. |
| [Apple: use FaceTime and iMessage behind a firewall](https://support.apple.com/en-us/102036) | Connectivity requirements; does not substantiate Harbour's provisional 2/2 Mbps choice. |
| [ESnet: invoking iperf3](https://software.es.net/iperf/invoking.html) | UDP, reverse and bidirectional modes, rate and length settings, JSON output and server limits used by the implementation. |
| [iperf2 user manual](https://iperf2.sourceforge.io/iperf2-user-manual.html) | Isochronous frame-oriented traffic as a possible future extension; requires a different endpoint and parser. |
| [W3C: WebRTC statistics API](https://www.w3.org/TR/webrtc-stats/) | Media statistics for a possible browser-based test, including freezes, concealment and jitter-buffer delay; actual browser support needs checking. |
| [IETF RFC 8836: congestion control requirements for interactive real-time media](https://www.rfc-editor.org/rfc/rfc8836.html) | Interactive media adapts to network conditions; fixed-rate UDP alone does not reproduce that behaviour. |
