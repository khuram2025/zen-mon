/**
 * The server (agent) metrics a server rule can watch, shared by the server rule editor and the condition-tree
 * builder (conditionTree.ts). No imports: both depend on it.
 */

/**
 * pct: a reading compared with a limit (percentages and, despite the name, the depth metrics: what the host
 * API calls a threshold metric, with a clear level and a clear hold); svc/proc: a named service or process;
 * flag: agent offline; event: restart/shutdown; auto: an automatic service stopped (no limit; the target
 * lists services to leave out).
 */
export type ServerMetricKind = 'pct' | 'svc' | 'proc' | 'flag' | 'event' | 'auto'

export type ServerMetric = {
  value: string
  label: string
  kind: ServerMetricKind
  /** Appended to the limit: '%', ' ms', ' errors/s', ' s', ' days'. */
  unit?: string
  /** Operators the host API accepts for it (default: all six). */
  operators?: readonly string[]
  /** The limit a new rule starts with. */
  defaultThreshold?: number
  min?: number
  max?: number
  /** Shown under the condition. */
  hint?: string
}

export const SERVER_METRICS: ReadonlyArray<ServerMetric> = [
  { value: 'host_cpu_pct', label: 'CPU usage (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_memory_pct', label: 'Memory usage (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_swap_pct', label: 'Swap usage (%)', kind: 'pct', unit: '%', defaultThreshold: 80, min: 0, max: 100,
    hint: 'Swap (page file) in use, averaged over the hold. Servers without swap are not judged.' },
  { value: 'host_filesystem_pct', label: 'Filesystem usage (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_fs_days_left', label: 'Filesystem full in (days)', kind: 'pct', unit: ' days', operators: ['<', '<='], defaultThreshold: 7, min: 0,
    hint: 'Days until each filesystem is full at its growth over the last 7 days (needs a day of history; a filesystem that is not filling is healthy). Clears one day above the limit unless a reset value is set.' },
  { value: 'host_disk_util_pct', label: 'Disk I/O utilization (%)', kind: 'pct', unit: '%', defaultThreshold: 90, min: 0, max: 100 },
  { value: 'host_disk_latency_ms', label: 'Disk latency (ms)', kind: 'pct', unit: ' ms', defaultThreshold: 50, min: 0,
    hint: 'The slower of the average read and write time per I/O, per disk, averaged over the hold. Linux agents only: Windows servers are skipped until the agent reports real per-I/O times.' },
  { value: 'host_nic_errors', label: 'Network interface errors (per second)', kind: 'pct', unit: ' errors/s', defaultThreshold: 10, min: 0,
    hint: 'Receive plus transmit errors per second, per network interface, averaged over the hold.' },
  { value: 'host_clock_skew_s', label: 'Clock offset from the appliance (s)', kind: 'pct', unit: ' s', defaultThreshold: 60, min: 0,
    hint: 'How far the server’s clock is from the appliance’s, measured at every upload; every sample must breach for the hold.' },
  { value: 'host_service_down', label: 'Service stopped', kind: 'svc' },
  { value: 'host_auto_service_stopped', label: 'Automatic service stopped', kind: 'auto',
    hint: 'Any service set to start automatically that stopped unexpectedly (non-zero exit code, or failed), one alert per service. Services that end cleanly (delayed-start, trigger-start, one-shot) are not reported; to watch every stop of one service, use a Service stopped rule.' },
  { value: 'host_process_down', label: 'Process not running', kind: 'proc' },
  { value: 'host_agent_offline', label: 'Agent offline', kind: 'flag' },
  { value: 'host_restart', label: 'Server restart / unexpected shutdown', kind: 'event' },
  { value: 'host_shutdown', label: 'Server shutdown requested / reported', kind: 'event' },
]

export function serverMetric(metric: string | null | undefined): ServerMetric | undefined {
  return SERVER_METRICS.find((m) => m.value === metric)
}

/** The operators a server rule may use for this metric. */
export function serverOperators(metric: string): readonly string[] {
  return serverMetric(metric)?.operators || ['>', '>=', '<', '<=', '==', '!=']
}

/** Whether the metric's limit is a percentage (0-100). */
export function isServerPercent(metric: string): boolean {
  return serverMetric(metric)?.unit === '%'
}


export const OP_SYMBOL: Record<string, string> = { gt: '>', gte: '>=', lt: '<', lte: '<=', eq: '==', neq: '!=' }

export function isServerMetric(metric: unknown): boolean {
  return String(metric || '').startsWith('host_')
}

export function serverMetricKind(metric: string): ServerMetricKind {
  return SERVER_METRICS.find((m) => m.value === metric)?.kind || 'pct'
}

export function serverMetricLabel(metric: string): string {
  return serverMetric(metric)?.label || metric
}
