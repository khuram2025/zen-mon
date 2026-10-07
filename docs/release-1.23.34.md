# ZenPlus 1.23.34 — Role-based navigation, team roles and role visibility scope

Users with a narrow custom role no longer see menus their role does not allow.
Previously most sidebar entries (Devices, Servers, Applications, Alerting, NCCM,
Device Tracker, Reports, …) were shown to every role and then failed with
"access denied" errors.

- **Menus follow permissions.** Every sidebar entry requires the permission its
  page needs. Opening a page outside the role by URL shows a clear explanation,
  and each role lands on its own start page after sign-in.
- **Application team separated from Servers.** New `servers.view` /
  `servers.manage` permissions govern servers, agents, agent policies and
  baselines; `apm.*` now covers APM only. Every existing role that had APM
  access keeps matching Servers access, so nothing changes on upgrade.
- **Team roles.** Built-in Network Team, Systems Team, Application Team and
  Security Team roles, offered as starting points for custom roles.
- **Role visibility scope.** A role can be limited to tags (for example
  `network` or `riyadh`): its members see only resources carrying one of those
  tags, plus any scope tags set on the user. Tag-scoped managers cannot widen
  access beyond their own tags.
- **New role editor.** Four steps — Details, Access (per-module levels with a
  live menu preview), Visibility (tags with resource counts) and Review.
- Fixed a crash when opening the Edit User dialog.

Schema: `migrate-154-role-scope-and-server-permissions.sql` (adds
`roles.scope_tags`; copies `apm.*` grants onto `servers.*`). Appliance-only,
minimum prerequisite 1.23.9.
