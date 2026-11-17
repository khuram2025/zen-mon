# Discovery Rule Engine: research and phased plan

Status: plan only, 2026-11-16. Nothing here is implemented. Based on main at `55492d8` (1.25.9).
Shareable copy: https://claude.ai/artifact/Y9nJfeuHy6X531GEMvRiNK

## 1. Recommendation

Build one global, ordered rule engine whose preview, simulation and real run all execute the
same computed plan, and ship it in four phases starting with rules on discovery results.

* OpManager's Discovery Rule Engine applies string-matched rules once, at device add or on a
  manual Re-run, with no preview, no order, no history and no undo.
* The strongest competitors, LogicMonitor and Checkmk, add continuous evaluation and
  explainability, but LogicMonitor needs Groovy for the scan that has a dry run and deletes
  history on un-match, and Checkmk's revert is all-or-nothing.
* ZenPlus already has the scan, the staging tables, a dead "auto-import" option in the wizard,
  an unused `discovery_rules` table and every target a rule would write to. What is missing is
  the part that decides, explains and records.

Phase 1 goes first: it makes the wizard's existing option real, reaches OpManager's
discovery-time behaviour with a preview on top, and touches no device that is already
monitored.

## 2. OpManager's Discovery Rule Engine

Read from the public demo on 2026-11-16 (forms opened, nothing saved) and from ManageEngine's
help pages and release notes. Source keys are listed in section 12.

| Part | What OpManager has | Where it stops |
| --- | --- | --- |
| Rule | Name, description, criteria rows, action rows, enable/disable, copy, delete | List shows Name, Description, Status only: no priority, match count or last run |
| Criteria (demo) | Device properties: DNS Name, Category, Type, Vendor, IP Address, Device Display Name, Parent Net, sysObjectId, sysName, sysDescr, Virtual Type, Poller Name. Custom fields: SerialNumber, Building, Floor, Cabinet, Department, Contact Name, Telephone Number, Comments, VMware_Tags, Site Address. Process Name. NT service Name, Description, Display Name | Custom-field criteria work only for CSV discovery and Re-run (release notes 12.8.610) |
| Operators (demo) | equals, not equals, contains, not contains, starts with, ends with, regular expression | The same seven string operators for every field, IP Address included: no subnet or range test, no numeric or version comparison |
| Logic | One switch per rule: Match ANY (OR) or Match ALL (AND) | No nested groups |
| Actions (demo) | Associate process, service, Windows NT service, file, folder, script, URL monitor; event log rule; MSSQL monitors; notification profile; custom field; workflow; downtime schedule; uplink dependency; monitoring interval; device template; performance monitors; add to business view; add to NCM; add to IPAM | No add-to-group, tag or credential action. Performance monitors capped at 15 per rule (help page). Device-level actions only. Central server (Enterprise) supports only business view and notification profile |
| When it runs | At discovery or device add; on scheduled discovery; manual Re-run against chosen devices | No reaction to later property changes is documented. After a rule edit the FAQ says to reapply it |
| Order | none | No priority or conflict handling documented. One precedence statement: rule thresholds override template and device thresholds |
| Safety | Enable/disable | No preview, no match count, no per-rule history, no undo documented. Release notes record a Re-run bug that duplicated notification-profile associations (fixed 12.9.200) |

Not verified: whether a REST API for rules exists (a forum user could not find one), and
whether Re-run removes actions from devices that no longer match.

User complaints found: the Add Device path cannot attach rules; discovery auto-creates
application monitors with no switch; rediscovery resets category and template (pitstop
threads, section 12).

## 3. Competitors

"Not verified" means no source was found; it is not a claim that the feature is absent.
"Snippet" marks a forum page seen only as a search excerpt.

| Product | Conditions | What it sets on a new device | Re-evaluates later | Order and conflicts | Preview | History and undo |
| --- | --- | --- | --- | --- | --- | --- |
| OpManager | Flat list, one AND/OR switch, string operators | 20 actions (monitors, template, notification profile, business view, NCM, interval, dependency) | Manual Re-run only | None documented | None | None documented |
| SolarWinds | No rule engine; import filter for interfaces, volumes, applications | Imports node and sub-objects; custom properties cannot be set at discovery (repeated forum request, snippet) | Dynamic-query groups are continuous | None | Preview button on dynamic queries | Results tab (found, changed, imported, ignored); undo not verified |
| PRTG | None visible; device templates probe and create | Devices and sensors; credentials, intervals, notifications by inheritance from the group | Scheduled rediscovery; does not add sensors to existing devices at group level; re-adds manually deleted devices and sensors | None | None | A ticket per discovery run |
| Zabbix | Host IP, service, received value, proxy; And/Or or custom expression | Add/remove host, host groups, templates, tags (7.0), enable/disable, message, script | Fires on every discovery cycle; no automatic reversal | Not documented between actions; LLD overrides are ordered with "stop processing" | None found | Action log (content for host operations not verified) |
| LibreNMS | No discovery rules; dynamic device groups over database columns | Groups scope alert rules, service templates, maintenance | Groups continuous; service templates add and remove | No priority | Not verified | Not verified |
| Auvik | Fixed classification; no user rules | Manages everything on an approved network; credentials tried and pinned; alert tags | Continuous | Site hierarchy inheritance (child overrides can silently break inheritance) | "Matches" count on credential filters | User audit log |
| Datadog NDM | sysObjectID picks a profile; subnets in agent YAML | Profile metrics and tags; subnet tags; monitors follow tags | Each discovery interval | Custom profile beats default; credentials in list order | Profile manager lists matching devices | Not verified |
| LogicMonitor | NetScan include/exclude rules; AppliesTo expressions | Group, properties (credentials), DataSources, alert rules | On every property change; un-matching removes the DataSource and deletes its history | Priority, first match; equal priority is "nondeterministic" | Test AppliesTo; Dry Run for Enhanced Script NetScans | Scan and version history, audit log; no one-click undo |
| Checkmk | Folder, host tags, labels, host regex | Any setting through rulesets | On every configuration build | Declared per ruleset: first match, per-parameter merge, or all add | Per-host effective parameters; match icons per rule; ineffective rulesets | Pending changes, activate, audit log; revert is all-or-nothing |
| ZenPlus 1.25.9 | Template match on sysObjectID prefix and sysDescr regex | Profile defaults: one group, template and tag set for the whole scan | No | None | None | Import batches, audit log |
| ZenPlus planned | Nested AND/OR/NOT, typed operators (subnet, version, tag and port sets) | Import decision, group, tags, template, type, location, credentials, intervals, NCCM, Device Tracker, alert exclusions, maintenance, dependency, map, service checks, notify | On creation by any path and on change | First match per single value, merge for lists, shadowing shown | Unsaved rule against inventory, a past scan or one device; simulation mode | Per rule, per run, per asset; undo per run |

What each does well, and where it falls short:

* **SolarWinds**: review queue with ignore list, previewable dynamic groups, group-based SAM
  templates. Weak: no discovery-time assignment of custom properties, NCM auto-add is a global
  switch that consumes licences (snippet), new interfaces on existing nodes are not auto-imported
  (snippet).
* **PRTG**: zero-configuration first run, inheritance as the configuration model. Weak:
  templates not editable in the UI, sensor explosion against per-sensor licensing (snippet),
  duplicates on repeated runs.
* **Zabbix**: richest documented condition and operation matrix, metadata-driven
  autoregistration, LLD lifecycle (disable then delete; manually disabled entities are left
  alone). Weak: duplicates from IP-keyed identity (snippets), manually removed hosts are
  re-created unless a status condition is added, no preview.
* **LibreNMS**: neighbour- and routing-driven discovery, continuously evaluated groups as the
  one scoping primitive, service templates that add and remove. Weak: groups expose the raw
  schema, a malformed regex emptied a group in a community thread, no per-rule credential, tag
  or poller-group assignment.
* **Auvik**: near-zero configuration, dynamic alert tags, credential trial and pin. Weak:
  classification is fixed, the documented fix for a wrong type is a manual edit, and displayed
  type does not necessarily change billing.
* **Datadog NDM**: tag-scoped monitors cover new devices with no assignment, composable profile
  inheritance, version-controlled YAML. Weak: discovery scope lives in a per-agent file,
  matching is effectively sysObjectID only.
* **LogicMonitor**: one expression language everywhere, continuous re-evaluation, a real dry
  run with per-device reasons. Weak: Groovy required for Enhanced NetScans, same-level ties are
  nondeterministic, un-matching deletes history, a community thread reports scheduled NetScans
  overwriting manual group membership.
* **Checkmk**: best staging, explainability and explicit conflict semantics. Weak: network scan
  is ping only, dynamic host management and auto-registration are in commercial editions only,
  revert is all-or-nothing, automatic activation also activates other users' pending changes.

## 4. What "better" means

1. **See it before it happens.** Preview any rule, saved or not, against current inventory, a
   past scan or one device, with the reason each condition passed or failed.
2. **Simulation mode.** A rule runs on live triggers and logs what it would do, changing
   nothing, until it is promoted. Found in none of the nine products.
3. **Typed conditions.** Subnet and range on IP, version comparison, tag/port/protocol sets,
   nested groups.
4. **Every way in.** Add Device, CSV import, legacy SNMP import, agent enrolment and controller
   children go through the same rules as a scan.
5. **Re-evaluation on change.** A firmware upgrade, hardware swap or tag change re-runs the
   affected rules for that asset only.
6. **Deterministic order.** First match per single-valued setting, merge for lists, shadowed
   rules shown; no ties.
7. **Manual edits win by default.** The engine records what each rule set and leaves later
   manual edits alone unless the rule is marked enforce. This also keeps the existing rule that
   rediscovery preserves operator choices (`import_results`, `conflict_strategy = update`).
8. **History and undo.** Each run lists before and after per asset and can be undone alone.
9. **Stop-matching is a choice and never deletes data.** Keep (default) or revert, per rule.
10. **License and capacity aware.** Auto-import stops at the node limit and a per-run cap, says
    why, and never disables or deletes anything.

Actions OpManager lacks and ZenPlus will have: group, tags (and through tags, team visibility),
SNMP credential, per-asset alert rule exclusion, controller-managed children.

## 5. ZenPlus today

| Area | What exists | Gap |
| --- | --- | --- |
| Scan and staging | `discovery_profiles`, `discovery_schedules`, `discovery_runs`, `discovery_results_v2`, `discovery_import_batches`, `discovery_import_items`, `discovery_ignored_devices` (migration 017, models in `server/app/models/discovery_v2.py`); `server/app/services/discovery_executor.py`, `discovery_probes.py` (ICMP, TCP, SNMP, SSH, WinRM, HTTP, TLS, ARP), `discovery_identify.py`, `discovery_scheduler.py` | Results carry identity, ports, protocols, credential outcome and confidence, but only the profile defaults are copied onto them (`matched_template_id`, `suggested_group_id`, `suggested_tags`) |
| Rules | `discovery_rules` table and `DiscoveryRule` model: per profile, `priority`, `match_mode` all/any, `conditions`, one `action` of import, ignore, tag, assign | No API, evaluator or UI. `execute_run` phase 6 is the comment "applying rules (placeholder)" |
| Import mode | `discovery_profiles.import_mode` (review, auto_match, ignore_match); wizard step "Classification & Rules" in `dashboard/src/pages/discovery/WizardPage.tsx` | Stored and displayed; no server code reads it. Scheduled runs always wait for a manual import |
| Import | `POST /api/v1/discovery-v2/runs/{id}/import` in `server/app/api/v1/discovery_v2.py`: one group, template, credential, tag set, location and interval for the whole selection; `pg_advisory_xact_lock('discovery-import')`; `license_capacity.reserve` | No per-device decisions |
| Template matching | `device_profiles.match_rules` (`server/app/schemas/snmp.py` `MatchRules`), Go classifier `poller/internal/checker/snmp/profile.go`, `AssignProfileIfUnset` | Template only, and only when none is set |
| Action targets | `device_groups`; tag registry (`tags`, `tag_service.canonicalize_tags`); `device_service.apply_credential`; `device_ncm` and `POST /ncm/bulk-assign`; `udt_device_settings`; `device_maintenance`; `topology_dependencies`; `alert_rule_exclusions` (`exclude` / `include`); `sensor_assignments`; `manual_map_nodes`; `service_check_templates`; `servers.tags`, `servers.environment` | Each is set by its own page or bulk action |
| Other creation paths | `POST /devices`, `POST /devices/bulk-import`, legacy `POST /snmp/discover/{job}/import`, `POST /servers`, agent enrolment and fleet authorize (`api/v1/agents.py`, `servers.py`), controller child sync (`managed_device_service._sync_controller`) | None would pass through a discovery-only engine |
| Change detection | Poller writes sysObjectID, vendor, model, OS version, sysName, sysDescr straight to PostgreSQL (`UpsertSystemInfo`); `inventory_identity` uses a dirty marker (`norm_rev` NULL) | The API gets no event when identity changes |
| Guard rails | `write_audit_log` (`audit_logs`), `alert_worker_lock.run_pass`, `scoping.assert_tags_assignable` / `visible_tags`, `license_capacity`, `alert_rule_scope.impact` and `guard_rule_change` (fleet-wide rule stance) | No record of which setting came from which rule |
| Permissions and nav | `discovery.view`, `discovery.run` (`core/permissions.py`, route policy `discovery`); sidebar children in `dashboard/src/components/layout/navigation.ts`; tabs in `DiscoveryLayout.tsx` | No rules page or permission |

The live appliance (192.168.8.221, 1.25.9) matches the code: Discovery shows Profiles,
Scheduled, Scan Results, Import Queue, Ignored, Credentials; no profiles exist yet.

## 6. Design

### 6.1 Rule model

* **Scope.** Rules are global and ordered, not owned by a discovery profile. A rule names its
  asset kinds (`device`, `server`, `child`), its triggers (`discovery`, `created`, `changed`,
  `manual`) and optionally the discovery profiles it is limited to.
* **Conditions.** A tree of AND / OR / NOT groups, at most three levels deep, over typed facts.

  | Type | Operators |
  | --- | --- |
  | text | equals, not equals, contains, not contains, starts with, ends with, matches regex, in list, is empty |
  | ip | in subnet, in range, equals, in list |
  | number | =, !=, <, <=, >, >=, between |
  | version | same comparisons on dotted versions |
  | set (tags, ports, protocols) | has any, has all, has none |
  | reference (group, template, profile, controller) | is, is not, is any of |
  | boolean | is true, is false |

* **Facts.**

  | Phase | Facts |
  | --- | --- |
  | 1 (scan result) | hostname, fqdn, sys_name, sys_descr (from `raw_data`), sys_object_id, vendor, model, device_type, os, os_version, serial_number, mac vendor, ip_address, open_ports, protocols_detected, credential_status, credential used, confidence_score, status (new/existing/changed), discovery profile, is server-class |
  | 2 (existing asset) | tags, group, location, owner, template, ping/SNMP enabled, status, `inventory_identity` (vendor_key, os_family, os_release, hw_model, lifecycle_slug), creation source, NCCM enrolled; servers: os_type, collection_mode, environment |
  | 3 | controller (`managed_by_device_id`), `managed_source`, child model; Device Tracker enabled; alert exclusions present |
  | 4 | interfaces (count, name/alias/type match), LLDP/CDP neighbour, sensor and site |

* **Actions.** Each action is single-valued or additive and declares the asset kinds it
  applies to and the permission it needs.

  | Phase | Single-valued | Additive |
  | --- | --- | --- |
  | 1 | import decision (auto-import as device / server / both / auto, review, ignore), group, template, device type, location, SNMP credential, ping interval, SNMP poll interval | tags |
  | 2 | owner, server environment, ping/SNMP enabled (on only) | |
  | 3 | NCCM connection profile and enrolment, NCCM schedule, Device Tracker enabled, dependency parent, maintenance window for the first N hours | alert rule exclusion or inclusion, add to network map or site, service check from template, notify a channel |
  | 4 | sensor assignment, agent policy, software baseline | interface monitoring by pattern |

  Notification routing needs no action of its own: ZenPlus alert rules already scope by group,
  type, location and tag, so a rule that sets a tag or group routes the device.
* **Order and conflicts.** Rules have an explicit unique priority. For each single-valued
  setting the first matching rule wins; later matching rules are recorded as shadowed.
  Additive actions from every matching rule are merged. A rule may set "stop processing".
  Ties cannot occur. (Built in phase 1: the import decision follows the same first-match rule
  instead of "ignore beats import", so a catch-all Ignore at the bottom does not override the
  specific Auto-import rules above it.)
* **Modes.** `draft`, `simulate` (evaluated on live triggers, logged, nothing written),
  `active`, `disabled`.
* **Ownership.** Every value a rule sets is recorded with the previous value. `set_once`
  (default) leaves later manual edits alone; `enforce` re-applies. On un-match: `keep`
  (default) or `revert`.

### 6.2 Engine

New package `server/app/services/discovery_rules/`:

| Module | Role |
| --- | --- |
| `facts.py` | Builds the typed fact dict from a `DiscoveryResultV2` row or an existing device/server row with its joins |
| `conditions.py` | Condition schema, validation, compiled evaluator returning a trace (pure) |
| `actions.py` | Action registry: key, kind, asset kinds, permission, `plan()`, `apply()`, `revert()` |
| `engine.py` | `evaluate(facts, rules) -> Plan` (pure) and `execute(db, plan, mode)` |
| `preview.py` | Runs `evaluate` over a target set and summarises |
| `runner.py` | Triggers, run bookkeeping, reconcile loop, undo |

Python only. The Go poller is unchanged in phases 1 to 3; the sysObjectID template classifier
stays as it is and a rule's template action takes precedence only when a rule sets one.

### 6.3 Hooks

| Hook | Where | Behaviour |
| --- | --- | --- |
| Scan finished | `discovery_executor.execute_run`, the phase 6 placeholder | Each result gets `rule_plan`, `disposition`, `matched_rule_ids`; auto-import results go through the shared import service; ignore results are added to `discovery_ignored_devices` with the rule as reason |
| Manual import | `import_results` (`discovery_v2.py`), body extracted to `server/app/services/discovery_import.py` | Precedence: operator's dialog choices, then rule plan, then profile defaults |
| Created elsewhere | `device_service.create_device`, `bulk_import_devices`, `api/v1/snmp.py` import, `api/v1/servers.py` and `api/v1/agents.py` server creation, `managed_device_service._sync_controller` | Asset marked for evaluation after commit; creation is never blocked, slowed or failed by rules |
| Changed | Reconcile loop started in `main.py`, under `alert_worker_lock.run_pass('discovery-rules')` | SQL hash of rule-relevant columns compared with the stored hash; only differing assets and assets behind the current rules revision are evaluated, in bounded batches |
| Manual run | API | Preview, then apply to a chosen set |

`discovery_profiles.import_mode` gets its meaning: `review` lets rules annotate results but
never import or ignore; `auto_match` honours both decisions; `ignore_match` honours only
ignore. Default remains `review`, so an upgrade changes nothing.

### 6.4 Data model

Forward migrations only; the migration set stays append-only. The legacy `discovery_rules`
table is left in place and superseded; any rows are copied in as drafts. **Migration numbers
are chosen at build time by scanning main and every open PR** (main ends at 187 today; 186 is
not on main; HA/sensor and virtualization sessions are working in parallel). Each migration
carries the `zenplus` grant block and a `scripts/migrations.lock` entry.

| Table | Phase | Columns (main ones) |
| --- | --- | --- |
| `discovery_engine_rules` | 1 | id, name (unique), description, priority (unique), mode, asset_kinds[], triggers[], profile_ids[] NULL, condition JSONB, actions JSONB, enforcement, on_unmatch, stop_processing, max_changes_per_run, revision, created_by, updated_by, activated_by, timestamps, last_evaluated_at, last_match_count, builtin_key |
| `discovery_engine_rule_versions` | 1 | rule_id, revision, snapshot JSONB, changed_by, changed_at, reason |
| `discovery_engine_runs` | 1 | id, trigger, mode (apply/simulate), discovery_run_id NULL, rules_revision, started_by, started_at, finished_at, status (running, completed, partial, needs_approval, failed, undone), counts JSONB, undone_by, undone_at |
| `discovery_engine_run_items` | 1 | id, run_id, asset_kind, asset_id NULL, discovery_result_id NULL, rule_id, rule_revision, action_key, item_key, before JSONB, after JSONB, outcome (applied, simulated, shadowed, kept_manual_edit, blocked_license, blocked_child, blocked_permission, error, reverted), detail |
| `discovery_engine_assignments` | 2 | (asset_kind, asset_id, action_key, item_key) PK, value JSONB, previous JSONB, rule_id, rule_revision, run_id, applied_at |
| `discovery_engine_asset_state` | 3 | (asset_kind, asset_id) PK, facts_hash, rules_revision, matched_rule_ids[], evaluated_at, dirty_since |
| `discovery_results_v2` (columns) | 1 | rule_plan JSONB, disposition, matched_rule_ids[] |
| `system_settings['discovery_rules']` | 1 | enabled (default false), max_auto_import_per_run, license_reserve_nodes, max_changes_per_run, reconcile_interval_s, history_days |

Run items follow `history_days` retention (default 180) with a sweep in the reconcile loop.
`license_capacity.NODE_COUNT_SQL` and `updater/inventory.py` do not change.

### 6.5 API

Under `/api/v1/discovery-v2/rules`, inside the existing `discovery` route policy; writes need
`discovery.manage_rules`.

| Endpoint | Use |
| --- | --- |
| `GET /rules`, `POST /rules`, `GET/PUT/DELETE /rules/{id}`, `POST /rules/{id}/clone`, `POST /rules/reorder`, `POST /rules/{id}/mode` | Manage rules and order |
| `GET /rules/{id}/versions`, `POST /rules/{id}/versions/{rev}/restore` | Version history |
| `GET /rules/catalog` | Fields, operators, value sources, actions with parameter schemas and permissions; the builder is drawn from it |
| `POST /rules/preview` | Plan for an unsaved or saved rule (or the whole set) against inventory with a filter, a discovery run, one asset, or sample facts |
| `POST /rules/run`, `GET /rules/runs`, `GET /rules/runs/{id}`, `GET /rules/runs/{id}/items`, `POST /rules/runs/{id}/approve`, `POST /rules/runs/{id}/undo` | Run, history, approval, undo |
| `GET /rules/assets/{kind}/{id}` | Explain one asset: matching rules, rule-owned values, kept manual edits |
| `GET /rules/conflicts` | Rules setting the same single value for the same current assets; rules that match nothing or can never win |
| `GET /rules/export`, `POST /rules/import` | JSON with references by name; unresolved names reported, nothing guessed |
| `GET/PUT /rules/settings` | Engine settings |

### 6.6 UI

* **Rules list** `/discovery/rules` (`dashboard/src/pages/discovery/RulesPage.tsx`; sidebar
  child and `DiscoveryLayout` tab): drag to order, mode chip, one-sentence condition summary,
  action chips, "matches now", last run and change count, conflict badge, Run now, Import/Export,
  starter rules.
* **Rule editor** (`RuleEditorPage.tsx`): When (triggers, asset kinds, profiles), If (condition
  builder with nested groups and typed pickers), Then (action cards), Safety (set-once or
  enforce, on un-match, stop processing, change cap). A live preview panel sits beside it:
  "matches 42 of 318, would change 17", sample rows with before and after, "test a device",
  "replay on a past scan".
* **Run history** (`RuleRunsPage.tsx`): runs, items with before and after, filter by outcome,
  Undo, Approve.
* **Scan results** (`RunPage.tsx`): Rule and Decision columns with a why-popover.
* **Device page**: an Automation card in `dashboard/src/pages/device/` listing rule-owned
  settings and kept manual edits.
* **Wizard** (`WizardPage.tsx`): the radio that does nothing today becomes a per-profile
  "allow auto-import / allow ignore" choice with the count of rules that apply.
* Design system rules apply: rem type scale, `kpiTone`, no CDN assets.
* After `navigation.ts` changes: `node scripts/test-page-access.mjs --write` to regenerate
  `server/app/core/page_catalog.json`; `scripts/test-nav-access.mjs` must pass.

### 6.7 Permissions and scope

* New `discovery.manage_rules` in `server/app/core/permissions.py` (implies `discovery.view`),
  granted to administrators and the built-in `network_team` role (built-in definition version
  bump). Viewing rules and history needs `discovery.view`.
* Activating a rule also requires the permission behind each of its actions: `devices.manage`
  (device fields, credentials), `servers.manage`, `ncm.manage`, `alerts.manage` (exclusions),
  `udt.manage`, `maps.manage`, `service_checks.manage`. `activated_by` is stored and audited.
* Rules are fleet-wide configuration. A tag-scoped user cannot create, edit or run them, the
  same stance `alert_rule_scope.guard_rule_change` takes for fleet-wide alert rules. Run
  history shown to a scoped viewer is filtered with `scoping.visible_tags`.
* Tag actions pass `scoping.assert_tags_assignable` for the activating user and use
  `tag_service.canonicalize_tags`. A subnet-to-tag rule is how a newly imported device becomes
  visible to the right team (untagged resources stay hidden from scoped users).

### 6.8 License and safety

* Auto-import goes through `license_capacity.reserve` under the `license-node-capacity` lock,
  inside the existing `discovery-import` lock. At capacity the remaining results stay in
  review with reason `license_capacity_reached`, the run is `partial`, a banner and optional
  notification say so. Nothing existing is disabled or deleted. Optional
  `license_reserve_nodes` keeps room for manual adds. Preview shows "would import 37, room
  for 12".
* Controller children: organising actions only (group, tags, exclusion, maintenance, map).
  Polling, ping, credential and NCCM actions are refused at save time for the `child` kind;
  children never take a license node.
* The engine never deletes an asset, never turns monitoring off, and never edits alert rule
  thresholds or `alert_rules.enabled`. Turning a shared rule off for an asset is an
  `alert_rule_exclusions` row, as `docs/alert-rule-exclusions.md` requires.
* A run that would change more assets than the cap, or a rule whose match count jumps sharply
  against its last run, pauses as `needs_approval`.
* Undo restores a value only if it still equals what the rule set; imported devices are listed
  for a deliberate bulk delete and are never deleted by undo.
* Regular expressions are validated and length-limited at save and evaluated with a time
  limit.
* Credential actions reference stored credentials by id; no secret is copied into rules, run
  items or exports.
* Every rule change, mode change, run, approval and undo is written with `write_audit_log`.

## 7. Phases

Sizes are rough working days for one developer with Claude sessions, including tests and docs,
excluding canary soak. Each phase ships behind `system_settings['discovery_rules'].enabled`.

| Phase | Delivers | Size |
| --- | --- | --- |
| 1 | Rules on discovery results, preview first | 8 to 10 days |
| 2 | Existing inventory, ownership, simulation, history, undo, all creation paths | 10 to 12 days |
| 3 | Continuous re-evaluation and wider actions, controller children, caps | 10 to 14 days |
| 4 | Depth by demand: interfaces, topology, sensors/sites, server policies, scheduled runs | 3 to 6 days each |

### Phase 1: rules at discovery, preview first (build this first)

Scope: rules, versions, runs, run-items tables; condition tree and typed operators over scan
facts; phase-1 actions; engine in executor phase 6 and in the import path; preview against a
past scan and sample facts; rules list and editor; Rule and Decision columns; `import_mode`
honoured; license-aware auto-import with a per-run cap; `discovery.manage_rules`; engine off by
default.

Touches: new `server/app/services/discovery_rules/`, `server/app/services/discovery_import.py`,
`server/app/api/v1/discovery_rules.py`, `server/app/schemas/discovery_rules.py`;
`server/app/models/discovery_v2.py`, `discovery_executor.py`, `api/v1/discovery_v2.py`,
`core/permissions.py`, `core/route_policy.py`, `main.py` (router); one forward migration and
`scripts/migrations.lock`; `dashboard/src/pages/discovery/RulesPage.tsx`, `RuleEditorPage.tsx`,
`RunPage.tsx`, `WizardPage.tsx`, `DiscoveryLayout.tsx`, `api.ts`, `types.ts`,
`dashboard/src/App.tsx`, `components/layout/navigation.ts`, `server/app/core/page_catalog.json`;
`docs/discovery-rule-engine.md`, `docs/license-capacity.md` (new creating path), a paragraph in
`AGENTS.md`.

Acceptance:

- [ ] A rule "sysObjectID starts with 1.3.6.1.4.1.12356 AND IP in 10.51.0.0/16" with template,
      group, two tags and auto-import produces, on a scheduled scan of an `auto_match` profile,
      devices with exactly those settings and no manual step.
- [ ] The same scan on a `review` profile imports nothing; scan results show the rule and the
      decision it would have made.
- [ ] Preview of an unsaved rule against a past scan equals the plan the run later applies (a
      test compares them).
- [ ] Two rules setting different groups for one device: the higher wins, the other shows as
      shadowed.
- [ ] With 3 nodes free and 10 results marked auto-import, 3 are imported, 7 stay in review
      with the capacity reason, the run is partial, no existing asset changes.
- [ ] An ignore rule adds matching results to the Ignored list with the rule named as reason,
      and later scans do not offer them again.
- [ ] Without `discovery.manage_rules`: 403 on every write, no edit controls;
      `test-nav-access.mjs` and `test-page-access.mjs` pass.
- [ ] Engine off: scans and imports behave exactly as in 1.25.9 (existing discovery tests
      unchanged and green).

### Phase 2: existing inventory, ownership, history and undo

Scope: assignments table; facts for existing devices and servers; manual run against a
filtered set, always through preview; set-once and enforce; simulation mode; run history page
with Undo; explain endpoint and device Automation card; conflict report; evaluation on Add
Device, bulk import, legacy SNMP import, server create and agent enrolment.

Touches: `discovery_rules/facts.py`, `runner.py`, `actions.py`; `device_service.py`,
`api/v1/devices.py`, `api/v1/snmp.py`, `api/v1/servers.py`, `api/v1/agents.py`; one forward
migration; `RuleRunsPage.tsx`, `dashboard/src/pages/device/` (card), `DevicesPage.tsx` (bulk
action "Run rules").

Acceptance:

- [ ] Running a rule on 300 existing devices shows a preview; applying changes exactly the
      previewed set; a second run changes nothing.
- [ ] A value edited by hand after a set-once rule set it is left alone and reported as "kept
      manual edit"; the same rule in enforce mode restores it and records that.
- [ ] Undo restores every value still equal to what the rule set, skips values changed since,
      and lists both.
- [ ] A rule in Simulating mode records what it would do on each trigger and writes nothing to
      `devices` or `servers`.
- [ ] A device added through Add Device or CSV import gets the same outcome as one found by a
      scan; a failure inside the engine never fails the add.
- [ ] The device page shows which rule set its group, template and tags, with a link to the run.
- [ ] A tag-scoped user cannot manage rules and sees only in-scope assets in run history.

### Phase 3: continuous re-evaluation and wider actions

Scope: asset-state table and reconcile loop; keep or revert on un-match; phase-3 actions;
controller children hooked in `managed_device_service._sync_controller`; change cap with
approval and growth breaker; JSON export/import; starter rules.

Touches: `discovery_rules/runner.py`, `actions.py`; `main.py`; `managed_device_service.py`;
calls into `alert_rule_exclusions`, NCCM enrolment (`device_ncm`), `udt_device_settings`,
`device_maintenance`, `topology_dependencies`, `manual_maps`, service check templates; one
forward migration; rule editor action cards; `docs/controller-managed-children.md`,
`docs/alert-rule-exclusions.md`, `docs/network-maps.md`.

Acceptance:

- [ ] After the poller records a new firmware version or sysObjectID, the dependent rules are
      re-evaluated within two reconcile intervals and only that device is evaluated.
- [ ] A device that loses the tag a revert-on-unmatch rule depends on gets its rule-owned
      values restored to the previous values, with the reason in history.
- [ ] A rule "vendor Fortinet, type firewall" enrols matching devices in NCCM with the chosen
      connection profile; a user lacking `ncm.manage` cannot activate it.
- [ ] A rule targeting FortiAP children can tag, group and exclude them from an alert rule; a
      polling, credential or NCCM action on children is refused at save with a clear message;
      the node count is unchanged.
- [ ] An exclusion created by a rule resolves that rule's open incidents on the asset without
      a recovery notice, exactly as a manual exclusion does.
- [ ] A rule edit that would change more than the cap pauses as "needs approval"; nothing is
      applied until approved.
- [ ] Two API workers never apply the same reconcile batch twice.

### Phase 4: depth, chosen by demand

* Interface rules: monitor or ignore interfaces by name, alias, type or speed
  (`device_interfaces.monitored`).
* Topology facts: LLDP/CDP neighbour, "behind controller X" (current evidence only, as
  `docs/network-maps.md` defines it).
* Sensor and site facts and a sensor-assignment action (`sensor_assignments`), after the HA and
  sensors work lands.
* Server rules: agent policy and software baseline assignment.
* Scheduled rule runs and a report of assets that match no rule.

## 8. Testing

* **Contract vectors**: `scripts/dev/discovery-rules-contract/vectors.json` (facts, rules,
  expected plan), following `scripts/dev/device-status-contract/`. The Python engine and the
  dashboard's condition-summary code both read it.
* **Engine unit tests** (`server/tests/test_discovery_rules_engine.py`): every operator per
  type, nested groups, priority and shadowing, additive merge, stop processing, ignore beats
  import, invalid or oversized regex refused at save.
* **Database tests on `pg_fixture`** (`test_discovery_rules_pg.py`): CRUD and versions,
  permissions and tag scope, preview equals apply, idempotent second run, undo, manual-edit
  protection, license budget, children refusing polling actions, two workers and one lock.
* **Regression**: `test_discovery_contracts.py`, `test_discovery_import_contract.py`,
  `test_discovery_identify.py` green with the engine off; `test_license_capacity.py` gains the
  auto-import path; the `NODE_COUNT_SQL` equality test is untouched.
* **Dashboard**: strict build, route smoke checks, `scripts/test-nav-access.mjs`,
  `scripts/test-page-access.mjs --write`, `scripts/test-offline-assets.mjs`, a model test for
  the builder.
* **Scale**: 5,000 assets against 100 rules evaluates in under 2 seconds on the fixture; a
  reconcile pass is bounded per batch.
* **Rules of the host**: disposable fixtures only, never installed databases; `export
  ZENPLUS_API=http://127.0.0.1:9` before pytest on .221.
* **Acceptance**: canary on .221 with rules in Simulating mode against a real profile before
  activation; release acceptance on the reference appliance; the five required CI checks stay.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| A broad rule changes many assets at once | Preview before every manual run, simulation mode, change cap with approval, ownership records, undo |
| Auto-import floods the license or device list | Per-run cap, `license_capacity.reserve`, optional reserve, profiles default to review, engine off by default |
| Rules overwrite deliberate operator settings | Set-once default, manual edits detected and kept, enforce is explicit per rule |
| A user regex stalls an API worker | Validation and length limit at save, time-limited evaluation; library choice (must ship as a bundled wheel, see 1.25.8) decided at build |
| Parallel sessions (HA/sensors drafts, virtualization) | Migration numbers picked at build time; the creation hook is one small function other paths call; sensor facts wait for phase 4 |
| Team scopes | Untagged auto-imported devices are hidden from scoped users (existing fail-closed rule); starter rules include subnet-to-tag |
| Reconcile load on large fleets | SQL hash comparison, bounded batches, one worker under the existing lock |
| Two template deciders (Go classifier and rules) | Classifier keeps assigning only when unset; a rule's template is an owned value shown on the device page |
| Identity quality limits rule quality | Rules use the same identity the scan reports; confidence score is a fact, so low-confidence results can be held for review |

## 10. Decisions needed from the owner

1. Start with phase 1 alone (recommended), or phases 1 and 2 together (about 20 days before
   anything ships)?
2. Auto-import default: engine off and every profile on review after upgrade (recommended)?
3. Who may manage rules: new `discovery.manage_rules`, for administrators and the built-in
   network team role, not usable by tag-scoped users (recommended)?
4. Enforce mode: allow a rule to overwrite manual edits when explicitly set (recommended: yes,
   off by default)?
5. Undo and auto-imported devices: list them for a deliberate bulk delete, never delete
   automatically (recommended)?
6. Servers: include from phase 2 (recommended) or keep to network devices until phase 4?
7. Legacy `discovery_rules` table: leave and supersede with new tables (recommended)?
8. Product name: "Discovery Rules" under Discovery (recommended) or "Automation Rules"?
9. Starter rules to ship: Fortinet, Cisco, F5, MikroTik, Aruba, Juniper; printers and phones
   as ignore examples?
10. Phase 4 order: interface rules, topology facts, sensor/site facts or server policies first?

## 11. Not in scope of this plan

No code, migration, PR or release was produced. Discovery scanning itself (probes, sensors
running scans, neighbour crawl) is unchanged by this plan.

## 12. Sources

Pages opened 2026-11-16. "Snippet" = seen only as a search excerpt.

OpManager
* Help: https://www.manageengine.com/network-monitoring/help/configuring-discovery-rule-engine.html
* Enterprise: https://www.manageengine.com/network-monitoring/help/discovery-rule-engine-enterprise.html
* Re-run FAQ: https://www.manageengine.com/network-monitoring/faq/re-run-discovery-rule-engine.html
* Enable/disable FAQ: https://www.manageengine.com/network-monitoring/faq/status-enable-disable.html
* Release notes: https://www.manageengine.com/network-monitoring/help/read-me-complete.html
* Scheduled discovery: https://www.manageengine.com/network-monitoring/how-to/automatically-discover-devices.html
* Discovery filter: https://www.manageengine.com/network-monitoring/help/discovery-filter.html
* Device templates: https://www.manageengine.com/network-monitoring/help/device-templates.html
* Groups: https://www.manageengine.com/network-monitoring/help/grouping.html
* Demo: https://demo.opmanager.com/apiclient/ember/index.jsp#/Settings/Discovery/RuleEngine
* Forum: https://pitstop.manageengine.com/portal/en/community/topic/unable-to-add-device-not-in-inventory-or-search ,
  https://pitstop.manageengine.com/portal/en/community/topic/rules-engine-filter-by-entityid ,
  https://pitstop.manageengine.com/portal/en/community/topic/the-failure-of-opmanager-discovery-hardware-changes-and-applications-services

SolarWinds
* https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-network-discovery-using-the-network-sonar-wizard-sw1574.htm
* https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-automatically-add-discovered-nodes.htm
* https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-creating-groups-sw2591.htm
* https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-build-dynamic-query-sw2597.htm
* https://documentation.solarwinds.com/en/success_center/sam/content/sam-managing-scheduled-discovery-results-sw720.htm
* https://documentation.solarwinds.com/en/success_center/sam/content/sam-assign-template-to-group-learn-more-sw1010.htm
* Snippets: https://thwack.solarwinds.com/discussion/126353/assigning-custom-property-during-network-discovery ,
  https://thwack.solarwinds.com/discussion/130213/make-the-skip-adding-nodes-to-ncm-during-discovery-setting-a-per-discovery-toggle-rather-than-a-global-on-off ,
  https://thwack.solarwinds.com/discussion/92181/automatically-importing-interfaces-for-existing-nodes/p1

PRTG
* https://www.paessler.com/manuals/prtg/auto_discovery
* https://manuals.paessler.com/add_an_auto-discovery_group.htm
* https://www.paessler.com/manuals/prtg/create_device_template
* https://www.paessler.com/manuals/prtg/inheritance_of_settings
* https://helpdesk.paessler.com/en/support/solutions/articles/76000071390-custom-device-templates-xml-info
* Snippets: https://kb.paessler.com/en/topic/81063-auto-discovery-creates-all-aws-cloudwatch-sensors ,
  https://kb.paessler.com/en/topic/66983-auto-discovery-creates-duplicate-entries

Zabbix
* https://www.zabbix.com/documentation/current/en/manual/discovery/network_discovery
* https://www.zabbix.com/documentation/current/en/manual/discovery/network_discovery/rule
* https://www.zabbix.com/documentation/current/en/manual/config/notifications/action/conditions
* https://www.zabbix.com/documentation/current/en/manual/config/notifications/action/operation
* https://www.zabbix.com/documentation/7.0/en/manual/api/changes
* https://www.zabbix.com/documentation/current/en/manual/discovery/auto_registration
* https://www.zabbix.com/documentation/current/en/manual/discovery/low_level_discovery

LibreNMS
* https://docs.librenms.org/Extensions/Auto-Discovery/
* https://docs.librenms.org/Extensions/Device-Groups/
* https://docs.librenms.org/Alerting/Rules/
* https://docs.librenms.org/Extensions/Services/
* https://docs.librenms.org/Extensions/Distributed-Poller/
* https://community.librenms.org/t/device-group-members-disappearing/3696

Auvik
* https://support.auvik.com/hc/en-us/articles/206303333 (discovery settings)
* https://support.auvik.com/hc/en-us/articles/204565804 (device management)
* https://support.auvik.com/hc/en-us/articles/202956414 (identification)
* https://support.auvik.com/hc/en-us/articles/360048431532 (credentials)
* https://support.auvik.com/hc/en-us/articles/27942890723732 (alert tags)
* https://support.auvik.com/hc/en-us/articles/360040421152 (audit log)
* https://support.auvik.com/hc/en-us/articles/51296898905236 (billing counts)

Datadog NDM
* https://docs.datadoghq.com/network_monitoring/devices/snmp_metrics/
* https://docs.datadoghq.com/network_monitoring/devices/profiles/
* https://docs.datadoghq.com/network_monitoring/devices/guide/device_profiles/
* https://datadoghq.dev/integrations-core/tutorials/snmp/profile-format/
* https://docs.datadoghq.com/monitors/configuration/
* https://github.com/DataDog/datadog-agent/blob/main/pkg/config/example/datadog-agent_linux.yaml.example

LogicMonitor
* https://www.logicmonitor.com/support/the-icmp-discovery-method
* https://www.logicmonitor.com/support/enhanced-script-netscan
* https://www.logicmonitor.com/support/dry-run-for-enhanced-script-netscan
* https://www.logicmonitor.com/support/terminology-syntax/what-is-appliesto-scripting
* https://www.logicmonitor.com/support/applying-datasources-to-resources
* https://www.logicmonitor.com/support/resource-group-overview
* https://www.logicmonitor.com/support/alert-rules
* https://community.logicmonitor.com/discussions/product-discussions/enhanced-script-netscans-continually-overwrites-resource-groups/18760

Checkmk
* https://docs.checkmk.com/latest/en/wato_rules.html
* https://docs.checkmk.com/latest/en/wato.html
* https://docs.checkmk.com/latest/en/hosts_setup.html
* https://docs.checkmk.com/latest/en/labels.html
* https://docs.checkmk.com/latest/en/wato_services.html
* https://docs.checkmk.com/latest/en/dcd.html
* https://docs.checkmk.com/latest/en/hosts_autoregister.html
