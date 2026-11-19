# ZenPlus 1.25.6

The new **Network Maps → All maps** page (`/maps`) answered "403 Forbidden"
when it was opened from a bookmark or a link, or reloaded. The world-map data
used by the RUM country map was published under the same address; it now lives
under `/geo/`, and a build check stops a static folder from ever shadowing a
page again.

This release also carries everything in 1.25.4 (Synthetics scenario detail
pages, time range, per-step timing and fixes) and 1.25.5 (network maps
redesign: maps overview, NOC wall mode and rotation, sites with drill-down,
live summary tiles and device inspector, custom icons and background images).
See docs/release-1.25.4.md and docs/release-1.25.5.md.

No migrations. Appliance-only release requiring the signed 1.24.36 bridge;
existing Windows agent packages are preserved.
