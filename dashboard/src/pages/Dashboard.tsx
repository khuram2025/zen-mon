import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import {
  Monitor, Wifi, WifiOff, AlertTriangle, Activity, Globe,
  ArrowUpRight, ArrowDownRight, Clock, Zap, ShieldCheck, MapPin,
  Server, Radio, Shield, Printer, HelpCircle, ChevronRight,
  AlertCircle, Info, TrendingUp, BarChart3, Gauge,
} from 'lucide-react'
import { useDeviceSummary, useDevices, useDeviceGroups, useDeviceLocations } from '@/hooks/useDevices'
import { useServiceCheckSummary, useServiceChecks } from '@/hooks/useServiceChecks'
import { useSSE } from '@/hooks/useSSE'
import { useTimezone } from '@/hooks/useSettings'
import { api } from '@/lib/api'
import { cn, statusColors, statusLabels, severityColors, timeAgo, formatRTT } from '@/lib/utils'
import type { Device, DeviceStatus, Alert, PaginatedResponse, ServiceCheck } from '@/types'

// ── Icon map ─────────────────────────────────────────────────────────────────
const typeIcons: Record<string, typeof Monitor> = {
  router: Radio, switch: Monitor, firewall: Shield, server: Server,
  access_point: Wifi, printer: Printer, other: HelpCircle,
}


// ── Time ranges ──────────────────────────────────────────────────────────────
const timeRanges = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '12h', hours: 12 },
  { label: '24h', hours: 24 },
  { label: '3d', hours: 72 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
]

// ── Animated number ──────────────────────────────────────────────────────────
function AnimNum({ value, color }: { value: number; color?: string }) {
  return <span className="font-mono text-3xl font-bold tabular-nums" style={{ color }}>{value}</span>
}

// ── Uptime gauge using ECharts ───────────────────────────────────────────────
function UptimeGauge({ percent }: { percent: number }) {
  const color = percent >= 99 ? '#22C55E' : percent >= 95 ? '#EAB308' : '#EF4444'
  const option = {
    series: [{
      type: 'gauge',
      startAngle: 220,
      endAngle: -40,
      radius: '100%',
      center: ['50%', '55%'],
      min: 0,
      max: 100,
      progress: { show: true, width: 14, roundCap: true, itemStyle: { color } },
      axisLine: { lineStyle: { width: 14, color: [[1, '#1E2130']] } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      pointer: { show: false },
      title: { show: false },
      detail: {
        valueAnimation: true,
        fontSize: 22,
        fontFamily: 'ui-monospace, monospace',
        fontWeight: 'bold',
        color,
        formatter: '{value}%',
        offsetCenter: [0, '10%'],
      },
      data: [{ value: percent }],
    }],
  }
  return <ReactECharts option={option} style={{ height: 160 }} opts={{ renderer: 'svg' }} />
}

// ── Device type donut chart ──────────────────────────────────────────────────
function DeviceTypeChart({ devices }: { devices: Device[] }) {
  const data = useMemo(() => {
    const counts: Record<string, number> = {}
    devices.forEach(d => { counts[d.device_type] = (counts[d.device_type] || 0) + 1 })
    return Object.entries(counts).map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value)
  }, [devices])

  const palette = ['#6366F1', '#22C55E', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#14B8A6']
  const option = {
    tooltip: { trigger: 'item', backgroundColor: '#1A1D27', borderColor: '#2D3140', textStyle: { color: '#E8EAED', fontSize: 12 }, formatter: '{b}: {c} ({d}%)' },
    legend: { show: false },
    series: [{
      type: 'pie',
      radius: ['55%', '80%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#12141E', borderWidth: 2 },
      label: { show: false },
      emphasis: { label: { show: true, fontSize: 13, fontWeight: 'bold', color: '#E8EAED' }, itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' } },
      data: data.map((d, i) => ({ ...d, itemStyle: { color: palette[i % palette.length] } })),
    }],
  }

  return (
    <div>
      <ReactECharts option={option} style={{ height: 200 }} opts={{ renderer: 'svg' }} />
      <div className="flex flex-wrap justify-center gap-x-4 gap-y-1 mt-2">
        {data.map((d, i) => (
          <div key={d.name} className="flex items-center gap-1.5 text-xs text-[var(--text-muted)]">
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: palette[i % palette.length] }} />
            <span className="capitalize">{d.name}</span>
            <span className="font-mono text-[var(--text-secondary)]">{d.value}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Status distribution bar ──────────────────────────────────────────────────
function StatusBar({ summary, onSegmentClick }: { summary: { total: number; up: number; down: number; degraded: number; unknown: number; maintenance: number }; onSegmentClick?: (status: DeviceStatus) => void }) {
  if (summary.total === 0) return null
  const segments: { status: DeviceStatus; count: number; color: string }[] = [
    { status: 'up', count: summary.up, color: '#22C55E' },
    { status: 'degraded', count: summary.degraded, color: '#EAB308' },
    { status: 'down', count: summary.down, color: '#EF4444' },
    { status: 'unknown', count: summary.unknown, color: '#6B7280' },
    { status: 'maintenance', count: summary.maintenance, color: '#3B82F6' },
  ]
  return (
    <div onClick={e => e.stopPropagation()}>
      <div className="flex h-3 rounded-full overflow-hidden bg-[var(--bg-tertiary)]">
        {segments.filter(s => s.count > 0).map(s => (
          <div key={s.status} onClick={() => onSegmentClick?.(s.status)} style={{ width: `${(s.count / summary.total) * 100}%`, background: s.color }} className="transition-all duration-500 cursor-pointer hover:opacity-80" title={`${statusLabels[s.status]}: ${s.count} — click to filter`} />
        ))}
      </div>
      <div className="flex items-center gap-4 mt-2.5">
        {segments.filter(s => s.count > 0).map(s => (
          <div key={s.status} onClick={() => onSegmentClick?.(s.status)} className="flex items-center gap-1.5 text-xs text-[var(--text-muted)] cursor-pointer hover:text-[var(--text-primary)] transition-colors">
            <span className="w-2 h-2 rounded-full" style={{ background: s.color }} />
            {statusLabels[s.status]} <span className="font-mono text-[var(--text-secondary)]">{s.count}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Response time sparkline ──────────────────────────────────────────────────
function RTTSparkline({ devices }: { devices: Device[] }) {
  const upDevices = useMemo(() =>
    devices.filter(d => d.status === 'up' && d.last_rtt_ms !== null).sort((a, b) => (b.last_rtt_ms ?? 0) - (a.last_rtt_ms ?? 0)).slice(0, 15),
    [devices]
  )
  if (upDevices.length === 0) return <div className="text-sm text-[var(--text-muted)] text-center py-8">No RTT data</div>

  const option = {
    tooltip: { trigger: 'axis', backgroundColor: '#1A1D27', borderColor: '#2D3140', textStyle: { color: '#E8EAED', fontSize: 12 }, formatter: (p: { name: string; value: number }[]) => `${p[0]?.name}<br/>RTT: <b>${p[0]?.value?.toFixed(2)}ms</b>` },
    grid: { top: 10, right: 10, bottom: 30, left: 10 },
    xAxis: { type: 'category', data: upDevices.map(d => d.hostname), axisLabel: { color: '#5F6578', fontSize: 10, rotate: 45 }, axisLine: { lineStyle: { color: '#2D3140' } } },
    yAxis: { type: 'value', show: false },
    series: [{
      data: upDevices.map(d => ({
        value: d.last_rtt_ms,
        itemStyle: { color: (d.last_rtt_ms ?? 0) < 10 ? '#22C55E' : (d.last_rtt_ms ?? 0) < 50 ? '#EAB308' : '#EF4444' },
      })),
      type: 'bar',
      barWidth: '60%',
      itemStyle: { borderRadius: [4, 4, 0, 0] },
    }],
  }
  return <ReactECharts option={option} style={{ height: 220 }} opts={{ renderer: 'svg' }} />
}

// ── Service checks summary donut ─────────────────────────────────────────────
function ServiceSummaryChart({ summary }: { summary: { total: number; up: number; down: number; warning: number; degraded: number; unknown: number } }) {
  const data = [
    { name: 'Healthy', value: summary.up, color: '#22C55E' },
    { name: 'Warning', value: summary.warning, color: '#F59E0B' },
    { name: 'Degraded', value: summary.degraded, color: '#EAB308' },
    { name: 'Down', value: summary.down, color: '#EF4444' },
    { name: 'Unknown', value: summary.unknown, color: '#6B7280' },
  ].filter(d => d.value > 0)

  if (data.length === 0) return <div className="text-sm text-[var(--text-muted)] text-center py-8">No service checks</div>

  const option = {
    tooltip: { trigger: 'item', backgroundColor: '#1A1D27', borderColor: '#2D3140', textStyle: { color: '#E8EAED', fontSize: 12 } },
    series: [{
      type: 'pie', radius: ['60%', '82%'], center: ['50%', '50%'],
      label: { show: false },
      itemStyle: { borderRadius: 4, borderColor: '#12141E', borderWidth: 2 },
      data: data.map(d => ({ name: d.name, value: d.value, itemStyle: { color: d.color } })),
    }],
    graphic: [{
      type: 'text', left: 'center', top: 'center',
      style: { text: `${summary.total}`, fontSize: 24, fontWeight: 'bold', fill: '#E8EAED', fontFamily: 'ui-monospace, monospace' },
    }, {
      type: 'text', left: 'center', top: '55%',
      style: { text: 'checks', fontSize: 11, fill: '#5F6578', fontFamily: 'system-ui' },
    }],
  }

  return (
    <div>
      <ReactECharts option={option} style={{ height: 180 }} opts={{ renderer: 'svg' }} />
      <div className="flex flex-wrap justify-center gap-x-4 gap-y-1 mt-1">
        {data.map(d => (
          <div key={d.name} className="flex items-center gap-1.5 text-xs text-[var(--text-muted)]">
            <span className="w-2 h-2 rounded-full" style={{ background: d.color }} />
            {d.name} <span className="font-mono text-[var(--text-secondary)]">{d.value}</span>
          </div>
        ))}
      </div>
    </div>
  )
}


// ── Uptime by Location chart ─────────────────────────────────────────────────
function UptimeByLocationChart({ devices, deviceUptime }: { devices: Device[]; deviceUptime: Record<string, number> }) {
  const data = useMemo(() => {
    const map: Record<string, { total: number; uptimeSum: number }> = {}
    devices.forEach(d => {
      const loc = d.location || 'Unassigned'
      if (!map[loc]) map[loc] = { total: 0, uptimeSum: 0 }
      map[loc].total++
      map[loc].uptimeSum += (deviceUptime[d.id] ?? (d.status === 'up' ? 100 : 0))
    })
    return Object.entries(map)
      .map(([name, { total, uptimeSum }]) => ({ name, pct: total > 0 ? parseFloat((uptimeSum / total).toFixed(1)) : 0, total, up: Math.round(uptimeSum / 100 * (total > 0 ? 1 : 0)) }))
      .sort((a, b) => a.pct - b.pct)
  }, [devices, deviceUptime])

  if (data.length === 0) return <div className="text-sm text-[var(--text-muted)] text-center py-8">No location data</div>

  const option = {
    tooltip: { trigger: 'axis', backgroundColor: '#1A1D27', borderColor: '#2D3140', textStyle: { color: '#E8EAED', fontSize: 12 },
      formatter: (p: { name: string; value: number }[]) => {
        const d = data.find(x => x.name === p[0]?.name)
        return `<b>${p[0]?.name}</b><br/>Uptime: <b style="color:${p[0]?.value >= 99 ? '#22C55E' : p[0]?.value >= 90 ? '#EAB308' : '#EF4444'}">${p[0]?.value}%</b><br/>Devices: ${d?.up}/${d?.total}`
      }
    },
    grid: { top: 10, right: 50, bottom: 5, left: 10, containLabel: true },
    xAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: '#5F6578', fontSize: 10, formatter: '{value}%' }, splitLine: { lineStyle: { color: '#1E2130' } }, axisLine: { show: false } },
    yAxis: { type: 'category', data: data.map(d => d.name), axisLabel: { color: '#9BA1B0', fontSize: 11, width: 80, overflow: 'truncate' }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{
      type: 'bar', data: data.map(d => ({
        value: d.pct,
        itemStyle: { color: d.pct >= 99 ? '#22C55E' : d.pct >= 90 ? '#EAB308' : '#EF4444', borderRadius: [0, 4, 4, 0] },
      })),
      barWidth: '60%',
      label: { show: true, position: 'right', fontSize: 11, fontFamily: 'ui-monospace, monospace', color: '#9BA1B0', formatter: '{c}%' },
    }],
  }
  return <ReactECharts option={option} style={{ height: Math.max(150, data.length * 36) }} opts={{ renderer: 'svg' }} />
}

// ── Uptime by Group chart ────────────────────────────────────────────────────
function UptimeByGroupChart({ devices, groups, deviceUptime }: { devices: Device[]; groups: { id: string; name: string }[]; deviceUptime: Record<string, number> }) {
  const data = useMemo(() => {
    const map: Record<string, { name: string; total: number; uptimeSum: number }> = {}
    const gMap: Record<string, string> = {}
    groups.forEach(g => { gMap[g.id] = g.name })
    devices.forEach(d => {
      const name = d.group_id ? (gMap[d.group_id] || 'Unknown') : 'Ungrouped'
      if (!map[name]) map[name] = { name, total: 0, uptimeSum: 0 }
      map[name].total++
      map[name].uptimeSum += (deviceUptime[d.id] ?? (d.status === 'up' ? 100 : 0))
    })
    return Object.values(map)
      .map(({ name, total, uptimeSum }) => ({ name, pct: total > 0 ? parseFloat((uptimeSum / total).toFixed(1)) : 0, total, up: Math.round(uptimeSum / 100) }))
      .sort((a, b) => a.pct - b.pct)
  }, [devices, groups, deviceUptime])

  if (data.length === 0) return <div className="text-sm text-[var(--text-muted)] text-center py-8">No group data</div>

  const option = {
    tooltip: { trigger: 'axis', backgroundColor: '#1A1D27', borderColor: '#2D3140', textStyle: { color: '#E8EAED', fontSize: 12 },
      formatter: (p: { name: string; value: number }[]) => {
        const d = data.find(x => x.name === p[0]?.name)
        return `<b>${p[0]?.name}</b><br/>Uptime: <b style="color:${p[0]?.value >= 99 ? '#22C55E' : p[0]?.value >= 90 ? '#EAB308' : '#EF4444'}">${p[0]?.value}%</b><br/>Devices: ${d?.up}/${d?.total}`
      }
    },
    grid: { top: 10, right: 50, bottom: 5, left: 10, containLabel: true },
    xAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: '#5F6578', fontSize: 10, formatter: '{value}%' }, splitLine: { lineStyle: { color: '#1E2130' } }, axisLine: { show: false } },
    yAxis: { type: 'category', data: data.map(d => d.name), axisLabel: { color: '#9BA1B0', fontSize: 11, width: 80, overflow: 'truncate' }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{
      type: 'bar', data: data.map(d => ({
        value: d.pct,
        itemStyle: { color: d.pct >= 99 ? '#22C55E' : d.pct >= 90 ? '#EAB308' : '#EF4444', borderRadius: [0, 4, 4, 0] },
      })),
      barWidth: '60%',
      label: { show: true, position: 'right', fontSize: 11, fontFamily: 'ui-monospace, monospace', color: '#9BA1B0', formatter: '{c}%' },
    }],
  }
  return <ReactECharts option={option} style={{ height: Math.max(150, data.length * 36) }} opts={{ renderer: 'svg' }} />
}

// ── Uptime by Type chart ─────────────────────────────────────────────────────
function UptimeByTypeChart({ devices, deviceUptime }: { devices: Device[]; deviceUptime: Record<string, number> }) {
  const data = useMemo(() => {
    const map: Record<string, { total: number; uptimeSum: number }> = {}
    devices.forEach(d => {
      const t = d.device_type || 'other'
      if (!map[t]) map[t] = { total: 0, uptimeSum: 0 }
      map[t].total++
      map[t].uptimeSum += (deviceUptime[d.id] ?? (d.status === 'up' ? 100 : 0))
    })
    return Object.entries(map)
      .map(([name, { total, uptimeSum }]) => ({ name, pct: total > 0 ? parseFloat((uptimeSum / total).toFixed(1)) : 0, total, up: Math.round(uptimeSum / 100) }))
      .sort((a, b) => b.pct - a.pct)
  }, [devices, deviceUptime])

  if (data.length === 0) return <div className="text-sm text-[var(--text-muted)] text-center py-8">No type data</div>

  const palette = ['#6366F1', '#22C55E', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#14B8A6']
  const option = {
    tooltip: { trigger: 'item', backgroundColor: '#1A1D27', borderColor: '#2D3140', textStyle: { color: '#E8EAED', fontSize: 12 },
      formatter: (p: { name: string; value: number; data: { total: number; up: number } }) => `<b style="text-transform:capitalize">${p.name}</b><br/>Uptime: <b>${p.value}%</b><br/>Devices: ${p.data.up}/${p.data.total}`
    },
    series: [{
      type: 'pie', radius: ['45%', '75%'], center: ['50%', '50%'],
      roseType: 'radius',
      itemStyle: { borderRadius: 6, borderColor: '#12141E', borderWidth: 2 },
      label: { show: true, color: '#9BA1B0', fontSize: 11, formatter: (p: { name: string; value: number }) => `${p.name}\n${p.value}%` },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.3)' } },
      data: data.map((d, i) => ({ name: d.name, value: d.pct, total: d.total, up: d.up, itemStyle: { color: palette[i % palette.length] } })),
    }],
  }
  return <ReactECharts option={option} style={{ height: 230 }} opts={{ renderer: 'svg' }} />
}

// ── Severity icons ───────────────────────────────────────────────────────────
const sevIcons: Record<string, typeof AlertCircle> = { critical: AlertCircle, warning: AlertTriangle, info: Info }

// ── MAIN DASHBOARD ───────────────────────────────────────────────────────────
export function DashboardPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const timezone = useTimezone()
  const [rangeHours, setRangeHours] = useState(24)

  const { data: summary } = useDeviceSummary()
  const { data: devicesData } = useDevices({ limit: 200 })
  const { data: svcSummary } = useServiceCheckSummary()
  const { data: svcData } = useServiceChecks({ limit: 50 })
  const { data: groups } = useDeviceGroups()
  const { data: alertsData } = useQuery({
    queryKey: ['active-alerts'],
    queryFn: () => api.get<PaginatedResponse<Alert>>('/alerts?status=active&limit=20'),
    refetchInterval: 15_000,
  })

  const { data: uptimeStats } = useQuery({
    queryKey: ['dashboard-uptime-stats', rangeHours],
    queryFn: () => api.get<{ hours: number; devices: Record<string, number> }>(`/devices/dashboard/uptime-stats?hours=${rangeHours}`),
    refetchInterval: 30_000,
  })

  useSSE('/api/v1/stream/status', {
    onMessage: () => {
      queryClient.invalidateQueries({ queryKey: ['device-summary'] })
      queryClient.invalidateQueries({ queryKey: ['devices'] })
      queryClient.invalidateQueries({ queryKey: ['active-alerts'] })
      queryClient.invalidateQueries({ queryKey: ['service-check-summary'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-uptime-stats'] })
    },
  })

  const devices = devicesData?.data || []
  const alerts = alertsData?.data || []
  const services = svcData?.data || []
  const s = summary || { total: 0, up: 0, down: 0, degraded: 0, unknown: 0, maintenance: 0 }
  const sv = svcSummary || { total: 0, up: 0, down: 0, warning: 0, degraded: 0, unknown: 0 }
  const uptimePct = s.total > 0 ? parseFloat(((s.up / s.total) * 100).toFixed(2)) : 100

  // Compute uptime per device (from historical metrics or live status as fallback)
  const deviceUptime = useMemo(() => {
    const map: Record<string, number> = {}
    const um = uptimeStats?.devices || {}
    devices.forEach(d => {
      if (um[d.id] !== undefined) {
        map[d.id] = um[d.id]
      } else {
        map[d.id] = d.status === 'up' ? 100 : d.status === 'degraded' ? 50 : 0
      }
    })
    return map
  }, [devices, uptimeStats])

  // Top offenders - highest RTT or down
  const topOffenders = useMemo(() => {
    const down = devices.filter(d => d.status === 'down').map(d => ({ ...d, _sort: Infinity }))
    const degraded = devices.filter(d => d.status === 'degraded').map(d => ({ ...d, _sort: 10000 + (d.last_rtt_ms ?? 0) }))
    const slowest = devices.filter(d => d.status === 'up' && (d.last_rtt_ms ?? 0) > 20).map(d => ({ ...d, _sort: d.last_rtt_ms ?? 0 }))
    return [...down, ...degraded, ...slowest].sort((a, b) => b._sort - a._sort).slice(0, 6)
  }, [devices])

  // Group stats
  const groupStats = useMemo(() => {
    if (!groups) return []
    const map: Record<string, { name: string; total: number; up: number; down: number }> = {}
    groups.forEach(g => { map[g.id] = { name: g.name, total: 0, up: 0, down: 0 } })
    devices.forEach(d => {
      if (d.group_id && map[d.group_id]) {
        map[d.group_id].total++
        const upt = deviceUptime[d.id] ?? (d.status === 'up' ? 100 : 0)
        if (upt >= 50) map[d.group_id].up++
        if (d.status === 'down') map[d.group_id].down++
      }
    })
    return Object.values(map).filter(g => g.total > 0).sort((a, b) => b.total - a.total)
  }, [devices, groups])

  return (
    <div className="min-h-screen w-full bg-[var(--bg-primary)]">
      {/* ─── Header ─── */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-[var(--text-primary)]">Dashboard</h1>
            <p className="mt-0.5 text-sm text-[var(--text-muted)]">Network monitoring overview</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] overflow-hidden">
            {timeRanges.map(r => (
              <button key={r.hours} onClick={() => setRangeHours(r.hours)}
                className={cn(
                  "px-3 py-1.5 text-xs font-medium transition-all",
                  rangeHours === r.hours
                    ? "bg-[var(--accent)] text-white"
                    : "text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]"
                )}>
                {r.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
            <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
            Live &middot; {timezone}
          </div>
        </div>
      </div>

      {/* ─── KPI Cards ─── */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-6">
        {/* Total */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Devices</span>
            <Monitor className="w-4 h-4 text-[var(--accent)]" />
          </div>
          <AnimNum value={s.total} color="var(--accent)" />
          <div className="mt-2"><StatusBar summary={s} onSegmentClick={(status) => navigate(`/devices?status=${status}`)} /></div>
        </div>
        {/* Online */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices?status=up')}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Online</span>
            <Wifi className="w-4 h-4 text-green-400" />
          </div>
          <AnimNum value={s.up} color="#22C55E" />
          <div className="flex items-center gap-1.5 mt-2 text-xs">
            <ArrowUpRight className="w-3.5 h-3.5 text-green-400" />
            <span className="text-green-400 font-medium">{s.total > 0 ? ((s.up / s.total) * 100).toFixed(1) : 0}%</span>
            <span className="text-[var(--text-muted)]">availability</span>
          </div>
        </div>
        {/* Down */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices?status=down')}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Offline</span>
            <WifiOff className="w-4 h-4 text-red-400" />
          </div>
          <AnimNum value={s.down} color="#EF4444" />
          {s.down > 0 && (
            <div className="flex items-center gap-1.5 mt-2 text-xs">
              <ArrowDownRight className="w-3.5 h-3.5 text-red-400" />
              <span className="text-red-400 font-medium">{((s.down / s.total) * 100).toFixed(1)}%</span>
              <span className="text-[var(--text-muted)]">of fleet</span>
            </div>
          )}
        </div>
        {/* Services */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/service-checks')}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Services</span>
            <ShieldCheck className="w-4 h-4 text-indigo-400" />
          </div>
          <AnimNum value={sv.total} color="#818CF8" />
          <div className="flex items-center gap-1.5 mt-2 text-xs">
            <span className="text-green-400 font-medium">{sv.up} up</span>
            {sv.down > 0 && <><span className="text-[var(--text-muted)]">&middot;</span><span className="text-red-400 font-medium">{sv.down} down</span></>}
          </div>
        </div>
        {/* Alerts */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/alerts')}>
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider">Alerts</span>
            <AlertTriangle className="w-4 h-4 text-amber-400" />
          </div>
          <AnimNum value={alerts.length} color={alerts.length > 0 ? '#F59E0B' : '#6B7280'} />
          <div className="flex items-center gap-1.5 mt-2 text-xs">
            {alerts.filter(a => a.severity === 'critical').length > 0 && (
              <span className="text-red-400 font-medium">{alerts.filter(a => a.severity === 'critical').length} critical</span>
            )}
            {alerts.filter(a => a.severity === 'critical').length === 0 && (
              <span className="text-green-400 font-medium">All clear</span>
            )}
          </div>
        </div>
      </div>

      {/* ─── Row 2: Uptime Gauge + Device Types + Service Summary ─── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        {/* Uptime Gauge */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
          <div className="flex items-center gap-2 mb-2">
            <Gauge className="w-4 h-4 text-[var(--accent)]" />
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Network Uptime</h3>
          </div>
          <UptimeGauge percent={(() => { const vals = Object.values(deviceUptime); return vals.length > 0 ? parseFloat((vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(2)) : uptimePct })() } />
        </div>
        {/* Device Types */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
          <div className="flex items-center gap-2 mb-2">
            <BarChart3 className="w-4 h-4 text-[var(--accent)]" />
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Device Types</h3>
          </div>
          <DeviceTypeChart devices={devices} />
        </div>
        {/* Service Summary */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/service-checks')}>
          <div className="flex items-center gap-2 mb-2">
            <ShieldCheck className="w-4 h-4 text-[var(--accent)]" />
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Service Checks</h3>
          </div>
          <ServiceSummaryChart summary={sv} />
        </div>
      </div>

      {/* ─── Row 3: RTT Chart + Alerts + Groups ─── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        {/* Response Times */}
        <div className="lg:col-span-2 rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-[var(--accent)]" />
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Response Times (Top 15)</h3>
            </div>
            <span className="text-xs text-[var(--text-muted)]">Current RTT per device</span>
          </div>
          <RTTSparkline devices={devices} />
        </div>

        {/* Active Alerts */}
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 text-amber-400" />
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Active Alerts</h3>
            </div>
            {alerts.length > 0 && (
              <button onClick={() => navigate('/alerts')} className="text-xs text-[var(--accent)] hover:underline flex items-center gap-0.5">
                View all <ChevronRight size={12} />
              </button>
            )}
          </div>
          <div className="space-y-2.5 max-h-[250px] overflow-y-auto pr-1">
            {alerts.length === 0 ? (
              <div className="text-center py-8">
                <ShieldCheck className="w-8 h-8 text-green-500/30 mx-auto mb-2" />
                <p className="text-sm text-[var(--text-muted)]">No active alerts</p>
              </div>
            ) : alerts.map(alert => {
              const Icon = sevIcons[alert.severity] || Info
              return (
                <div key={alert.id} onClick={() => navigate('/alerts')} className="flex items-start gap-2.5 p-2.5 rounded-lg bg-[var(--bg-tertiary)] hover:bg-[var(--bg-elevated)]/50 transition-colors cursor-pointer">
                  <Icon className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: severityColors[alert.severity] }} />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-[var(--text-primary)] truncate">{alert.message}</p>
                    <p className="text-[10px] text-[var(--text-muted)] mt-0.5">{alert.device_hostname || alert.device_ip} &middot; {timeAgo(alert.triggered_at)}</p>
                  </div>
                  <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded" style={{ color: severityColors[alert.severity], backgroundColor: `${severityColors[alert.severity]}15` }}>
                    {alert.severity}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      </div>


      {/* ─── Row 3b: Uptime by Location / Group / Type ─── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
          <div className="flex items-center gap-2 mb-3">
            <MapPin className="w-4 h-4 text-[var(--accent)]" />
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Uptime by Location</h3>
          </div>
          <UptimeByLocationChart devices={devices} deviceUptime={deviceUptime} />
        </div>
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
          <div className="flex items-center gap-2 mb-3">
            <Globe className="w-4 h-4 text-[var(--accent)]" />
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Uptime by Group</h3>
          </div>
          <UptimeByGroupChart devices={devices} groups={groups || []} deviceUptime={deviceUptime} />
        </div>
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
          <div className="flex items-center gap-2 mb-3">
            <Server className="w-4 h-4 text-[var(--accent)]" />
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Uptime by Device Type</h3>
          </div>
          <UptimeByTypeChart devices={devices} deviceUptime={deviceUptime} />
        </div>
      </div>

      {/* ─── Row 4: Device Heatmap + Top Offenders + Group Stats ─── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        {/* Device Heatmap */}
        <div className="lg:col-span-2 rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4 text-[var(--accent)]" />
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Device Status Map</h3>
            </div>
            <button onClick={() => navigate('/devices')} className="text-xs text-[var(--accent)] hover:underline flex items-center gap-0.5">
              View all <ChevronRight size={12} />
            </button>
          </div>
          <div className="flex flex-wrap gap-1.5 mb-3">
            {devices.map(device => (
              <button key={device.id} onClick={() => navigate(`/devices/${device.id}`)}
                className="w-6 h-6 rounded transition-all hover:scale-150 hover:z-10 cursor-pointer relative group"
                style={{ backgroundColor: statusColors[device.status as DeviceStatus] || '#6B7280' }}>
                <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover:block z-20 whitespace-nowrap">
                  <div className="bg-[#1A1D27] border border-[#2D3140] rounded-lg px-3 py-2 text-xs shadow-xl">
                    <div className="font-semibold text-[var(--text-primary)]">{device.hostname}</div>
                    <div className="text-[var(--text-muted)]">{device.ip_address}</div>
                    <div className="flex items-center gap-1 mt-1">
                      <span className="w-1.5 h-1.5 rounded-full" style={{ background: statusColors[device.status as DeviceStatus] }} />
                      <span style={{ color: statusColors[device.status as DeviceStatus] }}>{device.status}</span>
                      {device.last_rtt_ms && <span className="text-[var(--text-muted)]">&middot; {formatRTT(device.last_rtt_ms)}</span>}
                    </div>
                  </div>
                </div>
              </button>
            ))}
          </div>
          <div className="flex items-center gap-4 text-xs text-[var(--text-muted)]">
            {(['up', 'degraded', 'down', 'unknown'] as DeviceStatus[]).map(st => (
              <div key={st} className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded" style={{ background: statusColors[st] }} />
                <span className="capitalize">{st}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Top Offenders + Group Stats */}
        <div className="space-y-4">
          {/* Top Offenders */}
          <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5">
            <div className="flex items-center gap-2 mb-3">
              <Zap className="w-4 h-4 text-amber-400" />
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Attention Needed</h3>
            </div>
            {topOffenders.length === 0 ? (
              <p className="text-xs text-[var(--text-muted)] text-center py-4">All devices healthy</p>
            ) : (
              <div className="space-y-2">
                {topOffenders.map(d => {
                  const Icon = typeIcons[d.device_type] || HelpCircle
                  return (
                    <button key={d.id} onClick={() => navigate(`/devices/${d.id}`)}
                      className="w-full flex items-center gap-2.5 p-2 rounded-lg hover:bg-[var(--bg-tertiary)] transition-colors text-left">
                      <Icon className="w-4 h-4 flex-shrink-0" style={{ color: statusColors[d.status as DeviceStatus] }} />
                      <div className="flex-1 min-w-0">
                        <div className="text-xs font-medium text-[var(--text-primary)] truncate">{d.hostname}</div>
                        <div className="text-[10px] text-[var(--text-muted)]">{d.ip_address}</div>
                      </div>
                      <div className="text-right">
                        <span className="text-xs font-mono" style={{ color: statusColors[d.status as DeviceStatus] }}>
                          {d.status === 'down' ? 'DOWN' : d.last_rtt_ms ? `${d.last_rtt_ms.toFixed(1)}ms` : '--'}
                        </span>
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </div>

          {/* Group Health */}
          {groupStats.length > 0 && (
            <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 hover:border-[var(--accent)]/30 transition-colors cursor-pointer" onClick={() => navigate('/devices')}>
              <div className="flex items-center gap-2 mb-3">
                <Globe className="w-4 h-4 text-[var(--accent)]" />
                <h3 className="text-sm font-semibold text-[var(--text-primary)]">Group Health</h3>
              </div>
              <div className="space-y-2.5">
                {groupStats.map(g => {
                  const pct = g.total > 0 ? (g.up / g.total) * 100 : 0
                  return (
                    <div key={g.name}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs text-[var(--text-secondary)] truncate">{g.name}</span>
                        <span className="text-xs font-mono" style={{ color: pct >= 99 ? '#22C55E' : pct >= 80 ? '#EAB308' : '#EF4444' }}>{pct.toFixed(0)}%</span>
                      </div>
                      <div className="h-1.5 rounded-full bg-[var(--bg-tertiary)] overflow-hidden">
                        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, background: pct >= 99 ? '#22C55E' : pct >= 80 ? '#EAB308' : '#EF4444' }} />
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ─── Row 5: Recent Service Checks Table ─── */}
      {services.length > 0 && (
        <div className="rounded-xl border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] p-5 mb-6">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-[var(--accent)]" />
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">Service Checks Overview</h3>
            </div>
            <button onClick={() => navigate('/service-checks')} className="text-xs text-[var(--accent)] hover:underline flex items-center gap-0.5">
              View all <ChevronRight size={12} />
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-[var(--bg-elevated)]">
                  <th className="text-left py-2.5 px-3 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Name</th>
                  <th className="text-left py-2.5 px-3 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Type</th>
                  <th className="text-left py-2.5 px-3 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Target</th>
                  <th className="text-center py-2.5 px-3 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Status</th>
                  <th className="text-right py-2.5 px-3 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Response</th>
                  <th className="text-right py-2.5 px-3 font-semibold text-[var(--text-muted)] uppercase tracking-wider">Last Check</th>
                </tr>
              </thead>
              <tbody>
                {services.slice(0, 10).map(svc => (
                  <tr key={svc.id} onClick={() => navigate(`/service-checks/${svc.id}`)}
                    className="border-b border-[var(--bg-elevated)]/30 hover:bg-[var(--bg-tertiary)]/50 cursor-pointer transition-colors">
                    <td className="py-2.5 px-3 font-medium text-[var(--text-primary)]">{svc.name}</td>
                    <td className="py-2.5 px-3">
                      <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase" style={{
                        background: svc.check_type === 'http' ? '#6366F120' : svc.check_type === 'tcp' ? '#22C55E20' : '#F59E0B20',
                        color: svc.check_type === 'http' ? '#818CF8' : svc.check_type === 'tcp' ? '#22C55E' : '#F59E0B',
                      }}>{svc.check_type}</span>
                    </td>
                    <td className="py-2.5 px-3 font-mono text-[var(--text-muted)]">{svc.target_url || svc.target_host}</td>
                    <td className="py-2.5 px-3 text-center">
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold" style={{
                        background: `${statusColors[svc.status as DeviceStatus] || '#6B7280'}15`,
                        color: statusColors[svc.status as DeviceStatus] || '#6B7280',
                      }}>
                        <span className="w-1.5 h-1.5 rounded-full" style={{ background: statusColors[svc.status as DeviceStatus] || '#6B7280' }} />
                        {svc.status.toUpperCase()}
                      </span>
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-[var(--text-secondary)]">
                      {svc.last_response_ms ? `${svc.last_response_ms.toFixed(0)}ms` : '--'}
                    </td>
                    <td className="py-2.5 px-3 text-right text-[var(--text-muted)]">{timeAgo(svc.last_check_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
