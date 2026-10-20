# License node capacity

A ZenPlus license is a number of **nodes**. A node is one monitored machine:

| Inventory row | Nodes |
|---|---|
| Device (`devices`, `poll_mode` other than `via_controller`) | 1 |
| Server not linked to a device | 1 |
| Server linked to a device (`servers.device_id`, e.g. discovery "Both" import, Add Server with a device) | 0, it shares the device's node |
| Controller-managed child (`poll_mode = 'via_controller'`, a WLC's APs, FortiLink switches) | 0 |
| Service check | 0 (not a node) |

The single definition is `NODE_COUNT_SQL` in `server/app/services/license_capacity.py`.
`updater/inventory.py` carries an identical copy (a test asserts they match) because the
appliance reports this count to zentryc.com on every check-in: the count it reports is the
count it enforces.

## Where the limit comes from

The updater and the dashboard cache the zentryc.com subscription in
`/opt/zenplus/updater/config/subscription.json` on every check-in, registration and
**Refresh** in Settings > General > Licenses.

1. `license.total_node_cap` is the subscription's node pool, shared by every appliance on it.
   `used_node_count` is the pool's usage at the time of the snapshot, and
   `reported_node_count` (added by this appliance when it saves the snapshot) is what this
   appliance reported with that check-in. This appliance's limit is
   `total_node_cap - (used_node_count - reported_node_count)`: the pool minus what the
   other appliances use. A snapshot without `reported_node_count` (written by an older
   version, or after a failed count reported 0) gives this appliance the whole pool.
2. No node cap: `max_devices` is used. Neither one means the license server does not meter
   nodes, and nothing is enforced (`state = unmetered`).
3. `is_expired`, an `expires_at` in the past or `is_active = false` block all additions
   (`expired` / `inactive`).
4. An unregistered appliance gets the evaluation allowance, `EVALUATION_NODE_LIMIT` (50).

## What is enforced

Every path that creates a node takes the transaction-scoped advisory lock
`license-node-capacity`, counts, and refuses with **HTTP 402** once the limit is reached:

```json
{"detail": {"code": "license_capacity_reached",
            "message": "You have reached your license capacity (50 of 50 nodes). Extend your license to add more devices or servers.",
            "capacity": { ...GET /subscription/capacity... }}}
```

| Path | Behaviour at capacity |
|---|---|
| `POST /devices` (Add Device) | 402 |
| `POST /devices/bulk-import` | creates up to the remaining nodes, reports the rest as skipped; 402 when none are left |
| `POST /snmp/discover/{job}/import` | same as bulk import |
| `POST /discovery-v2/runs/{id}/import` | each new device or unlinked server takes a node; items past the limit fail with the capacity message; updates of existing rows still work |
| `POST /servers` (Register / Add Server) | 402, unless linked to a device that already holds a node |
| Agent enrollment creating a new server | 402; the agent retries on its normal schedule |
| Approving pending agents (`POST /agent-fleet/{id}/authorize`, fleet bulk authorize) | 402 when the approval would create servers past the limit |
| Accepting a cloned agent as a new machine | 402 |

Enforcement never removes anything. When a license shrinks, lapses or is exceeded, every
existing device and server keeps being monitored and can still be edited or deleted;
deleting frees nodes immediately. Decommissioned (disabled) servers still count until they
are deleted.

## UI

* `GET /api/v1/subscription/capacity` (any signed-in user) returns the state, limit, usage
  breakdown and message. `dashboard/src/hooks/useLicenseCapacity.ts` reads it.
* The capacity response also carries `pool_used` / `pool_available`: the other
  appliances' share from the last check-in plus this appliance's live count. The
  Subscription card's **Subscription nodes** tile uses them, so it always agrees with
  Node capacity; zentryc.com's own `used_node_count` only moves on a check-in (Refresh
  pushes one) and is shown only until the capacity has loaded.
* Settings > General > Licenses shows the **Node capacity** card: used / limit meter,
  devices, servers, linked servers and controller-managed children, the shared-pool split,
  and the capacity message.
* `LicenseCapacityNotice` appears on the Devices and Servers pages and in Add Device,
  Register Server, Add Server and the discovery import dialog. It is amber at 90 % and red
  once nothing can be added; the dialogs then disable their create button and show the
  same message the API returns.
