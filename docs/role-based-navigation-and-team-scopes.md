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

### Custom page-access roles (migrate-185)

For helpdesk teams and NOC wall screens that should see only certain pages.
An administrator limits a custom role to selected pages and network maps in
the role editor's **Pages** step and assigns the role to users as usual.

* `roles.page_access` is `NULL` (not limited) for every existing role, so an
  upgrade changes nobody's access. Otherwise it is
  `{"pages": [...] | null, "maps": [...] | null}`: pages are sidebar entries by
  their address (`to` of the leaf entries in `navigation.ts`), maps are
  network map ids; `null` in a field means no limit there.
* **Not a parallel system.** A page list narrows the role's permissions
  (`app.core.page_access.restrict_permissions`), and
  `security.get_role_permissions` returns the narrowed set, so every existing
  guard (route policy, `require_permission`, stream guards, delegation
  ceilings, `/auth/me`) applies it. A permission stays when a listed page the
  role can open needs it (its navigation gate, parent included), when it
  extends one of those on a page that is not view-only (`PERMISSION_IMPLIES`,
  e.g. `devices.manage` on Devices, `alerts.acknowledge` on Active Alerts),
  when a listed page shows it read-only (`uses`: dashboard widgets), or when a
  kept permission depends on it. Everything else is withdrawn, so calling the
  API of a hidden page is refused. Dashboards are `viewOnly`: a wall role
  limited to one dashboard reads its widgets' data and changes nothing.
* The API boundary is the permission: pages that read the same permission
  (Devices and Link Utilization both read `devices.view`) share an API.
  Listing one makes that data readable through the API even though the other
  page is hidden from the menu and the route guard.
* **Maps** (the NOC wall views): the map API filters `GET /maps` to the
  listed maps, refuses every `/maps/{map_id}…` route for other maps (403) and
  refuses creating maps while the role is limited. A device's "on these maps"
  panel only names listed maps.
* **Dashboard**: the sidebar, the Inventory tab strip and the Settings tabs
  show only listed pages (Appearance, Support and My Profile are always open);
  `RouteGate` denies other pages, and `/` sends the user to the first listed
  page. Header widgets (device search, alert centre) appear only when their
  page is listed. The role editor shows a checkbox tree grouped like the
  sidebar, the map picker, the permissions the list withdraws, and the menu
  preview (including the landing page).
* The server's copy of the page catalog,
  `server/app/core/page_catalog.json`, is generated from `navigation.ts` by
  `node scripts/test-page-access.mjs --write` (in `dashboard/`); CI fails when
  it drifts. Both sides run the same restriction vectors.
* Full administration cannot be limited (the API refuses the combination).
  A manager whose own role is page-limited can only assign, create or edit
  roles within their own pages and maps, and cannot lift a limit.
* Lookups are cached like role permissions (≤ 5 s). A failed lookup with
  nothing cached limits the role to no page and no map; a database without
  the column has no page limits.

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
* Page access: `dashboard/scripts/test-page-access.mjs` (catalog in sync,
  sidebar/landing/URL rules, restriction vectors), `test-role-model.mjs`
  (editor selection helpers), `server/tests/test_page_access.py` (model,
  lookup fail-closed, delegation) and `test_page_access_pg.py` (migration,
  map and hidden-page API refusals end to end, history, validation).
