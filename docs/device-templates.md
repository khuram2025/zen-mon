# Device Templates

A device template says what a kind of device can report over SNMP and how to read it: which
metric groups to poll, at what cadence, how to label states, which page tab shows them, and how
controller-managed children are read. Templates live in `device_profiles`; the page is
**Network → Configuration → Device Templates** (`/device-templates`). Phase 1 of
`docs/plans/device-templates-plan.md`, migration 189.

## What a template is

| Column | Meaning |
| --- | --- |
| `oid_groups` | The effective, published metric groups. The poller, the device page and the alert evaluators read only this. |
| `match_rules`, `match_priority` | How devices are classified to it (below). |
| `device_type`, `icon` | What a device on the template is. |
| `builtin`, `builtin_key` | Shipped and updated with releases; read-only. `builtin_key` is stable across renames. |
| `parent_id`, `overlay` | A customised template: the parent's groups plus the differences in `overlay`. |
| `revision`, `content_hash` | Bumped on every published change; the hash notices changes made outside the API. |
| `draft` | Unpublished edits. Devices keep polling the published revision. |
| `pilot_device_ids`, `pilot_oid_groups` | A draft published to a few devices only. |

Every published revision is a row in `device_profile_versions` (snapshot, action, reason, who).
Actions: `baseline` (state when history began), `created`, `published`, `rollback`, `imported`,
`cloned`, `customized`, `external_update` (a release changed a built-in), `parent_update` (a
customised template was re-resolved after its parent changed).

## Classification

`scripts/dev/template-match-contract/vectors.json` is the contract; the Go poller
(`poller/internal/checker/snmp/profile.go`) and Python (`app.services.device_templates.classify`,
used by legacy discovery import, the Unmatched view and Explain) both pass it. Change both and the
vectors together.

* OIDs are normalised; a sysObjectID prefix matches only on a dot boundary.
* A template matches by prefix (strength 2) or, when none of its prefixes matched, by
  `sys_descr_regex` (strength 1). With `require_sys_descr` a prefix hit counts only when the
  pattern matches too (for models that share a sysObjectID).
* Rank: `match_priority` (higher first), strength, longest prefix, name. No ties.
* Patterns must run in both engines: lookarounds, backreferences, atomic groups and possessive
  quantifiers are refused at save; an unusable stored pattern never matches.

Who assigned a device's template is `devices.profile_source`: `auto` (classifier), `rule`
(discovery rule), `manual` (operator), `import`, or NULL (assigned before 1.25.10). The poller
assigns a template only to a device that has none, and never to a controller-managed child. When
a device that already has a template now matches a different one, the poller records
`profile_suggested_id`; it is listed under **Suggestions** and applied only when someone accepts
it. A template's `device_type` is given to a device the first time the classifier assigns it, and
only while the device's type is still `other`; operators can apply it when assigning.

## Customising, cloning, editing

* **Customise** creates a template with `parent_id` set and an empty overlay, one priority step
  (10) above the parent, optionally moving the parent's devices. Edits store only differences:
  disabled groups and metrics, changes to a group's name, interval, tab, description or
  applicability, changes to a metric's name, unit, thresholds or labels, added groups and
  metrics. Inherited OIDs, types, table settings and children mappings cannot change (that would
  fork the parent; clone instead). At most three levels.
* When a release updates a built-in, the background resync (`resync_loop`, every 5 minutes and
  20 s after start) records an `external_update` revision and re-resolves every customised
  template from it (`parent_update`). Overlay entries that no longer apply are kept, skipped and
  shown as warnings on the template.
* **Clone** makes an independent copy that does not follow the source.
* Edits are saved as a **draft**. **Impact** shows groups and metrics added or removed, OIDs per
  poll before and after, devices and customised templates affected, and alert rules that use a
  removed metric. **Publish** goes to every device or to a **pilot** (devices already on the
  template); the pilot devices poll the draft until it is published to all or cancelled.
  **Rollback** publishes an earlier revision as a new one.
* Publishing removes current readings (`device_template_values`) of groups that no longer exist;
  switching a device's template removes the previous template's readings. ClickHouse history is
  never deleted by a template change.

## Test on a device

`POST /snmp/profiles/{id}/test` runs the draft or published groups against one device from the
API host (`snmpget` per scalar chunk, `snmpbulkwalk` per table column, at most 200 rows) and
stores nothing. Refused for controller-managed children and for devices polled through a remote
sensor. One test per device at a time, two at once per worker.

## Device types

`device_types` replaces the old seven-value CHECK on `devices.device_type` with a foreign key.
Fifteen built-in types are seeded (router, switch, firewall, load balancer, SD-WAN edge, wireless
controller, access point, server, storage, UPS, PDU, printer, IP camera, VoIP phone, other);
customers can add their own. Built-in types can be relabelled, not deleted; a type in use cannot be
deleted. `category` is the roll-up used in charts.

## Permissions

* Read templates and types: the `snmp` module's view permission (`devices.view` or
  `discovery.view`) and `devices.view` respectively.
* Change templates and types: `devices.manage_templates`, and the user must not be tag-scoped
  (templates apply to every device). Built-in definition version 8 grants it to admin, operator and
  network_team; a custom role that relied on `devices.manage` to edit templates needs it added.
* Assign or unassign devices: `devices.manage` and every device in the caller's scope.
* Every change is audited (`device_template.*`, `device_type.*`).

## API (`/api/v1/snmp/profiles`, `/api/v1/device-types`)

| Endpoint | Use |
| --- | --- |
| `GET /snmp/profiles`, `GET /snmp/profiles/{id}` | List; detail with draft, overlay, parent groups and warnings |
| `POST /snmp/profiles`, `PUT /snmp/profiles/{id}` | Create; save and publish at once (API clients) |
| `PUT /snmp/profiles/{id}/draft`, `DELETE …/draft` | Save or discard a draft |
| `POST /snmp/profiles/{id}/impact` | What publishing the draft would change |
| `POST /snmp/profiles/{id}/publish` | Publish to all, or `pilot_device_ids` |
| `POST /snmp/profiles/{id}/pilot/cancel` | End a pilot |
| `POST /snmp/profiles/{id}/rollback` | Publish revision N again |
| `GET /snmp/profiles/{id}/versions[/{rev}]` | History, and a revision compared with the current one |
| `POST /snmp/profiles/{id}/customize`, `/clone` | Overlay child; independent copy |
| `GET /snmp/profiles/{id}/export`, `POST /snmp/profiles/import` | Format 2 (overlay with parent by `builtin_key`); format 1 still imports |
| `POST /snmp/profiles/{id}/test` | Test on a device |
| `GET /snmp/profiles/{id}/devices` | Devices with source, suggestion, pilot flag and coverage |
| `POST /snmp/profiles/{id}/assign`, `/unassign` | Put devices on or take them off a template |
| `GET /snmp/profiles/unmatched`, `/suggestions`; `POST /suggestions/accept`, `/classify`, `/resync` | Classification views |
| `GET/POST/PUT/DELETE /device-types` | Type catalogue |

## Not in phase 1

Components shared by many templates, probe-based (extra OID) classification, a shared default
page layout per template, dynamic groups, rule packs and threshold overrides, and policy bundles
are later phases of the plan.
