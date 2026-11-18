# Thresholds per class: recommended rules, overrides, interface roles (Device Templates phase 3)

Phase 3 of `docs/plans/device-templates-plan.md`. Alarms still come only from
`alert_rules` with the existing incident, hold and delivery rules
(`docs/alert-notification-policy.md`); this phase adds where rules come from
(a template's recommendations), how one rule can differ per device, server or
group (overrides), and how interface rules pick interfaces (roles).
Migration: `scripts/migrate-193-alert-rule-packs-overrides.sql`.

## Recommended rules (rule packs)

A template recommends alert rules. **Nothing is created or enabled
automatically**: not when a template is assigned, not on upgrade. On the
template page (**Recommended alerts**) an operator with `alerts.manage`
accepts a recommendation, which creates one ordinary alert rule:

* scoped to the template (`alert_rules.profile_id`): devices whose template is
  this one or customised from it, at any depth (optionally narrowed to a
  group when accepting);
* remembering the recommendation (`pack_key`) and its values when accepted
  (`pack_accepted`);
* visible and editable in the normal rule list. One accepted rule per
  recommendation and template (a unique index).

Recommendations come from two places (`app.services.rule_packs`):

| Source | What |
| --- | --- |
| Metric thresholds | Every gauge metric with display thresholds (`warn` / `crit`) yields "above warn" (warning) and "above crit" (critical) on `tpl_<metric>` with a 5-minute hold. Groups shown only on child devices are skipped. Custom templates get these too. |
| `device_profiles.rule_pack` | Explicit recommendations, part of the versioned template document. Built-in network templates ship *Uplink down*, *WAN link down* (interface down, role uplink / wan, critical, 60 s) and *Uplink utilisation high* (> 80 % for 10 minutes, warning). A customised template whose pack is NULL uses its parent's. |

**A new template revision never changes a customer's rule.** When the
current recommendation differs from `pack_accepted` (a release raised the
FortiGate CPU level from 90 to 95), the template page shows the change
("threshold 90 → 95") with **Apply** (update the rule, record the new
recommendation) or **Keep** (keep the rule, stop showing the notice). Both are
audited. A template with rules scoped to it cannot be deleted (409, and the
foreign key is `ON DELETE RESTRICT`).

## Overrides

`alert_rule_overrides` (`app.services.alert_rule_overrides`): per rule, an
ordered list of rows. Each row names **one** device, server or group (and
optionally an instance pattern) and changes any of:

| Field | Meaning |
| --- | --- |
| `threshold` | trigger level of the condition (`metric` names which; NULL = the rule's first) |
| `reset_threshold` | clear level (must stay on the healthy side of the trigger) |
| `min_duration` | breach hold |
| `clear_duration` | stable-recovery hold |
| `priority` | incident priority P1–P4 |

plus a required `reason`, the author and an optional `expires_at`.

* **Order:** device and server rows are tried before group rows; within each
  by `position` (the list's order), then age. The **first** matching,
  unexpired row applies; rows never merge.
* **Groups** are memberships with subgroups (`docs/device-groups.md`), for
  devices and servers.
* **Instance pattern** (glob, case-insensitive): an interface name for
  interface rules, a component suffix for template table metrics (`root` of
  `tpl_fgt_vd_cpu_root`), a mount point for server filesystem rules. A row
  with a pattern applies only to matching instances.
* **Expiry:** an expired row stops applying at once and is listed as expired.
* **Where:** SNMP interface, device and template metric rules
  (`network_alert_service`) and server threshold metrics
  (`host_alert_service`). Both load overrides once per pass, as they load
  exclusions, and evaluate an asset with the rule as overridden for it. Status,
  trap, syslog, Device Tracker, service, NetFlow and APM rules have no
  threshold to override (422). Turning a rule **off** for one asset stays an
  exclusion (`docs/alert-rule-exclusions.md`).
* **Incidents:** an open incident whose effective threshold changes follows
  the existing hold and stable-recovery rules (the clear hold is measured on
  real samples); missing samples never resolve. An incident raised under an
  override records it in `metadata.override`.
* **Who:** `alerts.manage` and the rule in the caller's scope; a device or
  server row needs the asset visible to the caller; group rows and reordering
  need a user without a tag scope. A group used by an override cannot be
  deleted.

## Effective thresholds

`GET /alert-rules/effective?asset_kind=device|server&asset_id=` lists every
rule covering the asset with the rule's values, the values in force for it and
the source: the rule, an override (which row, why), an exclusion or the rule
being off; overrides limited to some instances are listed with their pattern.
The device page shows it as the **Alert thresholds** card.

**Display thresholds follow the rule.** Where an enabled, not excluded rule on
a template metric covers the device (`tpl_<metric>`, one condition, critical →
`crit`, warning → `warn`), the device page colours that reading with the
rule's effective level instead of the template's, and marks it
`thresholds_source: "alert_rule"`. Several rules on one metric and severity:
the one that alarms first decides.

## Interface roles

`device_profiles.interface_roles` (versioned with the template; NULL on a
customised template = the parent's) is an ordered list of
`{role, field, pattern}`; the first match gives an interface its role
(`app.services.interface_roles`):

* `field` is `if_type` (IF-MIB type numbers, comma separated) or `if_name` /
  `if_alias` / `if_descr` (a regular expression; `(?i)` for case-insensitive).
* Roles: uplink, access, wan, server, tunnel, vlan, loopback, management.
* Built-in network templates ship rules that use only what an interface says
  about itself: tunnel (131), loopback (24), VLAN (135, 136), management port
  names, "uplink"/"upstream" and "WAN"/"ISP"/"internet" in the description,
  FortiOS `wan*` port names, and Ethernet as access on the AOS-CX and OS10
  switch templates. Nothing is guessed from position or speed.
* An operator can set one interface's role (`device_interfaces.role`,
  `PUT /devices/{id}/interfaces/{if_index}/role`), which wins over the
  template. **Roles never change whether an interface is monitored.**

An interface rule with `if_role` covers only interfaces with that role, in
addition to its `target` filter.

## Deviations from the plan

* Roles are computed when needed from the template's rules and the
  interface's fields; nothing is stored per interface except a manual role,
  and the Go poller is unchanged (roles do not touch "monitored").
* Template scope applies to SNMP and template metric rules only (the rules
  `network_alert_service` evaluates); other families refuse `profile_id`.
* The evaluator scope differences listed in phase 2 (traps ignore type and
  location, Device Tracker's location match) are unchanged.
* Server templates and host rule packs remain phase 5.

## Tests

`server/tests/test_alert_rule_overrides_pg.py` (disposable PostgreSQL): the
migration on built-ins and on production-shaped data (an old-version built-in
copy, a customised template, re-run); Accept / Keep / Apply; template delete
guard; group and device overrides and expiry in the SNMP evaluator; an open
incident under a raised threshold with the clear hold and missing samples;
server overrides; display colours; uplink-role rules and manual roles;
permissions and validation; order and patterns; loading without the table.
