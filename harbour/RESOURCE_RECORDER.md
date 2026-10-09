# Resource recorder and remote probes

Harbour automatically uses a healthy installed recorder. Without one, a
session-bound probe collects resources on request. Both use pinned SSH: one
connection and one Python reader are reused for resource requests. Nothing
listens on an additional network port. The reader exits when SSH closes; only an
installed recorder samples independently or starts at boot.

## Install or upgrade

In Server settings, choose **Install recorder** and download the bundle. Extract
it on the monitored Linux/macOS host. Use the same account Harbour uses for SSH:

```sh
python3 resource_install.py --reader harbour --dry-run
sudo python3 resource_install.py --reader harbour
```

Python 3.9+ and a trusted, permanently available interpreter are required. Use
`--python /absolute/path/to/python3` if necessary. Installation needs root; the
service runs as the reader account. It uses systemd on Linux and launchd on
macOS. Install on the actual monitored host, not inside Harbour's container.
The resource and authentication collectors are separate services.

Harbour detects the recorder on its next check; there is no agent-enable switch.
The private socket and queue are accessible only to the reader account and root.
Reinstalling the bundle replaces the code and preserves pending samples. Changes
to sampling settings arrive on the next successful exchange and persist locally.

## Intervals and history

- Probe / collection interval: remote sampling, or recorder queue retrieval.
- Recorder sample interval: autonomous sampling (default 60 seconds).
- Disk capacity interval: default 300 seconds. Cached capacity remains visible
  with its original measurement time and is not repeatedly added to history.
- Docker inventory interval: default 300 seconds, independent of resources.
- Registry checks retain their separate configured schedule.

The SSH helper is loaded once per connection and reused. Recorder exchanges do
not run the full resource probe. In remote mode CPU, memory and selected sensors
are sampled on request. Linux disabled sensor inputs and excluded disk mounts
are skipped; shared macOS hardware interfaces can still read several sensors in
one call. CPU counters remain available for boot/interval accounting. CPU history
uses duration-weighted averages where all contributing samples have durations;
legacy mixed buckets retain their original sample-average interpretation.

Memory and temperature are snapshots, and CPU peaks are peaks of sampled interval
averages. No mode reconstructs unsampled spikes. Alerts use the latest collected
reading: increasing collection intervals delays alerts even when local samples
are frequent. Pausing monitoring stops transfer, not the installed recorder.

Each sample records source time, uptime and boot identity. The queue commits
samples durably to SQLite. Harbour stores a batch and its acknowledgement in one
transaction, deduplicates retries, and sends the acknowledgement on the next
exchange. Only then is that batch removed locally. The latest reading remains
available even after its history batch has been acknowledged.
SSH reachability and latency are recorded at transfer time; backfilled samples
do not turn earlier connection failures into successful checks.

Default bounds are 20,000 samples and 64 MiB of sample payloads (whichever fills
first); SQLite overhead, reusable pages and the latest snapshot require additional
space. Oldest unacknowledged samples are dropped at the limit and the loss count
is shown in Harbour. At 60 seconds, 20,000 samples represent approximately 13.9
days, provided the byte limit is not reached first. A batch holds at most 200
samples / approximately 2 MiB; a single sample cannot exceed 500 KB.

An unreachable, stale or inaccessible installed recorder produces a visible
warning and remote probing fallback. Retained recorder samples are replayed when
it recovers. Network loss cannot be backfilled on hosts with no local recording.

**Boot history** beside Uptime lists the first observed boot and later distinct
boot identities. A recorder restart does not count as a host reboot. Estimated
boot times depend on the host clock. Remote probes cannot count multiple reboots
between checks; local recording preserves only boots that it actually samples.
Observations follow the workspace history retention policy, with the latest 100
shown. Uptime is also stored in the resource-history API.

## Remove / off-board

Collect pending samples before removal if they are needed centrally. On the host:

```sh
sudo python3 /usr/local/libexec/harbour-resources/resource_install.py --uninstall --dry-run
sudo python3 /usr/local/libexec/harbour-resources/resource_install.py --uninstall
```

This stops/disables the service and removes its program and private socket.
Harbour returns to remote probes at the next check. The local queue is retained
by default, and Harbour's existing history is unchanged. Add `--purge-data` to
the uninstall command to explicitly delete local recorder data. If necessary,
use `resource_install.py` from a freshly extracted bundle to purge retained data
after uninstall. Removal does not require the original reader account to exist.

Removing the server entry in Harbour does not uninstall host services or revoke
its SSH key. Remove the recorder first, then remove the server entry. A separate
login collector must be removed separately if it is no longer needed.

Local data: `/var/lib/harbour-resources` on Linux;
`/Library/Application Support/HarbourResources` on macOS.
No live host installation or hardware-independent CPU/RAM guarantee is implied
by the implementation: measure on the smallest actual host before fleet rollout.
