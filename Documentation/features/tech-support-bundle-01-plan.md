# Tech Support Bundle Feature Plan

Date: 2026-05-19

## Goal

Add a professional appliance-style Support tab under `/settings/general` that lets an admin generate one downloadable tech support file. The file should give ZenPlus support enough evidence to diagnose update failures, UI/backend 502s, migration drift, discovery/SNMP/Windows credential issues, performance problems, storage pressure, service crashes, and future feature regressions without asking the customer to run a long command list manually.

The user flow should be one primary action:

1. Admin opens Settings -> General -> Support.
2. Admin optionally enters issue context and privacy options.
3. Admin clicks Generate support file.
4. The UI shows progress and automatically downloads the bundle when ready.

## Current Project Assessment

Relevant frontend:

- `dashboard/src/pages/GeneralSettingsPage.tsx` owns `/settings/general` tabs: Company, SMTP / Email, Appearance, Licenses, Updates, Sensors, Profile.
- `dashboard/src/App.tsx` routes `/settings/general` and redirects legacy `/settings/:tab` paths.
- `dashboard/src/components/UpdatesTabContent.tsx` already shows an appliance operations pattern: status query, action button, polling while work is active, and recent updater log display.
- `dashboard/src/lib/api.ts` uses Axios with a 30 second timeout. A full diagnostic bundle can exceed this, so generation should not be a single blocking request.
- Existing download patterns use `responseType: 'blob'` or generated object URLs.

Relevant backend:

- `server/app/api/v1/system_updates.py` already has admin-only system operations, update status, updater logs, registration, storage, and health checks.
- `server/app/core/security.py` provides `require_admin_user`; support bundle generation should be admin-only.
- `server/app/services/audit_service.py` provides best-effort audit logging and should record generation/download/delete actions.
- `server/app/main.py` registers routers directly; a new support router can be added cleanly.
- `updater/inventory.py` already gathers version, host, arch, OS, uptime, services, disk, and node count. Reuse or mirror this logic instead of duplicating all inventory collection.
- The API service runs as `zenplus`, while `zenplus-updater.service` runs as root. Full journal and system diagnostics require a root worker, not broad root privileges inside the web process.

Relevant runtime paths:

- `/opt/zenplus/.env`
- `/opt/zenplus/.version`
- `/opt/zenplus/updater/config/agent.conf`
- `/opt/zenplus/updater/config/subscription.json`
- `/opt/zenplus/updater/logs/update.log*`
- `/opt/zenplus/updater/logs/update-history.json`
- `/opt/zenplus/updater/backups/`
- `/etc/systemd/system/zenplus-*.service`
- `/etc/systemd/system/zenplus-updater.timer*`
- `/etc/nginx/conf.d/zenplus.conf`
- `/var/log/nginx/access.log*`
- `/var/log/nginx/error.log*`
- ClickHouse container: `zenplus-clickhouse`
- Host PostgreSQL database: `zenplus`
- Host Redis service: `redis-server`

Important constraints:

- Secrets must be redacted. The bundle must never include usable passwords, API keys, JWT secrets, SNMP passphrases, Windows credentials, bearer tokens, private keys, or raw database dumps.
- The API currently has narrow sudo privileges for updater actions only. The support bundle needs its own narrow privilege path.
- Bundle creation can be slow. Use a background job and polling.
- Bundles can be sensitive and large. Store them with strict permissions, retain only a small number, and expire old files.

## Product Design

### Navigation

Add a Support tab to `GeneralSettingsPage`:

- Label: `Support`
- Icon: `LifeBuoy` or `FileArchive` from `lucide-react`
- URL: `/settings/general?tab=support`
- Component: `SupportTabContent`

The existing tab pattern should be preserved. Do not create a new standalone settings page.

### Support Tab Layout

Use a restrained appliance operations layout, consistent with Updates:

- Header card: current appliance identity, version, health, last generated bundle.
- Primary action card: issue details and Generate support file button.
- Collection scope card: time range, privacy options, included sections.
- Generated bundle list: most recent bundles with status, size, checksum, created time, download/delete actions.
- Safety note: explain that credentials are redacted and operational identifiers may be included depending on selected privacy mode.

Do not use marketing copy. This is an operational tool.

### Admin Inputs

Fields:

- Issue category:
  - Update / migration
  - Device management
  - SNMP / discovery
  - Windows credentials / server monitoring
  - Alerts / notifications
  - Performance / storage
  - UI / API error
  - Other
- Short issue summary: optional text, max 500 chars.
- Time range:
  - Last 1 hour
  - Last 6 hours
  - Last 24 hours, default
  - Last 7 days
- Privacy mode:
  - Standard, default: redact secrets, keep hostnames/IPs/device names because they are useful for troubleshooting.
  - Privacy enhanced: redact secrets and anonymize hostnames, IP addresses, usernames, MAC addresses, and device names.
- Include larger diagnostics:
  - Include extended logs, default off for first release.
  - Include table row counts and schema checks, default on.

### Button States

States:

- Ready: `Generate support file`
- Queued: disabled, spinner
- Collecting: progress label from backend status
- Packaging: progress label
- Ready: automatic download, plus persistent Download button
- Failed: show failure reason and “Try again”

The UI should poll `GET /api/v1/support/bundles/{id}` every 2 seconds while status is queued/running. When status becomes `ready`, call the download endpoint with `responseType: 'blob'`.

## Backend API Design

Create a new router:

- File: `server/app/api/v1/support.py`
- Register in `server/app/main.py`
- Prefix: `/api/v1/support`
- Auth: `require_admin_user` for every endpoint.

Endpoints:

```text
POST   /api/v1/support/bundles
GET    /api/v1/support/bundles
GET    /api/v1/support/bundles/{bundle_id}
GET    /api/v1/support/bundles/{bundle_id}/download
DELETE /api/v1/support/bundles/{bundle_id}
```

Suggested response model:

```json
{
  "id": "uuid",
  "status": "queued|running|ready|failed|expired",
  "phase": "queued|inventory|logs|database|clickhouse|package|done",
  "created_at": "iso8601",
  "completed_at": "iso8601|null",
  "size_bytes": 123456,
  "sha256": "hex|null",
  "filename": "zenplus-support-<appliance>-<timestamp>.tar.gz",
  "error": ""
}
```

`POST /bundles` should:

1. Validate admin.
2. Create a job ID.
3. Write a request JSON under `/opt/zenplus/support/requests/<job_id>.json`.
4. Start a root worker through a narrow systemd/sudo path.
5. Write an audit log: `support_bundle.generate`.
6. Return `202 Accepted` with bundle ID.

`GET /download` should:

1. Verify bundle exists and status is ready.
2. Return `FileResponse`.
3. Set `Content-Disposition` with a stable filename.
4. Write audit log: `support_bundle.download`.

## Privilege Model

Use a root systemd worker rather than broad sudo from the API process.

Add:

- `support/worker.py` or `server/app/services/support_bundle_worker.py`
- `updater/systemd/zenplus-support-bundle@.service`
- Installer entries in `install.sh`
- A narrow sudoers file, for example `/etc/sudoers.d/zenplus-support`

Recommended systemd template:

```ini
[Unit]
Description=ZenPlus Support Bundle Generator %i

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/zenplus
Environment=PYTHONPATH=/opt/zenplus/server
EnvironmentFile=-/opt/zenplus/.env
ExecStart=/opt/zenplus/venv/bin/python -m app.services.support_bundle_worker --job-id %i
```

The API should only be allowed to run:

```text
/bin/systemctl start zenplus-support-bundle@*.service
```

The worker must validate that `%i` is a UUID and only read request files from `/opt/zenplus/support/requests/`.

Directory ownership:

```text
/opt/zenplus/support/requests   zenplus:zenplus  0750
/opt/zenplus/support/jobs       zenplus:zenplus  0750
/opt/zenplus/support/bundles    root:zenplus     0750
```

Bundle files:

```text
root:zenplus 0640
```

## Bundle Contents

Archive format:

```text
zenplus-support-<appliance-id-or-host>-<UTC timestamp>.tar.gz
```

Top-level structure:

```text
manifest.json
README.txt
redaction-report.json
checksums.sha256
inventory/
health/
logs/
database/
clickhouse/
config/
network/
storage/
updates/
features/
```

### manifest.json

Include:

- bundle ID
- creation time
- appliance ID, redacted or hashed in privacy enhanced mode
- hostname, redacted or hashed in privacy enhanced mode
- ZenPlus version and installed timestamp
- git commit if available
- OS version, kernel, architecture
- uptime
- selected issue category and user summary
- privacy mode
- collection time range
- bundle schema version, starting with `1`

### inventory/

Files:

- `system.json`: hostname, OS, kernel, uptime, CPU count, memory, time zone, current time.
- `services.json`: status of `zenplus-api`, `zenplus-poller`, `zenplus-updater.service`, `zenplus-updater.timer`, `zenplus-wait-deps`, `nginx`, `postgresql`, `redis-server`, `docker`.
- `docker.json`: `docker ps`, ClickHouse container status, container image, restart count, health.
- `versions.json`: Python, Node, npm, Go, Docker, PostgreSQL, Redis, ClickHouse, nginx versions.
- `limits.json`: file descriptor limit, process limit, disk inode usage, memory summary.

### health/

Files:

- `system-health.json`: same checks as `/api/v1/system/health`, plus `snmp_encryption`.
- `api-health.txt`: HTTP status/body from `http://127.0.0.1:8000/api/v1/system/health`.
- `nginx-health.txt`: HTTP status/body from `http://127.0.0.1/api/v1/system/health`.
- `dependency-health.json`: PostgreSQL, Redis, ClickHouse connectivity checks.
- `known-risk-checks.json`: targeted checks for migration drift, missing columns, missing `SNMP_ENC_KEY`, invalid sudoers, updater registration, ClickHouse reachability, storage thresholds.

### logs/

Collect bounded logs, not unlimited history.

Systemd journals:

- `journal-zenplus-api.log`
- `journal-zenplus-poller.log`
- `journal-zenplus-updater.log`
- `journal-zenplus-wait-deps.log`
- `journal-nginx.log`
- `journal-postgresql.log`
- `journal-redis-server.log`
- `journal-docker.log`
- `journal-kernel.log`, warning/error priority only

Suggested command pattern:

```bash
journalctl -u <unit> --since "<range>" --no-pager -o short-iso
```

App logs:

- `updater-update.log`
- `updater-update-history.json`
- `nginx-error.log`
- `nginx-access-tail.log`, last N lines only

Use maximum sizes per file. Suggested first-release limits:

- 5 MB per log file after redaction.
- 50 MB max final bundle.
- If truncated, append a clear truncation marker.

### database/

Never include a database dump.

Include PostgreSQL diagnostics:

- `postgres-version.txt`
- `postgres-size.json`
- `schema-migrations.json`: filename, checksum, applied_at, duration.
- `migration-status.txt`: output of `run-migrations.py --status` if safe and available.
- `critical-schema-checks.json`: existence/type checks for tables and columns used by current features.
- `row-counts.json`: counts for key tables only.
- `recent-audit-actions.json`: last 100 audit log rows with actor username and resource metadata redacted as needed.
- `recent-update-history.json`
- `pg-stat-activity-redacted.json`
- `pg-locks.json`
- `extensions.json`

Key tables to count/check:

- `users`
- `devices`
- `device_groups`
- `snmp_credentials`
- `service_checks`
- `alert_rules`
- `alerts`
- `audit_logs`
- `subscriptions`
- `discovery_profiles`
- `discovery_runs`
- `discovery_results`
- `windows_credentials`
- `servers`
- `agents`
- `sensors`
- `sensor_assignments`
- `notification_channels`
- `notification_gateways`

Critical schema checks should cover features that have recently caused support issues:

- `devices.snmp_credential_id`
- `device_groups.snmp_credential_id`
- `snmp_credentials`
- `windows_credentials`
- `discovery_profiles.snmp_credential_ids`
- `discovery_profiles.windows_credential_ids`
- `discovery_results.credential_used`
- `discovery_results.windows_credential_used`
- `schema_migrations`

### clickhouse/

Include:

- `clickhouse-version.txt`
- `clickhouse-ping.txt`
- `clickhouse-tables.json`
- `clickhouse-table-sizes.json`
- `clickhouse-row-counts.json`
- `clickhouse-mutations.json`
- `clickhouse-errors.log`, recent server log if accessible through Docker logs.

Key tables:

- `ping_metrics`
- `ping_metrics_5m`
- `ping_metrics_1h`
- `service_metrics`
- `service_metrics_5m`
- `service_status_log`
- `snmp_metrics`
- `snmp_if_metrics`
- `snmp_traps`
- NetFlow tables
- Server monitoring host metric tables

### config/

Include redacted copies:

- `.env.redacted`
- `agent.conf.redacted`
- `subscription.json.redacted`
- `docker-compose.yml.redacted`
- `nginx-zenplus.conf.redacted`
- systemd unit files for ZenPlus services
- sudoers presence/status, not full arbitrary sudoers.
- `migrations.lock` if present.

### network/

Include:

- listening sockets: `ss -ltnp`
- routing table: `ip route`
- interface summary: `ip -br addr`
- DNS resolver config: `/etc/resolv.conf`, redacted if needed.
- firewall status: `ufw status verbose` if available.

Privacy enhanced mode should anonymize IPs and MACs.

### storage/

Include:

- `df -h`
- `df -ih`
- `lsblk -f`
- mount table
- `/data` mount status
- ClickHouse data path check
- LVM summary if commands exist: `pvs`, `vgs`, `lvs`
- largest ZenPlus directories with depth limits, excluding full file listings of sensitive areas.

### updates/

Include:

- updater config redacted
- updater timer status
- updater service status
- update history
- current `.version`
- recent release/update errors
- recent migration runner output if available
- backup directory summary, names/sizes only

### features/

Include targeted health summaries:

- `snmp.json`: credential table exists, number of credentials, number of devices linked to credentials, SNMP encryption key configured, no secrets.
- `discovery.json`: profile/run/result counts, most recent failed discovery runs, credential failure counts.
- `windows-credentials.json`: table exists, count of credentials, recent use counts, no secrets.
- `server-monitoring.json`: servers/agents/policies counts, recent agent heartbeats, package status.
- `sensors.json`: sensor/site counts, recent heartbeat ages, assignment counts.
- `notifications.json`: channel/gateway counts and enabled flags, no passwords/tokens.
- `netflow.json`: table presence, recent row counts, exporter count.

## Redaction Policy

Redaction must happen before data is written into the final tarball when practical. Also run a final redaction pass over text/JSON files before packaging.

Always redact:

- `POSTGRES_PASSWORD`
- `CLICKHOUSE_PASSWORD`
- `REDIS_PASSWORD`
- `JWT_SECRET`
- `SNMP_ENC_KEY`
- `DATABASE_URL` password segment
- `REDIS_URL` password segment
- updater `api_key`
- license keys and registration tokens
- API keys
- bearer tokens
- private keys
- SMTP passwords
- SMS provider tokens
- SNMP communities
- SNMPv3 auth/priv passphrases
- Windows passwords
- SSH/private credentials
- sensor API keys
- agent enrollment/install tokens

Redaction marker:

```text
[REDACTED:<kind>]
```

Privacy enhanced mode additionally anonymizes:

- hostnames
- usernames
- email addresses
- IP addresses
- MAC addresses
- device names
- site names

Anonymization should be stable within a bundle:

```text
host-0001
user-0001
10.x.x.1
device-0001
```

Generate `redaction-report.json` with counts by redaction type, not original values.

## Failure Handling

The bundle worker should be best-effort:

- One failing collector must not fail the whole bundle.
- Each section should record `status: ok|warning|failed`.
- Command timeouts should be short and explicit.
- Failed commands should write stderr into that section's metadata after redaction.
- If final package exceeds max size, truncate large log files and record truncation.

Job states:

- `queued`
- `running`
- `ready`
- `failed`
- `expired`

Worker writes:

- `/opt/zenplus/support/jobs/<id>.json`
- `/opt/zenplus/support/bundles/<id>.tar.gz`

## Implementation Plan

### Phase 1: Backend job model

Add:

- `server/app/api/v1/support.py`
- `server/app/services/support_bundle.py`
- `server/app/services/support_redaction.py`
- `server/app/services/support_collectors/`

Collectors:

- `inventory.py`
- `logs.py`
- `database.py`
- `clickhouse.py`
- `config.py`
- `network.py`
- `storage.py`
- `features.py`

Add router registration to `server/app/main.py`.

Add audit actions:

- `support_bundle.generate`
- `support_bundle.download`
- `support_bundle.delete`

### Phase 2: Root worker and installer

Add:

- root systemd template `zenplus-support-bundle@.service`
- installer setup for support directories, unit, and sudoers
- cleanup policy: keep last 5 bundles or 7 days, whichever is smaller

The API starts the worker with a validated UUID job ID.

### Phase 3: Frontend Support tab

Add:

- `dashboard/src/components/SupportTabContent.tsx`
- TABS entry in `GeneralSettingsPage.tsx`
- Types for bundle status/request

UI behavior:

- POST job on generate
- Poll status
- Auto-download when ready
- Display ready bundle list
- Delete old bundles
- Show clear privacy/inclusion summary

### Phase 4: Tests

Backend unit tests:

- redaction regexes and URL redaction
- anonymization stability
- bundle manifest shape
- collector failure isolation
- job ID validation
- download blocked unless ready
- admin-only access

Backend integration tests:

- generate minimal bundle with mocked collectors
- status transitions
- audit log writes
- bundle file cleanup

Frontend tests:

- Support tab renders
- generate button posts expected payload
- polling transitions to download
- failed job displays error
- privacy mode toggles payload

Installer checks:

- `bash -n install.sh`
- sudoers validation
- systemd unit installed

Manual QA:

- Healthy appliance bundle
- Broken PostgreSQL connection
- Missing `SNMP_ENC_KEY`
- Failed update in history
- Missing migration column
- ClickHouse container down
- Nginx 502
- Large logs truncation
- Privacy enhanced anonymization

### Phase 5: Future Enhancements

Not required for first implementation:

- Direct upload to zentryc.com support case.
- Case number entry and remote support token.
- Signed support bundles.
- Customer-visible bundle preview before download.
- CLI command: `sudo zenplus support-bundle`.
- Sensor appliance support bundles.
- Automated diagnosis report with probable causes.

## Acceptance Criteria

Feature acceptance:

- Admin can generate a support bundle from `/settings/general?tab=support`.
- The UI requires one click to start generation and automatically downloads when ready.
- Bundle generation works even if one collector fails.
- Final archive contains manifest, checksums, redaction report, logs, config summaries, DB diagnostics, ClickHouse diagnostics, and feature-specific health summaries.
- Bundle does not contain known secret patterns.
- Bundle includes enough data to diagnose the recent observed classes of issues:
  - migration checksum drift
  - missing migration columns/tables
  - missing `SNMP_ENC_KEY`
  - API 502/backend exceptions
  - updater failure
  - ClickHouse reachability
  - storage pressure
  - SNMP/discovery/Windows credential failures
- Non-admin users cannot generate or download bundles.
- Bundles are retained with bounded count/age and strict permissions.

## Recommended First Implementation Scope

Build the first release with:

- Support tab UI.
- Async job API.
- Root systemd worker.
- Redaction engine.
- Core collectors: manifest, health, journals, updater logs/history, redacted config, PostgreSQL schema checks/counts, ClickHouse table status, service statuses, storage summary.
- Feature collectors for SNMP credentials, discovery v2, Windows credentials, update/migration status.

Leave direct cloud upload and signed bundles for a later release.
