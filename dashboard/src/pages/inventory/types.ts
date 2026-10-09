// Response shapes of the Inventory API (server/app/api/v1/inventory.py), in ONE file so the final
// contract (Documentation/Inventory/05-P1-API-CONTRACT.md) is reconciled in one place.
//
// These follow Documentation/Inventory/05-P1-API-CONTRACT.md and server/app/schemas/inventory.py. Every
// type that has a golden fixture (./fixtures/*.json) is checked against it twice: at compile time by
// ./contract.typecheck.ts (the strict build fails on drift) and at run time by scripts/test-inventory-ui.mjs.
// A null is always present, never omitted, so a client can tell "unknown" from "not in this version of the
// API". Timestamps are UTC ISO-8601 strings. Filter and paging types live in ./filters.ts, because the URL and
// the API share one vocabulary.

import type { AssetKind, Freshness, HealthPredicate, SortOrder } from './filters'

export interface ScopeInfo {
  /** True when the caller's tag scope narrowed what the numbers cover. */
  restricted: boolean
  tags: string[]
}

/* ── Summary (GET /inventory/summary, cached 60 s per scope key) ────────── */

export interface InventorySummary {
  generated_at: string
  scope: ScopeInfo
  assets: { total: number; server: number; device: number; linked_devices: number }
  coverage: {
    fresh: number
    aging: number
    stale: number
    never: number
    /**
     * Assets that cannot report an inventory by design (ping-only and controller-managed devices, servers
     * whose collection mode cannot upload one). Listed and counted, never part of the denominator of
     * `fresh_pct`, of `never` or of the all-clear. Drills to `freshness=not_applicable`.
     */
    not_applicable: number
    /** fresh / (assets.total - not_applicable); null when nothing can report. */
    fresh_pct: number | null
    fresh_hours: Record<string, number>
    stale_days: number
  }
  os: {
    families: number
    releases: number
    unknown: number
    top: { family: string; product: string | null; release: string | null; count: number }[]
  }
  software: {
    products: number
    installs: number
    publishers: number
    hidden_categories: string[]
    top: SummaryProduct[]
    sprawl: SummaryProduct[]
    /**
     * What `sprawl` ranks: `all_products` is exact; `top_installed` means only the `candidates` most
     * installed of `products` products were ranked (the panel must say so).
     */
    sprawl_basis: { scope: 'all_products' | 'top_installed'; candidates: number; products: number }
    /**
     * Endpoint protection (category `endpoint-protection`, or a running Defender service) per server.
     * covered + missing + unknown = total = assets.server. `unknown`: the software list is incomplete
     * (truncated, not baselined or not yet normalised).
     */
    security_agent_coverage: { covered: number; missing: number; unknown: number; total: number }
  }
  hardware: {
    devices: number
    vendors: number
    models: number
    top_models: { vendor: string; model: string; count: number }[]
  }
  changes_7d: {
    since: string
    total: number
    software: { added: number; removed: number; changed: number; upgraded: number; downgraded: number }
    os: number
    firmware: number
    hardware: number
  }
  /**
   * One count per health predicate; each is the total of `/inventory/assets?health=<key>`. Written out key by key
   * (the contract test reads this file); the keys are the eight `HealthPredicate`s plus `not_collected_by_design`.
   */
  health: {
    missing_os_version: number
    missing_model: number
    missing_serial: number
    unknown_vendor: number
    stale: number
    never: number
    software_truncated: number
    missing_security_agent: number
    /** Assets that cannot report by design; drills to `freshness=not_applicable`. */
    not_collected_by_design: { count: number }
  }
  /** Whether existing rows are still being normalised, in which case the software figures are incomplete. */
  data_status: DataStatus
  /** P4. Always null in Phase 1. */
  lifecycle: Record<string, unknown> | null
}

/** Backlog of the normaliser (scope-aware): rows with no key, or one from an older rule pack. */
export interface DataStatus {
  normalising: boolean
  pending_software_rows: number
  pending_identity_rows: number
}

export interface SummaryProduct {
  product_key: string
  name: string
  category: string | null
  hosts: number
  versions: number
}

/* ── Assets (the asset union, doc 02 section 3.1) ──────────────────────── */

export interface AssetRow {
  /** `<kind>:<uuid>`, the address used in `?asset=` and in `/inventory/assets/{kind}/{id}`. */
  key: string
  kind: AssetKind
  id: string
  name: string
  ip: string | null
  asset_type: string | null
  status: string | null
  vendor: string | null
  model: string | null
  serial: string | null
  os_family: string
  os_product: string | null
  os_release: string | null
  os_version: string | null
  os_build: string | null
  os_edition: string | null
  os_source: string | null
  hw_source: string | null
  site: string | null
  tags: string[]
  /** Collection sources, for example `['agent', 'snmp']`. */
  sources: string[]
  last_seen: string | null
  inventory_at: string | null
  inventory_age_s: number | null
  freshness: Freshness
  software_truncated: boolean
}

/** GET /inventory/assets. */
export interface AssetList {
  generated_at: string
  items: AssetRow[]
  total: number
  page: number
  page_size: number
  sort: string
  order: SortOrder
}

export interface FacetItem {
  /** The filter value; `unknown` is the sentinel for a missing one. */
  value: string
  label: string
  count: number
}

export interface FacetGroup {
  distinct: number
  items: FacetItem[]
}

export type FacetName = 'kind' | 'os_family' | 'os_product' | 'vendor' | 'model' | 'site' | 'tag' | 'source' | 'freshness' | 'health'

/** GET /inventory/assets/facets: counts per facet, reflecting the other active filters. */
export interface FacetsResponse {
  generated_at: string
  total: number
  facets: Record<FacetName, FacetGroup>
}

/* ── Asset flyout (GET /inventory/assets/{kind}/{id}) ──────────────────── */

/** One identity field with where it came from and how old it is (doc 03 section 3.2). */
export interface IdentityValue {
  field: string
  label: string
  /** Null = unknown (an `os_family` of `unknown` is reported as null here). */
  value: string | null
  /** The badge: agent | snmp | template | entity-mib | ssh. */
  source: string | null
  /** The stored source: agent, profile, entity, template, template_stale, device, sysdescr, managed. */
  source_detail: string | null
  observed_at: string | null
  age_s: number | null
}

export interface ServerSoftwareRow {
  package_name: string
  product_key: string | null
  product: string
  version: string | null
  publisher: string | null
  category: string | null
  architecture: string | null
  source: string | null
  install_date: string | null
  first_seen_at: string | null
}

export interface ServerBody {
  os_name: string | null
  architecture: string | null
  hostname: string | null
  fqdn: string | null
  cpu_model: string | null
  cpu_cores: number | null
  cpu_physical_cores: number | null
  memory_total_bytes: number | null
  /** As the agent reported them (`{model, size_bytes, ...}`). */
  disks: Record<string, unknown>[]
  virtualization: string | null
  bios_version: string | null
  bios_date: string | null
  boot_time: string | null
  /** A first software snapshot has been accepted. */
  baselined: boolean
  software: {
    total: number
    shown: number
    hidden: number
    /** The agent's list was cut at its cap. */
    truncated: boolean
    top: ServerSoftwareRow[]
    /**
     * Endpoint protection. `present` is null while nothing can be said (`state` `unknown`; `reason` says why:
     * no list reported, a list cut at the agent's cap, rows still being normalised, or a complete list without an
     * endpoint-protection title from a server that has reported no service list, so built-in Defender cannot be judged).
     */
    security_agent: {
      present: boolean | null
      state: 'present' | 'missing' | 'unknown'
      reason: 'no_software_list' | 'list_truncated' | 'normalising' | 'no_service_list' | null
      /** Titles of the endpoint-protection products installed. */
      products: string[]
      /** Running services (Defender) that count as endpoint protection. */
      services: string[]
    }
  }
}

export interface DeviceModuleGroup {
  class: string
  class_label: string
  model: string | null
  count: number
  items: { ent_index: number; name: string | null; serial: string | null; hw_revision: string | null; fw_revision: string | null }[]
}

export interface DeviceBody {
  profile: string | null
  group: string | null
  device_type: string | null
  sys_object_id: string | null
  controller_managed: boolean
  modules: DeviceModuleGroup[]
  /** More than 500 components: the list is cut. */
  modules_truncated: boolean
  ha: { mode: string | null; role: string | null; peer: string | null; sync: string | null; observed_at: string | null; age_s: number | null } | null
}

export interface AssetDetail {
  generated_at: string
  asset: AssetRow
  /** Always the same nine fields: vendor, model, serial, os_family, os_product, os_release, os_version, os_build, os_edition. */
  identity: IdentityValue[]
  /** The raw evidence the values were normalised from. */
  raw: Record<string, string | null>
  server: ServerBody | null
  device: DeviceBody | null
  /**
   * The 10 newest events of the asset (and of its SNMP twin); `total` is all of them.
   * `first_change_at` is the oldest of those events (null when there is none): the Changes page opened
   * with `since=first_change_at` lists exactly `total` events.
   */
  changes: { items: ChangeRow[]; total: number; first_change_at: string | null }
  /** Ids only: /servers/:id and /devices/:id, shown as links only with servers.view / devices.view. */
  links: { server_id: string | null; device_id: string | null }
}

/* ── Changes (GET /inventory/changes) ───────────────────────────────────── */

export type ChangeCategory = 'software' | 'os' | 'firmware' | 'hardware'
export type ChangeAction = 'added' | 'removed' | 'changed'

export interface ChangeRow {
  id: number
  observed_at: string
  asset_key: string
  asset_kind: AssetKind
  asset_id: string
  /** The name at the time of the event. */
  asset_name: string | null
  /** False: the asset has been deleted since (history survives deletion). */
  asset_present: boolean
  /** For a device that is a server's SNMP twin: that server (link to it instead). */
  server_id: string | null
  category: ChangeCategory
  action: ChangeAction
  subject: string
  subject_key: string | null
  old_value: string | null
  new_value: string | null
  /** Software version changes that can be ordered; null otherwise (never a guess). */
  label: 'upgrade' | 'downgrade' | null
  pair: { event_id: number; label: 'upgrade' | 'downgrade' | null } | null
  /** The "(change budget exceeded)" row, not a real change. */
  marker: boolean
  /** Recorded from a list that was cut at the agent's cap. */
  snapshot_truncated: boolean
  detail: Record<string, unknown>
}

export interface ChangesResponse {
  generated_at: string
  /** `tz` is the zone actually used (a canonical alias, or UTC when `tz_fallback`). */
  window: { since: string; until: string | null; tz: string; tz_fallback: boolean }
  items: ChangeRow[]
  total: number
  page: number
  page_size: number
  /** When change capture began (install-wide, not the first event); null before it is recorded. */
  history_start: string | null
  /** Events older than this many days are removed. */
  retention_days: number
  /** Over the window, honouring kind/asset/subject_key/q but NOT category/action/label. */
  stats: {
    total: number
    software: {
      added: number
      removed: number
      changed: number
      upgraded: number
      downgraded: number
      paired: { upgraded: number; downgraded: number }
    }
    os: number
    firmware: number
    hardware: number
  }
  series: { day: string; software: number; os: number; firmware: number; hardware: number; total: number }[]
}

/* ── Operating systems (GET /inventory/os) ──────────────────────────────── */

export interface OsVersionNode {
  version: string | null
  count: number
  /** For network firmware, a raw string that differs from the normalised version. */
  raw_example: string | null
}

export interface OsCurrency {
  latest: number
  previous: number
  older: number
  unknown: number
  latest_version: string
  previous_version: string | null
}

export interface OsReleaseNode {
  release: string | null
  count: number
  latest_version: string | null
  distinct_versions: number
  /** Null when the observed versions cannot all be ordered. */
  currency: OsCurrency | null
  /** Newest first, null last. */
  versions: OsVersionNode[]
}

export interface OsProductNode {
  /** Null = known family, no product. */
  product: string | null
  count: number
  releases: OsReleaseNode[]
}

export interface OsFamilyNode {
  family: string
  label: string
  count: number
  products: OsProductNode[]
}

export interface OsResponse {
  generated_at: string
  scope: ScopeInfo
  kind: AssetKind | null
  total: number
  families: OsFamilyNode[]
}

/* ── Hardware (GET /inventory/hardware) ─────────────────────────────────── */

export interface HardwareBucket {
  value: string
  label: string
  count: number
}

export interface HardwareVendorRow {
  /** Null = devices with no vendor. */
  vendor: string | null
  count: number
  /** Distinct KNOWN models (the same as `models_known`). */
  models: number
  models_known: number
  /** Devices of this vendor with no model. */
  unknown_model_devices: number
  firmware_versions: number
  serial_missing: number
  /** Devices that cannot report by design (ping-only, controller-managed); not in `serial_coverage_pct`. */
  not_collected: number
  /** Over the devices that can report; null when none can. */
  serial_coverage_pct: number | null
  modules: number
}

export interface HardwareModelRow {
  vendor: string
  /** Null = vendor known, model not. */
  model: string | null
  /** True for the row of devices with no model. */
  unknown: boolean
  count: number
  firmware_versions: number
  serial_missing: number
  not_collected: number
  serial_coverage_pct: number | null
  modules: number
}

export interface HardwareResponse {
  generated_at: string
  scope: ScopeInfo
  network: {
    devices: number
    /** Distinct known (vendor, model) pairs: the Overview's "hardware models". */
    models_known: number
    /** Devices with no model, whatever their vendor. */
    unknown_model_devices: number
    vendors: HardwareVendorRow[]
    /** One row per (vendor, model); a vendor's devices with no model are one extra row with `unknown: true`. */
    models: HardwareModelRow[]
  }
  /** Only when `vendor` was requested. */
  matrix: {
    vendor: string
    /** Columns: newest first, `unknown` last. */
    releases: string[]
    models: { model: string | null; unknown: boolean; count: number; cells: { release: string; count: number }[] }[]
  } | null
  modules: { class: string; class_label: string; model: string | null; count: number; devices: number; with_serial: number }[]
  servers: {
    total: number
    cpu_models: HardwareBucket[]
    cpu_models_more: { models: number; servers: number }
    memory: HardwareBucket[]
    disks: HardwareBucket[]
    virtualization: HardwareBucket[] | null
  }
}

/* ── Trends (GET /inventory/trends, unscoped users only) ────────────────── */

export interface TrendPoint {
  day: string
  value: number
}

export interface TrendSeries {
  dimension: string
  /** Only the days that have a snapshot. */
  points: TrendPoint[]
}

/**
 * Daily snapshot series of one metric family (server/app/schemas/inventory.py TrendsResponse). The page
 * reads it through `readTrends` (components/inventory/overviewModel.ts), which does not trust the shape:
 * no golden fixture covers this endpoint yet, so it is not part of the compile-time conformance check.
 */
export interface TrendsResponse {
  generated_at: string
  /** True for a scoped caller: no series are returned (fleet-wide data is not leaked). */
  restricted: boolean
  /** The owner's display switch (`trends_enabled`); recording continues when it is off. */
  enabled: boolean
  metric: string
  dimension: string | null
  days: number
  window: { since: string; until: string }
  first_day: string | null
  /** Distinct days over all series: the Overview draws a sparkline from three. */
  days_with_data: number
  dimensions_total: number
  truncated: boolean
  series: TrendSeries[]
}
