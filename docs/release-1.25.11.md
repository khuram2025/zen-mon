# ZenPlus 1.25.11

Discovery rules: classify what discovery scans find with ordered rules, in the
style of OpManager's Discovery Rule Engine but with preview, priority and
history.

- Discovery -> Rules: rules with nested All/Any/None conditions over the scan's
  facts (sysObjectID subtree, IP subnet or range, vendor, model, sysDescr
  regex, open ports, OS version comparisons, credential status, ...) and
  actions: auto-import, keep for review or ignore; device group, monitoring
  template, tags, device type, location, SNMP credential, ping and SNMP
  intervals. Rules run in order; the first matching rule sets each value and
  tags merge.
- Preview any rule, saved or not, against a past scan or typed-in device
  details before activating it.
- Scan Results: Rule and Decision columns with the reason, and Re-evaluate
  rules (annotate only, or apply).
- The profile's import mode now has its meaning: Review only labels results,
  Auto-match lets rules import and ignore, Ignore-match lets rules ignore.
- Auto-import uses the Import dialog's path and respects the license node
  limit, a per-scan cap and an optional reserve; existing devices are never
  changed.

The engine is off after the upgrade; nothing changes until an administrator
turns it on under Discovery -> Rules. New permission discovery.manage_rules
(administrators and the Network Team role).

Migration 190 adds the discovery rule tables. Builds on 1.25.10 (Device
Templates, migration 189); an appliance updating straight from 1.25.9 gets both.
Appliance-only package; minimum upgrade base 1.24.36; signed Windows agent
catalog preserved. Details: docs/discovery-rule-engine.md.
