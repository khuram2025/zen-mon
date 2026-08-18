# Raw investigation: ZenPlus ↔ zentryc.com sync rails (for the vuln/patch/EOL feed design)

> Investigator report for the Compliance & Vulnerability Management module plan.
> Scope: every way the appliance talks to the central server `zentryc.com`, the exact
> protocol/auth/packaging, and how a new content channel ("vuln feed", "patch catalog",
> "EOL catalog") can ride the same rails with minimal new machinery.
> Sources read in full: `/opt/zenplus/updater/*` (agent, config, crypto, downloader,
> clickhouse_sync, executor, inventory, history, schema_gate, steps/, systemd/),
> `/opt/zenplus/server/app/api/v1/system_updates.py`, `subscription.py`,
> `/opt/zenplus/scripts/build-release.py`, `install.sh`, `scripts/setup-updater.sh`,
> `Documentation/11-SERVER-SIDE-IMPLEMENTATION-GUIDE.md`, `14-REMOTE-SERVER-INTAKE.md`,
> `15-RELEASE-RUNBOOK.md`, dashboard components, plus project memory on the live server.
> Date: 2026-08-18. Appliance branch: `feat/udt-module`, last release 1.15.2.

---

## 1. Big picture — every appliance↔zentryc.com touchpoint

| # | Channel | Direction | Component | Endpoint(s) | Cadence |
|---|---------|-----------|-----------|-------------|---------|
| 1 | OTA registration | appliance → server | `updater/agent.py` `register()` AND `server/app/api/v1/system_updates.py` `POST /api/v1/system/register` (proxies) | `POST https://zentryc.com/api/v1/appliances/register` | once (license key paste) |
| 2 | Check-in / heartbeat + update offer | appliance → server | `updater/agent.py` `checkin()` | `POST /api/v1/appliances/checkin` | systemd timer: boot+5min, then every 4h (configurable, ±5min jitter) |
| 3 | Explicit update check | appliance → server | `updater/agent.py` `check_for_update()` | `GET /api/v1/updates/check?current_version&arch` | inside the multi-release walk loop and `--check` |
| 4 | Package download | appliance → server | `updater/downloader.py` | `GET <release.package_url>` = `https://zentryc.com/api/v1/updates/download/<uuid>` | per offered release |
| 5 | Status report | appliance → server | `updater/agent.py` `report_status()` | `POST /api/v1/updates/report` | 3–4× per update attempt |
| 6 | Subscription query | appliance → server | `updater/agent.py` `query_subscription()`; FastAPI `POST /api/v1/system/refresh-subscription` (uses checkin) | `GET /api/v1/appliances/subscription`; `POST /api/v1/appliances/checkin` | on demand (dashboard Refresh button) |
| 7 | Release publishing (ops, not appliance) | build host → server | `scripts/build-release.py`, `scripts/release.sh` / `/usr/local/sbin/zenplus-release` | `POST /api/v1/admin/auth/login`, `POST /api/v1/admin/releases/create`, `POST /api/v1/admin/releases/{id}/publish`, `POST /api/v1/admin/rollouts`, `GET /api/v1/admin/releases` | per release |
| 8 | KB deep links | user browser → server | `dashboard/src/components/{apm,udt}/KbLink.tsx` (`KB_BASE = 'https://zentryc.com/kb/zenplus/{apm,udt}'`) | static Django pages | user click; **not** appliance egress |
| 9 | GeoIP (not zentryc) | appliance → db-ip.com | `scripts/fetch-geoip.py` (`BASE = "https://download.db-ip.com/free"`), run as best-effort OTA hook | external CDN | monthly-ish, always exits 0 |

There is **no push channel**: zentryc.com never initiates a connection to an appliance. Everything is appliance-initiated HTTPS pull, which is exactly the shape a feed sync must keep.

---

## 2. Appliance-side updater — files and lifecycle

Directory `/opt/zenplus/updater/`:

| File | Role |
|------|------|
| `agent.py` (627 lines) | main daemon: register, checkin, download/verify/extract, apply, report, multi-release walk loop |
| `config.py` | INI config loader/saver (`config/agent.conf`), subscription cache (`config/subscription.json`) |
| `crypto.py` | Ed25519 sign/verify, sha256, `verify_checksums`, `verify_manifest` |
| `downloader.py` | streaming HTTPS download w/ resume (Range) + sha256 integrity |
| `executor.py` | manifest step registry/executor with rollback |
| `steps/` | `apply_code, backup, build_dashboard, health_check, install_binary, install_config, install_systemd, os_package (apt_install/remove/update), pip_install, run_hook, run_migration, service_control` |
| `inventory.py` | check-in payload builder (incl. node_count, schema_status, dashboard_build) |
| `history.py` | local `logs/update-history.json` (drives dashboard Updates tab), MAX_HISTORY=50 |
| `schema_gate.py` | post-apply schema convergence gate (`scripts/sync-schema.py --json`, timeout 1800s) |
| `clickhouse_sync.py` | thin adapter to `scripts/ch_migrate.py` |
| `lockfile.py`, `health.py`, `rollback.py` | update lock, HTTP health check, backup restore |
| `keys/zentryc-release.pub` (0644, 113 bytes) / `zentryc-release.key` (0400 zenplus, 119 bytes, build hosts only) | Ed25519 release keypair |
| `config/agent.conf` (0600), `config/subscription.json` (0600) | per-appliance credentials + subscription snapshot — **excluded from release packages** (build-release.py:641–647 ignores `config`, `keys`, `*.key`, `agent.conf`, `subscription.json`) |
| `systemd/zenplus-updater.service`, `zenplus-updater.timer` | oneshot root service + timer |

### 2.1 Update lifecycle (`agent.py`)

`main()` (agent.py:498–581): load config → require `appliance.id` + `appliance.api_key` (else "not registered", exit 1) → acquire `UpdateLock` → `checkin()` → if a release is offered, loop:

1. `check_min_version()` (agent.py:377–396) — refuses a release whose `min_version` floor is above `current_version` (added 1.12.1; the server has no stepwise delivery, see §7 gap list).
2. `run_update()` (agent.py:399–495):
   - `report_status(..., "downloading")`
   - `download_and_extract()` (agent.py:280–350): download `.zup` → path-traversal guard on tar members (`startswith("/")` or `".."` → `SecurityError`, agent.py:315–319) → extract → if `manifest.json.sig` absent and the API supplied `manifest_sig` b64, write it (agent.py:329–331) → `verify_manifest()` → `verify_checksums()`.
   - `report_status(..., "applying")` → `execute_manifest()` (executor.py:76–109; on step failure runs `rollback_steps`, errors logged but non-fatal during rollback).
   - **Schema gate** (agent.py:448–473): `schema_gate.sync_and_verify()`; on failure → `rollback_manifest()` + report failed. "A passing HTTP health check is not evidence that the schema matches the code."
   - Only then stamp `/opt/zenplus/.version` (line 1 = semver, line 2 = ISO timestamp; agent.py:476–479) and report `success`.
3. Walk-forward loop (agent.py:547–571): after each success, `check_for_update()` again and continue while the server offers something strictly newer (`_version_key` is numeric-tuple; anything non-numeric — e.g. a legacy git SHA — sorts as (0,0,0), agent.py:353–370).

### 2.2 systemd cadence

`updater/systemd/zenplus-updater.timer`:

```
OnBootSec=5min
OnUnitActiveSec=4h
RandomizedDelaySec=300
Persistent=true
```

`zenplus-updater.service`: `Type=oneshot`, `User=root`, `WorkingDirectory=/opt/zenplus`,
`Environment=PYTHONPATH=/opt/zenplus`, `EnvironmentFile=-/opt/zenplus/.env`,
`ExecStart=/opt/zenplus/venv/bin/python -m updater`, `TimeoutStartSec=1800`.

The dashboard can change the interval: `PUT /api/v1/system/update-config` writes
`check_interval_seconds` into agent.conf **and** a systemd drop-in
`/etc/systemd/system/zenplus-updater.timer.d/override.conf` (`OnUnitActiveSec=<N>h`)
via narrow sudo (`system_updates.py:388–438`, `_write_timer_override`, using
`sudo -n /bin/mkdir|/usr/bin/tee|/bin/systemctl` entries from `/etc/sudoers.d/zenplus-updater`,
installed by `scripts/setup-updater.sh`). Live box currently overridden to `4h`.
On-demand check: `POST /api/v1/system/check-update` → `sudo -n systemctl --no-block start zenplus-updater.service` (system_updates.py:496–552).

---

## 3. The wire protocol, exactly

### 3.1 Base URL and headers

- Base URL: `cfg.server.url`, default **`https://zentryc.com`** (config.py:16; install.sh writes the same; overridable per appliance via `[server] url` in agent.conf; build side via `ZENPLUS_RELEASE_SERVER_URL`).
- Client: `httpx.Client(base_url=..., timeout=Timeout(30, connect=10), verify=cfg.security.verify_tls, follow_redirects=True)` (agent.py:90–98). Download client: `Timeout(download_timeout_seconds=600, connect=30)`.
- Headers on **every** authenticated call (agent.py:71–87):

```
User-Agent: zenplus-updater/<agent __version__>
Content-Type: application/json
Authorization: Bearer <api_key>        # omitted entirely pre-registration
X-Appliance-ID: <uuid>                 # omitted pre-registration
```

Download requests carry the same headers (agent.py:302 passes `_api_headers(cfg)` into `download_package`), plus `Range: bytes=N-` on resume (downloader.py:46–48; a 416 restarts fresh, downloader.py:57–64).

### 3.2 `POST /api/v1/appliances/register` (auth: none; license key in body)

Request (agent.py:116–125):

```json
{"hostname": "...", "arch": "amd64", "os_version": "ubuntu-24.04",
 "current_version": "1.15.2", "registration_token": "<license key pasted by user>"}
```

Response 200:

```json
{"appliance_id": "<uuid>", "api_key": "<hex>", "subscription": {…}}
```

- `api_key` is plaintext-once; server stores bcrypt hash only (doc 11 §4.1).
- License keys are **single-use** (runbook §5: `403 {"error":"Registration token has already been used"}`).
- Persisted to `agent.conf` (`[appliance] id / api_key`, chmod 0600, config.py:161–165) and `subscription.json`.
- Two callers: CLI `zenplus-updater --register <LICENSE_KEY>` (agent.py:598–624) and the dashboard path `POST /api/v1/system/register` (system_updates.py:573–721) which builds the same payload itself, refuses if already registered (409), then immediately pushes a first checkin with `node_count` so the remote license panel is fresh (system_updates.py:654–676), and syncs the remote plan into the local `subscriptions` PG table (`_sync_from_remote`, subscription.py:101–158).
- Recovery: `POST /api/v1/system/reset-registration` blanks id/api_key locally and deletes subscription.json; does NOT notify the server (system_updates.py:724–770).

### 3.3 `POST /api/v1/appliances/checkin` (auth required)

Request = full inventory (`inventory.collect_inventory()`, inventory.py:182–196):

```json
{"hostname", "arch", "os_version", "current_version", "agent_version": "1.0.0",
 "uptime", "services_status": {"zenplus-api": "active", …},
 "disk": {"total","used","free"},
 "node_count": <devices + service_checks count>,        // license usage push
 "schema_status": {"ok", "checked_at", "problem_count", "problems"[…20]},
 "dashboard_build": "index-<hash>.js,…"}                // served-bundle fingerprint
```

Response:

```json
{"next_action": "none" | "update",
 "release": null | {"id","version","changelog","severity","package_url",
                    "package_sha256","manifest_sig","size","min_version"},
 "subscription": {"id","name","plan","max_appliances","max_devices","used_slots",
                  "available_slots","is_active","is_expired","expires_at","days_remaining",
                  "license": {"total_node_cap","used_node_count","available_nodes"}}}
```

- Subscription is cached on every checkin (`save_subscription`, agent.py:164–173). Expired subscription still returns `next_action:"none"` — monitoring keeps running, only updates stop (intake doc Q16; that was the chosen behavior).
- `severity` ∈ `critical | security | normal | optional` (releases table CHECK, doc 11:110–111).

### 3.4 `GET /api/v1/updates/check?current_version=X&arch=amd64` (auth required)

Response: `{"available": bool, "release": {same shape as above} | null}` (agent.py:209–228).
**Known server behavior (verified 2026-08-11, memory `ota-update-sequencing`): always returns the NEWEST published release regardless of `current_version`** — no next-step ordering, no appliance-facing release-list endpoint (`/updates/releases`, `/releases`, `/updates/history` all 404). The appliance-side loop + `min_version` gate compensates.

### 3.5 `GET /api/v1/updates/download/{release_id}` (auth required)

- `package_url` in the release object points here: `https://zentryc.com/api/v1/updates/download/<uuid>` (runbook §7).
- Server streams the `.zup` (Option A local-disk storage per doc 11 §10); supports `Range` resume; the client re-downloads on 416.
- Client verifies sha256 of the whole file against `package_sha256` after download (downloader.py:96–101); a pre-existing complete file with matching hash short-circuits (downloader.py:38–42).

### 3.6 `POST /api/v1/updates/report` (auth required)

```json
{"release_id","status","from_version","to_version","error_message","log_data"}
```

`status` sequence per attempt: `downloading` → `applying` → `success` | `failed`. Local history JSON is written first (best-effort) so the UI works even if the server report fails (agent.py:243–259). Server upserts `update_history`, updates `appliances.current_version` on success, and feeds the rollout auto-abort math (doc 11 §4.5).

### 3.7 `GET /api/v1/appliances/subscription` (auth required)

`{"subscription": {…}}` — on-demand pull (agent.py:189–206). Note the dashboard "Refresh" deliberately uses **checkin** instead so it also pushes `node_count` (system_updates.py:786–857, comment at 791–799: "Any release advice in the response is ignored here").

### 3.8 Error shapes (both exist on the server)

`_extract_remote_error` (system_updates.py:117–147) documents the live contract:
- Business errors: `{"error": "..."}` / `{"detail": "..."}` / `{"message": "..."}`
- Validation 400s: **DRF-style** `{"field": ["msg1", ...]}` — i.e. the live zentryc.com API is Django REST Framework, not the FastAPI stack doc 11 recommended.

Status mapping observed/documented (runbook §5): 400 version-exists / signature-failed; 401 expired JWT / bad admin creds; 403 used registration token; 404 appliance not linked to subscription (handled at system_updates.py:834–835).

---

## 4. Package format and signature verification (the trust model to reuse)

### 4.1 `.zup` = gzipped tar (despite the extension)

Internal layout (doc 11 §7; build-release.py produces exactly this):

```
manifest.json          # REQUIRED — metadata + ordered steps
manifest.json.sig      # REQUIRED — RAW 64-byte Ed25519 signature of manifest.json bytes
checksums.sha256       # REQUIRED — "sha256␣␣relative/path" per line, all files except itself and the .sig
code/                  # server/ poller/ scripts/ support/ docker/ updater/ .version docker-compose.yml
dashboard-dist.tar.gz  # prebuilt vite bundle
go-binaries/           # zenplus-poller, zenplus-netflow-collector
agent-artifacts/       # Windows MSI etc. + manifest.json (data files delivered via install_config steps!)
sensor-artifacts/bin/linux-amd64/  # zenplus-sensor + sha256 + manifest.json
requirements.txt, migrations/ (optional, emphasis only)
```

### 4.2 Verification chain on the appliance (crypto.py)

`verify_manifest(manifest_path, signature_path, public_key_path, max_age_days=30)` (crypto.py:126–175):
1. Ed25519 signature of the raw `manifest.json` bytes against `/opt/zenplus/updater/keys/zentryc-release.pub` — else `SecurityError`.
2. `manifest["release_date"]` freshness: reject if older than `max_manifest_age_days` (**30 default**, agent.conf) or more than 24h in the future.
3. JSON parse → manifest dict.

Then `verify_checksums(checksums.sha256, extract_dir)` (crypto.py:98–123) → list of `MISSING:`/`MISMATCH:` failures aborts (agent.py:341–348).

Key facts:
- Algorithm: **Ed25519**, `cryptography.hazmat` PEM keys. Signature is raw 64 bytes in the file; base64 only on the API wire (`manifest_sig`).
- Current production public key (rotated 2026-05-03; doc 11:613–619, runbook §1): `MCowBQYDK2VwAyEAhwZpk2+cPN57lhIbcsPAI3Xtx9MyfMPM5m3Ny81swF8=`, file sha256 `58a71bf2…b70e57`, 113 bytes. An older superseded key sits at `keys/zentryc-release.pub.old`.
- Private key: 119 bytes, `0400 zenplus`, lives on the build host (this dev VM) with an off-VM escrow; the server holds only the public key and **re-verifies the manifest signature at upload time** (runbook step 9; bad key → `400 {"error":"Manifest signature verification failed"}`).
- The signature currently ships **inside** the `.zup` (option (b) of intake Q4); the API's `manifest_sig` field is a redundant fallback path the agent still honors (agent.py:329–331).
- `manifest_sig` covers only `manifest.json`; file integrity is transitively covered because `checksums.sha256` is itself listed content — but note: **checksums.sha256 is NOT covered by the signature** (it's excluded from itself and not hashed into the manifest). The `.zup`-level `package_sha256` comes from the server API (unauthenticated-by-signature; TLS + server trust). This is a known weakness worth fixing for a feed channel (put the data-file digests, or the checksums-file digest, inside the signed manifest).

### 4.3 Manifest shape (build-release.py:949–969)

```json
{"format_version": 2, "update_id": "<uuid4>", "version": "1.15.2",
 "from_version": null, "min_version": null,
 "release_date": "<utc iso>", "changelog": "...", "severity": "normal",
 "arch": "amd64", "os_min": "ubuntu-22.04",
 "agent_packages": [ {platform, version, file_name, file_size, sha256} ],
 "steps": [ {"type": "stop_services", ...}, ... ],
 "rollback_steps": [ {"type": "restore_backup"}, {"type": "start_services", ...} ]}
```

Standard step sequence emitted for every release (build-release.py:809–947): `stop_services` → `backup` → `apt_install [snmp, iputils-ping]` → `apply_code (replace, code/)` → idempotent `run_hook` setup scripts (`setup-support.sh`, `setup-storage.sh`, `setup-security.sh`, best-effort `fetch-geoip.py`) → `pip_install` → (named `run_migration`s if any) → **`run_hook code/scripts/sync-schema.py` (the schema gate, timeout 1800, on every release)** → `build_dashboard (prebuilt)` → `install_binary` poller/netflow-collector (+`install_systemd`) → `install_config` for agent/sensor artifacts → `start_services` → `health_check http://localhost:8000/api/v1/system/health`.

**Precedent that matters for feeds:** the Windows agent MSI and the remote-sensor binary are *pure data artifacts* delivered through `install_config`/`install_binary` steps into `/opt/zenplus/artifacts/...` — proof the rails already carry non-code content. But they only move when a full release ships.

### 4.4 Migration philosophy (pattern to copy for feed convergence)

Every release carries the **complete** `migrate-*.sql` set + `scripts/migrations.lock` (append-only, checksum-locked; `lint_migrations` build-release.py:422–510 fails the build on drift, consults git history for pre-lockfile edits, and `SUPERSEDED_CHECKSUMS` in `run-migrations.py` reconciles historical rewrites). Appliances converge from any starting point in one pass. The equivalent feed principle: **publish full snapshots (or snapshot+delta with snapshot fallback) so any stale appliance converges in one download.**

---

## 5. What the appliance UI shows (FastAPI + dashboard)

Routers mounted in `server/app/main.py:72–73` under `/api/v1`:

### 5.1 `system_updates.py` (`prefix="/system"`), all admin-gated (`require_admin_user`)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/system/update-status` (L328) | current version + install time from `/opt/zenplus/.version`; `SchemaHealth` from `/opt/zenplus/.schema-status.json`; appliance_id/server_url/auto_update/interval from agent.conf; timer state via `systemctl show zenplus-updater.timer`; `updater_running` (in-flight history record or service state); last/active update + 10-record history from `updater/logs/update-history.json`; last 30 log lines from `updater/logs/update.log`; cached `subscription` from subscription.json |
| `PUT /api/v1/system/update-config` (L441) | auto_update, check_interval_hours (→ timer drop-in), maintenance window (fields exist; not currently enforced by the agent) |
| `POST /api/v1/system/check-update` (L496) | fire the oneshot updater now (sudo systemctl --no-block) |
| `GET /api/v1/system/registration` (L557) / `POST /api/v1/system/register` (L573) / `POST /api/v1/system/reset-registration` (L724) | registration lifecycle (see §3.2) |
| `GET /api/v1/system/subscription` (L773) / `POST /api/v1/system/refresh-subscription` (L786) | cached / refreshed remote subscription |
| `GET /api/v1/system/health` (L862) | local health (pg, clickhouse :8123/ping, redis, services, snmp crypto) — this is what the manifest `health_check` step hits |

The **file-based hand-off pattern** between root updater and non-root API is central: the updater (root) writes `agent.conf`, `subscription.json`, `update-history.json`, `update.log`, `.version`, `.schema-status.json`; the FastAPI process only reads them (plus two sudo-whitelisted actions). No DB involvement for update state. A feed-sync agent can reuse the identical pattern (e.g. `.feed-status.json` + a `feeds/` state dir) — or, since feeds land as DB rows anyway, report status from the DB.

### 5.2 `subscription.py` (`prefix="/subscription"`)

- `GET /api/v1/subscription` (L161): local `subscriptions` PG row (auto-creates a 30-day trial), then **overlays the remote OTA snapshot** from `updater/config/subscription.json` via `_sync_from_remote` (L101–158: plan, max_devices, derived max_service_checks/max_users from `PLAN_LIMITS`, expires_at, status active/expired/inactive, name→activated_by). Usage counts exclude `poll_mode == "via_controller"` children (L206–208).
- `PLAN_LIMITS` (L23–28): trial 50/20/5, starter 100/50/10, professional 500/200/25, enterprise 10000/5000/100.
- `POST /api/v1/subscription/activate` → **410 deprecated**, use `/system/register` (L255–269).
- Entitlement hook for the new module: plan/status data is available both locally (PG row) and remotely (subscription object each checkin) — a `features` or `entitlements` key added to the remote subscription object would flow through `save_subscription` untouched (it's stored verbatim as JSON) and could gate the vuln-feed UI without agent changes.

### 5.3 Dashboard

- `UpdatesTabContent.tsx` — polls `GET /system/update-status` (queryKey `system-update-status`); triggers `/system/check-update`; saves `/system/update-config`.
- `UpdateNotificationBell.tsx` — same query, refetch 60s idle / 5s during an update; amber dot when unregistered.
- `LicensesTabContent.tsx` — registration UI ("Enter your license key to register this appliance with zentryc.com"), `/system/refresh-subscription`.
- `KbLink.tsx` (apm/udt) — `https://zentryc.com/kb/zenplus/{apm,udt}` opened in the user's browser.

---

## 6. Server side: what zentryc.com actually runs

- **Live implementation is Django/DRF**, not the FastAPI recommended in doc 11 (evidence: DRF-style field-error shape handled in system_updates.py:117–147 and intake doc A7; the public site/KB is a Django project on host `187.77.177.190`, gunicorn `zentryc.service` + `zentryc-celery.service`, user `net`, **no git on that host** — memory `zentryc-kb-publishing`). Fronted by Cloudflare (runbook §5: `curl (55)` HTTP/2-multipart failure note; publish uses httpx HTTP/1.1).
- Admin auth: `POST /api/v1/admin/auth/login {"email","password"}` → `{"token": <24h JWT>}` (build-release.py:582–592 also accepts `access_token`/`key`). Current publish account **`admin@zentryc.com`**, creds file `/root/.zenplus-admin-creds` → staged to `~zenplus/.zenplus-admin-creds` by release.sh (the old `zenai-release@zentryc.com` no longer authenticates).
- Database schema (doc 11 §3 — the intake doc treats it as implemented): `appliances` (id UUID, api_key_hash bcrypt, hostname, ip_address, arch, os_version, current_version, agent_version, last_checkin, is_active, tags JSONB, metadata JSONB, rollout_group canary|beta|stable), `releases` (version UNIQUE, min_version, changelog, severity CHECK, arch, package_url, package_size, package_sha256, manifest_sig, is_published, published_at, created_by), `rollout_policies` (release_id FK, stage canary|percentage|full|paused|aborted, target_group, target_pct 0–100, auto_promote, promote_after INTERVAL, max_failure_pct default 5), `update_history` (appliance_id, release_id, status CHECK pending|downloading|applying|verifying|success|failed|rolled_back, attempt, UNIQUE(appliance_id,release_id,attempt)), `admin_users`, `audit_log`, plus a `registration_tokens` table (single-use license keys).
- Rollout engine (doc 11 §9.1): newest published release for arch → skip if up-to-date / below min_version / already succeeded / ≥3 failures → active rollout policy required (`no active rollout = not available`) → group match → deterministic percentage bucket `sha256(f"{appliance.id}:{release.id}") % 100 < target_pct`. Auto-abort when failure rate > max_failure_pct; auto-promote canary→percentage(10)→full after `promote_after`.
- Package storage: local disk Option A (`/var/www/zentryc/packages/`), streamed through the API download endpoint; nginx rate-limit guidance: register 5r/h, checkin 10r/m, download 5r/m per IP (doc 11 §12.3).
- Version monotonicity enforced at upload (`400 Version X already exists`); the `.zup` signature is re-verified server-side at upload.
- **Publish flow** (runbook §3.3 + build-release.py:1035–1097): login → `POST /api/v1/admin/releases/create` multipart (`file`, `version`, `changelog`, `severity`, `package_sha256`, `manifest_sig` b64) → `POST /api/v1/admin/releases/{id}/publish` → optional `POST /api/v1/admin/rollouts` (`{"release_id","stage","target_group","target_pct","auto_promote","promote_after":"24:00:00","max_failure_pct":5}`); promote via `PATCH /api/v1/admin/rollouts/{id} {"action":"promote"|"pause"|"abort"}`. Everyday wrapper: `sudo zenplus-release <version> "<changelog>" [severity] [rollout]` (root-owned copy of `scripts/release.sh` at `/usr/local/sbin/zenplus-release`, NOPASSWD for `zen`).
- Release object as returned by admin API (runbook §7): includes `package_url: "https://zentryc.com/api/v1/updates/download/<uuid>"` and `"rollouts": [...]`. `manifest_sig` is NOT in the appliance-facing API today (sits in the `.zup`).

### 6.1 Known server-side gaps/constraints relevant to a new channel

1. `GET /updates/check` returns the newest release, never the "next after current" (memory `ota-update-sequencing`, still outstanding server-side).
2. Only the admin API is drivable from this repo; changing zentryc.com's Django code means SSH to the host (no git, hand-deployed) — so a feed channel design should **minimize novel server logic** and ideally reuse the release/rollout tables' shape.
3. Cloudflare in front: uploads must be HTTP/1.1; large-body limit ~150MB was the tested ballpark (intake Q22).
4. Audit retention of `update_history` on the server was an open question (intake Q29) — appliance mirrors what it needs locally. Same principle for feed state.

---

## 7. Constraints observed for appliance egress

- **Appliances can be egress-restricted.** Direct evidence: at least one production deployment blocks public NTP entirely (memory `server-clock-ntp-via-core-switch` — chrony syncs to a core switch instead), and `fetch-geoip.py` is written to always exit 0 because `db-ip.com` may be unreachable ("no route to db-ip.com never fails or delays the OTA update", build-release.py:863–871). Assume some customers allow only `zentryc.com:443`, and some allow nothing (fully air-gapped: OTA simply never happens; the appliance runs fine unregistered — the trial license is seeded locally by install.sh:614–625 and "the trial gates nothing — a key is only needed to unlock OTA updates").
- **Proxy support:** nothing explicit in the agent, but `httpx.Client` defaults to `trust_env=True`, so `HTTPS_PROXY`/`HTTP_PROXY` set in `/opt/zenplus/.env` (loaded by the updater unit via `EnvironmentFile=-/opt/zenplus/.env`) would be honored. Not documented/productized; the FastAPI-side calls (`/system/register`, `/system/refresh-subscription`) run under the API service with its own env. A feed feature should not assume better connectivity than the updater already has.
- **TLS:** system CA bundle, public CA cert on zentryc.com; `verify_tls` can be flipped off per appliance in agent.conf (config.py:31, 94) — the escape hatch for TLS-intercepting middleboxes already exists.
- **Clock skew tolerance:** manifest freshness window is ±(30 days past / 24 h future). Feed manifests must tolerate the same reality (see §8.3).
- **Bandwidth:** `.zup`s run 50–150 MB routinely (intake Q5); downloads resume; check-in bodies are small JSON. A vuln feed snapshot (NVD-derived subset + KEV + patch/EOL catalogs, compressed ndjson) should target the same order of magnitude or less; deltas much smaller.
- **Frequency:** update timer default 4h ± 5min jitter, per-fleet spread via `RandomizedDelaySec`. The server's nginx budget assumed checkin ≈ 10r/m/IP — a feed poll at 4–24h cadence is negligible.

---

## 8. Design: adding a "vuln feed / patch catalog / EOL catalog" content channel

### 8.1 The reusable building blocks (verbatim reuse, no rewrites)

| Concern | Existing machinery to reuse |
|---------|------------------------------|
| Transport + auth | `updater/agent.py::_api_client/_api_headers` — Bearer api_key + X-Appliance-ID against `cfg.server.url` |
| Download w/ resume + integrity | `updater/downloader.py::download_package(url, dest, expected_sha256, headers, timeout, verify_tls)` |
| Signature + freshness | `updater/crypto.py::verify_manifest` / `verify_checksums` / `load_public_key` — same Ed25519 key pair, same PEM files |
| Packaging | tar.gz bundle with `manifest.json` + `manifest.json.sig` + `checksums.sha256` — same builder helpers in `build-release.py` (`sign_manifest`, `sha256_file`) |
| Scheduling | a sibling `zenplus-feedsync.timer/.service` cloned from `zenplus-updater.*` (oneshot; can run as `zenplus`, not root — feeds only write DB rows and a state file, no code) |
| Status surfacing | the `.schema-status.json` / `update-history.json` file pattern + a `GET /api/v1/system/*-status` reader, or plain DB tables since the loader owns Postgres anyway |
| Fleet visibility | add a `feed_status` key to `inventory.collect_inventory()` exactly like `schema_status` (inventory.py:143–166) — zero server changes needed for it to be stored in `appliances.metadata` |
| Entitlement | subscription object already flows on every checkin and is cached verbatim; server adds a key (e.g. `"features": ["compliance"]`), appliance gates the module on it |

### 8.2 Recommended shape: a parallel "feed release" channel (Option A)

**Server (Django), minimal additions — deliberately isomorphic to `releases`:**

```sql
CREATE TABLE feed_releases (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel        VARCHAR(30) NOT NULL      -- 'vuln' | 'patch' | 'eol' (one bundle MAY carry all three)
                   CHECK (channel IN ('vuln','patch','eol','combined')),
    feed_serial    BIGINT NOT NULL,          -- monotonic, e.g. 2026081801 (YYYYMMDDNN)
    schema_version INTEGER NOT NULL DEFAULT 1,
    kind           VARCHAR(10) NOT NULL DEFAULT 'snapshot'  -- 'snapshot' | 'delta'
                   CHECK (kind IN ('snapshot','delta')),
    base_serial    BIGINT,                   -- for deltas: snapshot they apply to
    package_url    VARCHAR(500) NOT NULL,
    package_size   BIGINT,
    package_sha256 VARCHAR(64) NOT NULL,
    is_published   BOOLEAN DEFAULT FALSE,
    published_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (channel, feed_serial)
);
```

**Appliance-facing endpoints (2 new, same auth middleware):**

- `GET /api/v1/feeds/check?channels=vuln,patch,eol&serials={"vuln":2026081101,...}` →
  `{"feeds": [{"channel","feed_serial","schema_version","kind","package_url","package_sha256","size"}]}` — return the newest published snapshot per channel newer than the appliance's serial (plus applicable deltas if implemented). This mirrors `/updates/check` exactly, and because feed bundles are *idempotent full snapshots first*, the known "server returns newest, not next" behavior is harmless here — no ordering problem exists for data snapshots.
- `GET /api/v1/feeds/download/{id}` → stream the bundle, `Accept-Ranges: bytes`, same nginx download rate-limit zone.

Cheaper still (zero new poll loop): **embed the same `feeds` array in the existing `/appliances/checkin` response.** The agent already ignores unknown keys; a small handler added after `checkin()` in `updater/agent.py::main` (or the appliance-side FastAPI service, see below) consumes it. Downside: couples feed cadence to the 4h update timer and puts feed logic inside the root updater; a separate `zenplus`-user timer keeps privileges minimal and cadence independent — preferred.

**Bundle format `.zfd` (identical mechanics to `.zup`, data-only):**

```
manifest.json        # {"format_version":1, "channel":"combined", "feed_serial":2026081801,
                     #  "schema_version":1, "kind":"snapshot",
                     #  "release_date":"<utc iso>",                     ← freshness-checked
                     #  "files":[{"name":"cve.ndjson.gz","sha256":"…","records":123456}, …]}
manifest.json.sig    # Ed25519, SAME zentryc-release.key → existing pubkey on every appliance verifies
checksums.sha256
cve.ndjson.gz        # normalized CVE subset (id, cvss, cpe/vendor-product-version ranges, refs)
kev.json             # CISA KEV flags
patches.ndjson.gz    # patch catalog: product → fixed-in versions / advisory URLs (vendor advisories: Cisco PSIRT, MSRC, USN/DSA…)
eol.json             # EOL/EOS dates per product/train (endoflife.date-style)
```

Two hardening deltas over the `.zup` precedent (both trivial):
1. Put each data file's sha256 **inside the signed manifest** (`files[]` above) so content integrity chains to the signature, closing the `.zup` gap noted in §4.2.
2. Sign with the same key initially (fastest path — every fielded appliance already trusts it), but plan a second `zentryc-feed.pub` distributed via a normal OTA release later, so feed publishing automation (which will run frequently, likely on a server, unlike the manually-guarded release key) doesn't hold the code-signing key. The 30-day `max_manifest_age_days` freshness check comes for free and is *desirable* for a feed ("stale feed" is a real alarm condition); regenerating `release_date` on every publish satisfies it.

**Appliance side:**

- New `updater/feeds.py` (or `server/app/services/feed_sync.py` invoked by a `zenplus-feedsync` oneshot): read `agent.conf` for url/credentials (config.py already parses it) → `GET /feeds/check` with current serials (stored in a `feed_state` PG table or `/opt/zenplus/data/feeds/state.json`) → `download_package()` → `verify_manifest` + per-file sha256 → load into Postgres staging tables → atomic swap/upsert → write `.feed-status.json` (`{channel, serial, records, synced_at, ok, problems}`).
- Timer: clone `zenplus-updater.timer` → `OnBootSec=10min`, `OnUnitActiveSec=6h`, `RandomizedDelaySec=1800`, `Persistent=true`. Runs as `zenplus` (needs only DB + `agent.conf` read; make agent.conf group-readable or add a tiny root-owned copy of just `[server]`+`[appliance]` — note agent.conf is 0600 today, so decide this explicitly).
- Report `feed_status` in `collect_inventory()` (one dict, like `schema_status`) for fleet visibility with zero server change.
- UI: `GET /api/v1/system/feed-status` in the FastAPI app + a card in Settings (mirror `SchemaHealth` rendering), and `POST /api/v1/system/sync-feeds` on-demand trigger (mirror `check-update`'s `sudo -n systemctl --no-block start` pattern — or plain in-process call if the sync runs as the API user).

**Publishing pipeline (ops side):** `scripts/build-feed.py` modeled 1:1 on `build-release.py` (`build` = normalize upstream sources → bundle → sign; `publish` = admin JWT login → `POST /api/v1/admin/feeds/create` multipart → `.../publish`). Runs on a schedule (cron/GH action on the build host), not manually. Rollout staging is *probably unnecessary* for data (start with publish-to-all; the `rollout_policies` pattern can be cloned later if a bad feed ever bricks evaluations — cheap insurance: keep `N-1` snapshot available and let the appliance keep serving its last-good tables on verification failure, which the design above gives naturally since a failed bundle is discarded before load).

### 8.3 Air-gapped / restricted appliances

- **Manual sideload:** dashboard "Upload feed bundle" (admin-gated) → FastAPI saves to a staging path → the same `updater.crypto.verify_manifest`/`verify_checksums` run server-side (the module is importable from the API process; `PYTHONPATH=/opt/zenplus` already) → same loader. Because the bundle is signed, sideloading is exactly as trustworthy as online sync. This is the only path for fully air-gapped sites and costs one endpoint + reusing the verify/load code.
- Freshness: surface "feed age" prominently (serial is date-derived); alert (existing alert engine) when the vuln feed exceeds e.g. 14 days.
- Note the OVA/installer story: like OTA, feed sync activates only after license registration (needs api_key). Sideload should work **without** registration (verify needs only the public key, which ships in every install).

### 8.4 Rejected/secondary options

- **Ship feed data inside ordinary `.zup` releases** (as `install_config` data files, like the agent MSI): zero new machinery, but cadence is wrong (releases are occasional code events; the full update lifecycle stops services, takes a backup, and runs the schema gate for what is just a data refresh), rollout gating would delay vuln data, and a feed-only change would force a version bump. Use only as a bootstrap: seed the *initial* snapshot in a release so day-one evaluation works before first sync.
- **Diff-over-checkin JSON:** embedding actual vuln data in checkin responses — chatty, unbounded body sizes, loses the signed-artifact property. No.
- **Third-party direct pulls (NVD/OSV from the appliance):** contradicts the observed egress constraint (§7) and pushes API-key/rate-limit problems onto every customer. zentryc.com should be the single aggregation point; appliances speak only the existing rails.

### 8.5 Concrete minimal work list

Appliance repo: `updater/feeds.py` (~200 lines, mostly glue over existing modules) + `systemd/zenplus-feedsync.{service,timer}` + `feed_status` in `inventory.py` + `system` API endpoints for status/trigger/sideload + PG migration for feed tables (registered in `scripts/migrations.lock`, classifiable per the OTA gate rules) + Settings UI card.
Server (Django on zentryc.com): 1 model + 3 endpoints (`feeds/check`, `feeds/download/{id}`, `admin/feeds/create|publish`) cloned from the releases implementation; nginx `location /api/v1/feeds/download/` with the download rate-limit zone.
Ops: `scripts/build-feed.py` + a scheduled runner + upstream-source normalizers (separate investigation).

---

## 9. Key facts index (for the plan writer)

1. Base URL default `https://zentryc.com` from `updater/config.py:16` and `[server] url` in `agent.conf`; overridable per appliance.
2. Auth = `Authorization: Bearer <api_key>` + `X-Appliance-ID: <uuid>`, minted once by `POST /api/v1/appliances/register` from a **single-use license key** (`registration_token`); server stores bcrypt hash only.
3. Appliance-facing endpoints today: `POST /appliances/register`, `POST /appliances/checkin`, `GET /appliances/subscription`, `GET /updates/check`, `GET /updates/download/{id}`, `POST /updates/report` — all under `/api/v1/`, all JSON except the octet-stream download.
4. Cadence: systemd timer boot+5min then 4h (`RandomizedDelaySec=300`, `Persistent=true`), interval editable via dashboard → timer drop-in override; on-demand via `systemctl start zenplus-updater.service` under narrow sudo.
5. Trust: Ed25519 signature over `manifest.json` verified against `/opt/zenplus/updater/keys/zentryc-release.pub` (current key b64 `MCowBQYDK2VwAyEAhwZpk2…swF8=`); freshness window −30d/+24h; per-file `checksums.sha256`; whole-package `package_sha256` verified after download; tar path-traversal guard.
6. `.zup` = tar.gz; signature ships inside it; server re-verifies at upload; packages 50–150 MB; download supports Range resume; `package_url = https://zentryc.com/api/v1/updates/download/<uuid>`.
7. Live server is **Django/DRF** (dual error shapes `{"error"|"detail"|"message"}` vs DRF field maps), Cloudflare-fronted, hand-deployed (no git) on `187.77.177.190`; admin JWT (24h) via `POST /api/v1/admin/auth/login`, publish account `admin@zentryc.com` (creds `/root/.zenplus-admin-creds`).
8. Server rollout engine: newest-published-only offers (no stepwise), rollout policy required, deterministic pct bucket `sha256(appliance_id:release_id)%100`, ≤3 failures per release per appliance, auto-abort >5% failures.
9. Update state files the UI reads: `/opt/zenplus/.version`, `.schema-status.json`, `updater/logs/update-history.json`, `update.log`, `updater/config/subscription.json` — root-written, API-read; the feed can clone this pattern or use DB tables.
10. Subscription object (`id,name,plan,max_appliances,max_devices,used_slots,available_slots,is_active,is_expired,expires_at,days_remaining,license{total_node_cap,used_node_count,available_nodes}`) is cached verbatim on every checkin → natural place for a feature/entitlement flag gating the compliance module.
11. Egress reality: some deployments block everything but (at best) zentryc.com; `fetch-geoip.py` (db-ip.com) is deliberately best-effort; public NTP blocked at one site. The feed needs an offline sideload path; signed bundles make that safe.
12. Checkin already reports `node_count`, `schema_status`, `dashboard_build` — adding `feed_status` to `collect_inventory()` (inventory.py:182–196) gives fleet-wide feed observability with zero server schema change (lands in `appliances.metadata`).
