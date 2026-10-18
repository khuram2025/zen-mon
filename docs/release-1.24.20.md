# ZenPlus 1.24.20 — Device page: context and personalization

The final part of the device page redesign (see `docs/device-detail-page.md`).

- **Vendor tabs:** template groups are grouped into tabs such as VPN & SD-WAN, Wireless, Pools & virtual servers, Sessions and VDOMs. Templates can declare `"tab"` on a group. Issues and health groups open the tab that shows them.
- **Related panel** on Summary: the managing controller and managed devices, topology dependencies, maps that show the device (opening the map centred on it), and devices whose confirmed status changed within two minutes of this device's last changes. Every entry respects tag scope.
- **Owner and notes** for each device (Configuration tab; the owner also appears in the header). Editing needs `devices.manage`.
- **Layouts follow you:** device page layouts are saved on the server per user, device (or vendor/type template) and tab, so they are the same in every browser. Existing browser layouts are uploaded once.
- **Alert titles name their cause:** device status alerts read "Packet loss 33% (limit 10%) — fw-01 (10.0.0.1)", "Latency 412 ms (limit 100 ms) — …", "No response — …" or "Responding again — …". Rule matching, thresholds and the status policy are unchanged; only the wording of new alerts changes.
- **Performance:** compare with the previous period (dashed overlay and the change of the average) and export the charts as PNG.

Migration 165 is additive: `devices.owner`, `devices.notes` and the `device_widget_layouts` table. Positions saved in the old "Vendor metrics" layout do not move into the new tabs, which start from the standard layout.
