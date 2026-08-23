/**
 * Shared building blocks for the per-team dashboards
 * (/dashboards/network, /dashboards/servers, /dashboards/apps).
 *
 * Visual language matches the home NOC dashboard: KPI cards with a gradient
 * accent bar, bordered section headers, proportional status bars, and
 * theme-aware charts driven by rgb(var(--token)) CSS variables.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Clock } from 'lucide-react'
import { Card } from '@/components/ui/Card'

/* ── Categorical palette ────────────────────────────────────────────────────
 * Fixed assignment order, never cycled. Validated (CVD ΔE ≥ 8 adjacent pairs,
 * ≥3:1 contrast, lightness band) on both the light and dark chart surfaces.
 * Status colors (success/warning/danger/info tokens) are reserved for state
 * and are NOT part of this list. */
export const CATEGORICAL = ['#0284c7', '#d97706', '#047857', '#db2777', '#7c3aed'] as const

/** Single-series accent hues (readable on both surfaces). */
export const SERIES = {
  traffic: '#0284c7',
  latency: '#7c3aed',
  errors: '#db2777',
  requests: '#0284c7',
} as const

export const chartTooltipStyle = {
  backgroundColor: 'rgb(var(--surface))',
  border: '1px solid rgb(var(--border))',
  borderRadius: 8,
  fontSize: 12,
  color: 'rgb(var(--text))',
} as const

/* ── Time-range pills ───────────────────────────────────────────────────── */

export type RangeKey = '1h' | '6h' | '24h' | '7d'
export type Range = { key: RangeKey; label: string; minutes: number; hours: number; days: number }
export const RANGES: Range[] = [
  { key: '1h', label: '1H', minutes: 60, hours: 1, days: 1 },
  { key: '6h', label: '6H', minutes: 360, hours: 6, days: 1 },
  { key: '24h', label: '24H', minutes: 1440, hours: 24, days: 1 },
  { key: '7d', label: '7D', minutes: 10080, hours: 168, days: 7 },
]

export function RangePills({ value, onChange, keys }: {
  value: RangeKey
  onChange: (k: RangeKey) => void
  keys?: RangeKey[]
}) {
  const ranges = keys ? RANGES.filter((r) => keys.includes(r.key)) : RANGES
  return (
    <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
      <Clock className="ml-1 h-3.5 w-3.5 text-muted" />
      {ranges.map((r) => (
        <button
          key={r.key}
          onClick={() => onChange(r.key)}
          className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
            r.key === value ? 'bg-primary text-white shadow-sm' : 'text-muted hover:bg-surface3 hover:text-text'
          }`}
        >
          {r.label}
        </button>
      ))}
    </div>
  )
}

/* ── Page header ────────────────────────────────────────────────────────── */

export function LiveClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return (
    <span className="hidden rounded-md border border-border bg-surface2/60 px-2.5 py-1 font-mono text-xs font-semibold tabular-nums text-text2 md:inline-block">
      {now.toLocaleTimeString([], { hour12: false })}
    </span>
  )
}

export function TeamHeader({ title, subtitle, right }: {
  title: string
  subtitle?: React.ReactNode
  right?: React.ReactNode
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <div className="flex items-center gap-2.5">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          <span className="flex items-center gap-1.5 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[10px] font-bold tracking-widest text-success">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
            </span>
            LIVE
          </span>
        </div>
        {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-2">{right}</div>
    </div>
  )
}

/* ── KPI card ───────────────────────────────────────────────────────────── */

export const KPI_ACCENT: Record<string, string> = {
  success: 'from-emerald-500/80 to-green-500/80',
  danger: 'from-rose-500/80 to-red-500/80',
  warning: 'from-amber-500/80 to-orange-500/80',
  info: 'from-cyan-500/80 to-sky-500/80',
  accent: 'from-fuchsia-500/80 to-purple-500/80',
  primary: 'from-blue-500/80 to-indigo-500/80',
}

export function KpiCard({ to, label, icon, accent, value, sub, foot }: {
  to: string
  label: string
  icon: React.ReactNode
  accent: keyof typeof KPI_ACCENT
  value: React.ReactNode
  sub?: React.ReactNode
  foot?: React.ReactNode
}) {
  return (
    <Link to={to} className="group">
      <Card className="relative h-full overflow-hidden transition group-hover:border-primary/40">
        <span className={`absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r ${KPI_ACCENT[accent]}`} />
        <div className="flex items-start justify-between gap-2 px-4 pt-3">
          <div className="min-w-0">
            <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted">{label}</div>
            <div className="mt-1 text-2xl font-bold tabular-nums leading-none text-text">{value}</div>
            <div className="mt-1 min-h-[14px]">{sub}</div>
          </div>
          <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br text-white shadow ${KPI_ACCENT[accent]}`}>
            {icon}
          </span>
        </div>
        <div className="px-4 pb-3 pt-1.5">{foot}</div>
      </Card>
    </Link>
  )
}

/* ── Section chrome ─────────────────────────────────────────────────────── */

export function SectionHeader({ icon, title, hint, right }: {
  icon?: React.ReactNode
  title: string
  hint?: string
  right?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
      <div className="flex min-w-0 items-center gap-2">
        {icon && <span className="text-muted">{icon}</span>}
        <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
        {hint && <span className="truncate text-[10px] uppercase tracking-wider text-muted">{hint}</span>}
      </div>
      {right}
    </div>
  )
}

export function Empty({ text }: { text: string }) {
  return <div className="py-4 text-center text-xs text-muted">{text}</div>
}

/* ── Small meters ───────────────────────────────────────────────────────── */

/** Single horizontal percent meter with threshold coloring (status use only). */
export function PctBar({ value, warnAt = 60, dangerAt = 80 }: { value: number; warnAt?: number; dangerAt?: number }) {
  const v = Math.max(0, Math.min(100, value))
  const tone = v >= dangerAt ? 'bg-danger' : v >= warnAt ? 'bg-warning' : 'bg-success'
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface2">
      <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.max(2, v)}%` }} />
    </div>
  )
}

/** Proportional share bar with 2px surface gaps between segments. */
export function ShareBar({ parts, height = 12 }: {
  parts: Array<{ label: string; value: number; color: string }>
  height?: number
}) {
  const total = parts.reduce((s, p) => s + p.value, 0) || 1
  return (
    <div className="flex w-full overflow-hidden rounded-full bg-surface2" style={{ height, gap: 2 }}>
      {parts.filter((p) => p.value > 0).map((p) => (
        <div
          key={p.label}
          title={`${p.label} ${((p.value / total) * 100).toFixed(1)}%`}
          style={{ width: `${Math.max(1.5, (p.value / total) * 100)}%`, background: p.color }}
          className="rounded-sm"
        />
      ))}
    </div>
  )
}

/* ── Formatters ─────────────────────────────────────────────────────────── */

export function fmtBps(bps: number): string {
  if (!isFinite(bps) || bps <= 0) return '0 bps'
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  let i = 0
  let v = bps
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`
}

export function fmtBytes(b: number): string {
  if (!isFinite(b) || b <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0
  let v = b
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

export function fmtCount(n: number): string {
  if (!isFinite(n) || n <= 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(0)}K`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return `${Math.round(n)}`
}
