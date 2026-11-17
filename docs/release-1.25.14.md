# ZenPlus 1.25.14

Discovery rules keep your inventory organised as it changes, and an update
check that stopped some appliances from upgrading is fixed.

- New rule trigger "when the asset changes": within a minute of a device or
  server changing (firmware, vendor, tags, group, location and so on), the
  rules are evaluated again for that asset only. Up/down status changes never
  trigger it.
- "When an asset stops matching": each rule keeps its values (the default) or
  puts back what it replaced, and only where the value is still the one the
  rule set. Turning a rule off or deleting it always keeps its values.
- Approval for large changes: an automatic pass that would make more than the
  configured number of changes (default 100) applies nothing and waits on the
  Rules page. Approve applies it with every value checked again; Reject keeps
  it from being proposed until an asset or a rule changes.
- Controller-managed switches and access points can be organised by rules
  (group, location, owner, tags, map). Rules never poll or license them.
- New actions: add to a network map, and set the uplink dependency parent.
  Undo removes the map node again.
- Rules can be exported and imported as JSON (names only, no ids or secrets);
  imported rules arrive as drafts.
- Starter rules for Fortinet, Cisco, F5, MikroTik, Aruba and Juniper, plus
  printer and IP-phone examples, can be added as drafts from the Rules page.
  Nothing is created by the upgrade.
- Fixed: updates failed at the security step with "Missing privilege
  separation directory: /run/sshd" on appliances where nobody had logged in
  over SSH since the last restart. The update now checks the SSH
  configuration correctly on those appliances; an invalid SSH configuration
  still stops the update.

The engine stays off until turned on under Discovery -> Rules. Migration 194.
Includes 1.25.13. Appliance-only package; minimum upgrade base 1.24.36; signed
Windows agent catalog preserved. Details: docs/discovery-rule-engine.md.
