# ZenPlus 1.24.25 — Network maps: trustworthy topology, build, export and history

Network Maps now builds and checks a map from current evidence instead of drawing whatever the switches ever reported. These fixes and features come from building the Al Aridh branch map on a customer appliance. See `docs/network-maps.md`.

- **Only current CDP/LLDP evidence:** neighbour entries not seen for 24 hours, or that name a port that is down or has never been up, are now treated as stale. Quick connect opens a review list. Current links are pre-ticked; stale ones show the reason and stay unticked. On the Al Aridh map, two links that Quick connect used to draw onto ports that had never been up are now caught.
- **Build from site / subnet:** places a location's, subnet's or seed device's devices in tiers (WAN edge, core, then by hop distance) and draws their current links. A version is saved first.
- **Add neighbours:** right-click a device to place its unplaced neighbours on free spots around it. Neighbours ZenPlus does not monitor appear as grey "not monitored" icons with a cable to the right port.
- **Verify topology:** checks every cable for an unbound end, a port that is down or never up, a port that no longer exists, missing or contradicting evidence, and down devices. Click a row to jump to the cable.
- **Servers and service checks as live nodes:** new Servers and Checks tabs in the palette. They show their own status and open their own pages.
- **Export and import:** PNG, SVG, PDF, draw.io (diagrams.net can convert to Visio) and a JSON backup. A JSON import creates a new map. Devices are re-matched by IP or hostname on another appliance.
- **Version history:** save, list and restore snapshots (newest 50 per map). Restore saves the current state first.
- **Live mode:**
  - Click a cable for both ends' port state, speed, rates and a 24-hour traffic sparkline.
  - Throughput pills avoid each other; zoomed out they shrink to the utilisation figure.
  - The hover card is readable at wall zoom and names both ends.
  - Fiber cables keep an amber stripe under the utilisation colour.
  - Bundles show ×N.
  - An automatic legend lists what the map actually uses.
- **Editing:**
  - Undo now covers deletes, inserts, Quick connect, paste and Add neighbours.
  - Shortcuts keep working after a toolbar click, and Esc clears the selection.
  - Design mode always shows port labels.
  - Locked zones let clicks and box-selection pass through.
  - Tidy layout keeps devices inside their zone.
  - Icons carry captions, and an icon or zone can open another map.
  - The view is remembered per map.
  - Find also searches captions.
  - The link dialog never wipes a port it doesn't recognise. It shows each port's state and speed, and adds Speed and Notes fields.
  - New links are straight.
- **Large estates:** the device picker now loads every device, not just the first 200.
- **Concurrent editing:** cables to icons are saved through their own row-locked requests, so two editors no longer overwrite each other's cables. Deleting a node or shape removes its cables.

Also included: the dashboard dependency security update from 1.24.24 (follow-redirects 1.16.1, DOMPurify 3.4.17) and the reports delivery changes from 1.24.23. Migration 166 (reports delivery) runs before 167.

Schema: migration 167. A map node can be a device, a server or a service check (exactly one), and a new `manual_map_versions` table holds snapshots. Existing maps need no changes.

API (additive):
- `kind` and `entity_id` on map nodes
- `include_stale` on `GET /maps/{id}/suggested-links`, plus `stale`, `reasons`, `members` and port state on each suggestion
- `pairs` on `POST /maps/{id}/auto-connect`
- `/maps/{id}/annotation-links`, `/maps/{id}/verify`, `/maps/populate/options`, `/maps/{id}/populate`, `/maps/{id}/nodes/{node_id}/expand`, `/maps/{id}/versions`, `/maps/{id}/bulk` and `/maps/{id}/bulk-delete`

After updating, reload any Network Maps tab that was open during the upgrade. An old tab still saves icon cables the legacy way and can overwrite cables added since.

No changes to device status confirmation, alert rules, thresholds or licensing.
