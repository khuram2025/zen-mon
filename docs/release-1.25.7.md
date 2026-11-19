# ZenPlus 1.25.7

Site groups in Live and NOC wall modes now open their related detail map with
one click on the tile, title or health ring. The tile is keyboard accessible
with Enter and Space. Existing Open, Devices/Hide and member-device actions
stay independent, and Design-mode editing does not navigate. A missing or
inaccessible map remains inert; navigation uses the scope-filtered sites-live
response rather than an unchecked map ID from the shape metadata.

The change is confined to the SiteTile component. It does not change map
membership, topology, API behavior, permissions, monitoring or notification
configuration. No migrations. Appliance-only package; preserve the signed
Windows agent catalog and minimum upgrade base 1.24.36.

Validation: rendered React/jsdom event regressions (tile/title/ring, one
navigation, keyboard, nested controls, members, Design mode, unavailable maps),
strict dashboard build, 50-route smoke, map export and page-access contracts.
The event regressions run in the required dashboard CI job. Release acceptance
and installed-appliance results are recorded separately after verification.

Recovery uses the updater's verified pre-update backup. The source change can
also be reverted without changing database records or map configuration.
