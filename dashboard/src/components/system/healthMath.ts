// Pure helpers and types for Settings -> System status (the appliance's own health).
// Self-contained (no app imports) so scripts/test-system-status.mjs can exercise them directly.
// A reading the appliance could not take is null and is shown as "—" (unavailable), never as zero.

export type ServiceState = 'running' | 'down' | 'stopped' | 'starting' | 'not_installed' | 'unknown'

export type ServiceRow = {
  id: string
  label: string
  component: string
  core: boolean
  state: ServiceState
  detail: string | null
  uptime_s: number | null
  memory_bytes: number | null
  cpu_seconds: number | null
  restarts: number | null
  version: string | null
}

export type Finding = { severity: 'critical' | 'warning'; code: string; message: string }

export type DataStore = {
  id: string
  label: string
  component: string
  holds: string
  size_bytes: number | null
  kind: 'disk' | 'memory'
}

export type SystemStatus = {
  sampled_at: string
  state: 'ok' | 'warning' | 'critical'
  findings: Finding[]
  host: { os: string | null; cpu_model: string | null; cores: number | null; uptime_s: number | null; boot_time: string | null }
  cpu: {
    usage_pct: number | null
    iowait_pct: number | null
    steal_pct: number | null
    window_s: number | null
    cores: number | null
    load: number[] | null
    average_15m_pct: number | null
  }
  memory: {
    total_bytes: number | null
    available_bytes: number | null
    used_bytes: number | null
    used_pct: number | null
    swap_total_bytes: number | null
    swap_used_bytes: number | null
    swap_used_pct: number | null
  }
  disk: { used_bytes: number; free_bytes: number; used_pct: number } | null
  services: ServiceRow[]
  data_stores: DataStore[]
  ingestion: { monitoring: 'ready' | 'reconnecting' | 'interrupted' | null; uploads_paused: boolean | null }
  software: {
    version: string | null
    installed_at: string | null
    last_update: { version: string | null; status: string | null; at: string | null } | null
  }
  clock: { now: string; timezone: string | null; ntp_synchronized: boolean | null }
}

export type HistoryPoint = {
  t: number
  cpu: number | null
  cpu_max: number | null
  iowait: number | null
  mem: number | null
  mem_max: number | null
  swap: number | null
  load: number | null
  disk: number | null
}

export type SystemHistory = {
  available: boolean
  bucket_s: number
  hours: number
  since: string | null
  samples: number
  points: HistoryPoint[]
  disk_change: { bytes: number; hours: number } | null
}

export type Tone = 'primary' | 'warning' | 'danger' | 'muted'

export const UNAVAILABLE = '—'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** "12.3%"; one decimal below 10 so a nearly idle CPU does not read as 0. */
export function pct(v: number | null | undefined): string {
  if (!isNum(v)) return UNAVAILABLE
  return `${v < 10 ? v.toFixed(1) : Math.round(v)}%`
}

/** Binary units, labelled as such (GiB), like the storage sections. */
export function fmtBytes(n: number | null | undefined, decimals = 1): string {
  if (!isNum(n) || n < 0) return UNAVAILABLE
  if (n === 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)))
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : decimals)} ${units[i]}`
}

/** "12d 4h", "3h 12m", "7m", "45s". */
export function duration(seconds: number | null | undefined): string {
  if (!isNum(seconds) || seconds < 0) return UNAVAILABLE
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${Math.floor(seconds)}s`
}

/** CPU time a service has used since it started: "2h 05m", "3m 12s", "8s". */
export function cpuTime(seconds: number | null | undefined): string {
  if (!isNum(seconds) || seconds < 0) return UNAVAILABLE
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

/** A tile is quiet until its reading needs attention; a missing reading is muted, not green. */
export function usageTone(v: number | null | undefined, warn: number, crit: number): Tone {
  if (!isNum(v)) return 'muted'
  return v >= crit ? 'danger' : v >= warn ? 'warning' : 'primary'
}

/** Load is judged per CPU core: 4.0 on 16 cores is idle, on 2 cores it is a queue. */
export function loadTone(load: number[] | null | undefined, cores: number | null | undefined): Tone {
  if (!load || !isNum(load[2]) || !isNum(cores) || cores <= 0) return 'muted'
  const perCore = load[2] / cores
  return perCore >= 3 ? 'danger' : perCore >= 1.5 ? 'warning' : 'primary'
}

export const SERVICE_STATE: Record<ServiceState, { label: string; dot: 'up' | 'down' | 'warn' | 'info' | 'idle'; text: string }> = {
  running: { label: 'Running', dot: 'up', text: 'text-success-fg' },
  down: { label: 'Down', dot: 'down', text: 'text-danger-fg' },
  starting: { label: 'Starting', dot: 'info', text: 'text-info-fg' },
  stopped: { label: 'Switched off', dot: 'idle', text: 'text-muted' },
  not_installed: { label: 'Not installed', dot: 'idle', text: 'text-muted' },
  unknown: { label: 'Could not be checked', dot: 'idle', text: 'text-muted' },
}

export function serviceStateLabel(s: Pick<ServiceRow, 'state' | 'detail'>): string {
  const base = (SERVICE_STATE[s.state] || SERVICE_STATE.unknown).label
  return s.state === 'down' && s.detail ? `${base} (${s.detail})` : base
}

/** "3 of 4 core services running" for the summary line; unknown rows are not counted as running. */
export function serviceSummary(services: ServiceRow[]): { running: number; total: number; down: number; unknown: number } {
  const core = services.filter((s) => s.core)
  return {
    running: core.filter((s) => s.state === 'running').length,
    total: core.length,
    down: services.filter((s) => s.state === 'down').length,
    unknown: core.filter((s) => s.state === 'unknown').length,
  }
}

export const INGESTION_LABEL: Record<string, { label: string; tone: Tone; hint: string }> = {
  ready: { label: 'Processing results', tone: 'primary', hint: 'Polling results are being taken in normally.' },
  reconnecting: { label: 'Catching up after a restart', tone: 'warning', hint: 'Sensors and agents are reconnecting; status alerts are held until they have reported.' },
  interrupted: { label: 'Interrupted', tone: 'danger', hint: 'Results are not being processed. Status and alerts may be out of date.' },
}

/** Series for the history chart: [epoch ms, value | null]. Null keeps a gap where nothing was sampled. */
export function chartSeries(points: HistoryPoint[], key: keyof HistoryPoint): [number, number | null][] {
  return points.map((p) => [p.t * 1000, isNum(p[key]) ? (p[key] as number) : null])
}

/** Highest and mean of a reading over the points that have it; null when none do. */
export function seriesStats(points: HistoryPoint[], key: keyof HistoryPoint, peakKey?: keyof HistoryPoint): { mean: number; peak: number } | null {
  const values = points.map((p) => p[key]).filter(isNum)
  if (!values.length) return null
  const peaks = peakKey ? points.map((p) => p[peakKey]).filter(isNum) : values
  return {
    mean: Math.round((values.reduce((a, b) => a + b, 0) / values.length) * 10) / 10,
    peak: Math.max(...(peaks.length ? peaks : values)),
  }
}

/** How much of the requested window actually has samples ("Collected for 38 minutes so far"). */
export function historyCoverage(h: Pick<SystemHistory, 'points' | 'bucket_s' | 'hours'>): { covered_s: number; complete: boolean } {
  const filled = h.points.filter((p) => isNum(p.cpu) || isNum(p.mem)).length
  const covered = filled * h.bucket_s
  return { covered_s: covered, complete: covered >= h.hours * 3600 * 0.9 }
}

/** "+2.3 GiB over the last 23.5 hours" for the data volume, or null when it was not measured long enough. */
export function storageChange(change: SystemHistory['disk_change']): string | null {
  if (!change || !isNum(change.bytes) || !isNum(change.hours) || change.hours <= 0) return null
  const hours = change.hours >= 10 ? Math.round(change.hours) : change.hours
  const span = `over the last ${hours} hour${hours === 1 ? '' : 's'}`
  // Under a MiB of movement on a disk is noise, not a trend.
  if (Math.abs(change.bytes) < 1024 ** 2) return `Storage use is unchanged ${span}.`
  return `Storage use ${change.bytes > 0 ? 'grew' : 'fell'} by ${fmtBytes(Math.abs(change.bytes))} ${span}.`
}

/** Update outcome in plain words. */
export function updateStatusLabel(status: string | null | undefined): string {
  if (status === 'success') return 'installed'
  if (status === 'failed') return 'failed'
  if (status === 'rolled_back') return 'rolled back'
  return status || UNAVAILABLE
}

export function clockLabel(clock: SystemStatus['clock']): { text: string; tone: Tone } {
  if (clock.ntp_synchronized === true) return { text: 'Synchronised with a time server', tone: 'primary' }
  if (clock.ntp_synchronized === false) return { text: 'Not synchronised', tone: 'warning' }
  return { text: 'Synchronisation could not be checked', tone: 'muted' }
}
