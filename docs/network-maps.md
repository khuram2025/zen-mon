# Network maps (Network Studio)

`/maps/manual` is the hand-built topology editor (React Flow, `dashboard/src/features/maps/`) backed by
`/api/v1/maps` (`server/app/api/v1/manual_maps.py`, topology logic in `server/app/services/map_topology.py`).
This page records the rules the map follows since 1.24.23, which came out of building the Al Aridh branch
map on a customer appliance (assessment D1–D15, F1–F12).

## Nodes

A node is exactly one of a device, a server (Servers module) or a service check
(`manual_map_nodes.device_id | server_id | service_check_id`, enforced by `manual_map_nodes_one_entity_check`,
migration 167). The API returns `kind` and `entity_id` with every node; older clients that only know
`device_id` keep working for device nodes. Status mapping for the map's up/degraded/down/unknown palette:

| Source | Mapping |
| --- | --- |
| Device | `devices.status` as is |
| Server | healthy → up, warning → degraded, critical → down, disabled → maintenance, stale/unknown → unknown |
| Service check | warning → degraded, others as is |

The palette pages through `/devices` (200 per page) so estates above 200 devices can place every device.

## Discovered links must be current evidence

CDP/LLDP rows in `topology_links` are never deleted; a recabled port leaves its old row behind. A row is
**stale** when it was last seen more than 24 hours ago (`FRESH_HOURS`), or when the local or remote
interface is down (including never up) or missing. Stale rows are:

- excluded from suggested links, Quick connect, Build from site and Add neighbours by default;
- returned with `stale` and `reasons` (`old_evidence`, `local_port_never_up`, `remote_port_never_up`,
  `local_port_down`, `remote_port_down`, `local_port_missing`, `remote_port_missing`) when
  `include_stale=1`, so the Quick connect review shows them unticked with the reason;
- flagged by **Verify topology** when an existing cable relies on them.

Interface names are stored in the owning device's own `if_name` spelling. Ports that cannot be matched
(unmonitored neighbours) are abbreviated (`GigabitEthernet1/0/52` → `Gi1/0/52`). The link editor matches a
stored name by its normalised form and keeps an unmatched name visible instead of clearing it.

Quick connect never creates links silently: it opens a review of the adjacencies (fresh ones pre-ticked).
Created links have no text label, a straight shape, `kind` fiber/ethernet from the port type or speed, a
speed label, and `members` when the pair has more than one physical link (drawn as one cable with a ×N
badge).

## Annotation cables

Cables that touch an icon or shape live in `manual_maps.metadata.annotation_links`. They are edited only
through `POST/PUT/DELETE /maps/{id}/annotation-links[/{al_id}]`, each of which locks the map row, so two
editors cannot overwrite each other's cables. `PUT /maps/{id}` keeps the stored cables when its metadata has
no `annotation_links` key. Deleting a node or shape prunes the cables touching it. A cable with no port on
either end is a *context cable* (an iLO, a cloud): it is never reported as unbound.

## Editing

- Undo covers moves, property edits, creates and deletes (device/shape/cable/annotation cable, Quick
  connect, Add neighbours, paste/duplicate). A delete is undone by re-creating the items in one
  `POST /maps/{id}/bulk`; the editor maps old ids to the new ones so earlier history entries still apply.
- Keyboard shortcuts listen on `window` (ignored while typing or while a dialog is open), so they keep
  working after a toolbar click. They run after React Flow's own node keyboard handler, which treats Escape
  as a selection key.
- Locked rectangles/ellipses are background zones: clicks and box-selection pass through; the caption and
  lock badge stay selectable. Box-select never picks up a zone.
- Tidy layout tidies each zone on its own and keeps devices inside their zone.
- Design mode always shows port labels; the Live toolbar toggles (Throughput, Ports, Flow, Attention) only
  affect Live.
- The viewport is remembered per map in the browser (design mode). Find on map also searches captions.
- Icons carry a caption (`shape.text`). Any icon or zone can open another map (`metadata.link_map_id`).

## Building and checking a map

- **Build from site / subnet** (`POST /maps/{id}/populate`): location, CIDR or seed device (+ depth).
  Tiers: WAN edge (firewall/router), core (highest-degree switch per connected group), then by hop distance;
  devices without current links go in the last row. A version is saved first.
- **Add neighbours** (`POST /maps/{id}/nodes/{node_id}/expand`): places a device's unplaced current
  neighbours on free spots around it; unmonitored neighbours become grey "not monitored" icons with a
  cable to the right port.
- **Verify topology** (`GET /maps/{id}/verify`): `unbound`, `half_bound`, `port_not_found`, `port_down`,
  `port_never_up`, `no_evidence`, `contradicted`, `stale_adjacency`, `device_down`, `device_unknown`.

## Versions, export and import

- `manual_map_versions` keeps the newest 50 snapshots per map. Build and restore save one first; restore
  saves the current state before replacing it.
- Export: PNG (2×), SVG, PDF (A3), draw.io XML (open in diagrams.net to convert to Visio) and a JSON backup.
  Image export switches the canvas to 100 % so labels are true scale, and does not embed webfonts.
  SVG output inlines every element's computed style and is large (tens of MB for a ~30-device map).
- Import JSON creates a new map. Device/server/check ids differ between appliances, so ids not found locally
  are re-matched by IP, then hostname; anything still unmatched is skipped and reported.

## Live mode

The link drill-down (click a cable) shows both ends' port state, speed, rates, utilisation and a 24-hour
sparkline from `/devices/{id}/interfaces/{if_index}/metrics`. Throughput pills slide along their cable to
avoid each other and device tiles; port chips hide below 45 % zoom unless the cable is hovered or selected.
Fiber cables carry an amber centre stripe that stays visible under the utilisation colour.
