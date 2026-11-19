/**
 * Synthetic scenarios: the pure model behind the list and detail pages (types, URL state, formatting, chart shaping).
 * No runtime imports, so `scripts/test-synthetics-ui.mjs` compiles and runs it on its own.
 */

// ─── API shapes (/api/v1/apm/synthetics) ─────────────────────────────────────

export interface SynAssertion { type: string; operator?: string; value?: unknown; path?: string }
export interface SynExtract { var: string; from: string; path: string }
export interface SynStep {
  name: string; method: string; url: string
  headers?: Record<string, string>; body?: string
  assertions?: SynAssertion[]; extract?: SynExtract[]
}

export interface SynMonitor {
  id: string; name: string; steps: SynStep[]
  variables?: Record<string, string>; verify_tls?: boolean
  notify_channels?: string[]; check_interval: number; timeout: number
  retry_count: number; tags?: string[]; enabled: boolean; status: string
  last_check_at?: string | null; created_at?: string | null
  runs?: number; failures?: number; uptime_pct?: number | null
  avg_ms?: number | null; p95_ms?: number | null
  last_run_at?: string | null; last_failure_at?: string | null
  last_error?: string | null; last_failed_step?: string | null
  spark?: Array<number | null>
  secrets_masked?: boolean
}

export interface SynListResponse {
  monitors: SynMonitor[]
  range?: { from: string; to: string }
  summary: { total: number; up: number; down: number; disabled: number; runs?: number; failures?: number }
}

export interface SynTiming {
  connect_ms?: number | null; tls_ms?: number | null; send_ms?: number | null
  ttfb_ms?: number | null; download_ms?: number | null
}

export interface SynSeriesPoint { t: string; runs: number; failed: number; uptime_pct: number | null; avg_ms: number | null; p95_ms: number | null }

export interface SynStepStats {
  index: number; name: string; method: string; url: string
  executed: number; passed: number; failed: number; skipped: number
  pass_pct: number | null; avg_ms: number | null; p95_ms: number | null
  last_status: number | null; status_codes: number[]
  timing: SynTiming | null
  top_errors: Array<{ error: string; count: number; last_seen: string }>
  series: Array<{ t: string; avg_ms: number | null; failed: number }>
}

export interface SynErrorGroup { failed_step: string; error: string; count: number; first_seen: string; last_seen: string }
export interface SynOutage { start: string; end: string | null; ongoing: boolean; duration_s: number; failed_runs: number; failed_step: string; error: string }

export interface SynSummary {
  range: { from: string; to: string; bucket_seconds: number }
  totals: {
    runs: number; passed: number; failed: number; uptime_pct: number | null
    avg_ms: number | null; p50_ms: number | null; p95_ms: number | null; max_ms: number | null
    first_run_at: string | null; last_run_at: string | null; outages: number; downtime_s: number
  }
  series: SynSeriesPoint[]
  steps: SynStepStats[]
  errors: SynErrorGroup[]
  outages: SynOutage[]
}

export interface SynRunStepBrief { name: string; ok: boolean; status_code: number; ms: number }
export interface SynRun {
  timestamp: string; status: string; success: boolean; total_ms: number
  steps_total: number; steps_passed: number; failed_step: string; error: string
  steps: SynRunStepBrief[]
}
export interface SynRunsResponse { total: number; runs: SynRun[] }

export interface SynRunStep {
  name: string; method: string; url: string; ok: boolean; status_code: number; ms: number; error: string
  asserts: Array<{ type: string; ok: boolean; detail: string }>
  body_snippet?: string
  timing?: SynTiming | null
  server_ip?: string | null; response_bytes?: number | null; content_type?: string | null
  redirects?: number | null; http_version?: string | null
}
export interface SynRunDetail {
  timestamp?: string; status: string; success: boolean; total_ms: number
  steps_total: number; steps_passed: number; failed_step: string; error: string
  location?: string; steps: SynRunStep[]
}

export const SPARK_BUCKETS = 48
export const MASK = '••••••'
export const RUNS_PAGE_SIZE = 50

// ─── URL state ───────────────────────────────────────────────────────────────

/** Time-range parameters shared by the list and detail pages (see components/TimeRangePicker). */
export const RANGE_PARAMS = ['range', 'from', 'to'] as const

/** Only the time-range part of a query string, so a link keeps the range and drops page-local state. */
export function rangeQuery(search: string | URLSearchParams): string {
  const src = typeof search === 'string' ? new URLSearchParams(search) : search
  const out = new URLSearchParams()
  for (const k of RANGE_PARAMS) {
    const v = src.get(k)
    if (v) out.set(k, v)
  }
  return out.toString()
}

/** The detail page for a scenario, keeping the time range. */
export function detailHref(id: string, search: string | URLSearchParams = ''): string {
  const q = rangeQuery(search)
  return `/apm/synthetics/${encodeURIComponent(id)}${q ? `?${q}` : ''}`
}

/** The list page, keeping the time range. */
export function listHref(search: string | URLSearchParams = ''): string {
  const q = rangeQuery(search)
  return `/apm/synthetics${q ? `?${q}` : ''}`
}

/** A copy of the query string with one parameter set (or removed when value is null/empty). */
export function withParam(search: string | URLSearchParams, key: string, value: string | number | null | undefined): URLSearchParams {
  const next = new URLSearchParams(typeof search === 'string' ? search : search.toString())
  if (value == null || value === '') next.delete(key)
  else next.set(key, String(value))
  return next
}

/** `?step=<index>`: a non-negative integer below the step count, else null. */
export function parseStepParam(raw: string | null | undefined, stepCount: number): number | null {
  if (raw == null || !/^\d+$/.test(raw)) return null
  const n = Number(raw)
  return n < stepCount ? n : null
}

/** `?run=<timestamp>`: a parseable timestamp, else null. */
export function parseRunParam(raw: string | null | undefined): string | null {
  if (!raw) return null
  return Number.isFinite(Date.parse(raw)) ? raw : null
}

export type RunStatusFilter = 'all' | 'failed' | 'passed'

/** Query string for GET /{id}/runs. Timestamps keep their offset ("+00:00" is encoded, never a space). */
export function runsQuery(p: { from: string; to: string; status?: RunStatusFilter; step?: string | null; limit?: number; offset?: number }): string {
  const q = new URLSearchParams()
  q.set('from', p.from)
  q.set('to', p.to)
  q.set('status', p.status ?? 'all')
  if (p.step) q.set('step', p.step)
  q.set('limit', String(Math.min(500, Math.max(1, p.limit ?? RUNS_PAGE_SIZE))))
  q.set('offset', String(Math.max(0, p.offset ?? 0)))
  return q.toString()
}

/** Pager arithmetic for the runs table. */
export function pageInfo(total: number, offset: number, size = RUNS_PAGE_SIZE): { page: number; pages: number; first: number; last: number; hasPrev: boolean; hasNext: boolean } {
  const pages = Math.max(1, Math.ceil(total / size))
  const page = Math.min(pages, Math.floor(offset / size) + 1)
  return {
    page, pages,
    first: total ? offset + 1 : 0,
    last: Math.min(total, offset + size),
    hasPrev: offset > 0,
    hasNext: offset + size < total,
  }
}

/**
 * The time window to inspect for an outage: its span plus a margin each side, clamped to now. An ongoing outage
 * (no end) runs to now. Returned as ISO strings for the custom range.
 */
export function outageWindow(o: Pick<SynOutage, 'start' | 'end' | 'ongoing'>, nowMs: number, marginMs = 10 * 60_000): { from: string; to: string } | null {
  const start = Date.parse(o.start)
  if (!Number.isFinite(start)) return null
  const endRaw = o.ongoing || !o.end ? nowMs : Date.parse(o.end)
  const end = Number.isFinite(endRaw) ? endRaw : nowMs
  const from = start - marginMs
  const to = Math.min(nowMs, Math.max(end, start) + marginMs)
  if (!(from < to)) return null
  return { from: new Date(from).toISOString(), to: new Date(to).toISOString() }
}

// ─── Formatting ──────────────────────────────────────────────────────────────

/** "1h 5m", "3m 20s", "45s"; days for long spans. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) {
    const m = Math.floor(s / 60), r = s % 60
    return r ? `${m}m ${r}s` : `${m}m`
  }
  if (s < 86_400) {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
    return m ? `${h}h ${m}m` : `${h}h`
  }
  const d = Math.floor(s / 86_400), h = Math.floor((s % 86_400) / 3600)
  return h ? `${d}d ${h}h` : `${d}d`
}

/** Check interval: "every 60s", "every 5m", "every 1h". */
export function fmtInterval(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '—'
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds % 60 === 0 && seconds >= 120) return `${seconds / 60}m`
  return `${seconds}s`
}

/** Uptime percentage with two decimals below 100, so 99.95 never reads as 100. */
export function fmtUptime(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return '—'
  if (pct >= 100) return '100%'
  return `${(Math.floor(pct * 100) / 100).toFixed(2)}%`
}

/** Bytes as B / KB / MB. */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** Host and path of a step URL ("api.example.com/login"); template URLs ("{{base}}/x") are returned as written. */
export function hostPath(url: string | null | undefined): string {
  if (!url) return ''
  const m = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)([^?#]*)/i.exec(url)
  if (!m) return url
  const path = m[2] && m[2] !== '/' ? m[2] : ''
  return `${m[1]}${path}`
}

export type UptimeTone = 'ok' | 'warn' | 'bad' | 'none'

/** State colour for an uptime percentage: full availability ok, a few failed runs warn, more bad. */
export function uptimeTone(pct: number | null | undefined): UptimeTone {
  if (pct == null || !Number.isFinite(pct)) return 'none'
  if (pct >= 99.5) return 'ok'
  if (pct >= 90) return 'warn'
  return 'bad'
}

/** The 48-bucket availability sparkline: one bar per bucket, height from uptime (floor 15% so a failed bucket shows). */
export function sparkBars(spark: ReadonlyArray<number | null> | null | undefined, buckets = SPARK_BUCKETS): Array<{ i: number; pct: number | null; height: number; tone: UptimeTone }> {
  const src = spark ?? []
  return Array.from({ length: buckets }, (_, i) => {
    const raw = src[i]
    const pct = raw == null || !Number.isFinite(raw) ? null : Math.max(0, Math.min(100, Number(raw)))
    return { i, pct, height: pct == null ? 100 : Math.max(15, pct), tone: uptimeTone(pct) }
  })
}

/** The time span a spark bucket covers, for its tooltip. */
export function sparkBucketRange(i: number, fromISO: string, toISO: string, buckets = SPARK_BUCKETS): { from: number; to: number } | null {
  const a = Date.parse(fromISO), b = Date.parse(toISO)
  if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a) return null
  const span = (b - a) / buckets
  return { from: a + i * span, to: a + (i + 1) * span }
}

// ─── Timing breakdown ────────────────────────────────────────────────────────

export const TIMING_PHASES = [
  { key: 'connect_ms', label: 'Connect', series: 0 },
  { key: 'tls_ms', label: 'TLS', series: 1 },
  { key: 'send_ms', label: 'Send', series: 6 },
  { key: 'ttfb_ms', label: 'Wait', series: 2 },
  { key: 'download_ms', label: 'Download', series: 3 },
] as const

export interface TimingSegment { key: string; label: string; ms: number; pct: number; series: number }

/**
 * The phases of a request with time in them, as shares of their sum (connect / TLS / send / wait / download).
 * Missing or zero phases are left out; an empty list means no breakdown was recorded.
 */
export function timingSegments(t: SynTiming | null | undefined): TimingSegment[] {
  if (!t) return []
  const rows = TIMING_PHASES
    .map((p) => ({ key: p.key, label: p.label, series: p.series, ms: Number((t as Record<string, unknown>)[p.key]) }))
    .filter((r) => Number.isFinite(r.ms) && r.ms > 0)
  const total = rows.reduce((s, r) => s + r.ms, 0)
  if (!total) return []
  return rows.map((r) => ({ ...r, pct: (r.ms / total) * 100 }))
}

/** "Connect 12 ms · TLS 30 ms · Wait 80 ms · Download 4 ms". */
export function timingText(t: SynTiming | null | undefined, fmt: (ms: number) => string = (ms) => `${Math.round(ms)} ms`): string {
  return timingSegments(t).map((s) => `${s.label} ${fmt(s.ms)}`).join(' · ')
}

/**
 * Waterfall rows for a run: steps run one after another, so each starts where the previous ended.
 * Offsets and widths are percentages of the run's total.
 */
export function waterfall(steps: ReadonlyArray<Pick<SynRunStep, 'ms' | 'timing'>>): Array<{ offsetPct: number; widthPct: number; startMs: number; segments: TimingSegment[] }> {
  const total = steps.reduce((s, x) => s + Math.max(0, Number(x.ms) || 0), 0)
  let at = 0
  return steps.map((s) => {
    const ms = Math.max(0, Number(s.ms) || 0)
    const row = {
      offsetPct: total ? (at / total) * 100 : 0,
      widthPct: total ? Math.max(0.5, (ms / total) * 100) : 0,
      startMs: at,
      segments: timingSegments(s.timing ?? null),
    }
    at += ms
    return row
  })
}

// ─── Runs ────────────────────────────────────────────────────────────────────

export type ChipState = 'ok' | 'fail' | 'skipped'

/**
 * One chip per configured step for a run. Steps after a failure were not executed (skipped). Steps are matched by
 * position; names are taken from the run when present (the scenario may have been edited since).
 */
export function stepChips(run: Pick<SynRun, 'steps' | 'steps_total'>, configured: ReadonlyArray<{ name: string }> = []): Array<{ name: string; state: ChipState; status_code: number | null; ms: number | null }> {
  const executed = run.steps ?? []
  const count = Math.max(executed.length, run.steps_total || configured.length)
  return Array.from({ length: count }, (_, i) => {
    const s = executed[i]
    if (s) return { name: s.name || configured[i]?.name || `Step ${i + 1}`, state: s.ok ? 'ok' : 'fail', status_code: s.status_code || null, ms: s.ms ?? null }
    return { name: configured[i]?.name || `Step ${i + 1}`, state: 'skipped', status_code: null, ms: null }
  })
}

/** Runs grouped by calendar day (key from `dayKey`), newest first as received, with pass/fail counts. */
export function groupRunsByDay<T extends Pick<SynRun, 'timestamp' | 'success'>>(runs: ReadonlyArray<T>, dayKey: (iso: string) => string): Array<{ day: string; runs: T[]; failed: number }> {
  const out: Array<{ day: string; runs: T[]; failed: number }> = []
  for (const r of runs) {
    const day = dayKey(r.timestamp)
    let g = out[out.length - 1]
    if (!g || g.day !== day) { g = { day, runs: [], failed: 0 }; out.push(g) }
    g.runs.push(r)
    if (!r.success) g.failed += 1
  }
  return out
}

// ─── Charts ──────────────────────────────────────────────────────────────────

/** Per-step duration series aligned on bucket time: rows `{ t, s0, s1, … }` for a stacked chart. */
export function stackStepSeries(steps: ReadonlyArray<Pick<SynStepStats, 'index' | 'series'>>): Array<Record<string, string | number | null>> {
  const rows = new Map<string, Record<string, string | number | null>>()
  for (const s of steps) {
    for (const p of s.series ?? []) {
      let row = rows.get(p.t)
      if (!row) { row = { t: p.t }; rows.set(p.t, row) }
      row[`s${s.index}`] = p.avg_ms == null ? null : Number(p.avg_ms)
    }
  }
  return [...rows.values()].sort((a, b) => Date.parse(String(a.t)) - Date.parse(String(b.t)))
}

/** Availability bars: passed and failed runs per bucket (empty buckets have no runs). */
export function availabilityBars(series: ReadonlyArray<SynSeriesPoint>): Array<{ t: string; passed: number; failed: number; uptime_pct: number | null }> {
  return series.map((p) => ({
    t: p.t,
    passed: Math.max(0, (p.runs || 0) - (p.failed || 0)),
    failed: Math.max(0, p.failed || 0),
    uptime_pct: p.uptime_pct,
  }))
}

// ─── Configuration ───────────────────────────────────────────────────────────

const OPERATOR_TEXT: Record<string, string> = { eq: '=', neq: '≠', lt: '<', lte: '≤', gt: '>', gte: '≥', exists: 'exists' }
const ASSERT_TEXT: Record<string, string> = { status_code: 'Status code', latency_ms: 'Latency (ms)', body_contains: 'Body contains', json_path: 'JSON path' }

/** "Status code = 200", "JSON path user.id exists", "Body contains ••••••" (text values hidden when masked). */
export function describeAssertion(a: SynAssertion, masked = false): string {
  const type = a.type || 'status_code'
  const label = ASSERT_TEXT[type] ?? type
  const path = type === 'json_path' && a.path ? ` ${a.path}` : ''
  if (a.operator === 'exists') return `${label}${path} exists`
  const raw = a.value
  const isNumber = typeof raw === 'number' || (typeof raw === 'string' && raw.trim() !== '' && Number.isFinite(Number(raw)))
  const value = raw == null || raw === '' ? '' : masked && !isNumber ? MASK : typeof raw === 'string' ? raw : JSON.stringify(raw)
  if (type === 'body_contains') return `${label} ${value ? `"${value}"` : ''}`.trim()
  const op = OPERATOR_TEXT[a.operator ?? 'eq'] ?? a.operator ?? '='
  return `${label}${path} ${op} ${value}`.trim()
}

/** Variables as editable text, one `NAME=value` per line. */
export function formatVariables(vars: Record<string, string> | null | undefined): string {
  return Object.entries(vars ?? {}).map(([k, v]) => `${k}=${v}`).join('\n')
}

/** Parse `NAME=value` lines (blank lines and lines without a name are ignored; the first "=" splits). */
export function parseVariables(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const i = line.indexOf('=')
    if (i <= 0) continue
    const k = line.slice(0, i).trim()
    if (k) out[k] = line.slice(i + 1).trim()
  }
  return out
}

/**
 * The PUT/POST body for a scenario, from its current configuration plus changes. Every stored field is sent
 * (variables, TLS verification and tags included), so toggling `enabled` or saving the editor never resets them.
 */
export function monitorPayload(m: Partial<SynMonitor> & Pick<SynMonitor, 'name' | 'steps'>, patch: Partial<SynMonitor> = {}): Record<string, unknown> {
  const merged = { ...m, ...patch }
  return {
    name: merged.name,
    steps: (merged.steps ?? []).map((s) => ({
      name: s.name,
      method: s.method || 'GET',
      url: s.url,
      ...(s.headers && Object.keys(s.headers).length ? { headers: s.headers } : {}),
      ...(s.body ? { body: s.body } : {}),
      assertions: (s.assertions ?? []).filter((a) => a && a.type),
      extract: (s.extract ?? []).filter((e) => e && e.var && e.path),
    })),
    variables: merged.variables ?? {},
    verify_tls: merged.verify_tls ?? true,
    notify_channels: merged.notify_channels ?? [],
    check_interval: Number(merged.check_interval) || 60,
    timeout: Number(merged.timeout) || 30,
    retry_count: merged.retry_count == null || !Number.isFinite(Number(merged.retry_count)) ? 1 : Number(merged.retry_count),
    enabled: merged.enabled ?? true,
    tags: merged.tags ?? [],
  }
}

/** True when a masked value came back from the server (the configuration cannot be saved as shown). */
export function hasMaskedValues(m: Pick<SynMonitor, 'variables' | 'steps' | 'secrets_masked'>): boolean {
  if (m.secrets_masked) return true
  if (Object.values(m.variables ?? {}).some((v) => v === MASK)) return true
  return (m.steps ?? []).some((s) => s.body === MASK || Object.values(s.headers ?? {}).some((v) => v === MASK))
}
