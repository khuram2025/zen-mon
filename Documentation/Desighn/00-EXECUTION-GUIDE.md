# ZenPlus Enterprise Program — Execution Guide

**Master architecture document:** [`zenplus_architect.md`](zenplus_architect.md) (assessment, target topology, rationale).
This folder turns that plan into executable phases. Each phase has its own document, and each task inside a phase carries its own verification criteria, so a team can pick up a task, implement it, prove it, and move on — and a lead can gate the whole phase before the next one starts.

## Documents

| Doc | Phase | Duration | Goal in one line |
|---|---|---|---|
| [PHASE-0-stop-the-bleeding.md](PHASE-0-stop-the-bleeding.md) | 0 | 1–2 weeks | Backups exist and restore; nothing double-fires; nothing anonymous controls the system; updates stop hurting |
| [PHASE-1-single-node-hardening.md](PHASE-1-single-node-hardening.md) | 1 | 3–6 weeks | One node is fast, honest, and correct under load — every silent-loss and blocking path fixed |
| [PHASE-2-ha-pair.md](PHASE-2-ha-pair.md) | 2 | 4–8 weeks | Two active-active controllers + witness; any single node can die with no monitoring loss |
| [PHASE-3-distributed-collection.md](PHASE-3-distributed-collection.md) | 3 | 6–10 weeks | Sharded pollers, hardened sensors, multi-site quorum probes with site-isolation logic |
| [PHASE-4-enterprise-polish.md](PHASE-4-enterprise-polish.md) | 4 | ongoing | SSO/MFA/RBAC, mTLS, DR site, published capacity tiers |

## How to work a task

Every task has this shape:

```
### Px-Ty · Title
Priority | Effort | Depends on
Problem   — what is wrong today, with file:line evidence
Change    — what to implement
Files     — where the work lands
Verify    — the commands/scenarios that PROVE it works. A task is not done
            until every Verify item passes and the evidence (command output,
            screenshot, query result) is attached to the task in the tracker.
Rollback  — how to back out, where it applies
```

**Definition of Done for any task:**
1. Code merged (with tests where the repo has a test home — `server/tests/`).
2. Any schema change shipped as a numbered `scripts/migrate-*.sql` following the existing conventions (`migrations.lock` updated; ClickHouse files named `migrate-NNN-*-clickhouse.sql` so `updater/clickhouse_sync.py` picks them up).
3. Every **Verify** item executed on the staging appliance and evidence recorded.
4. Documentation touched if operator-facing (runbook, README, install.sh).

**Phase gate:** at the end of each phase document is an **Exit Gate** — a drill/soak procedure with measurable pass criteria. The gate is run start-to-finish, results are recorded in a short gate report (template at the bottom of each phase doc), and the next phase does not start until the gate passes. Tasks marked **[blocker]** must be complete before the gate; tasks marked **[parallel-ok]** can trail into the next phase without blocking the gate.

## Priorities, IDs, estimates

- Task IDs are stable: `P0-T1`, `P1-T4`, etc. Reference them in commit messages (`P0-T6: leader-elect background loops`).
- Priorities: **[blocker]** gates the phase · **[high]** should land in-phase · **[parallel-ok]** may trail.
- Efforts are in person-days (pd) for an engineer familiar with the repo; treat as ±50%.

## Environments you need

| Env | What | Used for |
|---|---|---|
| **Dev** | any workstation per `README.md` manual install | unit work |
| **Staging appliance** | 1 VM built with `install.sh` from the release branch (Phase 2+: 2 VMs + witness + 1–2 sensor VMs + a lab "site" VM you can firewall) | all Verify steps and every Exit Gate. Never verify on production |
| **SMTP sink** | Mailpit container (`docker run -p 1025:1025 -p 8025:8025 axllent/mailpit`) configured as the staging email gateway | counting notification emails in drills |
| **Offsite backup target** | S3-compatible bucket (MinIO is fine) or NFS export on a *different* host | Phase 0 backups |

## Ground rules discovered in the assessment (read before touching anything)

1. **Production is currently schema-ahead of git `main`** (branch `feat/apm-phase1`, OTA 1.3.0 with uncommitted migrations 043–051 on disk). Do not run `zenplus update` (it `git reset --hard`s) on the production appliance — P0-T14 removes that footgun.
2. ClickHouse migrations auto-apply on OTA update; Postgres migrations are opt-in per release (`scripts/run-migrations.py`). Both are append-only once released (`scripts/migrations.lock`).
3. Postgres local admin runs as the `postgres` OS user (peer auth); app tables are owned by `postgres`, and ClickHouse SQL applied through Python must escape literal `%` as `%%`.
4. The dashboard is built with `npx vite build` (not `npm run build`); nginx serves `dashboard/dist` and needs `chmod a+rX`.
5. The 2-worker uvicorn setup means every concurrency bug in Phase 0 is reproducible **today** on one box — you do not need two nodes to test leader election.

## Progress tracker

Copy this table into your tracker (or tick it here) — one row per task, filled from the phase docs:

| Task | Title | Owner | Status | Evidence link |
|---|---|---|---|---|
| P0-T1 | Postgres continuous backup (pgBackRest) | | ☐ | |
| … | | | | |

Status values: `todo → in-progress → in-review → verifying → done`. A task sits in `verifying` until its Verify evidence is attached.
