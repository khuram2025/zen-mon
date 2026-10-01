/**
 * Panel states (contract §10, NF-03).
 *
 * A NetFlow panel is in exactly one of eight states, and each is rendered differently: a panel
 * that is loading, failed, forbidden or has no coverage must never look like a successful zero
 * (the review found "0 B", "No active incidents" and "All security checks passed" on pending and
 * failed requests).
 *
 *  loading      no answer for the current request yet (also while previous data is shown as a placeholder)
 *  forbidden    403: the role or exporter scope does not include this data
 *  error        the request failed and there is nothing to show
 *  stale        a refresh failed; the last good data is still shown, with its time
 *  no_coverage  the window has no records at all from the exporters in scope: traffic is unknown
 *  expired      nothing retained can answer the window for this query (e.g. a raw-only filter such as
 *               DSCP on a range older than raw retention): unknown, not "no flows matched" (X-25)
 *  unsupported  the exporters do not provide the field this panel needs (e.g. TCP flags)
 *  empty        a successful query that genuinely matched nothing
 *  ok           data
 */

export type PanelStatus = 'loading' | 'forbidden' | 'error' | 'stale' | 'no_coverage' | 'expired' | 'unsupported' | 'empty' | 'ok'

/** Whether the window's records cover the panel: from `/netflow/coverage` or `meta`. */
export type CoverageState = 'covered' | 'none' | 'unknown'

/** The parts of a TanStack `UseQueryResult` the status needs (structural, so any query fits). */
export interface PanelQueryLike<D = unknown> {
  data: D | undefined
  error: unknown
  status: 'pending' | 'error' | 'success'
  isPlaceholderData?: boolean
  isFetching?: boolean
  dataUpdatedAt?: number
  refetch?: () => unknown
}

export interface PanelStatusOptions<D = unknown> {
  /** Whether a successful result matched nothing. Default: `isEmptyData`. */
  isEmpty?: (data: D) => boolean
  coverage?: CoverageState | null
  /** Truthy (a reason) when the telemetry this panel needs is not exported. */
  unsupported?: string | boolean | null
}

/**
 * Default emptiness: no data, an empty array, or an envelope/object whose `data`, `items`, `rows`
 * or `cells` list is empty. Aggregates (totals objects) are never "empty" by default — a panel
 * that shows totals decides that itself (e.g. `flows === 0`).
 */
export function isEmptyData(data: unknown): boolean {
  if (data == null) return true
  if (Array.isArray(data)) return data.length === 0
  if (typeof data !== 'object') return false
  const obj = data as Record<string, unknown>
  if ('data' in obj && 'meta' in obj) return isEmptyData(obj.data)
  for (const k of ['items', 'rows', 'cells']) {
    if (Array.isArray(obj[k])) return (obj[k] as unknown[]).length === 0
  }
  return false
}

interface EnvelopeLike {
  meta?: {
    window?: { effective_from?: string | null; effective_to?: string | null } | null
    retention?: { limited?: boolean } | null
    warnings?: { code: string; message: string }[] | null
  } | null
}

/**
 * True when an enveloped result answers nothing because of retention: the effective window is
 * empty (`effective_from >= effective_to`) and the API says retention limited it. Such a result
 * holds zeros/empty lists that mean "unknown", never "nothing matched".
 */
export function isExpiredResult(data: unknown): boolean {
  const meta = (data as EnvelopeLike | null)?.meta
  const w = meta?.window
  if (!meta?.retention?.limited || !w?.effective_from || !w?.effective_to) return false
  const ef = Date.parse(w.effective_from)
  const et = Date.parse(w.effective_to)
  return Number.isFinite(ef) && Number.isFinite(et) && ef >= et
}

/** The retention warnings of an expired result, for the panel's explanation. */
export function retentionWarnings(data: unknown): string[] {
  const warnings = (data as EnvelopeLike | null)?.meta?.warnings ?? []
  return warnings.filter((w) => /_retention$/.test(w.code) && w.message).map((w) => w.message)
}

function statusOfData<D>(data: D, opts: PanelStatusOptions<D>): PanelStatus {
  if (isExpiredResult(data)) return 'expired'
  if (opts.coverage === 'none') return 'no_coverage'
  if (opts.unsupported) return 'unsupported'
  if ((opts.isEmpty ?? isEmptyData)(data)) return 'empty'
  return 'ok'
}

export function panelStatus<D>(q: PanelQueryLike<D>, opts: PanelStatusOptions<D> = {}): PanelStatus {
  if (q.status === 'error') {
    // A revoked permission hides retained data too.
    if (httpStatus(q.error) === 403) return 'forbidden'
    return q.data !== undefined ? 'stale' : 'error'
  }
  if (q.data === undefined || q.status === 'pending') return 'loading'
  // keepPreviousData: the data on hand answers a different window or filter.
  if (q.isPlaceholderData) return 'loading'
  return statusOfData(q.data, opts)
}

/**
 * The state the placeholder data (previous window/filter) would have, so a panel can keep
 * showing a previous chart dimmed while loading — but only when that previous data was real.
 */
export function placeholderStatus<D>(q: PanelQueryLike<D>, opts: PanelStatusOptions<D> = {}): PanelStatus | null {
  if (!q.isPlaceholderData || q.data === undefined) return null
  return statusOfData(q.data, opts)
}

/* ─── Errors ───────────────────────────────────────────────────────── */

export function httpStatus(err: unknown): number | null {
  const s = (err as { response?: { status?: unknown } } | null)?.response?.status
  return typeof s === 'number' ? s : null
}

const REF_RE = /\(ref (nf-[0-9a-f]{8})\)/i

/** The API's error reference (`error_id`, or the "(ref nf-…)" suffix of `detail`). */
export function apiErrorRef(err: unknown): string | null {
  const data = (err as { response?: { data?: unknown } } | null)?.response?.data as Record<string, unknown> | undefined
  if (data && typeof data.error_id === 'string' && data.error_id) return data.error_id
  const detail = data?.detail
  if (typeof detail === 'string') {
    const m = REF_RE.exec(detail)
    if (m) return m[1]
  }
  return null
}

/** The API's machine-readable error code (`query_failed`, `query_timeout`, `invalid_filter`, …). */
export function apiErrorCode(err: unknown): string | null {
  const data = (err as { response?: { data?: unknown } } | null)?.response?.data as Record<string, unknown> | undefined
  return data && typeof data.code === 'string' ? data.code : null
}

function locText(loc: unknown): string {
  if (!Array.isArray(loc)) return ''
  // FastAPI prefixes the source ('query', 'body', 'path'); the field name is what users know.
  const parts = loc.filter((p) => p !== 'query' && p !== 'body' && p !== 'path' && p !== 'header')
  return parts.map(String).join('.')
}

/**
 * A human message for any request failure, always a string (never an object a React tree would
 * choke on). Handles string `detail`, FastAPI 422 validation arrays, `{msg}` objects, timeouts and
 * unreachable servers. The "(ref nf-…)" suffix is removed; show `apiErrorRef` next to it.
 */
export function apiErrorMessage(err: unknown, fallback = 'The request failed'): string {
  const e = err as {
    response?: { status?: number; data?: unknown }
    code?: string
    message?: string
  } | null
  if (!e) return fallback
  const status = e.response?.status
  const data = e.response?.data
  const detail = data && typeof data === 'object' ? (data as Record<string, unknown>).detail : undefined
  if (typeof detail === 'string' && detail.trim()) {
    if (status === 404 && detail === 'Not Found') return 'Not found: this data does not exist or is outside your access'
    return detail.replace(/\s*\(ref nf-[0-9a-f]{8}\)\s*$/i, '').trim() || fallback
  }
  if (Array.isArray(detail)) {
    const lines = detail
      .map((d) => {
        if (typeof d === 'string') return d
        if (d && typeof d === 'object') {
          const item = d as { loc?: unknown; msg?: unknown }
          const msg = typeof item.msg === 'string' ? item.msg : null
          if (!msg) return null
          const where = locText(item.loc)
          return where ? `${where}: ${msg}` : msg
        }
        return null
      })
      .filter((x): x is string => !!x)
    if (lines.length) return lines.join('; ')
  }
  if (detail && typeof detail === 'object') {
    const d = detail as { msg?: unknown; message?: unknown }
    if (typeof d.msg === 'string') return d.msg
    if (typeof d.message === 'string') return d.message
  }
  if (status === 403) return 'You do not have permission to view this data'
  if (status === 504 || e.code === 'ECONNABORTED') return 'The query took too long and was stopped'
  if (!e.response && e.code === 'ERR_NETWORK') return 'The appliance could not be reached'
  if (status) return `${fallback} (HTTP ${status})`
  return typeof e.message === 'string' && e.message ? e.message : fallback
}

/** Client errors (400–499) will fail identically on retry; 504 means the query budget ran out. */
export function isRetryableError(err: unknown): boolean {
  const s = httpStatus(err)
  if (s === null) return true
  if (s >= 400 && s < 500) return false
  return s !== 504
}

/* ─── Coverage ─────────────────────────────────────────────────────── */

interface CoverageLike {
  totals?: {
    records?: number | null
    tcp_records?: number | null
    tcp_flagged_records?: number | null
    flag_coverage_pct?: number | null
  } | null
}

/** 'none' when the exporters in scope delivered no records in the window; 'unknown' without data. */
export function coverageState(coverage: CoverageLike | null | undefined): CoverageState {
  const records = coverage?.totals?.records
  if (typeof records !== 'number') return 'unknown'
  return records > 0 ? 'covered' : 'none'
}

export type FieldSupport = 'supported' | 'partial' | 'unsupported' | 'unknown'

/**
 * Whether TCP flags were exported for the window's TCP records (NF-07): a template without the
 * flags field stores zeros that are not observations, so flag-based panels and detectors must
 * say "unavailable" rather than "no SYN/RST".
 */
export function tcpFlagSupport(coverage: CoverageLike | null | undefined): { support: FieldSupport; pct: number | null } {
  const t = coverage?.totals
  if (!t || typeof t.tcp_records !== 'number' || t.tcp_records === 0) return { support: 'unknown', pct: null }
  const pct =
    typeof t.flag_coverage_pct === 'number'
      ? t.flag_coverage_pct
      : typeof t.tcp_flagged_records === 'number'
        ? (100 * t.tcp_flagged_records) / t.tcp_records
        : null
  if (pct === null) return { support: 'unknown', pct: null }
  if (pct <= 0) return { support: 'unsupported', pct: 0 }
  return { support: pct >= 99.5 ? 'supported' : 'partial', pct }
}
