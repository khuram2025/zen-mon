# 03 — UI/UX Specification

*Status: designed 2026-10-07; Phase 1 is built and has been through its review, its fix round and a re-check with a second round of fixes (2026-10-09). Routes and permissions are pinned in `00-INDEX.md`. Every building block named here exists in the dashboard today (see doc 01 §B.5). The module is assembled from the house design system so it looks native on day one. A clickable prototype with example data accompanies this set.*

*How to read this doc after Phase 1: the wireframes and rules are the design intent. Each page section ends with an **As built (P1)** block that says what shipped, in the names of the shipped components, and what moved or was left out. Where the two differ, the As built block is right. The filter parameters and responses are in `05-P1-API-CONTRACT.md`; the phase status is in `04`. Three decisions reshaped Phase 1: the Changes page moved from P2 to P1 (S1), the Settings page ships minimal, with only its Data & retention tab (S1), and the model, serial and vendor health checks cover network devices only (S3). The review fix round added five more that the As built blocks reflect: endpoint protection is its own software category and the security question has three answers, present, missing and unknown, the last with four reasons: no software list, a list cut at the agent's cap, a list still being processed, and no service list (X1, X9, X10); a banner on every Inventory page says when existing software lists are still being normalised (X2); an asset that cannot report an inventory by design has the freshness *Not reported by design* and is not "never" (X3); the Changes footnote prints when capture began and the flyout's "View all" link lands on exactly its total (X4); and runtimes are hidden by default (X5). Anything described as planned, or tagged with a phase (P2 to P6), is not in this release.*

**Design intent: an inventory people trust enough to make decisions from.**

- Summaries come first. Every number is a link to the hosts behind it, and every value shows where it came from and how old it is.
- The page answers the three questions operators ask most, each in three clicks or fewer:
  - *How many of X do we have?*
  - *Which hosts run version Y?*
  - *What changed?*
- Gaps such as missing or stale data are shown as a worklist, never hidden.

---

## 1. Navigation and shell

### 1.1 Sidebar group (`components/layout/navigation.ts`)

Inserted after the `servers` group:

```ts
{
  id: 'inventory',
  label: 'Inventory & Compliance',
  short: 'Inventory',
  icon: PackageSearch,
  items: [
    { to: '/inventory', label: 'Overview', icon: Gauge, end: true, permission: 'inventory.view',
      hint: 'Every OS, software title and hardware model at a glance' },
    { to: '/inventory/assets', label: 'Assets', icon: Boxes, permission: 'inventory.view',
      hint: 'Servers and network devices in one list' },
    { to: '/inventory/os', label: 'Operating Systems', icon: MonitorCog, permission: 'inventory.view',
      hint: 'OS and firmware releases, versions and builds' },
    { to: '/inventory/software', label: 'Software', icon: Package, permission: 'inventory.view',
      extra: ['/inventory/software/'], hint: 'Installed software catalog and versions' },
    { to: '/inventory/hardware', label: 'Hardware', icon: Cpu, permission: 'inventory.view',
      hint: 'Vendors, models, modules and server hardware' },
    { to: '/inventory/changes', label: 'Changes', icon: History, permission: 'inventory.view',
      hint: 'Installs, removals, upgrades and firmware changes' },
    // P4
    { to: '/inventory/lifecycle', label: 'Lifecycle', icon: CalendarClock, permission: 'inventory.view',
      hint: 'End-of-life and end-of-support exposure' },
    // P5+ — one node, children mirror the Compliance tab strip
    { to: '/compliance', label: 'Compliance', icon: ShieldCheck, permission: 'compliance.view',
      hint: 'Policies, exceptions and framework posture',
      children: [
        { to: '/compliance', label: 'Posture', icon: Gauge, end: true },
        { to: '/compliance/policies', label: 'Policies', icon: ListChecks },
        { to: '/compliance/exceptions', label: 'Exceptions', icon: ClipboardCheck },
        { to: '/compliance/frameworks', label: 'Frameworks', icon: Layers },
        // P6
        { to: '/compliance/vulnerabilities', label: 'Vulnerabilities', icon: Bug, extra: ['/compliance/vulnerabilities/'] },
        { to: '/compliance/patches', label: 'Remediation', icon: Wrench },
      ] },
    // Design: permission 'inventory.manage'. As built: 'inventory.view', read-only without manage (section 3.9).
    { to: '/inventory/settings', label: 'Inventory Settings', icon: SlidersHorizontal, permission: 'inventory.view',
      hint: 'Freshness targets and change retention' },
  ],
}
```

Rules:

- **Rows ship with their phase.** A route is never registered in navigation before its page exists.
- `usePermittedGroups` hides the whole group when the user holds none of the permissions.
- **Existing nav edits:**
  - The Servers group's "Inventory" row is relabelled **"All Servers"** (same `/servers/inventory` route) so that "Inventory" means one thing.
  - Servers → "Baselines" stays until P5. It then moves to Compliance → Policies, and `/server-baselines` redirects.
  - Reports → Inventory stays and gains the new sections (P2).
- **Collapsed rail:** "Inventory" fits under the icon (the first design said "Assets"; the row below it is named Assets). Hints appear in the flyout and the sidebar search.

**As built (P1).** The group is registered with `id: 'inventory'`, label *Inventory & Compliance*, short label *Inventory*, icon `PackageSearch`, between `servers` and `apm`, with the seven rows that have pages: Overview, Assets, Operating Systems, Software (with `extra: ['/inventory/software/']`), Hardware, Changes and **Inventory Settings** (hint *Freshness targets and change retention*). Every row, Settings included, declares `inventory.view`: the Settings page opens for every role that can see Inventory and renders its values read-only without `inventory.manage` (section 3.9), which keeps the sidebar row, the tab and the route guard in step with `navAccess.ts`. Lifecycle, Compliance and its children are not registered; their rows ship with their phases. The Servers row is relabelled **All Servers** (route and `extra: ['/servers/']` unchanged), and so are the *View all servers* link and the heading of the Servers page. Every row declares its permission, `navAccess.ts` drives the sidebar, the tab strip and the route guard, and `scripts/test-nav-access.mjs` plus `scripts/test-inventory-ui.mjs` enforce coverage.

### 1.2 Routes (`App.tsx`) and access gate

```tsx
<Route element={<InventoryAccessGate />}>          {/* path-less, like NetflowAccessGate */}
  <Route path="inventory" element={<InventoryLayout />}>
    <Route index element={<InventoryOverviewPage />} />
    <Route path="assets" element={<InventoryAssetsPage />} />
    <Route path="os" element={<InventoryOsPage />} />
    <Route path="software" element={<InventorySoftwarePage />} />
    <Route path="software/:productKey" element={<InventoryProductPage />} />
    <Route path="hardware" element={<InventoryHardwarePage />} />
    <Route path="changes" element={<InventoryChangesPage />} />
    <Route path="lifecycle" element={<InventoryLifecyclePage />} />   {/* P4 */}
    <Route path="settings" element={<InventorySettingsPage />} />     {/* read-only without inventory.manage */}
  </Route>
</Route>
```

- `InventoryAccessGate` uses `useCan()`. Without `inventory.view` it renders a designed denied state: the icon, "You don't have access to Inventory", the permission name, and "Ask an administrator to add **inventory.view** to your role". There is no blank page or redirect loop.
- Every route is added to `requiredRoutes` in `dashboard/scripts/smoke-routes.mjs`.
- **Companion fix (P1):** extend the smoke script, or add a new `scripts/test-inventory-ui.mjs` that runs in the dashboard CI job, so that **every `navigation.ts` `to:` resolves to a declared route**. Today it doesn't (doc 01 G12).

**As built (P1).** `InventoryAccessGate` (`components/inventory/InventoryAccess.tsx`) wraps the module as a path-less layout route. Behind it, the product's own `RouteGate` already shows its *no access* screen (which names `inventory.view` and links to the start page) before the module is reached; for any page of the Inventory group it says *Your role does not include "Inventory & Compliance"* (`deniedWording.ts`), because a bare *Overview* would read as the home dashboard. The gate's denied state is defence in depth with the same wording rule; it renders nothing and sends no request while permissions are unknown. The eight pages are lazy chunks (`App.tsx`), so the shell stays on screen while one loads. `smoke-routes.mjs` lists all eight routes, `/inventory/software/:productKey` included, and `test-inventory-ui.mjs` validates that **every `to:` in `navigation.ts` resolves to a declared route** (mutation-checked), closing the gap named in doc 01 G12. The dashboard CI job (`.github/workflows/development.yml`) runs `test-inventory-ui.mjs`, `test-inventory-os-hardware-changes.mjs`, `test-inventory-software-settings.mjs` and `test-inventory-touchpoints.mjs`; the server job runs the contract generator's `--check` and `check_contract.py`.

### 1.3 `InventoryLayout`

This follows `UdtLayout` exactly:

- an `h1` with the `PackageSearch` icon, "Inventory", `KbLink`, and the subtitle "Every operating system, software title and hardware model across servers and network devices";
- a `NavLink` underline tab strip mirroring the sidebar rows;
- `<Outlet />`.

The product detail route owns the full canvas with no tab strip, and shows a back link "← Software".

A **meta-line** under the tabs is visible on every Inventory page:

> Inventory as of 14:05 · **97%** of assets reported within freshness targets · 6 stale · Lifecycle data as of 2026-10-01 *(P4)*

Clicking "6 stale" opens Assets filtered `freshness=stale`.

**As built (P1).** `InventoryLayout` renders the `h1` with the icon, *Inventory* and an `InventoryKbLink` whose article follows the screen (`kbArticleForPath`), the subtitle, a tab strip built from the *same* filtered nav rows as the sidebar (so a role gets only the tabs it may open; the active tab scrolls into view on a phone) and the meta-line, then the **data-status banner** and the page. The product page drops the heading and strip and shows *← Software* and the meta-line; its breadcrumb in the header names the product (`breadcrumbDetail.ts`), not the generic *Detail*. `InventoryMetaLine` reads: *Inventory as of 14:05 · 94.4% fresh · 15 aging · 6 stale · 2 never reported · 8 not reported by design*, where each count is a `DrillLink` to the Assets list with that `freshness`. The percentage is of the assets **that can report** (not-reported-by-design assets are out of the denominator) and is honest at the edges (`fmtPct`, `fmtShare`): 99.95 reads 99.9%, a share below 0.05 percent reads *<0.1%*, and 100.0% or 0.0% is printed only when the share is exactly 100 or 0, so a single missing asset is never rounded away. The line says *every asset that can report did so within its target* when nothing is aging, stale or missing, shows *may be out of date* with a retry when a refresh failed, and carries a **Showing assets tagged …** badge for a scoped user. It is the same request as the Overview (`['inventory','summary']`). The **banner** (`DataStatusBanner`) reads *Normalising existing software lists; figures are incomplete* with the number of software rows and assets still to process at the last check and *The figures refresh when processing finishes*; it renders while `data_status.normalising` is true, and the summary is polled every 15 s instead of every 60 s while it is. The visible banner is not a live region: a separate screen-reader-only `role="status"` region carries the sentence and, once, the completion *Software lists are processed; figures are complete*. When the backlog clears the page refetches its data queries once (`useRefreshWhenNormalised`). The count is the number of rows the normalise loop will pick up, so a row it never selects never keeps the banner on screen (X10). The *Lifecycle data as of* part waits for P4 (planned).

## 2. Cross-cutting conventions

| Convention | Rule |
|---|---|
| **Drill-down contract** | Every KPI, chart segment, bar, table count and health item is a `<Link>` to a list with the equivalent URL filters, e.g. Overview "Windows Server 2019 · 23" → `/inventory/assets?os_family=windows&os_release=2019`. A test asserts that the drilled list's `total` equals the clicked number (doc 04, T-UI2) |
| **URL state** | All filters, sorts, pages and tabs live in search params (UDT/RUM `useUrlState` pattern), so every view is shareable and back-button safe |
| **Freshness** | `FreshnessBadge`: *Fresh* (success-fg), *Aging* (warning-fg), *Stale* (danger-fg), *Never* (muted), always with the age in words ("6 h ago"). The tooltip separates *last seen* (reachability) from *last inventory* (data) |
| **Source badges** | `SourceBadge`: Agent · SNMP · Template · ENTITY-MIB · WinRM · SSH (P3). Shown next to every identity value in the flyout ("FortiOS 7.2.5, from FortiGate template, 4 min ago") |
| **Unknown is visible** | "Unknown" is a real slice and row: `text-muted`, hatched fill in charts, always counted, always drillable |
| **Numbers** | `tabular-nums`; `fmtCount`; versions, builds and serials in `font-mono text-xs`; percentages to one decimal place |
| **Color** | Series use `CATEGORICAL` (`pages/dashboards/shared.tsx`). Status tokens are reserved for state (freshness, health, and lifecycle stage in P4). Color is never the only signal: every slice also has a text label or legend count |
| **Export** | `ExportCsvButton` on every table, shown only when `useCan('inventory.export')`. It calls the server-side `/inventory/export` with the current filters (full result, not just the page), and a toast reads "Export started — 2,410 rows" |
| **Links to existing pages** | Asset names link to `/servers/:id` or `/devices/:id` only if the user holds `servers.view` / `devices.view`; otherwise plain text with a tooltip |
| **Charts** | Recharts, `chartTooltipStyle`, a faint grid. Every chart has a **"View as table"** toggle (accessibility, and copying numbers) |
| **Density** | 13 px table text, row height 36 px, sticky table headers, 50 rows per page by default (`PAGE_SIZES`) |

**As built (P1).**

- **Drill-down contract.** `DrillLink` (a filter object to an Assets address, used by the meta-line and the Hardware summary) and `DrillTarget` (a typed `Drill` to an assets, changes or page address) serialise every link from `filters.ts`, the single source of the vocabulary. Every link carries `data-drill-count`, and the test that clicks them compares it with the `total` of the list it opens (T-UI2: the server half is `test_inventory_api.py`; the browser half is `test-inventory-ui.mjs` and its two siblings `test-inventory-os-hardware-changes.mjs` and `test-inventory-software-settings.mjs`, which render the pages from the golden fixtures and compare each link's `data-drill-count` with the number of assets the same filter selects in the fixture fleet). A number that is not a count of machines (components in a chassis, the *of N* of the endpoint-protection line, *Older* in build currency, *Behind latest*) is plain text by design; `05` section 4 lists them. A donut slice is a click target that navigates (it cannot be opened in a new tab); the legend rows beside it are real links and the keyboard path.
- **URL state.** `useInventoryUrlState` (assets, changes), `useSoftwareUrlState` (software) and per-page equivalents read and write the API's own parameter names. A value the page does not understand is dropped and reported in an *Ignored in the address* notice, never silently applied. A page number past the end shows *Page N is past the end* with a link to the last page. Every write goes against the live address (`urlState.createUrlWriter`), so a control changed while another write was waiting is never undone. The search boxes of Assets, Changes, Software and a product's Installed tab share one state machine (`searchBoxModel.ts`, `useDebouncedSearch`): the text is committed to the address after a short pause, on Enter or on blur, a character typed between a commit and its echo is never lost, and a facet clicked while a commit is waiting survives it. After a filter chip is removed or *Clear all* is pressed, focus moves to the neighbouring chip or to the search box (`useChipFocus`).
- **Freshness and source.** `FreshnessBadge` prints *Fresh*, *Aging*, *Stale*, *Never* or **Not reported by design** (dashed outline, hollow dot, no age) and, except for those last two, the age (*Aging · 6h ago*); its tooltip keeps *Last inventory (data)* apart from *Last seen (reachability)*. *Not reported by design* (API value `not_applicable`) is for an asset that cannot send an inventory and never has: a server with no inventory stamp whose collection mode is not the agent, a device with no identity stamp that is ping-only or managed through a controller. It is listed, counted and drillable (`freshness=not_applicable`), is not *Never*, is out of the fresh percentage and the all-clear, and the four identity health checks skip it. `SourceBadge` shows Agent, SNMP, SNMP profile, Template, Template (old), ENTITY-MIB, sysDescr, Managed, WinRM, SSH, WMI, Controller, ICMP or *Unknown source*. Contrast of the *Never* badge and the unknown-source chip was measured and set to text2 on surface2.
- **Charts.** The family donut and the KPI sparkline are small custom SVG components (`Donut`, `Sparkline`) because a slice or a point has to lead somewhere; the changes-per-day chart is a stacked bar. Each has a *View as table* toggle (`ViewToggle`) and an `aria-label` summary. Wide tables sit in a `ScrollShadow` (a scroll region with an edge fade and a hint).
- **Export.** The button is `ExportDatasetButton` (OS, Hardware, Changes, product installations) or the page's own button (Assets, Software); it exists only with `inventory.export` and calls `GET /inventory/export` with the current filters, so the file is the whole result, not the page. The toast reads *Export started* with the row count, or *Export started, cut at the row limit* past 200,000 rows.
- **Layout preferences.** The Assets column choice, density and the open/closed state of the filter pane are stored per browser (`useAssetPrefs`).

## 3. Pages

### 3.1 `/inventory`: Overview

```
┌ Inventory ─────────────────────────────────────────────────────────── (i) ─┐
│ Overview | Assets | Operating Systems | Software | Hardware | Changes │ … │
│ Inventory as of 14:05 · 97% fresh · 6 stale                     [Export ▾] │
├ KPI row (grid-cols-2 md:grid-cols-3 xl:grid-cols-6) ───────────────────────┤
│ ┌Assets────┐ ┌OS releases┐ ┌Software──┐ ┌HW models─┐ ┌Fresh─────┐ ┌Changes─┐│
│ │ 412      │ │ 31        │ │ 1,840    │ │ 57       │ │ 94.4%    │ │ 431    ││
│ │128 srv · │ │ 9 families│ │39.1k inst│ │12 vendors│ │ 6 stale  │ │ 7 days ││
│ │284 net   │ │           │ │          │ │          │ │ ▁▂▃▅▆▇   │ │ ▃▅▂▇   ││
│ └──────────┘ └───────────┘ └──────────┘ └──────────┘ └──────────┘ └────────┘│
├ row 2 (lg:grid-cols-2) ────────────────────────────────────────────────────┤
│ ┌ Operating systems — servers ────────┐ ┌ Network OS & firmware ──────────┐ │
│ │ donut by family → legend by release │ │ per vendor OS: stacked bar of   │ │
│ │ Windows Server 2022 ████████ 61     │ │ versions (FortiOS 7.4.4 ▇▇ 7.2.8 │ │
│ │ Windows Server 2019 █████    38     │ │ ▇ 7.2.5 ▇ …), count, # versions │ │
│ │ Windows Server 2016 ██       17     │ │ PAN-OS · IOS XE · TMOS · Junos  │ │
│ │ Windows 11 / 10    █         7      │ │ (click segment → assets list)   │ │
│ │ Unknown            ░         5      │ │                                 │ │
│ └─────────────────────────────────────┘ └─────────────────────────────────┘ │
├ row 3 ─────────────────────────────────────────────────────────────────────┤
│ ┌ Top software (by hosts) ────────────┐ ┌ Version sprawl ─────────────────┐ │
│ │ Google Chrome   120 hosts · 14 vers │ │ products with most distinct     │ │
│ │ 7-Zip           118 · 6             │ │ versions — the upgrade backlog  │ │
│ │ FortiClient      97 · 3             │ │ Chrome 14 · Java 8 · Notepad++ 7│ │
│ │ [Hide runtimes & components ✓]      │ │ (mini version-distribution bars)│ │
│ └─────────────────────────────────────┘ └─────────────────────────────────┘ │
├ row 4 ─────────────────────────────────────────────────────────────────────┤
│ ┌ Hardware ───────────────────────────┐ ┌ Recent changes ─────────────────┐ │
│ │ top models: FortiGate-200F 14,      │ │ 14:02 web-01  Chrome 151 → 152  │ │
│ │ C9300-48P 22, PA-3220 4, …          │ │ 13:40 fw-dc1  FortiOS 7.2.5→7.2.8│ │
│ │ servers: CPU / RAM buckets          │ │ 11:15 app-03  + TeamViewer 15.5 │ │
│ └─────────────────────────────────────┘ │ [View all changes →]            │ │
│ ┌ Inventory health ──────────────────────────────────────────────────────┐ │
│ │ ⚠ 22 devices missing model · 31 missing serial · 9 missing OS version  │ │
│ │ ⚠ 8 unknown vendor (unmapped sysObjectID) · 6 stale · 2 never reported │ │
│ │ ⚠ 1 software list truncated at 1,000 · 37 software titles unmatched    │ │
│ │ (each item → filtered worklist; all-clear state: "Inventory is complete")│ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────┘
```

- **Data:** `GET /inventory/summary`, key `['inventory','summary']`, `refetchInterval: 60_000`. Trends come from `GET /inventory/trends` (unscoped users only).
- **KPI cards:** `KpiCard` (dashboards/shared.tsx). Every card links: Assets → `/inventory/assets`, OS releases → `/inventory/os`, and so on.
- **Endpoint-protection coverage** (category `endpoint-protection` or a running Defender service; the design said "from category `security`", which proved far too broad, see X1): a small line under the Software card reads "Endpoint protection on 121 of 128 servers with a complete software list". The gap of 7 links to the hosts without it; servers whose list is missing, cut or not yet normalised are a separate *unknown* line. This is the most-requested inventory insight in the research, available before any policy engine exists.
- **First-run empty state:** with no agents and no SNMP devices, the page shows three onboarding cards: "Install the Windows agent" (→ `/server-agents`), "Add network devices" (→ `/discovery`) and "Inventory fills in automatically". With partial data, each panel shows its own empty state, and the rest of the page still renders.
- **P4 additions:** an "EOL / EOL ≤ 180 d" KPI, and lifecycle colouring on the OS and firmware bars.

**As built (P1).**

- **Six cards** (`InventoryKpiCard`, grid `2 / 3 / 6` columns): *Assets*, *OS releases*, *Software titles*, *Hardware models*, *Fresh inventory* and *Changes*. A card has one main link and its sub-figures as separate links (no nested anchors). *Fresh inventory* shows the share of the assets **that can report** with the note *of N assets that can report*, and a *not reported by design* part. The Software card carries the **endpoint-protection** line (`AgentCoverageLine`), repeated under *Top software*: *Endpoint protection on N of M servers with a complete software list (75.0%) · K without*, where *N* and *K* are links (`security_agent=present` and `missing`) and *M* is text (covered plus without); a second line, *3 servers unknown: no complete software or service list to judge*, links to `security_agent=unknown`; its tooltip names every reason and promises none of them will pass (an agent that reports no software, such as Linux today, stays unknown). The three figures add up to every server, and with no complete list at all the line says *Endpoint protection cannot be assessed yet*. Endpoint protection is a software row of the category `endpoint-protection` or a running `WinDefend` or `Sense` service; *without* needs a complete, processed software list **and** a service list, and every other state is unknown, never *without*.
- **Sparklines (S4).** Only on *Assets*, *Software titles* and *Fresh inventory*, and *Changes per day*, and only when `GET /inventory/trends` returns at least **three days** with data (the *Changes per day* line leaves out today, whose count is still being written, so it needs three complete days before today and appears one day after the others). Below that there is no line and no placeholder. A restricted caller never gets one, even if something is cached from an earlier session; the page says *Trend lines are available to unrestricted users: they are fleet-wide.* `trends_enabled = false` also removes them.
- **Panels**, each with its own query, skeleton and failure state: *Operating systems: servers* (`Donut` plus a legend of releases, hatched *Unknown*, *View as table*), *Network OS and firmware* (a bar of versions per product, five segments and *other*, the unnamed *firmware not identified* kept), *Top software* (by hosts) and *Version sprawl* (most distinct versions, with a note that says when only the N most installed of M products were ranked: `software.sprawl_basis`), *Hardware* (most common network models, and servers by installed memory), *Recent changes* (the last eight, *N in 7 days*, *View all changes*), and *Inventory health*.
- **Inventory health** lists the **eight** predicates in a fixed order as links to `health=<key>`: missing OS version, missing model, missing serial number, unknown vendor (the last three for network devices only, S3), stale, never reported (*assets that can report but never have*), truncated software list and no endpoint protection (servers with a complete list only), followed by a ninth, informational line, *assets not reported by design* (`freshness=not_applicable`), which never counts against the all-clear. A predicate at zero is a tick with a sentence, not a link. When all eight are zero and the normaliser has nothing left to do the panel reads *Inventory is complete: every asset that can report did so within its targets and nothing is missing.* The unmatched-software item waits for the P2 catalog (planned).
- **First run.** With no assets and no scope, three `OnboardingCards`: *Install the Windows agent* (to `/server-agents`, with `servers.view`), *Add network devices* (to `/discovery`, with `discovery.view`) and *Inventory fills in automatically*. A scoped user with nothing in scope sees *No assets match your scope*.
- Not built: the EOL KPI and lifecycle colouring (P4, planned); the *Export ▾* menu in the wireframe (each page exports its own list).

### 3.2 `/inventory/assets`: unified asset list and flyout

```
┌ Facets (w-64, collapsible) ┐ ┌ [search name / IP / serial / model]   [Saved views ▾] [Export] ┐
│ Kind        Server 128     │ │ Name ▲        Kind  IP            Vendor     Model        OS / Firmware      │
│             Network 284    │ │ fw-dc1        🛡    10.0.0.1      Fortinet   FortiGate-200F FortiOS 7.2.8    │
│ OS family   Windows 121    │ │ web-01        🖥    10.1.2.11     Dell*      R650*        Win Server 2022    │
│             FortiOS  38    │ │ core-sw-01    ⇄     10.0.0.2      Cisco      C9300-48P    IOS XE 17.9.4a    │
│             IOS XE   44    │ │   … Serial · Site/Location · Sources · Last inventory (FreshnessBadge)       │
│ Vendor …  Model …          │ │                                                                          │
│ Site / Location …  Tags …  │ │ 1–50 of 412                                         ◀ 1 2 3 … 9 ▶       │
│ Source  Agent · SNMP · …   │ └──────────────────────────────────────────────────────────────────────────┘
│ Freshness Fresh · Aging ·  │
│   Stale · Never            │   * P3 (agent hardware identity)
│ Health  missing model …    │
└────────────────────────────┘
```

- **Data:** `GET /inventory/assets` + `/assets/facets`, server-side paging, sorting and filtering. Facet counts reflect the other active filters, CSAM-style. A column chooser and density toggle persist per user in localStorage (house rule for layout preferences).
- **Flyout** (Radix `Dialog` as a right sheet, 480 px; opens on row click; URL `?asset=device:<id>`):
  - **Header:** name, kind icon, status dot, IP, "Open device page →" (permission-gated).
  - **Identity:** vendor, model, serial, OS product/release/version/build. Each carries a `SourceBadge` and age.
  - **Server body:** CPU, RAM, disks, top 10 software with "View all on server page", and the endpoint-protection pill (present with the products and services that count, none found, or unknown with the reason).
  - **Device body:** ENTITY-MIB modules (class, model, serial), with HA role and mode from template values.
  - **Recent changes (10):** "View all → `/inventory/changes?asset=…`".
  - **Meta (P2):** owner, department, criticality, lifecycle state, warranty end; editable with `inventory.manage`; "Mark reviewed" stamps `last_reviewed_at` (CIS 1.1).
- There is **no separate asset detail page**. The existing device and server pages remain the detail pages, which avoids the duplicate-page anti-pattern.

**As built (P1).**

- **List.** `AssetToolbar` (search, active-filter chips, column menu, density, *Filters*, *Export*), `FacetPanel` and `AssetTable` with `TablePager` (page sizes 25, 50, 100, 250). The facet pane is a sticky 16 rem column from 1024 px and a left `Sheet` drawer below, with a footer button *Show N assets*. Facet groups: Kind, OS family, OS / firmware, Vendor, Model, Site / Location, Tags, Source, Freshness (Fresh, Aging, Stale, Never, *Not reported by design*) and Inventory health; counts follow the other active filters, *Unknown* is always shown, never behind *Show more*, and values that differ only in case (`Cisco`, `CISCO`) are one value, labelled with the most common spelling. The endpoint-protection (`security_agent`), CPU, memory, disk, virtualization and component filters have no facet; they are reached from the Overview and Hardware pages and appear as chips.
- **Columns.** Name (locked), Kind, IP address, Vendor, Model, OS / Firmware, Serial, Site / Location, Sources, Last inventory, Type, Status, Tags and Last seen; six are on by default (Name, IP address, Vendor, Model, OS / Firmware, Last inventory). The name is a roving-tabindex link: arrow keys move between rows, `Enter` opens the flyout, `/` focuses the search.
- **Flyout** (`AssetFlyout` on `Sheet`, a Radix dialog): header with kind, status, IP, freshness and the links *Open server page* / *Open device page* / *Open SNMP device page* (locked, naming the permission, without `servers.view` / `devices.view`); an asset that is *Not reported by design* says so in one sentence; *Identity* (nine fields, each with `SourceBadge` and age, plus a *Raw evidence* disclosure); for a server *Hardware* (CPU, memory, disks, virtualization, architecture) and *Software* (count and hidden count, the **endpoint-protection pill** with the products and running services that count or the reason it is unknown (*No software list has been reported for this server*; *The software list was cut off, so protection cannot be judged*; *The software list is still being processed*; *Built-in Windows Defender can only be seen through the service list, and this server has not reported one*; any other reason string gets a generic sentence), the truncated-list note, up to ten titles with security and endpoint-protection products first, linking to their product pages, and *View all N on the server page*); for a device *Device* (profile, group, type, sysObjectID), *High availability* (mode, role, peer, sync), and *Modules* grouped by class and model, each expandable to components and serials; *Recent changes* (ten, with *View all N* to the Changes page filtered to the asset and carrying `since=first_change_at`, so the landed total equals N) and *Placement*. An unknown or out-of-scope address shows *Asset not found* with one body for both.
- Not built: saved views (P2, planned), the *Meta* section (owner, department, criticality, lifecycle state, warranty; P2, planned), server manufacturer and model (P3, planned).

### 3.3 `/inventory/os`: Operating systems and firmware

- **Segmented control:** **All · Servers · Network** (`?kind=`).
- **Left (lg:col-span-2):** a family → release → version tree table with expandable rows. Each row shows:
  - an asset-count bar (`ShareBar`);
  - the number of distinct versions;
  - the latest observed version;
  - in P4, a lifecycle stage pill and the EOL date.
- **Right:** a family donut, plus a **"Build currency"** card per release. Example: "Windows Server 2022: 41 on the latest observed build 20348.2700, 14 on N-1, 6 older". This uses observed-latest until P4 supplies feed-latest.
- **Click** any row, version or slice to open Assets with `os_family`/`os_release`/`os_version` filters.
- **Firmware rows** show the raw string on hover ("v7.2.5,build1517,230606 (GA.F)"), so normalisation is transparent.

**As built (P1).** `?kind=` accepts `server` or `device` (anything else is *All* and is reported). The tree is `OsTree` (family, product, release, version with `ShareBar`-style bars, distinct versions and *Latest observed*; sortable headers; *Expand all* / *Collapse all*; a *Reported as* column shows the raw firmware string while a version row with one is visible). Counts link to the Assets list, *Unknown* is a row. Nodes group on lower-cased product and release and on the version equivalence key, so `7.2` and `7.2.0` are one version and the label is the longer spelling. The side column holds `OsFamilyPanel` (donut of the five largest families with the remaining families folded into one grey *Other (N families)* slice, hatched *Unknown*, legend, *View as table*) and `OsCurrencyPanel` (**Build currency**: per release the assets on the latest observed version and on N-1 as links, *older* (two or more versions behind) and *no usable version* as text, with the note that *latest observed* is the newest version on your own assets, not the vendor's, and that a later phase (lifecycle data, planned for P4) would replace it). The comparison uses the family's own version rules and refuses to order across classic IOS trains. Export: one row per version.

### 3.4 `/inventory/software`: Software catalog

```
┌ [search product / publisher]  Category ▾  Publisher ▾  [✓ Hide runtimes, drivers & updates]  [Export] ┐
│ Product ▲            Publisher        Category   Hosts   Versions (distribution)        Latest   First seen │
│ Google Chrome        Google           Browser     120   ▇▇▇▇▅▃▂▁ 14                   152.0.8  2025-11-02 │
│ 7-Zip                Igor Pavlov      Utility     118   ▇▇▇▂ 6                          25.01    2025-10-14 │
│ FortiClient          Fortinet         Security     97   ▇▇▁ 3                           7.4.3    2026-01-20 │
│ Microsoft VC++ Redist Microsoft       Runtime     128   ▇▇▅▃▂ 11  (shown when unhidden) …                 │
│ ⓘ 37 titles unmatched — Review in catalog (P2)                                                         │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Data:** `GET /inventory/software` (server-paged) and `/software/publishers`.
- **Versions cell:** a mini stacked bar of hosts per version, ordered by comparator (newest on the left), with a tooltip listing the top versions.
- **Hidden categories:** the default-hidden ones (`update, driver, os-component, runtime` as built; the design listed the first three) are counted in a footer: "312 hidden entries · Show". The wireframe's switch label is the design's; the built label follows the configured list.

**As built (P1).** Toolbar: search (every word must appear in the product name or publisher), multi-select Category (including **Endpoint protection**), Publisher (with product counts), the hide switch and *Export*. The switch is on by default and its label is built from the categories hidden by default (`hidden_categories` of the catalog response), so with the shipped settings it reads **Hide updates, drivers, OS components and runtimes**, follows whatever an administrator saves in Inventory Settings, counts them (*Hide 5 categories hidden by default*) beyond four and is neutral before the list has arrived; the Settings sentence and the footer use the same list. Columns: Product, Publisher, Category, Hosts, Installs, Versions (`SoftwareVersionBar`, no percentages; versions are counted by equivalence key), Latest, First seen; sortable server-side, with the default `hosts` descending. *First seen* is the day Inventory first saw the row; a value at or before the capture start (`history_start`, fetched once and cached for 30 minutes by `useSoftwareHistoryStart`) reads *before <capture date>* with a tooltip that gives the earliest date known. Under the table: *N products (M installations) in hidden categories not listed. Show all N* / *Hide them again*, and *N titles unmatched* (a link to `unmatched=true`; editing rules waits for P2, planned). The empty catalog says *Normalising existing software lists; this page fills in over the next minutes* while `data_status.normalising` is true; with no pending rows it says the list arrives with the Windows agent's first inventory report and that Linux servers and servers without the agent have no software list in this release. `SoftwareVersionBar` replaces the shared `VersionDistributionBar` here because the latter's shares are of the sum of per-version hosts (`02` section 14).

### 3.5 `/inventory/software/:productKey`: Product page

- **Header:** product name, publisher, category, normalisation status pill ("Normalized" / "Unmatched"), and the KPIs Hosts, Versions, Latest, First seen.
- **Tabs (`?tab=`):**
  - **Versions:** a horizontal bar per version (hosts), sorted by comparator, with an N / N-1 / older grouping. Click a version to filter Installations.
  - **Installed on:** a server table with version, architecture, source (HKLM/HKCU/dpkg), install date, first seen and last report, plus CSV export.
  - **Timeline** (P2 UI, data from P0): installs, removals and version changes for this product over 90 days, with a daily stacked bar and an event list below.
  - **Evidence:** the raw names and publishers aliased into this product, and which rule matched. A "**Report inaccuracy**" action (manage) opens the rule editor pre-filled.
- **P6:** a Vulnerabilities tab appears when the feed is active.

**As built (P1).** `ProductHeader` shows the name, the normalisation pill (*Normalized*, *Publisher only*, *Unmatched*), publisher, category, the product key and four figures: *Hosts*, *Versions*, *Latest seen* (links) and *Behind latest* (text), with *First seen before <capture date>* when the product was already there. The tab is `?tab=versions|installed|timeline|evidence`. **Versions** groups by version scheme (Windows and other installers, Debian/Ubuntu packages, RPM packages), one bar per version with an **N / N-1 / Older / Unordered** pill (*Older* means two or more versions behind the newest seen, in the pill and in its tooltip), and *No version reported* as a row of its own, addressed as the version `__none__`; a version links to the servers on exactly that version, and two spellings of one version (`7.2`, `7.2.0`) are one row. **Installed on** is the installation list (Server, Package, Version, Architecture, Source, Installed, First seen, Last report) with version filter, search, sort, paging and CSV; the source column reads HKLM, HKCU or, for 32-bit programs on 64-bit Windows, *Hklm_wow6432*. **Timeline** is built in P1 (the data and the Changes list exist): 7, 30 or 90 days of this product's events, with the Changes page's pairing rule. **Evidence** is read-only: raw names and publishers, hosts and installations each, and how the normaliser reads each today. The *Report inaccuracy* action waits for the P2 rule editor (planned). An unknown or out-of-scope product is *Product not found*.

### 3.6 `/inventory/hardware`: Hardware

- **Network hardware:**
  - a vendor → model table with count, distinct firmware versions, serial coverage % and module count;
  - a vendor bar chart;
  - a "**Models by firmware**" matrix (model × release heat cells) that shows mixed-firmware fleets at a glance.
- **Modules and components:** `device_entities` grouped by class (chassis, module, PSU, fan, transceiver) and model, with counts. Clicking a group lists devices plus entity serials (RMA planning).
- **Server hardware:**
  - CPU model distribution, memory buckets (≤ 8, 16, 32, 64, 128, > 128 GB), physical disk count and size buckets;
  - in P3: manufacturer/model, virtualisation split (physical / VMware / Hyper-V / KVM) and BIOS age.
- **P2:** warranty-end buckets (from `inventory_asset_meta`), shown when any value exists.

**As built (P1).** Panels in order: *Network hardware* (`HardwareNetworkPanel`: vendor rows expanding to model rows with Devices, Firmware versions, **Serial coverage** as a percentage over the devices that can report, plus the words *All serials known* / *Some serials missing* / *Many serials missing*, and Modules; *N missing* links to `health=missing_serial` with the row's filters, *N not reported by design* links to `freshness=not_applicable`; a vendor-less row and a model-less row are `vendor=unknown` and `model=unknown`), *Models by firmware* (the matrix of one vendor, `?vendor=`, every cell a number and a link, a legend of shade steps), *Modules and components* (ENTITY-MIB groups by class and model; the devices count links, component counts are text) and *Server hardware* (CPU models, installed memory, physical disks, virtualization). Model counts always mean *known* models ("12 models plus 3 devices with no model"). The percentage is rounded to one decimal by the server but never rounds up to 100.0% while a serial number is missing (and never down to 0.0% while one is known), so the words and the exact *N missing* link always agree. The *Virtualization* section says that manufacturer, model, serial number, BIOS and physical-or-virtual are not reported by the Windows agent in this release instead of drawing an empty chart. The warranty buckets wait for P2 (planned).

### 3.7 `/inventory/changes`: Change history

- **Stats strip (7 d, switchable 24 h / 7 d / 30 d / 90 d):** Installed · Removed · Upgraded · Downgraded · OS changes · Firmware changes · Hardware changes. Each is a filter chip.
- **Daily stacked bar** by category.
- **Event list** grouped by day. Each row shows time, asset (link), category icon, and a sentence: "**Google Chrome** upgraded 151.0.7922.110 → 152.0.8001.12", "**FortiOS** changed 7.2.5 → 7.2.8 *(firmware)*", "**TeamViewer 15.5.3** installed".
- **Filters:** category, action, asset kind, asset, product, search; all in the URL.
- **Data-integrity footnote:** "History starts 2026-10-xx (when change capture was enabled). Earlier changes were not recorded." This is honest, and auditors ask for it.
- **Pairing and caveats (from the Phase 0 review):** pair an `added` and a `removed` event with the same `subject_key` on the same asset in the same transaction (`observed_at`) as an **upgrade or downgrade** by comparator, because a product whose display name embeds its version (VC++ redistributables, Java) is stored as a new row. Show events whose `detail.snapshot_truncated` is true with a "list was capped at 1,000 titles" note, and the daily `(change budget exceeded)` marker as a distinct row. Scoped users' feeds must be built asset-first (doc 02 section 14).

**As built (P1).** The page moved from P2 to P1 because the data has accumulated since Phase 0.

- **Stats strip** (`ChangesStatsStrip`): the window switch **24 h / 7 d / 30 d / 90 d** (a custom range from a link reads *Custom*) and **eight chips**: All changes, Installed, Removed, Upgraded, Downgraded, OS changes, Firmware changes and Hardware changes. A chip sets `category`, `action` and `label` and keeps the window; its number equals the list it opens. *Upgraded* and *Downgraded* count in-place version changes, and a note adds the pairs recorded as an installation plus a removal.
- **Changes per day** (`ChangesChart`): a stacked bar by category with *View as table*; every day links to that day.
- **List** (`ChangesList`): grouped by day in the browser's time zone, one sentence per change (*Google Chrome upgraded 151.0.7922.110 → 152.0.8001.12*, *FortiOS changed 7.2.5 → 7.2.8*, *Serial number changed … → …*), a small category label, the asset name opening its flyout. `ChangesToolbar`: search, the kind switch *All assets / Servers / Network devices*, removable chips for asset, product and window.
- **Pairing (client side, from the API's `pair`).** One `added` and one `removed` of the same asset, product and instant become **one row** (*Java 8 Update 401 upgraded 8.0.351 → 8.0.401 (replaces Java 8 Update 351) on dc1-dc-01*, with *1 change, 2 events*); the count line says *445 events, 49 rows on this page (1 pair of installation and removal shown as one upgrade)*. The label *upgrade* or *downgrade* comes only from the API, never from the client. A half whose partner is on another page says so.
- **Caveats shown.** *The list was capped at 1,000 titles when this was recorded, so this title may have been cut off rather than removed*; the daily *Change budget exceeded for …: further changes of that report were not recorded* marker as a dashed row labelled *Gap* (an italic line in the Overview and flyout lists); a deleted asset as plain text (*This asset has been deleted; its history is kept*).
- **Integrity footnote** (`ChangesFootnote`): *History starts Wed 7 Oct 2026, when change capture began. Changes older than 400 days are removed.* Both facts come from the response itself: `history_start` is when capture began on this installation (the time migration 157 was applied, stored by migration 158; install-wide), not the day of the first recorded change, and `retention_days` is the retention setting. When the retention window has moved past the capture start the text says *Change capture began … The history you can see therefore starts …*; until the date is known it says *History starts when change capture was enabled*. For a scoped user a second sentence says changes of since-deleted assets are not shown. When the server cannot use the browser's time zone a line says *Days are grouped in UTC* (`window.tz_fallback`). `since` or `until` outside 1970 to 2100 or a malformed `tz` is a `422`.

### 3.8 `/inventory/lifecycle` (P4)

- **KPIs:** End of life now · EOL ≤ 90 d · ≤ 180 d · ≤ 365 d · Supported · Unknown (always shown).
- **Table:** a row per product/release in use, with assets, stage pill, EOAS/EOL/EOES dates (product-specific labels), latest version, source + "as of", and an override indicator.
- **Timeline:** horizontal bars from today to EOL for releases in use, with a today marker.
- **Hardware:** models with end-of-sale and end-of-support dates (override or CSV import), plus a "Suggested / Assigned / No data" workflow, as in SolarWinds NCM.
- **Manage:** add or edit an override (reason, expiry), and import a CSV.

### 3.9 `/inventory/settings` (view; changing needs manage)

Tabs:

- **Software catalog** (P2): rules table (priority, pattern, product, category, hits) with a draft-rule **preview** panel showing matched raw names and host counts before saving; products (rename, category, hide, business purpose); publishers (aliases); the *Unmatched* queue sorted by host count.
- **Lifecycle** (P4): overrides and hardware EoS import.
- **Data & retention:** freshness thresholds per kind, change-retention days, snapshot hour, default-hidden categories.

Every save writes an audit entry, and the UI shows a confirmation toast that names the effect ("Rule saved — 14 titles re-grouped").

**As built (P1).** The page has one tab, **Data & retention**, in three sections: *Freshness targets* (servers fresh for 24 h, network devices fresh for 2 h, stale after 7 days, with a live sentence restating the rule), *History and trends* (keep change history for 400 days, daily snapshot at hour 0 and the *Show trends on the Overview* switch) and *Software catalog defaults* (the hidden categories). The form (`SettingsEditor`, loaded on demand) validates against the limits the API returns, offers *Save changes*, *Discard* and *Restore defaults*, and the toast names the effect of the change. The row and the route need only `inventory.view`: a role with `inventory.view` and without `inventory.manage` (the Viewer, Read Only and the three team roles) gets the same values read-only, with the note *You can read these settings but not change them* naming `inventory.manage`. A number typed as `024` is not a change, numbers are normalised on blur, and a submit with nothing to change says *Nothing to change*. The hidden-category list includes **Endpoint protection** and defaults to Update, Driver, OS component and Runtime; the page shows the effective values and `adjusted` lists any value the server reduced (for example the server freshness target when the stale limit is lowered). Writers are serialised, and every save is audited: if the audit entry cannot be stored the save is refused (`503`) and nothing changes. The *Software catalog* and *Lifecycle* tabs are P2 and P4 (planned).

### 3.10 Compliance pages (P5–P6, summary)

- **`/compliance` Posture:**
  - KPIs: compliant %, failing assets, open exceptions (expiring ≤ 30 d), policies, and EOL without exception;
  - a framework coverage card (CIS 1/2, CSF ID.AM, ISO, PCI), where each control is pass/fail/unknown with drill-down to evidence.
- **`/compliance/policies`:** list plus editor (rule-type picker → scope → parameters → preview "would fail 7 assets") and per-policy results.
- **`/compliance/exceptions`:** requested / active / expired register. Approval needs `compliance.approve`. Export is the auditor's register.
- **`/compliance/frameworks`:** control catalog mapped to policies, with an evidence pack export.
- **P6:** pages as specified in `Documentation/Compliance/05-UI-UX-SPEC.md` §3+ (CVE workbench, remediation).

## 4. Touchpoints in existing pages

| Page | Change | Phase |
|---|---|---|
| `DevicesPage.tsx` | Add **Vendor, Model, OS version** columns (optional; as built they are **off by default**, see the as-built text below) and vendor/OS filters. The data is already in the `Device` type (79-110) | P1 |
| `DeviceDetailPage.tsx` `InventoryConfigCard` (1535) | Firmware value links to `/inventory/os?os_family=…&os_release=…`; adds a "Changes" mini-list | P1 |
| `servers/detail/tabs/SystemTab.tsx` `SoftwareSection` (815) | Each package shows its normalised product as a link to `/inventory/software/:key` with "N hosts fleet-wide"; honours the `truncated` flag with a banner; "Changes" sub-section | P1 / P2 |
| `ServerTeamDashboard` / Server Overview | OS mix tile links to `/inventory/os?kind=server` | P1 |
| Servers nav "Inventory" | Relabelled "All Servers" | P1 |
| `/server-baselines` | Redirects to `/compliance/policies` after migration | P5 |
| `ReportsPage` Inventory tab | Shows the new sections | P2 |

**As built (P1, task 1.9).**

- **Servers nav row** relabelled *All Servers* (with the shell, task 1.3); the page heading and breadcrumb read *All Servers*, the Server Overview's button reads *View all servers* and its link *All servers →*.
- **Devices list** (`DevicesPage.tsx`, helpers in `components/devices/deviceInventory.ts`): **Vendor**, **Model** and **OS version** columns, **off by default for everyone** (the three together pushed Status past the edge of the table at 1280 px; a saved column choice keeps its layout) and switched on from the *Columns* menu, sortable; Vendor and OS version filters in *More filters*. The filters are address parameters (`vendor` and `os_version`, with `unknown` for none; there is no Model filter on the page) and `GET /devices` accepts `vendor`, `model` and `os_version` (exact, case-insensitive, `unknown` selects devices with no value; the tag scope is untouched). The request sends every filter the API applies, so the 200 devices loaded are the right 200, plus `facets=true`: the Vendor and OS version menus then count over all devices the caller may see, and a menu count lands on a list of exactly that many. The OS version shown, filtered and exported is `os_version_display` (the normalised Inventory version where one exists, so PAN-OS and F5 devices no longer read *Not reported*, else the device's own value). The CSV export (built in the browser from the list on screen) keeps its 13 legacy columns in place and appends `vendor`, `model` and `os_version` at the end, whether or not the columns are shown, with the formula-injection guard. The Vendor and Model menus count the device's **own** stored columns while Inventory's vendor facet counts the **normalised** vendor (aliases and spellings merged), so the same vendor can have different counts on the two pages; the OS version menu counts `os_version_display`. The KPI tiles, charts and the *Filtered* line of the page still describe the loaded devices (at most 200), and that line says which of its filters the API applied and which the browser did; a notice says *first N of M* when the list is cut short. The empty state follows what narrows the list (clear the filters, change the search, or run discovery for an empty estate), and the status, type, location and paging controls carry accessible names.
- **Device detail** (`InventoryConfigCard`): the *OS / version* value, or the *Firmware* value when the firmware is what identified the device, links to `/inventory/assets?kind=device&os_family=…&os_product=…&os_release=…` (the Assets list of devices on the same release), using the normalised family and release from the Inventory flyout request. This differs from the design above, which named `/inventory/os`: that page honours only `?kind=`, so a link there would have been silently unfiltered. Without `inventory.view`, or when the asset is a server's SNMP twin or out of scope, the value stays plain text. The design's *Changes* mini-list on this card is P2 (planned).
- **Server detail, System → Software**: each package with a catalog product links to `/inventory/software/<product_key>` (underlined, so it reads as a link; `GET /servers/{id}/software` returns `product_key`, `category` and `agent_truncated` per row and answers `404` outside the caller's tag scope), and a banner says when the agent cut its list at its cap or this response stopped at its request limit. The empty hint says the Windows agent uploads the list and that Linux servers and servers without the agent do not report software. The design's *N hosts fleet-wide* text and the *Changes* sub-section are P2 (planned).
- **Server Overview**, *By operating system*: a header link *OS inventory →* to `/inventory/os?kind=server`, shown only with `inventory.view`. The rows keep their own link to `/servers/inventory?os=` because their counts are the servers' `os_type`.

## 5. States

| State | Treatment |
|---|---|
| Loading | `SkeletonCard` for each KPI and panel, `SkeletonTable` for lists. Layout never shifts |
| Forbidden | `QueryBoundary` forbidden state, or `InventoryAccessGate` |
| Partial failure | Each panel fails independently with `QueryFailure` and a retry; the rest renders |
| Stale data | `QueryBoundary` stale banner when a refetch fails but cached data exists |
| Empty (no data yet) | Onboarding cards (§3.1); lists say what will appear and how to make it appear |
| Scoped user | A badge in the meta-line: "Showing assets tagged *dc-east, pci*"; trends show "available to unrestricted users" |
| All-clear | Inventory health: "Inventory is complete — every asset reported within targets", shown in success-fg |

**As built (P1).** Every list page also has *past the end* (the page number is beyond the last page; the API answers `200` with no items), *no match* (active filters, with *Clear all filters*) and *ignored in the address* states. Forbidden is the product's `RouteGate` screen, with the module gate behind it. A refetch failure with cached data keeps the table and shows a compact failure with a retry and *Showing the last list that loaded; it may be out of date*. A scoped user's empty result says *Your role limits Inventory to assets tagged …, and none carry those tags yet*. While `data_status.normalising` is true, every page carries the banner of section 1.3, the catalog says it is being built instead of empty, endpoint protection counts unprocessed servers as unknown, and the Overview shows no all-clear. A page whose code chunk no longer exists after an update (a stale tab) is reloaded once automatically, at most once in five minutes (the guard is written to `sessionStorage` before the reload; without storage nothing reloads), and after that the product's error boundary shows a notice with a *Reload page* button instead of *Try again*; `InventoryLayout` has a boundary of its own, so the tab strip stays usable.

## 6. Accessibility and responsiveness

- Tables are real `<table>`s with `scope` headers. Keyboard: `/` focuses search, `Esc` closes the flyout, arrow keys move between rows, `Enter` opens the flyout.
- Charts have a "View as table" toggle, `aria-label` summaries ("Windows Server 2022: 61 assets, 48 percent"), and no color-only encoding.
- Contrast uses the `*-fg` tokens, which are ratio-checked by `test-netflow-foundation.mjs`.
- The layout works at 1280 px and degrades to 768 px: the facet pane becomes a drawer, and KPI grids go 6 → 3 → 2 columns.

**As built (P1).** The keyboard rules hold on the Assets page (`/`, arrows, `Enter`, `Esc` returning focus to the opening row), `/` also focuses the search on Software and Changes, every chart has *View as table*, and the three node scripts render the pages from the golden fixtures and assert on what they print (labels, table structure, links). Contrast was measured for the badges, the matrix shades (light at least 5.2:1, dark at least 4.8:1) and the *Never* chip. The layout was checked at 1440 and 390 px in both themes, and the Devices page and the Assets list at 1280 px; the strip scrolls horizontally on a phone and no page scrolls sideways. Filter chips return focus to a neighbouring chip or the search box when removed, the count links of the Hosts, Installs and version tables carry accessible names (the product and the number, not the digits alone), and a donut slice is a click target while its legend row is the link. **Known gap:** support for forced-colors mode (Windows High Contrast) is incomplete: the data bars and the selected tab can lose their contrast there. The numbers of the read-only settings form keep full opacity on a muted fill; the disabled switch and category checkboxes still use the shared 50 percent dimming, which is low-contrast (a shared-component matter).

## 7. New shared components (`components/inventory/`)

The design named seven components. Phase 1 shipped them, with the names below, and the pieces that proved necessary while building the pages. Pure logic lives in `*Model.ts` files that the node scripts import directly (no React, router or API), so the rules that matter (what a link filters by, how pairs and generations are decided, what a number means) are tested without a browser.

| Component | Purpose |
|---|---|
| `InventoryAccess` (`InventoryAccessGate`, `useInventoryAccess`) | The path-less layout route and hook that give one denied state without `inventory.view`, expressed with `navAccess.ts` |
| `InventoryMetaLine`, `DataStatusBanner` | Data age and coverage line under the tab strip, and the *Normalising existing software lists* banner under it (section 1.3); `useInventoryMeta` (page folder) shares the summary query with the Overview and polls every 15 s while normalising |
| `InventoryKbLink` | The (i) icon linking to the article for the screen; the key table `KB_ARTICLES` is documented in `inventory-kb/README.md` |
| `InventoryEmpty` | The three empty states: no data yet, no match for the filters, nothing in scope |
| `FreshnessBadge`, `SourceBadge` | Fresh / aging / stale / never / not reported by design with the age; Agent / SNMP / Template / ENTITY-MIB / … chip |
| `DrillLink`, `DrillTarget` | Serialise a filter object or a typed `Drill` into an Assets, Changes or page address and stamp `data-drill-count`; used by every number |
| `InventoryKpiCard`, `Sparkline`, `Donut`, `ViewToggle`, `ScrollShadow`, `PanelCard` (`Panel`, `QueryPanel`) | Overview building blocks: card with sub-links, sparkline and donut as SVG with real links, chart/table toggle, panel with its own loading and failure state |
| `FacetPanel`, `AssetToolbar`, `AssetTable`, `Sheet`, `AssetFlyout` | The Assets page: facets (generalised from the `ServersPage` status chips), toolbar and chips, table with roving focus, the Radix-based sheet used by the flyout and the filter drawer, and the flyout itself |
| `OsTree`, `OsFamilyPanel`, `OsCurrencyPanel`, `OsKindSwitch`, `QueryGate` (in `OsQueryGate.tsx`), `ExportDatasetButton` (in `OsExportButton.tsx`) | The Operating Systems page and the export button the OS, Hardware, Changes and installation lists share |
| `HardwareNetworkPanel`, `HardwareMatrixPanel`, `HardwareModulesPanel`, `HardwareServersPanel` | The Hardware page |
| `ChangesStatsStrip`, `ChangesChart`, `ChangesToolbar`, `ChangesList`, `ChangesFootnote` | The Changes page (and the product Timeline tab, which reuses the list) |
| `SoftwareToolbar`, `SoftwareFilterSelect`, `SoftwareSortHeader`, `SoftwareTable`, `SoftwareVersionBar`, `SoftwareFooter`, `useSoftwareUrlState`, `useSoftwareHistoryStart` | The software catalog (the hook reads the capture start for *before <date>*) |
| `ProductHeader`, `ProductVersionsTab`, `ProductInstalledTab`, `ProductTimelineTab`, `ProductEvidenceTab`, `ProductNotFound` | The product page |
| `SettingsEditor` (lazy), `SettingsEditorSkeleton` | The Data & retention form |
| `searchBoxModel.ts` (`useDebouncedSearch`), `useChipFocus` | The shared search-box state machine and the focus rule after a chip is removed (section 2) |
| `VersionDistributionBar` | Mini stacked bar of hosts per version with an "other" bucket. Without its `total` prop it prints each segment's share of the **sum** of per-version hosts, which is right for network firmware (one version per device, its only use) and wrong where a host runs several versions; pass the distinct hosts as `total` to use it for software. The software pages use `SoftwareVersionBar`, which prints no percentages |

Pure models: `model.ts` (counts, percentages, ages, natural version order for display only), `overviewModel.ts`, `assetModel.ts`, `changeModel.ts`, `changesModel.ts`, `osTreeModel.ts`, `hardwareModel.ts`, `softwareModel.ts`, `productModel.ts`, `settingsModel.ts`; page-level vocabulary and fetchers in `pages/inventory/filters.ts`, `urlState.ts`, `api.ts`, `osHardwareChanges.ts` and `softwareSettings.ts`, with `*.typecheck.ts` files that fail the strict build if a type drifts from the contract.

Reused unchanged:

- `CATEGORICAL`, `chartTooltipStyle`, `fmtCount` and the `KPI_ACCENT` tokens (dashboards/shared.tsx). The shared `KpiCard` was not used: a card with sub-links needs a stretched main link and separate sub-links, which `InventoryKpiCard` provides
- `TablePager` (servers/tables.tsx), `QueryBoundary` and `QueryFailure` (ui/QueryState.tsx), `Skeleton`, `Button`, `Card`, `Switch`, `Label`, `Input`, `Tabs`, the toast
- `downloadBlob`, `filenameFromDisposition` and `blobErrorMessage` (services/list/model) for the downloads

## 8. Query keys and polling

| Key | Refetch |
|---|---|
| `['inventory','summary']` | 60 s |
| `['inventory','assets', params]`, `['inventory','facets', params]` | On filter change; `staleTime` 30 s |
| `['inventory','os', kind]`, `['inventory','hardware']` | 5 min |
| `['inventory','software', params]`, `['inventory','product', key, tab]` | 5 min |
| `['inventory','changes', params]` | 60 s on the Changes page |
| `['inventory','trends', metric]` | 15 min |

**As built (P1).**

| Key | `staleTime` / refetch |
|---|---|
| `['inventory','summary']` (shared by the meta-line, the Overview, the Assets empty state) | stale 30 s, refetch 60 s (15 s while `data_status.normalising`) |
| `['inventory','assets', params]`, `['inventory','facets', filters]`, `['inventory','asset', address]` | stale 30 s; refetch on filter change; previous data kept while the next loads |
| `['inventory','os', kind]`, `['inventory','hardware']`, `['inventory','hardware', vendor]` | stale 60 s, refetch 5 min |
| `['inventory','software', params]`, `['inventory','software','publishers', params]` | stale 60 s, catalog refetch 5 min |
| `['inventory','product', key]` and its `installed` / `timeline` children | stale 60 s, product refetch 5 min |
| `['inventory','changes', request]` | stale 30 s, refetch 60 s on the Changes page |
| `['inventory','history-start']` (the capture start behind *before <date>* on the software pages: a one-hour, one-row `GET /changes`; the Changes page reads `history_start` from its own answer) | stale 30 min, one retry |
| `['inventory','trends', metric]` | stale 5 min, refetch 15 min, no retry |
| `['inventory','settings']` | stale 30 s; a successful save invalidates every other `inventory` key |

