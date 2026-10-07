# 03 — UI/UX Specification

*Status: Design proposal · 2026-10-07. Routes and permissions are pinned in `00-INDEX.md`. Every building block named here exists in the dashboard today (see doc 01 §B.5). The module is assembled from the house design system so it looks native on day one. A clickable prototype with example data accompanies this set.*

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
  short: 'Assets',
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
    { to: '/inventory/settings', label: 'Inventory Settings', icon: SlidersHorizontal, permission: 'inventory.manage',
      hint: 'Software catalog rules, lifecycle overrides, retention' },
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
- **Collapsed rail:** "Assets" fits under the icon. Hints appear in the flyout and the sidebar search.

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
    <Route path="settings" element={<InventorySettingsPage />} />     {/* manage-gated inside */}
  </Route>
</Route>
```

- `InventoryAccessGate` uses `useCan()`. Without `inventory.view` it renders a designed denied state: the icon, "You don't have access to Inventory", the permission name, and "Ask an administrator to add **inventory.view** to your role". There is no blank page or redirect loop.
- Every route is added to `requiredRoutes` in `dashboard/scripts/smoke-routes.mjs`.
- **Companion fix (P1):** extend the smoke script, or add a new `scripts/test-inventory-ui.mjs` that runs in the dashboard CI job, so that **every `navigation.ts` `to:` resolves to a declared route**. Today it doesn't (doc 01 G12).

### 1.3 `InventoryLayout`

This follows `UdtLayout` exactly:

- an `h1` with the `PackageSearch` icon, "Inventory", `KbLink`, and the subtitle "Every operating system, software title and hardware model across servers and network devices";
- a `NavLink` underline tab strip mirroring the sidebar rows;
- `<Outlet />`.

The product detail route owns the full canvas with no tab strip, and shows a back link "← Software".

A **meta-line** under the tabs is visible on every Inventory page:

> Inventory as of 14:05 · **97%** of assets reported within freshness targets · 6 stale · Lifecycle data as of 2026-10-01 *(P4)*

Clicking "6 stale" opens Assets filtered `freshness=stale`.

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
| **Links to existing pages** | Asset names link to `/servers/:id` or `/devices/:id` only if the user holds `apm.view` / `devices.view`; otherwise plain text with a tooltip |
| **Charts** | Recharts, `chartTooltipStyle`, a faint grid. Every chart has a **"View as table"** toggle (accessibility, and copying numbers) |
| **Density** | 13 px table text, row height 36 px, sticky table headers, 50 rows per page by default (`PAGE_SIZES`) |

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
- **Security-agent coverage** (from category `security`): a small line under the Software card reads "EDR/AV on 121 of 128 servers". The gap of 7 links to the hosts without it. This is the most-requested inventory insight in the research, available before any policy engine exists.
- **First-run empty state:** with no agents and no SNMP devices, the page shows three onboarding cards: "Install the Windows agent" (→ `/server-agents`), "Add network devices" (→ `/discovery`) and "Inventory fills in automatically". With partial data, each panel shows its own empty state, and the rest of the page still renders.
- **P4 additions:** an "EOL / EOL ≤ 180 d" KPI, and lifecycle colouring on the OS and firmware bars.

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
  - **Server body:** CPU, RAM, disks, top 10 software with "View all on server page", and the security-agent present/absent pill.
  - **Device body:** ENTITY-MIB modules (class, model, serial), with HA role and mode from template values.
  - **Recent changes (10):** "View all → `/inventory/changes?asset=…`".
  - **Meta (P2):** owner, department, criticality, lifecycle state, warranty end; editable with `inventory.manage`; "Mark reviewed" stamps `last_reviewed_at` (CIS 1.1).
- There is **no separate asset detail page**. The existing device and server pages remain the detail pages, which avoids the duplicate-page anti-pattern.

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
- **Hidden categories:** the default-hidden ones (`update, driver, os-component`) are counted in a footer: "312 hidden entries · Show".

### 3.5 `/inventory/software/:productKey`: Product page

- **Header:** product name, publisher, category, normalisation status pill ("Normalized" / "Unmatched"), and the KPIs Hosts, Versions, Latest, First seen.
- **Tabs (`?tab=`):**
  - **Versions:** a horizontal bar per version (hosts), sorted by comparator, with an N / N-1 / older grouping. Click a version to filter Installations.
  - **Installed on:** a server table with version, architecture, source (HKLM/HKCU/dpkg), install date, first seen and last report, plus CSV export.
  - **Timeline** (P2 UI, data from P0): installs, removals and version changes for this product over 90 days, with a daily stacked bar and an event list below.
  - **Evidence:** the raw names and publishers aliased into this product, and which rule matched. A "**Report inaccuracy**" action (manage) opens the rule editor pre-filled.
- **P6:** a Vulnerabilities tab appears when the feed is active.

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

### 3.7 `/inventory/changes`: Change history

- **Stats strip (7 d, switchable 24 h / 7 d / 30 d / 90 d):** Installed · Removed · Upgraded · Downgraded · OS changes · Firmware changes · Hardware changes. Each is a filter chip.
- **Daily stacked bar** by category.
- **Event list** grouped by day. Each row shows time, asset (link), category icon, and a sentence: "**Google Chrome** upgraded 151.0.7922.110 → 152.0.8001.12", "**FortiOS** changed 7.2.5 → 7.2.8 *(firmware)*", "**TeamViewer 15.5.3** installed".
- **Filters:** category, action, asset kind, asset, product, search; all in the URL.
- **Data-integrity footnote:** "History starts 2026-10-xx (when change capture was enabled). Earlier changes were not recorded." This is honest, and auditors ask for it.

### 3.8 `/inventory/lifecycle` (P4)

- **KPIs:** End of life now · EOL ≤ 90 d · ≤ 180 d · ≤ 365 d · Supported · Unknown (always shown).
- **Table:** a row per product/release in use, with assets, stage pill, EOAS/EOL/EOES dates (product-specific labels), latest version, source + "as of", and an override indicator.
- **Timeline:** horizontal bars from today to EOL for releases in use, with a today marker.
- **Hardware:** models with end-of-sale and end-of-support dates (override or CSV import), plus a "Suggested / Assigned / No data" workflow, as in SolarWinds NCM.
- **Manage:** add or edit an override (reason, expiry), and import a CSV.

### 3.9 `/inventory/settings` (manage)

Tabs:

- **Software catalog** (P2): rules table (priority, pattern, product, category, hits) with a draft-rule **preview** panel showing matched raw names and host counts before saving; products (rename, category, hide, business purpose); publishers (aliases); the *Unmatched* queue sorted by host count.
- **Lifecycle** (P4): overrides and hardware EoS import.
- **Data & retention:** freshness thresholds per kind, change-retention days, snapshot hour, default-hidden categories.

Every save writes an audit entry, and the UI shows a confirmation toast that names the effect ("Rule saved — 14 titles re-grouped").

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
| `DevicesPage.tsx` | Add **Vendor, Model, OS version** columns (optional, default on) and vendor/OS filters. The data is already in the `Device` type (79-110) | P1 |
| `DeviceDetailPage.tsx` `InventoryConfigCard` (1535) | Firmware value links to `/inventory/os?os_family=…&os_release=…`; adds a "Changes" mini-list | P1 |
| `servers/detail/tabs/SystemTab.tsx` `SoftwareSection` (815) | Each package shows its normalised product as a link to `/inventory/software/:key` with "N hosts fleet-wide"; honours the `truncated` flag with a banner; "Changes" sub-section | P1 / P2 |
| `ServerTeamDashboard` / Server Overview | OS mix tile links to `/inventory/os?kind=server` | P1 |
| Servers nav "Inventory" | Relabelled "All Servers" | P1 |
| `/server-baselines` | Redirects to `/compliance/policies` after migration | P5 |
| `ReportsPage` Inventory tab | Shows the new sections | P2 |

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

## 6. Accessibility and responsiveness

- Tables are real `<table>`s with `scope` headers. Keyboard: `/` focuses search, `Esc` closes the flyout, arrow keys move between rows, `Enter` opens the flyout.
- Charts have a "View as table" toggle, `aria-label` summaries ("Windows Server 2022: 61 assets, 48 percent"), and no color-only encoding.
- Contrast uses the `*-fg` tokens, which are ratio-checked by `test-netflow-foundation.mjs`.
- The layout works at 1280 px and degrades to 768 px: the facet pane becomes a drawer, and KPI grids go 6 → 3 → 2 columns.

## 7. New shared components (`components/inventory/`)

| Component | Purpose |
|---|---|
| `FreshnessBadge` | Fresh / aging / stale / never pill with age text |
| `SourceBadge` | Agent / SNMP / Template / ENTITY-MIB / WinRM / SSH chip |
| `VersionDistributionBar` | Mini stacked bar of hosts per version, comparator-ordered, with an "other" bucket |
| `FacetPanel` | Collapsible facet groups with counts and URL binding (generalised from the `ServersPage` status chips) |
| `AssetFlyout` | The right sheet in §3.2 |
| `DrillLink` | `<Link>` that serialises a filter object into inventory URL params; used by every KPI and chart |
| `InventoryMetaLine` | Freshness/coverage line under the tab strip |

Reused unchanged:

- `KpiCard`, `SectionHeader`, `ShareBar`, `Empty`, `CATEGORICAL`, `chartTooltipStyle`, `fmtCount` (dashboards/shared.tsx)
- `TablePager`, `sortableTh`, `ExportCsvButton`, `TableStateRow` (servers/tables.tsx)
- `QueryBoundary` (ui/QueryState.tsx)
- `KbLink` (udt)
- Radix `Dialog` / `Select` / `Tabs` wrappers

## 8. Query keys and polling

| Key | Refetch |
|---|---|
| `['inventory','summary']` | 60 s |
| `['inventory','assets', params]`, `['inventory','facets', params]` | On filter change; `staleTime` 30 s |
| `['inventory','os', kind]`, `['inventory','hardware']` | 5 min |
| `['inventory','software', params]`, `['inventory','product', key, tab]` | 5 min |
| `['inventory','changes', params]` | 60 s on the Changes page |
| `['inventory','trends', metric]` | 15 min |
