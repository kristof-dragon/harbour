# Warning history

Harbour stores a warning episode when background monitoring first observes a warning. Further abnormal readings update that episode's last observation and extreme values. A confirmed normal reading closes it as resolved. A later recurrence is recorded as another occurrence of the same warning, identified by server and resource. Notifications, History and the warning count show one entry per warning, including for episodes recorded by older versions. The occurrence count and a paginated occurrence history retain the original evidence without hundreds of duplicate list entries. Counts and first detection cover retained history; the status, limits and readings on the grouped entry describe its latest occurrence.

Warnings stay in the list after acknowledgement, including resolved warnings. **Acknowledge** silences the signed-in user's warning count, server badges, banners, resource highlights and warning tooltips until a new occurrence. It does not change readings, monitoring, connection status or Telegram delivery. Repeated abnormal samples, missing readings and interrupted monitoring do not re-arm an acknowledged episode. A fresh occurrence is unacknowledged and visible again.

**Remove from list** changes only personal visibility. It neither acknowledges the warning nor deletes its evidence. **Bring back warnings** restores removed entries without changing acknowledgement or reactivating silenced indicators. These controls are independent of **Dismiss all updates** and **Bring back updates**. Bulk acknowledgement and bulk removal are separate actions. Opening a warning does not acknowledge it.

History includes acknowledged and removed warnings with server/status/acknowledgement filters, search and pagination. Each repeated warning links to its retained occurrences and their original limits, peaks and timestamps. Acknowledgement applies to occurrences up to the displayed warning's ID, so a concurrent newer recurrence remains unacknowledged.

## Recorded evidence

Each episode records the server name and identity, resource identity, first abnormal sample time, last abnormal sample time, Harbour's detection time, end time/reason, limits and units at detection, initial/latest readings, and the most extreme observed values. High-limit warnings retain maxima; low-limit warnings retain minima. Disk warnings retain both peak usage and minimum free space. Status warnings such as connection, container and recorder failures have descriptions rather than invented numeric peaks.

Times identify observations, not exact physical onset or recovery. Missing sensors, failed polling and long sampling gaps do not count as recovery. Episodes flag interrupted observations. Pausing monitoring, changing limits or removing a container ends affected episodes with an explicit reason instead of labelling them recovered. Resource data older than the latest warning-configuration change is not interpreted using newer thresholds.

## Collection and durability

`warning_events.py` writes episodes in the same SQLite transaction as accepted resource readings. Local-recorder batches are evaluated in order, including spikes which have resolved before collection. The live preview does not advance warning watermarks past an unprocessed recorder backlog. Recorder acknowledgement advances only after episode and history writes commit. Per-resource watermarks reject repeated or older samples, including repeated cached disk measurements. Telegram delivery remains independent and uses fresh readings and its existing duration/repeat rules.

Events also cover Docker inventory errors, unhealthy/restarting/dead containers, SSH collection failures, recorder fallback and recorder sample loss. The dashboard's computed freshness indicator remains a live indication rather than a separately timed event.

Recording starts when this version first initializes the database. Earlier resource graphs lack the historical warning configuration and cannot reliably reconstruct episodes. A spike that was never sampled cannot be recorded. Late samples older than an already evaluated observation are ignored for warning transitions; the resource-history graph still retains accepted recorder samples. No collector upgrade is needed.

## Storage and retention

- `warning_events`: one row per episode, with at most one open episode per server/resource.
- `warning_observations`: latest evaluated sample timestamp per server/resource.
- `warning_config_epochs`: conservative boundary for applying current warning settings to recorder samples.
- `warning_acknowledgements`: acknowledgement time per user/occurrence.
- `warning_hidden`: list removal time per user/occurrence; the latest occurrence determines whether the grouped entry is hidden.

Closed episodes use the existing monitoring-history retention period (90 days by default), whether acknowledged or not. Active episodes do not expire. Hourly cleanup removes at most 500 expired episodes per sweep using an indexed query; acknowledgement and visibility rows cascade. Removing a server removes its episodes, consistent with its other monitoring history. Deleting a user removes only that user's acknowledgement and visibility records.

GET `/api/warning-events` groups warnings before filtering/pagination and supports `view=warnings|occurrences`, `warning_key`, `visibility=all|visible|hidden`, `server_id`, `status`, `acknowledgement`, `search`, `before` and `limit` (maximum 100). PUT `/api/warning-events/{id}/acknowledgement` sets or clears the current user's acknowledgement (`scope=warning` by default, or `occurrence` for individual evidence). PUT `/api/warning-events/{id}/visibility` takes `{hidden: true|false}`. POST `/api/hide-warnings` hides the current list without acknowledging it. POST `/api/dismiss-all?kind=warnings|updates` acknowledges warnings or dismisses updates separately; DELETE `/api/dismissals?kind=warnings|updates` brings back that category without changing warning acknowledgements. Omitting `kind` affects both categories for older clients. POST `/api/current-warning` handles acknowledgement/removal of live-only warnings such as freshness. All endpoints require authentication; writes also require CSRF. Dashboard counts represent distinct unacknowledged warnings, including resolved or hidden warnings that have not been acknowledged.
