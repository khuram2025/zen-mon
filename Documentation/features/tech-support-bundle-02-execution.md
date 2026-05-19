# Tech Support Bundle — Execution Plan

Date: 2026-05-19
Status: Approved, Phase 1 in progress
Companion doc: [tech-support-bundle-01-plan.md](tech-support-bundle-01-plan.md) (earlier design draft)

## Context

ZenPlus runs as a sealed appliance at remote customer sites. Recent live incidents have shown that diagnosing customer-reported problems requires data scattered across many places no support engineer should have to walk a customer through by hand:

- **Migration drift** (today): one appliance hit `migrate-004/006 checksum differs from applied record` mid-OTA. We needed `schema_migrations` rows, the on-disk SQL hashes, and the updater log to reason about it.
- **502 on add-device / assign-credential** (today, second appliance): could be missing migration column, hung Postgres lock, OOM-killed API worker, or full disk — diagnosable from `journalctl` + `pg_stat_activity` + `dmesg` + `df`, all of which take five round-trip emails to get.
- Future feature regressions across SNMP discovery, Windows credentials, server monitoring agents, NetFlow, sensors, notifications — each has its own failure modes and dedicated tables/services.

**Goal:** one button under Settings → General → Support that produces a single tarball containing everything we'd ask for in those five emails, with secrets redacted. Admin clicks, browser downloads, customer attaches to a ticket. This is the same "tech support bundle" pattern Cisco / Palo Alto / F5 appliances ship with — table stakes for an appliance product.

User-confirmed scope guardrails for v1:

- Privilege escalation via narrow systemd-template + sudoers, not broad root in the API.
- "Include extended logs" defaults OFF (5 MB/file, 50 MB/bundle).
- Privacy-enhanced anonymization (host-0001 mapping) is deferred to v2.
- Direct cloud upload to zentryc.com is included as a stretch goal (Phase 3, see below); v1 ships with download-only and a hook the cloud-upload code can plug into.

## UX flow

```
Settings → General → [Support] tab
┌──────────────────────────────────────────────────────────┐
│ Tech Support Bundle                                       │
│ Generate a diagnostic archive containing logs, config,    │
│ and health data. Secrets are redacted before packaging.   │
├──────────────────────────────────────────────────────────┤
│ Issue category   [SNMP/discovery ▾]                       │
│ Short summary    [_____________________________________]  │
│ Time range       [Last 24 hours ▾]   (1h / 6h / 24h / 7d) │
│ ☐ Include extended logs (larger bundle, off by default)   │
│                                                           │
│            [ Generate support file ]                      │
├──────────────────────────────────────────────────────────┤
│ Recent bundles                                            │
│   2026-05-19 14:32  zenplus-support-…-1432.tar.gz  4.2MB  │
│                     [Download] [Delete]                   │
└──────────────────────────────────────────────────────────┘
```

While generating, the button becomes a progress label fed by the backend (`queued → inventory → logs → database → clickhouse → package → done`). On `done`, the browser auto-downloads via the standard blob-anchor-click pattern already used by `dashboard/src/components/reports/ExportMenu.tsx:31-58`. The bundle also stays in the "Recent bundles" list so the user can re-download or delete.

## Architecture

Three tiers, mirroring the existing OTA updater split (which already solved the same "API can't be root" problem):

```
┌── Browser (admin) ────────────────────────────────────────┐
│ SupportTabContent.tsx — React + TanStack Query + Axios    │
└───────────┬───────────────────────────────────────────────┘
            │ /api/v1/support/bundles  (admin-only)
┌───────────▼───────────────────────────────────────────────┐
│ server/app/api/v1/support.py — FastAPI router             │
│   • POST   /bundles            → enqueue + 202            │
│   • GET    /bundles            → list                     │
│   • GET    /bundles/{id}       → status                   │
│   • GET    /bundles/{id}/download                         │
│   • DELETE /bundles/{id}                                  │
└───────────┬───────────────────────────────────────────────┘
            │ writes request file, then:
            │ sudo -n /bin/systemctl --no-block start
            │   zenplus-support-bundle@<uuid>.service
┌───────────▼───────────────────────────────────────────────┐
│ /etc/systemd/system/zenplus-support-bundle@.service       │
│ Type=oneshot, User=root, runs:                            │
│   /opt/zenplus/venv/bin/python -m support --job %i        │
└───────────┬───────────────────────────────────────────────┘
            │ runs collectors as root (no FastAPI/SQLAlchemy deps)
            ▼
   /opt/zenplus/support/
     ├── requests/<uuid>.json   (mode 640, zenplus:zenplus)
     ├── jobs/<uuid>.json       (status; rewritten as phases progress)
     └── bundles/<uuid>.tar.gz  (mode 640, root:zenplus, API reads)
```

**Why a separate top-level `support/` Python module (not under `server/app/services/`):** the worker runs as root, outside the API process, and shouldn't drag in the entire FastAPI/SQLAlchemy import tree. It uses `asyncpg` directly (same pattern as `updater/inventory.py:117-140`) and `subprocess` for shell commands. This is exactly how `updater/` is organized and shipped today.

**Privilege grant:** copy the existing dual mechanism — polkit rule for the `zenplus` user (`updater/polkit/50-zenplus-updater.rules:1-9`) plus a sudoers entry (referenced at `install.sh:435` and used at `server/app/api/v1/system_updates.py:368,457` as `sudo -n /bin/systemctl ...`). The sudoers grant must allow only:

```
zenplus ALL=(root) NOPASSWD: /bin/systemctl start zenplus-support-bundle@*.service
zenplus ALL=(root) NOPASSWD: /bin/systemctl --no-block start zenplus-support-bundle@*.service
```

The `*.service` wildcard is safe because systemd validates the instance name and the unit file itself only invokes our worker. The worker additionally validates `%i` is a UUID and that `requests/<uuid>.json` exists with the expected owner/mode before doing anything else.

## Files to add / modify

### Backend (new)

- `server/app/api/v1/support.py` — router (~250 lines). Pattern lifted from `server/app/api/v1/system_updates.py`. Uses `require_admin_user` (from `server/app/core/security.py:76`) on every route. Writes audit rows via `write_audit_log()` (`server/app/services/audit_service.py:12`) for `support_bundle.generate`, `support_bundle.download`, `support_bundle.delete`.
- `server/app/services/support_jobs.py` — small helper module the router uses to spawn the systemd unit, read job status from disk, and stream the bundle file. No collector logic here.

### Worker module (new top-level, ships to `/opt/zenplus/support/`)

- `support/__init__.py`
- `support/__main__.py` — entry point: validates UUID, loads request, runs collectors, writes status updates, packages tarball.
- `support/manifest.py` — manifest.json schema (`bundle_schema_version: 1`).
- `support/redaction.py` — regex-based secret scrubber. Always-on patterns: `POSTGRES_PASSWORD`, `CLICKHOUSE_PASSWORD`, `REDIS_PASSWORD`, `JWT_SECRET`, `SNMP_ENC_KEY`, password segment of `DATABASE_URL` / `REDIS_URL`, updater `api_key`, bearer tokens, SMTP passwords, SNMP communities, SNMPv3 passphrases, Windows passwords, SSH keys, sensor/agent API keys, license keys, registration tokens. Writes `redaction-report.json` with counts (never original values).
- `support/archiver.py` — tarball builder with per-file and total size caps (5 MB / 50 MB). Records truncations.
- `support/collectors/__init__.py` — registry. Each collector returns `{status: ok|warning|failed, files: {...}, notes: [...]}` so a single broken collector never fails the bundle (mirrors `inventory.py` resilience).
- `support/collectors/inventory.py` — reuses `updater/inventory.py:collect_inventory()` plus extended versions (`python -V`, `node -v`, `go version`, `docker -v`, `psql --version`, `clickhouse-client --version`, `nginx -v`).
- `support/collectors/health.py` — internal HTTP GETs against `/api/v1/system/health`, `/system/storage`, `/system/update-status`, `/system/registration`. Also direct dep checks: `pg_isready`, `redis-cli ping`, `clickhouse-client --query "SELECT 1"`. The `known-risk-checks.json` runs the specific schema probes that would have caught today's two incidents: existence of `devices.snmp_credential_id`, `snmp_credentials`, `windows_credentials`, `audit_logs`; `SNMP_ENC_KEY` set; updater registered; `migrations.lock` matches on-disk hashes.
- `support/collectors/logs.py` — bounded `journalctl -u <unit> --since "<range>" --no-pager` for: `zenplus-api`, `zenplus-poller`, `zenplus-updater`, `zenplus-wait-deps`, `nginx`, `postgresql`, `redis-server`, `docker`; plus `--priority warning` kernel log. App logs: `/opt/zenplus/updater/logs/update.log*`, `/var/log/nginx/error.log*` (tailed), `update-history.json`.
- `support/collectors/database.py` — never dumps data. Captures: `SELECT version()`, `pg_database_size`, full `schema_migrations` table, `run-migrations.py --status` output (catches migration drift directly — see `scripts/run-migrations.py:144`), critical schema-presence checks, row counts for ~20 known tables, last 100 `audit_logs` (with metadata redacted), redacted `pg_stat_activity`, `pg_locks` for `devices`/`snmp_credentials`/`audit_logs`, installed extensions.
- `support/collectors/clickhouse.py` — `clickhouse-client` via `docker exec zenplus-clickhouse`: `SELECT version()`, `SELECT 1`, `system.tables` for `zenplus` db, `system.parts` sizes, `system.mutations` errors, `system.disks` free space, last 100 lines from `docker logs zenplus-clickhouse` (redacted).
- `support/collectors/config_files.py` — redacted copies of `/opt/zenplus/.env`, `/opt/zenplus/updater/config/agent.conf`, `subscription.json`, `docker-compose.yml`, `/etc/nginx/conf.d/zenplus.conf`, the zenplus-*.service / *.timer unit files, `scripts/migrations.lock`. Original files never enter the archive — every value passes through `redaction.py` first.
- `support/collectors/network.py` — `ss -ltnp`, `ip route`, `ip -br addr`, `/etc/resolv.conf`, `ufw status verbose` (if installed). No firewall *rules*, just status.
- `support/collectors/storage.py` — `df -h`, `df -ih`, `lsblk -f`, `findmnt`, `pvs`/`vgs`/`lvs` if present, `du -sh /data/clickhouse /opt/zenplus/updater/backups /opt/zenplus/support/bundles`.
- `support/collectors/updates.py` — `.version`, `update-history.json`, `systemctl status zenplus-updater.timer/.service`, list of backups in `/opt/zenplus/updater/backups/` (names + sizes only). This is the collector that would have made today's migration-drift incident a 30-second triage.
- `support/collectors/features.py` — small JSON summaries per feature so support can spot a missing column or empty table at a glance: `snmp.json`, `discovery.json`, `windows-credentials.json`, `server-monitoring.json`, `sensors.json`, `notifications.json`, `netflow.json`. Each does `EXISTS / COUNT(*) / MAX(updated_at)` queries — no PII, no secrets.

### Systemd / installer (new)

- `updater/systemd/zenplus-support-bundle@.service` — `Type=oneshot`, `User=root`, `ExecStart=/opt/zenplus/venv/bin/python -m support --job %i`, `TimeoutStartSec=600`, journal logging.
- `updater/systemd/zenplus-support-cleanup.service` + `.timer` — daily timer that calls `support.cleanup` to keep last 5 bundles or 7 days, whichever is smaller. Strict 0640 root:zenplus on bundles, 0750 on dirs.
- `scripts/setup-support.sh` — mirror of `setup-updater.sh`. Creates `/opt/zenplus/support/{requests,jobs,bundles}`, installs the systemd template + cleanup timer, writes `/etc/sudoers.d/zenplus-support` with the narrow `systemctl start zenplus-support-bundle@*.service` grant, optionally a polkit rule. Runs `visudo -c` before placing the sudoers file.
- `install.sh` — invoke `setup-support.sh` from the main installer flow (one line, near the existing updater setup).

### Build pipeline

- `scripts/build-release.py` — add `"support"` to `CODE_DIRS` (line 45) so the worker module ships in OTA bundles. Add a copy step for the new systemd unit files / sudoers template so OTA can install them.

### Frontend (new + modify)

- `dashboard/src/pages/GeneralSettingsPage.tsx` — add `{ value: 'support', label: 'Support', icon: LifeBuoy }` to the `TABS` array (currently lines 22-30) and a `<TabsContent value="support"><SupportTabContent /></TabsContent>` block. Two-line change.
- `dashboard/src/components/SupportTabContent.tsx` — new component. Uses `useQuery({ queryKey: ['support','bundles'] })` for the list, `useMutation` to POST a new job, and `useQuery` with `refetchInterval: 2000` while the job's status is `queued|running` for polling. On `ready`, auto-fetch the download via `api.get('/support/bundles/{id}/download', { responseType: 'blob' })` and trigger the blob-anchor-click flow already implemented at `dashboard/src/components/reports/ExportMenu.tsx:31-58`. Form fields mirror the UX flow above; same shadcn primitives as `SmtpCard` in `GeneralSettingsPage.tsx:210-381`.

### Tests

- `server/tests/test_support_redaction.py` — every redaction regex against known-bad inputs (`POSTGRES_PASSWORD=secret`, `postgresql://u:p@h/db`, `Authorization: Bearer xyz`, `api_key = abc123`, SNMPv3 passphrase fields, SMTP password, sensor api_key, etc.); test that `redaction-report.json` counts but does not log values; test that the redactor is idempotent.
- `server/tests/test_support_collectors.py` — each collector run against a mocked subprocess/asyncpg; one failing collector does not abort the bundle; status JSON ends `ready` even with one warning.
- `server/tests/test_support_router.py` — admin-only enforcement (403 for non-admin); 202 on POST; status transitions; download blocked unless `ready`; UUID validation rejects path-traversal attempts (`../`, absolute paths, non-UUID strings).
- `server/tests/test_support_archiver.py` — 5 MB / 50 MB caps respected; truncation markers present in the archive; tar integrity (`tarfile.open` round-trips).
- `dashboard/src/components/__tests__/SupportTabContent.test.tsx` — renders; POST payload shape; polling transitions to download; error UI on `failed`.
- Lint: `bash -n scripts/setup-support.sh`, `visudo -cf /etc/sudoers.d/zenplus-support`.

## Bundle contents (top-level layout)

```
zenplus-support-<applianceid8>-<UTC-YYYYMMDD-HHMMSS>.tar.gz
├── manifest.json            # schema_version, appliance id, version, ts, options
├── README.txt               # what's in here, redaction policy, contact info
├── redaction-report.json    # {kind: count} — never values
├── checksums.sha256
├── inventory/               # system.json, services.json, docker.json, versions.json, limits.json
├── health/                  # system-health.json, api-health.txt, dep-health.json, known-risk-checks.json
├── logs/                    # journal-<unit>.log×N, updater-update.log, update-history.json, nginx-error.log
├── database/                # postgres-version, postgres-size, schema-migrations.json,
│                            #   migration-status.txt, critical-schema-checks.json, row-counts.json,
│                            #   recent-audit-actions.json, pg-stat-activity-redacted.json, pg-locks.json
├── clickhouse/              # version, ping, tables, table-sizes, row-counts, mutations, errors.log
├── config/                  # *.redacted copies of .env, agent.conf, subscription.json,
│                            #   docker-compose.yml, nginx-zenplus.conf, systemd units, migrations.lock
├── network/                 # ss-listen.txt, ip-route.txt, ip-addr.txt, resolv.conf, ufw-status.txt
├── storage/                 # df.txt, df-i.txt, lsblk.txt, lvm.json, du-summary.txt, data-mount.txt
├── updates/                 # version.txt, update-history.json, timer-status.txt, backups-summary.json
└── features/                # snmp.json, discovery.json, windows-credentials.json,
                             #   server-monitoring.json, sensors.json, notifications.json, netflow.json
```

Every section's leaf JSON includes `{status: ok|warning|failed, collected_at, notes: [...]}` so support engineers can see at a glance which collectors had trouble.

## Verification plan

End-to-end manual QA:

1. **Healthy appliance** → generate bundle, confirm all sections `status: ok`, `redaction-report.json` shows zero hits on hostnames but non-zero on at least `.env` passwords, archive size <10 MB.
2. **Today's migration-drift case** → edit one row in `schema_migrations` to a wrong checksum, regenerate; `database/schema-migrations.json` shows the row, `health/known-risk-checks.json` flags drift.
3. **Today's 502 case** → stop `zenplus-poller`, drop the API worker by `kill -9`, regenerate; `inventory/services.json` shows failed services, `logs/journal-zenplus-api.log` shows the SIGKILL.
4. **Missing `SNMP_ENC_KEY`** → unset and regenerate; `known-risk-checks.json` flags it.
5. **ClickHouse container down** → `docker stop zenplus-clickhouse`; ClickHouse collector reports `status: failed` but bundle still completes.
6. **Large logs** → write a 200 MB log file; final archive ≤50 MB and the file's collector entry shows `truncated_at_bytes`.
7. **Non-admin user** → confirm UI tab is hidden / API returns 403.
8. **Secret leak check** → `grep -aiE 'password|secret|api_key|bearer ' bundle/**` should produce only `[REDACTED:*]` markers, no plaintext.

Automated:

- `python -m pytest server/tests/test_support_*` (full suite added in Phase 1).
- `bash -n scripts/setup-support.sh` and a `visudo -cf` syntax check.
- `python scripts/build-release.py lint-migrations` continues to pass.

## Phased implementation (PR-sized)

**Phase 1 — Backend + worker skeleton (one PR).**

- `support/` module with `__main__`, redaction, archiver, manifest, and 3 collectors (inventory, health, logs).
- `server/app/api/v1/support.py` router + `server/app/services/support_jobs.py`.
- Systemd template + setup script + sudoers grant.
- Tests for redaction, archiver, router auth.
- Manually trigger from the API and verify a real `.tar.gz` lands in `/opt/zenplus/support/bundles/`.

**Phase 2 — Remaining collectors + frontend tab (one PR).**

- database, clickhouse, config_files, network, storage, updates, features collectors.
- `SupportTabContent.tsx` and `GeneralSettingsPage.tsx` two-line wiring.
- Frontend tests; manual QA scenarios 1-8 above.
- Wire into `scripts/build-release.py` (`CODE_DIRS` and OTA install step).

**Phase 3 — Stretch: cloud upload (separate PR, not blocking).**

- `POST /api/v1/support/bundles/{id}/upload` → multipart upload to `zentryc.com/api/v1/admin/support-cases` using the existing appliance api_key (same auth path as `updater/agent.py`).
- "Send to support" button in the bundle list. Server side needs a corresponding endpoint on zentryc.com — coordinate with that codebase.

## Out of scope (v1)

- Privacy-enhanced anonymization (host-0001 mapping, IP randomization). Bundle keeps real hostnames/IPs — defer to v2 if customers ask. Redaction still strips secrets.
- Signed bundles (the recipient already trusts the channel — ticket attachment).
- Customer-facing bundle preview/diff before download.
- Sensor-appliance support bundles.
- CLI: `sudo zenplus support-bundle`.
- Auto-diagnosis ("we detected migration drift"). The bundle gives the data; analysis stays human for now.
