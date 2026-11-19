# ZenPlus 1.25.19

Discovery rules and dynamic groups can test what a server's agent reports, and
server alert rules can be limited to a tag or a device group.

- New conditions for servers in discovery rules and dynamic device groups:
  Windows services, running Windows services, installed software, software
  publishers and watched processes. Each can be tested for "has", "has one
  containing", "has none", "has none containing", "is reported" and "is not
  reported". A service matches by its service name or display name.
- A server that reports nothing for a condition (no agent, a Linux server for
  services, inventory not collected yet) is "not reported": every other test
  fails, so a server is never treated as lacking something only because
  nothing is known about it.
- Processes: the agent reports only its busiest processes plus the ones on the
  agent policy's watch list, so only watched processes can be tested; add a
  process to the policy's watch list to use it in a rule.
- Changed-trigger rules re-evaluate a server when its services, software
  inventory or watched processes change, only while a rule uses them. Only
  the names the conditions use are read, so large inventories stay cheap.
- Server alert rules (the host metric and service rules) can be
  limited to servers with a tag and/or servers in a device group (subgroups
  included). The rule editor, the alert rules list and the server's Alerts tab
  show the scope ("Servers in SQL servers with tag prod"). A server that
  leaves the scope has the rule's open incident resolved without a recovery
  notice.

Example: a dynamic group "SQL servers" (running service contains "MSSQL"), and
a disk rule limited to that group.

No migration. Includes 1.25.18. Appliance-only package; minimum upgrade base
1.24.36; signed Windows agent catalog preserved. Details:
docs/discovery-rule-engine.md (Server inventory conditions),
docs/device-groups.md (server alert rule scope).
