# ZenPlus 1.25.12

Discovery rules now also work on the devices and servers you already monitor.

- Discovery -> Rules -> Run on inventory (or Devices -> select -> Run discovery
  rules, or a device's Automation card): choose the rules and the devices or
  servers, preview exactly what would change, then apply. Apply refuses if
  anything changed since the preview; running again changes nothing.
- Values set by rules are remembered with the value they replaced. A value
  changed by hand later is kept by "set once" rules (the default) and put back
  by "enforce" rules. Tags are only ever added; nothing is deleted or turned
  off.
- Undo a run from the run history: values still as the rule set them are
  restored, the rest are listed. Undo never deletes a device.
- Rules can run automatically on devices and servers when they are added
  (within a minute; adding a device never waits for the rules).
- Simulating mode: a rule records what it would do without changing anything.
- Device Configuration tab and server Agent settings show an Automation card:
  which rule set what, hand edits that were kept, rules that match now.
- Rules page: a conflicts panel (rules overridden by higher rules, rules that
  match nothing or never win).
- New facts (tags, group, template, location, owner, status; server OS type,
  environment, collection mode) and actions (owner, server environment, turn
  ping on).

The engine stays off until turned on under Discovery -> Rules; rules for newly
added devices only see devices added after this upgrade. Migration 191.
Appliance-only package; minimum upgrade base 1.24.36; signed Windows agent
catalog preserved. Details: docs/discovery-rule-engine.md.
