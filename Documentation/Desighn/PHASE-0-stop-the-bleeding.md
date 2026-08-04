# Phase 0 — Stop the Bleeding

**Goal:** after this phase, the product can lose a disk without losing its history, runs its background work exactly once, exposes no anonymous control surface, and an update or crash no longer means silent monitoring loss.
**Duration:** 1–2 weeks (~23 pd across 2 engineers). **No new hardware.**
**Entry criteria:** staging appliance built from current release; Mailpit SMTP sink wired as the staging email gateway; offsite backup target reachable (S3/MinIO or NFS on another host).

Everything in this phase is deliberately small and independently shippable. Do **not** start Phase 1 work streams until the Exit Gate at the bottom passes — several Phase 1 tasks build directly on T6/T8/T10.

---

## Workstream A — Data safety

### P0-T1 · Continuous Postgres backup with pgBackRest **[blocker]**
**Effort:** 2 pd · **Depends on:** offsite target
**Problem:** The only scheduled backup is an untracked script (`bin/zenplus-backup.sh`, excluded by `.gitignore:69`) that `install.sh` never creates and that last produced a dump on 2026-04-15. There is no WAL archiving, so best-case recovery loses everything since the last dump.
**Change:**
1. Install and configure pgBackRest on the appliance: nightly full/differential base backup + continuous WAL archiving (`archive_command`) to the offsite target; retention 14 full days.
2. Ship it in the repo: config template + a `scripts/setup-backups.sh` invoked from `install.sh`, plus a systemd timer `zenplus-pg-backup.timer` (replace the untracked cron).
3. Backup failure must be *loud*: on non-zero exit, write a `pg_backup_failed` alert through the product's own alert pipeline (an internal POST or direct insert), not just a log line.
**Files:** new `scripts/setup-backups.sh`, `scripts/systemd/zenplus-pg-backup.{service,timer}`, `install.sh`, pgBackRest conf under `updater/config/` or `/etc/pgbackrest/`.
**Verify:**
1. `sudo -u postgres pgbackrest --stanza=zenplus info` shows a full backup < 24 h old and WAL archiving current (`min/max WAL` advancing).
2. Kill the WAL archive target for 10 min → a visible alert/notification fires; restore target → archiving catches up (info shows no gap).
3. Point-in-time restore on a scratch VM: restore to a timestamp 5 minutes back, `SELECT count(*) FROM devices;` matches production at that moment (insert a marker row before the test to prove the timestamp).
4. Fresh `install.sh` run on a scratch VM creates the timer: `systemctl list-timers | grep zenplus-pg-backup`.

### P0-T2 · ClickHouse scheduled backup **[blocker]**
**Effort:** 2 pd · **Depends on:** offsite target
**Problem:** ClickHouse — all metric history, ~18 GB — has **zero backups of any kind** (verified repo-wide: no `BACKUP`, no `FREEZE`, nothing in cron/compose/rollback).
**Change:**
1. Nightly native backup from inside the container: `BACKUP DATABASE zenplus TO S3('…')` (or `TO Disk('backups', …)` onto an NFS mount), incremental against the last full, weekly full, retention 4 weeks.
2. Ship as `zenplus-ch-backup.{service,timer}` + script in repo; wire into `install.sh`; failure raises an alert exactly like T1.
3. Document the restore procedure (per-table `RESTORE` and full-database) in the runbook (T4).
**Files:** new `scripts/ch-backup.sh`, `scripts/systemd/zenplus-ch-backup.{service,timer}`, `install.sh`, `docker-compose.yml` (mount backup disk / S3 credentials via env).
**Verify:**
1. Timer fired: `SELECT * FROM system.backups ORDER BY start_time DESC LIMIT 3` shows `BACKUP_CREATED` nightly, and the offsite bucket/mount contains the files.
2. Restore drill: `RESTORE TABLE zenplus.ping_metrics AS zenplus.ping_metrics_restored FROM …` on staging; `SELECT count() FROM ping_metrics_restored` equals the count captured at backup time.
3. Pause the offsite target → next run raises the failure alert.

### P0-T3 · Encrypted config backup, offsite **[high]**
**Effort:** 1 pd
**Problem:** Config tarballs (when they existed) contained the plaintext `.env` with every credential, stored on the same disk they protect.
**Change:** nightly + on-change tar of `.env`, `/etc/nginx/sites-available/zenplus`, `/etc/systemd/system/zenplus-*`, `updater/config/`, `poller/config.yaml`, encrypted with `age` (public key baked in, private key held offline by ops), pushed offsite; retention 30 days. Include in T1's timer or its own.
**Verify:** offsite object exists < 24 h old; `age -d` with the ops key on a scratch machine yields a tar whose `.env` matches; grep confirms **no unencrypted** config tar remains on the appliance.

### P0-T4 · Restore runbook + full restore drill **[blocker]**
**Effort:** 2 pd · **Depends on:** T1, T2, T3
**Problem:** No documented restore procedure has ever been executed. A backup that has never been restored is a hypothesis.
**Change:** write `Documentation/Desighn/runbooks/RESTORE.md`: bare-Ubuntu → `install.sh` → stop services → pgBackRest restore → CH restore → config restore → start → validation checklist. Then **execute it end-to-end** on a scratch VM against real offsite backups, timing each step.
**Verify:**
1. Drill completes with total time recorded; **target RTO ≤ 4 h**.
2. Restored appliance passes: login works, device list matches, a 7-day interface chart renders (proves CH restore), alert rules intact.
3. Runbook updated with the measured timings and any surprises hit during the drill.

### P0-T5 · Disk & journal headroom, disk alerting **[high]**
**Effort:** 1 pd
**Problem:** `/` is 56 GB at 63% with 3.5 GB of unbounded journald; `updater/backups` holds 3×133 MB tarballs including `dashboard/src`; the only disk check pipes to `logger` which nothing reads.
**Change:** set `SystemMaxUse=1G` in `/etc/systemd/journald.conf` (ship via install/OTA); drop `dashboard/src` from `BACKUP_TARGETS` in `updater/rollback.py:16-26`; grow the appliance disk (ops task — target ≥ 250 GB total per the BOM); route the existing disk-check cron into a real product alert at 80%/90% thresholds.
**Verify:** `journalctl --disk-usage` ≤ 1 GB after `systemd-journald` restart; next OTA backup tarball measurably smaller; fill `/data` with a test file past 80% → alert email arrives at the Mailpit sink; disk resize visible in `df -h`.

---

## Workstream B — Correctness under concurrency

### P0-T6 · Leader-elect every background loop **[blocker]**
**Effort:** 2 pd
**Problem:** 7 of 9 startup loops run in *every* uvicorn worker (`main.py:96-125`). Only `capture_sweeper_loop` takes `pg_try_advisory_xact_lock` (`network_capture_service.py:166-172`). With today's 2 workers: alert evaluators, health sweeper, report scheduler, APM registry and package sync all double-fire per tick.
**Change:**
1. Add `app/core/singleton.py` with a helper equivalent to `run_exclusive(lock_key: int)` / decorator `@singleton_tick("name")` that opens a session, takes `pg_try_advisory_xact_lock(hashtext('zenplus:<name>'))`, and skips the tick when not acquired — same semantics as the capture sweeper.
2. Apply to: `health_sweeper_loop` (`server_health_service.py:337`), `host_alert_evaluator_loop` (`host_alert_service.py:269`), `network_alert_evaluator_loop` (`network_alert_service.py:456`), `report_scheduler_loop` (`report_scheduler.py:373`), `apm_service_registry_loop` (`apm_services.py:345`), `_sync_agent_packages_once` (`main.py:127`). (Discovery tick already uses SKIP LOCKED; leave it.)
3. Log one line per tick at DEBUG: `tick <name> acquired|skipped` so verification is greppable.
**Files:** `server/app/core/singleton.py` (new), the six service files, `main.py`.
**Verify:**
1. Run staging with `--workers 4`. Over 10 minutes: `journalctl -u zenplus-api | grep "tick host_alert"` shows exactly 10 `acquired` (±1) and ~30 `skipped` — never two `acquired` in the same minute.
2. `SELECT count(*) FROM pg_locks WHERE locktype='advisory'` during a tick shows one holder per loop name.
3. Kill the worker PID currently holding a lock (`kill -9`) → within one interval another worker's tick logs `acquired`.
4. Existing behavior preserved: alerts still evaluate (flap a test device, alert row appears ≤ 2 min).

### P0-T7 · Report scheduler: claim before render **[blocker]**
**Effort:** 1 pd · **Depends on:** T6
**Problem:** `run_scheduler_tick` takes `FOR UPDATE SKIP LOCKED` but `generate_and_deliver` commits mid-work (`report_scheduler.py:226`), releasing the row lock **before** `next_run_at` advances (`:358-365`) — the other worker re-renders and re-emails the same schedule. Duplicate emails are reproducible today.
**Change:** advance `next_run_at` (or set `last_started_at`/`claimed_until` lease columns) in the same transaction that selects the due row, *before* rendering; deliver afterwards; on failure, reset for retry with a note.
**Files:** `server/app/services/report_scheduler.py`; migration only if lease columns are added.
**Verify:**
1. With `--workers 4` and Mailpit as gateway: create a schedule due in 1 min → **exactly one** email in Mailpit, exactly one `report_runs` row.
2. Repeat 5 times (or set a 5-min recurring schedule for 30 min): email count == run count == schedule fire count.
3. Kill the API mid-render → schedule is retried on next tick, still exactly one delivered email for that fire (check Mailpit count).

### P0-T8 · Unique indexes for alert dedup **[blocker]**
**Effort:** 1.5 pd
**Problem:** Alert dedup is check-then-insert with no DB constraint (`server_health_service.py:78-95`, `network_alert_service.py:239-245`) — near-simultaneous evaluator ticks create duplicate active alerts and duplicate notifications.
**Change:**
1. Migration: partial unique indexes on `alerts` for each dedupe shape actually used, e.g. `CREATE UNIQUE INDEX … ON alerts(rule_id, device_id) WHERE status='active'` and the server/interface variants (derive exact keys from the three writer call sites: `alert_engine.py`, `host_alert_service.py`/`server_health_service.py`, `network_alert_service.py` — include the interface/metric discriminator where it participates in identity). Pre-clean existing duplicates in the migration (keep newest, resolve older).
2. Writers switch to `INSERT … ON CONFLICT DO NOTHING RETURNING id` and only notify when a row was actually inserted.
**Files:** new `scripts/migrate-052-alert-dedup-unique.sql`, the three writer services.
**Verify:**
1. Migration applies on staging (which contains historical duplicates) without error; `SELECT rule_id, device_id, count(*) FROM alerts WHERE status='active' GROUP BY 1,2 HAVING count(*)>1` returns 0 rows.
2. Concurrency hammer: `for i in $(seq 20); do curl -s -XPOST …/alert-engine/evaluate -d @down_event.json & done; wait` (with T11's auth header) → exactly 1 active alert, exactly 1 notification in Mailpit.
3. 4-worker soak (Exit Gate) shows zero duplicate active alerts across 24 h.

### P0-T9 · Discovery recovery must not kill live runs **[high]**
**Effort:** 0.5 pd
**Problem:** `recover_stuck_runs` fails **every** queued/running run 15 s after any worker starts (`discovery_scheduler.py:115-128`) — worker B's boot kills worker A's live scans. (Full ownership leases come in Phase 1; this is the safety stopgap.)
**Change:** add `AND updated_at < NOW() - INTERVAL '15 minutes'` (runs write progress every ~4% of targets, so a live run's `updated_at` is always fresh) and log which run IDs were failed.
**Verify:** start a 2000-target discovery; `systemctl restart zenplus-api` mid-scan; the run either survives (other worker untouched) or, if its owner died, is failed only after 15 min — never instantly. `discovery_runs.error_details` no longer shows instant "Interrupted by API restart" on healthy runs.

### P0-T10 · Drop counters on all poller buffers **[blocker]**
**Effort:** 1.5 pd
**Problem:** All five poller ClickHouse buffers drop silently on overflow with **no counter and no log** (`poller/internal/store/clickhouse.go:78-81, 176-179, 305-317, 415-419`). Data loss during any ClickHouse stall is invisible — this is the current failure mode at ~80×48-port devices.
**Change:** atomic per-buffer drop counters + a rate-limited warn log on first drop per minute; expose in the `:8081/health` JSON (`dropped_ping`, `dropped_service`, `dropped_snmp`, `dropped_snmp_if`, `dropped_traps`, plus `flushed_rows` per table); same counters for failed flushes (`insertBatch` error path currently discards up to 1000 rows per `clickhouse.go:95-98`).
**Files:** `poller/internal/store/clickhouse.go`, `poller/cmd/poller/main.go` (health handler).
**Verify:**
1. `curl -s localhost:8081/health | jq` shows the six counters at 0 in steady state.
2. Induce a stall: `docker pause zenplus-clickhouse` for 60 s under load → counters increase, one warn line per buffer per minute in `journalctl -u zenplus-poller`; `docker unpause` → counters stop rising, flushes resume.
3. Counters survive into the Exit Gate soak report (zero drops at current estate = pass).

---

## Workstream C — Closing the anonymous surface

### P0-T11 · Authenticate the unauthenticated endpoints **[blocker]**
**Effort:** 2 pd
**Problem:** Reachable through nginx with no auth: `POST /api/v1/alert-engine/evaluate|evaluate-service|evaluate-trap` (`alert_engine.py:367,768,1040` — anyone can forge status changes and trigger real emails/SMS), `GET /api/v1/stream/{metrics,status,alerts}` (`realtime.py:43-59` — full live telemetry; the SSE auth deps exist unused in `core/security.py:100-128`), `GET /api/v1/system-updates/health` (`system_updates.py:802` — anonymous 4× subprocess), `POST /api/v1/ncm/run-scheduled` (`ncm.py:593` — SSH fan-out). Poller and NetFlow health listen on `0.0.0.0:8081/:8091`.
**Change:**
1. Internal caller token: add `INTERNAL_API_TOKEN` to `.env` (generated at install); poller sends it as `X-Internal-Token` on its three alert-engine POSTs (`engine.go:463,545,565`) and the ncm-backup timer's curl (`scripts/systemd/zenplus-ncm-backup.service`); FastAPI dependency rejects without it.
2. SSE: wire the existing `get_current_user_stream` dependency into the three stream routes; dashboard `useSSE.ts` passes the JWT (EventSource can't set headers — use the query-param path the dep already supports, or switch to `fetch`-based SSE).
3. `system-updates/health` → `require_admin_user`; keep an unauthenticated **shallow** `/api/v1/system/health` (static 200, no subprocesses) for LB checks.
4. Bind poller/netflow health servers to `127.0.0.1` (config-overridable for Phase 2 LAN checks).
**Files:** `server/app/api/v1/alert_engine.py`, `ncm.py`, `system_updates.py`, `api/websocket/realtime.py`, `core/security.py`, `poller/internal/pinger/engine.go`, `poller/cmd/{poller,netflow-collector}`, `install.sh`, `dashboard/src/hooks/useSSE.ts`.
**Verify:**
1. Anonymous matrix from another host: each of the five endpoints returns 401/403; with a valid JWT/`X-Internal-Token` they work. Script the matrix (`scripts/tests/anon-surface.sh`) so the Exit Gate can re-run it.
2. Poller still delivers alerts after the change (flap test device → alert row + email).
3. Dashboard live status updates still function (watch a status flip on the dashboard without refresh).
4. `ss -ltn` on the appliance: 8081/8091 bound to 127.0.0.1.

### P0-T12 · TLS on the appliance **[high]**
**Effort:** 1 pd
**Problem:** Production nginx listens on port 80 only; JWTs, agent/sensor API keys and SNMP credentials transit plaintext. The golden-image TLS path was never applied here; sensor installer even supports `curl -k`.
**Change:** enable the existing golden-image TLS config (`scripts/provision-main-appliance-golden.sh:220-312` pattern) on install.sh-based deployments: self-signed (or customer CA) cert, `listen 443 ssl http2`, 301 from 80 (exempt `/api/v1/agents|sensor` paths for a grace period until fleet URLs are HTTPS), rebind API to 127.0.0.1, set `APP_BASE_URL=https://…`. Document custom-CA replacement.
**Verify:** `curl -sI http://10.12.50.81/login` → 301 to https; `curl -skI https://…` → 200; sensor + agent on staging still heartbeat after their env URL is switched to https (check `last_heartbeat_at` advances); browser login works.

### P0-T13 · Evict and rotate the release signing key **[blocker]**
**Effort:** 2 pd · **Sequencing matters — read fully before starting.**
**Problem:** The Ed25519 **private** release key lives on the appliance (`updater/keys/zentryc-release.key`) with a second, group-readable copy in `Documentation/keys/` that ships inside golden OVAs. Anyone with a disk image can forge signed releases for the entire fleet (the updater runs them as root).
**Change (in this order):**
1. Generate a new keypair **offline** (build host / CI secret, never on an appliance).
2. Ship an OTA release (signed with the **old** key) whose only job is: install the new public key alongside the old (`updater/keys/`), delete both private-key copies, and delete `Documentation/keys/` from disk.
3. Switch `build-release.py` signing to the new private key (from CI secret store); updater trusts new pubkey (keep old pubkey accepted for one release cycle, then remove it).
4. Purge the key from any existing golden OVA artifacts and from `provision-main-appliance-golden.sh` export state; rebuild published OVAs.
**Verify:**
1. On appliance after the transition release: `sudo find / -name '*release*.key' 2>/dev/null` → empty; `ls updater/keys/` shows new `.pub` only.
2. Next real release signed with the new key installs successfully (staging updater log shows signature OK).
3. A package signed with the **old** key is rejected after the deprecation release (negative test on staging).
4. Fresh OVA build contains no private key (mount image, find).

### P0-T14 · Disarm the destructive legacy update path **[high]**
**Effort:** 0.5 pd
**Problem:** `zenplus update` (`install.sh:757`) does `git fetch && git reset --hard origin/main`. On an OTA-managed appliance (like production, currently schema-ahead on a feature branch) this silently rolls code back to `main` while the ClickHouse schema stays new — schema-ahead-of-code corruption in one command.
**Change:** the CLI subcommand checks for `updater/config/agent.conf` with a registered appliance id; if present, print "This appliance is OTA-managed; use the Updates page / zenplus-updater" and exit 1 (override flag `--force-git` for developers).
**Verify:** on staging (OTA-registered): `sudo zenplus update` refuses with the message, exit code 1, git tree untouched (`git status` unchanged); with `--force-git` old behavior still available on a dev box.

---

## Workstream D — Availability quick wins

### P0-T15 · systemd: never stay dead, watch the API **[blocker]**
**Effort:** 1 pd
**Problem:** `zenplus-api` has `Restart=on-failure` + `StartLimitBurst=5`/`StartLimitIntervalSec=60` — 5 crashes in 50 s and it stays down **forever, silently**. A hung (not crashed) API is never detected at all.
**Change:** on api/poller/netflow units: `Restart=always`, `StartLimitIntervalSec=0`, `RestartSec=10`. Add `zenplus-watchdog.{service,timer}` (every 60 s): curl the shallow health endpoint via nginx **and** direct `127.0.0.1:8000`; on 3 consecutive failures `systemctl restart zenplus-api` and log loudly; same shallow check for poller `:8081` and netflow `:8091`.
**Files:** `scripts/systemd/*.service` (+ live units via OTA `install_systemd` step), new watchdog script + units, `install.sh`.
**Verify:**
1. Crash-loop test: `for i in 1 2 3 4 5 6; do sudo kill -9 $(pgrep -f 'uvicorn app.main' | head -1); sleep 8; done` → API is running afterwards (`systemctl is-active zenplus-api` = active; old config would be `failed`).
2. Hang test: `sudo kill -STOP` the uvicorn master → within ~3 min the watchdog restarts it (journal shows the watchdog action) and `curl /api/v1/system/health` recovers.
3. `systemctl show zenplus-api -p NRestarts` increments across the tests; no manual intervention used.

### P0-T16 · Cut update downtime to ≤20 s and honor maintenance windows **[high]**
**Effort:** 1 pd
**Problem:** Updates stop all services for ~84 s; ~70 s of that is creating the 133 MB code backup **after** `stop_services` (`build-release.py:587-588` ordering). `maintenance_window_start/end` and `auto_update` are parsed (`updater/config.py:98-106`) but never read by `agent.py` — appliances update whenever the 4 h timer fires, including business hours.
**Change:** reorder manifest generation: `backup` (and dashboard prebuild) **before** `stop_services`; in `updater/agent.py`, honor `auto_update=false` (check-in but don't apply) and defer applying outside `maintenance_window` (apply-on-next-checkin-in-window).
**Files:** `scripts/build-release.py`, `updater/agent.py`, `updater/config.py` docs.
**Verify:**
1. Staging OTA update with the reordered manifest: from `updater/logs/update.log`, `stop_services` → post-update `health_check OK` **≤ 20 s**.
2. Set `maintenance_window_start=02:00`, `end=04:00`; publish a release at 14:00 → checkin logs "deferred (outside window)"; force the clock/window → applies.
3. `auto_update=false` → status shows update available, nothing applied until the UI "apply now".

### P0-T17 · External dead-man's switch **[high]**
**Effort:** 1 pd (appliance side) · **External dependency:** zentryc.com portal work
**Problem:** A dead/powered-off appliance is discovered by humans opening the dashboard. The only outbound signal is the 4 h OTA check-in, and nothing alerts on its absence.
**Change:** appliance sends a lightweight heartbeat POST (id + version + shallow health bits) every 5 min (separate tiny timer, reusing updater credentials); **portal side** (tracked as an external ticket): "no heartbeat for 15 min → email/SMS the customer contact". Until the portal work lands, ship the interim: a one-line cron on any *other* customer host (or a sensor VM) that curls `https://<appliance>/api/v1/system/health` and mails on failure — documented in the runbook.
**Verify:** portal (or interim watcher) receives heartbeats (portal log/inbox); power off the staging appliance for 20 min → notification arrives; power on → recovery visible.

---

## Exit Gate — Phase 0

Run after all **[blocker]** tasks are done. Record results in the gate report (template below).

| # | Drill | Pass criteria |
|---|---|---|
| G1 | **24 h soak** on staging with `--workers 4`, Mailpit as gateway, one test device flapped every 30 min (cron toggling an iptables DROP on a lab target), one report schedule firing hourly | Zero duplicate active alerts (`GROUP BY … HAVING count>1` query returns 0); Mailpit email count == expected fires exactly; each loop's `acquired` log count == elapsed ticks ±2; poller drop counters all 0 |
| G2 | **Restore drill** (T4) executed by someone who didn't write the backups | RTO ≤ 4 h; validation checklist passes |
| G3 | **Backup continuity**: 3 consecutive days | pgBackRest info shows daily backups + continuous WAL; `system.backups` shows 3 nightly CH backups; offsite listing confirms |
| G4 | **Anonymous surface scan**: `scripts/tests/anon-surface.sh` from an off-box host | All five endpoint groups 401/403; 8081/8091 unreachable off-box; port 80 redirects to 443 |
| G5 | **Kill tests** (T15): crash-loop ×6 and SIGSTOP hang | Service self-recovers both times, no human action |
| G6 | **Timed OTA update** on staging | Blind window ≤ 20 s measured from update.log; maintenance-window deferral demonstrated |
| G7 | **Key eviction** | `find / -name '*release*.key'` empty on staging & fresh OVA; new-key release applies; old-key release rejected |
| G8 | **Dead-man** | 20-min power-off produces an external notification |

**Gate report template** (commit as `Documentation/Desighn/gates/phase-0-gate-YYYYMMDD.md`):

```
Date / staging build / participants:
G1 soak: start/end, alert dup query output, mailpit counts, loop tick counts, drop counters
G2 restore: total time, checklist result, issues
G3 backups: pgbackrest info excerpt, system.backups rows, offsite listing
G4 scan: script output
G5 kill tests: journal excerpts
G6 update: log timestamps, window math
G7 keys: find output, release accept/reject evidence
G8 dead-man: notification screenshot/timestamps
Verdict: PASS / FAIL (+ follow-ups)
```
