# 04 — Roadmap, Phases, Tasks & Test Plan

*Status: designed 2026-10-07. This doc is the implementation status of record. Phase 0 is merged and Phase 1 is built, reviewed, re-checked and fixed; the owner closed both and paused the roadmap there on 2026-10-09 (next section). Both are unreleased and ship together as one release that is planned, not yet merged, tagged or published, with migrations 157, 158 and 159; section 2 has the details, the verification and the measured numbers. Updated 2026-10-09 after the re-check of the Phase 1 fix round. Rows tagged P2 to P7 are plans: nothing in them is in this release.*

## Status board and how to resume

*Written 2026-10-09, when the owner paused the roadmap after Phase 1. Every statement about the release below is **planned** unless it says "done": nothing is merged, tagged or published yet, and whoever does it updates this section and the table in `00-INDEX.md`.*

### Where each phase stands

| Phase | State on 2026-10-09 | What exists | Waits for |
|---|---|---|---|
| P0 Foundations and change capture | **Built and merged to `main`** (PR #40, `139363b`, 2026-10-08). Ships together with P1 in one release (planned); not released dark, because 1.23.34 and 1.23.35 carried other work | Migration 157, ingest hardening, normaliser and loops, poller change, the `inventory.*` permissions | Reference-appliance acceptance (below) |
| P1 Inventory dashboard MVP | **Built, reviewed, re-checked and fixed**, then closed by the owner. On branch `claude/inventory-phase1-dashboard` as local checkpoint commits to be squashed; the pull request, merge, tag and publication are **planned** | Eight pages, the read API and export, `05-P1-API-CONTRACT.md`, the knowledge-base sources, migrations 158 and 159, role definition version 6 | The pull request with its five CI checks, the release, reference-appliance acceptance |
| P2 Catalog, history UI, trends, asset attributes, reports | **Not started. Paused by the owner on 2026-10-09** | Nothing beyond what P1 already pulled forward (Changes page, trends endpoint, minimal Settings) | P1 released; nothing external |
| P3 Coverage expansion | **Not started. Paused** | Nothing | The `ZenPlus_Agent` repository for tasks 3.1 to 3.3 (**external**: a new agent release and, when an installer is bundled, a signed Windows installer) |
| P4 Lifecycle intelligence | **Not started. Paused** | Nothing | P1. The signed refresh of lifecycle data needs the zentryc feed channel or a signed `.zvb` bundle (**external**); the release-time endoflife.date snapshot is built by this repository's release builder |
| P5 Compliance policies and frameworks | **Not started. Paused** | Nothing | P2 and P4 |
| P6 Vulnerability correlation | **Not started. Paused** | Nothing | P2 and P4, and the zentryc service that builds the CVE feed (**external**, `Documentation/Compliance/07`) |
| P7 Backlog | Not scheduled; each item needs its own spec | Nothing | A decision |

### What ships in the release that carries P0 and P1 (planned)

- **Version:** 1.24.0 is the proposal; the number is set when the release is tagged. The release is an appliance-only signed OTA package: Windows agents and installers are untouched.
- **Migrations 157, 158 and 159**, in that order of `scripts/migrations.lock`. Confirm the numbers against `origin/main` before the pull request opens: the local copy of `origin/main` (`139363b`) ends at 157, and `git fetch` is needed to see anything newer.
- **Permissions and roles:** `inventory.view`, `inventory.export` and `inventory.manage`; built-in role definition version 6 (view for the network, systems and security teams); custom roles unchanged and to be granted by hand.
- **Documents:** the release notes of `06-RELEASE-NOTES-DRAFT.md` become `docs/release-<version>.md`; the knowledge-base sources in `inventory-kb/` are published on release day, after **all ten screenshots are retaken**.
- **Process:** squash the checkpoint commits, open the pull request, merge only after the five required CI checks and conversation resolution, then the release marker pull request, the tag, the clean build, offline verification, publication to the **canary** group, and a **full rollout only after the reference appliance has accepted the exact signed package** (`/home/zen/releases/README.md`, `Documentation/28-RELEASE-PATH-RUNBOOK.md`).

### What is still open for that release's acceptance

None of these has been done; `.221` is not the reference appliance.

1. Identity coverage on SNMP devices with a profile: vendor at least 95 percent, OS version at least 90 percent, model at least 90 percent.
2. Agent-ingest p95 latency unchanged (within 5 percent) while the normalise loop runs, and the measured lock time of migration 157 and run time of the 158 repair on the canary.
3. Zero change events caused by the upgrade itself.
4. The real built-in role reconcile from definition version 4 to 6, and the live check that a viewer role is refused on every mutating Inventory route.
5. A browser pass of the Radix menus (the Devices facets and the Inventory selects) against a real backend; so far they were checked only in the mock harness and by server-side rendering.
6. The owner's UAT (T18): the four exit questions in three clicks each, with matching totals.
7. The N-1 to N upgrade and a fresh install through the schema gate (T16), and the end-to-end access test `integration/test_access_control_e2e.py` against a disposable API (its team-role assertions have never been executed).
8. The canary stage before the full rollout.

### First actions when work resumes

1. Read `07-PHASE1-FOLLOWUPS.md` first: every finding that was not fixed, with its severity and the phase that should take it. Take the items marked for the next phase together with that phase's own tasks.
2. Bring the tree up to date and re-run the checks from the repository root, using the server's Python (`server/requirements.txt`): `python scripts/dev/inventory-contract/build_contract.py --check` and `python scripts/dev/inventory-contract/check_contract.py` (both must pass; if a fixture or a constant changed, rebuild `05` with `build_contract.py` and never edit it by hand), `python scripts/build-release.py lint-migrations`, the server suite, and from `dashboard/` `npm run build`, `npm run smoke:routes`, `scripts/test-nav-access.mjs`, `scripts/test-role-model.mjs` and the four inventory scripts (`test-inventory-ui.mjs`, `test-inventory-os-hardware-changes.mjs`, `test-inventory-software-settings.mjs`, `test-inventory-touchpoints.mjs`). Then re-read the last lines of `scripts/migrations.lock` on `origin/main`: every planned migration of P2 to P6 takes the next free number, not the placeholders in this document.
3. **P2 starts with tasks 2.1 and 2.2** of section 2: the catalog tables (migration N+1) with the catalog service (rule precedence, revision bump and re-key, preview, unmatched queue), then the Settings > Software catalog UI. The normaliser items of `07` (HP Inc. words, MikroTik model, fallback OS names, Azure Local release) belong with them, because they are rule-pack and catalog work.
4. Decide what the owner has not decided (next table) before the phase that needs it starts.
5. Keep the process of the first line of this document: a branch, a pull request with the five required checks, migrations appended to the lock, a signed release, acceptance on the reference appliance.

### Owner decisions still open

| # | Decision | Recommended default | Needed before |
|---|---|---|---|
| D5 | Linux coverage path | **Agentless SSH first** (task 3.4, reuses `ncm_credentials`); a Linux agent GA stays in the agent roadmap | P3 |
| D6 | Lifecycle data source | **A bundled endoflife.date snapshot per release plus a signed refresh**; the appliance never calls out to it directly | P4 |
| D7 | Accept the amendments to the Compliance design set (`00-INDEX.md`, reconciliation table) | **Yes**. They are applied in these documents but the owner has not confirmed them | P4, P5 and P6 |
| D8 | Ship P0 dark in the next patch release | **Moot**: it did not happen (1.23.34 and 1.23.35 carried other work); P0 ships with P1. Close it | none |

D1 to D4 and D9 are implemented as the recommended defaults and can still be reversed (section 7).

### The review tooling is outside the repository

The adversarial review (110 findings), its re-check (98 fixed, 7 partly fixed, 4 deferred, plus new findings) and the helper scripts that print one finding with its verifier write-ups and reproduction commands (`p1-finding.py`, `p1-recheck-finding.py`, and the result files they read) live in the author's Claude work directory, `/home/zen/.claude/projects/-home-zen-development-zen-mon--claude-worktrees-inventory-compliance-dashboard-4b2e17/work/`, together with the clickable prototype, the mock harness and the scratch databases' scripts. That directory is not backed up and is lost when it is cleaned. What matters has been written down here: `07-PHASE1-FOLLOWUPS.md` lists every finding that remains open, and the paragraph *The review and its fix round* in section 2 summarises the rest.

---

*Estimates are in engineering days for one engineer working with Claude Code. They include tests and docs but not review wait time.*

*Every phase ships through the normal workflow:*
- *a branch, then a PR with all five required CI checks (server, dashboard, poller, agent-ubuntu, agent-windows);*
- *migrations appended to `scripts/migrations.lock`;*
- *a signed OTA release through `zentryc-release`;*
- *acceptance on the reference appliance.*

*Integration tests use disposable Postgres/ClickHouse fixtures, never `/opt/zenplus` databases.*

---

## 1. Phase map

```
 P0 ───────► P1 ───────► P2 ───────► P3 ───────► P4 ───────► P5 ───────► P6 ──► P7
 Foundations Inventory   Catalog,    Coverage    Lifecycle   Compliance  CVE     Backlog
 & capture   dashboard   history UI, expansion   (EOL/EOS)   policies &  corre-
 (dark)      MVP         trends,     (agent,     intelli-    frameworks  lation
             ◄ the ask   reports     Linux, KB)  gence
 ~3 wk       ~5 wk       ~3.5 wk     ~4.5 wk     ~3 wk       ~4.5 wk     Compliance set
```

**Critical path:** P0 → P1.

- P2 and P3 can overlap: P3's agent work lives in the `ZenPlus_Agent` repo.
- P4 needs only P0 and P1.
- P5 needs P2 (catalog + asset meta) and P4 (lifecycle).
- P6 needs P2 and P4.

**The one rule that sets the order:** P0 ships first, as soon as possible, inside a normal release and without UI. Change history can't be backfilled, so every day earlier is a day of evidence kept.

---

## 2. Phases

### P0 — Foundations & change capture (dark ship, no UI) · ≈ 14 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 0.1 | `migrate-157-inventory-foundation.sql` (doc 02 §3.2, §3.3, §5, §6): `inventory_identity`, evidence columns, `inventory_change_events` with triggers and suppression GUC, `inventory_daily_snapshots`, baselined-at seed, indexes, GRANTs. Plus lockfile entry and `lint-migrations` | db | 2 | — |
| 0.2 | `services/versioning.py` (comparators: win_build, semverish, fortios, panos, cisco_ios/iosxe, junos) and `services/inventory_normalize.py` (OS, network OS, vendor, software name). Rule packs `software_rules.json`, `vendor_aliases.json`, `os_patterns.json`. Golden fixtures cover every row of the tables in doc 02 §4 plus the live values seen on .221 | api | 4 | — |
| 0.3 | Ingest hardening (`host_metric_service._upsert_inventory`): keep architecture, source and install_location; inline keys and category; same-name dedupe; truncation guard (no prune at ≥ 1,000); stamp `inventory_updated_at` / `inventory_baselined_at`; normalise the server identity inline | api | 1.5 | 0.1, 0.2 |
| 0.4 | Poller (Go): persist `sys_descr`; stamp `identity_updated_at` on every successful system-info poll. In P0 `os_family` is derived by the normaliser from profile name, then sysObjectID prefix, then sysDescr (`os_patterns.json`); an explicit `device_profiles.os_family` column is deferred because built-in profiles are seeded by shipped migrations that must not be edited. Verify the PAN/F5 template-value precedence end to end (no Go change needed for it) | poller, content | 2 | 0.1 |
| 0.5 | `inventory_normalize_loop` (2 min, lock 1515074397, batches of 500, `asyncio.to_thread`, suppression on re-normalise) and `inventory_snapshot_loop` (daily 00:15, lock 1515074398, retention purge). Registered in `main.py` startup and shutdown lists | api | 1.5 | 0.2 |
| 0.6 | RBAC: `inventory` module in `PERMISSION_MODULES`; `_OPERATOR`/`_VIEWER`/`_READ_ONLY` lists; `PERMISSION_IMPLIES`; `PERMISSION_RISK["inventory.export"]="sensitive"`; `BUILTIN_ROLE_DEFINITION_VERSION = 5`; `MODULE_AREAS["inventory"]`; `_POLICIES["inventory"]`; `test_permissions_model.py` and `integration/test_access_control_e2e.py` `READ_ONLY_PERMS` updated | api | 1 | — |
| 0.7 | Integration tests on a disposable Postgres fixture: trigger and suppression behaviour (T3–T9 below). The fixture (`server/tests/pg_fixture.py`) provisions its own private cluster (initdb in a temp dir, loopback port, template database from a full migration replay), so the tests also run in the CI `server` job on runners that ship PostgreSQL binaries, and skip where they don't | test | 2 | 0.1–0.5 |

**Exit criteria**

- Change events accumulate on the reference appliance with **zero events from the upgrade itself**.
- `inventory_identity` is populated for 100% of assets.
- Identity coverage is measured and reported in the PR. Targets on SNMP devices with a profile: vendor ≥ 95%, OS version ≥ 90% (PAN/F5 now filled from templates), model ≥ 90%.
- Agent-ingest p95 latency is unchanged (± 5%).

**Status (2026-10-08): built and reviewed, pending the Phase 0 pull request and reference-appliance acceptance.** Tasks 0.1 to 0.7 are implemented on `claude/inventory-phase0-foundations`. Verification on a clean checkout: full server suite 3,931 passed and 274 skipped with the PostgreSQL-backed tests required; migration lint passes; poller tests, `go vet` for the touched packages and `go test -race` pass; the loop tests passed 8 out of 8 repeated runs (the cancel tests used to hang about 1 in 10).

A six-reviewer adversarial review (44 agents, every medium-or-worse finding independently verified) found no data-loss bug, but several ways to **write false history**: a rule-pack revision bump producing a fleet-wide false OS change (fixed with `inventory_identity.values_rev`), a transient missing version writing two false events, capped lists leaving stale rows, out-of-order snapshots rewinding state, and silent normaliser failures stamping rows clean. All are fixed with regression tests; the remainder are recorded in doc 02 section 14. Still to do for the exit criteria: measure identity coverage and the ingest latency on the reference appliance, and confirm the migration on a canary (it holds exclusive locks on `devices`, `servers` and `server_software_inventory` for about 0.5 to 1.5 seconds at 5,000 servers by 400 packages, with services stopped by the OTA flow).

**Release coupling:** migration 157, the ingest hardening (0.3) and the poller change must ship together. Without the ingest hardening the triggers would record false events from duplicate names and capped lists. The Phase 1 review fix round added two forward migrations that complete Phase 0's data: 158 (the capture start, the freshness of servers that had already reported, the first-seen repair) and 159 (`audit_logs.actor_role` width).

### P1 — Inventory dashboard MVP (the request) · ≈ 24 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 1.1 | `services/inventory_query.py`: asset union with server↔device dedupe, filters, facets, health predicates, tag scoping, 60 s summary cache per scope key | api | 3 | P0 |
| 1.2 | `api/v1/inventory.py` read endpoints: summary, assets, facets, asset flyout, os, software, publishers, product, installations, hardware, changes (read), settings GET/PUT. `GET /inventory/export` with CSV streaming, injection guard and audit. Mounted with `policy("inventory")` | api | 4 | 1.1 |
| 1.3 | UI shell: nav group (doc 03 §1.1; P1 rows only), routes, `InventoryAccessGate`, `InventoryLayout`, `InventoryMetaLine`, `components/inventory/*` primitives, `requiredRoutes` update, **nav-target validation test** in the dashboard CI job | ui | 2 | — |
| 1.4 | Overview page: KPIs, OS panel, network firmware panel, top software, version sprawl, hardware, recent changes, inventory health, security-agent coverage, empty/onboarding states | ui | 2.5 | 1.2, 1.3 |
| 1.5 | Assets page: facets, server-side table, column chooser, flyout | ui | 3 | 1.2, 1.3 |
| 1.6 | Operating Systems page: tree table, kind switch, build-currency card | ui | 1.5 | 1.2 |
| 1.7 | Software catalog and product page (Versions, Installed on, Evidence tabs; the Timeline tab is built too, because the Changes data and page moved into P1) | ui | 3 | 1.2 |
| 1.8 | Hardware page (network vendors/models, models × firmware matrix, modules, server CPU/RAM/disk buckets) | ui | 1.5 | 1.2 |
| 1.9 | Existing-page touchpoints: Devices list Vendor/Model/OS columns and filters (+ `GET /devices` vendor/os params), device firmware link, server Software links and truncated banner, Server Overview OS tile link, "All Servers" relabel | ui+api | 1.5 | 1.2 |
| 1.10 | Tests: `test_inventory_route_guards.py`, scope tests (T10–T12), export injection (T13), perf fixture (T15), dashboard drill-down equality script (T-UI2), strict build, smoke | test | 2 | 1.1–1.9 |
| 1.11 | KB articles (`zentryc.com/kb/zenplus/inventory`, one per tab), release notes (announce the new permissions and that **custom roles need `inventory.view`**), `.env.example` unchanged (no new secrets) | docs | 1 | — |

**Exit criteria**

- Each of these questions takes ≤ 3 clicks from the Overview, and the drilled list's total equals the clicked number:
  - "How many servers run Windows Server 2019, and which builds?"
  - "Which hosts run Chrome older than 150?"
  - "Which FortiGates run 7.2.x?"
  - "Which devices are missing a serial?"
- A scoped user sees only in-scope assets on every endpoint (T10–T12).
- An export of 10k rows completes in < 5 s and is audit-logged.
- All five CI checks are green, and acceptance passes on the reference appliance.

**Scope decisions for Phase 1** (made when the phase was planned; they override older wording in this set where they differ; `05-P1-API-CONTRACT.md` section 1 carries S1, S3, S4 and S6 in its own words):

| ID | Decision |
|---|---|
| S1 | P1 ships `/inventory`, `/assets`, `/os`, `/software`, `/software/:productKey`, `/hardware`, `/changes` and `/settings`. **Changes moves from P2 to P1** because the data has accumulated since Phase 0. **Settings is minimal**: only the *Data & retention* tab; catalog rules, lifecycle and saved views stay in later phases. No Lifecycle or Compliance rows |
| S2 | Sidebar group id `inventory`, label *Inventory & Compliance*, short label *Inventory* (it was *Assets* in the first design; the group's rows are named *Assets* too), after `servers` and before `apm`. The Servers row *Inventory* is relabelled *All Servers* |
| S3 | `missing_model`, `missing_serial` and `unknown_vendor` apply to **network devices only** (servers have no hardware identity until P3). `missing_security_agent` and `software_truncated` apply to servers only. `missing_os_version`, `stale` and `never` apply to both |
| S4 | `GET /inventory/trends` exists; KPI sparklines show only when at least **three days** of snapshots exist, with no placeholder otherwise, and never to a scoped user |
| S5 | Built-in team roles `network_team`, `systems_team` and `security_team` get `inventory.view` (view only; owner recommendation D9). `application_team` gets nothing; no team role gets export or manage. `BUILTIN_ROLE_DEFINITION_VERSION` 5 → 6 |
| S6 | The address of every list page carries its filters under the **same names as the API parameters** of the matching endpoint |
| X1 | Review round. The software category `security` stays broad; a new category `endpoint-protection` holds only antivirus, EPP and EDR engines. The filter `security_agent` (`present` \| `missing` \| `unknown`) and the health key `missing_security_agent` keep their pinned names but are defined on that category plus a running `WinDefend` or `Sense` service. A truncated, absent or not yet normalised software list is `unknown`, never `missing`. All copy says "Endpoint protection". The ZenPlus Agent is filed under `monitoring` |
| X2 | `data_status` (`normalising`, `pending_software_rows`, `pending_identity_rows`) on the summary and the software responses, scope-aware; a banner on every Inventory page while it is true |
| X3 | Freshness has a fifth value, `not_applicable`, for assets that cannot report by design (a server whose collection mode is not the agent and that never reported; a device that is ping-only or controller-managed and never reported). Counted and drillable, excluded from the fresh percentage, from `never`, from `stale_or_never`, from the four identity predicates and from the all-clear; `health.not_collected_by_design` counts them |
| X4 | `GET /changes` returns `history_start` and `retention_days`; the flyout returns `changes.first_change_at` so that *View all N* lands on N |
| X5 | `runtime` joins the default hidden software categories; the toolbar label follows the configured list |

**Status (2026-10-09): built, reviewed, re-checked and fixed on branch `claude/inventory-phase1-dashboard` (local checkpoints, to be squashed for the pull request), pending the Phase 1 pull request, reference-appliance acceptance and the release; the owner closed the phase and paused the roadmap.** Phase 1 itself added no table and no new dependency; its review fix round added migrations 158 and 159 (section 2, P0 release coupling). What was built, per task:

| Task | Built as | Verified by |
|---|---|---|
| 1.1 | `services/inventory_query.py` (asset union with the server↔device dedupe on the link only, one filter vocabulary, facets merged case-insensitively, the eight health predicates and the `not_applicable` freshness, the endpoint-protection state (`present`, `missing`, or `unknown` for one of four reasons), versions by equivalence key, tag scoping inside the union, the 60-second summary cache per scope, `data_status`, OS tree, hardware, changes, flyout) and `services/inventory_software_query.py` (software catalog with its per-scope tables and background warmer, publishers, product, installations). Reads run with JIT off and custom plans (`_tune`), and the change feed is asset-driven for scoped users (`02` section 14) | `test_inventory_query.py`, `test_inventory_software.py`, `test_inventory_api.py`, `test_inventory_fix_*.py` |
| 1.2 | `api/v1/inventory.py`: summary, assets, facets, flyout, os, hardware, changes, software, publishers, product, installations, trends, settings (GET and PUT) and export (streaming CSV, formula-injection guard, 200,000-row cap, audit before the first byte and refusal when it cannot be stored, one export per user and two in all, each counted per API worker process, and the appliance runs two). Mounted with `policy("inventory")`; `GET /export` declares `inventory.export` and `PUT /settings` declares `inventory.manage`. Contract: `05`, generated from the code and the golden fixtures by `scripts/dev/inventory-contract/` | `test_inventory_route_guards.py`, `test_inventory_export.py`, `test_inventory_fix_export_audit_stream.py`, `test_inventory_trends_settings.py`, `test_inventory_fixtures.py` |
| 1.3 | Nav group and tab strip from one definition, eight routes, `InventoryAccessGate`, `InventoryLayout`, `InventoryMetaLine`, the shared components of `03` section 7, `requiredRoutes`, and the **nav-target validation** (every `to:` in `navigation.ts` resolves; mutation-checked). Role definition version 6 with the D9 grants. Inventory Settings needs only `inventory.view` (read-only without `inventory.manage`) | `scripts/test-nav-access.mjs`, `scripts/test-inventory-ui.mjs`, `smoke:routes` (48 routes), `test_permissions_model.py` |
| 1.4 | Overview: six KPI cards with sub-links, sparklines under S4, server OS donut, network firmware bars, top software, version sprawl (labelled when only the most installed products were ranked), hardware, recent changes, the eight-item health panel plus the not-reported-by-design line, the endpoint-protection line with its unknown count, the normalising banner, onboarding and empty states | `test-inventory-ui.mjs`; reviewed in a browser against the mock harness in light and dark at 1440 and 390 px |
| 1.5 | Assets: facets (drawer on a phone), URL-bound filters, server-side table with a column chooser, the flyout (identity with source and age, server and device bodies, recent changes), export | `test-inventory-ui.mjs`, keyboard flow checked in a browser |
| 1.6 | Operating Systems: kind switch, family tree, family donut and **Build currency** per release | `test-inventory-os-hardware-changes.mjs` |
| 1.7 | Software catalog and product page with all four tabs, N / N-1 / Older generations per version scheme, hidden categories and unmatched footers | `test-inventory-software-settings.mjs` |
| 1.8 | Hardware: network vendors and models with serial coverage, models x firmware matrix, ENTITY-MIB module groups, server CPU, memory and disk distributions | `test-inventory-os-hardware-changes.mjs` |
| 1.9 | Existing-page touchpoints: the *All Servers* relabel (with 1.3, also the heading and the Server Overview button); Devices list Vendor, Model and OS version columns (opt-in) and Vendor and OS version filters with the matching `GET /devices` parameters, `facets=true`, `os_version_display` and CSV columns appended at the end; the device OS / firmware value linking to the Inventory Assets list of the same release (to `/inventory/assets`, not `/inventory/os`, which honours only `?kind=`); the server Software tab linking each package to its catalog product, with the truncated-list banner (`GET /servers/{id}/software` returns `product_key`, `category` and `agent_truncated`); and an *OS inventory →* link on the Server Overview OS tile. Details and what stays text: `03` section 4 | `test_inventory_touchpoints.py` (32), `scripts/test-inventory-touchpoints.mjs` |
| 1.10 | Route guards (T14), scope (T10 to T12), export injection (T13), the performance run (T15) and the drill-down equality tests, server and browser halves (T-UI2); strict build and route smoke (T-UI1) | see the test status table in section 4 |
| 1.11 | The knowledge-base article **sources** in `inventory-kb/` (twelve articles, one per `KB_ARTICLES` key; not published; their ten screenshots were taken before the review fix rounds and **all ten need retaking**, see `inventory-kb/README.md`), and `06-RELEASE-NOTES-DRAFT.md` (migrations, roles, first hour, how to go back, checklist, known limitations, support). `.env.example` is unchanged (no new secrets) | A documentation check script kept outside the repository: every relative link, route, parameter, permission, migration number, API path and article key in the documents resolves against the code; the draft release notes list what to confirm before publishing |

Added to Phase 1 beyond the original task list: the Changes page and the product Timeline tab (from P2), the trends endpoint and KPI sparklines (from P2), the minimal Settings page, the golden fixtures under `dashboard/src/pages/inventory/fixtures/` that keep `05` and the dashboard types in step with the API (`server/tests/test_inventory_fixtures.py` fails on drift), the `*.typecheck.ts` files that make the strict build fail when a type drifts from the contract, and `scripts/dev/inventory-contract/`, the generator that writes `05` from the code and the fixtures (its `--check` and `check_contract.py` run in the server CI job).

**The review and its fix round.** Seven adversarial reviewers produced 110 findings, each verified independently; the confirmed ones were fixed in four waves (query and normaliser; software, export, audit, servers and devices backend with the shared frontend layer; software, product and settings pages with the existing-page touchpoints; documentation and the contract generator) and each fix carries a regression test named after its finding. The product changed in the ways the decisions X1 to X5 above describe, plus: versions are counted by equivalence key (`7.2` is `7.2.0`); facet values merge case-insensitively; `vendor=unknown` and `model=unknown` select devices with none; the Hardware page counts known models only and reports devices that cannot report separately; the Changes feed carries `history_start`; the software catalog has a background warmer; the export is admission-controlled and refuses to run without its audit entry; settings writes are serialised; `GET /servers/{id}/software` is tag-scoped; the Devices list can show Vendor, Model and OS version from the normalised identity; Visual C++ redistributables are separate products per year; HP Inc. and HPE are separate vendors; Hyper-V, Azure Stack HCI, ESXi, Proxmox and further Linux distributions are recognised. Three findings were deliberately not fixed: UI-05 (a defect of the review mock, not of the product), UP-10 (servers that stopped reporting still count in software figures; documented as a limitation, see the open items) and UI-16 (forced-colors support).

**The re-check and round 2.** Independent agents then re-checked every finding of the review against the fixed tree and reviewed the new code: 98 findings were verified fixed, 7 partly fixed and 4 deliberately deferred, and the new code produced further findings, each judged by a skeptic. Round 2 fixed those that belong to this release, again with a regression test named after each: one total natural-sort key (a version with a superscript digit or a very long digit run can no longer crash a sort); the `since` and `until` parameters of Changes and export parsed in one place (an unparseable or out-of-range date is a `422`, and the export answers it before it takes an export slot); one eligibility rule shared by the normalise loop, the backlog count of `data_status`, the endpoint-protection joins, the flyout and the snapshot (X10); the fourth unknown reason `no_service_list` (X9); a rule-pack re-render replacing a stored OS block (a MikroTik board name stored as the version by an earlier build is cleared without an event); rule pack revision 3; the equivalence key of a version; one bucket rule for the OS page and its CSV; and, on the frontend, recovery from a failed lazy chunk after an update (one automatic reload per five minutes, then a notice), the search-box races, the chip focus rules, honest percentages (99.95 reads 99.9%; 100.0% only when exactly 100), accessible names, contrast, the banner's live region, the Devices page names and empty states, and the trim rule for filter values. What was not fixed is in `07-PHASE1-FOLLOWUPS.md`. Two pre-existing problems outside the module were filed as separate tasks and are not part of this release: the missing tag-scope check of `GET /server-baselines/{id}/results`, `GET /agent-fleet` and the sibling `GET /servers/{id}/…` routes, and the failure of the Services daily bars and reports for browser time zones the host cannot open (`06` section 8).

**Verification.** Recorded by the orchestrator at the checkpoints of the fix rounds (the figure for the final tree replaces them when the pull request is opened): the baseline of this branch before Phase 1 was **4,926 passed, 274 skipped**; after fix wave 2 the full server suite was **5,912 passed, 274 skipped** with the PostgreSQL-backed tests required; at the round 2 checkpoint it was **6,033 passed, 274 skipped, 0 failed**, with the strict dashboard build, `smoke:routes` over **48 routes** (40 before Phase 1), the dashboard node scripts (those of the CI job and the four inventory scripts), and the contract generator's `--check` and `check_contract.py` green, and the golden fixtures and `05` regenerated. `lint-migrations` passes with 157, 158 and 159. The Phase 1 server test files collect these tests (`test_inventory_query.py` 142, `test_inventory_software.py` 102, `test_inventory_api.py` 89, `test_inventory_export.py` 124, `test_inventory_fixtures.py` 33, `test_inventory_scope.py` 27, `test_inventory_trends_settings.py` 52, `test_inventory_route_guards.py` 19, `test_inventory_touchpoints.py` 32) and the two fix rounds added thirteen `test_inventory_fix_*.py` files (442 tests: normalizer 139, changes and scope 54, export audit and stream 33, numbers 37, software 30, loops 19, endpoint protection 17, migration 158 9, freshness 9, summary concurrency 5, coverage percentage 4, and the two round 2 files, backend 75 and endpoint protection 11). Not run: the end-to-end access test in `integration/test_access_control_e2e.py` (it needs a disposable API; its team-role assertions are unexecuted), anything against the reference appliance, and a browser pass of the Radix menus against a real backend (the node tests render markup and the menus were checked only in the mock harness and a headless browser against it).

**Measured performance** at the sizing target (5,000 servers x about 400 packages = 1.8 to 2 million software rows, 10,000 devices, 1,000,000 change events; scratch PostgreSQL 16 with default settings on a 4-CPU shared host; full tables in `05` section 6.5 and a summary against the budgets in `02` section 13). The numbers are from the code after the fix round:

- The asset list takes 34 / 37 ms (p50 / p95), the facets 93 / 101 ms, the OS tree 29 / 31 ms, hardware 48 / 53 ms and a flyout 6.5 / 10 ms. The summary is 230 to 265 ms (p50) cold for an unrestricted caller, about 255 ms for a scope of 40 percent of the estate, and 0.3 ms cached; the `data_status` backlog count is 1.7 ms.
- `/changes` takes 15, 89 and 199 ms (p50) for 7, 90 and 400 days; the 400-day window over the whole retention is within the 400 ms budget now (248 ms p95).
- **The misses:** the software catalog on a process-cold request (477 / 556 ms unrestricted, 458 / 530 ms for a scope of 63 percent, p50 / p95; publisher facet 573 / 617 ms), deep and large pages (page 5: 558 ms, `page_size=500`: 802 ms) and the sorts that need a figure for every product (`hosts` ascending 657 ms; about 1.1 s and 3.3 s on a catalog of 24,000 products). Warm requests are 15 to 20 ms: a background warmer keeps the whole-estate table and the publisher facet hot. On a tenth of this fleet every such request is under 150 ms.
- Exports: 14,750 assets in 1.7 s, 50,000 change events in 1.4 s (the exit criterion is 10,000 rows in under 5 s).
- Round 2 did not repeat the full-estate benchmark harnesses (they are not in the repository). Its backend agent measured only the window right after an upgrade, while unkeyed software rows exist: the backlog count and the endpoint-protection joins cost somewhat more per call there (the figures are in `07-PHASE1-FOLLOWUPS.md`, section 8); in the steady state (no unkeyed rows) the partial index is empty and nothing changed. The figures above are from before round 2.

**Exit criteria, as of 2026-10-09:**

| Criterion | State |
|---|---|
| The four questions in three clicks or fewer with matching totals | Designed and tested for totals. *Windows Server 2019 and its builds*: Overview OS legend, then the Assets list, or Operating Systems expanded to versions. *FortiGates on 7.2.x*: the firmware bar on the Overview, or Hardware, Fortinet, the matrix. *Devices missing a serial*: one click on the health panel. *Chrome older than 150*: Overview, Chrome, the *Installed on* tab sorted by version, because P1 has no *older than* filter (the vocabulary has an exact `product_version` only). The owner's UAT on the reference appliance (T18) is open |
| A scoped user sees only in-scope assets on every endpoint | Met in tests: `test_inventory_scope.py` and the scoped golden fixtures cover summary, assets, facets, flyout (404 outside the scope), os, software, product, installations, hardware, changes, trends (no series) and export; the fix round also scoped `GET /servers/{id}/software` |
| An export of 10k rows completes in under 5 s and is audit-logged | Met: 14,750 assets in 1.7 s; audit written before the first byte and the export refused when it cannot be stored (`test_inventory_export.py`, `test_inventory_fix_export_audit_stream.py`) |
| All five CI checks green and acceptance on the reference appliance | Open: they follow the pull request |

**Open items after Phase 1** (none blocks the release; the first five are release steps, the rest are limitations that the release notes state and inputs to P2):

| Item | State / where |
|---|---|
| CI wiring | **Done.** `.github/workflows/development.yml` runs the four inventory node scripts in the dashboard job and the contract generator's `--check` and `check_contract.py` in the server job |
| Reference-appliance acceptance | **Open.** Identity coverage (vendor 95 percent, OS version 90 percent, model 90 percent), agent-ingest p95 while the normalise task runs, zero events from the upgrade itself, the v4 to v6 role reconcile, a browser pass of the Radix menus. A separate reference appliance is required; `.221` is not it |
| Migration numbers against `origin/main` | **To confirm** when the pull request opens: `scripts/migrations.lock` on `main` ended at 157 when the fix round started; 158 and 159 are appended here |
| Knowledge-base screenshots | **Open.** All ten were taken before the review fix rounds and differ from the screens now (rail label, line under the tabs, endpoint-protection wording, hidden categories, donut fold, product crumb, Settings); retake all before publishing (`inventory-kb/README.md` lists the difference per file) |
| Real-backend pass of the existing-page touchpoints | **Open.** The Devices facets and the Inventory selects were verified in the mock harness and by server-side rendering only |
| Servers that stopped reporting count in software figures | **Open (UP-10, deliberately not done).** No staleness setting in P1; documented in `06` section 8 and `02` section 14; deleting a decommissioned server removes it |
| A server and its SNMP device are two assets unless linked | **Open (UP-05).** Automatic pairing is not implemented; the link comes from a discovery import or the API |
| Sprawl exactness above 400,000 visible rows, cold restricted catalog, time zone database, role descriptions, forced-colors support | **Open**, each listed in `02` section 14 |
| Tag scope of `/server-baselines/{id}/results`, `/agent-fleet` and the sibling `GET /servers/{id}/…` routes | **Closed for reads, outside the module**; the baseline list counts, the capture detail routes and the `servers.manage` actions are still unscoped; stated in `06` section 8 |
| Services daily bars and reports for browser zones the host cannot open | **Open, outside the module and older than it**; filed as a separate task; stated in `06` section 8 |
| Every other finding that was not fixed (export slot edge cases, accessibility contrast of the flyout badge and chip prefix, normaliser breadth, facet values with a trailing tab or no-break space, the age of twin-supplied values, the controller child source label, documentation nits) | **Open**: one table in `07-PHASE1-FOLLOWUPS.md`, with severity, reason and the phase that should take it |
| Product page breadcrumb ends in *Detail* | **Closed.** The crumb names the product |
| The *All Servers* page heading and Server Overview button | **Closed.** They read *All Servers* and *View all servers* |
| `05` generated outside the repository | **Closed.** `scripts/dev/inventory-contract/` generates it (`README.md` there) |
| Cold software catalog, presence check without an index, history-start probe, `VersionDistributionBar` shares | **Closed** in the code (warmer, `idx_servers_device_id`, `history_start`, `total` prop), see `02` section 14 |
| Software empty-state sentence about Linux, the switch label, the security-software figure | **Closed.** The empty state says Linux and agentless servers report no software, the label follows the configured categories, endpoint protection is its own category (X1, X5) |

### P2 — Catalog normalisation, history UI, trends, asset attributes, reports · ≈ 16 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 2.1 | Migration N+1 (planned; the next free number in `scripts/migrations.lock`): the catalog tables of `00-INDEX.md`. Catalog service: rule precedence, revision bump and re-key, preview, unmatched queue, "report inaccuracy" pre-fill | db+api | 3 | P1 |
| 2.2 | Settings → Software catalog UI: rules with preview, products, publishers, unmatched queue | ui | 3 | 2.1 |
| 2.3 | ~~Changes page; product Timeline tab~~ **Moved to P1** (S1). Remaining: change mini-lists on the device and server pages (the asset flyout already lists the last ten changes of an asset) | ui | 1 | P1 |
| 2.4 | ~~`GET /inventory/trends` (scope-refused) and Overview sparklines~~ **Moved to P1** (S4). Remaining: the 30/90-day trend charts beyond the four KPI sparklines | ui | 1 | P0 snapshots |
| 2.5 | Asset meta API and flyout editing (owner, department, business service, criticality, lifecycle state, purchase/warranty, mark reviewed), with facets and Assets columns | api+ui | 2 | 2.1 |
| 2.6 | Saved views (personal and shared) on Assets, Software and Changes | api+ui | 1.5 | 2.1 |
| 2.7 | Report sections (`inventory_overview`, `_os_distribution`, `_software_summary`, `_hardware_models`, `_changes`), extended `inventory` preset, new `inventory_audit` preset, `_REPORT_TYPES`, schedule smoke test | api+ui | 2.5 | P1 |

**Exit criteria**

- Unmatched software is below 5% of installations on the reference fleet.
- A local rule fix regroups titles within one loop cycle, with zero change events.
- A weekly inventory PDF is delivered by email on schedule.

### P3 — Coverage expansion · ≈ 21 d (the agent tasks run in the `ZenPlus_Agent` repo in parallel)

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 3.1 | Agent `hardware_identity_v1` capability: Win32_ComputerSystem, Win32_ComputerSystemProduct and Win32_BIOS (manufacturer, model, serial, BIOS version/date), virtualisation detection, TPM and Secure Boot state. Ingest into the `servers.hw_*` columns. Gated by `_agent_supports` | agent+api | 3 | P0 |
| 3.2 | Agent `patch_inventory_v1`: QFE/Get-HotFix plus the Windows Update history (KB, title, installed_on). `server_patch_inventory`. Server-page "Updates" sub-tab and a fleet "Updates" view under Software | agent+api+ui | 4 | P0 |
| 3.3 | Agent: lift the 1,000-entry cap (raise to 5,000 and set an explicit `truncated` flag in the batch). Server honours the flag | agent+api | 1 | — |
| 3.4 | **Linux agentless inventory over SSH**, reusing `ncm_credentials` (the cross-module SSH store): `/etc/os-release`, `dpkg-query -W` / `rpm -qa --qf`, `lscpu`, `/proc/meminfo`, `dmidecode` when permitted. Scheduled every 6 h per server with `collection_mode='ssh'`, bounded concurrency, and results written through the same ingest path (`source='dpkg'/'rpm'`) | api | 6 | P0 |
| 3.5 | Migration N+2 (planned): `server_patch_inventory`; widen the `server_software_inventory` key to `(server_id, source, package_name, architecture)` with a data move under the suppression GUC | db | 2 | 3.2 |
| 3.6 | Poller: walk `entPhysicalSoftwareRev` and `entPhysicalMfgName` (OIDs already defined at `oids.go:59,61`); module firmware on the Hardware page | poller | 2 | P0 |
| 3.7 | UDT endpoints as asset kind `endpoint` (OUI vendor, type, authorized). KPIs for **unauthorised assets** (CIS 1.2) and **discovered but not monitored** (links to `/discovery/imports`) | api+ui | 3 | P1 |

**Exit criteria**

- Server manufacturer, model and serial are known for ≥ 95% of agent servers.
- At least one Linux server is fully inventoried over SSH on the reference lab.
- The KB list matches `Get-HotFix` exactly on the test VMs.

### P4 — Lifecycle intelligence (EOL/EOS) · ≈ 13.5 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 4.1 | Migration N+3 (planned): `eol_definitions`, `inventory_lifecycle_overrides`, and an alert-metric constraint supersede (`inventory_eol_reached`, `inventory_eol_within_90d`). The base constraint owner must be re-verified at implementation | db | 1.5 | P1 |
| 4.2 | Release-time builder: fetch the endoflife.date `/products/full` snapshot (MIT, about 3 MB) during the clean build, record its sha256 in the signed release inventory, then load it on upgrade. Mapping from identity/catalog to `(slug, cycle)` via pack rules and endoflife.date `/identifiers` | build+api | 3 | 4.1 |
| 4.3 | Refresh between releases: through the zentryc feed channel (Compliance doc 06) when available, otherwise a signed `.zvb` lifecycle bundle upload. The appliance never calls endoflife.date directly. **Signature verification is mandatory** | api+server | 3 | 4.2 |
| 4.4 | Lifecycle page; stage pills and EOL columns on OS, Software and Assets; Overview EOL KPI; lifecycle colouring of the firmware bars; N / N-1 using feed-latest | ui | 3 | 4.2 |
| 4.5 | Overrides UI (reason, expiry) and hardware EoS/EoL CSV import; optional Cisco EoX API connector when entitled credentials exist (credentials stored with existing credential encryption, never in the repo) | api+ui | 2 | 4.1 |
| 4.6 | Alerts (EOL reached, EOL ≤ 90 d), the `inventory_lifecycle` report section, and the PCI 12.3.4 annual-review report | api | 1 | 4.4 |

**Exit criteria**

- Every OS and firmware release in use on the reference fleet shows a stage or an explicit *Unknown* with an override path.
- Lifecycle works with no internet egress.

### P5 — Compliance policies & frameworks · ≈ 22 d

| # | Task | Area | Est | Depends |
|---|---|---|---|---|
| 5.1 | Migration N+4 (planned) for the policy tables, and the engine (doc 02 §10.2): rule types, scope resolution, evaluation on ingest, identity change and daily, results with latched `first_failed_at`, snapshots | db+api | 6 | P2, P4 |
| 5.2 | Exceptions: requested → approved (`compliance.approve`, new permission, admin-only default) → expired; reason, owner, expiry; audit trail | api | 2 | 5.1 |
| 5.3 | Baselines → policies migration with a parity test (identical outcomes on the fixture fleet). `/server-baselines` redirect; nav row moves | api+ui | 2 | 5.1 |
| 5.4 | Posture overview, Policies list and editor with "would fail N assets" preview, per-policy results | ui | 5 | 5.1 |
| 5.5 | Exceptions register; Frameworks page (CIS v8.1 1.1/1.2/2.1/2.2/2.3/2.5, NIST CSF 2.0 ID.AM-01/02/08, ISO 27001 A.5.9/A.8.19, PCI DSS 4.0 12.5.1/12.3.4/6.3.3); **audit pack** export (snapshot CSVs + results + exception register + change log, for a date range) | api+ui | 4 | 5.2 |
| 5.6 | Alerts on transitions (dedupe `policy:{rule}:{asset}`), compliance report sections | api | 2 | 5.1 |
| 5.7 | Surface NCM config-compliance on Posture as a summary card linking to the NCM Compliance workspace (no duplicate page) | ui | 1 | 5.4 |

**Exit criteria**

- An auditor walkthrough of CIS 2.1–2.3 on the reference fleet is possible entirely from the UI and the exported audit pack.
- Baseline parity is 100%.

### P6 — Vulnerability correlation

Executes `Documentation/Compliance/07` CV-E1 → CV-E3, with the amendments in `00-INDEX.md` (shared catalog, asset meta, comparators, lifecycle; their migrations take the next free numbers after the Inventory ones, N+5 and later, planned).

Sizing is per that document: three M-sized phases, including the zentryc feed builder.

### P7 — Backlog (own specs when scheduled)

- License position (entitlements vs installs) and software metering
- CIS Benchmarks / SCAP-OVAL config hardening (with the NCM config-compliance packs)
- CMDB sync and export (ServiceNow, CSV/REST webhooks) and a public inventory API
- macOS agent inventory
- MSP multi-tenant reporting and branding
- AI copilot grounding ("which upgrade clears the most risk?")

---

## 3. Release plan (proposal)

Only the 1.24.0 row describes this release. The rows below it are a plan: the contents and the numbers are indicative and nothing in them is committed.

| Release | Content | Notes |
|---|---|---|
| ~~Next patch (1.23.34)~~ | ~~P0, dark~~ | **Did not happen.** Releases 1.23.34 and 1.23.35 carried other work, so Phase 0 (merged 2026-10-08) was not released dark. It ships together with P1, which is safe because the migration, the ingest hardening and the poller change must ship together anyway; history starts when the combined release is installed |
| 1.24.0 (proposed; planned, not yet merged, tagged or published) | **P0 + P1** (migrations 157, 158, 159) | Headline feature: "Inventory". New permissions; built-in roles reconciled to definition version 6; custom roles unchanged and must be granted `inventory.view` by hand. KB live on the day. Draft notes: `06-RELEASE-NOTES-DRAFT.md` (first hour after the upgrade, how to go back, support). The number is set when the release is tagged |
| 1.24.x (plan) | **P2** | Catalog, history extras (mini-lists, saved views), trend charts, reports |
| 1.25.0 (plan) | **P3 + P4** | Requires the new agent release for 3.1–3.3; appliance-only OTA is valid for the rest (AGENTS.md) |
| 1.26.0 (plan) | **P5** | Compliance policies |

Every release:

- tarball permissions normalised (0644/0755, including nested dashboard archives);
- migrations named explicitly in the packager;
- rollback stops binary consumers before the legacy restore;
- signed packages only.

## 4. Test plan

| ID | Type | Case | Expected |
|---|---|---|---|
| T1 | unit | Comparator matrix: `15.2(4)E8 < 15.2(4)E10`, `17.9.4 < 17.9.4a`, `11.1.2-h3 < 11.1.2-h10`, `7.2.5 build1517 < 7.2.8`, `21.4R3-S5.4 > 21.4R3-S5`, `10.0.20348.587 < 10.0.20348.2700`, cross-train refused | Pinned orderings; `None` on cross-train |
| T2 | unit | Normaliser golden fixtures (every row in doc 02 §4, plus live values from .221) | Exact family/product/release/version/edition |
| T3 | integration | First software snapshot of a new server | 0 events; `inventory_baselined_at` set |
| T4 | integration | Second snapshot: 1 added, 1 removed, 1 version change | Exactly 3 events with the correct old/new values |
| T5 | integration | Delete a server that has software rows | 0 `removed` events; prior history retained |
| T6 | integration | Snapshot with ≥ 1,000 entries missing 200 previously seen | No prune; 0 `removed`; truncated health flag |
| T7 | integration | Duplicate same-name entries in one snapshot (HKLM + HKCU) | One row (highest version); 0 flapping events across 3 uploads |
| T8 | integration | Rule-pack revision bump, no source change | All rows re-normalised; **0 events** |
| T9 | integration | FortiGate template `fgt_fw_version` changes 7.2.5 → 7.2.8 | Identity updated within one loop; 1 firmware event |
| T10 | security | Scoped user: summary, assets, facets, os, software, product, installations, hardware, changes, export | Only in-scope assets counted and listed |
| T11 | security | Scoped user requests an out-of-scope asset flyout | 404 |
| T12 | security | Scoped user requests trends or report data | Restricted response / 403, consistent with `reports.py` |
| T13 | security | Export with cells starting `=`, `+`, `-`, `@`, tab | Neutralised; audit log row written |
| T14 | security | Viewer role calls every mutating inventory route | 403 (route-guard test plus live check) |
| T15 | perf | 5k servers × 400 packages, 10k devices, 1M events | Doc 02 §13 budgets met; ingest p95 unchanged while the normalise loop runs |
| T16 | regression | Upgrade N-1 → N and fresh install, with migrations 157+ | Schema gate green both paths; baselined seed correct |
| T17 | regression | Built-in role reconcile v3 → v4 | Built-ins gain inventory perms; custom roles byte-identical |
| T-UI1 | ui | Strict build, `smoke:routes`, nav-target validation | Green; every nav `to:` routed |
| T-UI2 | ui | Drill-down equality: each Overview KPI or chart link → list `total` | Equal for every link on the fixture dataset |
| T-UI3 | ui | User without `inventory.view` | Group hidden; direct URL shows the designed denied state |
| T18 | UAT | Owner answers the four P1 exit questions on the reference appliance | ≤ 3 clicks each, no docs needed |

**Status of the test plan after Phase 1** (file names under `server/tests/` unless stated; counts are collected tests at the time of writing):

| ID | Covered by | State |
|---|---|---|
| T1 | `test_versioning.py` (805) | Passing (P0) |
| T2 | `test_inventory_normalize.py` (691), `test_inventory_fix_normalizer.py` (139: the review round's rule-pack and category cases), `test_inventory_fix_r2_backend.py` (75: round 2, including the natural-sort total order, date parsing, rule pack revision 3 and the silent re-key) | Passing |
| T3 to T9 | `test_inventory_ingest.py` (160), `test_inventory_service.py` (168), `test_inventory_migration_157.py` (51), on the private PostgreSQL fixture | Passing (P0) |
| T10 | `test_inventory_scope.py` (27), `test_inventory_software.py` (scoped catalog, product and installations), the scoped golden fixtures | Passing |
| T11 | `test_inventory_scope.py` and `test_inventory_api.py`: an out-of-scope flyout is the same `404` as an unknown one | Passing |
| T12 | `test_inventory_trends_settings.py` and `test_inventory_scope.py`: a scoped caller gets `restricted: true` and no series | Passing |
| T13 | `test_inventory_export.py` (124) and `test_inventory_fix_export_audit_stream.py` (33): cells starting `=`, `+`, `-`, `@`, tab and carriage return; the audit row is written before the first byte; the export is refused when the audit entry cannot be stored; admission control and stall handling | Passing |
| T14 | `test_inventory_route_guards.py` (19): every non-GET route declares more than `inventory.view`, any export route declares `inventory.export`, and the router policy is pinned. The live check on an appliance is open | Passing; live check open |
| T15 | The 5,000 x 400, 10,000-device, 1 M-event run, results in `05` section 6.5 (the fixture is built with set-based SQL outside the repository). Read budgets: met except the process-cold software catalog and publisher facet, deep and large catalog pages and the per-product sorts (`02` section 13). The half about ingest p95 while the normalise loop runs belongs to Phase 0 acceptance and is open | Measured; one miss |
| T16 | `test_inventory_migration_157.py` (51) and `test_inventory_fix_migration_158.py` (9: the capture start, the freshness seed, the first-seen repair, replay) for the schema gate; the N-1 to N upgrade and the fresh install are exercised by the release process | Open for the combined release |
| T17 | `test_permissions_model.py` (30): definitions, the D9 grants (view only for network, systems and security; nothing for application), custom roles untouched. The real v4 to v6 reconcile is checked on the reference appliance | Passing; reference check open |
| T-UI1 | `npm run build` (strict), `smoke:routes` (48 routes), `scripts/test-nav-access.mjs`, `scripts/test-inventory-ui.mjs` (every nav `to:` resolves) | Passing |
| T-UI2 | Server half `test_inventory_api.py` and `test_inventory_software.py` (every number of summary, os, hardware, changes and software equals the total of the list it opens); browser half in `scripts/test-inventory-ui.mjs`, `test-inventory-os-hardware-changes.mjs` and `test-inventory-software-settings.mjs` (every rendered link's `data-drill-count` equals the number of assets its filter selects in the fixture fleet) | Passing |
| T-UI3 | `scripts/test-nav-access.mjs` (a role without `inventory.view` has no Inventory group) and `scripts/test-inventory-ui.mjs` (no tab, and the gate shows the denied state and renders no page) | Passing |
| T18 | The owner's UAT on the reference appliance | Open |

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Normalisation wrong, so counts can't be trusted | Raw evidence kept and shown; golden fixtures from live data; Unmatched queue; local rules with preview; "report inaccuracy" |
| Trigger overhead on hot write paths (60 s device polls, agent ingest) | `UPDATE OF` column lists plus `WHEN` clauses (no-op updates never enter PL/pgSQL); perf test T15 compares ingest p95 before and after |
| False history (upgrade storms, re-keys, truncation) | Baselined gate, suppression GUC, truncation guard, tests T3–T8; never backfill |
| Double counting (server + its SNMP device row) | Union dedupe on `servers.device_id`; fixture asserts total = distinct machines. Only linked pairs fold: an unlinked pair is two assets (a documented limitation; automatic pairing by address is not implemented because a wrong guess hides a real asset) |
| Scope leaks through aggregates | Scope applied inside the union CTE (single choke point); T10–T12; trends refused for scoped users |
| Agent-repo dependency delays P3 | P1/P2/P4 need no agent change; P3 agent work is capability-gated and degrades gracefully |
| Lifecycle data errors or date changes (e.g. FortiOS 7.4/7.6 extended in 2026) | "As of" date on every value; overrides with reason and expiry; feed refresh between releases |
| Widening software visibility to `inventory.view` holders | Explicit owner decision D3; tag scope still applies; release note |
| Scope creep into CVE matching | P6 is explicitly the Compliance set's phases; nothing CVE-related before P5 completes |

## 6. KPIs to track after P1

- Identity coverage % per field (vendor, model, OS version, serial)
- Fresh-inventory %
- Unmatched-software share
- Median time for an operator to answer an inventory question (UAT)
- Weekly active users of `/inventory/*`
- Exports per week
- From P5: policy pass rate and exception count by age

## 7. Decisions for the owner

| # | Decision | Recommended default |
|---|---|---|
| D1 | Group name and placement | **"Inventory & Compliance"** group after Servers; short label "Inventory" (first proposed as "Assets") |
| D2 | Relabel Servers → "Inventory" to "All Servers" | **Yes**: one meaning for "Inventory" |
| D3 | Does `inventory.view` alone reveal per-server software lists (today `servers.view`)? | **Yes, within tag scope**: the inventory is the point of the module |
| D4 | Do built-in viewers get `inventory.export`? | **No**: export is classified *sensitive*; operators and admins only |
| D5 | Linux coverage path | **Agentless SSH first** (P3.4, reuses `ncm_credentials`); a Linux agent GA stays in the agent roadmap |
| D6 | Lifecycle data source | **Bundled endoflife.date snapshot per release + signed refresh**; no direct appliance egress |
| D7 | Accept the Compliance-set amendments (`00-INDEX.md`, reconciliation table) | **Yes** |
| D8 | Ship P0 dark in the next patch release, ahead of the UI | **Yes**: history accrues from day one |
| D9 | Which built-in team roles get `inventory.view` when the UI lands (P1)? Release 1.23.35 added `network_team`, `systems_team`, `application_team` and `security_team` as least-privilege starting points | **`network_team`, `systems_team` and `security_team`: view only** (devices and servers inventory is core to their work and CIS 1 and 2 are a security-team concern); **`application_team`: none** (it sees applications, not the estate). No team role gets `export` or `manage`. Applied with a role-definition bump in P1, when there is something to see |

**State of the decisions after Phase 1.** Implemented as the recommended default in the product (the owner can still reverse them): **D1** (group name and placement), **D2** (*All Servers* relabel), **D3** (`inventory.view` alone shows per-server software within the tag scope, and the release notes say so), **D4** (no built-in viewer exports) and **D9** (definition version 6: `inventory.view` only for the network, systems and security teams, nothing for the application team; the three team descriptions mention Inventory only on a row created by this release, because the reconcile changes permissions and not text). **D7** is applied in these documents. **D5** (Linux path) and **D6** (lifecycle data source) wait for P3 and P4 and are still open; **D7** is applied here but the owner has not confirmed it (see *Owner decisions still open* at the top of this document). **D8** (ship P0 dark in the next patch) did not happen: Phase 0 ships together with Phase 1 (section 3), so it is moot.

