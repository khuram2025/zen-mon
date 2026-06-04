# E10 — Custom Dashboards / NOC Video Wall

## 1. Goal & competitive rationale
ZenPlus dashboards (`DashboardPage.tsx`, `Dashboard.tsx`) are rich but hard-coded — every customer sees the same fixed layout. 12 of 15 competitors (PRTG, LibreNMS, Zabbix, Datadog) ship user-built dashboards; their absence blocks NOC/MSP deals where teams need per-team boards and a kiosk video wall on a wall-mounted TV. This epic delivers a drag-drop builder, a widget catalog reusing our existing chart components, per-user/shared boards with template variables, and a kiosk playlist mode — turning a fixed product into a customizable platform.

## 2. Scope
### In scope
- Drag-drop grid builder (resize/move/add/remove widgets) with autosave.
- Widget catalog: metric/time-series chart, device list, world map, top-N, ring gauge, alert table, status heatmap, single-stat, text/markdown, flow panel (stub — NetFlow not in local repo).
- Per-user, shared (org-wide), and role-restricted dashboards; clone/template.
- Dashboard-level variables/filters (group, location, device-type, time-range) bound into widget queries.
- Kiosk/NOC video-wall mode: full-screen, auto-rotating playlist of boards, public tokened URL, auto-refresh.
- Widgets + layout persistence API; RBAC on edit/share.

### Out of scope
- Authoring brand-new visualization types beyond the catalog above.
- NetFlow/Servers/Agent-Fleet data sources beyond a registered-but-disabled stub (modules absent locally).
- Cross-tenant dashboard marketplace; PDF export of custom boards (reuse `export_service` later).
- Alerting from widgets (lives in `alert_rules`).

## 3. Current state in ZenPlus
- **Fixed dashboards**: `dashboard/src/pages/DashboardPage.tsx` (43 KB) hard-codes KPI cards, availability `AreaChart`, world map, gauges via `useQuery`/`useQueries`. No builder, no persistence.
- **Reusable visual primitives already exist**: `components/dashboard/WorldMap.tsx`, `RingGauge.tsx`, `Sparkline.tsx`, `StatusCard.tsx`, `AlertBanner.tsx`; `components/charts/TimeSeriesChart.tsx`, `StatusHeatmap.tsx`. These become the widget render layer.
- **Routing**: `App.tsx` uses lazy routes + `Protected`; new `/d` routes slot in cleanly.
- **API patterns**: routers under `server/app/api/v1/*` registered in `main.py:create_app`; SQLAlchemy models (`models/service_check.py` shows `JSONB`/`ARRAY` columns) — the exact pattern for storing layout/widget JSON.
- **Metrics**: `services/metric_service.py` → `get_clickhouse_client()`, auto-granularity over `ping_metrics{,_5m,_1h}`. Widgets reuse this.
- **Realtime**: `api/websocket/realtime.py` SSE over Redis pub/sub — kiosk live refresh reuses it.
- **RBAC**: `core/security.py:get_current_user`; roles `admin/operator/viewer/read_only` and `require_admin` in `api/v1/users.py`.
- **Vestigial schema**: `scripts/init-postgres.sql` already defines `dashboard_configs(layout JSONB, widgets JSONB)` — **defined but referenced by zero code**. We will supersede it with a normalized schema.
- **Missing**: no widget registry, no builder UI, no grid lib (`package.json` has only `recharts`), no sharing/variables/kiosk. Flow panels depend on NetFlow which is **not in the local repo**.

## 4. Target design & architecture
```
Builder UI (react-grid-layout) ──PUT /dashboards/{id}──► FastAPI dashboards router
  │ widget registry (TS)                                    │
  │ <WidgetRenderer> per type                               ├─ Postgres: dashboards, dashboard_widgets,
  ▼                                                         │   dashboard_shares, kiosk_playlists
WidgetDataProvider ──GET /dashboards/{id}/widgets/{wid}/data──► resolver
   (applies vars+timerange)                                  ├─ ClickHouse (metric/top-N/heatmap)
Kiosk /kiosk/{token} ◄── SSE realtime ◄── Redis              └─ Postgres (device list/alert table/map)
```
- **Poller**: no changes — dashboards are a read/compose layer over existing metrics.
- **API**: new `dashboards` router. CRUD on dashboards; a **widget-data resolver** endpoint that maps `widget.type`+`config`+resolved `variables` to the right service (`metric_service`, `device_service`, `alert_service`) so the dashboard never queries ClickHouse directly from the browser.
- **Data model**: normalized tables (below); `layout`/`config`/`variables` as `JSONB`, matching `service_check.config` precedent.
- **Dashboard**: a `widgetRegistry` (id → component + default config + config-form), a `<GridCanvas>` (react-grid-layout), `<WidgetRenderer>`, a `<VariableBar>`, and a `<KioskRunner>`.

## 5. Data model & migrations
New `scripts/migrate-008-dashboards.sql` (Postgres), continuing the numbered-migration convention. Drop the unused `dashboard_configs`.

- **`dashboards`**: `id UUID PK`, `owner_id UUID FK users`, `name`, `slug`, `description`, `visibility ENUM(private|shared|public)` default private, `variables JSONB '[]'`, `default_time_range VARCHAR`, `is_default BOOL`, `created_at/updated_at`. Index `(owner_id)`, unique `(slug)`.
- **`dashboard_widgets`**: `id UUID PK`, `dashboard_id UUID FK ON DELETE CASCADE`, `type VARCHAR`, `title`, `config JSONB`, `layout JSONB` (`{x,y,w,h,minW,minH}`), `sort_order INT`. Index `(dashboard_id)`. (Embedding layout per-widget avoids index-drift bugs.)
- **`dashboard_shares`**: `dashboard_id`, `principal_type ENUM(user|role)`, `principal_id`, `permission ENUM(view|edit)`, PK `(dashboard_id, principal_type, principal_id)`.
- **`kiosk_playlists`**: `id UUID`, `name`, `owner_id`, `token VARCHAR UNIQUE` (random 32-byte), `dashboard_ids UUID[]`, `dwell_seconds INT default 30`, `enabled BOOL`, `expires_at TIMESTAMPTZ`. Index `(token)`.
- **ClickHouse**: none — all widgets read existing `ping_metrics*` aggregates via `metric_service`. Add a `top_n` query helper (ORDER BY metric DESC LIMIT N).
- **Migration notes**: idempotent `CREATE TABLE IF NOT EXISTS`; reuse `update_updated_at()` trigger; seed one shared "Operations Overview" board mirroring today's fixed page so existing users keep parity. Models added under `server/app/models/dashboard.py`.

## 6. API changes
New router `server/app/api/v1/dashboards.py`, prefix `/dashboards`, registered in `main.py`.
- `GET /dashboards` — list boards visible to user (owned + shared + public). Resp: `[{id,name,slug,visibility,owner,can_edit}]`.
- `POST /dashboards` — create (body `{name,description,visibility,variables}`).
- `GET /dashboards/{id}` — full board incl. ordered `widgets[]`.
- `PUT /dashboards/{id}` — upsert board + widgets array (layout + config) atomically. RBAC: owner/`edit` share/admin.
- `DELETE /dashboards/{id}`.
- `POST /dashboards/{id}/clone` — duplicate (templating).
- `PUT /dashboards/{id}/shares` — set shares (owner/admin only).
- `POST /dashboards/{id}/widgets/{wid}/data` — **resolver**: body `{variables, time_range}` → returns shaped series/rows for that widget type. Central place to enforce device-visibility filtering.
- `GET /dashboards/catalog` — widget catalog metadata (types, default config, required fields) for the builder.
- `POST /kiosk` / `GET /kiosk` / `DELETE /kiosk/{id}` — manage playlists (admin/operator).
- `GET /kiosk/run/{token}` — **token-auth, no JWT**: returns playlist + embedded board defs for the wall display (read-only, rate-limited).

## 7. Poller / collector changes
None. Dashboards compose over existing collected metrics. (When NetFlow lands live, the flow-panel resolver maps to its store; locally we register a `flow_panel` catalog entry returning HTTP 501 "data source unavailable" so the UI degrades gracefully.)

## 8. Dashboard changes
- **Routes** (`App.tsx`): `/d` (list), `/d/:slug` (view), `/d/:slug/edit` (builder), `/kiosk/:token` (full-screen, outside `Layout`/`Protected`).
- **New deps**: `react-grid-layout` (drag/resize), `@dnd-kit/core` for the catalog palette.
- **Components** (`components/dashboard/builder/`): `GridCanvas.tsx`, `WidgetRenderer.tsx`, `WidgetPalette.tsx`, `WidgetConfigPanel.tsx`, `VariableBar.tsx`, `widgetRegistry.ts`, `KioskRunner.tsx`, `useDashboard.ts` (react-query CRUD + debounced autosave).
- **Widget adapters**: thin wrappers binding `TimeSeriesChart`, `WorldMap`, `RingGauge`, `StatusHeatmap`, `AlertBanner`, plus new `DeviceListWidget`, `TopNWidget`, `SingleStatWidget`, `MarkdownWidget`.
- **Nav**: add "Dashboards" to `components/Layout.tsx`; make user-default board the home route when set.

## 9. Task breakdown
| # | Task | Area | Est (d) | Depends on |
|---|------|------|---------|-----------|
| 1 | Migration 008 + drop `dashboard_configs`; seed default board | db | 1.5 | — |
| 2 | SQLAlchemy models `dashboard.py` (+shares, kiosk) | api | 1 | 1 |
| 3 | Dashboards CRUD router + Pydantic schemas | api | 2 | 2 |
| 4 | RBAC/visibility helper + share resolution | api | 1.5 | 3 |
| 5 | Widget-data resolver (metric/top-N/list/alert/map) | api | 3 | 3 |
| 6 | Kiosk playlist + token endpoint (no-JWT, rate-limit) | api | 2 | 3 |
| 7 | Add `react-grid-layout`/`dnd-kit`; `widgetRegistry` | ui | 1.5 | — |
| 8 | `GridCanvas` + `WidgetRenderer` + autosave hook | ui | 3 | 7 |
| 9 | Widget palette + per-widget config panel | ui | 2.5 | 8 |
| 10 | Adapt existing charts + new list/topN/single-stat | ui | 3 | 7 |
| 11 | `VariableBar` (vars/filters) bound into resolver calls | ui | 2 | 5,8 |
| 12 | Dashboard list/view/share UI + clone | ui | 2 | 3 |
| 13 | Kiosk runner (rotation, SSE refresh, full-screen) | ui | 2.5 | 6,10 |
| 14 | Routes/nav wiring; default-board home | ui | 1 | 12 |
| 15 | Backend tests (CRUD, RBAC, resolver, kiosk token) | api | 2 | 5,6 |
| 16 | E2E (build→save→reload→kiosk) + perf test | ui | 2 | 13 |

## 10. Acceptance criteria
- [ ] A user can create a board, drag widgets from the palette, resize/move them, and changes persist across reload.
- [ ] All catalog widgets render real data: metric chart, device list, map, top-N, gauge, alert table, heatmap, single-stat, markdown.
- [ ] Dashboard variables (group/location/type/time-range) update all bound widgets without reload.
- [ ] Boards can be private, shared org-wide, or role-restricted; `viewer`/`read_only` cannot edit shared boards but can view.
- [ ] Cloning a board produces an independent editable copy.
- [ ] A kiosk playlist rotates ≥2 boards full-screen at a configurable dwell, refreshes live, and loads via tokened URL without login.
- [ ] Resolver enforces device visibility — users only see metrics for devices they may access.
- [ ] Flow-panel widget degrades gracefully (clear "data source unavailable") when NetFlow absent.

## 11. Test cases
| ID | Type | Precondition | Steps | Expected result |
|----|------|-------------|-------|-----------------|
| T1 | unit | resolver svc | Call resolver for `metric_chart` with range 24h | Returns auto-granularity series from `ping_metrics_5m` |
| T2 | unit | top-N helper | Request top-5 by RTT | Exactly 5 rows, DESC ordered |
| T3 | integration | admin token | POST then GET `/dashboards` | Board persisted with widgets+layout intact |
| T4 | integration | board exists | PUT widgets array reordered/resized | Layout+sort_order saved, no widget drift |
| T5 | integration | shared board, viewer token | PUT `/dashboards/{id}` | 403 Forbidden |
| T6 | integration | private board, other user | GET `/dashboards/{id}` | 404/403, not leaked in list |
| T7 | integration | role-share=ops | operator GET list | Board visible; read_only excluded |
| T8 | integration | clone | POST `/clone` | New id/slug, independent widgets |
| T9 | integration | kiosk playlist | GET `/kiosk/run/{token}` no JWT | 200 board payload; bad token → 404 |
| T10 | security | expired/disabled playlist | GET run token | 410/404; rate-limit on brute force |
| T11 | security | user lacks device X | resolver widget scoped to X | Empty/denied, no cross-tenant leak |
| T12 | e2e | logged in | Build board, add 4 widgets, reload | Same layout/data after reload |
| T13 | e2e | board w/ variables | Change "location" var | All bound widgets refetch & update |
| T14 | e2e | kiosk 3 boards | Open `/kiosk/:token` | Full-screen rotates every dwell; live updates via SSE |
| T15 | manual | NetFlow absent | Add flow_panel | "Data source unavailable" message, no crash |
| T16 | perf | 20-widget board | Load + 60s live | First paint <2.5s; resolver p95 <400ms |
| T17 | perf | 50 kiosk clients | Concurrent token loads | No ClickHouse saturation; cached responses |
| T18 | regression | existing fixed dash | Open legacy `/` route | `DashboardPage` unaffected |
| T19 | manual | autosave | Edit, kill tab mid-drag | Last debounced save retained, no corruption |
| T20 | integration | drop dashboard_configs | Run migration twice | Idempotent; no error on rerun |

## 12. Risks & rollout
- **Feature flag** `custom_dashboards` (org setting via existing `settings` API); legacy `DashboardPage` stays default home until GA, eliminating regression risk for current users.
- **Migration/back-compat**: `dashboard_configs` is unused, so dropping it is safe; gate behind `IF EXISTS`. Seed an "Operations Overview" board so flagged tenants get parity.
- **Performance**: browser never hits ClickHouse directly — the resolver caps series points, applies auto-granularity (`metric_service`), and Redis-caches kiosk payloads (TTL = dwell). Limit widgets/board (e.g. 30) and enforce minimum refresh interval to prevent wall displays from hammering ClickHouse.
- **Security**: kiosk tokens are high-entropy, expirable, revocable, rate-limited, and strictly read-only with no JWT surface; the resolver re-applies device visibility on every call (never trust client `variables`); validate widget `config` server-side against the catalog schema to prevent injection.
- **Phased rollout**: (1) API + models + resolver behind flag; (2) builder for `admin`/`operator` on internal tenants; (3) sharing + variables; (4) kiosk/video-wall; (5) GA — flip default home to user's default board, keep legacy route reachable for one release.
