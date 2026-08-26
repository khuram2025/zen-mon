import { useSearchParams } from 'react-router-dom'
import { Clock } from 'lucide-react'
import { Switch } from '@/components/ui/Switch'

/**
 * The APM range vocabulary, shared by every page.
 *
 * Pages used to declare their own arrays — Services offered 15m/1h/6h/24h,
 * Errors offered 1h/6h/24h/7d — so moving between screens silently changed the
 * window you were looking at. One list, one `?range=` param, one control.
 *
 * These keys are exactly what the APM API accepts as `?range=`.
 */
export const APM_RANGES = ['15m', '1h', '6h', '24h', '7d'] as const
export type ApmRangeKey = typeof APM_RANGES[number]

export const RANGE_MS: Record<ApmRangeKey, number> = {
  '15m': 15 * 60_000,
  '1h': 3_600_000,
  '6h': 6 * 3_600_000,
  '24h': 24 * 3_600_000,
  '7d': 7 * 24 * 3_600_000,
}

export const RANGE_LABEL: Record<ApmRangeKey, string> = {
  '15m': '15m', '1h': '1h', '6h': '6h', '24h': '24h', '7d': '7d',
}

/** Mid-sentence phrasing: "No errors in the last 24h". */
export function rangePhrase(range: ApmRangeKey): string {
  return `the last ${RANGE_LABEL[range]}`
}

export function previousWindow(range: ApmRangeKey, now = Date.now()): { from_ms: number; to_ms: number; windowMs: number } {
  const windowMs = RANGE_MS[range]
  const to_ms = now - windowMs
  return { from_ms: to_ms - windowMs, to_ms, windowMs }
}

export function shiftTimestamp(iso: string, offsetMs: number): string {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return iso
  return new Date(t + offsetMs).toISOString()
}

export function useApmCompare(): [boolean, (v: boolean) => void] {
  const [params, setParams] = useSearchParams()
  const on = params.get('compare') === '1'
  const set = (v: boolean) => {
    const next = new URLSearchParams(params)
    if (v) next.set('compare', '1')
    else next.delete('compare')
    setParams(next, { replace: true })
  }
  return [on, set]
}

export function useApmRange(fallback: ApmRangeKey = '1h'): [ApmRangeKey, (r: ApmRangeKey) => void] {
  const [params, setParams] = useSearchParams()
  const raw = params.get('range')
  const range = (APM_RANGES as readonly string[]).includes(raw || '') ? (raw as ApmRangeKey) : fallback
  const set = (r: ApmRangeKey) => {
    const next = new URLSearchParams(params)
    next.set('range', r)
    setParams(next, { replace: true })
  }
  return [range, set]
}

interface Props {
  value: ApmRangeKey
  onChange: (r: ApmRangeKey) => void
  /** Restrict the offered presets (the trace live view only makes sense short). */
  options?: readonly ApmRangeKey[]
  className?: string
}

export function ApmRangePicker({ value, onChange, options = APM_RANGES, className = '' }: Props) {
  return (
    <div
      role="tablist"
      aria-label="Time range"
      className={`inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/40 p-0.5 ${className}`}
    >
      <Clock className="ml-1 mr-0.5 h-3 w-3 text-muted" aria-hidden />
      {options.map((r) => {
        const active = value === r
        return (
          <button
            key={r}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(r)}
            className={`rounded px-2 py-1 text-[11px] font-semibold transition-colors ${
              active ? 'bg-primary text-black' : 'text-muted hover:text-text'
            }`}
          >
            {RANGE_LABEL[r]}
          </button>
        )
      })}
    </div>
  )
}

export function CompareToggle({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-md border border-border bg-surface2/40 px-2.5 text-[11px] font-semibold text-muted">
      <Switch checked={value} onCheckedChange={onChange} />
      Compare previous
    </label>
  )
}
