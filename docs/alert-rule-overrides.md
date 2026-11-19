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

Recommendations come from three places (`app.services.rule_packs`):

| Source | What |
| --- | --- |
| Metric thresholds | Every gauge metric with display thresholds (`warn` / `crit`) yields "above warn" (warning) and "above crit" (critical) on `tpl_<metric>` with a 5-minute hold. Custom templates get these too. |
| `device_profiles.rule_pack` | Explicit recommendations, part of the versioned template document. Built-in network templates ship *Uplink down*, *WAN link down* (interface down, role uplink / wan, critical, 60 s) and *Uplink utilisation high* (> 80 % for 10 minutes, warning). A customised template whose pack is NULL uses its parent's. |
| State (enum) metrics | Every enum metric with an unambiguous bad state in its value map yields one state rule (see *State rules* below). Custom templates get these too. |

**Controller-managed children are never recommended on the controller.**
Groups whose rows are FortiAPs or FortiSwitches (a `children` mapping,
`display: "children"` or `belongs_to`; see
[controller-managed-children.md](controller-managed-children.md)) are skipped
by all three sources: their CPU, memory and status belong to the child
devices, which the ordinary `cpu` / `memory` / device status rules cover.
Before 1.25.18 the FortiGate template recommended *FortiAP Access Points: CPU
above 90%* and similar rules on the controller.

### State rules

A template's enum metric (`type: "enum"`) has a value map: `labels` gives
each code a text and a severity (`ok`, `info`, `warn`, `crit`). A state rule
is an ordinary rule on `tpl_<metric>` whose numeric conditions breach exactly
on the bad codes (`app.services.template_states`):

* the bad codes are those labelled `crit`; when a metric has none, every code
  other than its single `ok` one, if all of those are `warn` (a BGP peer that
  is not Established). A mix of `warn` and `info` (Juniper yellow alarm) is
  ambiguous and gets no recommendation;
* the set is written as at most four AND-combined conditions on the same
  metric, so the wizard shows and edits it like any compound rule:
  `tpl_fgt_ha_sync == 0`, `tpl_jnx_bgp_state != 6`, and for Cisco ENVMON
  power supplies and fans `tpl_cisco_psu_state >= 3` AND `!= 5` (Critical,
  Shutdown or Not Functioning; an empty bay, *Not Present*, never pages);
* 2-minute hold, recovery notice on; **critical P2** for power supplies, HA /
  failover / config sync, BGP and a red chassis alarm, **warning P3**
  otherwise (a tunnel, a fan, an SD-WAN link, a pool). The recorded values
  include the bad codes (`states`), so a release that changes a value map
  shows "recommendation changed" like a threshold change.

Built-in examples: *HA Members: Sync Status is Not Synchronized*, *IPsec
Tunnels: Status is Down*, *Power Supplies: State is Critical, Shutdown or Not
Functioning*, *Fans: …*, *BGP Peers: State is not Established*, *Red Alarm is
On*, *Pool members: Availability is Offline*, *Failover State is Offline or
Forced Offline*.

Each table row is its own incident (rule + device + `series:tpl_<metric>_<row>`).
The evaluator names it from the template, never by series key or code: the
row label from `device_template_values`, the group as a noun, the metric's
display name and the value's label — "IPsec tunnel Branch-12 on Firewall
FGT-01 (10.0.0.1) is Down.", "Power supply PS2 on Switch core-1 (10.0.0.2) is
Critical.", recovery "… is Up again.". A row that stops reporting is missing
data: its incident stays open. A hand-written `==` / `!=` condition on a state
metric must name one of its codes (422 lists them).

The canonical keys `fan_state`, `psu_state`, `vpn_tunnel_state`, `ha_state`
and `bgp_neighbor_down` have no collector and are refused with a 422 naming
the template state metric to use. `session_count` (no collector under that
name) and `jitter` (device status checks do not measure it) are refused for
new rules; existing rules on them keep loading and stay editable.
`temperature` rules are evaluated per ENTITY-SENSOR-MIB sensor
(`temperature_<entPhysicalIndex>`, Celsius sensors), one incident per sensor
named "Temperature sensor <entPhysicalName>"; before 1.25.18 they never
matched any series.

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

`server/tests/test_template_state_alerts.py` (no database) and
`server/tests/test_template_state_alerts_pg.py` (disposable PostgreSQL): bad
codes and their numeric conditions, state wording, state recommendations
(severity, priority, hold) and the managed-children exclusion, Accept writing
every condition, one incident per tunnel / power supply with recovery,
per-sensor temperature, and the 422s for unmapped, retired and unknown-code
conditions.
