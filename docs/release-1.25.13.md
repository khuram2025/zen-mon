# ZenPlus 1.25.13

Device groups you can use everywhere, and custom fields.

- Network -> Groups & Fields: groups are static (you pick the members) or
  dynamic (members are every device or server that matches a condition, kept
  up to date within a minute). Groups nest up to five levels and include
  their subgroups' members. The editor previews who matches while you type.
- Servers can be group members, and a device can be in more groups than its
  own. A device's own group is always a static group.
- Alert rules, maintenance windows, syslog, trap and Device Tracker rules
  scoped to a group now cover every member: the device's own group, devices
  and servers added by hand, dynamic matches and subgroups. Rules on existing
  groups cover the same devices as before until you add members.
- Each group shows what uses it (alert rules, maintenance, sensors, discovery
  profiles and rules, subgroups); a group in use cannot be deleted.
- Custom fields: typed fields (text, number, date, choice, yes/no) for devices
  and servers, edited on the device's Configuration tab, optionally shown as
  Devices list columns, usable in dynamic group conditions and in device
  status and SNMP alert messages as {custom.<key>}. Import values from CSV
  with a dry run first.
- New permission "Manage groups and policies" (admin, operator, network team).

Migration 192. Includes 1.25.12. Appliance-only package; minimum upgrade
base 1.24.36; signed Windows agent catalog preserved. Details:
docs/device-groups.md.
