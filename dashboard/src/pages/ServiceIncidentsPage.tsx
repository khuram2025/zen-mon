import { useMemo } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Clock,
  Filter,
  HelpCircle,
  XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'
import type { ServiceCheck } from '@/types'

/* Match the palette used by ServiceCheckDetail so the page feels consistent. */
const C = {
  bg: '#0B111F',
  panel: '#0F172A',
  border: '#1f2a44',
  borderSoft: '#172135',
  text: '#E5E7EB',
  textDim: '#94A3B8',
  textMuted: '#64748B',
  up: '#22c55e',
  down: '#ef4444',
  warn: '#f59e0b',
  unknown: '#475569',
  primary: '#38bdf8',
}

const statusMeta: Record<string, { label: string; color: string; Icon: any }> = {
  up: { label: 'Up', color: C.up, Icon: CheckCircle2 },
  down: { label: 'Down', color: C.down, Icon: XCircle },
  degraded: { label: 'Degraded', color: C.warn, Icon: AlertTriangle },
  warning: { label: 'Warning', color: C.warn, Icon: AlertTriangle },
  unknown: { label: 'Unknown', color: C.unknown, Icon: HelpCircle },
}

type StatusEvent = {
  timestamp: string
  old_status: string | null
  new_status: string
  reason?: string | null
  duration_sec?: number | null
}

const FILTERS = [
  { key: 'all', label: 'All Events' },
  { key: 'incidents', label: 'Incidents Only' },
  { key: 'down', label: 'Down' },
  { key: 'warning', label: 'Warning / Degraded' },
] as const

type FilterKey = (typeof FILTERS)[number]['key']

function formatDur(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return '—'
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${Math.floor(sec % 60)}s`
  return `${Math.floor(sec)}s`
}

export function ServiceIncidentsPage() {
  const { id = '' } = useParams<{ id: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const filter: FilterKey = (FILTERS.find((f) => f.key === searchParams.get('filter'))?.key) || 'incidents'
  const setFilter = (k: FilterKey) => {
    const next = new URLSearchParams(searchParams)
    next.set('filter', k)
    setSearchParams(next, { replace: true })
  }

  const { data: check } = useQuery<ServiceCheck>({
    queryKey: ['service-check', id],
    queryFn: async () => (await api.get(`/service-checks/${id}`)).data,
    enabled: !!id,
  })

  const { data: history = [], isLoading } = useQuery<StatusEvent[]>({
    queryKey: ['service-status-history-full', id],
    queryFn: async () => (await api.get(`/service-checks/${id}/status-history?limit=500`)).data,
    enabled: !!id,
    refetchInterval: 30_000,
  })

  const filtered = useMemo(() => {
    if (filter === 'all') return history
    if (filter === 'down') return history.filter((h) => h.new_status === 'down')
    if (filter === 'warning')
      return history.filter((h) => h.new_status === 'warning' || h.new_status === 'degraded')
    return history.filter((h) => h.new_status !== 'up')
  }, [history, filter])

  const stats = useMemo(() => {
    const incidents = history.filter((h) => h.new_status !== 'up')
    const totalDown = incidents.reduce((sum, h) => sum + (h.duration_sec || 0), 0)
    const longest = incidents.reduce((mx, h) => Math.max(mx, h.duration_sec || 0), 0)
    const downCount = history.filter((h) => h.new_status === 'down').length
    const warnCount = history.filter((h) => h.new_status === 'warning' || h.new_status === 'degraded').length
    return {
      total: incidents.length,
      down: downCount,
      warn: warnCount,
      totalDown,
      longest,
    }
  }, [history])

  return (
    <div className="space-y-4 p-0" style={{ background: C.bg, color: C.text }}>
      <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <div className="flex items-start gap-2">
          <Link
            to={`/services/${id}`}
            className="mt-1 rounded-md p-1.5 hover:bg-white/5"
            style={{ color: C.textMuted }}
            aria-label="Back to service"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Incident History</h1>
            <p className="text-[11px]" style={{ color: C.textMuted }}>
              {check?.name ? `${check.name} · ` : ''}Full timeline of every status transition recorded for this service.
            </p>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Total Incidents" value={stats.total} tint={stats.total > 0 ? C.warn : C.up} />
        <Stat label="Down Events" value={stats.down} tint={stats.down > 0 ? C.down : C.up} />
        <Stat label="Warn / Degraded" value={stats.warn} tint={stats.warn > 0 ? C.warn : C.up} />
        <Stat label="Total Downtime" value={formatDur(stats.totalDown)} tint={stats.totalDown > 0 ? C.down : C.up} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Filter className="h-3.5 w-3.5" style={{ color: C.textMuted }} />
        <span className="text-[11px]" style={{ color: C.textMuted }}>Filter</span>
        <div className="flex gap-0.5 rounded-md p-0.5" style={{ background: C.borderSoft }}>
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded px-2.5 py-1 text-[11px] font-medium transition-colors ${
                filter === f.key ? '' : 'hover:bg-white/5'
              }`}
              style={
                filter === f.key
                  ? { background: C.panel, color: C.text, boxShadow: '0 1px 0 rgba(255,255,255,0.04)' }
                  : { color: C.textDim }
              }
            >
              {f.label}
            </button>
          ))}
        </div>
        <span className="ml-2 text-[11px]" style={{ color: C.textMuted }}>
          Showing {filtered.length} of {history.length} events
        </span>
      </div>

      <div className="rounded-xl p-3" style={{ background: C.panel, border: `1px solid ${C.border}` }}>
        {isLoading ? (
          <div className="py-12 text-center text-[12px]" style={{ color: C.textMuted }}>
            Loading incident history…
          </div>
        ) : filtered.length === 0 ? (
          <div className="py-12 text-center text-[12px]" style={{ color: C.textMuted }}>
            No events match this filter.
          </div>
        ) : (
          <div className="overflow-hidden rounded-md border" style={{ borderColor: C.border }}>
            <table className="w-full text-[11px]" style={{ color: C.text }}>
              <thead>
                <tr style={{ background: C.borderSoft, color: C.textMuted }}>
                  <th className="px-3 py-2 text-left font-medium">When</th>
                  <th className="px-3 py-2 text-left font-medium">Transition</th>
                  <th className="px-3 py-2 text-left font-medium">Status</th>
                  <th className="px-3 py-2 text-left font-medium">Duration</th>
                  <th className="px-3 py-2 text-left font-medium">Reason</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((h, i) => {
                  const sm = statusMeta[h.new_status] || statusMeta.unknown
                  const oldMeta = h.old_status ? statusMeta[h.old_status] || statusMeta.unknown : null
                  const Icon = sm.Icon
                  const isLongest = (h.duration_sec || 0) === stats.longest && (h.duration_sec || 0) > 0
                  return (
                    <tr
                      key={`${h.timestamp}-${i}`}
                      style={{ borderTop: `1px solid ${C.border}` }}
                      className="hover:bg-white/[0.03]"
                    >
                      <td className="px-3 py-2 align-top font-mono" style={{ color: C.textDim }}>
                        <div>{new Date(h.timestamp).toLocaleString()}</div>
                        <div className="text-[10px]" style={{ color: C.textMuted }}>
                          {relativeTime(h.timestamp) || ''}
                        </div>
                      </td>
                      <td className="px-3 py-2 align-top" style={{ color: C.textDim }}>
                        {oldMeta ? (
                          <span className="font-mono">
                            <span style={{ color: oldMeta.color }}>{oldMeta.label}</span>
                            <span className="mx-1" style={{ color: C.textMuted }}>→</span>
                            <span style={{ color: sm.color }}>{sm.label}</span>
                          </span>
                        ) : (
                          <span className="font-mono" style={{ color: sm.color }}>{sm.label}</span>
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <span
                          className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                          style={{ background: `${sm.color}20`, color: sm.color }}
                        >
                          <Icon className="h-3 w-3" />
                          {sm.label}
                        </span>
                      </td>
                      <td className="px-3 py-2 align-top font-mono" style={{ color: C.textDim }}>
                        <span className="inline-flex items-center gap-1">
                          <Clock className="h-3 w-3" style={{ color: C.textMuted }} />
                          {formatDur(h.duration_sec)}
                          {isLongest && (
                            <span
                              className="ml-1 rounded px-1 py-0.5 text-[9px] font-semibold uppercase"
                              style={{ background: `${C.down}20`, color: C.down }}
                            >
                              longest
                            </span>
                          )}
                        </span>
                      </td>
                      <td className="px-3 py-2 align-top" style={{ color: C.textDim }}>
                        {h.reason ? (
                          <span title={h.reason}>{h.reason}</span>
                        ) : (
                          <span style={{ color: C.textMuted }}>—</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, tint }: { label: string; value: number | string; tint: string }) {
  return (
    <div
      className="rounded-xl p-3"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="text-[10px] uppercase tracking-wider" style={{ color: C.textMuted }}>
        {label}
      </div>
      <div className="mt-1 inline-flex items-center gap-1.5">
        <AlertCircle className="h-4 w-4" style={{ color: tint }} />
        <span className="text-2xl font-semibold tabular-nums" style={{ color: tint }}>
          {value}
        </span>
      </div>
    </div>
  )
}
