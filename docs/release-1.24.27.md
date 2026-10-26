# ZenPlus 1.24.27 — Redesigned, reorganized sidebar

The main menu is regrouped by function and redrawn. Every page keeps its address and its permission, so roles see exactly the pages they saw before; only where a page sits in the menu has changed.

**New menu layout**
- **Three bands:** *Operations* (Dashboards, Alerts & Events), *Monitoring* (Network, Device Tracker, Servers, Applications, Uptime & SLA) and *Management* (Inventory & Compliance, Reports, Administration).
- **Alerts & Events** moved up to sit right under the dashboards. Active Alerts, SNMP Traps and Network Syslog come first; Alert Rules, Escalation & On-call, Notification Channels and Delivery Gateways are under *Configuration*.
- **Network** replaces the old catch-all *Monitoring* group: Devices, Link Utilization, NetFlow, NetPath and Network Maps, with **Config Manager (NCCM)** and Discovery under *Configuration*. The one-row NCCM group is gone.
- **Uptime & SLA** is a new group for Service Checks (HTTP, TCP and DNS probes) and Availability & SLA.
- Setup pages are listed last in each group under a *Configuration* heading (Agent Fleet and Agent Policies, APM Settings, Tracker Settings, Inventory Settings).
- Renamed: Dashboard → Network Operations, Services (checks) → Service Checks, Availability → Availability & SLA, Channels → Notification Channels, Gateways → Delivery Gateways.

**New sidebar features**
- **Starred pages:** hover a menu row and click the star. Starred pages are listed at the top of the menu, and in a Starred flyout on the collapsed rail. They are saved in your browser.
- **Menu search:** press `/`. Results list every page you can open, with the group it lives in; use the arrow keys and Enter to open one.
- **`[`** collapses or expands the sidebar.
- Group headings now show their icons; the open group, the current page and the live alert count are easier to see. The collapsed rail shows readable labels and separates the three bands.
- The menu scrolls the current page into view.
- Dark theme: menu headings and muted text now meet the 4.5:1 contrast ratio.

Schema: no migration.

Appliance-only release (dashboard only), minimum version 1.23.9.
