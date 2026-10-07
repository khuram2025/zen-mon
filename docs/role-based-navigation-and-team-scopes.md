# Role-based navigation, team roles and role visibility scope

## Problem

A user assigned a narrow custom role still saw most of the sidebar. Only
NetFlow, NetPath and a few Settings tabs declared a permission in
`dashboard/src/components/layout/navigation.ts`; every other entry (Devices,
Servers, Applications, Alerting, NCCM, Device Tracker, Discovery, Reports, …)
was shown to every role. The API refused the underlying calls (route policy),
so those pages rendered as a wall of failed requests.

Two related gaps:

* Servers, agents, agent policies and baselines were governed by `apm.view` /
  `apm.manage`, so an Application team could not be separated from a Systems
  team.
* Visibility scope (tag-based resource filtering) existed only per user, so a
  team role could not be limited to its team's resources.

## Design

### Navigation follows permissions

* Every sidebar entry names the permission(s) its page needs — the same ones
  the server's route policy checks (`permission` = any of, `requireAll` = all
  of). Children also need their parent's permission. A parent with no visible
  child, and an empty group, are hidden.
* The rules live in `navAccess.ts` (no React/icon imports), used by the
  sidebar, the route guard and the role editor's menu preview.
* `RouteGate` (in `Layout`) applies the same rules to direct URLs: a page the
  role cannot open shows an explanation and the permission it needs, and `/`
  redirects to the role's first permitted page (each team lands on its own
  dashboard). Settings hides its own tabs, so only the item is route-gated.
* Header widgets no longer poll endpoints the role cannot read (alerts,
  update status, device search).
* `scripts/test-nav-access.mjs` fails CI when an entry has no permission, names
  a permission the server does not know, or when a built-in team role (parsed
  from `server/app/core/permissions.py`) would see another team's area.

### Servers separated from APM (migrate-154)

* New `servers.view` / `servers.manage`; `/servers`, `/agent-policies`,
  `/agent-fleet`, `/server-monitoring` and `/server-baselines` use them.
  `apm.*` now covers APM only.
* The migration copies `apm.view → servers.view` and `apm.manage →
  servers.manage` onto every existing role (with a revision bump and a
  `role_history` entry), so nobody loses access on upgrade. Administrators can
  then remove whichever half a role should not have.

### Built-in team roles

`network_team`, `systems_team`, `application_team`, `security_team` (built-in
definition version 4). They are created at startup by
`role_service.reconcile_builtin_roles` and kept current like the other
built-ins; their names are reserved. A custom role that already uses one of
those names keeps it and the built-in is skipped. None of them can manage
users, roles, settings or the system.

### Role visibility scope (migrate-154)

* `roles.scope_tags`: tag names every member inherits. A member's effective
  scope is the role's tags **plus** the user's own `scope_tags` (ANY-of,
  case-insensitive); both empty = unrestricted; `system.admin` bypasses.
  Untagged resources stay hidden from scoped users (fail-closed).
* One change point: `scoping.visible_tags()` — every existing enforcement
  point (devices, servers, service checks, APM, alerts, reports, NetFlow)
  picks it up. The NetFlow saved-view owner check, which read user tags
  directly, now includes the owner's role tags.
* Role scopes are cached like role permissions (≤ 5 s staleness). A lookup
  failure with nothing cached yields a tag no resource can carry (hide, never
  reveal); a database without the column has no role scopes.
* Tag renames propagate to `roles.scope_tags`; tag deletes deliberately do not
  (a stripped last tag would widen every member).
* Delegation: a tag-scoped manager may only assign roles scoped within their
  own tags, may not create or edit a role scoped outside them, may not lift a
  role's scope, and may not leave an account they manage unrestricted (moving a
  user to an unscoped role is checked against the user's effective scope).
* Built-in roles are never scoped; start a custom role from one instead.

### Role editor

Four steps — Details (start from blank or a team/built-in role), Access
(per-module No access / View / Full presets grouped by product area, with
sensitive and privileged permissions ticked individually, and a live preview of
the menu members will get), Visibility (tags with per-tag resource counts) and
Review (diff, affected accounts, reason). Presets never include sensitive,
privileged or critical permissions (`scripts/test-role-model.mjs`).

## Verification

* Python unit tests: permission model, delegation (role scope rules), scoping
  (role scope union, fail-closed lookup, pre-migration schema), route policy
  (server routes use `servers.*`).
* Disposable PostgreSQL: migration applied after 153 with custom roles,
  re-run by the runner (skipped) and replayed raw (no duplicate history);
  built-in reconcile creates the four team roles once.
* Disposable API: role scope limits members' device lists and follows edits;
  delegated-manager rules; access-control end-to-end suite, including new cases
  for team separation and role scope.
