# E5 — RBAC + SSO/SAML/OIDC + Multi-Tenancy

## 1. Goal & competitive rationale
ZenPlus today ships a single-tenant app with coarse role strings and local password login (default `admin/admin123`). 13 of 15 competitors (PRTG, LogicMonitor, Datadog, Auvik) offer enterprise SSO and granular RBAC, and the MSP segment is unreachable without per-client tenant isolation. This epic delivers granular permissions, SAML 2.0 / OIDC SSO with JIT provisioning, and row-level tenant scoping so one ZenPlus instance can host many isolated clients — the table-stakes gate for enterprise and MSP deals.

## 2. Scope
### In scope
- First-class `roles` / `permissions` model replacing the hardcoded `ROLE_PERMISSIONS` map; per-permission enforcement.
- SAML 2.0 (SP-initiated) and OIDC/OAuth2 login with JIT user provisioning and group→role mapping.
- `tenant_id` row-level isolation across devices, services, alerts, reports, flows, sensors, credentials, and ClickHouse metrics.
- MSP "org switcher", tenant CRUD, and per-tenant identity-provider config.
- Feature-flagged, copy-tested query-scoping migration.

### Out of scope
- SCIM auto-deprovisioning (phase 2), per-field/column-level RBAC, hierarchical sub-tenants, billing/quota per tenant, hardware MFA.

## 3. Current state in ZenPlus
Verified locally:
- **Auth**: `server/app/core/security.py` — JWT HS256 with `{sub, role, exp}`, `get_current_user` resolves `User` by id; `server/app/api/v1/auth.py` `login()` does local password verify only.
- **RBAC**: `server/app/api/v1/users.py` defines `VALID_ROLES`, `ROLE_PERMISSIONS`, and `require_admin()` — a hardcoded Python dict, not enforced per-permission (most routes only gate on `role == "admin"`).
- **Model**: `server/app/models/user.py` `User` has a `role: String(20)` column, no tenant, no permission link.
- **No tenant anywhere**: `server/app/models/device.py` `Device` has `created_by` but no `tenant_id`; queries (`server/app/api/v1/devices.py`) `select(Device)` with no scoping filter. ClickHouse tables (`scripts/init-clickhouse.sql`) order by `device_id`/`service_check_id` — no tenant key.
- **Schema bootstrap**: no Alembic; ordered SQL files in `scripts/` (latest `migrate-008-sensors.sql`). New work continues that numbering.
- **Frontend**: `dashboard/src/stores/auth.ts` persists `{token,user}`; `App.tsx` `Protected` gates only on token presence (no permission checks). `UsersPage.tsx`/`LoginPage.tsx` exist.

**Missing**: any SSO/SAML/OIDC, tenant column/scoping, permission tables, org switcher, IdP config UI.

## 4. Target design & architecture
```
Login ─┬─ local pwd ──────────────► /auth/login
       ├─ OIDC code flow ─► /auth/oidc/{provider}/callback ─┐
       └─ SAML POST  ────► /auth/saml/{provider}/acs ───────┤
                                                            ▼
                          JIT provision → map IdP groups → role
                                                            ▼
   JWT { sub, tenant_id, role_id, perms[], idp } (HS256, short TTL)
                                                            ▼
  get_current_principal() ── TenantContext (ContextVar) ── require_perm("devices.manage")
                                                            ▼
   ScopedSession: every ORM query auto-filtered WHERE tenant_id = ctx.tenant
   ClickHouse helper: every SELECT/INSERT carries tenant_id predicate
```
- **API**: replace `get_current_user` with `get_current_principal` returning a `Principal(user, tenant_id, permissions:set)`. Add `require_perm(code)` dependency factory. Tenant resolved from JWT; MSP admins may switch via `X-Tenant-ID` validated against their memberships.
- **Scoping**: a SQLAlchemy `with_loader_criteria` / global `before_compile` hook injects `tenant_id == ctx` for all `TenantMixin` models, gated by a `TENANT_ISOLATION` flag so the default path is identical to today during rollout.
- **SSO**: `pysaml2` for SAML, `authlib` for OIDC. Per-tenant `identity_providers` rows hold metadata/client secrets (encrypted via existing `app/core/crypto.py`).

## 5. Data model & migrations
New Postgres tables (`scripts/migrate-009-rbac-tenancy.sql`):
- `tenants(id uuid pk, slug unique, name, status, created_at)` — seed a `default` tenant.
- `roles(id uuid, tenant_id uuid null, name, is_system bool, unique(tenant_id,name))`.
- `permissions(code pk text, description)` — seed from existing permission strings.
- `role_permissions(role_id, permission_code)`.
- `user_tenants(user_id, tenant_id, role_id, primary key(user_id,tenant_id))` — enables MSP users in N tenants.
- `identity_providers(id, tenant_id, type[saml|oidc], config jsonb, group_map jsonb, enabled, encrypted secrets)`.
- `audit_log(id, tenant_id, user_id, action, target_type, target_id, ip, ts)`.

Alter every tenant-owned table: `ALTER TABLE devices ADD COLUMN tenant_id uuid; ... UPDATE ... SET tenant_id = <default>; ... SET NOT NULL; CREATE INDEX ix_devices_tenant ON devices(tenant_id);` — repeat for `device_groups, alerts, alert_rules, service_checks, sensors, snmp_credentials, reports, flows`. Index `(tenant_id, status)` where queries filter by status.

ClickHouse (`migrate-009-rbac-clickhouse.sql`): add `tenant_id String DEFAULT '<default>'` to `ping_metrics`, `service_metrics`, `*_status_log`, rollup tables; rebuild ORDER BY to `(tenant_id, device_id, timestamp)` via new tables + `INSERT SELECT` backfill (ORDER BY change requires table recreate). Poller writes `tenant_id`; backfill maps via device→tenant join.

**Migration notes**: run on a restored copy first; `tenant_id` is nullable→backfilled→NOT NULL in three steps so it is online-safe; isolation enforcement stays OFF until backfill verified.

## 6. API changes
- `POST /auth/oidc/{provider}/login` → 302 to IdP; `GET /auth/oidc/{provider}/callback` → exchanges code, JIT-provisions, returns app JWT.
- `GET /auth/saml/{provider}/metadata`; `POST /auth/saml/{provider}/acs` → consumes assertion, returns JWT.
- `POST /auth/switch-tenant` `{tenant_id}` → re-issues JWT for another membership (MSP).
- `GET /auth/me` → now returns `permissions[]`, `tenant_id`, `tenants[]`.
- `GET/POST/PUT/DELETE /roles`, `/roles/{id}/permissions`, `GET /permissions` — role CRUD (replaces static `/users/roles`).
- `GET/POST/PUT/DELETE /tenants` (super-admin); `GET/POST /tenants/{id}/identity-providers`.
- All existing list/detail endpoints: response unchanged; server silently scopes by tenant.

## 7. Poller / collector changes
- `poller/internal/config` (or device-loader package): include `tenant_id` when loading device/service config from Postgres.
- `poller/internal/clickhouse` writer structs (`PingMetric`, `ServiceMetric`, status-log rows): add `TenantID` field; include column in batch `INSERT`.
- Discovery/flow collectors stamp `tenant_id` from the owning device. No new protocols; reuse existing native-protocol ClickHouse client.

## 8. Dashboard changes
- New `dashboard/src/stores/rbac.ts` `hasPerm(code)`; `<Can perm="...">` guard component; extend `Protected` in `App.tsx` to accept `requirePerm`.
- `LoginPage.tsx`: render IdP buttons from `GET /auth/providers`; handle callback token.
- New `OrgSwitcher` in `Layout` header (visible to multi-tenant users) → calls `/auth/switch-tenant`.
- New pages: `RolesPage.tsx` (role/permission matrix), `TenantsPage.tsx`, `IdentityProvidersPage.tsx`. Extend `UsersPage.tsx` with per-tenant role assignment. Hide menu items lacking permission.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | `migrate-009` Postgres: tenants/roles/perms/user_tenants/idp/audit + seed | db | 2 | — |
| 2 | `TenantMixin` + `before_compile`/`with_loader_criteria` scoping, flag-gated | api | 3 | 1 |
| 3 | `Principal`, `get_current_principal`, `require_perm` factory; replace `require_admin` | api | 3 | 1 |
| 4 | Roles/permissions CRUD endpoints | api | 2 | 3 |
| 5 | OIDC login/callback via authlib + JIT provisioning + group map | api | 4 | 3 |
| 6 | SAML metadata/ACS via pysaml2 + JIT | api | 4 | 3 |
| 7 | Tenant CRUD + IdP config endpoints (secrets via crypto) | api | 2 | 1 |
| 8 | Switch-tenant + multi-tenant `/auth/me` | api | 1.5 | 3 |
| 9 | Backfill `tenant_id` on all PG tables + add-column migration steps | db/infra | 2 | 1 |
| 10 | ClickHouse tenant column, ORDER BY rebuild, backfill | db/infra | 3 | 9 |
| 11 | Poller: add `TenantID` to writers + config load | poller | 2 | 10 |
| 12 | Audit-log writes on auth/role/tenant mutations | api | 1.5 | 3 |
| 13 | RBAC store, `<Can>`, permission-aware nav/routing | ui | 2 | 4 |
| 14 | LoginPage SSO buttons + callback handling | ui | 2 | 5,6 |
| 15 | RolesPage / TenantsPage / IdentityProvidersPage | ui | 4 | 4,7 |
| 16 | OrgSwitcher + UsersPage per-tenant roles | ui | 2 | 8 |
| 17 | Copy-DB migration dry-run + scoping correctness harness | infra | 2 | 9,10 |
| 18 | Docs + rollout flag runbook | infra | 1 | all |

## 10. Acceptance criteria
- [ ] A user logs in via Okta (OIDC) and Azure AD (SAML); JIT creates the user with the role mapped from their IdP group.
- [ ] An admin creates a custom role, grants `devices.view` only, and that user sees devices but gets 403 on create/edit.
- [ ] Two tenants' devices/alerts/reports/metrics never appear in each other's lists or charts (verified at API and ClickHouse).
- [ ] An MSP user with two memberships switches orgs and the entire UI/data context changes.
- [ ] With `TENANT_ISOLATION=off`, behavior is byte-identical to pre-epic (back-compat).
- [ ] Last-admin and self-lockout protections still hold; all role/tenant changes are audit-logged.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|--------------|-------|-----------------|
| T1 | unit | seed perms | Resolve principal for custom role | `permissions` set matches `role_permissions` |
| T2 | unit | — | Decode JWT missing `tenant_id` | 401 / rejected |
| T3 | integration | flag on, 2 tenants | List devices as tenant A | Only A's devices returned |
| T4 | integration | 2 tenants | Query ClickHouse rollups as B | No A `device_id` rows |
| T5 | integration | viewer role | POST /devices | 403 `insufficient permission` |
| T6 | integration | operator role | POST /devices | 201 created, scoped to tenant |
| T7 | e2e | Okta app | OIDC login first time | User JIT-created, mapped role, redirected in |
| T8 | e2e | Azure SAML | SAML ACS valid assertion | Session established, correct tenant |
| T9 | security | — | Replay/expired SAML assertion | Rejected, audit entry written |
| T10 | security | tenant A token | Call with forged `X-Tenant-ID: B` | 403, no data leak |
| T11 | security | — | IdP `group_map` to admin spoofed claim | Mapping only honored from signed assertion |
| T12 | integration | MSP user in A,B | switch-tenant to B then list alerts | B alerts only; new JWT issued |
| T13 | manual | last admin | Demote sole tenant admin | Blocked (existing guard) |
| T14 | perf | 50k devices/tenant | List + metrics under scoping | p95 within 10% of baseline (tenant index used) |
| T15 | regression | flag off | Full smoke of devices/alerts/reports | Identical to pre-epic responses |
| T16 | integration | direct ORM | Cross-tenant `get device by id` | Returns 404, not 403 (no existence leak) |
| T17 | unit | — | Backfill maps device→tenant | All rows non-null post-migration |
| T18 | e2e | UI viewer | Load nav | Admin-only menu items hidden, routes 403-guarded |
| T19 | integration | disabled IdP | Attempt SSO login | 400 provider disabled |
| T20 | perf | concurrent logins | 200 parallel OIDC callbacks | No session/token collision, stable latency |

## 12. Risks & rollout
- **Flags**: `TENANT_ISOLATION` (scoping enforcement) and `SSO_ENABLED` in `core/config.py`, default off. Ship code dark; enable per-environment.
- **Migration/back-compat**: three-step nullable→backfill→NOT NULL keeps writes online; everything pre-existing maps to the seeded `default` tenant, so single-tenant installs are unaffected. ClickHouse ORDER BY change is the highest-risk step — done via shadow tables + `INSERT SELECT` and validated row-count parity before cutover. Always dry-run on a restored DB copy (task 17).
- **Perf**: `(tenant_id, …)` indexes and ClickHouse sort-key prefix keep scoped queries fast; benchmark before enabling (T14).
- **Security**: HS256 JWT stays but add short TTL + tenant binding; IdP secrets encrypted via `app/core/crypto.py`; enforce assertion signature/audience/replay checks; never trust client-supplied tenant header beyond membership. Audit-log all privileged mutations.
- **Phased rollout**: (1) merge dark; (2) run migration + backfill on staging copy; (3) enable isolation on internal tenant; (4) enable SSO for one pilot enterprise tenant; (5) GA + flip default to on for new installs.

Relevant files: `/home/zen/zen-mon-push/server/app/core/security.py`, `/home/zen/zen-mon-push/server/app/api/v1/{auth.py,users.py}`, `/home/zen/zen-mon-push/server/app/models/{user.py,device.py}`, `/home/zen/zen-mon-push/server/app/core/database.py`, `/home/zen/zen-mon-push/scripts/init-clickhouse.sql`, `/home/zen/zen-mon-push/dashboard/src/{App.tsx,stores/auth.ts}`.
