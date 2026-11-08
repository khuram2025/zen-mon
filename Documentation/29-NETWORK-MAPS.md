# Network maps: NOC wall, sites, live statistics and map design

## Delivery status

This document describes the network maps redesign (release 1.25.x). It covers the maps overview page, the
redesigned live view, site (grouped) nodes with drill-down, the NOC wall mode, custom icons and background
images, and the editor changes that support them. The rules the map editor already followed (one node per
device, server or service check; discovered links used only as current evidence; annotation cables changed only
through row-locked endpoints) are unchanged and remain documented in `docs/network-maps.md`.

Validation covers the API contract tests in `server/tests/test_manual_maps_api.py` (site aggregation, role and
visibility limits, list totals and metadata), the strict dashboard build, the route smoke check and the existing
map export test. The live view and the wall were checked on the development appliance at desk (1280 px),
desktop (1920 px) and wall (2560 px and wider) sizes.

## Where things are

| Page | URL | Purpose |
|---|---|---|
| Maps overview | `/maps` | Every map as a card with its health bar, device states and active alerts. Maps with problems sort first. Edit, Live and Wall links per map, and the NOC wall rotation builder. |
| Map editor / live view | `/maps/manual?map=<id>` (`&mode=live`) | Design mode edits the map; Live shows status, traffic and alerts. **All maps** (top left) returns to the overview. |
| NOC wall | `/maps/manual?map=<id>&wall=1` | Dark, chrome-free full-screen live view for video walls. See [NOC wall mode](#noc-wall-mode). |

The sidebar entry **Network → Network Maps** still opens the editor on the last map used; `/maps` is covered by the
same page permission (`maps.view`), so page-access roles that include Network Maps include the overview.

## Live view

### Summary tiles

The live view opens with a row of tiles across the top left of the canvas:

| Tile | Shows |
|---|---|
| Live | Clock, LIVE / STALE beacon (stale after 60 s without fresh data), data age. |
| Health | Share of nodes that are up, coloured by the worst state present, with a status-mix bar. |
| Devices | Down, warning and up counts; maintenance and unknown underneath. Members of site nodes are included. |
| Links | Links whose interface is down, links at 85 % utilisation or more, and how many cables have live data. |
| Traffic | Total throughput over every cable and the busiest link with its utilisation. |
| Sites | Sites with a down or warning member, total sites and the devices inside them (only when the map has sites). |
| Alerts | Critical and warning alert counts and the active total; click to open the alert list. |

On narrow screens the Links, Traffic and Sites tiles drop out first; the wall draws the same tiles larger.

### Devices on the canvas

- Each node is a lit tile whose ring carries the status colour (up green, warning amber, down red, maintenance
  blue, unknown grey). A status pip sits on the lower-right corner so the state reads even when the ring is subtle;
  a down node glows red and pulses.
- Active alerts appear as a badge with a bell and the count on the upper-right corner (red when any is critical).
- The label card shows the name and address. In Live it also shows CPU and memory with small bars, inside the card,
  so they no longer collide with cables. Labels are wider and only truncate very long names (the full name is in
  the tooltip).
- Text sizes are in rem, so the display density (compact, comfortable, large, NOC wall) scales them; when the map is
  zoomed out to fit, text counter-scales so it stays readable.
- Cables are weighted by their nominal speed: 10M/100M thinner, 10G/25G heavier, 40G and above heaviest. Live colour
  follows utilisation (green → lime → amber → red), a down interface draws the cable red and dashed with a ✕ at the
  failed end, and cables without a bound interface are drawn grey and dotted.

### Inspecting a device or link

Click a device in Live to open its **inspector** on the right: status, last seen, response time, uptime, CPU,
memory and temperature (when collected), every cable it terminates with the far end, ports, throughput and
utilisation (down cables first), and its newest active alerts. Click a cable in the list to jump to it and open
the link drill-down (both ends' port state, rates, utilisation and a 24-hour history). Esc closes either panel.
The hover card is hidden for the device whose inspector is open. The Attention list returns when both panels are
closed.

### Legend

The legend is collapsed by default in both modes and sits next to the zoom controls (bottom left), so it no longer
covers the map. Click it to open.

## Sites (grouped nodes)

A **site** is one tile that stands for many devices — a branch, a building, a data-centre hall. It is built for
large sites: the tile draws a single ring, and per-device cells are drawn only when the grid is opened.

### What a site shows

- Header: site name, active alert badge, and **Open** when the site has a drill-down map.
- A health ring (share of members up, segments by status) and the member counts: down and warning first, then
  maintenance, unknown and up.
- Footer: where the members come from, and **Devices** to open the member grid under the tile: the most urgent
  members listed by name, then one coloured cell per device (worst first, a critical alert outlines the cell).
  Clicking a member opens its page in a new tab. The grid shows up to 600 cells; a larger site says how many are
  shown and points to the site map for the rest.

Sites count in the summary tiles and appear in the Attention list when a member is down or warning.

### Membership

A site's members are the union of:

| Source | Meaning |
|---|---|
| Location | Every monitored device whose Location matches the site's location (case-insensitive, surrounding spaces ignored). Devices added to that location later join automatically. |
| Drill-down map | Every node (device, server or service check) on the site's own map. |
| Picked devices | Devices chosen by hand (from **Group into a site**). |

A device found by more than one source is counted once. Devices outside the viewer's visibility scope are never
counted or listed, and a drill-down map the viewer's role may not open is neither expanded nor offered.

### Creating and editing sites

- **Insert → Site (grouped devices)…** creates a site at the centre of the view. Choose a name, a location and/or
  a drill-down map. *Create a new map for this site* makes the site's own map; with a location chosen it can be
  built immediately from that location's devices and their discovered links (the same builder as **Build from
  site / subnet**).
- **Group into a site…** (right-click a multi-selection) turns the selected devices into a site placed where they
  were. With *Create a new map for this site* and *Move the selected items into the new site map* (the default),
  the selected devices, the shapes selected with them and the cables between them move to the new map, and this
  map keeps only the site tile. Cables from the moved devices to the rest of this map are not carried over. Ctrl+Z
  puts the devices back on this map (the new site map stays). Without a new map, the selection becomes the site's
  picked devices and stays on the map.
- Double-click a site (Design) or right-click → **Edit site…** to change its name or members. Resize it like any
  shape; its text scales with the tile. Sites can be cabled to devices like icons.
- *Open the device grid by default* makes Live and the wall show the member grid without a click.

### Drill-down

**Open** on a site (or double-click in Live, or right-click → **Open site map**) opens the site's map in the same
mode. The editor shows a breadcrumb of the maps you came from; click one to go back. On the wall the header shows
the same trail with a back arrow, and Esc goes back one level before it leaves the wall.

### API

`GET /api/v1/maps/{map_id}/sites-live` (permission `maps.view`) returns, for every site node on the map:

```json
{"data": {"<shape id>": {
  "total": 42, "counts": {"up": 39, "down": 1, "degraded": 2, "maintenance": 0, "unknown": 0},
  "alerts": {"active": 3, "critical": 1, "warning": 2}, "worst": "down",
  "members": [{"kind": "device", "id": "…", "hostname": "…", "ip_address": "…", "device_type": "switch",
               "status": "down", "alerts": {"active": 1, "critical": 1, "warning": 0}}],
  "truncated": false, "map_id": "<drill-down map or null>", "map_name": "…"}}}
```

`members` is sorted worst first and capped at 2000 (`truncated` says when more exist); the counts always cover every
member. The live view polls it every 15 s (every 60 s in Design) and only when the map has a site. A site is stored
as a shape (`kind: rectangle`) whose metadata carries `widget: "site"` and
`site: {location, map_id, device_ids, expanded}`; it is versioned, exported and imported like any other shape. No
schema migration is needed.

## NOC wall mode

The wall is the live view without application chrome, for video walls and large NOC monitors.

- **Wall** in the editor's top bar (or **Map → Open NOC wall in new tab**, or **Wall** on a map card) opens it. Full
  screen is requested when the browser allows it; the wall works the same without it (some browsers and kiosk PCs
  refuse full screen, which previously showed "Fullscreen is not available in this browser").
- Dark, high-contrast palette regardless of the user's theme; no sidebar, header, toolbars, minimap or zoom
  controls. The header shows the map name (and drill-down trail) and the summary tiles; the Attention list stays on
  the right (`attention=0` hides it).
- Data refreshes on its own: links and nodes every 15 s, sites every 15 s, the map itself every 30 s, so topology
  edits reach the wall without a reload.
- The map re-fits when the screen size changes or the map gains or loses items.
- Sizing follows the screen: below 1900 px wide nothing is enlarged (a desktop monitor looks like the desk view),
  from 1900 px on-canvas text grows 1.25×, from 2500 px 1.6× and from 3400 px 2.1×; the page's display density
  moves to Large (≥ 1700 px) or NOC wall (≥ 2500 px) while the wall is open and is restored when it closes.
  `scale=<0.5–4>` overrides the canvas text size; `density=` in the URL keeps the chosen density.
- The cursor and the Exit / Pause buttons hide after 3.5 s without mouse movement. Esc leaves the wall (or goes back
  from a drilled-into site first).

### Rotation

`/maps` → **NOC wall rotation…** builds a link that cycles through chosen maps:

```
/maps/manual?map=<first>&mode=live&wall=1&kiosk=1&maps=<id>,<id>,<id>&rotate=60
```

| Parameter | Meaning |
|---|---|
| `maps` | Map ids in display order. Ids the viewer cannot open are skipped. |
| `rotate` | Seconds per map, 15–3600 (default 60). A progress bar runs along the bottom. |
| `attention=0` | Hide the Attention list. |
| `scale` | Canvas text multiplier, 0.5–4. |
| `kiosk=1` | Keep the application in kiosk mode on that browser (no sidebar or header after leaving the wall). |

Space pauses or resumes the rotation, ← / → step through it, and opening a site pauses it until you come back.
A role limited to selected maps (page-access roles) suits a wall account: it only sees and rotates through its maps.

## Custom icons and background images

- **Custom device icon:** Design → double-click a device → **Custom icon image** → *Upload image…*. PNG, SVG,
  JPEG, WebP or GIF; raster images are scaled to 256 px in the browser and stored with the node
  (`metadata.image_src`). *Use built-in icon* reverts. The status ring, pip and alert badge still apply.
- **Map background image:** **Map → Background image…** uploads a floor plan, rack photo or region outline (up to
  15 MB in, scaled to at most 3200 px and stored compressed in the map, `metadata.background_image`). Opacity,
  width and position are a share of the drawing area; the height follows the image. It is drawn under every node
  and cable, zooms with the map and appears on the wall and in exports. **Remove** clears it.
- Images live inside the map, so versions, JSON export/import and duplication carry them, and nothing is fetched
  from outside the appliance. The map list (`GET /maps`) leaves out background image bytes and annotation cables
  (it reports `background_image.present` instead), so the overview stays light.

## Fixes in this release

| Problem | Fix |
|---|---|
| Fullscreen button showed "Fullscreen is not available in this browser" and did nothing on browsers that refuse full screen. | Replaced by the NOC wall mode, which fills the window itself and requests full screen only as a bonus. |
| The live legend opened over the map's own content. | Collapsed by default and moved next to the zoom controls. |
| CPU/MEM micro-bars under each device were unreadable (sub-8-px text) and overlapped cables. | Moved into the label card with labelled bars in the type scale. |
| Device labels truncated short names (for example "B8_SW01 (3750"). | Label cards size to their text up to 15 rem. |
| Alert badges were small dots that collided with the status dot. | Separate bell badge with count (upper right) and status pip (lower right). |
| The hover card and the pinned device card showed at the same time. | The pinned card is now the inspector; the hover card is hidden for that device. |
| The editor did not line up with the page edges on large screens (a 4 px gap at 2xl). | The editor frame follows the page padding at every breakpoint. |
| The summary bar and the Attention list competed for the top of the canvas. | Tiles sit top left, the Attention list / inspector top right; the inspector replaces the Attention list while open. |

## Files

| Area | Files |
|---|---|
| API | `server/app/api/v1/manual_maps.py` (`sites_live`, `_site_spec`, list totals and `_list_metadata`) |
| Overview | `dashboard/src/features/maps/MapsOverview.tsx` (route `/maps` in `App.tsx`) |
| Wall | `dashboard/src/features/maps/MapWall.tsx`; `.map-wall` palette in `styles/globals.css` |
| Sites | `canvas/SiteTile.tsx`, `canvas/SiteDialog.tsx`, site handling in `canvas/MapCanvas.tsx`, `ShapeNode.tsx` |
| Live view | `canvas/NocOverlays.tsx` (`computeMapStats`, summary tiles, problems), `canvas/NodeInspector.tsx`, `canvas/DeviceNode.tsx`, `canvas/NetworkEdge.tsx` |
| Images | `canvas/imageUpload.ts`, `canvas/BackgroundDialog.tsx`, custom icon field in `canvas/EditDialogs.tsx` |
| Tests | `server/tests/test_manual_maps_api.py` |
