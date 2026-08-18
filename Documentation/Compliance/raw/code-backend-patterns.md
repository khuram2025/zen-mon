# ZenPlus FastAPI Backend Patterns — Raw Investigation for the Compliance Module

Investigator report. All paths absolute; line numbers verified against the working tree at
commit `7dc63b5` (branch `feat/udt-module`, 2026-08-18). Server root: `/opt/zenplus/server`.

---

## 1. Application composition & router registration

### 1.1 Where routers are declared

- `/opt/zenplus/server/app/api/v1/__init__.py` is **empty**. There is no auto-discovery;
  every router module is imported explicitly in `app/main.py` and mounted explicitly.
- Each API module creates one or more `APIRouter` instances at module top level with a
  **module-local URL prefix and OpenAPI tag**, e.g.
  `/opt/zenplus/server/app/api/v1/netpath.py:43`:

  ```python
  router = APIRouter(prefix="/netpath", tags=["Network Path"])
  ```

  Multi-router modules export named routers, e.g. `servers.py:79`
  `baselines_router = APIRouter(prefix="/server-baselines", tags=["Software Baselines"])`
  plus `policies_router`, `fleet_router`, `overview_router` (mounted at
  `main.py:90–94`); `service_checks.py:62–65` exports four routers.

### 1.2 How routers mount (`/opt/zenplus/server/app/main.py`)

- `create_app()` (`main.py:30`) mounts everything under the version prefix:
  `app.include_router(<module>.router, prefix="/api/v1")` — lines 48–113. The final URL
  is `/api/v1/<module-prefix>/<route>`; e.g. NetPath probes are
  `GET /api/v1/netpath/probes`.
- Exception: the APM OTLP receiver mounts at root (`main.py:113`,
  `app.include_router(apm_ingest_api.router)`) so the path is exactly `/v1/traces`.
- Import conventions in `main.py:8–24`: plain modules in a single big import on line 8;
  modules whose names collide with stdlib/other names get aliased
  (`from app.api.v1 import settings as settings_api`, line 9).
- Health endpoint declared inline: `GET /api/v1/system/health` (`main.py:115–117`).

**To add a compliance router**: create `app/api/v1/compliance.py` with
`router = APIRouter(prefix="/compliance", tags=["Compliance"])`, import it in
`main.py` line 8 (or aliased), and add
`app.include_router(compliance.router, prefix="/api/v1")` in the mount block
(after line 84, near udt/netpath is the stylistic home). Endpoints become
`/api/v1/compliance/...`.

### 1.3 Pydantic schemas & SQLAlchemy models

- Newer modules define Pydantic request/response models **inline in the router file**
  (`netpath.py:50–92` `ProbeCreate`/`ProbeUpdate`; `roles.py:43–66`;
  `settings.py:24–99`). `/opt/zenplus/server/app/schemas/` exists (agent.py, alert.py,
  auth.py, device.py, metric.py, ...) but is legacy/shared-only; new modules do not add
  there.
- SQLAlchemy ORM models live in `/opt/zenplus/server/app/models/` (e.g.
  `models/role.py:11–21` uses `Mapped`/`mapped_column`, `UUID(as_uuid=True)`,
  JSON column) — but **most new modules skip the ORM entirely** and use raw
  `sqlalchemy.text()` SQL against the async session (see §4). ORM models are only
  created when `select(Model)` ergonomics are wanted (roles, users, discovery_v2).

---

## 2. RBAC — permissions end-to-end

### 2.1 The dependency

`require_permission` is in `/opt/zenplus/server/app/core/security.py:131–147`:

```python
def require_permission(*permissions: str):
    """Pass when the user's role grants any of the given permissions
    (``system.admin`` always passes)."""
    async def dependency(user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> User:
        perms = await get_role_permissions(db, user.role)
        if SUPERUSER_PERMISSION in perms or any(p in perms for p in permissions):
            return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Insufficient permissions")
    return dependency
```

- Role → permission resolution: `get_role_permissions()` (`security.py:44–70`) reads
  `SELECT permissions FROM roles WHERE name = :name` with a **15-second in-process
  cache** (`_ROLE_CACHE`, `_ROLE_CACHE_TTL = 15.0`, lines 36–37;
  `invalidate_role_cache()` line 40, called by roles CRUD at `roles.py:157,220,259`).
  If the `roles` table is missing (mid-update appliance, tests) it falls back to
  `LEGACY_ROLE_PERMISSIONS`.
- Other gates, still widely used by older modules: `require_admin_user`
  (`security.py:150`), `require_operator_user` (`security.py:165` — passes on role in
  `{"admin","owner","operator"}` or `devices.manage`/`system.admin` permission),
  `require_roles(*roles)` (`security.py:117`), and SSE variants
  `get_current_user_stream`/`require_operator_user_stream` (`security.py:186–220`,
  accept `?token=` because EventSource can't send headers).
- Module-level convention — bind gates once per router file:
  - `netpath.py:45–46`: `VIEW = require_permission("netpath.view", "netpath.manage")`;
    `MANAGE = require_permission("netpath.manage")`
  - `udt.py:87–88`: `udt_reader = require_permission("udt.view")`;
    `udt_user_reader = require_permission("udt.view_users", "udt.manage")`

### 2.2 The permission catalog

`/opt/zenplus/server/app/core/permissions.py` is the **single source of truth**:

- `PERMISSION_MODULES` (line 16): list of
  `(module id, module label, [(permission id, label, description), ...])` — one block per
  product module (dashboard, devices, discovery, alerts, service_checks, netflow, udt,
  netpath, ncm, apm, reports, maps, users, audit, settings, system).
- `ALL_PERMISSIONS` (line 86), `SUPERUSER_PERMISSION = "system.admin"` (line 94),
  `LEGACY_ROLE_PERMISSIONS` (line 108 — hardcoded fallback per legacy role name;
  mirrors the migrate-074 seeds), `is_known_permission()` (line 127 — role CRUD
  rejects unknown ids via `roles.py:76–81 _validate_permissions`), `catalog()`
  (line 135 — grouped catalog for the role-editor UI, served by
  `GET /api/v1/roles/catalog`, `roles.py:115–118`).

### 2.3 The roles table (migrate-074)

`/opt/zenplus/scripts/migrate-074-rbac-roles.sql`:

```sql
CREATE TABLE IF NOT EXISTS roles (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(50) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    permissions  JSONB NOT NULL DEFAULT '[]',
    is_system    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Seeds `admin` (includes `system.admin`), `operator`, `viewer`, `read_only` with
`INSERT ... ON CONFLICT (name) DO NOTHING` and grants DML to the `zenplus` role via a
guarded `DO $$` block. `users.role` stores `roles.name` by value; `users.auth_source`
('local'|'ldap'|'radius') added here too. Roles API: `app/api/v1/roles.py`
(`prefix="/roles"`, CRUD + catalog; system roles immutable; only a `system.admin`
holder may grant `system.admin` — `_guard_superuser_grant`, `roles.py:84`).

### 2.4 Adding a new permission end-to-end (observed procedure + a real gap)

1. Add the ids to `PERMISSION_MODULES` in `core/permissions.py` (new module tuple, e.g.
   `("compliance", "Compliance & Vulnerabilities", [("compliance.view", ...), ("compliance.manage", ...)])`).
2. Add them to the appropriate `LEGACY_ROLE_PERMISSIONS` lists (operator/viewer get view
   perms etc.) — this covers appliances mid-update and test fixtures.
3. Gate routes with `require_permission(...)`.
4. **Gap to know about**: nothing backfills new permission ids into *existing DB role
   rows*. No migration after 074 UPDATEs `roles.permissions` (verified: no
   `UPDATE roles` in any `scripts/migrate-*.sql`; netpath.* and udt.view_users are in
   the vocabulary but absent from the 074 seeds). Admin works anyway because its row
   carries `system.admin`; operators/viewers get new modules only through the OR-gate
   convention — new fine-grained permissions are declared alongside a coarse permission
   the seeded roles already hold (e.g. `require_permission("udt.view_users", "udt.manage")`:
   operator's seeded `udt.manage` satisfies it). NetPath's `netpath.view` is NOT held by
   the seeded operator/viewer rows — operators need a manual role edit to see NetPath.
   **For compliance, ship a replay-safe roles backfill in the module's migration**, e.g.:

   ```sql
   UPDATE roles SET permissions = permissions || '["compliance.view"]'::jsonb
   WHERE is_system AND name IN ('operator','viewer')
     AND NOT permissions @> '"compliance.view"';
   ```

   (UPDATEs are invisible to the replay-safety analyser — only INSERT/CREATE/ALTER/DROP
   are inspected — so this is both safe and gate-friendly; see §5.)

### 2.5 Auth plumbing

JWT bearer auth: `get_current_user` (`security.py:96–114`) decodes
`settings.JWT_SECRET`/`HS256`, loads `User` by `sub`, requires `is_active`. External
auth (LDAP/RADIUS) lives in `app/services/external_auth.py` + `api/v1/auth_settings.py`
(prefix `/system/auth`).

---

## 3. Audit logging

`/opt/zenplus/server/app/services/audit_service.py:12–53` — call from every mutating
endpoint before the final commit:

```python
await write_audit_log(db, actor=user, action="netpath.probe.create",
                      resource_type="netpath_probe", resource_id=str(row.id),
                      metadata={...})
await db.commit()
```

Signature: `write_audit_log(db, *, actor, action, resource_type, resource_id=None,
metadata=None)`. It inserts into `audit_logs`
(`actor_id, actor_username, actor_role, action, resource_type, resource_id, metadata jsonb`)
inside `db.begin_nested()` and swallows all exceptions — audit must never block the
operation. Action-name convention is dotted:
`settings.company.update`, `notification_gateway.create`, `system.refresh_subscription`.

---

## 4. Database access patterns

### 4.1 PostgreSQL — SQLAlchemy async + asyncpg, mostly raw SQL

`/opt/zenplus/server/app/core/database.py`:

- Engine (line 10): `create_async_engine(settings.DATABASE_URL, pool_size=20,
  max_overflow=10, pool_pre_ping=True)`; URL default
  `postgresql+asyncpg://zenplus:changeme@localhost:5432/zenplus`
  (`core/config.py:17`, pydantic-settings `BaseSettings` reading `.env`).
- `AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession,
  expire_on_commit=False)` (line 18). Request dependency `get_db()` (line 27) yields a
  session. Background loops open their own `async with AsyncSessionLocal() as db:`.
- Dominant idiom in new modules: **raw SQL** via
  `await db.execute(text("..."), {params})` with named `:params`; JSONB written as
  `CAST(:value AS jsonb)` with `json.dumps` (see `settings.py:115–123`). asyncpg quirk
  documented at `settings.py:654–658`: bare params arrive typed `unknown`, so
  polymorphic functions need explicit casts (`to_jsonb(CAST(:x AS boolean))`).
- The app role is `zenplus`, but **tables created by migrations are owned by
  `postgres`** (peer-auth applies migrations as the postgres OS user); every migration
  that creates tables includes a guarded GRANT block:

  ```sql
  DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN
      GRANT SELECT, INSERT, UPDATE, DELETE ON <tables> TO zenplus;
    END IF;
  END $$;
  ```

  (e.g. migrate-074:77–81; migrate-016-server-monitoring.sql:358–365). Some tables
  (`alert_rules`) can only be ALTERed by the superuser — noted in migration headers
  (migrate-076 header).

### 4.2 ClickHouse — clickhouse_connect over HTTP, thread-local client + to_thread

`core/database.py:38–79` exposes two clients:

- `get_clickhouse_client()` (line 38): process singleton — fine for serialized use.
- `get_ch_client()` (line 59): **thread-local** client. A single clickhouse_connect
  session forbids concurrent queries, so all async paths run CH work inside
  `asyncio.to_thread(...)`, each worker thread lazily getting its own client. Canonical
  call shape (`api/v1/netflow.py:290–300`):

  ```python
  def _query_sync(sql, params):
      return get_ch_client().query(sql, parameters=params)
  rows = await asyncio.to_thread(_query_sync, sql, params)
  ```

  Parameters use clickhouse_connect `%(name)s` style
  (`netflow_rollup.py:86: AND timestamp >= %(hour)s`). Note the memory gotcha: a
  literal `%` in CH SQL must be written `%%`.
- Connection settings (`core/config.py:20–25`): host `localhost`, HTTP port 8123
  (Python), native 9000 (Go poller), db `zenplus`, user `default`.
- Retention on CH tables is done with **TTL clauses in the DDL**
  (`migrate-071: TTL timestamp + INTERVAL 90 DAY DELETE`), adjusted later by the
  storage-management feature.

### 4.3 KV settings pattern (`system_settings` table)

No migration needed for new settings keys. `system_settings(key, value jsonb)` accessed
via helpers in `/opt/zenplus/server/app/api/v1/settings.py:106–123`:

```python
async def _get_system_setting(db, key) -> Optional[dict]:
    ... text("SELECT value FROM system_settings WHERE key = :key") ...

async def _upsert_system_setting(db, key, value) -> None:
    ... text("INSERT INTO system_settings (key, value) VALUES (:key, CAST(:value AS jsonb)) "
             "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb)") ...
```

Consumers import these helpers directly (security_settings.py:57 —
`from app.api.v1.settings import _get_system_setting, _upsert_system_setting`;
key `security.tls` at line 73). UDT stores module settings under key `'udt'` with a
**merge** upsert (`udt.py:715–724`:
`value = system_settings.value || EXCLUDED.value`). The appliance timezone lives in the
`'company'` blob; read via `get_configured_timezone(db)`
(`app/services/alert_schedule.py:27–37`). Pydantic model with defaults wraps the raw
dict on read (`GrafanaSettings(**(raw or {}))`, `settings.py:183`).

**Compliance settings** (feed URL override, sync cadence, enable/disable, last-sync
cursor) fit a `'compliance'` key in `system_settings` — zero migration cost.

---

## 5. Migration framework

Authoritative doc: `/opt/zenplus/Documentation/18-MIGRATION-RUNNER.md` ("Schema
Convergence", 2026-08-06). Code:

| Path | Role |
| --- | --- |
| `/opt/zenplus/scripts/migration_order.py` | Ordering + static analysis (dependency-free; imported everywhere) |
| `/opt/zenplus/scripts/run-migrations.py` | Postgres runner; ledger table `schema_migrations` |
| `/opt/zenplus/scripts/ch_migrate.py` | ClickHouse runner; ledger `zenplus.schema_migrations` (ReplacingMergeTree — re-insert supersedes, `ch_migrate.py:140`) |
| `/opt/zenplus/scripts/sync-schema.py` | Orchestrator + gate; writes `/opt/zenplus/.schema-status.json`; non-zero exit on drift |
| `/opt/zenplus/updater/schema_gate.py` | Updater wrapper; its verdict decides whether the version marker moves |
| `/opt/zenplus/scripts/build-release.py` | `lint-migrations` subcommand + release manifest builder |

### 5.1 Location & naming

- All migrations live flat in `/opt/zenplus/scripts/` as `migrate-NNN-description.sql`.
  Current highest: `migrate-079-interface-tags.sql` → **the compliance module takes 080+**.
- Engine is derived from the filename: `"clickhouse"` in the stem (or `ch-` prefix) →
  ClickHouse; otherwise Postgres (`migration_order.engine_of`, line 90). Convention:
  `migrate-NNN-foo.sql` + `migrate-NNN-foo-clickhouse.sql` pairs share a number.
- **Order comes from `/opt/zenplus/scripts/migrations.lock`, not filenames** — an
  append-only `sha256  filename` file whose line order is release order (numbers are
  not unique historically). Unlocked files sort after everything locked
  (`migration_order.ordered_migrations`, line 150).
- Registration procedure (18-MIGRATION-RUNNER.md:120–127):

  ```bash
  # 1. write scripts/migrate-080-compliance.sql (and -clickhouse variant if needed)
  python3 scripts/build-release.py lint-migrations --update-lock
  # 3. commit migration + scripts/migrations.lock together
  ```

### 5.2 Classification / probe gate (what happens on each appliance)

For every migration not in the ledger both runners probe the DB
(18-MIGRATION-RUNNER.md:57–74):

| Ledger | Objects present | Action |
| --- | --- | --- |
| recorded | yes | skip |
| not recorded | yes | **baseline** — record without running |
| not recorded | no | **apply**, then record |
| recorded | no | **heal** — ledger is lying; re-apply (ClickHouse) |

Two refuse-to-guess cases fail the gate as `unresolved`: partially-present +
not-replay-safe, and **nothing probeable + inserts rows**
(`run-migrations.py:307–314`).

Evidence rules (`run-migrations.py:207–234 evidence_of`): probeable evidence =
**created tables/views/dictionaries** (`migration_order.created_objects`, line 190),
**added columns** (`added_columns`, line 228, matched as `column:table.column`), and
**created indexes** (`created_indexes`, line 250). **Constraints are deliberately NOT
evidence** (nine migrations recreate `alert_rules_metric_check` under one name —
comment at `run-migrations.py:221–227`). Temp tables are excluded
(`migration_order.py:50–57`).

### 5.3 Replay safety (`migration_order.is_replay_safe`, line 301)

Guarded idioms recognized: `IF NOT EXISTS`/`IF EXISTS` DDL;
`DROP CONSTRAINT IF EXISTS x` followed by `ADD CONSTRAINT x`;
`INSERT ... ON CONFLICT` / `INSERT ... WHERE NOT EXISTS`. A bare `INSERT` is never
replay-safe (`writes_rows`, line 270 — the reason unverifiable+inserting migrations
fail the gate). `DO $$` blocks containing DDL must carry their own
`IF [NOT] EXISTS` probe (lines 78–87). Comments/dollar-quoted/single-quoted content is
stripped before analysis (`analyzable`, line 176) — a semicolon inside a seed string
once broke classification fleet-wide (comment at lines 70–76).

### 5.4 Build-time lint (`build-release.py`)

`lint_migrations` (line 422) rejects: an edit to an already-released migration
(git-history checksum drift — `_lint_migration_history`, line 328; reconcile via
`SUPERSEDED_CHECKSUMS` in run-migrations.py, never re-edit); a reused sequence number;
a deleted shipped migration; and **a new ClickHouse migration that is not replay-safe**
(line 413–416) — healing a lying CH ledger works by re-running the file.

### 5.5 What makes a migration OTA-fleet-safe (checklist distilled)

1. Every DDL statement guarded (`IF NOT EXISTS` / `DROP ... IF EXISTS` + re-ADD).
2. Every seed `INSERT` carries `ON CONFLICT`/`WHERE NOT EXISTS`.
3. The file creates at least one probeable object (table/column/index) **or** performs
   only guarded, verifiable work — a migration that only INSERTs rows and creates no
   probeable object is unclassifiable and fails the gate fleet-wide (memory:
   `migration-must-be-classifiable`).
4. Never edit a shipped migration (checksum gate strands appliances); supersede with a
   new file (see migrate-076 superseding migrate-073's metric list "verbatim + append").
5. No long backfills in migrations — the updater has statement timeouts. The
   netflow-rollup precedent (`migrate-071` header, lines 14–18): the migration only
   creates objects; **the API server backfills idempotently at startup** in an
   advisory-locked loop (`app/services/netflow_rollup.py`).
6. GRANT to `zenplus` inside the guarded `DO $$` block for every new Postgres table.
7. Wrap Postgres migrations in `BEGIN; ... COMMIT;` (074, 075 do; note evidence_of's
   comment that files without a wrapper can fail midway).
8. Release integration: `build-release.py` emits a `run_hook` for `sync-schema.py` on
   every release, after `apply_code`, before services start; failure → manifest
   rollback; version marker moves last.

### 5.6 Reference migrations to copy from

- **Feature-table Postgres migration**: `migrate-075-netpath.sql` — header documents
  every table, all-new guarded tables, denormalized latest-state columns on the parent
  row, partial indexes (`CREATE INDEX ... WHERE enabled`), `created_by UUID REFERENCES
  users(id) ON DELETE SET NULL`, `BEGIN/COMMIT`.
- **Widen-a-CHECK migration**: `migrate-076-netpath-alert-metrics.sql` — canonical
  alert-metric list, superuser-owned table, constraint drop/re-add.
- **ClickHouse rollup**: `migrate-071-flow-conversations-rollup-clickhouse.sql` —
  `AggregatingMergeTree` + `MATERIALIZED VIEW ... TO`, TTL retention, no backfill.
- **Roles/RBAC**: `migrate-074-rbac-roles.sql`.

Local testing: apply as the `postgres` OS user (peer auth); CH literal `%` must be `%%`
(memory: `applying-postgres-migrations-locally`). Scratch-schema SQL testing trick noted
in memory `udt-classification-feature`.

---

## 6. Background jobs

### 6.1 How jobs start

**Everything is an asyncio task started in the FastAPI startup hook** —
`main.py:119–188` `@app.on_event("startup")`:

```python
from app.services.udt_sweeper import udt_sweeper_loop
app.state.udt_sweeper = asyncio.create_task(udt_sweeper_loop())
```

18 loops today (health sweeper, capture sweeper, discovery scheduler, host/network/APM/
SLO/synthetic/UDT/NetPath alert evaluators, report scheduler, storage sweeper, netflow
rollup, UDT sweeper, UDT AD poller, netpath enrichment, managed-device sync, APM service
registry/graph). Shutdown cancels by attribute name — the tuple at `main.py:204` must
list every `app.state` attr. **No cron/systemd timers for app jobs** (systemd timers
exist only for the updater: `updater/systemd/zenplus-updater.timer` `OnUnitActiveSec=4h`,
and `zenplus-support-cleanup.timer` `OnCalendar=daily`).

### 6.2 The loop skeleton (uniform across services)

```python
async def <name>_loop() -> None:
    await asyncio.sleep(20)            # let the app settle (15–20s everywhere)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("<name> failed")
        await asyncio.sleep(TICK_INTERVAL_S)
```

(`udt_sweeper.py:342–353`, `report_scheduler.py:463–476`,
`discovery_scheduler.py:131–150`, `netflow_rollup.py:264–273`.)

### 6.3 Multi-worker safety — Postgres advisory locks

Uvicorn runs multiple workers; every worker starts every loop. Two patterns:

1. **Transaction-scoped advisory lock** (preferred; `udt_sweeper.py:307–339`):

   ```python
   got = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                           {"k": UDT_SWEEP_ADVISORY_LOCK})).scalar()
   if not got:
       await db.rollback(); return False
   ...work...
   await db.commit()   # releases the xact lock
   ```

   The docstring (lines 311–318) explains why session-scoped locks leak with pooled
   connections. `netflow_rollup.py` takes a fresh short transaction (fresh lock) per
   healed hour (lines 233–241).
2. **Row-level `FOR UPDATE SKIP LOCKED`** on due schedule rows
   (`report_scheduler.py:430–435`, `discovery_scheduler.py:67–77`) — prevents
   double-firing without a global lock.

Advisory-lock key registry (all `15150743xx`):

| Key | Constant | File |
| --- | --- | --- |
| 1515074384 | CAPTURE_SWEEP_ADVISORY_LOCK | network_capture_service.py:38 |
| 1515074385 | CAPTURE_PURGE_ADVISORY_LOCK | network_capture_service.py:39 |
| 1515074390 | STORAGE_SWEEP_ADVISORY_LOCK | storage_service.py:61 |
| 1515074391 | UDT_SWEEP (udt_sweeper.py:42) **and** SYNTHETIC_SWEEP (apm_synthetic_service.py:63) — **collision** |
| 1515074392 | NETFLOW_ROLLUP (netflow_rollup.py:55) **and** UDT_AD (udt_ad_service.py:33) **and** CHILD_SYNC (managed_device_service.py:51) — **triple collision** |
| 1515074393 | SERVICE_GRAPH_ADVISORY_LOCK | apm_service_graph.py:38 |
| 1515074394 | NETPATH_ENRICH_LOCK | netpath_enrichment.py:26 |

Collisions only cause skipped ticks (try-lock), but the compliance module must claim
**unused keys — 1515074395 (feed sync) and 1515074396 (CVE match)** and ideally note the
existing collisions to the team.

### 6.4 Scheduling idioms by cadence

- **Fast periodic tick (60 s)**: `TICK_INTERVAL_S = 60` — report_scheduler.py:39,
  discovery_scheduler.py:28, udt_sweeper.py:34.
- **Hourly-ish / heavy sweep**: `SWEEP_INTERVAL_S = 6 * 3600` netflow_rollup.py:48 —
  interval loop + idempotent catch-up work (compare rollup vs raw row counts, heal
  missing hours, bounded per sweep: `MAX_HEALS_PER_SWEEP = 6`, line 53).
- **Daily-at-a-time (user-facing schedules)**: `report_schedules` rows carry
  `frequency/hour/minute/day_of_week/day_of_month`, `next_run_at`, `last_run_at`,
  `last_status`, `last_error`; `compute_next_run(sched, tz_name)`
  (report_scheduler.py:52–99) evaluates in the appliance timezone from
  `get_configured_timezone(db)`; the tick selects
  `WHERE enabled AND next_run_at <= :now FOR UPDATE SKIP LOCKED`, runs, then writes
  `next_run_at` (run_scheduler_tick, lines 427–460).
- **Daily-inside-a-fast-loop**: udt_sweeper's `_capacity_snapshot`
  (udt_sweeper.py:272–289) runs every 60 s but is idempotent per day via
  `INSERT ... (device_id, CURRENT_DATE, ...) ON CONFLICT (device_id, day) DO UPDATE`.
- **One-time recovery at startup**: discovery's `recover_stuck_runs`
  (discovery_scheduler.py:115–129) marks orphaned runs failed once, before ticking.
- **Retention pruning inside the module's own sweeper**: `_prune`
  (udt_sweeper.py:292–305), `RETENTION = "90 days"`.

### 6.5 Recommendation mapping for compliance jobs

- **Hourly feed-sync (zentryc.com)**: interval loop, `SWEEP_INTERVAL_S = 3600`,
  transaction-scoped advisory lock (netflow_rollup shape), httpx.AsyncClient with the
  appliance credential headers (§7), cursor/etag persisted in `system_settings`
  key `'compliance'` (or a `compliance_feed_state` table). Best-effort external
  download precedent with retry-window backoff: OUI seeding
  (`udt_sweeper.py:56–118` — `OUI_RETRY_S = 6*3600`, `urllib` in `asyncio.to_thread`,
  minimum-row sanity check before commit).
- **Nightly CVE-match**: either (a) the report_scheduler pattern if per-user
  configurability is wanted (schedule row + hour/minute + tz), or (b) — simpler and
  consistent with the module — a 5-minute tick loop whose work is gated by an
  `ON CONFLICT (day) DO NOTHING`-style ledger or a `last_matched_at` check in the KV
  blob, running the match when local time passes the configured hour
  (`get_configured_timezone`). Evaluation itself should follow
  `baseline_service.evaluate_*` (§8.4): pure-Python matching, one outcome row per
  (asset, rule/CVE), raise/resolve alerts.

---

## 7. Talking to zentryc.com (feed sync transport)

- Appliance identity/config: `/opt/zenplus/updater/config/agent.conf` (INI), read by the
  server API at `system_updates.py:25`
  (`CONFIG_PATH = UPDATER_DIR / "config" / "agent.conf"`), sections
  `[server] url` (default `https://zentryc.com`, `updater/config.py:16`),
  `[appliance] id`, `[appliance] api_key`, `[update] auto_update`, etc.
  File chmod 0600 (`system_updates.py:645`).
- **Auth headers** for authenticated appliance endpoints
  (updater `agent.py:78–89 _api_headers`, and server-side
  `system_updates.py:822–826`):

  ```
  X-Appliance-ID: <appliance_id>
  Authorization: Bearer <api_key>
  User-Agent: zenplus-updater/<version>
  ```

- Existing central endpoints: `POST {server_url}/api/v1/appliances/register`
  (body: hostname, arch, os_version, current_version, registration_token —
  `system_updates.py:613–624`; returns `appliance_id`, `api_key`, `subscription`) and
  `POST {server_url}/api/v1/appliances/checkin` (body: hostname, arch,
  current_version, node_count — `system_updates.py:819–832`; returns subscription +
  release advice; check-in also reports `schema_status` and `dashboard_build`
  fleet-wide per 18-MIGRATION-RUNNER.md:114–118).
- Server-side calls use `httpx.AsyncClient(timeout=30, verify=True)`;
  `httpx.RequestError → HTTPException(502, "Cannot reach update server")`
  (`system_updates.py:856–857`); remote error extraction helper
  `_extract_remote_error` (line 118).
- A **compliance feed endpoint** on zentryc.com (e.g.
  `GET /api/v1/appliances/vuln-feed?since=<cursor>`) should reuse exactly this header
  auth and config source. Note: zentryc.com is a Django app on 187.77.177.190
  (memory: `zentryc-kb-publishing`); public NTP/some egress is blocked on-prem — feeds
  must tolerate long offline gaps (the OUI download precedent already handles this).
- Subscription/plan gating precedent: `subscription.py:23–28 PLAN_LIMITS`,
  `:30 PLAN_FEATURES` (trial/starter/professional/enterprise) — if compliance is a
  plan-gated feature, this is where limits live; cached subscription JSON is saved by
  `_save_subscription` (system_updates.py).

---

## 8. Existing assets the compliance module builds on

### 8.1 Network devices — identity & firmware columns (Postgres `devices`)

- Base table: `scripts/init-postgres.sql:31–67` — `id UUID`, `hostname`,
  `ip_address INET UNIQUE`, `device_type` CHECK
  (router|switch|firewall|server|access_point|printer|other), `tags JSONB`, `status`,
  `last_seen` ...
- SNMP identity added by `scripts/migrate-004-snmp.sql:9–23`:
  `sys_object_id VARCHAR(255)`, `vendor VARCHAR(100)`, `model VARCHAR(255)`,
  **`os_version VARCHAR(255)`**, `profile_id UUID` (+ indexes `idx_devices_vendor`,
  `idx_devices_sys_object_id`).
- `os_version` is populated by the monitoring-template engine: templates in
  `migrate-062-monitoring-templates.sql` carry `sys_object_id_prefixes`,
  `sys_descr_regex` and **`extract_os_version`** regexes per vendor (Cisco IOS/IOS-XE
  line 263–265, FortiGate line 95–98, ASA 360–362, Palo Alto 430, F5 519...). This is
  the firmware string CVE matching will key on, alongside `vendor`/`model`/
  `sys_object_id`.
- Managed children (controller-reported APs/switches) add `serial_number`,
  `managed_source`, `poll_mode` (`migrate-069:31–38`).

### 8.2 Servers — OS + installed software inventory

- `servers` table (`scripts/migrate-030-server-monitoring.sql:22–48`): `os_type`
  (windows|linux|macos|bsd|other|unknown), **`os_name varchar(255)`,
  `os_version varchar(128)`, `kernel_or_build varchar(128)`, `architecture`**,
  `collection_mode`, `status`, `tags jsonb`, `site_id`, `device_id` link.
- **`server_software_inventory`** (`migrate-030:263`, also `migrate-016:293–301`):

  ```sql
  server_id UUID REFERENCES servers(id) ON DELETE CASCADE,
  package_name VARCHAR(255) NOT NULL,
  version VARCHAR(128), vendor VARCHAR(255), install_date TIMESTAMPTZ,
  updated_at TIMESTAMPTZ, PRIMARY KEY (server_id, package_name)
  ```

  Refreshed by agent inventory ingest —
  `app/services/host_metric_service.py:471–498` upserts it. Read paths:
  `api/v1/servers.py:729,736`.

### 8.3 An in-tree compliance engine already exists (software baselines)

`app/services/baseline_service.py` — "Software-baseline (compliance) evaluation
engine": required/prohibited package rules scoped by os_type/site/tags, evaluated
against `server_software_inventory`, one outcome row per (server, rule) in
`server_baseline_results`, raises/resolves alerts via
`server_health_service.create_server_alert`/`resolve_server_alerts`. Key reusable
pieces for CVE work:

- `compare_versions(a, b)` (line 54) + `_version_key` (line 38) — tokenized
  mixed-numeric/alpha version comparison ('1.2.10' > '1.2.9', '1.2' == '1.2.0').
  Directly reusable for "affected version range" checks.
- `match_package(package_match, match_type, package_name)` (line 70) —
  exact|contains|regex matching.
- `evaluate_server` (line 152), `evaluate_baseline` (line 259), `evaluate_all`
  (line 291) — the evaluate/store/alert loop shape.

Tables (`migrate-030:308–362`): `software_baselines`, `software_baseline_rules`
(rule_type required|prohibited, match_type exact|contains|regex, min_version,
severity info|warning|critical), `server_baseline_results`
(status compliant|missing|outdated|prohibited, found_package, found_version,
first_failed_at, PK (server_id, rule_id)). API: `servers.py` `baselines_router`
(`/api/v1/server-baselines`, endpoints at lines 2930–3073 incl.
`POST /{baseline_id}/evaluate`). The compliance module should mirror this shape:
`compliance_vulns` (feed cache), `compliance_matches` (asset×CVE outcome rows),
`compliance_eol` — and may either extend or sit beside the baseline engine.

### 8.4 Alert integration

New alert metric keys (e.g. `compliance_critical_vuln`, `compliance_eol_reached`)
require a **new** migration that supersedes migrate-076 verbatim + appends (the
contract stated in migrate-076's header; `alert_rules` is superuser-owned;
constraint-only files always re-apply cleanly). Evaluator loops that raise/resolve
follow `netpath_alert_service.py`/`udt_alert_service.py` (started at
`main.py:174–184`). Notification wording is owned by
`app/services/alert_phrasing.py` (memory: `alert-message-phrasing`).

---

## 9. Concrete recipe — adding the compliance module

### 9.1 Files to create

1. **Migration** `/opt/zenplus/scripts/migrate-080-compliance.sql` (Postgres; next free
   number after 079). Contents pattern (all guarded, BEGIN/COMMIT):
   - `CREATE TABLE IF NOT EXISTS compliance_vulns` (feed-cache: cve_id PK or
     UNIQUE, cvss, severity, description, affected JSONB, published_at, modified_at,
     source, fetched_at) — feed rows are inserted at runtime by the sync job, **not**
     seeded in the migration (keeps it probeable + replay-safe, netflow-rollup
     precedent §5.5.5).
   - `CREATE TABLE IF NOT EXISTS compliance_matches` (asset_kind
     CHECK ('device'|'server'), asset_id UUID, cve_id, status
     CHECK ('affected'|'patched'|'dismissed'|'false_positive'), matched_version,
     recommended_fix, first_seen, last_evaluated, dismissed_by/at,
     `PRIMARY KEY (asset_kind, asset_id, cve_id)` — mirrors
     `server_baseline_results`).
   - `CREATE TABLE IF NOT EXISTS compliance_eol` (vendor, product_match, cycle,
     eol_date, source...).
   - Partial/lookup indexes `CREATE INDEX IF NOT EXISTS ...`.
   - Roles backfill UPDATE from §2.4 (guarded with `NOT permissions @> ...`).
   - Guarded `DO $$` GRANT of SELECT/INSERT/UPDATE/DELETE on the new tables to
     `zenplus` (copy migrate-074:77–81).
   Then `python3 scripts/build-release.py lint-migrations --update-lock` and commit the
   lockfile with it. ClickHouse is only needed if per-day vuln-count history at scale is
   wanted (then `migrate-080-compliance-clickhouse.sql`, replay-safe, TTL retention).
2. **Alert metrics** (optional, separate file): `migrate-081-compliance-alert-metrics.sql`
   — restore migrate-076's list verbatim, append `compliance_*` keys (superuser-applied,
   constraint-only).
3. **Permissions**: edit `/opt/zenplus/server/app/core/permissions.py` — new module
   tuple in `PERMISSION_MODULES` (after "ncm" or "apm" block, ~line 54) with
   `compliance.view` / `compliance.manage`; add `compliance.view`+`compliance.manage`
   to `_OPERATOR` (line 96) and `compliance.view` to viewer/read_only lists
   (lines 113–123).
4. **Router** `/opt/zenplus/server/app/api/v1/compliance.py` — head copied from
   netpath.py:26–47: `router = APIRouter(prefix="/compliance", tags=["Compliance"])`;
   `VIEW = require_permission("compliance.view", "compliance.manage")`;
   `MANAGE = require_permission("compliance.manage")`; inline Pydantic models; raw
   `text()` SQL; `write_audit_log(...)` + `await db.commit()` on every mutation;
   suggested endpoints `GET /summary`, `GET /vulns`, `GET /assets`,
   `GET /assets/{kind}/{id}`, `POST /matches/{...}/dismiss`, `GET /eol`,
   `GET|PUT /settings` (KV blob), `POST /sync-now`, `POST /evaluate-now`.
5. **Service** `/opt/zenplus/server/app/services/compliance_service.py` (matching:
   reuse `baseline_service.compare_versions` and `match_package`) and
   `/opt/zenplus/server/app/services/compliance_sync.py` (feed loops). Loop constants:
   `FEED_SYNC_INTERVAL_S = 3600`, `CVE_MATCH_TICK_S = 300` (+ nightly-hour gate),
   advisory keys `1515074395`/`1515074396`; loop bodies per §6.2/§6.3; httpx client with
   agent.conf credentials per §7.
6. **Wiring** in `/opt/zenplus/server/app/main.py`: import (line 8 region), mount
   (`app.include_router(compliance.router, prefix="/api/v1")`, after line 84), two
   `asyncio.create_task` startup entries assigned to
   `app.state.compliance_feed_sync` / `app.state.compliance_matcher`
   (startup block, after line 188), and both attr names appended to the shutdown tuple
   at `main.py:204`.
7. **Tests** in `/opt/zenplus/server/tests/` (`test_compliance_*.py`; conftest.py
   overrides `get_db`/`get_current_user` with fakes — `tests/conftest.py:14–17`; live
   integration tests must target a running uvicorn on :8001 per memory).

### 9.2 Sequencing constraints

- Migration must land in the same release as the code (release builder ships all
  migrations + runs sync-schema before services start; version marker moves last).
- Feed-cache tables empty on install is the designed state; the hourly sync fills them.
- `main` is the release line — the release must fast-forward it
  (memory: `main-branch-is-the-release-line`).

---

## 10. Gotcha digest (things that will bite)

1. Empty `api/v1/__init__.py` — forgetting the `main.py` import/mount silently 404s.
2. Shutdown tuple at `main.py:204` is a hand-maintained list; a missing attr leaks the
   task on reload.
3. Advisory-lock keys already collide (1515074391 ×2, 1515074392 ×3) — pick fresh keys.
4. New permissions do **not** reach existing DB role rows without a migration UPDATE or
   the OR-with-coarse-permission gate convention.
5. Role permission edits take up to 15 s to apply (`_ROLE_CACHE_TTL`).
6. asyncpg `unknown`-typed params: CAST jsonb/boolean explicitly.
7. clickhouse_connect: thread-local client + `asyncio.to_thread`; `%%` for literal `%`.
8. Migrations: never edit shipped files; bare INSERTs are unclassifiable; constraints
   are not probe evidence; long backfills belong in startup healers, not SQL files.
9. `snmp_credentials` passwords are plaintext in Postgres (memory:
   `udt-settings-feature`) — never echo through compliance asset endpoints.
10. External feeds must be best-effort with retry windows (appliances may lack general
    egress; NTP is already blocked on this network) — cache aggressively, tolerate
    stale.
