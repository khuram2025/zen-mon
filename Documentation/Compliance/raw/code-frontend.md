# ZenPlus Dashboard — Frontend Conventions for a New "Compliance" Section

Raw investigation report. Everything below was read from the working tree at `/opt/zenplus/dashboard` on 2026-08-18 (branch `feat/udt-module`). All paths are absolute; line numbers refer to current file state.

---

## 1. Stack

From `/opt/zenplus/dashboard/package.json`:

| Concern | Choice | Version |
|---|---|---|
| Framework | React + ReactDOM | `^18.3.1` |
| Router | `react-router-dom` | `^6.28.0` (JSX `<Routes>/<Route>` tree, **not** the data router) |
| Server state | `@tanstack/react-query` | `^5.59.0` |
| Client state | `zustand` (+ `persist`) | `^5.0.1` — only for auth + theme |
| HTTP | `axios` | `^1.7.7` |
| Charts (dominant) | `recharts` | `^2.13.0` — used by 21 files incl. all new/polished pages |
| Charts (legacy) | `echarts` + `echarts-for-react` | `6.0.0` / `3.0.6` — only 6 files, all legacy (`TimeSeriesChart.tsx`, old `Dashboard.tsx`, `apm/ServiceMapPage.tsx`) |
| Diagrams | `@xyflow/react` `^12.11.0` (network maps only) |
| Styling | **Tailwind CSS** `^3.4.14`, `darkMode: 'class'`, CSS-variable tokens. No CSS modules, no styled-components |
| Headless UI | Radix primitives: `react-dialog`, `react-dropdown-menu`, `react-select`, `react-switch`, `react-tabs`, `react-toast`, `react-tooltip`, `react-popover`, `react-label`, `react-slot` |
| Variants | `class-variance-authority` (`cva`) + `clsx` + `tailwind-merge` via `cn()` |
| Icons | `lucide-react` `^0.454.0` — exclusively; sized `h-4 w-4` / `h-3.5 w-3.5` |
| Dates | `date-fns` `^4.1.0` (rarely used; most formatting is hand-rolled in `lib/utils.ts`) |
| PDF export | `jspdf` + `jspdf-autotable` (reports only) |

### Build & tooling

- `/opt/zenplus/dashboard/vite.config.ts` — alias `'@' → src` (line 9); dev proxy `'/api' → http://localhost:8000` (line 16); build to `dist`.
- Scripts (`package.json:6-13`): `build` = `tsc -b && vite build`; `smoke` = `smoke:routes` + `smoke:build`.
  **Deploy gotcha (from project memory):** production builds are done with `npx vite build` (not `npm run build`, which runs tsc); nginx serves `dist/`; run `chmod a+rX` after building.
- **Route smoke test**: `/opt/zenplus/dashboard/scripts/smoke-routes.mjs` parses `src/App.tsx` with regexes, asserts a `requiredRoutes` list (lines 53-90) exists, and asserts every `to:`/`path:` literal in `Layout.tsx` and `layout/Sidebar.tsx` resolves to a real route (lines 100-122). **Any new nav link must have a matching `<Route path=...>` in App.tsx or `npm run smoke:routes` fails.** New routes do *not* need to be added to `requiredRoutes` (that list is a floor, not a ceiling), but nav links are checked automatically.
- `tsconfig.app.json` provides the `@/*` path mapping for TS.
- `src/index.css` is a **legacy stale file** — `main.tsx` imports `./styles/globals.css` only. Do not touch `index.css`.

---

## 2. App entry, providers, router

`/opt/zenplus/dashboard/src/main.tsx` (whole file, 29 lines):

```tsx
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, refetchOnWindowFocus: false, retry: 1 },
  },
})
createRoot(...).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        <ToastViewport />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
```

`/opt/zenplus/dashboard/src/App.tsx` (250 lines) is the single route table:

- All pages are **statically imported named exports** (lines 4-74) *except* the reports pages which are `lazy()` (lines 76-84) with a `<Suspense fallback={<ReportTabFallback />}>` per route (lines 217-225). Convention: new sections use static imports unless they pull in heavy libs (jspdf).
- Auth guard: `Protected` (lines 96-100) checks `useAuth((s)=>s.token)` and `<Navigate to="/login" replace />`. There is **no per-route permission guard** — RBAC route protection is nav-hiding + in-page checks + backend 403s (see §5).
- The layout shell wraps everything at lines 131-138: `<Route path="/" element={<Protected><Layout /></Protected>}>` with all pages as children rendered into `Layout`'s `<Outlet />`.
- **Section-with-tabs pattern** (what Compliance should copy) — nested routes under a layout element, e.g. UDT at lines 172-181:

```tsx
<Route path="udt" element={<UdtLayout />}>
  <Route index element={<EndpointSearchPage />} />
  <Route path="ports" element={<SwitchPortsPage />} />
  ...
  <Route path="endpoints/:id" element={<EndpointDetailPage />} />
</Route>
```

APM does the same at lines 196-209 (`ApmLayout` with 11 children). Flat sections (Servers, lines 148-153) instead register sibling top-level routes (`servers`, `servers/inventory`, `servers/:id`, `server-agents`, …).
- 404: `<Route path="*" element={<NotFoundPage />} />` (line 245). Back-compat is handled with `<Navigate to=... replace />` redirect routes (lines 230-243).

### Layout shell

`/opt/zenplus/dashboard/src/components/Layout.tsx` (317 lines):

- `Layout()` (line 187): flex shell — `<Sidebar>` (fixed, collapsible rail 76px / wide 256px, persisted in `localStorage['zp-sidebar-pinned']`), mobile drawer overlay (lines 210-222), sticky `h-11` header (line 229) with breadcrumbs, global device search, theme toggle, alert center, update bell, user menu; content in `<main className="flex-1 overflow-y-auto p-5 animate-fade-in">` wrapping `<ErrorBoundary resetKey={pathname}><Outlet /></ErrorBoundary>` (lines 309-313).
- Breadcrumbs (lines 43-73) are derived automatically from the nav tree via `trailForLocation()` — **a correctly registered nav entry gets breadcrumbs for free**, including detail routes claimed via a nav node's `extra` prefixes.

---

## 3. Navigation registration

`/opt/zenplus/dashboard/src/components/layout/navigation.ts` is the single source of truth for the sidebar, the collapsed-rail flyouts, the nav search, and the header breadcrumbs.

Types (lines 59-86):

```ts
export type NavNode = {
  to: string                 // may carry a query string
  label: string
  icon: NavIcon
  hint?: string              // shown in flyout + search results
  end?: boolean              // exact-match only (landing pages)
  permission?: string        // hidden unless role grants this
  badge?: 'alerts'           // live count pill
  extra?: string[]           // extra path prefixes this node owns (detail routes)
  match?: (loc: { pathname: string; params: URLSearchParams }) => boolean
  children?: NavNode[]
}
export type NavGroup = { id: string; label: string; short: string; icon: NavIcon; items: NavNode[] }
```

`NAV_GROUPS` (lines 100-280) currently has 6 groups: `overview`, `monitoring`, `servers`, `apm`, `alerting`, `administration`. Each group renders as an accordion section (wide) or a rail icon with hover flyout (collapsed).

**Example entry with children** (NetFlow, lines 134-146) and **example with permission** (NetPath, lines 162-169):

```ts
{
  to: '/netpath',
  label: 'NetPath',
  icon: Waypoints,
  hint: 'Hop-by-hop path monitoring…',
  permission: 'netpath.view',
  end: false,
},
```

**Detail-route ownership**: `{ to: '/servers/inventory', label: 'Inventory', icon: Server, hint: 'Every monitored host', extra: ['/servers/'] }` (line 195) — `extra` makes `/servers/:id` highlight the Inventory row and produce a "… / Inventory / Detail" breadcrumb.

Helpers exported at lines 287-335: `isNodeActive`, `isBranchActive`, `groupForLocation`, `trailForLocation` — no changes needed when adding a section; just append to `NAV_GROUPS`.

**Permission filtering** happens in the Sidebar, `usePermittedGroups()` at `/opt/zenplus/dashboard/src/components/layout/Sidebar.tsx:61-74`: nodes with `permission` the user lacks are pruned recursively; a parent whose children are all pruned disappears; a group with zero items disappears.

Sidebar also polls two queries globally: `['alerts','stats']` → `GET /alerts/stats` every 15 s (badge count, lines 310-315) and `['system-update-status']` → `GET /system/update-status` (footer version, lines 317-321).

### To add a "Compliance" nav group (recipe)

Append to `NAV_GROUPS` (e.g. after the `servers` group, `navigation.ts:200`):

```ts
{
  id: 'compliance',
  label: 'Compliance',
  short: 'Comply',
  icon: ShieldCheck,          // already imported at line 44
  items: [
    { to: '/compliance', label: 'Overview', icon: Gauge, end: true, hint: 'Vulnerability posture at a glance', permission: 'compliance.view' },
    { to: '/compliance/vulnerabilities', label: 'Vulnerabilities', icon: Bug, hint: 'CVEs matched to your assets', permission: 'compliance.view', extra: ['/compliance/vulnerabilities/'] },
    { to: '/compliance/assets', label: 'Affected Assets', icon: Server, permission: 'compliance.view' },
    { to: '/compliance/eol', label: 'End of Life', icon: CalendarClock, permission: 'compliance.view' },
    { to: '/compliance/patches', label: 'Patches', icon: Download, permission: 'compliance.view' },
    { to: '/compliance/feeds', label: 'Feed Sync', icon: RefreshCw, permission: 'compliance.manage' },
  ],
},
```

(Alternatively add a single `{ to: '/compliance', children: [...] }` item inside an existing group; a new top-level group is what Servers/APM did and reads better for a flagship module.)

---

## 4. Routing recipe for the section

In `App.tsx`, mirroring UDT (lines 172-181):

```tsx
<Route path="compliance" element={<ComplianceLayout />}>
  <Route index element={<ComplianceOverviewPage />} />
  <Route path="vulnerabilities" element={<VulnerabilitiesPage />} />
  <Route path="vulnerabilities/:cveId" element={<VulnerabilityDetailPage />} />
  <Route path="assets" element={<AffectedAssetsPage />} />
  <Route path="eol" element={<EolPage />} />
  <Route path="patches" element={<PatchesPage />} />
  <Route path="feeds" element={<FeedSyncPage />} />
</Route>
```

Pages live in `/opt/zenplus/dashboard/src/pages/compliance/` (folder-per-module convention: `pages/udt/`, `pages/apm/`, `pages/servers/`, `pages/discovery/`, `pages/netpath/`, `pages/dashboards/`, `pages/reports/`).

### Section layout component (verbatim template)

`/opt/zenplus/dashboard/src/pages/udt/UdtLayout.tsx` (80 lines) and `/opt/zenplus/dashboard/src/pages/apm/ApmLayout.tsx` (69 lines) are the two exemplars. Structure:

```tsx
export function ComplianceLayout() {
  const { pathname } = useLocation()
  const isDetail = /^\/compliance\/vulnerabilities\/[^/]+/.test(pathname)  // detail pages own the full canvas
  return (
    <div className="space-y-4">
      {!isDetail && (
        <>
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <ShieldCheck className="h-6 w-6 text-primary" />
              Compliance &amp; Vulnerabilities
            </h1>
            <p className="mt-1 text-xs text-muted">One-line description of the module.</p>
          </div>
          <div className="-mb-px flex items-center gap-1 overflow-x-auto border-b border-border">
            {tabs.map((t) => (
              <NavLink key={t.to} to={t.to} end={t.end}
                className={({ isActive }) => cn(
                  'inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors',
                  isActive ? 'border-primary text-text' : 'border-transparent text-muted hover:text-text',
                )}>
                <t.icon className="h-4 w-4" />{t.label}
              </NavLink>
            ))}
          </div>
        </>
      )}
      <Outlet />
    </div>
  )
}
```

with `tabs: TabDef[] = [{ to, label, icon, end?: true-for-index }]` exactly as `UdtLayout.tsx:15-23`.

UDT additionally wires an **`(i)` KB-link** next to the `h1` (`UdtLayout.tsx:50`) using `/opt/zenplus/dashboard/src/components/udt/KbLink.tsx` — `KB_BASE = 'https://zentryc.com/kb/zenplus/udt'`, a `KB_ARTICLES` slug→path map, and `articleForPath()` picking the article per tab. New modules are expected to copy this pattern (per project memory, the UDT KB + i-icon convention was an explicit Aug 2026 feature); a Compliance version would use `https://zentryc.com/kb/zenplus/compliance`.

---

## 5. RBAC / permissions

- Store: `/opt/zenplus/dashboard/src/stores/auth.ts`. `User.permissions?: string[]` hydrated from `GET /auth/me`. Core helpers (lines 28-38):

```ts
export function hasPermission(user: User | null, permission: string): boolean {
  if (!user) return false
  if (!user.permissions) return user.role === 'admin'   // pre-RBAC sessions
  return user.permissions.includes('system.admin') || user.permissions.includes(permission)
}
export function useCan() {
  const user = useAuth((s) => s.user)
  return (permission: string) => hasPermission(user, permission)
}
```

- **Nav gating**: `NavNode.permission` (navigation.ts:69) + `usePermittedGroups` (Sidebar.tsx:61-74). Settings sub-tabs pass a permission to `settingsTab()` (navigation.ts:89-98, 262-274).
- **In-page gating** (buttons/actions, not routes): e.g. `/opt/zenplus/dashboard/src/pages/netpath/ProbesPage.tsx:40-41` — `const can = useCan(); const canManage = can('netpath.manage')` then conditionally rendering create/edit/delete affordances. `GeneralSettingsPage.tsx:49` and `components/access/*.tsx` do the same. There is **no** `<RequirePermission>` route wrapper anywhere; a user deep-linking to a hidden page gets backend 403s → per-panel `QueryError` states.
- **Server-side catalog** (the id namespace new permissions must join): `/opt/zenplus/server/app/core/permissions.py:16-84`, `PERMISSION_MODULES: list[(module_id, label, [(perm_id, label, description)])]`. Convention is `<module>.view` / `<module>.manage` (plus specials like `udt.view_users`, `alerts.acknowledge`, `discovery.run`, `reports.export`). `SUPERUSER_PERMISSION = "system.admin"` (line 94). A Compliance module would add e.g. `("compliance", "Compliance & Vulnerabilities", [("compliance.view", ...), ("compliance.manage", ...)])` and extend `_OPERATOR` (lines 96-104) / `LEGACY_ROLE_PERMISSIONS["viewer"]` (lines 108-119). The role editor UI (`components/access/RolesSection.tsx`) renders the catalog automatically from `GET` of the catalog endpoint — no frontend change needed for the new permission to appear there.

---

## 6. Design system

### 6.1 Theme tokens

`/opt/zenplus/dashboard/src/styles/globals.css` defines all colors as **space-separated RGB triplets** on `:root` (light, lines 5-30) overridden under `.dark` (lines 32-56):

`--bg, --surface, --surface2, --surface3, --border, --border-strong, --text, --text2, --muted, --muted2, --primary, --primary-hover, --success, --warning, --danger, --info, --accent`, plus 5 `--sidebar-*` tokens.

Dark palette (the product's default look): bg `7 10 16`, surface `13 18 27`, primary `99 179 255` (light blue), success `74 222 128`, warning `251 146 60`, danger `248 113 113`, info `56 189 248`, accent `192 132 252`.

`/opt/zenplus/dashboard/tailwind.config.js` maps every token: `bg`, `surface`, `surface2`, `surface3`, `border`, `border-strong`, `text`, `text2`, `muted`, `muted2`, `primary`, `primary-hover`, `success`, `warning`, `danger`, `info`, `accent`, `sidebar-*` — all as `rgb(var(--X) / <alpha-value>)` so `bg-primary/10`, `text-danger`, `border-warning/30` etc. work. Also:

- fonts: `font-sans` = Inter, `font-mono` = JetBrains Mono (lines 32-35)
- **compact type scale** (lines 36-44): root `html { font-size: 14px }` (globals.css:59); `text-sm` = 0.8125rem, `text-base` = 0.875rem, `text-2xl` = 1.375rem
- shadows: `shadow-card` / `shadow-card-dark` / `shadow-elevated` / `shadow-glow` (lines 45-50)

Theme switching: `/opt/zenplus/dashboard/src/stores/theme.ts` — zustand persist `'zp-theme'`, default `'dark'`, toggles `document.documentElement.classList` `dark`. Toggle button lives in the Layout header (Layout.tsx:257-259). **Rule: never hard-code hex colors for UI chrome — always the tokens.** Hex is only used for chart series palettes (see §8).

Animation/utility classes defined in globals.css: `.animate-fade-in` (0.15s rise), `.animate-slide-up`, `.animate-pulse-soft`, `.animate-shimmer` (skeleton), `.status-dot(-up/-down/-warn/-info/-idle/-live)` (lines 150-157), `.nav-collapse` grid-rows trick, custom scrollbars, `:focus-visible` outline in primary.

### 6.2 UI kit (`/opt/zenplus/dashboard/src/components/ui/`)

| Component | File | API notes |
|---|---|---|
| `Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter` | `Card.tsx` | Card = `rounded-lg border border-border bg-surface shadow-card dark:shadow-card-dark`. Header/Content pad `p-5` — most data pages override with `p-0`/`px-4` |
| `Table, THead, TBody, Tr, Th, Td` | `Table.tsx` | `Table` self-wraps in `overflow-auto`; `Tr` has built-in `hover:bg-surface2/60`; `Th` = `h-10 px-3 text-xs font-medium uppercase tracking-wider text-muted`; `Td` = `p-3` |
| `Badge` | `Badge.tsx` | cva variants: `default | success | warning | danger | info | outline` — pill `rounded-full border px-2 py-0.5 text-xs`, tinted `bg-X/10 border-X/30 text-X` |
| `Button` | `Button.tsx` | cva variants `default (bg-primary text-white) | destructive | outline | secondary | ghost | link`; sizes `default(h-9) | sm(h-8 text-xs) | lg | icon(h-9 w-9)`; `asChild` via Radix Slot; svg auto-sized `size-4` |
| `Tabs, TabsList, TabsTrigger, TabsContent` | `Tabs.tsx` | Radix tabs, "segmented" style (bg-surface2 list, active = bg-surface + shadow). Used for in-card tab switches; module-level tabs use the NavLink underline strip instead |
| `Dialog…` | `Dialog.tsx` | Radix; `DialogContent` = centered `max-w-lg` panel, overlay `bg-black/70 backdrop-blur-sm`; widen with `className="max-w-2xl"` etc. |
| `ConfirmDialog` | `ConfirmDialog.tsx` | `{ open, onOpenChange, title, description, confirmText, destructive, onConfirm }` |
| `Input`, `Textarea`, `Label`, `FormField`, `PasswordInput`, `Select…`, `Switch` | | `FormField` = label + hint wrapper; `Select` is the Radix select suite (`SelectTrigger/SelectValue/SelectContent/SelectItem`) |
| `Skeleton, SkeletonCard, SkeletonTable` | `Skeleton.tsx` | `animate-shimmer rounded-md` blocks |
| `StatusDot` + `deviceStatusKind()` | `StatusDot.tsx` | status `up|down|warn|info|idle`, optional `pulse` |
| `toast` + `ToastViewport` | `Toast.tsx` | zustand-based, `toast.success(title, description?)` / `.error` / `.info`, auto-dismiss 4.5 s, viewport bottom-right |

`cn()` = clsx + tailwind-merge, `/opt/zenplus/dashboard/src/lib/utils.ts:4-6`.

### 6.3 Table furniture kit (reuse this for every Compliance table)

`/opt/zenplus/dashboard/src/components/servers/tables.tsx` (222 lines) — despite living under `servers/`, it is generic and is the newest, best table toolkit:

- `usePagedRows<T>(rows, initialSize=50)` (lines 71-96) — client-side paging with clamp-on-filter; returns `{ pageRows, page, pageCount, pageSize, total, setPage, setPageSize, reset }`
- `TablePager` (lines 98-154) — footer with `25/50/100/250` page sizes
- `ExportCsvButton` / `toCsv` / `downloadCsv` (lines 156-206) — RFC-4180 quoting + UTF-8 BOM; exports all filtered rows
- `QueryError({ error, onRetry })` (lines 27-40) — "Could not load this data" with `apiErrorMessage()`; the codebase's explicit rule: *distinguish "we asked and there is nothing" from "we could not ask"*
- `EmptyState({ icon, title, hint })` (lines 42-56), `NoData`, `TableStateRow({ colSpan })` (full-width state row inside a table)
- Sorting: `sortableTh` class const, `sortIndicator(active, dir)`, `cmp(a,b)` (localeCompare with `numeric: true`) (lines 210-220)

### 6.4 Stat tiles / KPI cards (three sanctioned variants)

1. **Linkable gradient KpiCard** — `/opt/zenplus/dashboard/src/pages/dashboards/shared.tsx:124-151`. Card with a 0.5px gradient accent bar on top (`KPI_ACCENT` map keyed `success|danger|warning|info|accent|primary`, lines 115-122), uppercase 10.5px label, `text-2xl font-bold tabular-nums` value, gradient icon square, optional `foot`. Whole card is a `<Link>`. Used by all three team dashboards.
2. **Glow-ring KpiCard** — local to `/opt/zenplus/dashboard/src/pages/NetflowPage.tsx:1038-1072`: round icon in `ring-1` tinted circle with a colored glow shadow, tones `cyan|violet|amber|emerald|pink`. Rendered in `grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5` (line 662).
3. **Compact KpiTile** — `/opt/zenplus/dashboard/src/components/servers/shared.tsx:154-180`: icon square `bg-X/10 text-X`, tone `success|warning|danger|info|default`. Used by BaselinesPage; the natural choice for a Compliance KPI row. Also `PanelMiniStat` (ServerDetailPage.tsx:718-725) for tiny in-panel stat chips.

The absolute minimum (NcmPage.tsx:175-184): plain `Card > CardContent.py-3` with `text-xs text-muted` label + `text-2xl font-semibold` value.

### 6.5 Section/panel chrome

- `PanelHeader({ icon, title, hint, right })` — duplicated in `ServersPage.tsx:83-99`, `ServerDetailPage.tsx:698-716`, and as `SectionHeader` in `dashboards/shared.tsx:155-171`: flex row, `border-b border-border px-4 py-2.5`, `h3.text-sm.font-semibold.tracking-tight`, uppercase 10px `hint`, right-slot for actions.
- `TablePanel({ icon, title, hint, right, toolbar, children })` — `ServerDetailPage.tsx:774-791`: Card + PanelHeader + optional bordered toolbar strip + `CardContent px-0` body. **The canonical "filterable table in a card" wrapper.**
- `MetricChartCard` — `ServerDetailPage.tsx:750-772`: Card + PanelHeader + fixed-height `ResponsiveContainer` with Skeleton/`NoData` states.
- `InfoGrid(rows: [label, value][])` — `ServerDetailPage.tsx:793-804`: responsive definition-list of bordered mini-cells.
- `TabBar` + `CountPill` — `ServerDetailPage.tsx:140-209`: detail-page tab strip (sticky, `border-b-2` active underline in primary, count pills tinted danger/warning/muted), tab state in `?tab=` via `useSearchParams` with whitelist validation (lines 268-278).
- `SubNav` — `ServerDetailPage.tsx:213-245`: segmented control for sections nested inside a tab, deep-linked via `?sub=`.
- `FilterChip` — `NetflowPage.tsx:2634-2645`: `rounded-full border-primary/30 bg-primary/10 text-primary` pill with label, value, and an ✕ clear button. Active-filter chips render under the page title (NetflowPage.tsx:592-617) with a "Clear all" ghost pill.
- **Status chip row** — ServersPage.tsx:310-331: horizontal pill filters with counts from a facets endpoint; active = `border-primary/50 bg-primary/10 text-primary`.
- **Facet sidebar** — NcmPage.tsx:44-64 + 300-308: right column (`grid lg:grid-cols-[1fr_280px]`) of clickable facet bars with proportional fills; click toggles the filter.
- Meters: `UsageCell` (ServersPage.tsx:64-81, gradient thin bar + tabular % with warn/crit color switch), `PctBar` / `ShareBar` (dashboards/shared.tsx:180-208).

### 6.6 Empty / loading / error states — the house style

Every table body handles three states explicitly (see ServersPage.tsx:450-489 and SoftwareTab, ServerDetailPage.tsx:2087-2100):

1. `isLoading` → `Skeleton` rows (`<Td colSpan={N}><Skeleton className="h-10 w-full" /></Td>` ×5) or `SkeletonTable`
2. `isError` → centered icon + "Could not load X" + `apiErrorMessage(error)` (+ Retry via `QueryError`)
3. empty → icon + headline that distinguishes "no data yet" from "nothing matches the filters", plus a call-to-action button when it's a first-run state.

---

## 7. React Query conventions

### Query keys

Hierarchical arrays, module first: `['udt', 'endpoints', queryParams]`, `['netflow', 'overview', rangeKey]`, `['servers', 'list', {status, osType, ...}]`, `['servers', id, 'software']`, `['ncm', 'overview']`, `['tags']`, `['alerts','stats']`. Filters go into the key either as a params object (ServersPage.tsx:156) or a compact string `rangeKey` including every filter dimension (NetflowPage.tsx:410). Invalidation uses prefix matching: `qc.invalidateQueries({ queryKey: ['ncm'] })`, `['servers', id, 'compliance']`, etc. Shared keys are deliberately reused across pages so one fetch serves several consumers — `['tags']` (ServersPage.tsx:242-246, `staleTime: 5*60_000`), `['servers','latest-metrics']`. Project memory warns: **shared React Query keys must return the same shape everywhere (arrays)**.

A Compliance page should namespace everything `['compliance', ...]`.

### Polling intervals (observed distribution across src)

| Interval | Count | Typical use |
|---|---|---|
| `refetchInterval: 30_000` | 77 | default for lists/overviews (NCM overview, processes, services) |
| `15_000` | 46 | live health (alerts stats, server list, latest metrics) |
| `60_000` | 44 | slow-changing data (facets, software inventory, compliance results, heatmaps) |
| `isCustom ? false : pollMs(hours)` | 18+6 | time-ranged analytics |

`pollMs` (NetflowPage.tsx:271-274): `hours <= 6 → 15 s; <= 48 → 60 s; else 300 s`, with per-query floors (`pollMs(hours, 60_000)`). Custom absolute ranges never poll. Detail pages: 15-20 s for the entity, 30-60 s for sub-resources. Reasonable Compliance defaults: 60 s for CVE/EOL tables, 30 s for the overview, `refetchInterval: false` + manual refetch for feed-sync history with a "Sync now" mutation.

### Queries & mutations

- Fetch inline with axios: `queryFn: async () => (await api.get('/servers/facets')).data` — or through a module API wrapper object (`/opt/zenplus/dashboard/src/pages/udt/api.ts`: `export const udtApi = { async summary(): Promise<UdtSummary> { return (await api.get(`${base}/summary`)).data }, ... }`). UDT's wrapper + `pages/udt/types.ts` is the cleanest module structure and worth copying for Compliance (`pages/compliance/api.ts` + `types.ts`).
- Mutations always: `useMutation({ mutationFn, onSuccess: () => { toast.success('X updated'); qc.invalidateQueries({queryKey:['module',...]}) }, onError: (e) => toast.error('X failed', apiErrorMessage(e)) })` — see NcmPage.tsx:89-93, EndpointDetailPage.tsx:134-142, ComplianceTab (ServerDetailPage.tsx:2127-2134).
- `enabled: Boolean(id)` for param-dependent queries; `enabled: compareEnabled` for opt-in panels.
- `apiErrorMessage(e, fallback)` (`lib/utils.ts:112-118`) unwraps FastAPI `detail` (string | validation array | object).

### API client

`/opt/zenplus/dashboard/src/lib/api.ts` (36 lines): single axios instance `baseURL: '/api/v1'`, `timeout: 30_000`, bearer token injected from a module-level variable (kept in sync with zustand by `setApiToken`), 401 interceptor dispatches `window` event `'zp-auth-expired'` → auth store logs out (auth.ts:91-95). Long operations override timeout per call (`api.post('/ncm/run-scheduled', null, { timeout: 600000 })`, NcmPage.tsx:90). There is also `/opt/zenplus/dashboard/src/hooks/useSSE.ts` for server-sent events if live sync-progress streaming is wanted.

### URL state (critical convention)

Filters, sort, page, tab, and time range all live in the URL, not component state, so views are shareable and survive refresh:

- `useSearchParams` with a multi-key setter that batches updates and resets `page` on filter change — ServersPage.tsx:126-137 (`setQuery`/`setParam`), NetflowPage.tsx:311-329.
- Debounced search box → URL param (300 ms) — ServersPage.tsx:140-148.
- Tab state `?tab=` validated against a whitelist, `?sub=` cleared on tab change — ServerDetailPage.tsx:268-284.
- Time range: `useTimeRange()` from `/opt/zenplus/dashboard/src/components/TimeRangePicker.tsx:41-106` — `?range=1h|24h|7d|1M` or `?range=custom&from=ISO&to=ISO`; presets slide with wall clock bucketed to the minute so query keys stay stable; `<TimeRangePicker>` renders the pill strip + custom popover. Dashboards use the simpler `RangePills` (`dashboards/shared.tsx:49-71`, keys `1h|6h|24h|7d`).

---

## 8. Charts

- **Recharts is the standard** for new work (NetflowPage, ServerDetailPage, all team dashboards, reports). ECharts appears only in legacy files — do not use it for Compliance.
- Theme-awareness: axis/labels use CSS tokens, e.g. `tick: { fontSize: 10, fill: 'rgb(var(--muted))' }` (`chartAxis`, ServerDetailPage.tsx:736-748) and tooltip `chartTooltipStyle = { backgroundColor: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12, color: 'rgb(var(--text))' }` (`dashboards/shared.tsx:30-36`).
- Categorical series palette (validated for CVD + both themes): `CATEGORICAL = ['#0284c7', '#d97706', '#047857', '#db2777', '#7c3aed']` with the explicit rule that **status tokens (success/warning/danger/info) are reserved for state and never used as categorical series colors** (`dashboards/shared.tsx:15-28`). NetflowPage carries brighter dark-surface palettes (`TALKER_COLORS` etc., lines 149-152).
- Axis/tooltip time formatting helpers in `lib/utils.ts`: `timeAxisTickFormatter(rangeHours)` (lines 87-101), `timeTooltipLabelFormatter` (105-110), `timeTicks`/`axisRightPad` (37-48), `formatBps/formatBytes/formatBpsAxis/formatDuration/relativeTime`.
- Typical chart card: `MetricChartCard` (ServerDetailPage.tsx:750-772) or `DonutCard` (NetflowPage.tsx:1721+) — Card + PanelHeader + `ResponsiveContainer` + Skeleton/NoData fallbacks.
- Severity donut / heatmap precedents exist: `TrafficHeatmap` (NetflowPage.tsx:2194+, clickable hour×day grid that writes `?hour=&dow=` filters) and `components/charts/StatusHeatmap.tsx`.

---

## 9. Drill-down & detail conventions

There is **no slide-over drawer component** in the codebase (the only "drawer" is the mobile nav overlay, Layout.tsx:200-222). The house patterns for detail are:

1. **Expandable table row → inline detail panel** (the closest thing to a "detail drawer"): `NetflowSectionTable` (NetflowPage.tsx:1074-1290) keeps `expandedRow` state; the row gets `bg-primary/5` and a following full-width `<Tr><Td colSpan={N}>` renders a `DetailPanel` (`ConversationDetailPanel` at 1408+, `IpTrafficDetailPanel` at 1291+) containing `DetailMetric` tiles, `PillList`s, and action links ("filter to this", "open forensics"). This is the recommended pattern for a Compliance "CVE row → affected-asset breakdown" drawer.
2. **Dedicated detail route** for first-class entities: `/servers/:id`, `/udt/endpoints/:id`, `/apm/errors/:id`. Detail pages start with a `← Back` ghost button (`navigate(-1)`, EndpointDetailPage.tsx:159-161) or a breadcrumb line (ServersPage.tsx:266-272, NetflowPage.tsx:571-582), then a header block (icon square `h-11 w-11 rounded-lg bg-primary/10 text-primary`, `h1 text-xl font-semibold` + badges, `font-mono text-xs text-muted` identifier line, action buttons right), then an identity Card with `Field` label/value cells, then a responsive card grid of sub-tables (EndpointDetailPage.tsx:157-307 is the cleanest small exemplar).
3. **Radix Dialog** for create/edit forms and mid-weight detail (NcmPage's three dialogs, lines 328-586; BaselinesPage's baseline editor + results dialog). Widths via `DialogContent className="max-w-md|max-w-2xl"`.

Bulk-selection bars: checkbox column + floating action strip `rounded-lg border-primary/30 bg-primary/5 px-3 py-2` listing actions + clear (ServersPage.tsx:390-422; NcmPage.tsx:227-246). Row click navigates; interactive cells stop propagation (`onClick={(e) => e.stopPropagation()}`, ServersPage.tsx:499/537/541).

Zebra striping: `className={i % 2 === 0 && 'bg-surface2/10'}` on rows (ServersPage.tsx:496), `THead className="bg-surface2/40"`.

---

## 10. Existing compliance-adjacent frontend (build on, don't duplicate)

The product **already has a "software baseline compliance" mini-feature** in the Servers module:

- `/opt/zenplus/dashboard/src/pages/servers/BaselinesPage.tsx` (route `/server-baselines`, nav "Baselines" with hint "Config drift and compliance", navigation.ts:198). Manages baselines (required/prohibited software per OS/tag scope) via `GET/POST /server-baselines`, key `['server-baselines']`, 30 s poll. Status meta map at lines 42-47: `compliant→success, missing→danger, outdated→warning, prohibited→danger`.
- `ServerDetailPage` **already has a `ComplianceTab`** (ServerDetailPage.tsx:2120-2219): `GET /servers/{id}/compliance` → `{ items: ComplianceResult[]; summary: ComplianceSummary }` (60 s poll), `POST /servers/{id}/evaluate-baselines` mutation, `PanelMiniStat` summary strip (Compliant/Missing/Outdated/Prohibited), badge helper at lines 2139-2144. Tab registered at line 145 (`{ key: 'compliance', label: 'Compliance', icon: ClipboardCheck }`); the tab badge counts `missing + outdated + prohibited` (lines 389-391) and the Overview tab's `NeedsAttention` panel surfaces `complianceFailures`.
- `SoftwareTab` (ServerDetailPage.tsx:2014-2116): `GET /servers/{id}/software` → `{ items: ServerSoftware[] }` — the **installed-software inventory** a CVE matcher would join against. Columns: package_name, version, vendor, install_date, updated_at. Agent uploads inventory every ~6 h (empty-state copy, line 2098).
- Types in `/opt/zenplus/dashboard/src/types/servers.ts`: `Baseline`, `BaselineRule(Input)`, `BaselineMatchType`, `BaselineRuleType`, `ComplianceResult`, `ComplianceStatus` (`'compliant'|'missing'|'outdated'|'prohibited'`), `ComplianceSummary`, `ServerSoftware`, `Severity = 'info'|'warning'|'critical'`.

A new top-level Compliance section should cross-link to these (e.g. asset rows link to `/servers/:id?tab=compliance`, `/servers/:id?tab=inventory&sub=software`, `/devices/:id`), and the vocabulary (severity badges, status meta maps) should stay consistent with them.

Severity badge convention across the app: `critical → Badge danger`, `warning → warning`, `info → info` (ServerDetailPage.tsx:2207-2209, Layout.tsx:154-155).

---

## 11. Full page-skeleton recipe (Compliance list page)

Composite of the exemplars (ServersPage, NcmPage, SoftwareTab):

```tsx
// /opt/zenplus/dashboard/src/pages/compliance/VulnerabilitiesPage.tsx
export function VulnerabilitiesPage() {
  const [params, setParams] = useSearchParams()
  const severity = params.get('severity') || ''
  const q = params.get('search') || ''
  // ... setQuery batching + 300ms debounced search (ServersPage.tsx:126-148)

  const { data, isLoading, isError, error, refetch } = useQuery<VulnListResponse>({
    queryKey: ['compliance', 'vulnerabilities', { severity, q, page }],
    queryFn: async () => (await api.get('/compliance/vulnerabilities', { params: {...} })).data,
    refetchInterval: 60_000,
  })

  return (
    <div className="space-y-4">
      {/* KPI row */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiTile icon={Bug} label="Open CVEs" value={...} tone="danger" sub="12 critical" />
        ...
      </div>
      {/* severity chip filters with counts (ServersPage.tsx:310-331) */}
      {/* TablePanel: PanelHeader + toolbar (search Input + Select filters + ExportCsvButton) */}
      {/* Table with sortable Th, three-state body, expandable rows for the asset drill-down */}
      {/* TablePager */}
    </div>
  )
}
```

Wiring checklist for the whole section:

1. `src/pages/compliance/` — `ComplianceLayout.tsx` (tab strip, §4), pages, `api.ts`, `types.ts`, `helpers.tsx` (badge/meta maps), optional `KbLink.tsx` clone.
2. `App.tsx` — import block + nested `<Route path="compliance" element={<ComplianceLayout />}>` group.
3. `navigation.ts` — new `NavGroup` (§3) with `permission: 'compliance.view'` on items; icons already imported or add to the lucide import list at lines 1-55.
4. Backend: add the `compliance` module to `PERMISSION_MODULES` (`/opt/zenplus/server/app/core/permissions.py`) so `useCan('compliance.manage')` and nav gating have something to key on.
5. `npm run smoke` — will fail if any nav `to:` lacks a route.
6. Colors: severity=status tokens; categorical charts from `CATEGORICAL`; never hex for chrome.

---

## 12. Design language cheat-sheet (so it looks native)

- Page root: `<div className="space-y-4">`; content already padded by Layout's `p-5`. No max-width containers — full-bleed.
- Page title: `h1.flex.items-center.gap-2.text-2xl.font-semibold.tracking-tight` with a `h-5 w-5`/`h-6 w-6 text-primary` lucide icon; subtitle `p.text-xs.text-muted` (sometimes `mt-1`).
- Header row: `flex flex-wrap items-start justify-between gap-3`, actions right as `Button size="sm"` (primary action solid, secondary `variant="outline"`, each with a `h-3.5 w-3.5` icon).
- Cards: `Card` + `PanelHeader`; tables inside `CardContent className="px-0 pb-2 pt-1"`.
- Text sizes: table cells `text-sm`/`text-xs`, meta `text-[11px] text-muted`, micro-labels `text-[10px] font-semibold uppercase tracking-wider text-muted`, numbers always `tabular-nums`.
- Identifiers (IPs, MACs, versions, CVE ids): `font-mono text-xs`.
- Status: `Badge` variants or `StatusDot`; tinted chips are always `bg-TOKEN/10 border-TOKEN/30 text-TOKEN`.
- Grids: KPI `grid grid-cols-2 gap-3 md:grid-cols-4` (or `xl:grid-cols-5`); card grids `grid gap-4 lg:grid-cols-2`; sidebar layouts `grid gap-4 lg:grid-cols-[1fr_280px]`.
- Radii: cards/dialogs `rounded-lg` (10px-ish at 14px root), chips/pills `rounded-full`, inputs `rounded-md`.
- Hover affordances: rows `hover:bg-surface2/60` (built into `Tr`), clickable cards `hover:border-primary/40 transition`, links `text-primary hover:underline`.
- Entrance animation: `animate-fade-in` on the main pane (already applied by Layout) and on popovers/dialogs.
