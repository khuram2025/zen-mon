/**
 * Response shapes shared across NetFlow pages (contract §9.3 and §11). Fields the API may omit on
 * older appliances are optional; `null` means "unknown", never zero.
 */
import type { NfMetaWindow } from './window'

export type NfDataSource = NonNullable<NfMetaWindow['source']>

export interface NfWarning {
  code: string
  message: string
}

export interface NfMetaRetention {
  raw_days: number | null
  rollup_days: number | null
  interface_days: number | null
  /** Part of the requested window lies beyond a source's retention. */
  limited: boolean
}

export interface NfTruncation {
  limit: number
  returned: number
  more: boolean
}

export interface NfSampling {
  /** Share (0..1) of records that were sampled; null when unknown. */
  sampled_share: number | null
  max_interval: number | null
}

/** The `meta` block every enveloped read returns. */
export interface NfMeta {
  window: NfMetaWindow | null
  server_time: string | null
  query_ms: number | null
  /** The caller's visibility scope restricted the result. */
  restricted: boolean
  filters: Record<string, unknown>
  retention: NfMetaRetention | null
  truncated: NfTruncation | null
  sampling: NfSampling | null
  warnings: NfWarning[]
}

export interface NfResult<T> {
  data: T
  /** null when the endpoint does not (yet) return the envelope. */
  meta: NfMeta | null
}

/** `GET /netflow/retention`. */
/** Retention configured in Settings → Storage per flow table (null: install-time TTL applies). */
export interface NfRetentionConfigured {
  raw_days: number | null
  rollup_days: number | null
  qos_days: number | null
  interface_days: number | null
  exporter_minutes_days: number | null
}

/** NetFlow v5 sampling repair (X-02) outcome: `netflow_rollup.repair_status()` plus `affected`. */
export interface NfSamplingRepair {
  reconciled_at: string | null
  verified_from: string | null
  /** Rollup table -> hours still queued for a rebuild. */
  pending_hours: Record<string, number>
  /** Rollup table -> hours rebuilt from corrected raw. */
  rebuilt_hours: Record<string, number>
  /** Rollup table -> hours that could not be corrected (raw already expired). */
  unrecoverable_hours: Record<string, number>
  rows_found_after_upgrade: number
  /** True when this appliance held sampled v5 traffic the repair had to act on. */
  affected: boolean
}

/** `GET /netflow/retention` (and `retention` inside `/netflow/coverage`, without the last two keys). */
export interface NfRetention {
  raw_days: number | null
  rollup_days: number | null
  qos_days?: number | null
  interface_days: number | null
  exporter_minutes_days: number | null
  earliest_raw: string | null
  earliest_rollup: string | null
  earliest_qos?: string | null
  earliest_interface: string | null
  earliest_exporter_minutes?: string | null
  /** False when the table TTLs could not be read (every value then null = unknown). */
  known?: boolean
  /** Only on `/netflow/retention`; null when the storage settings cannot be read. */
  configured?: NfRetentionConfigured | null
  /** Only on `/netflow/retention`; null when the repair state cannot be read (unknown, not "nothing"). */
  sampling_repair?: NfSamplingRepair | null
}

export interface NfCoverageCounts {
  records: number
  tcp_records: number
  tcp_flagged_records: number
  v6_records: number
  sampled_records: number
  max_sampling: number | null
  legacy_records: number
}

export interface NfCoverageExporter extends NfCoverageCounts {
  exporter: string
  /** Minutes in which the exporter delivered countable records. */
  minutes_with_data: number
  /** Minutes in which anything at all arrived (also NSEL create/deny-only minutes). */
  minutes_with_receipts?: number
  minutes_in_window: number
  /** minutes_with_data / minutes_in_window in percent; null when the window has no measured minutes. */
  coverage_pct?: number | null
  first_received_at: string | null
  last_received_at: string | null
  /** Percent (0..100) of TCP records whose flags were exported; null when there were none. */
  flag_coverage_pct?: number | null
  /** Share (0..1) of records that were sampled. */
  sampled_share?: number | null
}

export interface NfCoverageTotals extends Partial<NfCoverageCounts> {
  minutes_with_data?: number | null
  minutes_with_receipts?: number | null
  minutes_in_window?: number | null
  coverage_pct?: number | null
  first_received_at?: string | null
  last_received_at?: string | null
  /** Percent (0..100) of TCP records whose flags were exported; null when there were none. */
  flag_coverage_pct?: number | null
  /** Share (0..1) of records that were sampled. */
  sampled_share?: number | null
}

/** `GET /netflow/coverage`. */
export interface NfCoverage {
  window?: (Partial<NfMetaWindow> & { measured_from?: string | null; measured_to?: string | null; minutes_in_window?: number }) | null
  exporters: NfCoverageExporter[]
  totals: NfCoverageTotals
  retention?: NfRetention | null
}

export type NfExporterState = 'receiving' | 'late' | 'silent' | 'never'

/** One row of `GET /netflow/exporters/status`. */
export interface NfExporterStatusRow {
  exporter: string
  display_name: string | null
  device_id: string | null
  device_hostname: string | null
  device_status: string | null
  last_received_at: string | null
  receipt_age_s: number | null
  state: NfExporterState
  records_15m: number | null
  expected_interval_s: number | null
  clock_offset_ms: number | null
  clock_suspect: boolean | string | null
  export_protocols: string[]
  loss_pct_1h: number | null
  flags_exported: boolean | null
}

/** `GET /netflow/exporters/status` — independent of page filters and window. */
export interface NfExporterStatus {
  server_time: string
  thresholds: { stale_s: number; silent_s: number }
  exporters: NfExporterStatusRow[]
}
