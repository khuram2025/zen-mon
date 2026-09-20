# ZenPlus 1.23.15

Services list availability now agrees with the service detail SLA: only confirmed
incidents affect availability, isolated failed probes remain diagnostic evidence,
and monitoring gaps remain unknown. Daily uptime bars use the same calculation and
the browser's calendar timezone. Availability percentages use two decimal places.
Batched reports respect user visibility scope and run outside the API event loop.

Server pages now present a simple agent download dialog with direct MSI and EXE
links to the latest official ZenPlus Windows agent installers. Downloads avoid
stale cached installer aliases.

This is a signed appliance-only update. Windows installers remain separate and are
not bundled or replaced by this release. Requires appliance version 1.23.9 or later.
No new database migrations are introduced; the package retains the complete
migration set for supported upgrade paths.

Validation includes regression coverage for confirmed outages, isolated failures,
missing data, retained rollups, visibility scope and local/DST day boundaries;
strict dashboard compilation and route smoke checks; and live comparisons of all
10 appliance services over identical 24-hour windows.
