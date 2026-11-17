# Discovery rules (phase 1)

Discovery rules classify what a discovery scan finds: auto-import it with the
right group, template, tags and polling settings, keep it for review, or
ignore it. They live under **Discovery → Rules** and are off until an
administrator turns the engine on.

The research and the four-phase plan are in
`docs/plans/discovery-rule-engine-plan.md`. Phase 1 (this document) acts only
on scan results: it never changes a device or server that already exists.

## Rules

A rule is global (not owned by a discovery profile) and has:

* **When**: the asset kinds it applies to (network devices, servers) and,
  optionally, the discovery profiles it is limited to. A scan result counts as
  a server when it looks like one (Windows/Linux/ESXi OS or type `server`).
* **If**: a condition tree. Groups are *All of* (AND), *Any of* (OR) and
  *None of* (NOT), nested at most three levels, at most 50 conditions. An empty
  *All of* matches everything (a catch-all rule).
* **Then**: actions (below).
* **Stop processing**: a matching rule ends the evaluation.
* **Mode**: Draft (never evaluated by scans), Active, Disabled.

### Facts and operators

| Type | Facts | Operators |
| --- | --- | --- |
| text | hostname, FQDN, sysName, sysDescr, sysLocation, vendor, model, device type, OS, serial, MAC | equals, does not equal, contains, does not contain, starts with, ends with, matches regex, is one of, is empty, is not empty |
| OID | sysObjectID | equals, is under (subtree), is not under, is one of, matches regex, is empty |
| version | OS version | equals, contains, starts with, matches regex, older than / at most / newer than / at least |
| IP | IP address | in subnet(s), not in subnet(s), in range, equals, is one of |
| number | identification confidence | =, ≠, <, ≤, >, ≥, between |
| set | open TCP ports, protocols detected | has any of, has all of, has none of, is empty |
| enum | credential status, result status | is, is not, is any of |
| reference | SNMP credential that worked, discovery profile | is, is not, is any of, is empty |
| boolean | server-class host | is true, is false |

* Text comparisons ignore case. OIDs are normalised (`iso.3…`, `.1.3…` and
  `1.3…` are the same) and "is under" is a subtree test: `1.3.6.1.4.1.12356`
  matches `…12356.101.1` but not `…123567`.
* Versions compare the first dotted number numerically (`v7.2.10,build1706`
  is 7.2.10, newer than 7.2.9; 7.2 equals 7.2.0).
* **Missing facts** never satisfy a positive test. Negative operators are true
  for a missing value: an unknown vendor "does not equal Fortinet". Use
  *is empty* / *is not empty* to test for the missing value itself. Preview
  shows the effect before a rule is active.
* **Regular expressions** are validated when the rule is saved: at most 200
  characters, no nested quantifiers such as `(a+)+` and no back-references,
  because Python's `re` has no time limit. They run against at most 2,048
  characters of the fact.

### Actions

| Action | Kind | Permission to activate |
| --- | --- | --- |
| Import decision: Auto-import (as device, server, both or auto), Keep for review, Ignore | single | `discovery.run` (+ `servers.manage` when it can create servers) |
| Device group, monitoring template, device type, location, SNMP credential (by id), ping interval, SNMP poll interval | single | `devices.manage` |
| Add tags | additive | `devices.manage` |

Group, template and SNMP credential are stored as ids and checked when the
rule is saved; no secret is ever copied into a rule, a run item or an export.
Tags are stored in the tag registry's spelling (`tag_service.canonicalize_tags`).

### Order and conflicts

Rules are evaluated in priority order (top of the list first; Move up / Move
down reorders). For each single-valued action the **first matching rule wins**;
a later matching rule that sets the same action is recorded as *shadowed*. Tags
from every matching rule are merged. A matching rule with *Stop processing*
ends the evaluation. Priorities are unique, so there are no ties.

The import decision follows the same first-match rule, so the usual pattern
works: specific *Auto-import* rules at the top, a catch-all *Ignore* at the
bottom. (The plan's "ignore beats import" was dropped for this reason.)

## What happens to a scan result

At the end of every scan (and when an operator re-evaluates a past scan) the
active rules are evaluated for each result that answered and is not yet
imported or ignored. The plan is stored on the result (`rule_plan`,
`disposition`, `matched_rule_ids`) and shown in the Rule and Decision columns
of the scan results, with the reason.

The profile's **import mode** decides what the rules may do:

| Profile import mode | Auto-import | Ignore |
| --- | --- | --- |
| `review` (default) | shown as "would import" | shown as "would ignore" |
| `auto_match` | done | done |
| `ignore_match` | shown as "would import" | done |

Dispositions: `imported`, `ignored`, `review` (a rule kept it, or reasons
`not_ready`, `cap`, `license_capacity_reached`, `conflict`, `error`),
`would_import` / `would_ignore` (profile does not allow it), `existing`
(already monitored: annotated, never changed in phase 1), `no_match`.

* **Auto-import** goes through the same code as the Import dialog
  (`app.services.discovery_import`), under the `discovery-import` lock and the
  license budget (`license_capacity.reserve`, see `docs/license-capacity.md`).
  Only results that are import-ready (new, credentials valid or not tested)
  are auto-imported. At most `max_auto_import_per_run` per scan;
  `license_reserve_nodes` nodes are always left free. What does not fit stays
  in review with the reason and the rules run is `partial`.
* **Ignore** adds the address to the Ignored list with the reason
  "Discovery rule: <name>"; later scans skip it like a manual ignore.
* **Settings** for an imported device: the Import dialog's explicit choices,
  then the rule values, then the profile defaults. The result's suggested
  group, template and tags are the rule value else the profile default (tags:
  profile defaults plus rule tags), so a later manual import uses them too.

A failure inside the engine is written to the scan's activity log and never
fails the scan.

**Re-evaluate rules** on a finished scan (Scan Results, `discovery.run`)
evaluates the current active rules again on the results not yet imported or
ignored: *Annotate only* refreshes the Rule and Decision columns, *Apply* also
acts as the profile allows. Apply is how results held back by the per-scan
limit or the license capacity are retried once room is made.

## Preview

The rule editor previews an unsaved rule against any past scan, either among
the active rules (what a scan would really do) or alone, and can pretend the
profile were `auto_match`. It shows the counts ("would import 17, ignore 3,
review 22; license room 12"), the per-result decision and settings and a "why"
trace for each rule. Sample facts can be typed in to test one device. Preview
and the real evaluation call the same functions (`engine.evaluate`,
`engine.disposition`, `runner.assign_capacity`), and a test compares them.

## Engine settings

`system_settings['discovery_rules']`, edited on the Rules page:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Scans evaluate rules only while on |
| `max_auto_import_per_run` | 50 | Auto-imports per scan or re-evaluation |
| `license_reserve_nodes` | 0 | Nodes always left free for manual adds |
| `history_days` | 180 | Rule-run history retention (minimum 7) |

## Permissions and scope

* `discovery.view`: see rules, the catalog, previews and history.
* `discovery.manage_rules` (new; administrators and the built-in Network Team
  role; built-in definition version 7): create, edit, order, change mode,
  engine settings. Activating a rule also needs every permission its actions
  use; `activated_by` is stored.
* `discovery.run`: re-evaluate a past scan (it can import).
* Rules are fleet-wide: a user whose view is limited by tags cannot change
  them (403), the stance fleet-wide alert rules take. Discovery results are
  not tag-scoped today, so history is not filtered in phase 1.
* A subnet-to-tag rule is how a newly imported device becomes visible to the
  right team: untagged devices stay hidden from scoped users.

Every rule change, mode change, reorder, settings change and re-evaluation is
written to the audit log (`discovery.rules.*`).

## API

Under `/api/v1/discovery-v2/rules`:

| Endpoint | Use |
| --- | --- |
| `GET /catalog` | Fields, operators, actions; the builder is drawn from it |
| `GET/PUT /settings` | Engine settings |
| `GET ""`, `POST ""` | List (priority order), create (`position` first/last) |
| `GET/PUT/DELETE /{id}` | One rule; `PUT` takes `expected_revision` (409 when stale) |
| `POST /{id}/mode`, `POST /{id}/clone`, `GET /{id}/versions` | Mode, draft copy, version history |
| `POST /reorder` | New order (every rule once) |
| `POST /preview` | Plans for an unsaved rule against a past scan or sample facts; writes nothing |
| `POST /evaluate-scan/{run_id}` | Re-evaluate a finished scan; `apply` acts as the profile allows |
| `GET /history`, `GET /history/{id}/items` | Rule runs and what each rule did |

## Data

Migration 190: `discovery_engine_rules`, `discovery_engine_rule_versions`,
`discovery_engine_runs`, `discovery_engine_run_items`, three columns on
`discovery_results_v2`, the settings row. The legacy `discovery_rules` table
(migration 017) was never written by any code path and is left in place,
superseded. Columns for later phases (`enforcement`, `on_unmatch`,
`max_changes_per_run`, mode `simulate`) exist but are not used yet.

## Not in phase 1

Rules on existing devices and servers, ownership of rule-set values and undo,
simulation mode, evaluation on Add Device / CSV import / agent enrolment,
continuous re-evaluation, NCCM / Device Tracker / alert exclusion /
maintenance / map actions, controller children, export/import and starter
rules. See the plan, phases 2 to 4.

## Tests

* `server/tests/test_discovery_rules_engine.py`: operators, groups, order,
  shadowing, merge, stop, dispositions, capacity assignment, 5,000 assets x
  100 rules in under 2 s, and the contract vectors in
  `scripts/dev/discovery-rules-contract/vectors.json`.
* `server/tests/test_discovery_rules_pg.py` (disposable PostgreSQL): the phase
  1 acceptance (auto-import with rule settings, review profile, preview equals
  apply, license capacity and reserve, ignore list, existing devices
  untouched, CRUD/order/versions/audit, permissions and tag scope).
