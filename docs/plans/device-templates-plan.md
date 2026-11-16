# Device Templates, classification and grouping: research and phased plan

Status: 2026-11-16. Phase 1 is built on branch `claude/device-templates-phase1` (migration 189, see
`docs/device-templates.md` for what shipped and where it differs from this plan); phases 2 to 5 are
not implemented. Based on main at `55492d8` (1.25.9)
and on the unmerged Discovery Rule Engine branch `claude/discovery-rules-phase1` (PR #160).
Shareable copy: https://claude.ai/artifact/Mjt1zjmhUt7ebzguwu72nE
Companion plan: `docs/plans/discovery-rule-engine-plan.md` (on the PR #160 branch).

## 1. Recommendation

Keep `device_profiles` as the one template object and build three layers on it, each with a
single job, resolved in a fixed order that every screen can explain:

1. **Template**: what a device model can report and how to read it. Vendor knowledge, versioned,
   upgradable.
2. **Policy**: what this customer wants for a class of devices (intervals, thresholds, who is
   told, NCCM, credentials). Ordered, previewable.
3. **Device override**: a recorded exception on one device, with a reason.

OpManager puts all three into the template, which is why its only way to push a change is
"Associate & Overwrite". Separating them is the main design decision of this plan.

* ZenPlus already has the hard half: per-model SNMP collection with table walks, state labels,
  applicability rules and controller children. That half is deeper than OpManager's.
* ZenPlus lacks the management half: a device type catalogue, thresholds that follow the
  template, interface defaults, usable groups, custom fields, versions, preview and a test
  against a real device.
* The Discovery Rule Engine (PR #160) decides which group, tags, type and template an asset
  gets. This plan defines what those things do once assigned. Both use one condition language,
  one preview and one record of who set what.

Build phase 1 first. It makes the existing templates trustworthy (type catalogue, one
classifier, a real editor, test on a device, versions, rollback) and changes no polling for a
device unless an operator publishes a template change.

## 2. Does ZenPlus have this today?

Partly. ZenPlus 1.25.9 has the collection half of a device template. It lacks most of the
management half.

Checked against the code on main and the live appliance at 192.168.8.221 (1.25.9, 106 devices,
10 built-in templates, 305 metrics, 8 devices covered). File paths are in section 6.

| OpManager capability | ZenPlus 1.25.9 | Verdict |
| --- | --- | --- |
| Template per device model: name, vendor, category, icon | `device_profiles` ("Monitoring Templates") has name, vendor, description. No device type, category or icon on the template | Part |
| Classify by sysObjectID, with extra criteria (sysDescr or a custom OID) | Longest sysObjectID prefix, else sysDescr regex. Assigned only while the device has no template, never re-checked. No custom-OID test. The Go and Python matchers order the sysDescr fallback differently | Part |
| Device categories, custom categories with a parent | `devices.device_type` is limited to 7 values by a database CHECK. Dashboard lists offer `storage`, `ups`, `load_balancer`, which that CHECK rejects | Lacks |
| Monitors attached to the template, each with an interval | `oid_groups`: scalar and table groups, per-group interval, value maps, state labels, applicability rules, page tabs, controller children | Has, deeper |
| Monitor catalogue, "Query Device", "Test Monitor" | Editor is a raw JSON text box. The MIB Library is not connected to it. No test of a template against a device | Lacks |
| Thresholds in the template that raise alarms (attention, trouble, critical, rearm, consecutive times) | Metric `thresholds` only colour the device page. Alarms come from separate alert rules. The API accepts `tpl_*` metrics but the rule wizard has no picker, and a rule cannot target a template or a vendor | Part |
| Associate a template with devices; Associate & Overwrite | Assign and unassign API and a Devices tab in the template dialog. No bulk action on the Devices page, no preview, no audit entry, old readings left behind | Part |
| Copy, import, export | Clone, Export JSON, Import JSON | Has |
| New templates from the vendor (Sync Templates) | Built-ins arrive with OTA migrations. A clone has no link to its source and gets no updates | Part |
| Template versions, history, rollback | A `version` column that nothing uses as history | Lacks (OpManager also lacks) |
| Interface templates per interface type | None. Every interface is monitored by default. No per-type interval or threshold | Lacks |
| Device groups: manual or by criteria, sub-groups, group health | `device_groups` with `parent_id`, one group per device. No API or page to create, edit or delete a group. No membership by rule | Part |
| Custom fields on devices and interfaces | None. Only owner, notes, description, serial | Lacks |
| Bulk configuration by category (Quick Configuration Wizard) | Bulk edit, bulk tag, bulk delete on the Devices page; NCCM bulk assign | Part |
| Notification profile by category, group or business view | Alert rules scope by device, one group, one type, location text or one tag, and carry channels, P1 to P4 priority and an escalation policy | Part, stronger escalation |
| Turn a shared rule off for one device | `alert_rule_exclusions` (1.25.9) | Has, OpManager lacks |
| Downtime schedule by category or group | `device_maintenance` by device, group, tag or all. One-off windows only | Part |
| Rules that attach templates at discovery | Discovery Rule Engine phase 1, PR #160, not merged | In progress |
| Controller-managed children read from the controller's template | FortiSwitch and FortiAP rows from the FortiGate template | Has, OpManager has no equivalent |
| Per-vendor and per-type page layout | `device_widget_layouts`, per user | Has |

## 3. OpManager's device templates, classification and grouping

Read from the public demo on 2026-11-16 (forms opened, nothing saved) and from ManageEngine's
help pages. Source keys are in section 13.

### 3.1 How it works end to end

1. **Discovery reads sysObjectID.** The template whose device identifier matches decides the
   device's type (the template name) and category [O1][O7].
2. **No match.** OpManager falls back to a vendor template and creates a custom template named
   "<Vendor> Device": availability only, no category, no monitors. Wrong credentials or a
   missing sysOID leave the device in "Unknown" [O1][O3][O4].
3. **The template's monitors are attached** to the device with their intervals and thresholds.
4. **Later changes** reach devices only when an operator presses Associate (new monitors only)
   or Associate & Overwrite (replace every monitor and threshold) [O1][O8].
5. **Alarms** come from the thresholds. Notification profiles, escalation rules, downtime
   schedules and workflows are separate objects chosen by device, category, business view or
   group [O28][O32].

### 3.2 What the screens hold

| Part | What the demo shows | Where it stops |
| --- | --- | --- |
| Template list | 11,592 templates in the demo. Columns Name, Category, Devices; Export and Delete per row; All / Custom filter; Add Template, Associate, Import | Most are a sysOID-to-type mapping with a few generic monitors. No version, no last-changed, no coverage |
| Edit template | Name, Vendor (pick or New), Category, icon, availability monitoring interval, device identifier (SNMP or CLI) with Add and Query Device, associated monitors, Copy, Save, Save & Associate | One category per template. At discovery the interval comes from the category setting in the Quick Configuration Wizard, not from the template [O1] |
| Identifier criteria ("Edit SysOID") | Criteria: Sys Description or Custom OID. Condition: Equals, Not Equals, contains, Not Contains, Starts With, Ends With, regular expression, Implemented | Two templates cannot share a sysOID; same-sysOID models need these extra criteria [O5][O6]. No priority, no "why did this match" |
| Associated monitors | Name, Type (SNMP, REST API seen), Interval, Show Dial switch, Threshold enabled or not | A monitor is a flat OID or expression. Tabular OID monitors support neither expressions nor thresholds (snippet) [O20] |
| Add monitor | Catalogue (315 offered for a Cisco ACI template: 271 vendor, 6 general, 24 UCS, 10 RMON, 4 custom) with Test Monitor; SNMP tab (device, OID chooser, functional expression, perf group, Query Device); Bulk SNMP tab (rows of OID, name, expression, interval, units, thresholds) | Scalar and multi-instance OIDs cannot be mixed in one expression [O18] |
| Edit thresholds | Per monitor: interval, dial, adaptive threshold, Attention / Trouble / Critical each with an operator and a value, rearm value, consecutive times | Thresholds are also set on the device, on the Performance Monitors page, in the wizard and by rule engine actions. The only documented precedence is that rule engine thresholds override template and device [O12][O19] |
| Associate | Vendor, template, device picker with filter. Associate: "newly added monitors without affecting any existing monitors or its thresholds". Associate & Overwrite: "replace all existing monitors and its thresholds" | All or nothing. No per-setting override, no preview, no undo |
| Device categories | 33 in the demo (Server, Switch, Router, Firewall, UPS, Meraki classes and so on), each with an optional parent; Add Category; Import Devices | A custom category inherits monitoring properties from its parent [O2]. Rediscovery rewrites type, vendor and category when the type resolves [O17] |
| Custom fields | Device fields and interface fields (Building, Cabinet, Department, SerialNumber and so on), Add Field, Import values, Associate | Text, numeric or date [O30]. Not a scoping primitive on their own |
| Groups | Wizard: group info, selection (manual or by criteria), status notification. Members are devices, interfaces, groups, IP SLA or applications. Device criteria: Category, DNS Name, Display Name, IP Address, Parent Net, Type, Vendor, Virtual Type and the custom fields | Group type and creation method cannot change after creation; at most four nested levels [O24][O27]. No preview of members in the wizard |
| Interface templates | One per ifType (Ethernet 6, Tunnel 131 and so on): manage or unmanage, interval (900 s), thresholds for utilisation, error rate, discard rate and power, rearm, consecutive times, status poll, Save and Apply. Interface groups carry thresholds for their members | Applying overrides the existing interface configuration in full [O23] |
| Quick Configuration Wizard | 15 bulk tools: device templates, notification profiles, service monitors, threshold settings, event log rules, dependencies, credentials, delete, manage/unmanage, alarm suppression, intervals, display name, custom field values, dissociate monitors, forecast alerts | Each tool is a one-off push. Nothing records what was pushed where |

### 3.3 Where it is weak

* **Overwrite is all or nothing.** Plain Associate does not update existing monitors; Associate
  & Overwrite destroys local thresholds. No diff, preview or per-field merge [O1][O8].
* **No versions, history, rollback or inheritance** for templates is documented. Import is an
  opaque XML file that must not be edited by hand [O13].
* **Thresholds live in six places** with one documented precedence rule [O12][O19].
* **Policy is fragmented.** Template, notification profile, escalation rule, downtime schedule
  and workflow are separate objects. The rule engine bundles some of them, only at discovery or
  on a manual re-run [O12].
* **One template, one category**, and rediscovery rewrites the category [O17]. A forum user
  reports custom categories lost on rediscovery [O36].
* **Unmatched devices get availability only** until someone writes a sysOID template [O1][O3].
* **Escalation is one step**: one email and one SMS target per rule [O32].
* **Reported failures** (forum, old builds, no staff reply seen): template thresholds not
  reaching existing devices [O37]; re-applying a template removing VMware monitors [O38];
  custom template thresholds lost after an upgrade [O42]; all devices back to Unknown after an
  upgrade [O43].
* **API**: list, view, update, delete and associate exist for templates; no create, import or
  export call was found [O26].

Not verified: whether an upgrade or Sync overwrites an edited default template; default
interface threshold values; whether groups can scope a downtime schedule (the FAQ and the help
page disagree) [O27][O45].

## 4. Competitors

"Snippet" marks a claim seen only as a search excerpt. "Not verified" means no source was found;
it is not a claim that the feature is absent. LogicMonitor, Auvik and most SolarWinds rows are
snippet-level.

| Product | Unit that defines monitoring | How a device gets it | Override order | Vendor updates and local edits | Groups and labels | Preview and explain | Unlink |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpManager | Device template: identifier, category, monitors, thresholds | sysOID match at discovery; manual Associate | Last write wins; rule engine thresholds override | Sync adds templates; effect on edited ones not verified | Categories, groups by criteria, custom fields, business views | Query Device, Test Monitor | Dissociate monitors tool |
| Zabbix 7.0 | Template: items, triggers, graphs, dashboards, discovery rules, tags, macros, value maps | Manual link, mass update, discovery and autoregistration actions | Macros: host, then templates by depth, then global; same-depth ties by lowest template ID | Not upgraded with the server; bulk import overwrites with no undo; import shows a diff | Host groups, template groups, tags on every entity | Resolved macro view; import diff | "Unlink" keeps data, "Unlink and clear" deletes |
| PRTG | Device template: a snapshot of one device's sensors | Auto-discovery only; no lasting link | Tree: root, probe, group, device, sensor; break inheritance per settings block | None; templates not editable in the UI | Tree groups, inherited tags, libraries | None found | n/a |
| SolarWinds | Several: Device Studio pollers, Universal Device Pollers, SAM application templates | Manual; SAM templates by group membership | Global thresholds unless a node overrides; SAM per-setting override | Re-import overwrites or copies (snippet) | Dynamic-query groups, custom properties | Group Preview | SAM "Auto Delete" on leaving a group (snippet) |
| LibreNMS | OS definition YAML, shipped with the code | sysObjectID, then sysDescr, then snmpget | No hierarchy; per-sensor limits per device | Ships with the code; no local layer documented | Static and dynamic groups over database columns | `lnms device:discover -vv` | Service template Remove deletes its services (snippet) |
| Datadog NDM | Device profile YAML: sysobjectid, metrics, tags | sysObjectID match or explicit `profile:` | `extends` chain; custom profile beats same-named default | Defaults reset on Agent upgrade; editing a built-in makes a copy (snippet) | Tags everywhere | Lists devices matched to a profile (snippet) | n/a |
| LogicMonitor | LogicModule (DataSource): collection, instance discovery, thresholds | AppliesTo expression, re-evaluated on every property change | Thresholds: global, group, resource, instance group, instance; same-level ties nondeterministic | Safe merge with diff and "preserve" toggles, opt-in each time | Dynamic groups carry properties and thresholds | Alert Thresholds report | Un-matching deletes the DataSource's history |
| Checkmk | No template: check plug-ins detect, rule sets parametrise | Rules on folder, host tags, labels | Declared per rule set: first match, per-parameter merge, or all; lower folder wins | Versioned extension packages; staged changes with revert | Folders, tags, labels (explicit, rule, discovered) | Effective parameters per host; match status per rule | n/a |
| Domotz | Device profile: type, credentials, alerts, sensors | Apply to devices or a saved filter; optional schedule | Overwrite or Additive per module | n/a | Saved filters | None found | "Cannot be undone"; deleting a profile leaves settings |
| Auvik | Fixed vendor classification | Automatic | Site hierarchy | n/a | Sites, alert tags | n/a | n/a |
| ZenPlus 1.25.9 | `device_profiles`: match rules and `oid_groups` | Auto when none is set; manual assign | None; alert rules separate | Built-ins by OTA migration; clones cut off | One static group, flat tags, free-text location | None | Unassign deletes current readings |
| ZenPlus planned | Template (model knowledge) + policy (customer intent) + device override | Classifier, discovery rule or operator, each recorded; re-checked on identity change | One published order: defaults, template chain, policies by priority, device override | Built-in stays intact; local edits live in an overlay; update shows a diff; nothing auto-enabled | Static tree, dynamic groups, typed custom fields, flat tags for visibility | Test on device, impact preview, effective configuration with the source of every value | Keep or clear is a choice; history is never removed by a rule edit |

What each does well, and where it falls short:

* **Zabbix**: richest template content, context macros for per-instance thresholds, ordered
  discovery overrides with "stop processing", explicit keep-or-clear on unlink. Weak: linking
  fails on a duplicate item key and discards host-level edits to same-key entities; edited
  triggers are overwritten on template update, so operators clone and diverge (snippets).
* **PRTG**: zero-configuration first run; inheritance down one tree is easy to reason about.
  Weak: a template is a one-time stamp, not editable in the UI, and a changed template does not
  change existing sensors (snippet).
* **SolarWinds**: dynamic groups with a Preview button; per-setting "Override template /
  Inherit from template" in SAM. Weak: several unrelated template kinds; alert scope uses custom
  properties rather than groups; overrides are reset one at a time (snippets).
* **LibreNMS**: detection and sensors as reviewable YAML; dynamic groups as the one scoping
  primitive. Weak: no per-device-type limits; custom sensor limits reset when rediscovery
  renumbers sensors (snippets).
* **Datadog**: `extends` composition, `_base` profile, tags as the universal join. Weak:
  profiles carry no thresholds, so a profile gives no default alerts.
* **LogicMonitor**: one expression language everywhere, continuous re-evaluation, five
  threshold levels, a merge that shows local differences. Weak: preserve is opt-in on every
  update; ties are nondeterministic; un-matching deletes history (snippets).
* **Checkmk**: the best explanation of effective settings, per-parameter merge, staged changes.
  Weak: moving a host can change its settings without notice; revert is all or nothing.
* **Domotz**: a profile bundles type, credentials, alerts and sensors, close to the owner's
  "combine actions" goal. Weak: applying cannot be undone and leaves no link.

## 5. What "better" means

1. **Three layers, one order.** Template, policy, device override. Every effective value names
   its source. No product above separates vendor knowledge from customer intent and also
   explains the result.
2. **Local edits survive vendor updates by construction.** A built-in template is never edited.
   A customer change is an overlay on top of it, so an update can never overwrite it and no
   merge step is needed.
3. **See it before it happens.** Test a template against a real device before saving. Preview a
   template, group or policy change: which devices, which metrics appear or stop, which alert
   rules are affected.
4. **Thresholds are alert rules, not a second engine.** A template recommends rules. Nothing is
   enabled or changed without an operator. A class or a device gets a different number through a
   recorded override, not a cloned rule.
5. **One classifier, tested by contract.** Go and Python read the same vectors. Every match can
   be explained. A manual choice is never overwritten.
6. **Unmatched is not unmonitored.** A device with no vendor template still gets the generic
   template (system, CPU, memory, interfaces, standard routing), and appears in a queue.
7. **Groups you can use everywhere.** One static tree plus dynamic groups defined by the same
   conditions the Discovery Rule Engine uses. The same group scopes alert rules, maintenance,
   policies, reports and maps.
8. **Versions, staged rollout, rollback.** Draft, publish to a pilot set, publish to all, roll
   back in one step. History of collected data is never deleted by a template or rule edit.
9. **Drift is visible.** Per device: template revision, metrics that do not answer, overrides
   in force, "a better template now matches".
10. **Children and licences stay correct.** Controller-managed children are never polled,
    pinged or licensed because of a template, group or policy.

## 6. ZenPlus today

| Area | What exists | Gap |
| --- | --- | --- |
| Template table | `device_profiles` (`scripts/migrate-004-snmp.sql:45`, model `server/app/models/device_profile.py`): name, vendor, `match_rules`, `oid_groups`, `version`, `builtin`, description; unique (name, version) | No parent, no revision history, no `builtin_key`. `version` is in the unique key and was bumped in place once (`migrate-120`), so a later upsert on (name, 1) could insert a duplicate |
| Template content | `server/app/schemas/snmp.py:85-270`: `OidGroup` (key, name, `interval_seconds`, kind scalar/table, `table`, `metrics`, `children`, `display`, `belongs_to`, `tab`, `applies_when`), `OidMetric` (key, oid, type, unit, scale, `value_map`, `labels`, `thresholds`), `MatchRules` (`sys_object_id_prefixes`, `sys_descr_regex`, `default_*`, `extract_*`) | `labels` and `thresholds` are display only; the poller ignores them (`poller/internal/checker/snmp/oidgroups.go`) |
| Built-ins | 10 packs seeded and patched by migrations 062, 063, 064, 069, 080, 081, 120, 182: FortiGate, Cisco IOS/IOS-XE, Cisco ASA, PAN-OS, F5 BIG-IP, JunOS, Aruba Wireless Controller, Aruba AOS-CX, Dell OS10, MikroTik | The API refuses edits to a built-in (`api/v1/snmp.py:962`). The only route is Clone, which cuts the copy off from updates |
| Standard groups | `MergeStandardNetworkOidGroups` (`poller/internal/checker/snmp/standard_network_groups.go`) adds hard-coded BGP, OSPF, VRRP, STP, LACP groups to every network device | Not visible in any template; only the hardware report pack reads them |
| Classification | Go `Match` (`poller/internal/checker/snmp/profile.go:167`): longest sysObjectID prefix, else first sysDescr regex in name order. `AssignProfileIfUnset` (`poller/internal/store/postgres.go:528`) writes only when `profile_id IS NULL` | Never re-checked. No record of auto versus manual. Python `_classify_from_db` (`api/v1/snmp.py:243`) orders the fallback by name length, unlike Go. Discovery v2 copies the profile default into `matched_template_id` without matching |
| Template API | `/api/v1/snmp/profiles`: list, get, create, update, delete, clone, export, import, assign, unassign, devices (`api/v1/snmp.py:875-1145`). Writes need `devices.manage` through the router policy (`core/route_policy.py:243`) | No test against a device, no preview, no history, no audit on assign. Assign and delete leave old `device_template_values` rows. The sidebar entry asks for `settings.manage` while the API asks for `devices.manage` |
| Template UI | Administration, Settings, Monitoring Templates (`dashboard/src/components/TemplatesTabContent.tsx`): cards, detail dialog (metric groups, devices, auto-match), Export, Clone, Import JSON, New Template | The editor is name, vendor, description, sysObjectID prefixes, sysDescr regex and a JSON text area. Saving sends only those two match keys, so `extract_*` and `default_*` are dropped from an edited clone (`:606-611`) |
| MIB Library | Upload, compile, search symbols (`api/v1/snmp.py:740-867`, `MibsTabContent.tsx`) | Not connected to the template editor |
| Readings | `device_template_values` (migrations 062, 182), ClickHouse `snmp_metrics` as `tpl_<key>`; per-group cadence by `interval_seconds` (`collector_profile.go`) | No per-device change to a template's groups or intervals |
| Device page | `server/app/services/device_insights.py` (`shape_template_groups`, `group_applicability`, `template_kpis`), `GET /devices/{id}/summary`, `dashboard/src/pages/device/` | Applicability and tabs for built-ins are hard-coded maps (`BUILTIN_APPLICABILITY`, `BUILTIN_TABS`), not template data |
| Device type | `devices.device_type` with a CHECK of router, switch, firewall, server, access_point, printer, other (`scripts/init-postgres.sql:35`); free-text vendor, model, os_version; `inventory_identity` (migration 157) | No type table, no icon or category per type. `DevicesPage.tsx` offers storage and ups, `network-icons` knows load_balancer, and the PR #160 `device_type` action accepts load_balancer, wireless_controller, ups, storage. No migration in the repository drops the CHECK (the live database was not queried) |
| Groups | `device_groups` (id, name, description, color, `parent_id`, `snmp_credential_id`); `devices.group_id`; `GET /devices/groups` | No create, edit or delete API or page. `parent_id` unused. The group SNMP credential is stored and never applied. No server groups |
| Location and sites | `devices.location` free text, matched by substring in alert rules; `sites` table used by servers, sensors and agents | Devices have no `site_id` |
| Tags and scope | `tags` registry (migration 067), `devices.tags`, `servers.tags`; `tag_service.canonicalize_tags`; `core/scoping.py` (`visible_tags`, `assert_tags_assignable`); `users.scope_tags`, `roles.scope_tags`, `roles.page_access` | Flat strings. Tags are the visibility boundary, so they should stay simple |
| Custom fields | None | |
| Alert rule scope | `alert_rules`: `device_id`, `group_id`, `device_type`, `location`, `scope_tag`, `server_id`, `target`, plus `min_duration`, `clear_duration`, `priority`, `escalation_policy_id`. Evaluators: `network_alert_service._device_in_scope`, `alert_engine._rule_in_device_scope`, `evaluate_trap`, `network_events`, `udt_alert_service._rule_scope_ok`. `alert_rule_scope.impact` and `guard_rule_change` | One value per dimension. No template or vendor scope. The evaluators differ (traps ignore type and location; Device Tracker ignores tags). No threshold override per device or group |
| Rule exclusions | `alert_rule_exclusions` (migration 187, `services/alert_rule_exclusions.py`, `docs/alert-rule-exclusions.md`) | On or off only |
| Escalation | `escalation_policies`, `escalation_steps`, on-call tables, `notification_digests` (migration 164); routing rule policy, then the rule's legacy levels, then the priority default | No routing by group, type or template |
| Credentials | `snmp_credentials` and `device_service.apply_credential` (values copied onto the device row for the poller); `ncm_credentials`, `device_ncm`, `POST /ncm/bulk-assign` | No default by group, type or template |
| Polling settings | Per device: `ping_interval`, `snmp_poll_interval`, timeouts. Device status policy is one global setting (`system_settings['monitoring']`) | No class-level setting |
| Interfaces | `device_interfaces` (`monitored` default true, `tags`, `configured_speed_bps`); `if_*` rule metrics with a `target` filter | No defaults by type, no thresholds per interface class |
| Maintenance | `device_maintenance` by device, group, tag or all | One-off only |
| Layouts | `device_widget_layouts` by user and device, or user and vendor + type | No shared default for everyone |
| Servers | `agent_policies` assigned per agent, token or platform default; host rules for one server or all | No server groups; outside the device template |
| Discovery rules (PR #160) | `discovery_engine_rules`, versions, runs, run items (migration 190); condition tree in `server/app/services/discovery_rules/conditions.py`; facts in `facts.py`; actions `decision`, `group`, `template`, `device_type`, `location`, `snmp_credential`, `ping_interval`, `snmp_interval`, `tags` | Acts on scan results only. Existing assets, ownership records and re-evaluation are its phases 2 and 3 |
| Audit and versions | `write_audit_log` (`audit_logs`); per-feature history only (roles, maps, NCCM, discovery rule versions) | No template history |
| Licence | `license_capacity.NODE_COUNT_SQL` equals the updater's copy; children and service checks never count | Unaffected by this plan |

The live appliance matches the code: Monitoring Templates shows 10 built-in cards with Export
and Clone, the New Template dialog is the JSON editor, the Devices page filters by Status, Type,
Location and Tags, and no group management page exists.

## 7. Design

### 7.1 The three layers

| Layer | Holds | Who owns it | Changes when |
| --- | --- | --- | --- |
| Template | What the model can report: match rules, metric groups, units, state labels, applicability, children mapping, interface roles, default page layout, recommended alert rules, recommended NCCM platform | ZenPlus for built-ins; the customer for custom templates and overlays | A release ships a new built-in revision, or an operator publishes an edit |
| Policy | What the customer wants for a class: polling intervals, which recommended rules are on and at what values, escalation and channels, status-policy limits, SNMP and NCCM defaults, Device Tracker, maintenance | The customer | An operator publishes a policy change |
| Device override | One device differs: a threshold, an interval, a group or metric turned off | The operator who set it, with a reason and an optional expiry | Set or cleared on the device |

Effective configuration for a device is resolved in this order, later entries winning:

1. Product defaults.
2. The template chain, parent first.
3. Policies whose group contains the device, in priority order. For a single-valued setting the
   highest-priority policy wins and the others are recorded as shadowed. Lists merge.
4. Device overrides.

The order is the same for every setting. Priorities are unique, so ties cannot occur. One
resolver function produces the result for preview, for apply and for the "why" view.

### 7.2 Template model

* **One template object.** `device_profiles` stays. The user-facing name becomes "Device
  Templates".
* **One primary template per device** (`devices.profile_id`, as today). Composition happens
  between templates, not on the device, so two templates can never fight over one device.
* **Composition.** A template may extend one parent, at most three levels deep. A child may add
  groups and metrics, turn a parent's group or metric off, and change intervals, labels,
  display thresholds, tabs and applicability. It may not redefine a parent's OID under the same
  key.
* **Components.** A template of kind `component` holds groups shared by many models (the
  hard-coded standard BGP, OSPF, VRRP, STP, LACP groups become the first one). A device
  template lists the components it includes.
* **Overlays instead of clones.** "Customise" on a built-in creates a child that extends it.
  The built-in is untouched and keeps receiving updates; the overlay holds only the
  differences. Clone remains for a real fork.
* **Flattening.** On publish the server resolves the chain into `resolved_oid_groups`. The
  poller reads that one document, so the Go change is one query (`COALESCE(resolved_oid_groups,
  oid_groups)`), and a broken parent can never reach the poller half-applied.
* **Identity.** Built-ins get a stable `builtin_key`. Every template gets a `revision` and a
  history row per publish. The unused `version` column leaves the unique key.
* **Draft and published.** Edits are saved as a draft. Devices use the published revision.

### 7.3 Classification and hand-over from discovery rules

* **Device type catalogue.** A `device_types` table replaces the CHECK: key, label, parent,
  icon, sort order, built-in flag. Seeded with today's seven plus the values the dashboard and
  PR #160 already use (load_balancer, wireless_controller, ups, storage) and the ones customers
  ask for (pdu, ip_camera, voip, sdwan_edge). Custom types allowed. A type's parent gives the
  roll-up OpManager calls category (Network, Security, Wireless, Server, Power, Other), which
  the Devices page already charts.
* **One classifier, by contract.** Match rules gain an explicit `priority` and two tests:
  `probe` (an OID exists, equals or matches) and `sys_name_regex`. Order: priority, then longest
  sysObjectID prefix, then sysDescr, then probe. `scripts/dev/template-match-contract/vectors.json`
  is read by the Go and the Python implementation, as the device status contract is.
* **Who assigned it.** `devices.profile_source` records `auto`, `rule`, `manual` or `import`.
  Precedence: manual, then discovery rule, then classifier. The classifier never overwrites a
  manual or rule choice.
* **Re-check on identity change.** When sysObjectID, sysDescr or model changes, the classifier
  runs again. For an `auto` device it records a suggestion (`profile_suggested_id`) and, by
  default, waits for an operator; switching automatically is an owner decision (section 11).
* **Generic fallback.** A built-in "Generic SNMP device" template (system, HOST-RESOURCES CPU
  and memory, ENTITY sensors where present, interfaces, the standard routing component) is
  assigned when nothing matches. The Unmatched view lists those devices with their sysObjectID
  and sysDescr and a "create template from this device" action.
* **Division of work with the Discovery Rule Engine.** A discovery rule decides *which* template,
  type, group, tags, location and credential an asset gets, at discovery and (its phases 2 and
  3) for existing assets. A template never contains conditions about subnets or names; a rule
  never contains OIDs. A rule's `template` action sets `profile_source = rule`. The rule
  engine's `device_type` action validates against `device_types`.
* **Explain.** `GET /snmp/profiles/classify?device_id=` returns every candidate with the test
  that passed or failed and the winner.

### 7.4 What a template carries

| Section | Content | Phase | Notes |
| --- | --- | --- | --- |
| Identity | Name, vendor, description, default device type, icon, `builtin_key`, parent, components | 1 | Type and icon apply when the device's own type is still unset or auto |
| Matching | Priority, sysObjectID prefixes, sysDescr regex, sysName regex, probe tests, vendor/model/OS extraction | 1 | |
| Metric groups | As today, edited through forms with the MIB picker; unknown keys preserved | 1 | |
| Applicability and tabs | `applies_when`, `tab` as data; the hard-coded built-in maps move into the built-ins | 1 | |
| Children mapping | `children`, `belongs_to`, `display` as today | 1 (unchanged) | Only on a controller's template |
| Recommended intervals | Per group, as a recommendation shown beside the policy value | 1 | |
| Default layout | A shared default page layout for the template | 1 | Stored in `device_widget_layouts` with no user; a user's own layout still wins |
| Interface roles | Ordered matches on ifType, name, alias, speed giving a role (uplink, access, loopback, tunnel, management, virtual) and a default for monitored | 3 | Sets `monitored` only where no operator has |
| Recommended alert rules | Named rules on the template's metrics with suggested values, durations and priority | 3 | Become ordinary `alert_rules` only when an operator accepts them |
| NCCM hint | Suggested `device_ncm.platform` and backup command set | 4 | Used by a policy; a template never enrols a device |

A template never carries a notification channel, an escalation policy, a credential, a
maintenance window or a customer's threshold. Those belong to policies.

### 7.5 Groups, dynamic groups and custom fields

* **Static tree.** Group create, edit, move and delete, using the existing `parent_id`.
  `devices.group_id` stays as the device's primary group, so today's rules and reports keep
  working.
* **Dynamic groups.** A group of kind `dynamic` has a condition instead of hand-picked members.
  The condition is the Discovery Rule Engine's tree and typed operators
  (`discovery_rules/conditions.py`) over facts for existing assets (`facts.py`, extended):
  vendor, model, type, template, OS version, sysObjectID, IP subnet, tags, location, site,
  primary group, custom fields, controller, NCCM enrolled. One language, defined once.
* **Membership table.** `device_group_members` (group, asset kind, asset, source) holds static
  and dynamic members. A device can be in many groups. Membership is recomputed when an asset's
  facts or a group's condition change, by the same reconcile loop the Discovery Rule Engine
  plans for its phase 3 (one loop under `alert_worker_lock`, bounded batches).
* **Everything scopes by group.** Alert rule `group_id`, maintenance `scope_group_id`, sensor
  assignment, reports and maps read `device_group_members` instead of `devices.group_id`. A rule
  "for all Fortinet firewalls" is a rule on a dynamic group; no new scope column per dimension.
* **Built-in dynamic groups** per template and per device type, created on demand, so "all
  devices on this template" is always available.
* **Groups never grant visibility.** Team scope stays on tags (`core/scoping.py`). A scoped
  user sees only the in-scope members of any group.
* **Custom fields.** Typed fields (text, number, date, choice, yes/no) for devices and servers:
  `custom_field_defs` and `custom_field_values`. CSV import. Optional source OID so a field
  fills itself from SNMP. Fields are facts for dynamic groups and discovery rules, columns in
  the Devices list, and placeholders in notification templates.
* **Tags stay flat.** They are the access boundary; typed data goes in custom fields.
* **Preview.** The group editor shows the member count and sample members live, and the
  rules, policies and maintenance windows that would start or stop applying.

### 7.6 Thresholds and alert rules

* **One alerting engine.** Alarms continue to come only from `alert_rules`, with the existing
  incident, hold and delivery rules (`docs/alert-notification-policy.md`).
* **Rule packs.** A template lists recommended rules. The template page shows them with
  Accept, which creates a normal alert rule scoped to the template (`alert_rules.profile_id`,
  covering templates that extend it) and tagged with `pack_key`. Nothing is created or enabled
  automatically, on first assignment or on upgrade.
* **A new built-in revision never changes a customer rule.** If its recommended value differs
  from the accepted rule, the template page says "recommendation changed: 85 to 90" with Apply
  or Keep.
* **Overrides without cloning.** `alert_rule_overrides`: an ordered list on the rule; each row
  names a group, a device or a server (and optionally an instance filter such as an interface
  pattern) and changes threshold, clear level, `min_duration`, `clear_duration` or priority.
  Device rows sort above group rows; the first matching row wins. Each row has a reason, an
  author and an optional expiry. An exclusion (off) remains `alert_rule_exclusions`.
* **Effective thresholds.** `GET /alert-rules/effective?device_id=` and a card on the device
  page list every rule that covers the device with the value in force and its source: rule,
  override (which row) or excluded.
* **Display thresholds.** The `thresholds` on a template metric keep colouring the device page.
  Where an accepted rule covers the same metric, the page uses the rule's effective value, so
  the colour and the alarm agree.
* **Evaluator contract.** All evaluators share one scope function built on
  `device_group_members`, ending today's differences (traps ignoring type and location, Device
  Tracker ignoring tags). `alert_rule_scope.affected` and `impact` use the same function, so
  the count shown before saving is the set evaluated.
* **Interfaces.** Interface rules gain a role filter (uplink, access and so on) from the
  template's interface roles, replacing substring matching on `target` for the common cases.

### 7.7 Policy bundles: combining actions per class of device

This is the base for the owner's goal of combining several actions and alert rules per class.

* **A policy** is a named bundle attached to one group (static or dynamic), with a unique
  priority and a mode: draft, simulate, active, disabled.
* **Sections** (each optional):

  | Section | Sets | Applied by |
  | --- | --- | --- |
  | Polling | Ping interval, SNMP poll interval, per-group template intervals | Written to the device row with its source recorded |
  | Status policy | Degraded and down confirmation limits, flap settings for this class | Read by the polling owner; global setting remains the default |
  | Alerting | Which rule packs and rules apply, with override values | Rows in `alert_rule_overrides` and rule scope, owned by the policy |
  | Notification | Default escalation policy and channels for incidents on this class | Routing: rule policy, the rule's legacy levels, class policy, priority default. A class default never takes over a rule that already escalates |
  | Credentials | Default SNMP credential, NCCM connection profile | `device_service.apply_credential`; this gives `device_groups.snmp_credential_id` its meaning |
  | NCCM | Enrol, platform, schedule, keep versions | `device_ncm` rows, owned by the policy |
  | Device Tracker | Enabled or not | `udt_device_settings` |
  | Maintenance | Recurring window for the class | Needs recurrence in `device_maintenance` |
  | Layout | Shared default page layout | `device_widget_layouts` |
  | Servers | Agent policy, software baseline | `agents.policy_id` |

* **Ownership.** Each value a policy writes is recorded with the previous value in one
  provenance table, `asset_setting_sources`. The Discovery Rule Engine's planned
  `discovery_engine_assignments` (its phase 2) is the same idea; build it once under this
  name and let both use it. Manual edits win unless a policy section is marked enforce.
* **Relation to discovery rules.** A discovery rule classifies (group, tags, type, template,
  location, credential, import decision). A policy configures the resulting class. The
  discovery plan's phase 3 "wider actions" (NCCM enrolment, Device Tracker, maintenance,
  intervals on existing devices) would be delivered as policy sections instead, with one
  action left in the rule engine: "apply policy". This avoids two places that set the same
  thing. It changes the scope of that plan's phase 3 and is an owner decision (section 11).
* **Simulation, caps, undo.** Same safety model as the discovery plan: simulate mode logs what
  would change; a change above the cap waits for approval; each run can be undone where the
  value is still what the policy set.

### 7.8 Preview, test, versions, rollout, drift

* **Test on a device.** `POST /snmp/profiles/{id}/test` runs a draft or published template (or
  one group) against a chosen device through its polling owner, stores nothing, and returns
  each metric's value or error, the row count per table and the time taken. Rate-limited, one
  test per device at a time, bounded by `max_rows`.
* **Coverage.** Per device and template: metrics that answered against metrics defined,
  computed from `device_template_values`; no poller change.
* **Impact preview.** Before publishing: devices affected, metrics added and removed, series
  that will stop, change in OIDs per poll cycle, alert rules and layouts that reference a
  removed metric.
* **Staged publish.** Publish to a pilot (chosen devices or a group), compare coverage and poll
  duration before and after, then publish to all. Roll back restores the previous revision in
  one step.
* **Built-in updates.** A release ships new built-in revisions. Devices on a built-in move to
  it as today. The template page shows what changed. Overlays keep working; an overlay entry
  whose parent key disappeared is flagged, not dropped.
* **Unassign and switch.** Changing a device's template clears the old template's current
  readings (fixing today's leftover rows) and keeps ClickHouse history until its normal
  retention. Clear is never a side effect of a rule or policy edit.
* **Drift view.** Per device: template and revision, source of the assignment, coverage,
  overrides in force, suggestion pending. Per template: devices by revision, lowest coverage,
  devices whose identity no longer matches.

### 7.9 Data model

Forward migrations only; the set stays append-only. **Migration numbers are chosen at build
time by scanning main and every open PR** (main ends at 187; PR #160 uses 188). Each migration
carries the `zenplus` grant block and a `scripts/migrations.lock` entry.

| Table or column | Phase | Main columns |
| --- | --- | --- |
| `device_types` | 1 | key PK, label, parent_key, icon, sort, builtin, created_at |
| `devices.device_type` | 1 | CHECK dropped; validated against `device_types` in the API |
| `device_profiles` (columns) | 1 | builtin_key UNIQUE NULL, kind (device/component), parent_id, components UUID[], device_type, icon, match_priority, revision, draft JSONB, resolved_oid_groups JSONB, defaults JSONB, content_hash, updated_by; unique key becomes (name) |
| `device_profile_versions` | 1 | profile_id, revision, snapshot JSONB, changed_by, changed_at, reason, pilot_scope |
| `devices` (columns) | 1 | profile_source, profile_assigned_at, profile_revision, profile_suggested_id, device_type_source |
| `device_widget_layouts` | 1 | user_id nullable for a shared template default |
| `device_groups` (columns) | 2 | kind (static/dynamic), condition JSONB, asset_kinds[], member_count, evaluated_at, builtin_key, updated_by |
| `device_group_members` | 2 | (group_id, asset_kind, asset_id) PK, source (static/rule), added_at |
| `custom_field_defs` | 2 | id, key UNIQUE, label, type, choices, asset_kinds[], source_oid, sort |
| `custom_field_values` | 2 | (field_id, asset_kind, asset_id) PK, value_text, value_num, value_date, source, updated_at |
| `alert_rules` (columns) | 3 | profile_id, pack_key, interface_role |
| `alert_rule_overrides` | 3 | id, rule_id, position, scope_kind (group/device/server), group_id, device_id, server_id, target, threshold, clear_threshold, min_duration, clear_duration, priority, reason, expires_at, owner_policy_id, created_by, created_at |
| `device_interfaces` (columns) | 3 | role, role_source, monitored_source |
| `monitoring_policies` | 4 | id, name UNIQUE, description, group_id, priority UNIQUE, mode, settings JSONB, enforce JSONB, revision, created_by, updated_by, activated_by, timestamps |
| `monitoring_policy_versions` | 4 | policy_id, revision, snapshot, changed_by, changed_at, reason |
| `asset_setting_sources` | 4 (or discovery phase 2, whichever lands first) | (asset_kind, asset_id, setting_key) PK, value, previous, source_kind (template/policy/rule/manual), source_id, source_revision, applied_at |
| `device_maintenance` (columns) | 4 | recurrence, timezone |

`license_capacity.NODE_COUNT_SQL` and `updater/inventory.py` do not change.

### 7.10 API

Existing `/api/v1/snmp/profiles` routes stay and keep their shapes. Additions:

| Endpoint | Use |
| --- | --- |
| `GET/POST/PUT/DELETE /device-types` | Type catalogue |
| `POST /snmp/profiles/{id}/customize` | Create an overlay of a built-in |
| `PUT /snmp/profiles/{id}/draft`, `POST /snmp/profiles/{id}/publish` (optional pilot scope), `POST /snmp/profiles/{id}/rollback` | Draft, staged publish, rollback |
| `GET /snmp/profiles/{id}/versions`, `GET /snmp/profiles/{id}/versions/{rev}/diff` | History and diff |
| `POST /snmp/profiles/{id}/test`, `POST /snmp/profiles/test-draft` | Test on a device |
| `POST /snmp/profiles/{id}/impact` | Impact preview of a draft |
| `GET /snmp/profiles/{id}/coverage`, `GET /snmp/profiles/unmatched`, `GET /snmp/profiles/classify` | Coverage, unmatched queue, explain |
| `POST /snmp/profiles/from-device` | Draft template from a device's walk and the MIB Library |
| `GET/POST/PUT/DELETE /device-groups`, `POST /device-groups/preview`, `GET /device-groups/{id}/members`, `POST /device-groups/{id}/members` | Groups and dynamic membership |
| `GET/POST/PUT/DELETE /custom-fields`, `PUT /custom-fields/values`, `POST /custom-fields/import` | Custom fields |
| `GET /snmp/profiles/{id}/rule-pack`, `POST /snmp/profiles/{id}/rule-pack/{key}/accept` | Recommended rules |
| `GET/POST/PUT/DELETE /alert-rules/{id}/overrides`, `POST /alert-rules/{id}/overrides/reorder`, `GET /alert-rules/effective` | Overrides and effective thresholds |
| `GET/POST/PUT/DELETE /monitoring-policies`, `POST /monitoring-policies/preview`, `POST /monitoring-policies/{id}/mode`, runs and undo as in the discovery plan | Policies |
| `GET /devices/{id}/effective-config` | Every effective setting with its source |

### 7.11 UI

* **Device Templates** moves from Administration, Settings to Network, Configuration, beside
  Discovery and NCCM, with its own page (`dashboard/src/pages/templates/`).
  * List: name, vendor, type, devices, coverage, revision, state (built-in, overlay, custom,
    draft pending, update available). Unmatched devices tab.
  * Editor tabs: Identity and matching, Metrics (group and metric forms, MIB picker, table
    preview), Interfaces, Recommended alerts, Layout, Devices, History. A Test panel beside the
    editor runs the draft against a chosen device.
  * Publish dialog: impact summary, pilot choice, reason.
* **Device Types** under Administration, Settings.
* **Groups** under Network: tree, members, dynamic group builder (the rule editor's condition
  builder reused) with live preview, and "used by" (rules, policies, maintenance).
* **Custom Fields** under Administration, Settings; columns and filters on the Devices list.
* **Policies** under Network, Configuration (phase 4): ordered list, sections, simulation
  results, run history.
* **Device page**: the Configuration tab gains "Effective configuration": template, revision,
  who assigned it, groups, policies, overrides, each value with its source and a link.
* **Devices list**: bulk actions Assign template, Add to group, Set custom field.
* **Alert rule wizard**: template metric picker, group scope including dynamic groups,
  overrides tab.
* Design system rules apply (`docs/design-system.md`). After `navigation.ts` changes:
  `node scripts/test-page-access.mjs --write`; `scripts/test-nav-access.mjs` must pass.

### 7.12 Permissions and scope

* New `devices.manage_templates` (templates, type catalogue) and `devices.manage_policies`
  (dynamic groups, custom field definitions, policies), both implying `devices.view`, granted
  to administrators and the built-in `network_team` role (built-in definition version bump).
  Viewing needs `devices.view`. The sidebar entries declare the same permission the API checks,
  ending today's `settings.manage` / `devices.manage` mismatch.
* Templates, dynamic groups and policies are fleet-wide configuration. A tag-scoped user cannot
  create, edit or publish them, the stance `alert_rule_scope.guard_rule_change` takes for
  fleet-wide alert rules.
* A device override needs `devices.manage` (or `alerts.manage` for a threshold override) and
  the device in the caller's scope.
* Activating a policy requires the permission behind each section: `alerts.manage`,
  `ncm.manage`, `udt.manage`, `servers.manage`. `activated_by` is stored and audited.
* Every publish, rollback, assignment, group change, override and policy run is written with
  `write_audit_log`.

### 7.13 Controller children, licence and safety

* **Controller children** (`poll_mode = via_controller`) never get a polled template. The
  classifier skips them, assign refuses them, and Test on device refuses them. Their readings
  keep coming from the controller's template rows. A child's device type comes from the
  controller's `children` mapping.
* Children may be members of groups and targets of alert overrides, exclusions, notification
  and maintenance sections. Polling, credential and NCCM sections are refused at save for a
  group that can contain only children and skipped per child otherwise, with the reason shown.
* **Licence.** No template, group or policy action creates, deletes, disables or promotes a
  device. The node count SQL is untouched.
* **Thresholds.** No customer threshold changes without an operator action that is previewed
  and audited. Built-in updates never touch alert rules.
* **History.** Template, group or policy edits never delete collected history.
* **Load.** Publishing a template that raises OIDs per cycle beyond a set factor needs
  confirmation. Test on device is rate-limited.
* **Regular expressions** are validated and length-limited at save and evaluated with a time
  limit, as in the discovery plan.
* **Credentials.** Policies reference stored credentials by id; no secret is copied into a
  policy, a version snapshot or an export.

### 7.14 Import, export and packs

* **Format v2.** JSON with `format_version: 2`, `builtin_key` or name, parent and components by
  key, rule pack, interface roles, layout. Version 1 files still import.
* **Import shows a diff** against an existing template of the same key and imports as a draft.
  Unresolved references are reported; nothing is guessed.
* **Packs ship with releases.** Built-ins arrive in the signed OTA package, as now. The
  appliance never fetches templates from the internet.
* **From a device.** "Create template from this device" walks the device, maps OIDs through the
  MIB Library and proposes groups for the operator to trim.
* **Converters** (phase 5, owner decision): read a Zabbix template YAML, a LibreNMS OS YAML or
  a Datadog profile YAML that the customer supplies and produce a draft. Shipping converted
  third-party content inside ZenPlus needs a licence review first.

### 7.15 Where this beats each product, and what not to copy

| Product | ZenPlus is better because | Deliberately not copied |
| --- | --- | --- |
| OpManager | Local changes survive updates; per-setting overrides instead of overwrite; test, preview, versions, rollback; one precedence order; unmatched devices still monitored; class policy in one object | Thresholds stored inside the template; Associate & Overwrite; 11,000 thin templates (a generic template plus type catalogue covers them) |
| Zabbix | No link conflicts (one template per device, composition between templates); edits to vendor content are an overlay, not lost on update; preview before linking | Many templates per host; macros as the override mechanism |
| PRTG | Templates stay linked and editable; a change reaches existing devices through a staged publish | One-time stamp templates; tag inheritance that cannot be turned off |
| SolarWinds | One template kind; alert scope and groups use the same membership | Separate poller kinds per technology |
| LibreNMS | Customer-editable templates; class-level thresholds; overrides keyed to stable identity | Exposing raw table.column names in the group builder |
| Datadog | Templates recommend alert rules; a test against a device before saving | Agent-local YAML as the only editing surface |
| LogicMonitor | Deterministic order with unique priorities; preserving local changes is the default, not a per-update choice; un-matching never deletes history | A scripting language for conditions |
| Checkmk | Per-run undo instead of all-or-nothing revert; a group change is previewed with what will start or stop applying | Folder moves that silently change settings |
| Domotz | Applying a policy is linked, previewable and undoable | Apply with no link back |

## 8. Phases

Sizes are rough working days for one developer with Claude sessions, including tests and docs,
excluding canary soak. Each phase ships something usable on its own.

| Phase | Delivers | Size | Needs |
| --- | --- | --- | --- |
| 1 | Trustworthy templates: type catalogue, one classifier, real editor, test on device, overlays, versions, rollback | 10 to 12 days | Nothing |
| 2 | Groups you can use: tree management, dynamic groups, custom fields, one scope function for all evaluators | 10 to 12 days | PR #160 merged (condition engine) |
| 3 | Thresholds per class: rule packs, overrides, effective thresholds, interface roles | 10 to 12 days | Phase 2 |
| 4 | Policy bundles: combined settings per class, simulation, ownership, undo | 12 to 15 days | Phases 2 and 3; coordination with discovery phase 2 |
| 5 | Depth by demand: converters, components as data, non-SNMP sources, server templates, scheduled drift report | 3 to 6 days each | |

### Phase 1: trustworthy templates (build this first)

Scope: `device_types` and removal of the CHECK; template identity, parent, components, draft,
revision, `resolved_oid_groups`; `device_profile_versions`; assignment source on devices;
classifier contract with priority and probe tests, Go and Python; re-check with suggestion;
generic fallback template and Unmatched view; form editor with MIB picker; test on device;
coverage; impact preview; staged publish and rollback; overlay ("Customise"); shared default
layout; bulk Assign template on the Devices list; audit on every change; permission
`devices.manage_templates`; fixes for the defects in section 12 that sit in this area.

Touches: `server/app/models/device_profile.py`, `server/app/schemas/snmp.py`,
`server/app/api/v1/snmp.py`, new `server/app/services/device_templates/` (resolve, classify,
impact, test, versions), `server/app/api/v1/devices.py`, `device_service.py`,
`device_insights.py` (built-in maps become data), `core/permissions.py`, `core/route_policy.py`;
`poller/internal/checker/snmp/profile.go`, `poller/internal/store/postgres.go` (profile query,
assignment source, clearing readings on switch), a poller or sensor entry point for the test
walk; one forward migration and `scripts/migrations.lock`;
`scripts/dev/template-match-contract/`; `dashboard/src/pages/templates/`, `DevicesPage.tsx`,
`DeviceFormDialog.tsx`, `components/network-icons`, `components/layout/navigation.ts`,
`App.tsx`, `server/app/core/page_catalog.json`; `docs/device-templates.md`, a paragraph in
`AGENTS.md`.

Acceptance:

- [ ] A device type added in the catalogue can be set on a device, shows its icon on the list,
      page and map, and can scope an alert rule. The eleven types the dashboard and PR #160 use
      are all accepted by the database.
- [ ] The Go and Python classifiers return the same template for every vector in the contract
      file; a test fails if either drifts.
- [ ] A manually assigned template is never changed by the classifier. A device whose
      sysObjectID changes gets a suggestion, shown on the device page and in the template's
      drift view.
- [ ] A device that matches no vendor template gets the generic template, shows CPU, memory and
      interfaces, and appears under Unmatched.
- [ ] "Customise" on the FortiGate built-in, turning one group off and adding one metric,
      survives a release that ships a new FortiGate revision; the new revision's added metrics
      appear on the device.
- [ ] Test on device returns values for a draft without storing a reading or changing the
      device.
- [ ] Publishing to a pilot of two devices changes only those two; Roll back restores the
      previous revision for them within one poller sync.
- [ ] Switching a device's template leaves no readings from the old one on the device page.
- [ ] Editing a custom template in the UI keeps its `extract_*` and `default_*` rules.
- [ ] Without `devices.manage_templates`: 403 on every template write and no edit controls;
      `test-nav-access.mjs` and `test-page-access.mjs` pass.
- [ ] A controller-managed child cannot be assigned a template or tested; the node count is
      unchanged by every action in this phase.
- [ ] With no template edited and no type added, polling and the device page behave as in
      1.25.9 (existing SNMP and device page tests green).

### Phase 2: groups you can use

Scope: group CRUD and tree; `device_group_members`; dynamic groups on the shared condition
engine with facts for existing devices and servers; reconcile on change; built-in groups per
template and type; custom fields with CSV import and optional SNMP source; one scope function
used by every evaluator, by `alert_rule_scope` and by maintenance; group picker everywhere a
group is chosen; preview with "used by".

Touches: `server/app/services/discovery_rules/conditions.py` and `facts.py` (asset facts), new
`server/app/services/device_groups.py` and `custom_fields.py`, new
`server/app/api/v1/device_groups.py` and `custom_fields.py`; `network_alert_service.py`,
`api/v1/alert_engine.py`, `network_events.py`, `udt_alert_service.py`, `alert_rule_scope.py`,
`alert_rule_exclusions.py`; `poller/internal/store/postgres.go` (maintenance by membership);
`main.py` (reconcile loop); one forward migration; `dashboard/src/pages/groups/`,
`DevicesPage.tsx`, alert rule wizard, maintenance dialog; `docs/device-groups.md`,
`docs/alert-notification-policy.md`.

Acceptance:

- [ ] A group can be created, renamed, moved under a parent and deleted from the UI; deleting a
      group in use is refused with the list of what uses it.
- [ ] A dynamic group "vendor is Fortinet AND type is firewall AND IP in 10.51.0.0/16" shows its
      members before saving, and a device that starts matching joins within two reconcile
      intervals with no manual step.
- [ ] An alert rule scoped to that group fires for a member and not for a non-member, on the
      SNMP, status, trap, syslog and Device Tracker evaluators alike.
- [ ] The affected count shown when saving a rule equals the set the evaluator uses (a test
      compares them).
- [ ] Rules scoped by `devices.group_id` before the upgrade cover the same devices after it.
- [ ] A custom field "Department" filled by CSV can be a Devices column, a dynamic group
      condition and a notification placeholder.
- [ ] A tag-scoped user sees only in-scope members of a group and cannot edit a dynamic group.
- [ ] Two API workers never apply the same membership batch twice.

### Phase 3: thresholds per class

Scope: rule packs on built-in templates and Accept flow; `alert_rules.profile_id`, `pack_key`;
`alert_rule_overrides` with ordering, reason and expiry; effective thresholds endpoint and
device card; template metric picker in the wizard; display thresholds following the effective
rule value; interface roles from the template and role filter on interface rules;
"recommendation changed" notice.

Touches: `server/app/services/network_alert_service.py`, `host_alert_service.py`,
`alert_rule_scope.py`, new `server/app/services/alert_rule_overrides.py`,
`server/app/api/v1/alert_rules.py`, `device_insights.py`, `device_templates/`;
`poller/internal/store/interface_policy.go` (role, monitored source); one forward migration
that also adds rule packs to the built-ins; alert rule wizard, device page card, template
Recommended alerts tab; `docs/alert-rule-overrides.md`, `docs/alert-rule-exclusions.md`.

Acceptance:

- [ ] Assigning a template or upgrading creates and enables no alert rule. Accept creates one
      rule scoped to the template, visible in the normal rule list.
- [ ] An override row "group Branch firewalls: CPU 95 % for 10 minutes" changes the level and
      hold for members only; a device row above it wins for that device; the device page names
      the row in force.
- [ ] An open incident whose effective threshold changes follows the existing hold and
      stable-recovery rules; missing samples still never resolve.
- [ ] An expired override stops applying at its expiry and is listed as expired.
- [ ] A new built-in revision with a different recommended value leaves the accepted rule
      unchanged and shows "recommendation changed".
- [ ] The colour of a metric on the device page and the alarm for the same metric agree for a
      device with an override.
- [ ] An interface rule with role "uplink" covers the interfaces the template marks as uplinks
      and no access port; an operator's manual monitored choice is not changed by a role.
- [ ] Every evaluator loads overrides once per pass, as it loads exclusions.

### Phase 4: policy bundles

Scope: `monitoring_policies` and versions; resolver and `asset_setting_sources`; sections
Polling, Status policy, Alerting, Notification, Credentials, NCCM, Device Tracker, Maintenance
(with recurrence), Layout, Servers; modes including simulate; change cap with approval; run
history and undo; effective configuration endpoint and device card; "apply policy" as a
discovery rule action.

Touches: new `server/app/services/monitoring_policies/` (resolve, apply, runs),
`server/app/api/v1/monitoring_policies.py`; `device_service.py`, `escalation_policy_service.py`
(class default in routing), `api/v1/ncm.py`, `api/v1/udt.py`, `api/v1/settings.py` (status
policy per class), poller status policy read, `api/v1/servers.py`;
`discovery_rules/actions.py`; one or two forward migrations; `dashboard/src/pages/policies/`,
device page card; `docs/monitoring-policies.md`, `docs/device-status-policy.md`,
`docs/alert-notification-policy.md`.

Acceptance:

- [ ] A policy "Branch firewalls" on a dynamic group sets a 60 s SNMP interval, enrols members
      in NCCM with a chosen profile, routes their incidents to one escalation policy and applies
      two threshold overrides; the preview lists every change per device before activation.
- [ ] In Simulate mode the policy records what it would change and writes nothing.
- [ ] A device that joins the group receives the policy within two reconcile intervals; one
      that leaves keeps or reverts its values according to the policy's setting, and its
      history is intact.
- [ ] Two policies setting different SNMP intervals for one device: the higher priority wins and
      the other is shown as shadowed on the device's effective configuration.
- [ ] A value edited by hand after a policy set it is left alone and reported; with enforce it
      is restored and that is recorded.
- [ ] A rule that already has its own escalation is not rerouted by a class default.
- [ ] Polling, credential and NCCM sections skip controller-managed children with the reason
      shown; the node count is unchanged.
- [ ] A user lacking `ncm.manage` cannot activate a policy with an NCCM section.
- [ ] Undo restores every value still equal to what the policy set and lists the ones changed
      since.

### Phase 5: depth, chosen by demand

* Converters for Zabbix, LibreNMS and Datadog template files supplied by the customer.
* Standard routing groups moved from Go code into a component template.
* Template sources beyond SNMP (REST and CLI collectors) where a vendor needs them.
* Server templates: agent policy and host alert rule packs under the same model.
* Scheduled drift and coverage report; "devices with no accepted rule pack" report.
* Template suggestions from a device walk using the MIB Library, ranked by coverage.

## 9. Testing

* **Contract vectors.** `scripts/dev/template-match-contract/vectors.json` (identity facts,
  templates, expected match and trace) read by Go and Python. A second file for resolution:
  parent, overlay, components, expected `resolved_oid_groups`.
* **Unit tests.** Resolver (add, disable, override, depth limit, missing parent key),
  precedence (defaults, template, policies by priority, override), override ordering and
  expiry, impact calculation, condition evaluation on asset facts.
* **Database tests on `pg_fixture`.** Type catalogue migration with existing rows; template
  CRUD, draft, publish, pilot, rollback; assignment source rules; group membership and
  reconcile under two workers; scope function equality between `affected` and each evaluator;
  overrides loaded once per pass; policy apply, simulate, undo; permissions and tag scope;
  children refused; `NODE_COUNT_SQL` equality test untouched.
* **Go tests.** Classifier against the vectors; poller reads `resolved_oid_groups`; readings
  cleared on template switch; interface role assignment keeps manual choices.
* **Regression.** Existing SNMP profile, device insights, controller children, alert rule
  scope, exclusions and escalation tests stay green with nothing configured.
* **Dashboard.** Strict build, route smoke checks, `scripts/test-nav-access.mjs`,
  `scripts/test-page-access.mjs --write`, `scripts/test-offline-assets.mjs`, model tests for the
  template editor and group builder.
* **Scale.** 5,000 assets against 200 dynamic groups reconcile in bounded batches; resolving
  the effective configuration of one device stays under 50 ms on the fixture.
* **Rules of the host.** Disposable fixtures only, never installed databases; `export
  ZENPLUS_API=http://127.0.0.1:9` before pytest on .221.
* **Acceptance.** Canary on .221 with a pilot publish against lab devices; release acceptance
  on the reference appliance; the five required CI checks stay.

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| A template edit breaks polling for many devices | Draft, test on device, impact preview, pilot publish, one-step rollback; the poller reads only a resolved, validated document |
| Dropping the `device_type` CHECK lets bad values in | API validation against `device_types`; migration maps unknown existing values to `other` and reports them |
| Changing every evaluator's scope function changes who is alerted | Equality tests between old and new scope for existing rules on a copy of real rule sets; release note; phase 2 ships with no rule edited |
| Overrides hide a real problem by loosening a threshold | Reason required, optional expiry, shown on the device and the rule, audited; report of all overrides |
| Three layers confuse operators | One effective configuration view with the source of each value; policies arrive only in phase 4 |
| Overlap with Discovery Rule Engine phases 2 and 3 | Shared condition engine, reconcile loop and provenance table; decide section 11 item 10 before either phase 2 is built |
| Dynamic group reconcile load | SQL hash of rule-relevant columns, bounded batches, one worker under the existing lock |
| Overlay points at a parent key removed by an update | Flagged in the template and on publish; resolution skips the entry and keeps the rest |
| Test on device adds SNMP load or reaches a device through the wrong sensor | Runs through the device's polling owner, rate limit, `max_rows`, one test per device |
| Converters import third-party content | Customer-supplied files only until a licence review is done |
| Parallel sessions (HA and sensors, virtualisation) take the same migration numbers | Numbers picked at build time |
| Built-in maps moved into data change the device page | Snapshot tests of `shape_template_groups` output before and after for every built-in |

## 11. Decisions needed from the owner

1. Start with phase 1 alone (recommended), or phases 1 and 2 together?
2. Name and place: "Device Templates" under Network, Configuration (recommended), or keep
   "Monitoring Templates" under Settings?
3. One template per device with composition between templates (recommended), or several
   templates per device as Zabbix does?
4. Customising a built-in creates an overlay that keeps receiving updates (recommended), with
   Clone kept for forks?
5. Device type catalogue: which types to seed beyond today's seven (proposed: load_balancer,
   wireless_controller, ups, storage, pdu, ip_camera, voip, sdwan_edge), and may customers add
   their own (recommended: yes)?
6. When a device's identity changes and a different template matches: suggest and wait
   (recommended), or switch automatically for devices that were auto-assigned?
7. Groups: keep one primary group per device and add many dynamic groups (recommended), or make
   every group many-to-many?
8. Custom fields as typed fields separate from tags (recommended), or key:value tags?
9. Template thresholds: shipped as recommended rules that an operator accepts (recommended), or
   enabled automatically for new devices?
10. Policy bundles and the Discovery Rule Engine: deliver that plan's phase 3 "wider actions" as
    policy sections with one "apply policy" rule action (recommended), or keep them as separate
    rule actions?
11. Permissions: `devices.manage_templates` and `devices.manage_policies` for administrators and
    the built-in network team role, not usable by tag-scoped users (recommended)?
12. Servers: include servers in dynamic groups and custom fields from phase 2 (recommended), and
    server templates later in phase 5?
13. Location: keep free text, or give devices a `site_id` so groups and policies can use sites
    (recommended, in phase 2)?
14. Converters for Zabbix, LibreNMS and Datadog template files: wanted, and who reviews the
    licences?
15. Merge order: PR #160 before phase 2 of this plan (required by the shared condition engine).

## 12. Not in scope, and defects found

No code, migration, PR or release was produced, and nothing was changed on .221.

Defects found during the survey. None was fixed; the first should be checked before PR #160
merges, the rest are part of phase 1.

1. **Device type values.** `scripts/init-postgres.sql:35` limits `devices.device_type` to seven
   values and no migration drops that CHECK. `DevicesPage.tsx` offers storage and ups, and the
   PR #160 `device_type` action accepts load_balancer, wireless_controller, ups and storage
   (`discovery_rules/actions.py:62`). On a database that still has the CHECK those writes would
   fail. To verify on a fixture; the live database was not queried.
2. **Editor drops match rules.** Saving a template in the UI sends only
   `sys_object_id_prefixes` and `sys_descr_regex`, so `extract_*` and `default_*` are lost
   (`TemplatesTabContent.tsx:606-611`).
3. **Two matchers.** Go orders the sysDescr fallback by name, Python by name length
   (`profile.go`, `api/v1/snmp.py:256`).
4. **Stale readings.** Assigning a different template, or deleting one, leaves the old
   `device_template_values` rows.
5. **Permission mismatch.** The sidebar entry needs `settings.manage`; the API needs
   `devices.manage`.
6. **`version` in the unique key.** F5 was bumped to version 2 in place (`migrate-120`); a later
   upsert on (name, 1) would insert a second row.
7. **Group SNMP credential is inert.** `device_groups.snmp_credential_id` is written and never
   applied.
8. **No audit entry** for template assign, unassign, create, update or delete.
9. **Standard routing groups are invisible.** They are collected for every network device and
   shown nowhere on the device page.

## 13. Sources

Pages opened 2026-11-16. "Snippet" = seen only as a search excerpt.

OpManager (demo, opened without saving)
* https://demo.opmanager.com/apiclient/ember/index.jsp#/Settings/Configurations/DeviceTemplate
  (list, Edit, Edit SysOID, Add Monitor, Edit Thresholds, Associate)
* https://demo.opmanager.com/apiclient/ember/index.jsp#/Settings/Configurations/Categories
* https://demo.opmanager.com/apiclient/ember/index.jsp#/Settings/Configurations/AdditionalFields/Device
* https://demo.opmanager.com/apiclient/ember/index.jsp#/Settings/Configurations/ElementsGroup/Index
* https://demo.opmanager.com/apiclient/ember/index.jsp#/Settings/Configurations/InterfaceTemplate/Types
* https://demo.opmanager.com/apiclient/ember/index.jsp#/Settings/Configurations/QuickConfig

OpManager (documentation and forum)
* [O1] https://www.manageengine.com/network-monitoring/help/device-templates.html
* [O2] https://manageengine.com/network-monitoring/kb/difference-between-device-type-and-category.html
* [O3] https://www.manageengine.com/network-monitoring/how-to/device-discovered-as-unknown.md
* [O4] https://www.manageengine.com/network-monitoring/faq/difference-between-vendor-and-device-templates.md
* [O5] https://manageengine.com/network-monitoring/how-to/associating-same-sysoid-devices.html
* [O6] https://www.manageengine.com/network-monitoring/faq/sysoid-criteria-and-additional-sysoid.html
* [O7] https://manageengine.com/network-monitoring/how-to/classify-devices-under-correct-devicetype.html
* [O8] https://www.manageengine.com/network-monitoring/help/quick-config-wizard.md
* [O12] https://www.manageengine.com/network-monitoring/help/configuring-discovery-rule-engine.html
* [O13] https://manageengine.com/network-monitoring/how-to/import-template-from-community.html
* [O14] https://www.manageengine.com/network-monitoring/how-to/sync-device-templates.md
* [O17] https://www.manageengine.com/network-monitoring/help/device-rediscovery-properties.html
* [O18] https://www.manageengine.com/network-monitoring/help/add-snmp-monitor.html
* [O19] https://www.manageengine.com/network-monitoring/help/configure-thresholds.md
* [O20] https://www.manageengine.com/uk/network-monitoring/help/tabular-oid-monitoring.html (snippet)
* [O23] https://www.manageengine.com/uk/network-monitoring/help/interface-templates.html
* [O24] https://www.manageengine.com/network-monitoring/help/grouping.html
* [O26] https://www.manageengine.com/latam/network-monitoring/help/rest-api-opmanager.html
* [O27] https://www.manageengine.com/network-monitoring/faq/grouping.html
* [O28] https://www.manageengine.com/network-monitoring/help/configuring-notifications.md
* [O30] https://manageengine.com/network-monitoring/help/custom-fields.html
* [O32] https://www.manageengine.com/network-monitoring/help/alarm-escalation-alerts.md
* [O36] https://pitstop.manageengine.com/portal/es/community/topic/the-failure-of-opmanager-discovery-hardware-changes-and-applications-services
* [O37] https://pitstop.manageengine.com/portal/en/community/topic/device-templates-not-applying-to-existing-devices-thresholds-in-particular
* [O38] https://pitstop.manageengine.com/portal/en/community/topic/opmanager-11600-re-apply-a-template-on-a-vm-removes-all-vmware-monitors
* [O42] https://pitstop.manageengine.com/portal/en/community/topic/opmanager-custom-template-lost-after-ppm-patched
* [O43] https://pitstop.manageengine.com/portal/en/community/topic/all-my-devices-default-back-to-unkown-category-after-upgrade
* [O45] https://www.manageengine.com/network-monitoring/help/scheduling-downtime.md

Zabbix
* https://www.zabbix.com/documentation/7.0/en/manual/config/templates/template
* https://www.zabbix.com/documentation/7.0/en/manual/config/templates/linking
* https://www.zabbix.com/documentation/7.0/en/manual/config/templates/nesting
* https://www.zabbix.com/documentation/7.0/en/manual/config/macros/user_macros
* https://www.zabbix.com/documentation/7.0/en/manual/config/macros/user_macros_context
* https://www.zabbix.com/documentation/7.0/en/manual/discovery/low_level_discovery
* https://www.zabbix.com/documentation/7.0/en/manual/config/templates_out_of_the_box
* https://www.zabbix.com/documentation/7.0/en/manual/xml_export_import/templates
* https://www.zabbix.com/documentation/7.0/en/manual/config/tagging
* Snippets: https://www.zabbix.com/forum/zabbix-troubleshooting-and-problems/469957-triggers-get-overwritten-after-template-update ,
  https://www.zabbix.com/forum/zabbix-help/452286-official-template-upgrades-best-practices

PRTG
* https://www.paessler.com/manuals/prtg/create_device_template
* https://www.paessler.com/manuals/prtg/auto_discovery
* https://www.paessler.com/manuals/prtg/inheritance_of_settings
* https://www.paessler.com/manuals/prtg/tags
* Snippet: https://helpdesk.paessler.com/en/support/solutions/articles/76000072448-how-to-apply-changed-template-to-exiting-devices-sensors

SolarWinds
* https://documentation.solarwinds.com/en/success_center/sam/content/sam-template-application-relationship-sw1255.htm
* https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-creating-groups-sw2591.htm
* Snippets: https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-creating-device-studio-pollers-sw2534.htm ,
  https://documentation.solarwinds.com/en/success_center/sam/content/sam-assign-template-to-group-learn-more-sw1010.htm ,
  https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-customize-thresholds-per-object.htm ,
  https://thwack.solarwinds.com/discussion/122381/npm-allow-setting-of-custom-properties-based-on-group-membership ,
  https://thwack.solarwinds.com/discussion/62861/force-multiple-components-on-assigned-application-monitors-to-inherit-from-template

LibreNMS
* https://docs.librenms.org/Developing/os/Initial-Detection/
* https://docs.librenms.org/Developing/os/Health-Information/
* https://docs.librenms.org/Extensions/Device-Groups/
* https://docs.librenms.org/Alerting/Rules/
* Snippets: https://community.librenms.org/t/alerts-changing-default-limits-for-specific-sensor-globally/2153 ,
  https://community.librenms.org/t/sensor-limits-changing-automatically/14263

Datadog NDM
* https://docs.datadoghq.com/network_monitoring/devices/profiles/
* https://datadoghq.dev/integrations-core/tutorials/snmp/profile-format/
* https://docs.datadoghq.com/network_monitoring/devices/guide/build-ndm-profile/
* Snippet: https://docs.datadoghq.com/network_monitoring/devices/profiles/device_profiles/

LogicMonitor (snippet-level)
* https://www.logicmonitor.com/support/applying-datasources-to-resources
* https://www.logicmonitor.com/support/resource-group-overview
* https://www.logicmonitor.com/support/alerts/about-alerts/different-levels-for-enabling-alert-thresholds
* https://www.logicmonitor.com/support/modules-management
* https://www.logicmonitor.com/release-notes/v-193-release-notes
* https://www.logicmonitor.com/support/devices/device-datasources-instances/disabling-monitoring-for-a-datasource-or-instance
* https://community.logicmonitor.com/blog/tech-talk/feature-enhancement-preserve-changes-to-alert-thresholds-when-updating-customize/12484

Checkmk
* https://docs.checkmk.com/latest/en/snmp.html
* https://docs.checkmk.com/latest/en/wato_rules.html
* https://docs.checkmk.com/latest/en/wato_services.html
* https://docs.checkmk.com/latest/en/hosts_setup.html
* https://docs.checkmk.com/latest/en/hosts_structure.html
* https://docs.checkmk.com/latest/en/labels.html
* https://docs.checkmk.com/latest/en/mkps.html
* https://docs.checkmk.com/latest/en/wato.html

Domotz, Auvik, Icinga
* https://help.domotz.com/monitoring-management/device-profiles/
* Auvik (snippets): https://support.auvik.com/hc/en-us/articles/204565804 ,
  https://support.auvik.com/hc/en-us/articles/13106082410004
* https://icinga.com/docs/icinga-2/latest/doc/03-monitoring-basics/

Not verified anywhere: a primary source for alert storms after a template is applied; PRTG
template file details; LibreNMS alert templates and API; Datadog monitor templates.
