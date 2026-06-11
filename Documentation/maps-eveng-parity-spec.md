I now have everything verified: the live schema, the Pydantic models, endpoint surface, icon set (14 slugs), migration convention, and the absence of static/upload infrastructure. The inventory was accurate. I have enough to write a concrete, grounded spec.

Key verified facts driving the spec:
- `manual_map_shapes` table + full CRUD endpoints **already exist** but have 0 rows — annotations are built on the backend but unused by the frontend.
- Links store shape/interface/kind in metadata JSONB; no waypoints anywhere in production.
- No StaticFiles mount, no upload subsystem anywhere in main.py.
- Migration convention: `/opt/zenplus/scripts/migrate-0NN-*.sql`, next free = **030**, applied via psql (no central runner).
- Node icon is `varchar(40)` slug; 14 frontend icons.
- Coordinates are `numeric(6,2)` percent, clamped 2–98 (nodes) / 0–100 (shapes).

Here is the design spec.

---

# ZenPlus Network Maps — EVE-NG / PNETLab Parity Design Spec

*Verified against live Postgres (`zenplus` DB, 127.0.0.1:5432) and source at `/opt/zenplus` on 2026-06-08. All schema claims below were confirmed with `psql` against the running database, not just migration files.*

---

## 1. Executive Summary

### Where we are vs EVE-NG

We already have **two** hand-rolled SVG+DOM map editors:

| | `ManualMapsPage.tsx` (2979 lines) | `TopologyPage.tsx` (1726 lines) |
|---|---|---|
| Purpose | User-curated weathermap (device-backed nodes, persisted server-side) | Auto-discovered LLDP/CDP topology (positions in localStorage) |
| Backend | `manual_maps`/`_nodes`/`_links`/`_shapes` (Postgres) | `topology_links`/`topology_dependencies` |
| Strengths | Live-status overlay, NetFlow throughput chips, NOC video-wall, suggested-links, server persistence | Auto-layout (hierarchical/grid/radial), dependency arrows, cursor-anchored fit-to-view |

Both render every node as an absolutely-positioned React `<button>` over a single percent-based `<svg viewBox="0 0 100 100" preserveAspectRatio="none">`. This is the right altitude for ~20 nodes (we have 19 nodes / 13 links in production today) but has **hard ceilings**: DOM-per-node, `preserveAspectRatio="none"` distorts orthogonal/curve geometry on non-square viewports, percent coords clamp 2–98, and there is **no entity model** for selection sets, z-order, locks, shapes, text, or background images.

Against EVE-NG we are **strong on live status** (EVE has none — it only color-codes nodes, not links) and **weak on editor mechanics**: no multi-select, no context menus, no per-element lock/z-order, no shapes/text/background, no snap, no undo. The `manual_map_shapes` table and its full CRUD endpoints **already exist on the live DB** (9 shape kinds, z_index column) but have **zero rows** — the backend for annotations is built and the frontend never uses it.

### The single biggest architectural decision

**Recommendation: Adopt React Flow (`@xyflow/react`) as the canvas substrate for `ManualMapsPage`, and converge `TopologyPage` onto the same component afterward. Do NOT extend the hand-rolled percent-SVG renderer.**

Justification:

1. **The percent + `preserveAspectRatio="none"` model is unfixable in place.** It is the root cause of geometry distortion and blocks port-anchored endpoints, snap-to-grid, and accurate waypoints. Every feature below (snap, align, marquee math, minimap viewport rect) requires a real absolute-pixel coordinate space with a proper viewport transform. React Flow gives us pan/zoom (cursor-anchored), marquee multi-select, snapGrid, minimap-with-viewport-rect, controlled viewport, and custom node/edge renderers **for free** — these are exactly our P0 gaps and each is hundreds of lines of fragile hand-rolled code otherwise.

2. **We are not throwing away the valuable work.** The ~3000 lines that matter — the live-status overlay (flow animation, util coloring, `links-live` join), LLDP/CDP suggested-links, NOC video-wall, the 14 bespoke `network-icons`, the LinkWizard, and all the React Query data wiring — are **engine-agnostic** and port directly into React Flow as custom node/edge components and side panels. The part we discard is the *geometry/transform/hit-testing* plumbing, which is exactly the part that's broken and that React Flow does better.

3. **PNETLab itself did this.** The research confirms PNETLab rebuilt EVE-NG's jQuery+jsPlumb canvas in React. We'd be following the proven modernization path, and our data model can stay close to EVE's (id, icon, position, interfaces) so the mental model transfers.

4. **Rejected alternatives:**
   - *Extend in place* — fastest to start, but we'd hand-build marquee, viewport-rect minimap, snap, and a transform that doesn't distort. That's reinventing React Flow's core, badly, and we'd still be DOM-per-node.
   - *jsPlumb Community* — what EVE uses, but it's jQuery-era, gives us links but not pan/zoom/multi-select/minimap; we'd bolt those on anyway. The good parts (Toolkit/Miniview) are commercial.
   - *Konva/canvas* — wins for thousands of free-draw shapes, but we'd rebuild floating edges, port labels, connection dragging, and pan/zoom UX by hand. Overkill for a network-diagram tool at our scale.
   - *Cytoscape* — great for graph analysis/auto-layout but fights custom rich-DOM nodes, free-form positioning, image backgrounds, and annotation shapes — the opposite of what we need.

**Migration cost is bounded** because React Flow accepts absolute `{x,y}` nodes and custom components; the work is "re-skin our existing node/edge JSX as RF custom types + write a one-time percent→pixel migration." We keep the API shape, add columns additively (as migrations 019/020 did), and ship behind a feature flag on the manual-maps route first.

**Coordinate decision:** Move from `x_pct/y_pct` (percent) to **absolute pixels on a fixed logical lab canvas** (default 4000×2400). Keep `x_pct/y_pct` columns populated via a generated/derived write for backward-compat during transition, but treat pixels (stored in a new `node_style`/typed columns) as canonical for rendering. Drop `preserveAspectRatio="none"` entirely.

---

## 2. Gap Matrix

Status: **Have** (full) / **Partial** / **Missing**. Priority: **P0** (parity-critical, ship first) / **P1** (strong differentiator or expected) / **P2** (polish).

| # | EVE-NG / PNETLab feature | ZenPlus current status | Where today | Priority |
|---|---|---|---|---|
| 1 | Pan canvas (drag/scroll) | Partial — middle/Shift-drag only, no edge-scroll | both pages | **P0** |
| 2 | Zoom (wheel + buttons) | Partial — wheel gated by ctrl/large delta; +/- not anchored; "Fit" is hard reset | ManualMaps | **P0** |
| 3 | Fit-to-screen (bbox + center) | Partial (real in Topology), Missing in ManualMaps | TopologyPage has it | **P0** |
| 4 | Add node to canvas (palette + right-click) | Partial — palette drag only, no right-click add, device-backed only | ManualMaps | **P0** |
| 5 | Node icon library + per-node icon change | Partial — 14 bespoke icons, no custom upload, slug-only | network-icons | **P1** |
| 6 | Node states (color + status glyph) | **Have** (stronger than EVE — live status, pulse halos) | both | — |
| 7 | Drag single node | Partial — works, no snap, no alignment guides | both | **P0** |
| 8 | Drag multiple nodes (group move) | **Missing** | — | **P0** |
| 9 | Multi-select (marquee + Ctrl/Shift-click) | **Missing** (single select only) | — | **P0** |
| 10 | Link drawing (hover-handle drag-to-connect) | Partial — Connect tool + cable handle; no port-anchored endpoints | ManualMaps | **P0** |
| 11 | Interface/port selection dialog on link create | Partial — LinkWizard has src/dst interface fields (free text) | LinkWizard | **P0** |
| 12 | Interface-name labels on link ends (cyan pills) | Partial — chips exist mid-link, not anchored at 0.15/0.85 ends; no global hide/show | LinkChip | **P1** |
| 13 | Link styling (straight/bezier/orthogonal, color, dash, width) | Partial — 3 shapes; no color/width/dash controls | ManualMaps | **P1** |
| 14 | Link waypoints / bend editing | Partial — orthogonal-only, **not persisted** (no DB column) | ManualMaps | **P1** |
| 15 | Network/Cloud objects (Bridge/NAT/Internet as link targets) | **Missing** — nodes are device-backed only | — | **P1** |
| 16 | Custom shapes (rect/round-rect/circle, resize, rotate) | **Missing in UI** — table+CRUD exist, 0 rows, no frontend | `manual_map_shapes` | **P1** |
| 17 | Text / sticky annotations (rich) | **Missing in UI** — `text`/`sticky` kinds in schema, unused | `manual_map_shapes` | **P1** |
| 18 | Custom background image (upload + place behind nodes) | **Missing — hard blocker** (no upload, no StaticFiles, no column) | — | **P1** |
| 19 | Z-order (send-to-back/front, z-index) | Partial — z_index on shapes only; nodes/links hardcoded; no UI | shapes table | **P1** |
| 20 | Per-element lock | **Missing** (only global live=read-only) | — | **P1** |
| 21 | Right-click context menus (per target + state) | **Missing** (no context menu anywhere) | — | **P0** |
| 22 | Grid + snap-to-grid + Auto Align | Partial — decorative grid only, no snap, no align | both | **P0** |
| 23 | Alignment / distribution (H/V/Circular) | **Missing** | — | **P1** |
| 24 | Undo / redo | **Missing** | — | **P1** |
| 25 | Keyboard shortcuts (delete, nudge, select-all, dup) | Partial — Esc/Del/C/V/L/0 only | both | **P1** |
| 26 | Minimap (with viewport rect + click-to-pan) | Partial — read-only dots, no viewport rect, non-interactive | MiniMap | **P1** |
| 27 | Lock Lab / Dark-Light theme | Partial — live mode = read-only; no theme toggle | — | **P2** |
| 28 | Auto-layout (hierarchical/grid/radial) | **Have** in Topology, **Missing** in ManualMaps | TopologyPage | **P1** |
| 29 | Export (PNG/SVG/JSON) / print | **Missing** | — | **P2** |
| 30 | Picture-to-node image-map (clickable hotspots) | **Missing** | — | **P2** |
| 31 | Save / persistence | **Have** (instant REST per edit) — but no batch/dirty/undo | ManualMaps | — |
| 32 | Server-side layout persistence for discovered topology | **Missing** — TopologyPage uses localStorage only | TopologyPage | **P1** |

---

## 3. Target UX Spec

The rebuilt editor lives at the manual-maps route, behind a `?v2` / feature flag during rollout. Visual identity stays ZenPlus (our existing palette), but interaction model copies EVE-NG's content-aware, right-click-driven canvas.

### 3.1 Page layout

Four zones (EVE's model, our chrome):
- **Left tool rail** — vertical icon column, ~48px, expands to ~200px on hover to show labels. Buttons set an editor *mode*: Select (V), Connect (C), Add Node, Add Network, Add Shape (rect/round/circle submenu), Add Text, Add Picture, plus a divider then Fit (0), Zoom +/−, Snap toggle, Grid toggle, Lock toggle.
- **Top bar** — map name + breadcrumb, mode badges (Design/Live), search/filter, undo/redo buttons, export menu, NOC fullscreen.
- **Canvas** — React Flow `<ReactFlow>` pane, fills remaining space. Background: our existing dual-layer grid as RF `<Background variant="dots"/lines>`, optional uploaded image layer beneath.
- **Right inspector** — context panel: when a node is selected → device props + icon picker + size + lock; link selected → LinkWizard fields + style; shape/text selected → style panel; nothing selected → map settings (background, theme, snap).
- **Bottom-right minimap** with viewport rectangle + click-to-pan.
- **Transient toasts** top-right (keep current).

### 3.2 "Add an object" palette (EVE parity)

Both entry points, mirroring EVE exactly:
1. **Left-rail buttons** (mode chooser), and
2. **Right-click empty canvas** → context menu: *Add Node, Add Network, Add Shape ▸ (Rectangle / Round rectangle / Circle), Add Text, Add Picture, ──, Auto Align, Paste, Select All, Reset View.*

Object types:
- **Node** — opens "Add Node" modal. Unlike EVE, our nodes are device-backed by default, but we add **two new node classes**: *abstract/placeholder node* (no device, free label+icon) and *network object* (Internet / Cloud / WAN / VLAN-bridge) so users can draw "this connects to the internet" exactly like EVE's Cloud0/NAT. Modal fields: device (searchable, or "Placeholder"/"Network"), label (auto-numbered on batch add: `R` + count → R1..R5), icon (picker), size, count.
- **Network** — shortcut to Add Node with class=network; pick type (Internet/Cloud/Bridge/WAN). Renders as cloud/internet icon, can fan out to many nodes (acts as a hub endpoint).
- **Shape** — Rectangle / Round rectangle / Circle. Drag-to-draw or click-to-place default size. Resizable via corner handles (RF NodeResizer), rotatable via inspector angle field.
- **Text** — click-to-place a text box; double-click to edit inline; rich styling (font size/weight/color/align) in inspector. `sticky` variant = filled note.
- **Picture** — opens upload dialog → uploaded image becomes a positionable, resizable image object (z-index low by default, i.e. behind nodes). Distinct from the map-level background.

### 3.3 Node icons

- Keep the 14 bespoke `network-icons` (router, switch, firewall, server, database, load_balancer, access_point, printer, storage, cloud, internet, workstation, camera, other) and **expand the set** to cover EVE-common gaps: `host/pc`, `laptop`, `phone/ip-phone`, `ups`, `nas`, `wlc`, `vm`, `container`, `iot`, `nat-cloud`, `wan-cloud`. All as inline SVG (`stroke=currentColor`) so they recolor by status.
- **Per-node icon picker** in inspector: searchable grid grouped by category (Routers / Switches / Firewalls / Servers / Endpoints / Clouds / Wireless / Other) — categories are UI grouping only (like EVE's filename convention), the stored value stays a slug.
- **Custom icon upload** (P1→P2): upload SVG/PNG, stored as an asset, referenced by node. Requires the asset subsystem in §4.
- **`auto`** keeps inferring from `device_type` (current `TYPE_TO_ICON`).

### 3.4 Link drawing + interface selection

- **Hover-to-connect (EVE canonical):** hovering a node reveals a connect handle (our orange dot, EVE-style); press-drag a rubber-band to target node; on drop, open the **LinkWizard** (already built).
- **Interface selection dialog:** LinkWizard gains real per-end interface dropdowns populated from `device_interfaces` (we already join this in `links-live`), **filtering out already-consumed interfaces** so each interface maps to ≤1 link. Free-text remains a fallback for placeholder/network nodes. On save, store typed `source_if_index`/`target_if_index` + labels (see §4).
- **Interface labels:** render as cyan rounded pills at edge t≈0.15 and t≈0.85 (near each end, not midpoint) via RF `EdgeLabelRenderer`. Global **Hide/Show interface labels** toggle in top bar (EVE parity). Optional free-text link label at midpoint.
- **Link styles:** right-click link → *Edit Style*: shape (Straight / Bezier / Orthogonal — keep our 3, map to RF edge types), color, width, dash (solid/dashed), arrowheads (none/single/double), plus per-link interface-label visibility. *Edit Quality* is out of scope (we have real live data instead). Right-click link menu: **Edit Style, Hide/Show labels, Suspend (visual), Delete.**
- **Waypoints:** keep our orthogonal bend editing (drag dots, click midpoint to add, right-click to remove) but **persist to a typed `waypoints` column** so it survives reload and is shareable. Extend bend editing to straight/bezier too.
- **Parallel links:** auto-offset multiple links between the same pair (fan-out) instead of overlapping.

### 3.5 Shapes / text styling

- **Shapes:** Rectangle, Round rectangle, Circle. Inspector: fill (color + transparent toggle), stroke color, stroke width, dash, border-radius (round-rect), opacity, rotation, z-index, lock, name.
- **Text:** inline rich editor (font family, size, weight, italic, color, align, background, transparent). Sticky = pre-filled background. Same shared style panel (border/background/transparent/rotate/z-index) as shapes (EVE shares one Edit panel — we mirror that).
- All shapes/text render on a **layer beneath nodes** by default (low z-index), so links/nodes stay clickable — the EVE "background decoration" pattern.

### 3.6 Custom background image

- **Map-level background** (the EVE "Picture as backdrop" use): upload via map settings → stored as an asset → referenced by `manual_maps.background` JSONB `{asset_id, opacity, fit: cover|contain|tile, locked, x, y, scale}`. Rendered as an absolutely-positioned layer inside the RF pane, **synced to the viewport transform** (pans/zooms with the diagram), beneath the grid/nodes. Opacity slider + lock + "fit to canvas."
- Distinct from **Picture object** (a movable/resizable image annotation on the canvas).

### 3.7 Grid / snap / align

- **Visible grid** (keep our dual-layer look) as RF `<Background>`; toggle on/off.
- **Snap-to-grid** while dragging via RF `snapToGrid` + `snapGrid={[gridSize,gridSize]}` (default 16px logical). Toggle in toolbar; persisted per-map in `manual_maps.metadata.snap`.
- **Auto Align** (EVE signature) — context-menu command that snaps all elements to the grid in one shot.
- **Alignment/distribution** on a multi-selection: Align L/R/T/B/center-H/center-V, Distribute H/V, plus EVE's **Circular Align** (arrange selection in a circle around the reference node). Exposed on the selected-group right-click menu.

### 3.8 Multi-select

- **Marquee** — drag on empty canvas (Select mode) draws a selection rect; RF reports nodes inside.
- **Ctrl/Cmd-click** toggles membership; **Shift-click** range/additive; **Ctrl/Cmd+A** select all.
- Selected set moves as a group (delta applied to all), can be aligned/distributed/deleted/locked/duplicated together.

### 3.9 Context menus (the most important pattern to copy)

A single dispatcher that branches on **target type AND state**:
- **Empty canvas:** Add Node/Network/Shape/Text/Picture, Auto Align, Paste, Select All, Reset View, Fit.
- **Node (design):** Edit, Change Icon, Connect from here, Lock/Unlock, Bring to Front/Send to Back, Duplicate, Delete.
- **Node (live):** Open device, Console/SSH (if available), Ping, View interfaces, (movement locked).
- **Link:** Edit Style, Hide/Show interface labels, Reset bends, Suspend (visual), Delete.
- **Network object:** Edit, Delete.
- **Shape/Text:** Edit, Duplicate, Bring to Front/Send to Back, Lock, Rotate, Delete.
- **Multi-select group:** Align ▸, Distribute ▸, Circular Align, Lock all, Group move, Duplicate, Delete selected.

### 3.10 Z-order, lock, zoom/pan/fit/minimap

- **Z-order:** every entity (node/link/shape/text/image) gets an integer `z`. Send-to-Back/Front commands + numeric field in inspector. Nodes default above shapes/images.
- **Lock:** per-element `locked` flag → disables drag/resize, shows a small lock badge; right-click toggles. Optional global "Lock Map" (read-only, EVE Lock-Lab parity) gated to operator role.
- **Zoom:** wheel zoom (no modifier required) cursor-anchored; +/- buttons anchored to center; reset/100% button; **true Fit** = bbox of all elements → set viewport (RF `fitView`).
- **Pan:** space-drag or middle-drag or plain drag on empty canvas (mode-dependent); RF handles it.
- **Minimap:** RF `<MiniMap>` with node-status coloring + **viewport rectangle** + click/drag-to-pan.

---

## 4. Backend Changes

All changes are **additive** (new nullable columns / new tables / new endpoints), matching how migrations 019/020 shipped. **Next migration number is 030** (verified: 029 is the latest in `/opt/zenplus/scripts/`). Migrations are plain `migrate-0NN-*.sql` applied via `psql` (no central runner found). **Per project memory, every migration below must be re-verified with `SHOW TABLES`/`\d` against the live DB after apply — init SQL is not always applied automatically.**

### 4.1 Asset / upload subsystem — **NEW (hard blocker, P1)**

There is **no StaticFiles mount and no upload subsystem anywhere** in `app/main.py` (verified). Background images and custom icons both depend on this.

**Migration 030 — `migrate-030-map-assets.sql`:**
```sql
CREATE TABLE map_assets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  map_id      uuid REFERENCES manual_maps(id) ON DELETE CASCADE,  -- nullable: shared/global icons allowed
  kind        varchar(20) NOT NULL CHECK (kind IN ('background','icon','picture')),
  filename    varchar(255) NOT NULL,
  content_type varchar(80) NOT NULL,
  byte_size   integer NOT NULL,
  storage_path text NOT NULL,         -- relative path under the assets root on disk
  created_by  uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at  timestamptz DEFAULT now()
);
CREATE INDEX idx_map_assets_map ON map_assets(map_id);
```

**App changes (`app/main.py`):**
- Add `app.mount("/assets", StaticFiles(directory=ASSETS_ROOT), name="assets")` (first StaticFiles mount in the app).
- `ASSETS_ROOT` new config key (e.g. `/opt/zenplus/data/map-assets`), created on startup.

**New endpoints (`manual_maps.py`):**
- `POST /maps/{map_id}/assets` (`UploadFile`) — validate content-type (png/jpg/svg/webp), max size (e.g. 5 MB), write to disk under `ASSETS_ROOT/<map_id>/<uuid>.<ext>`, insert row, return `{id, url}` where `url = /assets/<map_id>/<file>`.
- `GET /maps/{map_id}/assets` — list.
- `DELETE /maps/{map_id}/assets/{asset_id}` — delete row + file.
- (`UploadFile` is already used in `snmp.py`/`settings.py`, so the pattern exists — reuse it.)

### 4.2 Map background reference — **column add (P1)**

Store a *typed reference*, never a base64 data-URI (which would bloat every `GET /maps` response).

**Migration 030 (same file):**
```sql
ALTER TABLE manual_maps ADD COLUMN background jsonb DEFAULT '{}'::jsonb;
-- shape: {asset_id, opacity:0-1, fit:'cover'|'contain'|'tile', locked:bool, x, y, scale}
```
- `MapUpdate` Pydantic model gains `background: Optional[dict]`. Validate `asset_id` exists and belongs to the map.

### 4.3 Link waypoints + typed interface binding + style — **column add (P1, partial blocker)**

Verified: `manual_map_links` has **no waypoints anywhere in production** — only a `shape` string hint in metadata. Interface binding is fuzzy free-text (`metadata.src_interface`/`dst_interface`).

**Migration 030:**
```sql
ALTER TABLE manual_map_links
  ADD COLUMN waypoints jsonb DEFAULT '[]'::jsonb,     -- [{x,y}] absolute logical px (or x_pct/y_pct during transition)
  ADD COLUMN source_if_index integer,                 -- typed, survives interface rename
  ADD COLUMN target_if_index integer,
  ADD COLUMN source_label varchar(64),                -- per-end port label shown on canvas
  ADD COLUMN target_label varchar(64),
  ADD COLUMN style jsonb DEFAULT '{}'::jsonb;          -- {color, width, dash, arrows, shape, hide_labels}
```
- `LinkCreate`/`LinkUpdate` gain `waypoints`, `source_if_index`, `target_if_index`, `source_label`, `target_label`, `style`. Keep `metadata.src_interface` populated in parallel one release for back-compat, then deprecate.
- `links-live` interface matching switches to `source_if_index`/`target_if_index` when present (exact match), falling back to the current normalized-name fuzzy match — removes the brittle Cisco-abbrev matching for new links.

### 4.4 Node: icon reference, color, size, z, lock — **column add (P1)**

Verified: `manual_map_nodes.icon` is `varchar(40)` slug; `size_scale` lives only in metadata (no prod node uses metadata). Coordinates are percent.

**Migration 030:**
```sql
ALTER TABLE manual_map_nodes
  ADD COLUMN node_class varchar(20) DEFAULT 'device'   -- 'device'|'placeholder'|'network'
      CHECK (node_class IN ('device','placeholder','network')),
  ADD COLUMN icon_asset_id uuid REFERENCES map_assets(id) ON DELETE SET NULL,  -- custom icon
  ADD COLUMN icon_color varchar(20),
  ADD COLUMN size_scale numeric(4,2) DEFAULT 1.0 CHECK (size_scale BETWEEN 0.5 AND 3.0),
  ADD COLUMN z_index integer DEFAULT 1000,
  ADD COLUMN locked boolean DEFAULT false,
  ADD COLUMN x_px numeric(8,2),   -- absolute logical px (canonical going forward)
  ADD COLUMN y_px numeric(8,2);
ALTER TABLE manual_map_nodes ALTER COLUMN device_id DROP NOT NULL;  -- allow placeholder/network nodes
```
- **`device_id` must become nullable** to support placeholder/network nodes — verify the existing `UNIQUE(map_id, device_id)` index tolerates NULLs (Postgres treats NULLs as distinct, so multiple placeholder nodes are fine — confirm on live DB before relying on it).
- `NodeCreate`/`NodeUpdate` gain `node_class`, `icon_asset_id`, `icon_color`, `size_scale`, `z_index`, `locked`, `x_px`, `y_px`. Relax the `x_pct ge=2 le=98` clamp once pixels are canonical (or widen to 0–100).
- **One-time data migration:** populate `x_px`/`y_px` from existing `x_pct`/`y_pct` × logical-canvas-size in the migration so the 19 production nodes don't jump.

### 4.5 Link z-order/lock — **column add (P2)**

```sql
ALTER TABLE manual_map_links
  ADD COLUMN z_index integer DEFAULT 500,
  ADD COLUMN locked boolean DEFAULT false;
```

### 4.6 Shape styling depth — **column add (P1)**

Verified: `manual_map_shapes` exists with `fill`/`stroke`/`z_index` and full CRUD endpoints, **but 0 rows** — frontend never creates shapes. Schema needs richer styling to match EVE's shape/text editor.

**Migration 030:**
```sql
ALTER TABLE manual_map_shapes
  ADD COLUMN stroke_width numeric(5,2) DEFAULT 1,
  ADD COLUMN opacity numeric(4,3) DEFAULT 1.0,
  ADD COLUMN rotation numeric(6,2) DEFAULT 0,
  ADD COLUMN border_radius numeric(5,2) DEFAULT 0,
  ADD COLUMN dash varchar(20),                       -- 'solid'|'dashed'|'dotted'
  ADD COLUMN font_size numeric(5,2),
  ADD COLUMN font_weight varchar(10),
  ADD COLUMN text_align varchar(10),
  ADD COLUMN text_color varchar(20),
  ADD COLUMN image_asset_id uuid REFERENCES map_assets(id) ON DELETE SET NULL,  -- for kind='image'/'picture'
  ADD COLUMN locked boolean DEFAULT false;
```
- `ShapeCreate`/`ShapeUpdate` gain all the above. `kind` already supports `image`; now it has a real source via `image_asset_id`.

### 4.7 ORM models — **NEW (P2, addresses memory concern)**

Verified: **no SQLAlchemy ORM models exist for any map/topology table** — all raw `sqlalchemy.text()`. Add ORM models for `manual_maps`/`_nodes`/`_links`/`_shapes`/`map_assets` so schema drift is caught at import time, not query time. This directly addresses the project-memory "verify migrations first" pain — but is not parity-blocking, so it's P2.

### 4.8 Topology layout persistence — **NEW endpoint (P1)**

Verified: `TopologyPage` persists node positions/link shapes/waypoints to **localStorage only** (per-browser, not shareable). Add a `topology_layout` table (or reuse `manual_maps.metadata` keyed by a system map) + `GET/PUT /topology/layout` so discovered-topology positioning is server-side and multi-user. Lower priority than the manual-maps rebuild.

### 4.9 Verification gate (run after every migration)

```bash
PGPASSWORD=... psql -h 127.0.0.1 -U zenplus -d zenplus -c "\d manual_map_links"
# confirm: waypoints, source_if_index, style columns present
PGPASSWORD=... psql ... -c "\d manual_map_nodes"   # confirm x_px, node_class, locked, device_id nullable
PGPASSWORD=... psql ... -c "\dt map_assets"        # confirm table exists
```
Do **not** build frontend against a column until `\d` shows it on the live DB.

---

## 5. Frontend Implementation Plan

### 5.1 Component breakdown (target)

Replace the monolithic `ManualMapsPage.tsx` (2979 lines) with:

```
pages/MapEditorPage.tsx                 // route shell, mode/state, React Query wiring, feature flag
canvas/MapCanvas.tsx                    // <ReactFlow> host: viewport, background, minimap, controls
canvas/nodes/DeviceNode.tsx             // custom RF node (port our NodeCard + status rings/pulse)
canvas/nodes/NetworkNode.tsx            // cloud/internet/bridge endpoint
canvas/nodes/ShapeNode.tsx              // rect/round/circle w/ NodeResizer
canvas/nodes/TextNode.tsx               // inline-editable text / sticky
canvas/nodes/ImageNode.tsx             // picture object
canvas/edges/NetworkEdge.tsx            // custom edge: straight/bezier/orthogonal + waypoints + port pills + live flow
canvas/BackgroundImageLayer.tsx         // map-level backdrop synced to RF transform
canvas/ContextMenu.tsx                  // single dispatcher (target type + design/live state)
canvas/Toolbar.tsx                      // left rail (mode chooser) + top bar
panels/InspectorPanel.tsx               // routes to Node/Link/Shape/MapSettings inspectors
panels/IconPicker.tsx                   // categorized grid + custom upload
panels/LinkWizard.tsx                   // (port existing) + interface dropdowns from device_interfaces
panels/LiveLinkPanel.tsx                // (port existing live overlay)
state/useMapStore.ts                    // Zustand+immer: entities, selection set, command/undo stack
state/commands.ts                       // typed reversible commands (move/add/delete/align/style/zorder)
hooks/useMapData.ts                     // React Query: map, links-live, suggested-links, devices, assets
hooks/useSnapAndAlign.ts                // snap, align, distribute, circular-align, auto-align
icons/network-icons/                    // expand existing set
```

Keep TopologyPage as-is until WP7; then converge it onto `MapCanvas` with auto-layout (dagre/elk) added.

### 5.2 Ordered work packages (each independently testable, P0 first)

**WP0 — React Flow substrate + migration (P0, foundational).**
- Add `@xyflow/react`. Build `MapCanvas` rendering existing nodes/links as RF custom nodes/edges, behind `?v2` flag (old page stays default).
- Add migration 030's `x_px`/`y_px` + one-time percent→pixel backfill; render from pixels; drop `preserveAspectRatio="none"`.
- Port live-status overlay into `DeviceNode`/`NetworkEdge`.
- *Ship criteria:* the 3 production maps render identically (no geometry distortion), live status still works, pan/zoom/fit work via RF.

**WP1 — Pan/zoom/fit/minimap parity (P0).**
- Wheel zoom (no modifier) cursor-anchored, anchored +/- buttons, true `fitView`, RF `<MiniMap>` with viewport rect + click-to-pan.
- *Ship criteria:* zoom never drifts; Fit centers all elements; minimap navigates.

**WP2 — Selection + group move + marquee + keyboard (P0).**
- Marquee, Ctrl/Shift-click, Ctrl+A, group drag (delta to all), Delete/Backspace, arrow-nudge, Esc.
- Replace single `selectedNodeId`/`selectedLinkId` with a selection set in `useMapStore`.
- *Ship criteria:* select 3 nodes, drag together, relative spacing preserved, batch-delete works.

**WP3 — Context menus (P0).**
- `ContextMenu` dispatcher branching on target + design/live state (all menus from §3.9).
- *Ship criteria:* right-click empty/node(design)/node(live)/link/group each show correct items and actions fire.

**WP4 — Grid + snap + Auto Align + alignment (P0/P1).**
- RF `snapToGrid`/`snapGrid`, grid toggle, Auto Align command, Align/Distribute/Circular on selection.
- Persist snap setting in `manual_maps.metadata`.
- *Ship criteria:* dragging snaps; Auto Align tidies all; align/distribute math correct.

**WP5 — Undo/redo + command stack (P1).**
- Wrap every mutation in a typed command (move/add/delete/style/align/zorder); Ctrl+Z/Ctrl+Y; batch a drag into one command.
- Keep instant REST persistence but make it reversible (undo issues the inverse mutation).
- *Ship criteria:* every editor action is undoable/redoable, server state matches after each.

**WP6 — Links: interface selection + labels + waypoints + styling (P1).**
- LinkWizard interface dropdowns from `device_interfaces` (filter consumed); persist `source_if_index`/`target_if_index`/labels.
- Cyan port pills at edge ends (0.15/0.85), global hide/show toggle.
- Persist waypoints to new column; Edit Style menu (color/width/dash/arrows/shape).
- Parallel-link fan-out offset.
- *Ship criteria:* create link → pick real interfaces → labels render at ends → live stats bind by if_index → bends persist across reload.

**WP7 — Shapes + text + z-order + lock (P1).**
- `ShapeNode`/`TextNode`/`ImageNode` with NodeResizer + rotation; style panel; z-order send-to-back/front; per-element lock badge.
- Wire to the **already-existing** shapes CRUD (extend with new style columns).
- *Ship criteria:* draw rect/circle/text, style/rotate/resize/lock, layer behind nodes, persists.

**WP8 — Background image + custom icons (P1).**
- Asset upload subsystem (WP depends on §4.1 migration + StaticFiles mount).
- Map background layer (opacity/fit/lock); IconPicker custom upload.
- *Ship criteria:* upload a floorplan → set as backdrop → pans/zooms with map; upload custom node icon → assigned to node.

**WP9 — Auto-layout for manual maps + Network objects (P1).**
- Add dagre/elk hierarchical + grid/radial (port Topology's `layoutNodes`); Add Network/Cloud/Internet node class.
- *Ship criteria:* "Arrange ▸ Hierarchical" lays out a map; internet cloud connects to multiple nodes.

**WP10 — Export + theme + converge TopologyPage (P2).**
- PNG/SVG/JSON export; dark/light canvas theme; migrate TopologyPage onto `MapCanvas` with server-side layout persistence (§4.8).

**WP11 — Cutover.** Flip `?v2` to default; keep old page one release behind `?v1`; then delete.

---

## 6. Risks & Verification

### 6.1 Top risks

1. **Coordinate migration breaks the 3 live maps.** Mitigation: backfill `x_px`/`y_px` in the migration; keep `x_pct` in sync one release; verify the 19 nodes render in identical relative positions before/after. **This is the highest risk** — a visual diff of production maps is mandatory before cutover.
2. **`device_id DROP NOT NULL` + `UNIQUE(map_id, device_id)`.** Confirm on live DB that NULLs are treated as distinct (they are in Postgres) so multiple placeholder/network nodes don't collide. Test by inserting two placeholder nodes.
3. **Background data-URI bloat.** Enforce asset-reference-only; reject base64 in `background` JSONB. Cap upload size server-side.
4. **React Flow performance at scale.** We're at 19 nodes now; RF handles hundreds fine, but custom node re-renders on live-status polling could thrash — memoize node components, isolate live-status into a thin overlay store.
5. **Migration not applied to live DB** (the standing project-memory hazard). Gate every WP on `\d`-verified columns.
6. **Two link models diverge** (`manual_map_links` vs `topology_links`). Decide `manual_map_links` is canonical for the visual editor; only add EVE features there.

### 6.2 Playwright verification per milestone

Run against the live app (web login `admin`/`admin123` per local secrets). Resize browser to a non-square viewport to catch aspect distortion.

- **WP0:** Navigate to `?v2` map; screenshot; assert node count/positions match `?v1` (geometry diff). Resize to 1600×500 (wide) and confirm orthogonal links stay right-angled (the bug `preserveAspectRatio="none"` causes today).
- **WP1:** `browser_evaluate` to read RF viewport; wheel-zoom over a node, assert the node stays under cursor (anchored zoom); click Fit, assert bbox centered; click minimap, assert pan.
- **WP2:** Marquee-drag over 3 nodes (`browser_drag`), then drag the group; assert all 3 moved by equal delta (read positions before/after). Press Delete; assert removed + server `GET /maps/:id` reflects it.
- **WP3:** Right-click empty/node/link; `browser_snapshot` each menu; assert design vs live menus differ (toggle live with `L`).
- **WP4:** Enable snap; drag a node; assert final position is grid-aligned. Run Auto Align; assert all nodes on grid. Select 3 → Align Left; assert equal x.
- **WP5:** Move a node, Ctrl+Z; assert position reverts AND `GET /maps/:id` shows original. Redo; assert reapplied.
- **WP6:** Draw a link; assert interface dropdown lists real `device_interfaces` and excludes consumed ones; save; assert two cyan pills at ends; reload; assert waypoints persist; check `links-live` binds by `if_index`.
- **WP7:** Add a rectangle + text; resize/rotate/lock; send-to-back; reload; assert persisted in `manual_map_shapes` (`SELECT count(*)` now > 0). Assert locked shape can't be dragged.
- **WP8:** Upload a PNG via `browser_file_upload`; assert `POST /maps/:id/assets` returns a `/assets/...` URL that loads (network request 200); set as background; pan/zoom and assert it tracks the transform.
- **WP9:** Click Arrange→Hierarchical; assert no overlapping nodes (bbox checks). Add Internet node; connect 3 devices; assert fan-out.
- **Regression each WP:** assert live-status (pulse on a down node), NetFlow throughput chip, NOC fullscreen, and suggested-links still function — these are our differentiators and must not regress.

---

### Key file references
- Editors: `/opt/zenplus/dashboard/src/pages/ManualMapsPage.tsx` (2979 lines), `/opt/zenplus/dashboard/src/pages/TopologyPage.tsx` (1726 lines)
- Icons: `/opt/zenplus/dashboard/src/components/network-icons/index.tsx` (14 slugs)
- Backend routers: `/opt/zenplus/server/app/api/v1/manual_maps.py`, `/opt/zenplus/server/app/api/v1/topology.py`
- App entry (no StaticFiles mount today): `/opt/zenplus/server/app/main.py`
- Migrations dir: `/opt/zenplus/scripts/migrate-0NN-*.sql` — **next number is 030**; existing map migrations: `migrate-015-manual-maps.sql`, `migrate-019-manual-map-shapes.sql`, `migrate-020-manual-map-styling.sql`
- DB (native host, not docker): `postgresql://zenplus@127.0.0.1:5432/zenplus`

**Verified live:** all 4 `manual_map*` tables + `topology_*` tables exist with columns exactly as inventoried; `manual_map_shapes` has the 9-kind CHECK and full CRUD endpoints but **0 rows** (annotations unbuilt in UI); no node/map uses `metadata`; links store shape/interface in metadata JSONB with **no waypoints**; **no StaticFiles mount or upload subsystem exists**; production data is small (3 maps / 19 nodes / 13 links) so the percent→pixel migration is low-risk if backfilled correctly.