import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Server,
  XCircle,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { relativeTime } from '@/lib/utils'

type DeviceSummary = {
  total: number
  up: number
  down: number
  degraded: number
  unknown: number
  maintenance: number
}

export function DashboardPage() {
  const { data: summary } = useQuery<DeviceSummary>({
    queryKey: ['devices', 'summary'],
    queryFn: async () => (await api.get('/devices/summary')).data,
    refetchInterval: 15_000,
  })

  const { data: alertsStats } = useQuery<{ active: number; critical: number; warning: number }>({
    queryKey: ['alerts', 'stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  })

  const { data: activeAlerts } = useQuery<any[]>({
    queryKey: ['alerts', 'active'],
    queryFn: async () => {
      const r = (await api.get('/alerts?status=active&limit=5')).data
      return Array.isArray(r) ? r : r?.data || []
    },
    refetchInterval: 15_000,
  })

  const { data: uptimeStats } = useQuery<{ devices: Record<string, number> }>({
    queryKey: ['uptime-24h'],
    queryFn: async () =>
      (await api.get('/devices/dashboard/uptime-stats?hours=24')).data,
    refetchInterval: 60_000,
  })

  const total = summary?.total || 0
  const up = summary?.up || 0
  const down = summary?.down || 0
  const degraded = summary?.degraded || 0
  const availability = total > 0 ? Math.round((up / total) * 1000) / 10 : 0

  // Build a simple uptime histogram from the per-device percentages.
  const uptimeBuckets = (() => {
    const pcts = Object.values(uptimeStats?.devices || {})
    const buckets = [0, 0, 0, 0, 0, 0]
    pcts.forEach((p) => {
      if (p >= 99) buckets[0]++
      else if (p >= 95) buckets[1]++
      else if (p >= 90) buckets[2]++
      else if (p >= 75) buckets[3]++
      else if (p >= 50) buckets[4]++
      else buckets[5]++
    })
    return [
      { name: '≥99%', devices: buckets[0] },
      { name: '95–99%', devices: buckets[1] },
      { name: '90–95%', devices: buckets[2] },
      { name: '75–90%', devices: buckets[3] },
      { name: '50–75%', devices: buckets[4] },
      { name: '<50%', devices: buckets[5] },
    ]
  })()

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted">Network monitoring overview</p>
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          label="Devices"
          value={total}
          icon={<Server className="h-5 w-5" />}
          sub={`${availability}% availability`}
        />
        <Kpi
          label="Online"
          value={up}
          icon={<CheckCircle2 className="h-5 w-5" />}
          accent="success"
        />
        <Kpi
          label="Offline"
          value={down}
          icon={<XCircle className="h-5 w-5" />}
          accent={down > 0 ? 'danger' : undefined}
        />
        <Kpi
          label="Active alerts"
          value={alertsStats?.active ?? 0}
          icon={<AlertTriangle className="h-5 w-5" />}
          accent={(alertsStats?.active ?? 0) > 0 ? 'warning' : undefined}
          sub={degraded > 0 ? `${degraded} degraded` : 'All clear'}
        />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Device uptime distribution — 24h</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={uptimeBuckets} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="grad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.5} />
                      <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgb(var(--border))" strokeDasharray="3 3" />
                  <XAxis dataKey="name" stroke="rgb(var(--muted))" fontSize={12} />
                  <YAxis stroke="rgb(var(--muted))" fontSize={12} allowDecimals={false} />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: 'rgb(var(--surface))',
                      border: '1px solid rgb(var(--border))',
                      borderRadius: 8,
                      color: 'rgb(var(--text))',
                    }}
                  />
                  <Area
                    type="monotone"
                    dataKey="devices"
                    stroke="rgb(var(--primary))"
                    fill="url(#grad)"
                    strokeWidth={2}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent alerts</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {(activeAlerts || []).length === 0 && (
              <p className="text-sm text-muted">No active alerts 🎉</p>
            )}
            {(activeAlerts || []).map((a) => (
              <div
                key={a.id}
                className="flex items-start gap-3 rounded-md border border-border p-3"
              >
                <div className="mt-0.5">
                  <Activity className="h-4 w-4 text-warning" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{a.rule_name || 'Alert'}</div>
                  <div className="truncate text-xs text-muted">{a.message || a.device_hostname}</div>
                </div>
                <Badge variant={severityVariant(a.severity)}>{a.severity || 'info'}</Badge>
              </div>
            ))}
            <Link
              to="/alerts"
              className="block pt-1 text-xs text-primary hover:underline"
            >
              View all alerts →
            </Link>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function Kpi({
  label,
  value,
  icon,
  sub,
  accent,
}: {
  label: string
  value: number | string
  icon: React.ReactNode
  sub?: string
  accent?: 'success' | 'warning' | 'danger'
}) {
  const accentClass =
    accent === 'success'
      ? 'text-success'
      : accent === 'warning'
        ? 'text-warning'
        : accent === 'danger'
          ? 'text-danger'
          : 'text-primary'
  return (
    <Card>
      <CardContent className="p-5">
        <div className="flex items-start justify-between">
          <div>
            <div className="text-xs font-medium uppercase tracking-wider text-muted">
              {label}
            </div>
            <div className="mt-2 text-3xl font-semibold tracking-tight">{value}</div>
            {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
          </div>
          <div className={`rounded-md bg-surface2 p-2 ${accentClass}`}>{icon}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function severityVariant(s: string): any {
  switch (s) {
    case 'critical':
      return 'danger'
    case 'warning':
      return 'warning'
    case 'info':
      return 'info'
    default:
      return 'default'
  }
}
