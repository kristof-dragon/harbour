# Warning history

Harbour stores a warning episode when background monitoring first observes a warning. Further abnormal readings update that episode's last observation and extreme values. A confirmed normal reading closes it as resolved. A later recurrence creates a new episode.

Notifications show unacknowledged episodes, including resolved episodes. Acknowledgement belongs to the signed-in user and never deletes an episode or clears an active server warning indicator. The History tab includes acknowledged episodes, server/status/acknowledgement filters, text search and cursor pagination. Opening a warning does not automatically acknowledge it.

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
- `warning_acknowledgements`: acknowledgement time per user/event.

Closed episodes use the existing monitoring-history retention period (90 days by default), whether acknowledged or not. Active episodes do not expire. Hourly cleanup removes at most 500 expired episodes per sweep using an indexed query; acknowledgement rows cascade. Removing a server removes its episodes, consistent with its other monitoring history. Deleting a user removes only that user's acknowledgements.

GET `/api/warning-events` supports `server_id`, `status`, `acknowledgement`, `search`, `before` and `limit` (maximum 100). PUT `/api/warning-events/{id}/acknowledgement` sets or clears the current user's acknowledgement. Both require authentication; writes also require CSRF. Dashboard summary counts include unacknowledged resolved episodes. Existing bulk dismissal acknowledges stored warning events and dismisses image updates; restoring dismissals restores the current user's acknowledgements too.
