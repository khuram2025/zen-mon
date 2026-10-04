# Users, Roles & Authentication — verification, competitor assessment and fixes

Reviewed 1 October 2026 against **v1.23.23** (`release/1.23.23`, the code the original document reviewed; the
access-control files were byte-identical to the live appliance). Work is on branch
`claude/roles-permissions-assessment-20817e`.

Source document: `Roles-and-permissions-assessment.md` (41 findings: RP-01…RP-25, UA-01…UA-16).

## 1. Verdict on the document

**All 41 findings were checked against the source; every one that can be verified from code is confirmed.**
A few are source-only observations the original author also flagged as untested (concurrency races, multi-worker
timing); those are covered by new tests (below). Three statements deserve a correction or a qualification:

| Finding | Qualification |
|---|---|
| RP-09 | `/reports/data/{executive,technical,business}` **already** honour visibility scope (added with tag scoping); the gaps are `/reports/generate`, `/reports/data/inventory`, section rendering (`/reports/render/*`) and report schedules, plus NetFlow. |
| RP-22 | "APM Admins has the same 22 permissions as Operator" is a configuration fact of that appliance, not reproducible from the repository. Not a code defect; no code change. |
| UA-09 | The window is bounded by design: access tokens last `JWT_EXPIRE_MINUTES` (60), so a directory account is re-authenticated against the provider at least hourly. The gap is real (no immediate revocation on disable) but is *at most an hour*, not unbounded. A "sign out now" option was added. |

Everything else was reproduced or read directly in code. The route inventory is stronger than the document's
"679 declarations": introspecting the live application gives **684 routes**, of which **232 required nothing but a
valid sign-in** (including 22 mutating ones) and **46 were unauthenticated**.

### Additional defects found (not in the document)

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| X-01 | **P0** | `GET /api/v1/stream/{metrics,status,alerts}` (SSE) had **no authentication at all** — any anonymous client on the network received live device status and alert events. | `curl http://127.0.0.1:8000/api/v1/stream/alerts` → `200 text/event-stream` with no token; `/api/v1/alerts` → 401. Fixed (tickets + permissions + scope filtering). |
| X-02 | P1 | Any signed-in account (even Read Only) could create SNMP discovery jobs that reuse **stored credentials**, import discovered devices, edit/delete SNMP profiles, create/edit/delete custom reports, flip RUM error status. | Route inventory: `snmp.*`, `reports.*custom*`, `rum.rum_error_status` were `get_current_user` only. |
| X-03 | P1 | `maps.view` was inert: every manual-map **read** required `devices.manage`. | `manual_maps.list_maps/get_map/…` used `require_operator_user`. |
| X-04 | P1 | NetFlow saved views were listed to every user and could be deleted by any signed-in user (confirms RP-07 and widens it to disclosure). | `list_saved_views` returned all rows. |
| X-05 | P2 | A role row missing for an account whose role was named `owner`/`editor` silently resolved to **all permissions** by name. | `LEGACY_ROLE_PERMISSIONS["owner"]` consulted whenever no DB row existed. |
| X-06 | P2 | Persisted Operator/Viewer/Read Only lacked `netpath.*`/`udt.view_users` (RP-12) — so built-in roles could not use NetPath at all, a functional bug, not just drift. | `roles` seed (migrate-074) vs `permissions.py`. |
| X-07 | P2 | Username-enumeration timing oracle on sign-in (unknown user skipped the bcrypt verify). | `_login_impl`. Fixed (timing equaliser). |
| X-08 | P2 | `GET /roles`, `/roles/catalog`, `/users/roles` were readable by any signed-in account. | inventory. Fixed. |
| X-09 | P3 | Front-end `hasPermission` fell back to `role === 'admin'` (name-based) when permissions were absent. | `stores/auth.ts`. Removed. |

## 2. Finding-by-finding status

Legend: ✅ fixed and tested · 🟡 fixed with a stated limitation · ➕ extended beyond the document.

### Authorization (P1)

| ID | Verdict | Fix | Evidence |
|---|---|---|---|
| RP-01 | Confirmed | `core/delegation.py`: grant ceiling (a role may only be assigned if its permissions ⊆ the actor's), target boundary (cannot edit / disable / delete / reset / unlock accounts above own access), scope ceiling (a tag-scoped manager cannot create or manage unrestricted or wider accounts; cannot self-promote). `GET /roles/assignable` feeds the UI. ✅ | `test_delegation.py`; e2e `test_user_manager_cannot_create_administrators_or_reset_their_passwords` (HTTP) |
| RP-02 | Confirmed | Name-based bypass (`ADMIN_ROLES`, `OPERATOR_ROLES`, `require_roles`) deleted. Authorization is solely the role row's permissions; a role with no row has **none**. Reserved identifiers (`owner`, `admin`, `root`, `editor`, …) rejected on create **and** rename. Legacy vocabulary is used only when the `roles` *table* is missing (mid-update). ✅ | `test_authz_failure_modes.py`; e2e `test_reserved_and_legacy_names…` |
| RP-03 | Confirmed | Role create/update/delete enforce the grant ceiling **and** the target boundary (a delegated manager can no longer empty or alter a role more privileged than itself). ✅ | e2e `test_role_manager_cannot_grant_what_it_lacks…` |
| RP-04 | Confirmed (worse: X-01/X-02) | `core/route_policy.py`: every router is mounted with a **module default** (reads need a module *view* permission, mutations a *manage* permission); routes with their own guard keep it; public/self routes are an explicit allowlist. A new endpoint is protected by default. **Contract test fails the build** if any route is authenticated-only or unclassified. ✅ | `test_route_policy_contract.py` (0 unclassified of 684); e2e zero-permission role denied on 24 protected reads |
| RP-05 | Confirmed (+X-03) | `require_operator_user` removed; all 152 call sites replaced with the permission each action actually needs (`apm.manage`, `discovery.run`, `maps.manage`, `udt.manage`, `service_checks.manage`, `netflow.manage`, …). Manual-map reads need `maps.view`. ✅ | e2e `test_manage_permissions_are_not_interchangeable` |
| RP-06 | Confirmed | `audit.view` now satisfies the audit API; `settings.view`/`settings.manage` gate company/monitoring/Grafana settings; SMTP/SMS gateways need `settings.manage`, notification channels `alerts.manage` or `settings.manage`; report schedules need `reports.manage`; exports need `reports.export`. Security, storage, updates, licences and authentication stay with Full administration. Catalog wording corrected. ✅ | e2e `test_audit_view…`, `test_reports_and_settings_permissions_are_enforced` |
| RP-07 | Confirmed (+X-04) | Saved views have owner + `is_shared` (migrated: existing views stay shared). Create/delete need `netflow.manage`; sharing/deleting shared views needs `netflow.manage_shared`; foreign private views are invisible (404). ✅ | e2e `test_netflow_saved_views_are_owned…` |
| RP-08 | Confirmed | `core/admin_guard.py`: lock → mutate → verify. A transaction-scoped advisory lock serialises every administrator-affecting change (user demote/disable/delete **and role edits**); the resulting active-full-admin count must stay > 0. ✅ | e2e incl. a real **concurrent** race: exactly one of two simultaneous demotions succeeds |
| RP-09 | Confirmed (qualified) | NetFlow: a per-request visibility guard pins *every* flow query (both central helpers) to exporters that resolve to in-scope devices; a client filter can only narrow. Reports: scoped users are limited to device-filterable sections with the filter pinned to visible devices; fleet-wide sections, inventory and schedules are refused (fail closed). SSE streams are filtered per event. 🟡 *Scoped users cannot use fleet-wide report sections until those services accept a scope.* | `test_netflow_and_report_scope.py`; e2e `test_scoped_accounts_cannot_export_fleet_wide_reports…` |
| UA-01 | Confirmed | New rules start at **Choose role**; saving is blocked on incomplete rows; privileged destinations are flagged and need an explicit confirmation (server-enforced, `confirm_privileged`); RADIUS default role is now **Deny**. ✅ | e2e `test_directory_role_mappings_are_validated…`; browser check |

### Integrity & lifecycle (P2)

| ID | Verdict | Fix |
|---|---|---|
| RP-10 | Confirmed | Mappings validated against real roles at save and at sign-in (missing role → refuse). Role **delete** is blocked while a mapping/default references it (listed with remediation); role **rename** rewrites mappings atomically. ✅ |
| RP-11 | Confirmed | Role-policy cache TTL 5 s (declared SLA in the UI), fail-closed, savepoint lookup; `/auth/me` carries `permissions_revision`; the SPA re-reads the account on focus and every minute and after role saves. UI copy now states the real timing. ✅ |
| RP-12 | Confirmed | `BUILTIN_ROLE_DEFINITIONS` + `definition_version`; built-ins are reconciled once at API start under an advisory lock with a history entry; **custom roles are never touched**. Lookup errors keep the last valid policy; no rollback of the caller's transaction. ✅ |
| RP-13 | Confirmed | Parent tab and sub-tabs derive from child permissions (`roles.view/manage`, `users.view/manage`, `system.admin`); a direct link to a denied sub-tab explains why; new `roles.view` permission. ✅ |
| RP-14 | Confirmed | One password policy (`core/password_policy.py`, NIST 800-63B style: 12–128, blocklist, no username, no composition rules) exposed at `GET /auth/password-policy`; every form (create, reset, own, forced change) shows and checks it. ✅ |
| RP-15 | Confirmed | Inline identifier/display-name/description validation with API-matching limits; whitespace-only names rejected by the API; non-Latin names get a generated valid identifier. ✅ |
| RP-16 / UA-10 | Confirmed | `QueryBoundary`/`QueryFailure`: loading ≠ empty ≠ forbidden ≠ failed, with retry and stale-data banner; editors/mappings are blocked when the catalog or role list failed. ✅ |
| RP-17 | Confirmed | `users.role → roles.name` FK (`ON UPDATE CASCADE`, `ON DELETE RESTRICT`, added `NOT VALID` then validated when clean); `roles.revision` + `expected_revision` (409 with the current version and a reload/overwrite path); uniqueness races → 409. ✅ |
| RP-18 | Confirmed | `role_history` (who, when, added/removed permissions, affected accounts, reason) + History tab; audit events carry the same diff. ✅ |
| RP-19 | Confirmed | Role names are real buttons; every checkbox has a module-specific name ("Devices: Manage"); persistent help panel; table headers/scopes; risk shown as text. 🟡 No screen-reader run was performed. |
| RP-20 | Confirmed (measured) | Dialogs are bounded to `100dvh` with an internally scrolling body and a sticky footer — verified at 390×640 (dialog 7–633 px of 640, Save reachable). ✅ |
| RP-21 | Confirmed | "Select all standard" never includes sensitive/privileged/critical; per-module "All" likewise; risk summary + review step + reason before saving privileged grants; copying Administrator warns. ✅ |
| RP-22 | Qualified (see §1) | Members, History and an impact review (affected accounts, directory references, would-remove-last-admin) added. ✅ |
| RP-23 | Confirmed | `useDirtyGuard`: Escape/backdrop/close/cancel ask Keep editing / Discard; nothing dismisses a dialog mid-save. Applied to role, user and provider editors. ✅ |
| RP-24 | Confirmed | Explicit **All resources / Selected tags** control; administrators see a locked "All resources — full administration bypasses scope" state; stored tags preserved with explanatory copy. ✅ |
| RP-25 | Confirmed | Patch semantics: only changed fields are sent and applied; sessions are revoked only when role / status / scope really change. ✅ |
| UA-02 | Confirmed | Reset now **revokes every session** (and says so); optional force-change-at-next-sign-in; generator; session count in the result. ✅ |
| UA-03 | Confirmed | Tests report Connection / Credentials / Mapping / **ZenPlus admission** separately with reasons (provider disabled, account disabled, provisioning off, source collision, role missing), the **matched rule**, and never provision or touch last-login. ✅ |
| UA-04 | Confirmed | Username trimmed/validated (3–100, no control characters, unique ignoring case); email validated and normalised. ✅ |
| UA-05 | Confirmed | `model_fields_set`: explicit null clears, omission preserves. ✅ |
| UA-06 | Confirmed | Role changes for directory accounts are refused (409) on the server; a role is only changed through the mapping. ✅ |
| UA-07 | Confirmed (code) | Per-field case-insensitive conflict checks + `IntegrityError` → 409 (no more `MultipleResultsFound` 500). ✅ |
| UA-08 | Confirmed | Narrow `POST /system/auth/{provider}/enabled`; `revision` + `expected_revision` for full saves. ✅ |
| UA-09 | Confirmed (qualified) | Disable offers **sign out accounts now** (with counts) and records it. Default remains "no new sign-ins; sessions end within an hour". ✅ |
| UA-11 | Confirmed | `timeout × retries ≤ 30 s` enforced in API, client and form; the test runs under `wait_for` and reports a timeout distinctly from bad credentials. ✅ |
| UA-12 | Confirmed (reproduced) | Escaping-aware DN parsing (`CN=Ops\, East` → `ops, east`). ✅ |
| UA-13 | Confirmed | Ordered rules with up/down, named controls, inline validation of incomplete/duplicate rows (no silent drop). ✅ |
| UA-14 | Confirmed | Explicit "remove stored secret" (`clear_secret`); blank still means keep. ✅ |
| UA-15 | Confirmed | Redacted before/after diffs (secret → replaced/cleared, CA → fingerprint), revision, actor, privileged-grant flag, change-history dialog, last-test result on the overview. ✅ |
| UA-16 | Confirmed | Last-admin check evaluates the *resulting* active-admin set (inactive admins can be removed). ✅ |

### The document's "missing capabilities" list

| Item | Status |
|---|---|
| URL-persistent filters, source/scope/never-signed-in/MFA filters, sorting, pagination, exact sign-in timestamps | ✅ |
| User detail with effective access, history, authorised session revocation | ✅ |
| Forced password change after an administrative temporary password | ✅ |
| Invite/activation links and expiring reset links | ⏳ needs a configured mail gateway (none on the reference appliance); the forced-change flow covers the temporary-password case. |
| MFA / step-up | ✅ TOTP for local accounts (+ recovery codes, lockout, policy: off / administrators / all local) · ⏳ step-up per privileged action |
| OIDC | ✅ (authorization-code + PKCE; Entra ID, Okta, Keycloak, Google) · ⏳ SAML (needs XML-DSig native libraries), SCIM, directory lifecycle sync |
| Session inventory | ✅ |
| Provider health / last test, certificate expiry | ✅ last test · ⏳ certificate expiry, second-server failover |
| RADIUS attribute specificity | ✅ attribute-aware rules · ⏳ Access-Challenge (documented as unsupported) |
| Directory scope defaults | 🟡 stated in the UI (new directory users are unrestricted by tag; limit afterwards) · ⏳ per-mapping scope |

## 3. Competitor assessment (online research, 1 Oct 2026)

Sources: vendor documentation for SolarWinds Orion, Datadog, Dynatrace, ManageEngine OpManager, PRTG, Zabbix,
Grafana, LogicMonitor, Cisco Meraki, Elastic/Kibana (links in the appendix). "?" = not found in a source.

| Capability | SW | DD | DT | ME | PRTG | ZB | GF | LM | **ZenPlus before** | **now** |
|---|---|---|---|---|---|---|---|---|---|---|
| Custom roles | partial | ✔ | ✔ | ✔ | – | ✔ | ✔ | ✔ | ✔ | ✔ |
| Role manager cannot grant more than they hold | ? | partial | ? | partial | ? | partial | ✔ | ? | ✘ | **✔** |
| Resource scoping (groups/tags) | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | partial (tags; many APIs unscoped) | **✔ (NetFlow, reports, streams added)** |
| Optimistic concurrency / versioned role edits | ? | ? | ? | ? | ? | ? | ✔ | ? | ✘ | **✔** |
| Role change audit with before/after | ? | ✔ | ✔ | ? | ? | ? | ? | ✔ | partial | **✔ (history + diff)** |
| Effective-access view | ? | ? | ✔ | partial | ? | ? | ? | ? | ✘ | **✔** |
| Sensitive-permission flagging | ? | ✔ | ? | ? | ? | ? | ? | ? | ✘ | **✔ (risk classes)** |
| SAML / OIDC SSO | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✘ | **OIDC ✔, SAML ⏳** |
| IdP group → role mapping | partial | ✔ | ✔ | ? | ? | ✔ | ✔ | ? | LDAP/RADIUS only | **✔ ordered, validated, previewable** |
| Native MFA | IdP only | ? | IdP only | partial | hosted only | ✔ | ? | ✔ | ✘ | **✔ TOTP + recovery + policy** |
| Brute-force lockout / password policy | ✔ | ? | ? | ? | partial | ✔ | ? | ✔ | rate-limit only | **✔ lockout + NIST-style policy** |
| Active-session list / force logout | ? | ? | ? | ? | ? | ? | ✔ | ? | ✘ | **✔** |
| Minimum-admin / break-glass guard | partial | ? | ? | ? | ? | partial | ? | ? | partial (racy) | **✔ atomic** |
| Per-object ACLs, explicit deny | – | ✔ | – | – | ✔ | ✔ | ✔ | partial | ✘ | ⏳ roadmap |
| SCIM provisioning | ? | ✔ | ✔ | ? | ? | ? | ✔ | ? | ✘ | ⏳ roadmap |
| Scoped API tokens / service accounts | ? | ✔ | ✔ | ? | – | partial | ✔ | ✔ | ✘ | ⏳ roadmap |

Best-practice references applied: NIST SP 800-63B (length floor, blocklist, no composition/rotation rules),
OWASP Authorization Cheat Sheet and ASVS V8 (deny by default, server-side enforcement, changes effective promptly),
CIS Controls v8 5.4/6.8 (dedicated admins, periodic access review → the **Access review** CSV).

### Features added because of this assessment

1. **Delegation ceiling** everywhere (Grafana/OWASP model) — roles, users, scope.
2. **Route-to-capability contract** with a build-failing test; module defaults for new endpoints.
3. **Role editor v2**: risk classes, dependencies, locked-when-you-lack-it, impact review, reason, history, members, concurrency, dirty-draft protection.
4. **Users v2**: server-side filters/sort/pagination in the URL, effective-access dialog, session inventory + revoke, unlock, MFA reset, force-change, CSV **access review** export.
5. **Authentication v2**: OIDC SSO; sign-in policy (MFA requirement, lockout); admission-aware provider tests; change history; narrow enable/disable with impact.
6. **TOTP MFA** for local accounts with recovery codes and a forced-enrolment gate.
7. **Short-lived stream tickets** so the session token never appears in an SSE URL.

## 4. Behaviour changes to announce

* **Built-in Operator** gains `reports.manage`, `netflow.manage`, `netflow.manage_shared`, NetPath and `udt.view_users` (it could previously create custom reports and mutate views anyway, via unauthenticated-by-permission routes). **Viewer / Read Only** lose creating custom reports, saving NetFlow views and SNMP discovery/profile writes (the intended "no modification" promise). Custom roles are **not** changed; review any that relied on the old holes (the e2e matrix lists what each module needs).
* `settings.view` is needed to read company/monitoring settings; gateways need `settings.manage`.
* `audit.view` (not Full administration) reads the audit log.
* A custom role that should use NetFlow-only analytics must also hold `devices.view` for exporter names.
* New permissions: `roles.view`, `netflow.manage`, `netflow.manage_shared`.
* Existing sessions keep working (they carry no session id); new sign-ins are tracked and revocable.
* **Release packaging:** `migrate-150-access-control-hardening.sql` must be named explicitly in `build-release.py` (it packages no migrations unless each is named). It is idempotent and has no destructive step; the role foreign key is added `NOT VALID` and validated only if no orphan exists.

## 5. Test evidence

* Unit/contract: `pytest server/tests` → **1245 passed, 5 skipped** (new: route contract over all 684 routes, delegation, fail-closed lookup, TOTP RFC 6238 vectors, password policy, DN/RADIUS mapping, NetFlow/report scope, OIDC validation incl. algorithm-confusion, forged signature, nonce/aud/iss/exp, key rotation, PKCE).
* HTTP end-to-end (`server/tests/integration/test_access_control_e2e.py`, 29 tests, disposable DB): delegation, reserved names, 24-route zero-permission matrix, public routes, stream tickets, last-admin invariant **with concurrent requests**, role revision conflicts/history/impact/members, identity validation and conflicts, forced change, lockout/unlock/session revocation, MFA enrol/login/recovery/policy, directory-mapping validation and privileged confirmation, secret clearing and redacted audit, provider tests, effective access/CSV export, and an **OIDC flow against a mock IdP** (PKCE, replay, collision, revoke-on-disable).
* Dashboard: `vite build` and route smoke pass; browser-verified role editor validation, dirty-guard, 390×640 dialog bounds, user list URL filters, mapping editor defaults, profile security cards.

Not exercised: a real LDAP/RADIUS/IdP (none on the appliance), screen readers, multi-worker revocation timing, NetFlow scoping against live ClickHouse flow data (the predicate is unit-tested).

## 6. Roadmap (not built)

SAML 2.0, SCIM 2.0, scoped/expiring API tokens and service accounts, per-object ACLs (dashboards, maps, reports) with explicit-deny rules, reusable scope boundaries, invite/activation e-mail flows, step-up MFA for privileged actions, nested-group expansion, certificate-expiry and failover for directory servers.

## Appendix — sources

SolarWinds: documentation.solarwinds.com (account limitations, secure configuration) · Datadog: docs.datadoghq.com/account_management (rbac, granular_access, saml, scim, service_accounts, api-app-keys, audit_trail) · Dynatrace: docs.dynatrace.com (IAM policies/boundaries, SAML, SCIM, audit logs) · ManageEngine: manageengine.com/network-monitoring/help (custom-user-roles, SAML) · PRTG: paessler.com/manuals/prtg/user_access_rights · Zabbix: zabbix.com/documentation (user roles, user groups, SAML, API tokens) · Grafana: grafana.com/docs (assign/manage RBAC roles, SCIM, server-admin users) · LogicMonitor: logicmonitor.com/support (security settings, API tokens) · Meraki: documentation.meraki.com (administrator permissions) · Elastic: elastic.co/docs (role mapping, spaces) · NIST SP 800-63B-4 · OWASP Authorization Cheat Sheet / ASVS 5.0 V8 · CIS Controls v8.
