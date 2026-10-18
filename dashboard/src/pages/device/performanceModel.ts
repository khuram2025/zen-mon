/* Previous-period comparison and PNG export layout for the Performance tab. Kept free of React and the
 * DOM so scripts/test-device-page-phase3.mjs can check it. */

export type CmpPoint = { ts: number; value: number | null }

/** The window just before [fromTs, toTs], of the same length. */
export function previousWindow(fromTs: number, toTs: number): { fromTs: number; toTs: number; shiftMs: number } {
  const span = Math.max(0, toTs - fromTs)
  return { fromTs: fromTs - span, toTs: fromTs, shiftMs: span }
}

/** Previous-period samples moved forward onto the current window, so both share one time axis. */
export function shiftPoints(points: CmpPoint[], shiftMs: number): CmpPoint[] {
  return points.map((p) => ({ ts: p.ts + shiftMs, value: p.value }))
}

export type PeriodDelta = {
  current: number | null
  previous: number | null
  /** current − previous, in the metric's own unit; null when either side has no samples. */
  delta: number | null
  text: string
  direction: 'up' | 'down' | 'flat' | 'unknown'
}

const mean = (values: Array<number | null>): number | null => {
  const xs = values.filter((v): v is number => v != null && Number.isFinite(v))
  return xs.length ? xs.reduce((s, v) => s + v, 0) / xs.length : null
}

/** The average against the previous period's average as an absolute change in the metric's unit:
 *  "+3 pt" for percentages, "+22 ms" for latency. Missing on either side is unknown, never zero. */
export function periodDelta(current: Array<number | null>, previous: Array<number | null>,
  formatAbs: (v: number) => string): PeriodDelta {
  const cur = mean(current)
  const prev = mean(previous)
  if (cur == null || prev == null) return { current: cur, previous: prev, delta: null, text: 'no samples to compare', direction: 'unknown' }
  const delta = cur - prev
  const shown = formatAbs(Math.abs(delta))
  // A change too small to show at the metric's precision is no change.
  if (shown === formatAbs(0)) return { current: cur, previous: prev, delta, text: `±${shown}`, direction: 'flat' }
  return { current: cur, previous: prev, delta, text: `${delta > 0 ? '+' : '−'}${shown}`, direction: delta > 0 ? 'up' : 'down' }
}

/* ── PNG export layout ─────────────────────────────────────────────────── */

export type PngCell = { x: number; y: number; w: number; h: number }
export type PngLayout = { width: number; height: number; header: number; cells: PngCell[] }

/** Charts in reading order on a grid of `columns`, each cell `cellWidth` wide with its chart scaled to fit
 *  plus room for the title and the statistics line. A full-width chart (the pinned one) spans the row. */
export function pngLayout(charts: Array<{ width: number; height: number; full?: boolean }>, opts: {
  columns?: number; cellWidth?: number; gap?: number; header?: number; titleHeight?: number; footerHeight?: number
} = {}): PngLayout {
  const columns = Math.max(1, opts.columns ?? 2)
  const cellWidth = opts.cellWidth ?? 720
  const gap = opts.gap ?? 24
  const header = opts.header ?? 72
  const titleHeight = opts.titleHeight ?? 28
  const footerHeight = opts.footerHeight ?? 24
  const width = gap + columns * (cellWidth + gap)
  const cells: PngCell[] = []
  let col = 0
  let y = header
  let rowHeight = 0
  for (const c of charts) {
    const span = c.full ? columns : 1
    if (col + span > columns) { y += rowHeight + gap; col = 0; rowHeight = 0 }
    const w = span * cellWidth + (span - 1) * gap
    const chartH = c.width > 0 ? Math.round((c.height / c.width) * w) : c.height
    const h = titleHeight + chartH + footerHeight
    cells.push({ x: gap + col * (cellWidth + gap), y, w, h })
    rowHeight = Math.max(rowHeight, h)
    col += span
    if (col >= columns) { y += rowHeight + gap; col = 0; rowHeight = 0 }
  }
  const height = (col === 0 ? y : y + rowHeight + gap)
  return { width, height: Math.max(height, header + gap), header, cells }
}

/** "fw-01-performance-202610181300.png": host, the window start (UTC, minutes) and the extension. */
export function exportFilename(hostname: string, fromTs: number, ext: 'png' | 'csv'): string {
  const host = hostname.replace(/[^A-Za-z0-9._-]+/g, '_') || 'device'
  return `${host}-performance-${new Date(fromTs).toISOString().slice(0, 16).replace(/[-:T]/g, '')}.${ext}`
}
