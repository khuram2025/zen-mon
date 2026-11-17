# Device groups and custom fields (Device Templates plan, phase 2)

Groups collect devices and servers so alert rules, maintenance windows and
other policies can target a class of assets instead of a list. Custom fields
add typed attributes (contract, owner, cost centre, rack) that groups, list
columns and alert messages can use. Both live under **Network → Groups &
Fields** (`/device-groups`).

The research and the five-phase plan are in `docs/plans/device-templates-plan.md`;
phase 1 (templates and device types) is described in `docs/device-templates.md`.
Migration: `scripts/migrate-192-device-groups.sql`.

## Groups

`device_groups` keeps its existing columns and gains:

| Column | Meaning |
| --- | --- |
| `kind` | `static` (members are chosen) or `dynamic` (members are whatever matches `condition`). Cannot change after creation |
| `condition` | Dynamic groups only: a condition tree in the discovery-rules format (`app.services.discovery_rules.conditions`) |
| `asset_kinds` | What the group holds: `device`, `server` or both |
| `parent_id` | Groups nest at most **5** levels; cycles are refused (`service.check_parent`) |
| `member_count`, `evaluated_at`, `eval_error` | Maintained by the reconcile pass |

### Membership

`device_group_members (group_id, asset_kind, asset_id, source)` is the one
membership table. `source` says why the asset is in the group:

* `primary`: the device's own `devices.group_id`. The trigger
  `zp_device_primary_group_member` keeps this row in step with the column, so
  the existing device form, bulk edit, CSV import and discovery import keep
  working unchanged. A device has at most one primary group, and it must be a
  **static** group (`service.assert_static`, enforced in device create/update,
  discovery import and discovery profiles; the discovery rule action
  `set group` offers static groups only).
* `static`: added by hand on the group page (`POST /device-groups/{id}/members`).
  Servers and additional devices join static groups this way.
* `rule`: written by the reconcile pass for dynamic groups. Never edited by hand.

A group's members include the members of its subgroups. The views
`device_group_ancestry` and `device_group_member_closure` (group_id,
asset_kind, asset_id) give that roll-up; use the closure whenever the question
is "is this asset in group X". Deleting a device or server removes its rows
(`zp_asset_deleted_cleanup`).

### Dynamic groups

A dynamic group's condition is evaluated over the facts in
`app.services.device_groups.facts.BASE_FIELDS` plus one `custom.<key>` fact
per custom field:

| Facts | Type |
| --- | --- |
| kind (device/server), name, IP address, vendor, model, OS, OS version, sysObjectID, sysDescr, location, owner, environment, serial number, controller | text / OID / version / IP |
| device type, template, primary group, site | reference (pick list) |
| tags | set |
| status | enum |
| managed child, SNMP enabled, ping enabled, NCM enrolled | boolean |
| custom fields | their own type (text, number, date, choice, boolean) |

A fact an asset does not have (a server's sysObjectID, an unset custom field)
makes a positive test false and a negative test (`does not equal`, `is empty`)
true. The `date` type (on / before / after / is empty) was added to the shared
condition engine for custom date fields.

The editor shows a live **preview** (`POST /device-groups/preview`): how many
assets match now and the first matches, limited to the caller's scope.

Reconcile (`membership.reconcile`) recomputes the `rule` rows:

* after a group is created or its condition changes, for that group;
* after a custom-field CSV import, for all groups;
* every 60 s in the background (`reconcile_loop`, started in `main.py`), under
  the transaction advisory lock `device-groups-reconcile` so one worker runs it.
  A pass whose groups, facts and field definitions are unchanged since the
  previous pass in that process writes nothing;
* on demand with `POST /device-groups/reconcile`.

A condition that fails to evaluate keeps the previous members and records
`eval_error`; it never empties the group.

### Used by and deletion

`GET /device-groups/{id}` returns `used_by`: alert rules, maintenance windows,
sensor assignments, discovery profiles, discovery rules, subgroups and the
number of devices whose primary group it is. Deleting a group that is used
anywhere is refused with 409 and the list.

## Group scope in evaluators

Every place that asked "is this device in the rule's group" now asks the
membership (primary, static, rule, and subgroups) instead of comparing
`devices.group_id`:

* `network_alert_service` (SNMP metric rules), `alert_engine` (device status,
  traps), `device_flap_notice_service`, `network_events` (syslog),
  `udt_alert_service` (Device Tracker);
* maintenance windows: `alert_engine._device_in_maintenance` and the Go
  poller's `maintenanceDeviceIDsSQL` (with a legacy fallback when the closure
  view is missing, so a poller started before the migration keeps working);
* `alert_rule_scope` (the "affects N devices" count) and the Devices list
  group filter.

`membership.groups_of` and `membership.load` return `None` when membership
cannot be read; callers then fall back to the primary group, so an evaluator
never stops because the membership tables are unavailable.

Not changed in phase 2: traps still ignore device type and location scope;
Device Tracker still matches location exactly; sensor group assignments and
the vantage view still use the primary group; host (server) alert rules have
no group scope yet.

## Custom fields

`custom_field_defs` (key, label, type, choices, asset_kinds, description,
sort order, show in list) and `custom_field_values` (one typed value per field
and asset). Custom fields are separate from tags.

* Keys are lowercase letters, digits and `_`, starting with a letter (max 50)
  and cannot change. The type cannot change. Choices in use cannot be removed.
* A field used by a dynamic group cannot be deleted.
* Values are edited on the device page (Configuration tab, **Custom fields**
  card) with `devices.manage` / `servers.manage` and the asset in the caller's
  scope. Fields with **show in list** appear as Devices list columns.
* Alert messages for device status and SNMP metric alerts can use
  `{custom.<key>}`; an unset field renders empty, and a read failure never
  blocks the message.

### CSV import

`POST /custom-fields/import` (Groups & Fields → Custom fields → Import). The
first column names the asset: `ip_address` or `hostname` for devices,
`server` (display name or hostname) for servers. Every other column is a field
key. A blank cell leaves the value as it is. The first call is a dry run that
lists what would change and the rows that could not be matched; `apply=true`
writes it. At most 20 000 rows.

## Permissions

| Action | Needs |
| --- | --- |
| View groups, members, catalog, preview | `devices.view` (member lists are limited to the caller's scope) |
| Create, edit, delete groups; add/remove static members; reconcile; define custom fields; CSV import | `devices.manage_policies` and an unscoped user |
| Edit a device's / server's custom field values | `devices.manage` / `servers.manage` and the asset in scope |

`devices.manage_policies` is new (built-in role definition version 10): admin,
operator and network team have it.

## Tests

* `server/tests/test_device_groups_pg.py` (disposable PostgreSQL): trigger and
  closure, nesting limits, dynamic reconcile, preview, custom fields and CSV
  import, used-by refusal, evaluator scope and maintenance through a dynamic
  group, permissions.
* `server/tests/test_discovery_rules*.py` cover the shared condition engine,
  including the `date` type and the `fields`/`enums` parameters.
