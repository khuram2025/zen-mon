Sensors / Prober — remote probing VMs
Design for the sensor fleet: lightweight probing VMs deployed per site that pull their monitoring configuration from the central controller, run availability checks against local targets, and stream results back over a single outbound HTTPS connection. Builds on the v0.1 skeleton already in the repo.

Design summary. A sensor is a locked-down probe, deliberately not a second controller: one Go binary that enrolls once with a one-time token, then lives on two outbound loops — a 30-second heartbeat that piggybacks the config ETag, and a result uploader draining a disk-backed buffer. The controller owns everything: sensor identity, assignments (device / group / service-check), config versioning, health sweeping, and alerting. The two structural decisions this design fixes in place: devices have exactly one polling owner (an assigned sensor, else the central poller — never both), while service checks are multi-vantage (N sensors can probe the same URL, with per-vantage state and a k-of-n consensus rule deciding the aggregate). Everything else — lifecycle states, backfill contract, buffer spec, UI — follows from those two rules.

Baseline

What already exists (v0.1) and what's wrong with it
Roughly half the feature is built. The design below keeps all of it and treats the following as the authoritative inventory:

Piece	State	Where
Schema: sites, sensors, assignments	HAVE	migrate-008-sensors.sql — plus default_sensor_id on devices and service_checks
Admin API	HAVE	server/app/api/v1/sensors.py — CRUD, enrollment tokens, key rotation, enable/disable, assignments, OVA/ISO bootstrap downloads
Agent API	HAVE	server/app/api/v1/sensor_api.py — enroll, heartbeat, ETag'd config, ping/service result ingest with per-sensor scoping, binary/appliance manifests, install.sh
Go sensor binary	HAVE	poller/cmd/sensor — enroll, config pull, checkers (ICMP/TCP/HTTP/DNS/TLS/SNMP/NetPath), idempotency-keyed batches, heartbeat
OVA appliance build	HAVE	sensor-appliance/ — packer, firstboot, systemd, cloud-init with static-IP support
UI	HAVE	SensorsCard.tsx in Settings → Sensors: list, add wizard, token display, downloads, delete
Double polling	BUG	The central poller's device query (poller/internal/store/postgres.go:52) selects every ping_enabled device — sensor-assigned devices get polled twice, from two networks, writing conflicting rows and flapping devices.status
Stale-status overwrite	BUG	Result ingest updates devices.status / service_checks.status from every batch — a drained offline backlog overwrites the live status with hours-old data
Offline detection	GAP	Heartbeat sets status='online'; nothing ever transitions a silent sensor to degraded/offline
Durable buffering	GAP	Sensor queue is RAM-only; results are lost on restart or overflow
Multi-vantage semantics	GAP	service_checks.status is last-writer-wins when several sensors probe the same check
Alerting integration	GAP	No sensor-offline alert, no suppression of device storms behind a dead sensor
SNMP + events ingest	GAP	Both endpoints are acknowledge-and-log stubs
Sensor detail UI	GAP	No assignments editor, per-vantage views, or health drill-down
Architecture

Roles, principles, and the wire protocol
The sensor is a probe, not a controller. No local database of record, no local UI (until the HA plan's edge-autonomy phase), no inbound connections. If a sensor VM is lost, redeploying the OVA and re-enrolling recreates it fully from central state.
Outbound-only, one port. Everything rides HTTPS 443 to the controller: NAT-safe, one firewall rule per site. The controller never dials the sensor; anything it wants the sensor to do travels as a command in the heartbeat response (has_commands is already in the protocol, currently always false).
Two planes. Control plane: /enroll, /heartbeat, /config. Data plane: /results/*, /events. The split matters operationally — the heartbeat must stay tiny and always succeed even when the data plane is backed up, because it is what health sweeping keys on.
Sensor VM (Go binary, systemd)
config cache
disk · ETag · last-known
scheduler
per-check jitter
checkers
ICMP · TCP · HTTP · DNS · TLS · SNMP · NetPath
disk WAL buffer
size + age bounded
oldest-first eviction
uplink client
batches · idempotency
backoff · drain limit
future (P4): trap/syslog receiver · edge rules
from the HA plan
results
Central controller
control plane
POST /enroll
POST /heartbeat
GET /config (ETag)
data plane
POST /results/ping
POST /results/service
POST /events
Postgres
sensors · assignments
live status (fresh only)
ClickHouse
ping_metrics
service_metrics + poller_id
health sweep
online→degraded→offline
alert engine
k-of-n · suppression
heartbeat 30 s → config_etag, commands
timestamped batches, ack ⇒ delete from WAL
all outbound HTTPS 443 — controller never dials in
no inbound path (commands ride heartbeat replies)
The sensor's two loops: the control loop (heartbeat every 30 s, config re-pull when the returned ETag changes) and the data loop (checkers append to a disk WAL; the uplink drains it in acknowledged batches). On the controller, ingest fans out to ClickHouse (with poller_id identifying the vantage), fresh-only status updates in Postgres, and the health sweep that feeds the alert engine.
Protocol contract (v1)
Call	Cadence	Contract
POST /sensor/enroll	once	One-time token (24 h TTL, hashed at rest) → sensor_id + API key. Token is consumed on success; re-enrollment requires an admin-issued token.
POST /sensor/heartbeat	30 s	Sends version, uptime, queue depth/drops, hostname. Returns config_etag and a commands list (v1 verbs: update, flush_buffer, reload_config, set_log_level). Must never block on the data plane.
GET /sensor/config	on ETag change + 10 min fallback	If-None-Match → 304 when unchanged. Payload: assigned devices (ping/SNMP params) + service checks (full check params). Cached to disk; the sensor keeps probing on last-known config through any central outage.
POST /sensor/results/*	1–5 s drain	Batches carry a UUID idempotency key and a per-sample collection timestamp. Server ack ⇒ sensor deletes the batch from the WAL. Late samples accepted within the backfill window (72 h); older are dropped with a counted metric.
POST /sensor/events	as they occur	Sensor lifecycle events (started, config applied, buffer overflow, check-engine errors) → central event log, visible on the sensor detail page.
Versioning: the sensor sends its version in every heartbeat; the server replies with min_supported_version. An outdated sensor gets an update command pointing at the existing /sensor/bin/<platform>/manifest.json, verifies the SHA-256, swaps the binary, and restarts under systemd — same signed-release flow the appliance updater already uses.

Core decision

Polling ownership
The one rule that prevents data corruption and status flapping:

Target	Rule	Why
Device (ping / SNMP)	Exactly one owner. The assigned sensor if any (explicit assignment > group assignment > default_sensor_id); otherwise the central poller. The central poller's device query must exclude sensor-owned devices — today it doesn't (postgres.go:52), which is the double-polling bug.	A device has one home network; probing it from elsewhere measures the WAN, not the device. Two writers to devices.status guarantees flapping.
Service check (HTTP / TCP / DNS / TLS)	Multi-vantage. Any number of sensors (plus optionally the central poller as the "HQ vantage") can run the same check. Every result row already carries poller_id; aggregate status is computed by a consensus rule, never written last-writer-wins.	"Is the app up" legitimately has one answer per location — that's the entire point of multi-site probing, and the false-positive killer (alert only when k of n vantages agree).
Conflict resolution when a device matches several sensors (e.g. explicit on sensor A, group on sensor B): highest-precedence source wins; ties broken by the existing priority column, lowest value first. The config endpoint and the central poller exclusion must use the same resolution function so a device is never claimed by two pollers or by none — implement it once as a SQL view (device_polling_owner) both sides query.

Per-vantage state and k-of-n consensus
New table service_check_vantage_status (one row per check × vantage: last state, last change, last latency, updated by ingest). The alert engine evaluates a per-check consensus policy:

mode: any (default for single-vantage checks — today's behavior, unchanged),
mode: majority — down when > half of reporting vantages agree,
mode: threshold, k: N — ThousandEyes-style explicit count.
Vantages whose sensor is offline drop out of the denominator. A single failing vantage under majority/threshold produces a low-severity "degraded from Site X" event instead of an outage page. service_checks.status becomes the computed aggregate; ingest stops writing it directly.

Health model

Sensor lifecycle
pending
token issued
online
heartbeat fresh
degraded
late / dropping
offline
silent ≥ 3 min
disabled
enroll + 1st hb
2 missed hb, or drops
heartbeat ok
silence ≥ 3 min
heartbeat resumes (⇒ backfill drain)
admin enable / disable
States the health sweep maintains from last_heartbeat_at (existing column). With a 30 s heartbeat: degraded after 2 missed beats (~60–90 s) or sustained queue drops, offline after 3 minutes of silence. Only admin actions enter or leave disabled. All transitions write the sensor event log.
The sweep is a new pass in the existing health/stale sweeper cadence: no new daemon. It flips states, records events, and raises/clears the sensor-offline alert.
Suppression: while a sensor is offline, device-down alerts for its exclusively-owned targets are suppressed and those devices render as stale (grey, "last seen via Site X sensor, 4 m ago") — one actionable alert instead of a 50-device storm. This mirrors the alert engine's existing pending/cooldown machinery.
Degraded reasons are surfaced, not just the state: late heartbeats, queue_dropped_count increasing (buffer overflow — results being lost), or version below min_supported_version.
Backfill without lying about the present
Result ingest applies two clocks to every sample: the collection timestamp decides where the row lands in ClickHouse (always honored within the 72 h window — graphs and SLO math heal after an outage), and sample freshness decides whether live status moves. Rule: a sample may update devices.status / vantage status only if its timestamp is within 2× the check interval of now. This fixes the current bug where draining an offline backlog rewrites present-tense status from hours-old data, and it needs no protocol change — batches are already timestamped per sample.

On the sensor side, the WAL buffer spec: append-only segment files under /var/lib/zenplus-sensor/wal, default caps 512 MB and 72 h (matching the ingest window), oldest-segment eviction with a counted drop metric, drain rate-limited to a few batches per second after reconnect so a long outage doesn't stampede the controller — the replay-storm lesson from Icinga2.

Persistence

Schema changes (migrate-105)
sensors: add heartbeat_interval_s INT DEFAULT 30, degraded_after_s INT DEFAULT 90, offline_after_s INT DEFAULT 180, status_reason TEXT, min_supported_version VARCHAR (global setting mirrored for display).
service_check_vantage_status: (service_check_id, poller_id) PK, state, last_change_at, last_latency_ms, last_error, updated_at.
service_checks: add consensus_mode VARCHAR DEFAULT 'any', consensus_k INT.
sensor_events: (id, sensor_id, ts, kind, detail JSONB) — lifecycle + ingest anomalies, retained 30 days.
View device_polling_owner(device_id, owner_kind, sensor_id) — the single resolution function used by both the sensor config endpoint and the central poller's exclusion clause.
Alert rules: new rule kinds sensor_offline and sensor_degraded with per-kind default message templates, following the existing device/service/trap template pattern.
No ClickHouse schema change needed — ping_metrics and service_metrics already carry poller_id; the by-location views filter on it.

Experience

UI design
Settings → Sensors (exists, stays): the fleet table gains status-reason tooltips, queue-depth and drop sparkline columns, version-drift badges, and a site filter. The add wizard is already good (token, OVA/ISO, install.sh, static-IP bootstrap).
Sensor detail (new page): tabs — Overview (state timeline from sensor_events, heartbeat freshness, buffer depth/drops, version, resource info), Assignments (device/group/service-check picker writing the existing PUT /assignments, with an ownership preview showing what the central poller will stop polling), Deployment (downloads + rotate/regenerate, exists as modal today), Events.
Device pages: owned devices show a "polled from <Site>" chip; stale-while-sensor-offline rendering.
Service check detail: a by-location section — per-vantage status pills and a latency-comparison chart drawn from service_metrics grouped by poller_id, plus the consensus-policy editor (any / majority / k-of-n).
Dashboards: the network team page gets a sensors health tile (n online / degraded / offline) wired to the same client-side alert filtering the team dashboards already use.
Security

Security posture
Mostly inherited from v0.1, which got this right: one-time enrollment tokens (hashed, TTL, consumed-once with IP recorded), long-lived API keys stored hash-only with a display prefix, per-sensor authorization scoping on every result batch (a sensor can only post for targets it is assigned — already returns 403 otherwise). Additions: per-sensor rate limits on the data plane; reject result batches whose timestamps are in the future by more than clock-skew tolerance (60 s); audit entries for token regeneration and key rotation; and the enrollment response pins the controller's CA fingerprint so a re-deployed sensor refuses a MITM'd controller.

Delivery

Build order
P1
Correctness — make what exists safe
small · highest value
Fixes the two live bugs; no new surface area.

device_polling_owner view + exclusion clause in the central poller's device query.
Freshness guard on status updates in sensor_api.py ingest (backfill lands in ClickHouse only).
Health sweep pass: online → degraded → offline transitions + sensor_events.
sensor_offline alert kind + suppression of owned-device alerts while offline.
P2
Durability — survive restarts and WAN cuts
Go work in the sensor
Disk WAL buffer (segments, 512 MB / 72 h caps, eviction metric, rate-limited drain).
72 h backfill window + future-timestamp rejection on ingest.
Heartbeat commands: update, flush_buffer, reload_config, set_log_level; binary self-update via the existing manifest endpoints.
P3
Multi-vantage — the differentiator
alert engine + UI
service_check_vantage_status + consensus evaluation (any / majority / k-of-n); aggregate status becomes computed.
"Degraded from Site X" event severity; offline vantages leave the denominator.
Sensor detail page, assignments editor with ownership preview, by-location latency view.
P4
Site services — ties into the HA plan
later
Real SNMP ingest path (replace the stub) so sensors fully own remote-site devices.
Trap/syslog receiver on the sensor with buffered forwarding.
Edge minimal rule set + local notification channel (HA plan, Layer 3).