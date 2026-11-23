# ZenPlus 1.25.27

Discovery rules can place servers, put devices and servers in groups, test
and set custom fields, and run on a schedule of their own.

Discovery rules
- Site: a rule sets a server's site (devices keep their Location).
- Add to group: a rule makes a device or server a member of a static group,
  like a member added by hand, so alert scopes, maintenance windows and
  reports include it. It never removes a member an operator added.
- Custom fields: rules test any custom field from Groups & Fields (for
  example a Role of Core / Edge / Access) and set one or more fields. For
  each field the first matching rule wins; hand edits, enforce, revert and
  undo work as for every other value. A field a rule uses cannot be deleted.
- Own schedule: a rule with the scheduled trigger runs on the shared
  schedule, or on its own: once at a date and time, every 1 to 12 hours, on
  chosen weekdays (daily, weekdays, ...) or monthly, at a local time in a
  time zone. Each slot runs once; a slot missed while the rule or the engine
  was off is not caught up.
- The rules list and the scheduled-rules bar show each rule's own schedule
  and next run.

Notes for the upgrade
- Existing rules are unchanged and run exactly as before.
- Discovery rules stay off until turned on under Discovery -> Rules.

Migration 213. Appliance-only package; minimum upgrade base 1.24.36; signed
Windows agent catalog preserved. Details: PR #197.
