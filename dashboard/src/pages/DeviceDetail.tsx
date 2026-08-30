import { useParams, useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { StatusIndicator } from '@/components/dashboard/StatusIndicator'
import { StatusCard } from '@/components/dashboard/StatusCard'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { formatRTT, timeAgo, statusColors, cn, copyText, formatDateTime, formatTime } from '@/lib/utils'
import { useTimezone } from '@/hooks/useSettings'
import { generateDeviceReport } from '@/lib/deviceReport'
import type { StatusEvent as ReportStatusEvent } from '@/lib/deviceReport'
import {
  ArrowLeft, Clock, Gauge, Wifi, WifiOff,
  MapPin, Tag, Trash2, Edit3, RefreshCw, Copy,
  Monitor, Server, Shield, Radio, Printer, HelpCircle,
  AlertTriangle, CheckCircle, X, ExternalLink, Activity,
  ArrowUpCircle, ArrowDownCircle, History, Calendar,
  FileDown, Loader2,
} from 'lucide-react'
import type { Device, DeviceStatus, MetricResponse } from '@/types'

const timeRanges = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
  { label: '90d', hours: 2160 },
  { label: '1y', hours: 8760 },
]

const typeIcons: Record<string, typeof Monitor> = {
  router: Radio, switch: Monitor, firewall: Shield, server: Server,
  access_point: Wifi, printer: Printer, other: HelpCircle,
}

// ─── Status Change Event ───
interface StatusEvent {
  device_id: string
  old_status: string
  new_status: string
  reason: string
  timestamp: string
  duration_sec: number | null
}

// ─── Delete Confirmation ───
function DeleteConfirm({ hostname, onConfirm, onCancel }: {
  hostname: string; onConfirm: () => void; onCancel: () => void
}) {
  const [text, setText] = useState('')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onCancel} />
      <div className="relative bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex justify-center mb-4">
          <div className="w-14 h-14 rounded-2xl bg-red-500/10 flex items-center justify-center">
            <AlertTriangle className="w-7 h-7 text-red-400" />
          </div>
        </div>
        <h3 className="text-lg font-semibold text-[var(--text-primary)] text-center mb-2">Delete {hostname}?</h3>
        <p className="text-sm text-[var(--text-muted)] text-center mb-4">This will permanently remove the device and all its monitoring history.</p>
        <div className="mb-4">
          <label className="block text-xs text-[var(--text-muted)] mb-2 text-center">
            Type <span className="font-mono font-bold text-red-400 bg-red-500/10 px-1.5 py-0.5 rounded">delete</span> to confirm
          </label>
          <input type="text" value={text} onChange={e => setText(e.target.value)} autoFocus placeholder="Type 'delete'..."
            className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-4 py-3 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-red-500/50 text-sm text-center font-mono" />
        </div>
        <div className="flex gap-3">
          <button onClick={onCancel} className="flex-1 py-2.5 rounded-lg text-sm font-medium text-[var(--text-secondary)] bg-[var(--bg-tertiary)]">Cancel</button>
          <button onClick={onConfirm} disabled={text.toLowerCase() !== 'delete'}
            className={cn('flex-1 py-2.5 rounded-lg text-sm font-semibold flex items-center justify-center gap-2 transition-all',
              text.toLowerCase() === 'delete' ? 'bg-red-500 text-white hover:bg-red-600' : 'bg-red-500/20 text-red-400/40 cursor-not-allowed')}>
            <Trash2 className="w-4 h-4" /> Delete
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Uptime Bar ───
function UptimeBar({ points, timezone }: { points: { is_up: boolean | null; timestamp: string }[]; timezone: string }) {
  if (points.length === 0) return null
  const total = points.length
  const upCount = points.filter(p => p.is_up === true || (p.is_up as unknown as number) === 1).length
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
        {points.map((p, i) => {
          const isUp = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && p.is_up > 0.5)
          const isDown = p.is_up === false || (p.is_up as unknown as number) === 0 || p.is_up === null
          return (
            <div
              key={i}
              className="flex-1 min-w-[2px] transition-colors hover:opacity-80"
              style={{ backgroundColor: isUp ? '#22C55E' : isDown ? '#EF4444' : '#2D3140' }}
              title={`${formatTime(p.timestamp, timezone)} - ${isUp ? 'UP' : 'DOWN'}`}
            />
          )
        })}
      </div>
      <div className="flex justify-between mt-1">
        <span className="text-[10px] text-[var(--text-muted)]">{points.length > 0 ? formatTime(points[0]!.timestamp, timezone) : ''}</span>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1 text-[10px] text-[var(--text-muted)]"><span className="w-2 h-2 rounded-sm bg-[#22C55E]" />Up</span>
          <span className="flex items-center gap-1 text-[10px] text-[var(--text-muted)]"><span className="w-2 h-2 rounded-sm bg-[#EF4444]" />Down</span>
        </div>
        <span className="text-[10px] text-[var(--text-muted)]">{points.length > 0 ? formatTime(points[points.length - 1]!.timestamp, timezone) : ''}</span>
      </div>
    </div>
  )
}

// ─── Incident Table with Pagination ───
const INCIDENTS_PER_PAGE = 20

function IncidentTable({ events, timezone }: { events: StatusEvent[]; timezone: string }) {
  const [page, setPage] = useState(0)

  if (events.length === 0) {
    return (
      <div className="text-center py-8 text-[var(--text-muted)] text-sm">
        No status changes recorded
      </div>
    )
  }

  const formatDuration = (sec: number | null) => {
    if (!sec || sec <= 0) return '-'
    if (sec < 60) return `${sec}s`
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`
    return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
  }

  const totalPages = Math.ceil(events.length / INCIDENTS_PER_PAGE)
  const safePage = Math.min(page, totalPages - 1)
  const pagedEvents = events.slice(safePage * INCIDENTS_PER_PAGE, (safePage + 1) * INCIDENTS_PER_PAGE)

  return (
    <div>
      <div className="overflow-hidden rounded-lg border border-[var(--bg-elevated)]">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-[var(--bg-tertiary)]">
              <th className="text-left px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Time</th>
              <th className="text-left px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Change</th>
              <th className="text-left px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Reason</th>
              <th className="text-right px-3 py-2.5 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Duration</th>
            </tr>
          </thead>
          <tbody>
            {pagedEvents.map((e, i) => {
              const isDown = e.new_status === 'down' || e.new_status === 'degraded'
              return (
                <tr key={i} className="border-t border-[var(--bg-elevated)]/50 hover:bg-[var(--bg-tertiary)]/50">
                  <td className="px-3 py-2.5 font-mono text-[var(--text-secondary)]">
                    {formatDateTime(e.timestamp, timezone)}
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-1.5">
                      {isDown
                        ? <ArrowDownCircle className="w-3.5 h-3.5 text-red-400" />
                        : <ArrowUpCircle className="w-3.5 h-3.5 text-green-400" />
                      }
                      <span className="uppercase font-medium" style={{ color: statusColors[e.old_status as DeviceStatus] || '#6B7280' }}>
                        {e.old_status}
                      </span>
                      <span className="text-[var(--text-muted)]">→</span>
                      <span className="uppercase font-medium" style={{ color: statusColors[e.new_status as DeviceStatus] || '#6B7280' }}>
                        {e.new_status}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2.5 text-[var(--text-muted)]">{e.reason}</td>
                  <td className="px-3 py-2.5 text-right font-mono text-[var(--text-secondary)]">{formatDuration(e.duration_sec)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-3 px-1">
          <span className="text-xs text-[var(--text-muted)]">
            Showing {safePage * INCIDENTS_PER_PAGE + 1}–{Math.min((safePage + 1) * INCIDENTS_PER_PAGE, events.length)} of {events.length}
          </span>
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => setPage(0)}
              disabled={safePage === 0}
              className="px-2 py-1 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              First
            </button>
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={safePage === 0}
              className="px-2.5 py-1 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              Prev
            </button>
            <span className="text-xs text-[var(--text-secondary)] px-2 font-mono">
              {safePage + 1} / {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={safePage >= totalPages - 1}
              className="px-2.5 py-1 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              Next
            </button>
            <button
              onClick={() => setPage(totalPages - 1)}
              disabled={safePage >= totalPages - 1}
              className="px-2 py-1 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              Last
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Ping Stats ───
function PingStats({ points }: { points: { rtt_ms: number | null; packet_loss: number | null; jitter_ms: number | null; is_up: boolean | null }[] }) {
  const upPoints = points.filter(p => {
    const isUp = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    return isUp && p.rtt_ms !== null && p.rtt_ms > 0
  })
  if (upPoints.length === 0) return null

  const rtts = upPoints.map(p => p.rtt_ms!)
  const avg = rtts.reduce((a, b) => a + b, 0) / rtts.length
  const min = Math.min(...rtts)
  const max = Math.max(...rtts)
  const sorted = [...rtts].sort((a, b) => a - b)
  const p95 = sorted[Math.floor(sorted.length * 0.95)] || max
  const totalChecks = points.length
  const downChecks = points.filter(p => p.is_up === false || (p.is_up as unknown as number) === 0).length
  const avgLoss = totalChecks > 0 ? downChecks / totalChecks : 0

  const stats = [
    { label: 'Avg RTT', value: formatRTT(avg), color: 'var(--accent)' },
    { label: 'Min RTT', value: formatRTT(min), color: 'var(--status-up)' },
    { label: 'Max RTT', value: formatRTT(max), color: 'var(--status-degraded)' },
    { label: 'P95 RTT', value: formatRTT(p95), color: '#8B5CF6' },
    { label: 'Availability', value: `${((1 - avgLoss) * 100).toFixed(2)}%`, color: avgLoss < 0.01 ? 'var(--status-up)' : 'var(--status-down)' },
    { label: 'Total Checks', value: String(totalChecks), color: 'var(--text-secondary)' },
  ]

  return (
    <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
      {stats.map(s => (
        <div key={s.label} className="text-center p-2.5 bg-[var(--bg-secondary)] rounded-lg border border-[var(--bg-elevated)]">
          <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-1">{s.label}</div>
          <div className="text-sm font-mono font-semibold" style={{ color: s.color }}>{s.value}</div>
        </div>
      ))}
    </div>
  )
}

// ─── Quick Actions ───
function QuickActions({ device, onDelete }: { device: Device; onDelete: () => void }) {
  const [copied, setCopied] = useState(false)
  const copyIP = async () => { if (await copyText(device.ip_address ?? '')) { setCopied(true); setTimeout(() => setCopied(false), 2000) } }

  return (
    <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
      <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-3">Quick Actions</h3>
      <div className="grid grid-cols-2 gap-2">
        <button onClick={copyIP} className="flex items-center gap-2 p-3 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
          {copied ? <CheckCircle className="w-4 h-4 text-green-400" /> : <Copy className="w-4 h-4" />}
          {copied ? 'Copied!' : 'Copy IP'}
        </button>
        <a href={`http://${device.ip_address}`} target="_blank" rel="noopener noreferrer" className="flex items-center gap-2 p-3 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
          <ExternalLink className="w-4 h-4" /> Open Web UI
        </a>
        <button className="flex items-center gap-2 p-3 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
          <RefreshCw className="w-4 h-4" /> Ping Now
        </button>
        <button onClick={onDelete} className="flex items-center gap-2 p-3 rounded-lg bg-red-500/5 text-xs text-red-400/80 hover:text-red-400 hover:bg-red-500/10 transition-colors">
          <Trash2 className="w-4 h-4" /> Delete
        </button>
      </div>
    </div>
  )
}

// ─── Main Page ───
export function DeviceDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [rangeHours, setRangeHours] = useState(24)
  const [showCustom, setShowCustom] = useState(false)
  const [customFrom, setCustomFrom] = useState('')
  const [customTo, setCustomTo] = useState('')
  // Applied custom range: only set when user clicks Apply
  const [appliedFrom, setAppliedFrom] = useState('')
  const [appliedTo, setAppliedTo] = useState('')
  const [useCustomRange, setUseCustomRange] = useState(false)
  const [showDelete, setShowDelete] = useState(false)
  const [generatingPdf, setGeneratingPdf] = useState(false)
  const timezone = useTimezone()

  const { data: device, isLoading, error } = useQuery({
    queryKey: ['device', id],
    queryFn: () => api.get<Device>(`/devices/${id}`),
    enabled: !!id,
    refetchInterval: 30_000,
  })

  const now = new Date()
  const effectiveFrom = useCustomRange && appliedFrom ? new Date(appliedFrom) : new Date(now.getTime() - rangeHours * 60 * 60 * 1000)
  const effectiveTo = useCustomRange && appliedTo ? new Date(appliedTo) : now
  const effectiveHours = (effectiveTo.getTime() - effectiveFrom.getTime()) / 3600000

  const { data: metrics } = useQuery({
    queryKey: ['device-metrics', id, useCustomRange ? `custom-${appliedFrom}-${appliedTo}` : rangeHours],
    queryFn: () => api.get<MetricResponse>(
      `/devices/${id}/metrics?from=${effectiveFrom.toISOString()}&to=${effectiveTo.toISOString()}&granularity=${effectiveHours <= 6 ? 'raw' : 'auto'}`
    ),
    enabled: !!id && !!device,
    refetchInterval: useCustomRange ? false : 30_000,
  })

  // Status history: pass same time range, use higher limit for large ranges
  const historyLimit = effectiveHours > 2160 ? 1000 : effectiveHours > 720 ? 500 : effectiveHours > 168 ? 200 : 100
  const { data: statusHistory = [] } = useQuery({
    queryKey: ['device-status-history', id, useCustomRange ? `custom-${appliedFrom}-${appliedTo}` : rangeHours],
    queryFn: () => api.get<StatusEvent[]>(
      `/devices/${id}/status-history?from=${effectiveFrom.toISOString()}&to=${effectiveTo.toISOString()}&limit=${historyLimit}`
    ),
    enabled: !!id && !!device,
    refetchInterval: useCustomRange ? false : 30_000,
  })

  const deleteMutation = useMutation({
    mutationFn: () => api.delete(`/devices/${id}`),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['devices'] }); navigate('/devices') },
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-32">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-[var(--text-muted)]">Loading device...</span>
        </div>
      </div>
    )
  }

  if (error || !device) {
    return (
      <div className="flex flex-col items-center justify-center py-32 gap-4">
        <div className="w-16 h-16 rounded-2xl bg-red-500/10 flex items-center justify-center">
          <WifiOff className="w-8 h-8 text-red-400" />
        </div>
        <h3 className="text-lg font-semibold text-[var(--text-primary)]">Device Not Found</h3>
        <button onClick={() => navigate('/devices')} className="flex items-center gap-2 px-4 py-2 bg-[var(--accent)] text-white rounded-lg text-sm font-medium">
          <ArrowLeft className="w-4 h-4" /> Back to Devices
        </button>
      </div>
    )
  }

  const points = metrics?.points || []
  const TypeIcon = typeIcons[device.device_type] || HelpCircle
  const stColor = statusColors[device.status as DeviceStatus] || '#6B7280'

  const handleDownloadReport = async () => {
    if (generatingPdf) return
    setGeneratingPdf(true)
    try {
      await generateDeviceReport({
        device,
        points,
        incidents: statusHistory as ReportStatusEvent[],
        rangeHours: effectiveHours,
        fromDate: effectiveFrom,
        toDate: effectiveTo,
        timezone,
      })
    } catch (err) {
      console.error('PDF generation failed:', err)
    } finally {
      setGeneratingPdf(false)
    }
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-start gap-4 mb-6">
        <button onClick={() => navigate('/devices')} className="p-2 mt-1 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors">
          <ArrowLeft className="w-5 h-5 text-[var(--text-secondary)]" />
        </button>
        <div className="flex-1">
          <div className="flex items-center gap-3 mb-1">
            <div className="w-10 h-10 rounded-xl flex items-center justify-center" style={{ backgroundColor: `${stColor}15` }}>
              <TypeIcon className="w-5 h-5" style={{ color: stColor }} />
            </div>
            <div>
              <div className="flex items-center gap-3">
                <h2 className="text-xl font-semibold text-[var(--text-primary)]">{device.hostname}</h2>
                <StatusIndicator status={device.status as DeviceStatus} showLabel size="lg" />
              </div>
              <p className="text-sm text-[var(--text-muted)] font-mono flex items-center gap-2 mt-0.5">
                {device.ip_address}
                <span className="text-[var(--bg-elevated)]">|</span>
                <span className="capitalize text-[var(--text-secondary)] font-sans">{device.device_type.replace('_', ' ')}</span>
                {device.group_name && (<><span className="text-[var(--bg-elevated)]">|</span><span className="text-[var(--text-secondary)] font-sans">{device.group_name}</span></>)}
              </p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={handleDownloadReport} disabled={generatingPdf || points.length === 0}
            className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--accent)] text-sm text-white font-medium hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all">
            {generatingPdf ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileDown className="w-4 h-4" />}
            {generatingPdf ? 'Generating...' : 'PDF Report'}
          </button>
          <button onClick={() => navigate(`/devices/${id}/edit`)} className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors">
            <Edit3 className="w-4 h-4" /> Edit
          </button>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        <StatusCard
          title="Current Status"
          value={device.status === 'up' ? 'Online' : device.status === 'down' ? 'Offline' : device.status.charAt(0).toUpperCase() + device.status.slice(1)}
          subtitle={device.status === 'up' ? 'Responding normally' : device.status === 'down' ? `Since ${timeAgo(device.last_seen)}` : undefined}
          icon={device.status === 'up' ? <Wifi className="w-5 h-5" /> : <WifiOff className="w-5 h-5" />}
          color={stColor}
        />
        <StatusCard
          title="Last RTT"
          value={device.status === 'up' ? formatRTT(device.last_rtt_ms) : '--'}
          icon={<Gauge className="w-5 h-5" />}
          color="var(--accent)"
        />
        <StatusCard
          title="Ping Interval"
          value={`${device.ping_interval}s`}
          icon={<RefreshCw className="w-5 h-5" />}
          color="var(--text-secondary)"
        />
        <StatusCard
          title="Last Seen"
          value={device.status === 'up' && device.last_seen && (Date.now() - new Date(device.last_seen).getTime()) < 120000 ? 'Active Now' : timeAgo(device.last_seen)}
          subtitle={device.status === 'up' ? `Checking every ${device.ping_interval}s` : device.status === 'down' ? 'Device unreachable' : undefined}
          icon={<Clock className="w-5 h-5" />}
          color={device.status === 'up' ? 'var(--status-up)' : 'var(--text-secondary)'}
        />
      </div>

      {/* Ping Statistics */}
      {points.length > 0 && (
        <div className="mb-6">
          <PingStats points={points} />
        </div>
      )}

      {/* Main content */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left column - charts (2/3) */}
        <div className="lg:col-span-2 space-y-6">
          {/* Time series chart */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Response Time & Packet Loss</h3>
              <div className="flex items-center gap-1.5 flex-wrap justify-end">
                {timeRanges.map((r) => (
                  <button key={r.label} onClick={() => { setRangeHours(r.hours); setUseCustomRange(false); setShowCustom(false) }}
                    className={cn('px-3 py-1 rounded text-xs font-medium transition-colors',
                      !useCustomRange && rangeHours === r.hours ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]')}>
                    {r.label}
                  </button>
                ))}
                <button onClick={() => setShowCustom(!showCustom)}
                  className={cn('px-2.5 py-1 rounded text-xs font-medium transition-colors flex items-center gap-1',
                    useCustomRange ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]')}>
                  <Calendar size={12} /> Custom
                </button>
                {showCustom && (
                  <div className="flex items-center gap-2 ml-1">
                    <input type="datetime-local" value={customFrom}
                      onChange={e => setCustomFrom(e.target.value)}
                      className="rounded border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] px-2 py-1 text-xs text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]" />
                    <span className="text-xs text-[var(--text-muted)]">to</span>
                    <input type="datetime-local" value={customTo}
                      onChange={e => setCustomTo(e.target.value)}
                      className="rounded border border-[var(--bg-elevated)] bg-[var(--bg-tertiary)] px-2 py-1 text-xs text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]" />
                    <button onClick={() => {
                        if (customFrom && customTo) {
                          setAppliedFrom(customFrom)
                          setAppliedTo(customTo)
                          setUseCustomRange(true)
                        }
                      }}
                      disabled={!customFrom || !customTo}
                      className="px-3 py-1 rounded text-xs font-medium text-white transition-all hover:brightness-110 disabled:opacity-40"
                      style={{ background: 'var(--accent)' }}>
                      Apply
                    </button>
                  </div>
                )}
              </div>
            </div>
            {points.length > 0 ? (
              <TimeSeriesChart data={points} height={320} showPacketLoss timezone={timezone} rangeHours={effectiveHours} />
            ) : (
              <div className="h-[320px] flex flex-col items-center justify-center text-[var(--text-muted)] gap-2">
                <Activity className="w-8 h-8 opacity-30" />
                <span className="text-sm">No data for selected time range</span>
              </div>
            )}
          </div>

          {/* Uptime bar */}
          {points.length > 0 && (
            <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
              <UptimeBar points={points} timezone={timezone} />
            </div>
          )}

          {/* Incident History Table */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <div className="flex items-center gap-2 mb-4">
              <History className="w-4 h-4 text-[var(--accent)]" />
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Incident History</h3>
              <span className="text-[10px] text-[var(--text-muted)] bg-[var(--bg-tertiary)] px-2 py-0.5 rounded-full">{statusHistory.length} events</span>
            </div>
            <IncidentTable events={statusHistory} timezone={timezone} />
          </div>
        </div>

        {/* Right column - info (1/3) */}
        <div className="space-y-6">
          <QuickActions device={device} onDelete={() => setShowDelete(true)} />

          {/* Device Info */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Device Information</h3>
            <div className="space-y-3">
              <InfoRow label="IP Address" value={device.ip_address} mono />
              <InfoRow label="Type" value={device.device_type.replace('_', ' ')} capitalize />
              <InfoRow label="Group" value={device.group_name || 'None'} />
              <InfoRow label="Location" value={device.location || 'Not set'} icon={<MapPin className="w-3.5 h-3.5" />} />
              <InfoRow label="Ping" value={device.ping_enabled ? `Enabled (${device.ping_interval}s)` : 'Disabled'} />
              <InfoRow label="Description" value={device.description || 'None'} />
              {device.tags && device.tags.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-1.5">Tags</div>
                  <div className="flex flex-wrap gap-1.5">
                    {device.tags.map((tag, i) => (
                      <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-[var(--bg-tertiary)] text-[11px] text-[var(--text-secondary)]">
                        <Tag className="w-3 h-3" />{String(tag)}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Timestamps */}
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
            <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Timeline</h3>
            <div className="space-y-3">
              <InfoRow label="Last Seen" value={device.last_seen ? formatDateTime(device.last_seen, timezone) : 'Never'} />
              <InfoRow label="Created" value={formatDateTime(device.created_at, timezone)} />
              <InfoRow label="Updated" value={formatDateTime(device.updated_at, timezone)} />
            </div>
          </div>
        </div>
      </div>

      {showDelete && (
        <DeleteConfirm hostname={device.hostname} onConfirm={() => deleteMutation.mutate()} onCancel={() => setShowDelete(false)} />
      )}
    </div>
  )
}

function InfoRow({ label, value, mono, capitalize: cap, icon }: {
  label: string; value: string; mono?: boolean; capitalize?: boolean; icon?: React.ReactNode
}) {
  return (
    <div className="flex items-start justify-between gap-2">
      <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mt-0.5 flex items-center gap-1 flex-shrink-0">{icon}{label}</span>
      <span className={cn('text-sm text-right text-[var(--text-primary)]', mono && 'font-mono', cap && 'capitalize')}>{value}</span>
    </div>
  )
}
