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
2. In **Network → Call traffic tests → New traffic test**, choose a profile and
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

Copy [`deploy/call-test-endpoint/`](../deploy/call-test-endpoint/) to your remote
Linux Docker host and run inside that directory:

```sh
docker compose up -d --build
```

This starts two unprivileged iperf3 containers with read-only root filesystems
and a small temporary RAM filesystem: **5201 for upload** and
**5202 for download**. Both TCP control and UDP traffic must reach both ports.
Restrict these published ports to the intended survey sites at the host/cloud
firewall; the template does not provide authentication. Each listener has a
10 Mbps server-side limit. This is an abort threshold, not a traffic shaper or a
substitute for access control. The remote host must have spare bandwidth and CPU;
its own congestion would become part of the result. Multiple simultaneous sites
need separate port pairs, because each listener accepts one test at a time.

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
endpoint is deployed separately. Start a test from the Network view only after
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

## Validation for this implementation

Local validation passed 465 Python tests, the OpenWrt desktop/mobile browser
fixture, syntax checks and Docker image/Compose checks. The runner also completed
a ten-second simultaneous 128/256 kbps test using Linux iperf3 3.18 in a disposable
container with external networking disabled, returning receiver intervals in
both directions. Native iperf3 3.21 result formats were checked on loopback.
No remote endpoint, monitored router, real call, Wi-Fi client or WAN path was
tested or deployed. Actual-router CPU overhead and real-call correlation remain
field-validation work.

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
