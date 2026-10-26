# ZenPlus 1.24.29 — Fixes from the 1.24.28 real-data review

Found while checking every page of 1.24.28 on a live appliance. All 70 pages loaded without errors or sideways scrolling.

- **Sparklines draw correctly again.** The Traffic Now and Availability sparklines on Network Operations were drawn black (filled black in dark mode). Theme colours are now passed as plain hex values, and each sparkline gets its own valid gradient id.
- **NOC wall mode keeps stat tiles readable.** At the Wall density, the 6- and 7-tile stat rows now wrap to 3–4 per row, so labels such as "Devices Online" and "29 down · 4 degraded · 1 unknown" fit. The Large density does the same on screens narrower than 2200px.
- The small captions inside the dashboard's availability rings ("19/52 up") were 7px. They are now the 11px minimum used everywhere else.

Schema: no migration.

Appliance-only release (dashboard only), minimum version 1.23.9.
