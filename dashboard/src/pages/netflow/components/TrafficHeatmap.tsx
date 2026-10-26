import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useTheme } from '@/stores/theme'
import { cn } from '@/lib/utils'
import { dowName, formatBytes, formatDateTime, hourRangeLabel } from '@/lib/netflow/format'
import {
  HEAT_COLORS,
  HEAT_STATE_TEXT,
  HOUR_TICKS,
  heatBin,
  heatLegend,
  type HeatCell,
  type HeatmapPayload,
} from '../overviewModel'

// ─────────────────────────────────────────────────────────────────
// Traffic heatmap, weekday × hour (NF-28). Cells are evaluated in the
// response's time zone, valued as the average hour (bytes per covered
// occurrence) or the total, coloured on a numeric sequential blue scale,
// and a collection gap is hatched instead of reading as idle traffic.
// Keyboard: one tab stop, arrow keys move, Enter/Space filters.
// ─────────────────────────────────────────────────────────────────

const DOWS = [1, 2, 3, 4, 5, 6, 7]
const HOURS = Array.from({ length: 24 }, (_, i) => i)

export interface TrafficHeatmapProps {
  data: HeatmapPayload
  activeHour?: number | null
  activeDow?: number | null
  onSelect?: (dow: number, hour: number) => void
}

function unitSuffix(basis: HeatmapPayload['basis']): string {
  return basis === 'avg' ? '/h' : ''
}

export function heatCellLabel(c: HeatCell, basis: HeatmapPayload['basis'], tz: string): string {
  const when = `${dowName(c.dow)} ${hourRangeLabel(c.hour)} (${tz})`
  if (c.state === 'outside_window') return `${when}: ${HEAT_STATE_TEXT.outside_window}`
  if (c.value == null) return `${when}: unknown, ${HEAT_STATE_TEXT[c.state]}`
  const value = `${formatBytes(c.value)}${basis === 'avg' ? ' per hour on average' : ' in total'}`
  const occ = c.occurrences ? ` (${c.covered_occurrences} of ${c.occurrences} hour${c.occurrences === 1 ? '' : 's'} with records)` : ''
  return `${when}: ${value}, ${HEAT_STATE_TEXT[c.state]}${occ}`
}

export function TrafficHeatmap({ data, activeHour, activeDow, onSelect }: TrafficHeatmapProps) {
  const theme = useTheme((s) => s.theme)
  const ramp = HEAT_COLORS[theme === 'dark' ? 'dark' : 'light']
  const hatch = theme === 'dark' ? '100,116,139' : '148,163,184'
  const byKey = useMemo(() => new Map(data.cells.map((c) => [`${c.dow}-${c.hour}`, c])), [data.cells])
  const max = data.max_value || 0
  const legend = heatLegend(max)
  const [focus, setFocus] = useState<number>(() => {
    const i = activeDow && activeHour != null ? (activeDow - 1) * 24 + activeHour : 0
    return Math.max(0, i)
  })
  const refs = useRef<(HTMLButtonElement | HTMLDivElement | null)[]>([])

  const styleFor = (c: HeatCell | undefined) => {
    if (!c || c.state === 'outside_window') return { background: 'transparent' }
    if (c.state === 'no_coverage') return { background: `repeating-linear-gradient(45deg, rgba(${hatch},0.55) 0 2px, transparent 2px 5px)` }
    if (c.state === 'unknown') return { background: `repeating-linear-gradient(135deg, rgba(${hatch},0.25) 0 1px, transparent 1px 4px)` }
    if (c.state === 'zero') return { background: 'rgb(var(--surface2))' }
    const bin = heatBin(c.value, max)
    const base = bin < 0 ? 'rgb(var(--surface2))' : ramp[bin]
    if (c.state === 'partial') {
      return { background: `linear-gradient(225deg, rgb(var(--text2)) 0 4px, transparent 4px), ${base}` }
    }
    return { background: base }
  }

  const move = (e: KeyboardEvent, index: number) => {
    const row = Math.floor(index / 24)
    const col = index % 24
    let next = index
    if (e.key === 'ArrowRight') next = row * 24 + Math.min(23, col + 1)
    else if (e.key === 'ArrowLeft') next = row * 24 + Math.max(0, col - 1)
    else if (e.key === 'ArrowDown') next = Math.min(6, row + 1) * 24 + col
    else if (e.key === 'ArrowUp') next = Math.max(0, row - 1) * 24 + col
    else if (e.key === 'Home') next = row * 24
    else if (e.key === 'End') next = row * 24 + 23
    else return
    e.preventDefault()
    setFocus(next)
    refs.current[next]?.focus()
  }

  const tzShort = data.tz

  return (
    <div className="space-y-2">
      <div role="group" aria-label={`Traffic by weekday and hour in ${tzShort}${onSelect ? '; select a cell to filter the page' : ''}`} className="space-y-[3px]">
        {DOWS.map((dow, di) => (
          <div key={dow} className="flex items-center gap-1.5">
            <span className="w-8 shrink-0 text-xs text-text2" aria-hidden>{dowName(dow, true)}</span>
            <div className="grid flex-1 gap-[2px]" style={{ gridTemplateColumns: 'repeat(24, minmax(0, 1fr))' }}>
              {HOURS.map((h) => {
                const index = di * 24 + h
                const c = byKey.get(`${dow}-${h}`)
                const active = activeHour === h && activeDow === dow
                const label = c ? heatCellLabel(c, data.basis, tzShort) : `${dowName(dow)} ${hourRangeLabel(h)}`
                const common = {
                  title: label,
                  style: styleFor(c),
                  className: cn(
                    'h-3.5 rounded-[2px] sm:h-5 xl:h-6',
                    c?.state === 'outside_window' && 'border border-dashed border-border/70',
                    c?.state === 'zero' && 'border border-border',
                    active && 'ring-2 ring-primary ring-offset-1 ring-offset-surface',
                  ),
                }
                if (!onSelect) return <div key={h} {...common} ref={(el) => { refs.current[index] = el }} />
                return (
                  <button
                    key={h}
                    type="button"
                    {...common}
                    ref={(el) => { refs.current[index] = el }}
                    tabIndex={index === focus ? 0 : -1}
                    aria-label={`${label}${active ? '. Selected: press to clear the filter' : '. Press to filter the page to this hour'}`}
                    aria-pressed={active}
                    onFocus={() => setFocus(index)}
                    onKeyDown={(e) => move(e, index)}
                    onClick={() => onSelect(dow, h)}
                    className={cn(common.className, 'cursor-pointer hover:ring-2 hover:ring-primary/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary')}
                  />
                )
              })}
            </div>
          </div>
        ))}
        <div className="flex items-center gap-1.5" aria-hidden>
          <span className="w-8 shrink-0" />
          <div className="relative h-3 flex-1 text-2xs text-text2">
            {HOUR_TICKS.map((h) => (
              <span key={h} className="absolute -translate-x-1/2 tabular-nums" style={{ left: `${((h + 0.5) / 24) * 100}%` }}>
                {String(h).padStart(2, '0')}
              </span>
            ))}
          </div>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-2xs text-text2">
        {legend.length > 0 && (
          <div
            role="img"
            aria-label={`Colour scale: linear, ${legend.length} steps from above 0 to ${formatBytes(max)}${data.basis === 'avg' ? ' per hour' : ''}`}
            className="w-full max-w-[16rem]"
          >
            <div className="flex h-2.5 overflow-hidden rounded-sm">
              {legend.map((b, i) => (
                <span key={i} className="flex-1" style={{ background: ramp[i] }} title={`${formatBytes(b.lo)} – ${formatBytes(b.hi)}${unitSuffix(data.basis)}`} />
              ))}
            </div>
            <div className="mt-0.5 flex justify-between gap-2 tabular-nums">
              <span>&gt;0</span>
              <span>{formatBytes(max / 2)}</span>
              <span>
                {formatBytes(max)}
                {data.basis === 'avg' ? ' per hour' : ''}
              </span>
            </div>
          </div>
        )}
        <Swatch style={{ background: 'rgb(var(--surface2))' }} className="border border-border" label="0 (records, no match)" />
        <Swatch style={{ background: `repeating-linear-gradient(45deg, rgba(${hatch},0.55) 0 2px, transparent 2px 5px)` }} label="No records received" />
        <Swatch style={{ background: `repeating-linear-gradient(135deg, rgba(${hatch},0.25) 0 1px, transparent 1px 4px)` }} className="border border-border" label="Not tracked" />
        <Swatch style={{ background: `linear-gradient(225deg, rgb(var(--text2)) 0 4px, transparent 4px), ${ramp[2]}` }} label="Collection gaps in some of these hours" />
      </div>
      <p className="text-2xs leading-snug text-text2">
        Linear scale. {data.basis === 'avg' ? 'Average hour: bytes divided by the hours of that weekday and hour that had records.' : 'Total bytes over the window.'}{' '}
        {data.coverage.coverage_pct != null && `${data.coverage.hours_covered} of ${data.coverage.hours_in_window} hours had records.`}
      </p>
    </div>
  )
}

function Swatch({ style, className, label }: { style: React.CSSProperties; className?: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={cn('h-2.5 w-3.5 rounded-sm', className)} style={style} />
      {label}
    </span>
  )
}

/** "Sep 23, 22:38 → Sep 30, 22:38 (Asia/Riyadh)". */
export function heatmapPeriod(data: HeatmapPayload): string {
  const from = formatDateTime(data.effective_from || data.from, data.tz, { zone: 'none', seconds: false })
  const to = formatDateTime(data.effective_to || data.to, data.tz, { zone: 'none', seconds: false })
  return `${from} → ${to} (${data.tz})`
}
