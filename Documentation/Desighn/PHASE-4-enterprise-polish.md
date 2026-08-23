# Phase 4 — Enterprise Polish (Security, DR, Published Limits)

**Goal:** the platform sells into security reviews and survives a site disaster: SSO/MFA, real RBAC, full audit, mTLS to the edge, a DR site with tested RPO/RTO, and capacity tiers you can put in a datasheet because they were measured.
**Duration:** ongoing after Phase 3; tasks are independent — schedule by customer pull. Efforts are rough.
**Entry criteria:** Phase 3 gate passed. Tasks here assume the outbox, leader election, harness, and HA pair exist.

---

## P4-T1 · SSO (OIDC first, SAML optional) **[high, ~5 pd]**
**Change:** OIDC login flow (authlib) beside local accounts: `system_settings` for issuer/client; JIT user provisioning with role mapping from IdP claims; local login can be disabled per policy; sessions still issue the internal JWT.
**Verify:** login via Keycloak (lab IdP) lands with mapped role; deactivating the IdP user blocks login ≤ token expiry; local-login-disabled mode still allows a break-glass admin (documented).

## P4-T2 · MFA for local accounts **[high, ~3 pd]**
**Change:** TOTP enrollment (QR + recovery codes) enforced by role policy; login flow gains the second step; recovery-code burn logged to audit.
**Verify:** enroll, login requires code; wrong code ×5 → lockout per T5; recovery code works once; audit rows written for enroll/use/reset.

## P4-T3 · Granular RBAC **[high, ~8 pd]**
**Problem:** three flat roles, every user sees the whole estate.
**Change:** resource-scoped permissions: roles become permission sets (view/ack/configure per domain: devices, servers, netflow, apm, ncm, settings) with optional **scope** by site/device-group; enforcement via a single `require(permission, scope)` dependency replacing the three ad-hoc ones; UI hides what the token can't do.
**Verify:** matrix test in `server/tests/` (role × endpoint × expected code) — generated from the route table so new routes fail closed; a site-scoped operator sees only their site's devices/alerts (API and UI); privilege escalation attempts (editing own role) rejected + audited.

## P4-T4 · Full audit coverage + auth events **[high, ~4 pd]**
**Problem:** 61 call sites across 8 of ~25 routers; no login/logout/failed-auth records; audit failures are swallowed silently.
**Change:** middleware-level audit for every mutating request (method, route, actor, target id, outcome) replacing per-endpoint calls; auth events (success/failure/lockout/token-refresh) recorded; audit write failure raises a self-alert (never silent); export endpoint (CSV/JSONL) for SIEM pull, retention per P1-T9.
**Verify:** fuzz 20 random mutating endpoints → 20 audit rows with correct actor; failed login recorded with source IP; kill PG during a mutation → request fails **and** a self-alert fires about audit unavailability; SIEM export matches row counts.

## P4-T5 · Rate limiting + login lockout **[high, ~3 pd]**
**Problem:** No throttle anywhere; `/auth/login` allows unlimited bcrypt brute force on the event loop.
**Change:** slowapi (Redis-backed so limits are cluster-wide): login 5/min/IP + progressive account lockout with audit; agent/sensor ingest per-identity ceilings (protect against a misbehaving endpoint); global per-IP sane default on `/api/*`; 429s carry `Retry-After`.
**Verify:** hydra-style login loop → 429 then lockout alert; legitimate dashboard usage never hits limits under M-load; one misconfigured agent hammering at 100× cadence is throttled without affecting others (harness).

## P4-T6 · mTLS for sensors and agents **[medium, ~8 pd]**
**Change:** internal CA (step-ca or smallstep-lib) on the controllers; enrollment issues a client cert alongside the API key (the dormant `rotate_certificate` command becomes real); nginx enforces client certs on `/api/v1/sensor/*` and `/api/v1/agents/*` (grace mode: log-only → enforce per ring); cert rotation via the command channels; revocation on unenroll.
**Verify:** sensor with cert connects; without → 403 in enforce mode (grace mode logs); rotation command swaps certs with zero missed heartbeats; revoked sensor rejected ≤ 5 min; fleet-wide enforcement enabled ring-by-ring with rollback tested.

## P4-T7 · DR site **[high, ~10 pd + hardware]**
**Change:** third location (or cloud): async PG standby (Patroni standby-cluster) + nightly CH `RESTORE` rehearsal from offsite backups (or a delayed third CH replica if bandwidth allows) + config/artifact replication; **manual, documented promotion** (DR is a decision, not an automatism); DNS/VIP cutover plan; edge fleet reaches DR via the multi-URL list (P2-T9 — add the DR URL to the standard list).
**Targets:** RPO ≤ 15 min (PG ~0, CH ≤ backup/replica lag), RTO ≤ 1 h.
**Verify:** semi-annual DR drill: simulate total primary-site loss → promote DR per runbook, agents/sensors converge on DR URLs, dashboards show data ≤ RPO gap; measured RTO ≤ 1 h; failback documented and executed back to primary.

## P4-T8 · Published capacity tiers **[medium, ~4 pd]**
**Change:** run the P1 harness at S/M/L profiles on the reference hardware (BOM §12); record the knee points (API p95, CH ingest, poller cycle times, alert latency); publish `Documentation/CAPACITY.md` with supported tiers + headroom rules (the §9.1 table becomes measured, not derived); wire the key gauges (drop counters, queue depths, loop lag, replication lag) into a permanent `/api/v1/system/capacity` endpoint + UI page so operators see where they sit against the tier.
**Verify:** datasheet numbers reproduce within 15% on a second run; the capacity page shows live utilization vs tier on staging under M-load.

## P4-T9 · Multi-tenancy decision **[optional, spike ~3 pd]**
**Change:** written ADR: single-org appliances vs MSP multi-tenant (org_id columns + RLS vs separate instances per tenant). Recommendation from the assessment: **separate instances** (the OTA/appliance model already isolates tenants; retrofitting org_id across ~70 tables + every CH query is high-risk for low demand). Revisit only with concrete MSP demand.
**Verify:** ADR reviewed and signed; if multi-tenant is chosen, a dedicated phase plan is written before any schema work.

---

## Standing cadence (post-Phase 4)

| Ritual | Cadence |
|---|---|
| Restore drill (P0-T4 procedure) | quarterly |
| HA game day (Phase 2 gate, abbreviated G1/G4/G7) | quarterly |
| DR drill (P4-T7) | semi-annual |
| Capacity re-baseline after major releases | per minor version |
| Key rotation review (release key, JWT secret, internal token, CA) | annual |
| Anon-surface scan (`scripts/tests/anon-surface.sh`) in CI | every release |
