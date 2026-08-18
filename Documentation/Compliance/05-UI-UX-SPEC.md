# 05 — UI/UX Specification: the Compliance section

*Status: Design proposal · 2026-08-18. Every component named here exists in the dashboard today (file:line references in `raw/code-frontend.md`) — the section is assembled from the house design system so it looks native on day one. Routes/permissions pinned in `00-INDEX.md`.*

Design intent: **a security cockpit an operator trusts.** Lead with what to fix first (risk-ranked, KEV-flagged, SLA-aged), make every number clickable into the filtered list behind it, keep triage one interaction away, and never show a stale or unexplained value — every score expands into its components, every widget shows its data age.

---

## 1. Navigation & shell

New top-level sidebar group (after `servers`, `navigation.ts`):

```ts
{
  id: 'compliance', label: 'Compliance', short: 'Comply', icon: ShieldCheck,
  items: [
    { to: '/compliance', label: 'Overview', icon: Gauge, end: true, hint: 'Security posture at a glance', permission: 'compliance.view' },
    { to: '/compliance/vulnerabilities', label: 'Vulnerabilities', icon: Bug, hint: 'CVEs matched to your assets', permission: 'compliance.view', extra: ['/compliance/vulnerabilities/'] },
    { to: '/compliance/assets', label: 'Assets', icon: Server, hint: 'Per-asset risk & findings', permission: 'compliance.view' },
    { to: '/compliance/patches', label: 'Remediation', icon: Wrench, hint: 'What to upgrade, and what it clears', permission: 'compliance.view' },
    { to: '/compliance/eol', label: 'End of Life', icon: CalendarClock, hint: 'Lifecycle exposure', permission: 'compliance.view' },
    { to: '/compliance/software', label: 'Software', icon: Package, hint: 'Fleet software risk', permission: 'compliance.view' },
    { to: '/compliance/settings', label: 'Settings', icon: Settings2, hint: 'Feed, thresholds, SLA policy', permission: 'compliance.manage' },
  ],
}
```

`ComplianceLayout.tsx` follows the UDT/APM exemplar exactly: `h1` with `ShieldCheck` icon + one-line subtitle, NavLink underline tab strip, `KbLink` i-icon per tab (`KB_BASE = 'https://zentryc.com/kb/zenplus/compliance'`), `<Outlet />`. Detail routes (`/compliance/vulnerabilities/:cveId`) own the full canvas (no tab strip). Breadcrumbs come free from the nav registration. `npm run smoke:routes` covers every nav `to:`.

Global chrome: severity vocabulary matches the app everywhere — `critical → Badge danger`, `high → warning`… CVE ids, versions, KBs render `font-mono text-xs`. All colors via tokens; charts use the `CATEGORICAL` palette; status tokens reserved for state.

---

## 2. `/compliance` — Overview (the dashboard)

```
┌ Compliance ────────────────────────────────────────────── (i) ─┐
│ Overview | Vulnerabilities | Assets | Remediation | EOL | … │
├──────────────────────────────────────────────────────────────┤
│ [feed banner — only when degraded: "Feed 9 days stale · last  │
│  sync 2026-08-09 · Sync now / Upload bundle"]                 │
├ KPI row (grid-cols-2 md:grid-cols-3 xl:grid-cols-6) ─────────┤
│ ┌Critical┐ ┌KEV     ┐ ┌Overdue ┐ ┌Patch   ┐ ┌EOL    ┐ ┌Fleet ┐│
│ │  12    │ │  3 ⚠   │ │  7     │ │ 86%    │ │  5    │ │ 412  ││
│ │ +2 wk  │ │ fix now│ │ SLA    │ │ compl. │ │ assets│ │ risk ││
│ └────────┘ └────────┘ └────────┘ └────────┘ └───────┘ └──────┘│
├ 2-col grid (lg:grid-cols-2) ─────────────────────────────────┤
│ ┌ Severity × Age heat map ───────┐ ┌ Risk trend (30d) ──────┐ │
│ │        <7d  <30d <90d  >90d    │ │  area chart: open by   │ │
│ │ Crit    3    5    2     2      │ │  severity, stacked +   │ │
│ │ High    8   11    6     4      │ │  avg asset risk line   │ │
│ │ Med    14   22   31    19      │ │                        │ │
│ │ (cells clickable → filtered    │ │ (compliance_daily_     │ │
│ │  vulnerabilities list)         │ │  snapshots)            │ │
│ └────────────────────────────────┘ └────────────────────────┘ │
│ ┌ Top risky assets (10) ─────────┐ ┌ Top remediations ──────┐ │
│ │ risk│asset│type│open C/H│EOL   │ │ "IOS-XE → 17.9.5       │ │
│ │ 847 │core-sw-01│ …  (row link  │ │  12 devices · clears   │ │
│ │     │ → asset detail)          │ │  47 CVEs · risk −2.1k" │ │
│ └────────────────────────────────┘ └────────────────────────┘ │
│ ┌ New this week (matched CVEs) ──┐ ┌ SLA aging ─────────────┐ │
│ │ CVE · sev · EPSS · assets      │ │ overdue / due 7d /     │ │
│ │                                │ │ due 30d / on track bar │ │
│ └────────────────────────────────┘ └────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

- KPI tiles: `KpiTile` (servers/shared.tsx variant), tones: Critical=danger, KEV=danger with pulse `StatusDot`, Overdue=warning, Patch compliance=success when ≥ target, EOL=warning, Fleet risk=primary with trend arrow + spark delta ("+2 this week" foot). **Every tile links** to the pre-filtered list behind it.
- Persistent meta-line under the KPI row, visible to every `compliance.view` user (this is the view-level feed status the permission grants): `Feed data as of 2026-08-18 06:00 · synced 2 h ago · 96% of assets evaluated in 24 h` — coverage ≥ 95% is the auditor's first question; the full feed controls stay manage-only in Settings.
- Heat map: `TrafficHeatmap` pattern (clickable cells write URL filters). Trend: Recharts stacked `AreaChart` in `MetricChartCard`.
- The Action1 idea, adopted: when there are zero overdue findings and zero open KEV, the SLA widget renders a full-width success state ("**SLA compliant** — nothing overdue") — a legible green moment.
- Data: `GET /compliance/summary`, key `['compliance','summary']`, `refetchInterval: 30_000`.
- First-run empty state (no feed yet): centered `ShieldCheck` icon, "No vulnerability data yet — the feed syncs automatically after registration", buttons **Sync now** / **Upload bundle** / link to KB. Unentitled: activation call-to-action instead.

---

## 3. `/compliance/vulnerabilities` — CVE workbench

Toolbar: debounced search (CVE id / text), severity chip row with counts (`ServersPage` status-chip pattern), `Select` filters: State (open/fixed/resurfaced), Triage, KEV only, Fix available, Confidence (A–C shown by default; "include low confidence" reveals D/E — matching the pinned tier policy), Vendor/Product, Tag. All in URL params. `ExportCsvButton`.

Table (`TablePanel` + furniture kit; sortable; zebra):

| Risk ▾ | CVE | Severity | CVSS | EPSS | KEV | Affected | Fix | Age | State |
|---|---|---|---|---|---|---|---|---|---|
| 100 | `CVE-2026-20198` | ●Critical | 10.0 | 0.97 | ⚠ due 08-30 | **14 assets** | 17.9.5 | 12d | open |

(The list column shows the max FRS across affected assets; per-asset FRS lives in the expand/detail.)

- **Dual display rule:** CVSS severity and FRS risk render side by side everywhere ("●Critical severity · Medium risk") — an unexploited paper-critical scoring Medium risk must never look like the product missed it.
- Row expand (NetFlow expandable-row pattern) → inline panel: description excerpt, score breakdown chips exactly mirroring the FRS components (`I 1.00 × T 0.90 KEV × E 1.15 exposed × Q 1.00 A → 103.5 → capped 100`), affected-asset mini-table with per-asset triage shortcut, "Open detail →".
- Bulk selection → floating action bar: **Change triage** (dropdown over the triage enum + required-for-risk_accepted comment/expiry dialog; lifecycle *state* is matcher-owned and never operator-set), **Export**.
- Key: `['compliance','vulnerabilities', {filters}]`, 60 s poll.

### `/compliance/vulnerabilities/:cveId` — CVE detail (full canvas)

```
← Back   ┌──┐ CVE-2026-20198   ●Critical severity · Critical risk  ⚠ KEV due 2026-08-30
         │🐛│ Cisco IOS-XE Web UI privilege escalation
         └──┘ risk 100 (max across assets) · EPSS 0.97 (99.9th pct) · published 12d ago
┌ Summary Card ────────────────┬ Score breakdown Card ─────────┐
│ description, CWE, refs       │ I 1.00 (CVSS 10.0) × T 0.90   │
│ (NVD / vendor advisory links)│ (KEV) × E 1.15 (exposed) ×    │
│ CVSS vector InfoGrid         │ Q 1.00 (tier A) → capped 100  │
│                              │ SLA: due 2026-08-30 (KEV)     │
├ Affected assets (TablePanel) ┴───────────────────────────────┤
│ asset │ type │ version │ confidence │ evidence │ state │triage│
│ core-sw-01 │ device │ 17.6.5 │ A vendor_exact │ ⓘ │ open │ ▾  │
│  … evidence popover: "matched cisco/ios_xe train 17.6,       │
│     fixed_in 17.6.7 · source: cisco_psirt"                   │
│ [bulk: Change triage ▾ · comment]                             │
├ Remediation Card ────────────────────────────────────────────┤
│ "Upgrade to 17.9.5 clears this + 46 other CVEs on 12 assets" │
│ → link to /compliance/patches?cve=CVE-2026-20198             │
├ History (vuln_finding_events timeline, per selected asset) ──┤
└──────────────────────────────────────────────────────────────┘
```

Confidence renders as a lettered chip with tooltip ("A — vendor advisory names this exact version"); D/E rows carry a muted "verify" affordance. Evidence is always one popover away — the anti-false-positive-frustration design.

---

## 4. `/compliance/assets` — asset posture

Table of every matched asset: Risk score (0–1000, tinted bar cell like `UsageCell`), asset (link), kind/vendor/OS+version (`font-mono`), open findings as compact severity pills (`3C 8H 12M`), KEV count, EOL badge, criticality (1–5 editable inline for `compliance.manage` via popover), last evaluated. Filters: kind, vendor, tag, criticality, has-KEV, EOL state, min risk. Facet sidebar (NcmPage pattern) with vendor/type distribution bars.

Row click → the asset's existing detail page: devices get a new **Security** tab on DeviceDetailPage (findings + EOL + remediation for that device; the dead `firmware_version` slot now renders real firmware); servers get the existing **Compliance** tab extended with a "Vulnerabilities" section above the baseline results. Both tabs reuse one shared `<AssetFindingsPanel>` component (`GET /compliance/assets/{kind}/{id}`).

Bulk bar: set criticality, mark internet-facing, exclude from matching (manage-gated, audited).

---

## 5. `/compliance/patches` — Remediation

The ManageEngine differentiator, made central. Grouped by action, sorted by risk cleared:

```
┌ Upgrade Cisco IOS-XE 17.6.x → 17.9.5 ────────────── −2,140 risk ┐
│ 12 devices · clears 47 CVEs (3 critical, 2 KEV) · in-train      │
│ [expand] device list: name │ current │ findings cleared │ state │
│ actions: Mark remediation planned (sets triage on all findings) │
│          Export CSV · Copy upgrade summary                      │
├ Install KB5034122 (Windows Server 2022) ─────────── −860 risk ──┤
│ 3 servers · clears 18 CVEs · supersedes KB5033118               │
├ Update Google Chrome → 152.x ────────────────────── −410 risk ──┤
│ 9 servers · clears 12 CVEs · third-party                        │
└ [train-jump suggestions collapsed under "Requires train change"]┘
```

KPI strip on top: total risk clearable, actions count, assets covered, "% of open risk addressable by top 5 actions" (the motivating number). v1 is flag-and-recommend — no deployment; each action links out to the relevant workflow (NCM device page, server detail).

---

## 6. `/compliance/eol` — End of Life

- KPI strip: past EOL / EOL < 90 d / EOL < 1 y / supported.
- Timeline board (grouped list, not a gantt): sections by urgency, each row = product cycle (`FortiOS 7.2 — EOL 2026-09-30`) with affected-asset count and expandable asset list. Past-EOL rows carry the "unpatchable — upgrade/replace" badge; their assets show `replace_eol` remediation.
- Filters: kind, vendor, milestone. EOL status also appears as a column/filter on Assets (runZero lesson: EOL is an asset attribute, not just a report).

---

## 7. `/compliance/software` — fleet software risk

Aggregation of `server_software_inventory` × findings: package | version(s) | install count | open CVEs (severity pills) | fix version | EOL. Filters: only-vulnerable, vendor, search. Row expand → servers running it, with per-server versions. This page answers the user-story "evaluate installed software against active vulnerabilities" directly and doubles as a software-normalization health view: an "Unmatched software" sub-tab lists inventory rows with no dictionary identity (Tier-E), with counts — the telemetry that feeds the central alias-dictionary backlog.

---

## 8. `/compliance/settings` (manage-gated)

Cards, `GeneralSettingsPage` style:

1. **Feed status** — mirror of the Updates tab pattern: channel, schema version, applied offset, definitions count, last sync + result, feed built-at ("data age 6 h"), next scheduled sync. Buttons: **Sync now** (mutation + toast + invalidate), **Upload bundle** (file dialog → `POST upload-bundle`, shows verify result incl. signature/freshness errors verbatim). Sync history mini-table (last 10).
2. **Matching** — nightly full-evaluation hour (appliance tz), **Evaluate now**, per-kind enable (devices/servers), excluded-asset count with link.
3. **Alerting & thresholds** — min severity to alert, confidence gate (default A/B), KEV-always-alert toggle; link to Alert Rules for channel routing.
4. **SLA policy** — due-days per severity (defaults per doc 03 §5.3), KEV override note ("CISA due date always wins").
5. **Data & privacy** — what leaves the appliance: feed polls, applied-offset status, and (when `share_telemetry` is on, the default) observed product/version tuples plus unmatched-software name counts that improve central matching — each listed explicitly, with the opt-out toggle right there. Retention: fixed findings 365 d (12-month audit history), report archive ≥ 12 months.

---

## 9. Reports & alerts surfacing

- **Report sections** (existing section-based report engine): `compliance_executive_summary` (KPIs + trend + top actions), `compliance_vuln_detail` (findings by severity/asset), `compliance_sla` (aging + overdue table), `compliance_delta` (new/fixed since last report), `compliance_eol`. All schedulable/exportable like existing sections.
- **Alert Center**: `compliance_*` alerts render with existing severity styling; alert detail deep-links to the finding/CVE page. The sidebar alert badge already covers count surfacing.

---

## 10. Frontend engineering notes

- Module structure: `pages/compliance/{ComplianceLayout,OverviewPage,VulnerabilitiesPage,VulnerabilityDetailPage,AssetsPage,PatchesPage,EolPage,SoftwarePage,SettingsPage}.tsx` + `api.ts` (UDT-style typed wrapper) + `types.ts` + `helpers.tsx` (severity/state/confidence meta maps — single source for badge variants).
- Query keys: `['compliance', <page>, filtersObj]`; polling 30 s (summary) / 60 s (lists) / manual (settings/feed history). Mutations: toast + prefix invalidation `['compliance']`.
- All list/filter/tab/page state in URL params (house convention) — every view shareable.
- Loading/error/empty: three-state bodies everywhere (`Skeleton` rows, `QueryError` with retry, differentiated empty states: "no data yet" vs "nothing matches filters" vs "feed not synced").
- `useCan('compliance.triage'|'compliance.manage')` gates every mutating affordance in-page; backend 403s are the enforcement.
- Component caveat: `TablePanel` and `MetricChartCard` are **page-local** functions in `ServerDetailPage.tsx:750–791`, not shared exports — extract them into a shared module (or copy the pattern) as part of the first Compliance page work; the genuinely shared pieces are `components/servers/tables.tsx` and `KpiTile` in `components/servers/shared.tsx`.
- Build/deploy traps (documented project gotchas): never touch `src/index.css` — it is a stale legacy file; `main.tsx` loads only `styles/globals.css`. Production deploys use `npx vite build` (not `npm run build`, which runs tsc) and `chmod a+rX dist` afterward, since nginx serves `dist/`.
- The unmatched-software counts shown on `/compliance/software` are also reported to zentryc.com as part of feed telemetry (with observed OS/version tuples) so the central alias dictionary grows — disclosed in Settings → Data & privacy, opt-out `share_telemetry`.
- No new dependencies: Recharts + Radix + the table furniture kit cover everything above.
