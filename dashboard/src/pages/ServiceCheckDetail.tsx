import { useState, useMemo } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import {
  ArrowLeft, Globe, Plug, ShieldCheck, Trash2, Edit3, Clock, Activity,
  Gauge, RefreshCw, CheckCircle, XCircle, AlertTriangle, Copy, ExternalLink,
  ArrowUpCircle, ArrowDownCircle, History, Shield, Zap, WifiOff,
} from 'lucide-react'
import { useServiceCheck, useServiceCheckMetrics } from '@/hooks/useServiceChecks'
import { api } from '@/lib/api'
import { formatRTT, timeAgo, cn } from '@/lib/utils'
import dayjs from 'dayjs'
import type { ServiceMetricPoint } from '@/types'

const typeIcons = { http: Globe, tcp: Plug, tls: ShieldCheck }
const typeBgColors = { http: '#6366F1', tcp: '#22C55E', tls: '#F59E0B' }
const statusColors: Record<string, string> = { up: '#22C55E', down: '#EF4444', warning: '#F97316', degraded: '#EAB308', unknown: '#6B7280' }
const statusLabels: Record<string, string> = { up: 'Healthy', down: 'Down', warning: 'Warning', degraded: 'Degraded', unknown: 'Pending' }

const timeRanges = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
]

// ─── Response Stats ───
function ResponseStats({ points }: { points: ServiceMetricPoint[] }) {
  const upPoints = points.filter(p => p.is_up && p.response_ms !== null && p.response_ms! > 0)
  if (upPoints.length === 0) return null

  const times = upPoints.map(p => p.response_ms!)
  const avg = times.reduce((a, b) => a + b, 0) / times.length
  const min = Math.min(...times)
  const max = Math.max(...times)
  const sorted = [...times].sort((a, b) => a - b)
  const p95 = sorted[Math.floor(sorted.length * 0.95)] || max
  const totalChecks = points.length
  const upChecks = points.filter(p => p.is_up).length
  const availability = totalChecks > 0 ? (upChecks / totalChecks) * 100 : 0

  const stats = [
    { label: 'Avg Response', value: formatRTT(avg), color: '#6366F1' },
    { label: 'Min', value: formatRTT(min), color: '#22C55E' },
    { label: 'Max', value: formatRTT(max), color: '#F97316' },
    { label: 'P95', value: formatRTT(p95), color: '#8B5CF6' },
    { label: 'Availability', value: `${availability.toFixed(2)}%`, color: availability > 99 ? '#22C55E' : availability > 95 ? '#EAB308' : '#EF4444' },
    { label: 'Total Checks', value: String(totalChecks), color: '#9BA1B0' },
  ]

  return (
    <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
      {stats.map(s => (
        <div key={s.label} className="text-center p-3 bg-[var(--bg-secondary)] rounded-lg border border-[var(--bg-elevated)]">
          <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-1">{s.label}</div>
          <div className="text-sm font-mono font-semibold" style={{ color: s.color }}>{s.value}</div>
        </div>
      ))}
    </div>
  )
}

// ─── Uptime Bar ───
function UptimeBar({ points }: { points: ServiceMetricPoint[] }) {
  if (points.length === 0) return null
  const total = points.length
  const upCount = points.filter(p => p.is_up === true).length
  const pct = ((upCount / total) * 100).toFixed(2)

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs text-[var(--text-muted)]">Uptime Timeline ({total} checks)</span>
        <span className="text-xs font-mono font-medium" style={{ color: parseFloat(pct) > 99 ? '#22C55E' : parseFloat(pct) > 95 ? '#EAB308' : '#EF4444' }}>
          {pct}% uptime
        </span>
      </div>
      <div className="flex gap-[1px] h-7 rounded-lg overflow-hidden bg-[var(--bg-tertiary)]">
        {points.map((p, i) => (
          <div key={i} className="flex-1 min-w-[2px] hover:opacity-80 transition-opacity"
            style={{ backgroundColor: p.is_up ? '#22C55E' : '#EF4444' }}
            title={`${dayjs(p.timestamp).format('HH:mm:ss')} — ${p.is_up ? 'UP' : 'DOWN'}${p.response_ms ? ` (${p.response_ms.toFixed(1)}ms)` : ''}`}
          />
        ))}
      </div>
      <div className="flex justify-between mt-1">
        <span className="text-[10px] text-[var(--text-muted)]">{points.length > 0 ? dayjs(points[0].timestamp).format('HH:mm') : ''}</span>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1 text-[10px] text-[var(--text-muted)]"><span className="w-2 h-2 rounded-sm bg-[#22C55E]" />Up</span>
          <span className="flex items-center gap-1 text-[10px] text-[var(--text-muted)]"><span className="w-2 h-2 rounded-sm bg-[#EF4444]" />Down</span>
        </div>
        <span className="text-[10px] text-[var(--text-muted)]">{points.length > 0 ? dayjs(points[points.length - 1].timestamp).format('HH:mm') : ''}</span>
      </div>
    </div>
  )
}

// ─── Incident Table ───
interface StatusEvent { service_check_id: string; timestamp: string; old_status: string; new_status: string; reason: string; duration_sec: number }
function IncidentTable({ events }: { events: StatusEvent[] }) {
  if (events.length === 0) return <div className="text-center py-8 text-[var(--text-muted)] text-sm">No status changes recorded</div>
  const fmtDur = (s: number) => { if (!s || s <= 0) return '-'; if (s < 60) return `${s}s`; if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`; return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` }
  return (
    <div className="overflow-hidden rounded-lg border border-[var(--bg-elevated)]">
      <table className="w-full text-xs">
        <thead><tr className="bg-[var(--bg-tertiary)]">
          <th className="text-left px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Time</th>
          <th className="text-left px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Change</th>
          <th className="text-left px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Reason</th>
          <th className="text-right px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Duration</th>
        </tr></thead>
        <tbody>{events.map((e, i) => (
          <tr key={i} className="border-t border-[var(--bg-elevated)]/50 hover:bg-[var(--bg-tertiary)]/50">
            <td className="px-3 py-2.5 font-mono text-[var(--text-secondary)]">{dayjs(e.timestamp).format('MMM D, HH:mm:ss')}</td>
            <td className="px-3 py-2.5"><div className="flex items-center gap-1.5">
              {e.new_status === 'down' ? <ArrowDownCircle className="w-3.5 h-3.5 text-red-400" /> : <ArrowUpCircle className="w-3.5 h-3.5 text-green-400" />}
              <span className="uppercase font-medium" style={{ color: statusColors[e.old_status] || '#6B7280' }}>{e.old_status}</span>
              <span className="text-[var(--text-muted)]">→</span>
              <span className="uppercase font-medium" style={{ color: statusColors[e.new_status] || '#6B7280' }}>{e.new_status}</span>
            </div></td>
            <td className="px-3 py-2.5 text-[var(--text-muted)]">{e.reason}</td>
            <td className="px-3 py-2.5 text-right font-mono text-[var(--text-secondary)]">{fmtDur(e.duration_sec)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

// ─── Main Page ───
export function ServiceCheckDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data: check, isLoading } = useServiceCheck(id || '')
  const [rangeIdx, setRangeIdx] = useState(2) // 24h
  const [showDelete, setShowDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [copied, setCopied] = useState(false)

  const rangeHours = timeRanges[rangeIdx].hours
  const now = useMemo(() => dayjs(), [rangeIdx]) // eslint-disable-line
  const metricsParams = useMemo(() => ({
    from: now.subtract(rangeHours, 'hour').toISOString(),
    to: now.toISOString(),
    granularity: rangeHours <= 6 ? 'raw' : 'auto',
  }), [rangeHours, now])

  const { data: metrics } = useServiceCheckMetrics(id || '', metricsParams)
  const points = metrics?.points || []

  const { data: statusHistory = [] } = useQuery({
    queryKey: ['service-status-history', id],
    queryFn: () => api.get<StatusEvent[]>(`/service-checks/${id}/status-history`),
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })

  const handleDelete = async () => {
    setDeleting(true)
    try {
      await api.delete(`/service-checks/${id}`)
      queryClient.invalidateQueries({ queryKey: ['service-checks'] })
      navigate('/service-checks')
    } catch { setDeleting(false) }
  }

  const copyTarget = () => {
    navigator.clipboard.writeText(check?.target_url || `${check?.target_host}:${check?.target_port}`)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // ─── Chart ───
  const chartOption = useMemo(() => {
    if (points.length === 0) return null
    const timestamps = points.map(p => p.timestamp)
    const responseTimes = points.map(p => p.is_up ? p.response_ms : null)
    const statusCodes = points.map(p => p.status_code)

    // Down zones for red background
    const downPieces: { gt: number; lt: number; color: string }[] = []
    let inDown = false, downStart = 0
    points.forEach((p, i) => {
      if (!p.is_up && !inDown) { inDown = true; downStart = i }
      if (p.is_up && inDown) { inDown = false; downPieces.push({ gt: downStart - 0.5, lt: i - 0.5, color: 'rgba(239,68,68,0.08)' }) }
    })
    if (inDown) downPieces.push({ gt: downStart - 0.5, lt: points.length - 0.5, color: 'rgba(239,68,68,0.08)' })

    return {
      backgroundColor: 'transparent',
      grid: { top: 40, right: 20, bottom: 30, left: 60 },
      tooltip: {
        trigger: 'axis', backgroundColor: '#1A1D27', borderColor: '#2D3140',
        textStyle: { color: '#E8EAED', fontSize: 12 },
        formatter: (params: any[]) => {
          const ts = params[0]?.axisValue
          const idx = params[0]?.dataIndex
          const d = dayjs(ts)
          let html = `<div style="font-size:11px;color:#9BA1B0;margin-bottom:6px">${d.format('MMM D, HH:mm:ss')}</div>`
          const p = points[idx]
          if (p) {
            if (p.is_up) {
              html += `<div style="display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:#22C55E;display:inline-block"></span> <b style="color:#22C55E">UP</b></div>`
              if (p.response_ms !== null) html += `<div style="margin-top:4px">Response: <b>${p.response_ms.toFixed(2)} ms</b></div>`
              if (p.status_code) html += `<div>HTTP Status: <b>${p.status_code}</b></div>`
            } else {
              html += `<div style="display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:#EF4444;display:inline-block"></span> <b style="color:#EF4444">DOWN</b></div>`
              if (p.error_message) html += `<div style="margin-top:4px;color:#EF4444;font-size:11px">${p.error_message}</div>`
            }
          }
          return html
        },
      },
      xAxis: {
        type: 'category', data: timestamps, boundaryGap: false,
        axisLabel: { color: '#5F6578', fontSize: 11, formatter: (v: string) => dayjs(v).format('HH:mm') },
        axisLine: { lineStyle: { color: '#2D3140' } }, splitLine: { show: false },
      },
      yAxis: {
        type: 'value', name: 'Response (ms)', nameTextStyle: { color: '#5F6578', fontSize: 11 },
        axisLabel: { color: '#5F6578', fontSize: 11 },
        splitLine: { lineStyle: { color: '#1A1D2780' } }, min: 0,
      },
      visualMap: downPieces.length > 0 ? { show: false, dimension: 0, pieces: downPieces, seriesIndex: 0 } : undefined,
      series: [{
        type: 'line', data: responseTimes, smooth: true, symbol: 'none', connectNulls: false,
        lineStyle: { color: '#6366F1', width: 2 },
        areaStyle: {
          color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: 'rgba(99,102,241,0.25)' }, { offset: 1, color: 'rgba(99,102,241,0.0)' }] },
        },
        itemStyle: { color: '#6366F1' }, z: 2,
      }],
    }
  }, [points])

  if (isLoading) return (
    <div className="flex items-center justify-center py-32">
      <div className="flex flex-col items-center gap-3">
        <div className="w-8 h-8 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin" />
        <span className="text-sm text-[var(--text-muted)]">Loading service check...</span>
      </div>
    </div>
  )

  if (!check) return (
    <div className="flex flex-col items-center justify-center py-32 gap-4">
      <div className="w-16 h-16 rounded-2xl bg-red-500/10 flex items-center justify-center"><WifiOff className="w-8 h-8 text-red-400" /></div>
      <h3 className="text-lg font-semibold text-[var(--text-primary)]">Service Check Not Found</h3>
      <Link to="/service-checks" className="flex items-center gap-2 px-4 py-2 bg-[var(--accent)] text-white rounded-lg text-sm font-medium"><ArrowLeft className="w-4 h-4" /> Back</Link>
    </div>
  )

  const TypeIcon = typeIcons[check.check_type as keyof typeof typeIcons] || ShieldCheck
  const typeBg = typeBgColors[check.check_type as keyof typeof typeBgColors] || '#6366F1'
  const stColor = statusColors[check.status] || '#6B7280'
  const target = check.target_url || `${check.target_host}${check.target_port ? ':' + check.target_port : ''}`

  return (
    <div>
      {/* ─── Header ─── */}
      <div className="flex items-start gap-4 mb-6">
        <button onClick={() => navigate('/service-checks')} className="p-2 mt-1 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors">
          <ArrowLeft className="w-5 h-5 text-[var(--text-secondary)]" />
        </button>
        <div className="flex-1">
          <div className="flex items-center gap-3 mb-1">
            <div className="w-10 h-10 rounded-xl flex items-center justify-center" style={{ backgroundColor: `${typeBg}15` }}>
              <TypeIcon className="w-5 h-5" style={{ color: typeBg }} />
            </div>
            <div>
              <div className="flex items-center gap-3">
                <h2 className="text-xl font-semibold text-[var(--text-primary)]">{check.name}</h2>
                <span className="px-3 py-1 rounded-full text-xs font-semibold" style={{ backgroundColor: `${stColor}15`, color: stColor }}>
                  {check.status === 'up' ? '● ' : check.status === 'down' ? '● ' : ''}{statusLabels[check.status]}
                </span>
              </div>
              <p className="text-sm text-[var(--text-muted)] font-mono flex items-center gap-2 mt-0.5">
                <span className="px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[10px] uppercase font-semibold text-[var(--text-secondary)]">{check.check_type}</span>
                <span>{target}</span>
                {check.device_hostname && (<><span className="text-[var(--bg-elevated)]">|</span><span className="text-[var(--text-secondary)] font-sans">{check.device_hostname}</span></>)}
              </p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => navigate(`/service-checks/${id}/edit`)} className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
            <Edit3 className="w-4 h-4" /> Edit
          </button>
        </div>
      </div>

      {/* ─── KPI Cards ─── */}
      <div className={`grid gap-3 mb-6 ${check.check_type === 'tls' ? 'grid-cols-2 lg:grid-cols-6' : 'grid-cols-2 lg:grid-cols-4'}`}>
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4">
          <div className="flex items-center gap-2 mb-2">
            {check.status === 'up' ? <CheckCircle className="w-4 h-4" style={{ color: stColor }} /> : <XCircle className="w-4 h-4" style={{ color: stColor }} />}
            <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)]">Current Status</span>
          </div>
          <p className="text-2xl font-semibold" style={{ color: stColor }}>{check.status === 'up' ? 'Online' : check.status === 'down' ? 'Offline' : statusLabels[check.status]}</p>
          <p className="text-[11px] text-[var(--text-muted)] mt-1">{check.status === 'up' ? 'Responding normally' : check.last_error || 'Service unreachable'}</p>
        </div>
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4">
          <div className="flex items-center gap-2 mb-2">
            <Gauge className="w-4 h-4 text-[var(--accent)]" />
            <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)]">Response Time</span>
          </div>
          <p className="text-2xl font-mono font-semibold text-[var(--text-primary)]">{check.status === 'up' ? formatRTT(check.last_response_ms) : '--'}</p>
          <p className="text-[11px] text-[var(--text-muted)] mt-1">{check.check_type === 'http' && check.status === 'up' ? `HTTP ${check.http_expected_status}` : check.check_type === 'tcp' ? `Port ${check.target_port}` : ''}</p>
        </div>
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4">
          <div className="flex items-center gap-2 mb-2">
            <RefreshCw className="w-4 h-4 text-[var(--text-secondary)]" />
            <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)]">Check Interval</span>
          </div>
          <p className="text-2xl font-mono font-semibold text-[var(--text-primary)]">{check.check_interval >= 60 ? `${check.check_interval / 60}m` : `${check.check_interval}s`}</p>
          <p className="text-[11px] text-[var(--text-muted)] mt-1">Timeout: {check.timeout}s</p>
        </div>
        <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4">
          <div className="flex items-center gap-2 mb-2">
            <Clock className="w-4 h-4" style={{ color: check.status === 'up' ? '#22C55E' : '#6B7280' }} />
            <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)]">Last Checked</span>
          </div>
          <p className="text-lg font-semibold text-[var(--text-primary)]">
            {check.last_check_at && (Date.now() - new Date(check.last_check_at).getTime()) < 120000 ? 'Active Now' : check.last_check_at ? timeAgo(check.last_check_at) : 'Never'}
          </p>
          <p className="text-[11px] text-[var(--text-muted)] mt-1">{check.last_check_at ? dayjs(check.last_check_at).format('MMM D, HH:mm:ss') : ''}</p>
        </div>
        {check.check_type === 'tls' && (
          <>
            <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4">
              <div className="flex items-center gap-2 mb-2">
                <Shield className="w-4 h-4 text-[#F59E0B]" />
                <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)]">Certificate Expiry</span>
              </div>
              <p className="text-lg font-semibold text-[var(--text-primary)]">{check.tls_expiry_date ? dayjs(check.tls_expiry_date).format('MMM D, YYYY') : '--'}</p>
              <p className="text-[11px] text-[var(--text-muted)] mt-1">{check.tls_issuer || ''}</p>
            </div>
            <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4">
              <div className="flex items-center gap-2 mb-2">
                <Zap className="w-4 h-4" style={{ color: check.tls_days_remaining == null ? '#6B7280' : check.tls_days_remaining > 30 ? '#22C55E' : check.tls_days_remaining > 7 ? '#F97316' : '#EF4444' }} />
                <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)]">Days Remaining</span>
              </div>
              <p className="text-2xl font-mono font-semibold" style={{ color: check.tls_days_remaining == null ? '#6B7280' : check.tls_days_remaining > 30 ? '#22C55E' : check.tls_days_remaining > 7 ? '#F97316' : '#EF4444' }}>
                {check.tls_days_remaining != null ? check.tls_days_remaining : '--'}
              </p>
              <p className="text-[11px] text-[var(--text-muted)] mt-1">{check.tls_days_remaining != null && check.tls_days_remaining <= check.tls_warn_days ? 'Expiring soon!' : 'Certificate valid'}</p>
            </div>
          </>
        )}
      </div>

      {/* ─── Response Stats ─── */}
      {points.length > 0 && <div className="mb-6"><ResponseStats points={points} /></div>}

      {/* ─── Main Content (2-column) ─── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column - Charts (2/3) */}
        <div className="lg:col-span-2 space-y-6">
          {/* Response Time Chart */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Response Time</h3>
              <div className="flex gap-1">
                {timeRanges.map((r, i) => (
                  <button key={r.label} onClick={() => setRangeIdx(i)}
                    className={cn('px-3 py-1 rounded text-xs font-medium transition-colors',
                      rangeIdx === i ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]')}>
                    {r.label}
                  </button>
                ))}
              </div>
            </div>
            {chartOption ? (
              <ReactECharts option={chartOption} style={{ height: 320 }} notMerge={true} />
            ) : (
              <div className="h-[320px] flex flex-col items-center justify-center text-[var(--text-muted)] gap-2">
                <Activity className="w-8 h-8 opacity-30" />
                <span className="text-sm">No data for selected time range</span>
              </div>
            )}
          </div>

          {/* Uptime Bar */}
          {points.length > 0 && (
            <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
              <UptimeBar points={points} />
            </div>
          )}

          {/* Incident History */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <div className="flex items-center gap-2 mb-4">
              <History className="w-4 h-4 text-[var(--accent)]" />
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Incident History</h3>
              <span className="text-[10px] text-[var(--text-muted)] bg-[var(--bg-tertiary)] px-2 py-0.5 rounded-full">{statusHistory.length} events</span>
            </div>
            <IncidentTable events={statusHistory} />
          </div>
        </div>

        {/* Right Column - Info (1/3) */}
        <div className="space-y-6">
          {/* Quick Actions */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-3">Quick Actions</h3>
            <div className="grid grid-cols-2 gap-2">
              <button onClick={copyTarget} className="flex items-center gap-2 p-3 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
                {copied ? <CheckCircle className="w-4 h-4 text-green-400" /> : <Copy className="w-4 h-4" />}
                {copied ? 'Copied!' : 'Copy Target'}
              </button>
              {check.check_type === 'http' && check.target_url && (
                <a href={check.target_url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-2 p-3 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
                  <ExternalLink className="w-4 h-4" /> Open URL
                </a>
              )}
              <button onClick={() => navigate(`/service-checks/${id}/edit`)} className="flex items-center gap-2 p-3 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
                <Edit3 className="w-4 h-4" /> Edit Check
              </button>
              <button onClick={() => setShowDelete(true)} className="flex items-center gap-2 p-3 rounded-lg bg-red-500/5 text-xs text-red-400/80 hover:text-red-400 hover:bg-red-500/10 transition-colors">
                <Trash2 className="w-4 h-4" /> Delete
              </button>
            </div>
          </div>

          {/* Check Configuration */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Configuration</h3>
            <div className="space-y-3">
              <InfoRow label="Type" value={check.check_type.toUpperCase()} />
              <InfoRow label="Target" value={target} mono />
              <InfoRow label="Timeout" value={`${check.timeout}s`} />
              <InfoRow label="Interval" value={check.check_interval >= 60 ? `${check.check_interval / 60}m` : `${check.check_interval}s`} />
              {check.check_type === 'http' && (
                <>
                  <InfoRow label="Method" value={check.http_method} />
                  <InfoRow label="Expected" value={`HTTP ${check.http_expected_status}`} />
                  {check.http_content_match && <InfoRow label="Content Match" value={check.http_content_match} mono />}
                  <InfoRow label="Redirects" value={check.http_follow_redirects ? 'Follow' : 'No'} />
                </>
              )}
              {check.check_type === 'tls' && (
                <>
                  <InfoRow label="Warn Days" value={`${check.tls_warn_days}d`} />
                  <InfoRow label="Critical Days" value={`${check.tls_critical_days}d`} />
                  {check.tls_issuer && <InfoRow label="Issuer" value={check.tls_issuer} small />}
                  {check.tls_subject && <InfoRow label="Subject" value={check.tls_subject} small />}
                </>
              )}
              {check.description && <InfoRow label="Description" value={check.description} />}
            </div>
          </div>

          {/* Timeline */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Timeline</h3>
            <div className="space-y-3">
              <InfoRow label="Last Check" value={check.last_check_at ? dayjs(check.last_check_at).format('MMM D, YYYY HH:mm:ss') : 'Never'} />
              <InfoRow label="Created" value={dayjs(check.created_at).format('MMM D, YYYY HH:mm')} />
              {check.updated_at && <InfoRow label="Updated" value={dayjs(check.updated_at).format('MMM D, YYYY HH:mm')} />}
            </div>
          </div>

          {/* Last Error */}
          {check.last_error && (
            <div className="bg-red-500/5 rounded-xl border border-red-500/20 p-5">
              <h3 className="text-sm font-semibold text-red-400 mb-2 flex items-center gap-2"><AlertTriangle className="w-4 h-4" /> Last Error</h3>
              <p className="text-xs font-mono text-red-400/80 break-all">{check.last_error}</p>
            </div>
          )}
        </div>
      </div>

      {/* ─── Delete Modal ─── */}
      {showDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setShowDelete(false)} />
          <div className="relative bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] rounded-2xl shadow-2xl w-full max-w-md p-6">
            <div className="flex justify-center mb-4">
              <div className="w-14 h-14 rounded-2xl bg-red-500/10 flex items-center justify-center"><AlertTriangle className="w-7 h-7 text-red-400" /></div>
            </div>
            <h3 className="text-lg font-semibold text-[var(--text-primary)] text-center mb-2">Delete {check.name}?</h3>
            <p className="text-sm text-[var(--text-muted)] text-center mb-4">This will permanently remove this service check and all its monitoring history.</p>
            <div className="flex gap-3">
              <button onClick={() => setShowDelete(false)} className="flex-1 py-2.5 rounded-lg text-sm font-medium text-[var(--text-secondary)] bg-[var(--bg-tertiary)]">Cancel</button>
              <button onClick={handleDelete} disabled={deleting}
                className="flex-1 py-2.5 rounded-lg text-sm font-semibold bg-red-500 text-white hover:bg-red-600 disabled:opacity-50 flex items-center justify-center gap-2">
                <Trash2 className="w-4 h-4" /> {deleting ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function InfoRow({ label, value, mono, small }: { label: string; value: string; mono?: boolean; small?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-2">
      <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mt-0.5 flex-shrink-0">{label}</span>
      <span className={cn('text-right text-[var(--text-primary)]', mono && 'font-mono', small ? 'text-[11px] break-all' : 'text-sm')}>{value}</span>
    </div>
  )
}
