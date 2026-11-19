# ZenPlus 1.25.5

Network maps are redesigned for NOC walls, engineer desktops and large NOC
monitors.

New **maps overview** (Network Maps → All maps): every map as a card with its
health bar, down / warning / up counts and active alerts, maps with problems
first, and one click to edit, watch live or open it on a wall.

New **NOC wall mode** (the Wall button on any map): a dark, high-contrast live
view without menus that refreshes on its own, re-fits when the screen changes
and scales its text for 4K video walls while still looking right on a desktop
monitor. It can rotate through several maps (All maps → NOC wall rotation)
and lists what needs action beside the map. It no longer depends on the
browser's full-screen function, which some browsers refused ("Fullscreen is
not available in this browser").

New **sites**: one tile can stand for a whole branch, building or hall: the
devices at a location, the nodes of the site's own map and/or devices picked
by hand. The tile shows their combined health, down and warning counts and
alerts, opens a grid of every device on demand (fast for large sites) and
drills into the site's map with a breadcrumb back. Selected devices can be
grouped into a site, optionally moving them to a new site map.

The **live view** opens with summary tiles (health, devices, links, traffic
and the busiest link, sites, alerts). Clicking a device opens an inspector
with its response time, uptime, CPU, memory, every link with throughput and
utilisation, and its newest alerts. Device tiles are clearer, with a status
pip and an alert badge; CPU and memory sit inside the label instead of
overlapping cables; faster links are drawn heavier; the legend no longer
covers the map, and a hovered zone no longer hides the devices inside it.

Devices can use **custom icon images** and every map can have a **background
image** (floor plan, rack photo, region outline), stored with the map.

No migrations. Appliance-only release requiring the signed 1.24.36 bridge;
existing Windows agent packages are preserved.
