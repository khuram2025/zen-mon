# ZenPlus 1.24.28 — Professional NOC look: readable type, display density and wall mode

Every page gets a new type scale, colour palette and chart style. They are designed for NOC wall screens as well as for engineers' desks. No data, API or permission changes.

**Readable everywhere**
- **Larger, consistent text.** The UI used about 2,200 hand-set pixel font sizes, mostly between 9 and 11px. Text now follows one scale: 11px minimum for labels and axis ticks, 12–13px for table text and body copy, 28px for stat figures.
- **Fonts work offline.** Inter and JetBrains Mono are now part of the dashboard. Before, Inter came from an internet CDN, so appliances without internet access fell back to system fonts. Nothing in the UI loads from the internet any more, and a new CI test (`test-offline-assets`) keeps it that way.
- Tables use tabular numbers and non-wrapping headers. IP addresses and IDs use a monospace font with a slashed zero.

**Display density and NOC wall mode**
- A new **Display** menu in the header (also in Settings → Appearance) has four densities: **Compact** (laptops), **Comfortable** (default), **Large** (1440p and 4K monitors) and **NOC wall** (video walls). Text, spacing, the sidebar and chart text scale together. The choice is saved in your browser only.
- **NOC wall mode** shows the page full screen without the sidebar and header. Press Esc to leave. A wall screen can be set up by URL alone: `https://<appliance>/?density=wall&kiosk=1`.

**Colour and charts**
- New dark (NOC) and light palettes. All text meets 4.5:1 contrast, status colours are used only for state, and dark panels are separated by borders instead of shadows.
- Charts use one look: Inter in charts, readable axis text that grows with the density, a hairline grid, and an eight-colour series palette tested for colour-blind readability in both themes.
- Stat tiles look the same on every dashboard (Network Operations, team dashboards, Servers, APM, Inventory). The coloured top line and icon show the tile's status instead of decorative gradients.

**Layout fixes**
- The Network Operations stat tiles go from 4 to 7 per row as the screen widens, so labels no longer wrap on laptop screens.
- Devices: below 1536px the summary cards move under the table, so the table keeps its width. Group and Location cells are shortened (the full text is in the tooltip).
- The header is a little taller with larger controls, and the breadcrumbs use larger text.
- Fixed: users with the dark theme no longer see the light theme flash on every page load.

Schema: no migration.

Appliance-only release (dashboard only), minimum version 1.23.9.
