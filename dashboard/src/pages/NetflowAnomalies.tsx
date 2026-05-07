import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ChevronRight, Activity, AlertTriangle, ShieldAlert, ShieldCheck, Zap, Bug } from 'lucide-react'
import { api } from '@/lib/api'
import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'

type Finding = {
  id: string
  kind: string
  severity: 'critical' | 'warning' | 'info'
  title: string
  description: string
  metric?: number
  metric_label?: string
  src?: string
  dst?: string
  flows?: number
  bytes?: number
}

type AnomaliesResponse = { total: number; findings: Finding[]; generated_at: string }

const KIND_ICONS: Record<string, any> = {
  syn_scan: Bug,
  rst_flood: Zap,
  sensitive_egress: ShieldAlert,
  icmp_flood: Activity,
  volumetric_outlier: AlertTriangle,
}

const KIND_LABELS: Record<string, string> = {
  syn_scan: 'TCP SYN scan',
  rst_flood: 'RST flood',
  sensitive_egress: 'Sensitive port egress',
  icmp_flood: 'ICMP flood',
  volumetric_outlier: 'Volumetric outlier',
}

export function NetflowAnomaliesPage() {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const qs = new URLSearchParams({ hours: String(range.hours) })
  if (isCustom) { qs.set('from', range.fromISO); qs.set('to', range.toISO) }

  const data = useQuery<AnomaliesResponse>({
    queryKey: ['netflow', 'anomalies', qs.toString()],
    queryFn: async () => (await api.get(`/netflow/anomalies?${qs.toString()}`)).data,
    refetchInterval: isCustom ? false : 30_000,
  })
  const findings = data.data?.findings || []

  const counts = {
    critical: findings.filter((f) => f.severity === 'critical').length,
    warning: findings.filter((f) => f.severity === 'warning').length,
    info: findings.filter((f) => f.severity === 'info').length,
  }

  // Group findings by kind
  const grouped = new Map<string, Finding[]>()
  for (const f of findings) {
    const arr = grouped.get(f.kind) || []
    arr.push(f)
    grouped.set(f.kind, arr)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted">
            <Link to="/netflow" className="inline-flex items-center gap-1 hover:text-text">
              <ArrowLeft className="h-3 w-3" />
              NetFlow
            </Link>
            <ChevronRight className="h-3 w-3" />
            <span>Anomaly Detection</span>
          </div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <ShieldAlert className="h-5 w-5 text-primary" />
            Anomaly Detection
          </h1>
          <p className="text-xs text-muted">Flow-derived security signals. Algorithms: SYN scan, RST flood, sensitive-port egress, ICMP flood, volumetric outliers.</p>
        </div>
        <TimeRangePicker
          rangeIdx={rangeIdx}
          isCustom={isCustom}
          customFrom={range.fromISO}
          customTo={range.toISO}
          onPreset={setPreset}
          onCustom={setCustom}
        />
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <SevTile label="Critical" value={counts.critical} tone="rose" icon={ShieldAlert} />
        <SevTile label="Warning" value={counts.warning} tone="amber" icon={AlertTriangle} />
        <SevTile label="Info" value={counts.info} tone="cyan" icon={Activity} />
        <SevTile label="Total" value={findings.length} tone={findings.length === 0 ? 'emerald' : 'slate'} icon={findings.length === 0 ? ShieldCheck : ShieldAlert} />
      </div>

      {grouped.size === 0 ? (
        <Card>
          <CardContent className="p-10 text-center">
            <ShieldCheck className="mx-auto h-10 w-10 text-emerald-400" />
            <p className="mt-3 text-sm font-semibold text-emerald-300">No anomalies detected</p>
            <p className="mt-1 text-xs text-muted">All flow-derived security checks passed for this time window.</p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {Array.from(grouped.entries()).map(([kind, items]) => {
            const Icon = KIND_ICONS[kind] || AlertTriangle
            const label = KIND_LABELS[kind] || kind
            return (
              <Card key={kind}>
                <CardHeader className="flex flex-row items-center gap-2 pb-3">
                  <Icon className="h-4 w-4 text-amber-400" />
                  <div>
                    <CardTitle className="text-sm">{label}</CardTitle>
                    <p className="text-[11px] text-muted">{items.length} {items.length === 1 ? 'finding' : 'findings'}</p>
                  </div>
                </CardHeader>
                <CardContent className="space-y-2">
                  {items.map((f) => {
                    const sevTone = f.severity === 'critical'
                      ? 'border-l-rose-500/70 bg-rose-500/5'
                      : f.severity === 'warning'
                      ? 'border-l-amber-400/70 bg-amber-500/5'
                      : 'border-l-cyan-400/70 bg-cyan-500/5'
                    return (
                      <div key={f.id} className={`rounded-md border border-border border-l-4 p-3 ${sevTone}`}>
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <Badge variant={f.severity === 'critical' ? 'danger' : f.severity === 'warning' ? 'warning' : 'info'}>{f.severity}</Badge>
                              <span className="font-mono text-xs">{f.src}</span>
                              {f.dst && (
                                <>
                                  <span className="text-muted">→</span>
                                  <span className="font-mono text-xs">{f.dst}</span>
                                </>
                              )}
                            </div>
                            <p className="mt-1 text-sm">{f.description}</p>
                            <div className="mt-1.5 flex flex-wrap gap-3 text-[11px] text-muted">
                              {f.flows !== undefined && <span>{f.flows.toLocaleString()} flows</span>}
                              {f.bytes !== undefined && <span>{formatBytes(f.bytes)}</span>}
                              {f.metric !== undefined && <span>{f.metric.toLocaleString()} {f.metric_label}</span>}
                            </div>
                          </div>
                          {f.src && (
                            <Link
                              to={`/netflow/forensics?src=${encodeURIComponent(f.src)}`}
                              className="rounded-md border border-border px-2 py-1 text-[11px] text-muted hover:border-primary/40 hover:text-text"
                            >
                              Investigate →
                            </Link>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}

function SevTile({ label, value, tone, icon: Icon }: { label: string; value: number; tone: 'rose' | 'amber' | 'cyan' | 'emerald' | 'slate'; icon: any }) {
  const map = {
    rose: 'border-rose-500/30 bg-rose-500/5 text-rose-300',
    amber: 'border-amber-400/30 bg-amber-500/5 text-amber-300',
    cyan: 'border-cyan-400/30 bg-cyan-500/5 text-cyan-300',
    emerald: 'border-emerald-400/30 bg-emerald-500/5 text-emerald-300',
    slate: 'border-border bg-surface2/30 text-muted',
  }
  return (
    <div className={`rounded-md border p-3 ${map[tone]}`}>
      <div className="flex items-center gap-1.5">
        <Icon className="h-3.5 w-3.5" />
        <span className="text-[10px] uppercase tracking-wider opacity-80">{label}</span>
      </div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
    </div>
  )
}
