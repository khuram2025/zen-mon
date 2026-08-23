# Managed Child Devices (controller-managed APs & switches)

*Aug 2026 — migrate-069, `managed_device_service.py`*

## What it does

A FortiGate acting as WiFi/FortiLink controller (and any wireless controller
whose vendor pack opts in — the Aruba builtin already does) reports the APs
and switches it manages through its monitoring-template tables. This feature
promotes those rows to **first-class child devices**, the way SolarWinds /
Zabbix (host prototypes) / PRTG model thin APs under a WLC:

- per-AP/switch status, alert rules, maintenance windows, tags, reports;
- a `via <controller>` link both ways in the UI (devices list badge, device
  header pill, vendor-insight rows link to the child's page);
- dependency-aware suppression: each child gets a `controller` edge in
  `topology_dependencies`, so a controller outage raises **one** root-cause
  alert instead of one per AP.

## Design rules

1. **Controller is the inventory source of truth.** Children are created,
   renamed and re-parented from its template tables (`device_template_values`),
   never hand-added. A child the controller stops reporting decays to
   `unknown` after a 10-minute grace and is kept — history survives.
2. **Identity**: serial number when the pack collects one (`serial_key`),
   else the stable `(controller, group_key, instance)` triple. A serial that
   matches a directly-polled device only *links* the records (metadata) and
   never flips its polling or status.
3. **Children are `poll_mode='via_controller'`**: `ping_enabled=snmp_enabled=false`,
   `ip_address` NULL allowed (`managed_ip` holds the controller-reported one),
   status translated from the vendor enum via the pack's `status_map`.
   The Go poller additionally skips IP-less rows defensively.
4. **Opt-in per controller** (`devices.promote_managed`, default OFF — an OTA
   update must never silently inflate an inventory). Toggle lives on the
   controller's Vendor Insights header; flipping it on syncs immediately.
5. **First sight is a silent baseline** — only *observed transitions* feed
   `evaluate_status_change` (same pipeline as the poller, so device-down
   alert rules, notifications and maintenance windows just work).
6. **Licensing**: `via_controller` children are excluded from the
   subscription device count.

## How a vendor pack opts in

Add `children` to a table group in `device_profiles.oid_groups`
(schema: `OidGroupChildren` in `server/app/schemas/snmp.py`):

```json
{ "key": "access_points", "kind": "table", "...": "...",
  "children": {
    "device_type": "access_point", "vendor": "Fortinet",
    "status_key": "fgt_ap_status",
    "status_map": {"2": "up", "5": "up", "1": "down", "0": "degraded"},
    "model_key": "fgt_ap_model", "ip_key": "...", "serial_key": "...",
    "os_version_key": "..." } }
```

migrate-069 seeds this for the Fortinet FortiGate pack (`access_points`,
`fortiswitch`) and the Aruba Wireless Controller pack (`access_points`).
Cisco WLC / Ruckus / others are data-only additions.

## Moving parts

| Piece | Where |
| --- | --- |
| Schema (columns, indexes, dep type, pack seeds) | `scripts/migrate-069-managed-child-devices.sql` |
| Sync service + sweeper (60 s, advisory lock `…392`) | `server/app/services/managed_device_service.py`, started in `main.py` |
| API (fields, `managed_by` filter, promote toggle, insights `child_device_id`) | `server/app/api/v1/devices.py`, `schemas/device.py` |
| License exclusion | `server/app/api/v1/subscription.py` |
| Poller guards (`ip_address IS NOT NULL`) | `poller/internal/store/postgres.go` |
| UI (badge+filter, header pill, insights toggle & row links) | `DevicesPage.tsx`, `DeviceDetailPage.tsx`, `TemplateInsightsSection.tsx` |

## Known limitations / next phases

- **Fortinet serials**: fgWcWtp*/fgSwDevice tables are string-indexed and the
  poller hashes long instances (`instanceToken`), so serials aren't captured
  yet → identity falls back to the instance triple, and an AP moving between
  *separate* controllers becomes a new child. Fix by collecting an accessible
  serial column (`serial_key`) or decoding printable string indexes in the
  poller.
- Child **availability/SLA** (they have no ping rollups) and per-child metric
  history fan-out (clients, radio, CPU) are phase 2; the numbers remain
  chartable today from the controller's insight tables (`tpl_*` series).
- Direct-poll **merge** (child that is also reachable by IP) is wired for
  serial matches only.
- Deleting a controller orphans its children (`managed_by SET NULL`,
  they decay to `unknown`); bulk cleanup UI is future work.

## Verification (2026-08-09)

Sandbox schema clone (`mdtest`) + real service code: migration applies and
replays idempotently (classifier: `created_objects=[] / writes_rows=False /
replay_safe=True`, 46 convergence tests pass); harness passed all scenarios —
promote-off no-op, 5 children materialized with correct fields, no baseline
events, idempotent re-run, up↔down transitions fire exactly one event each,
rename propagation, stale decay to `unknown`, 5 `controller` dependency rows,
suppression lookup finds the downed controller.
