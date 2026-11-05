import { useMemo, useState } from 'react'
import { BellOff } from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { regionalDate } from '@/lib/regionalTime'
import { relativeTime } from '@/lib/utils'
import { KIND_LABEL, sortAlerts, type Alert, type Index } from './model'
import { ProviderLink, SeverityBadge, selectStyle } from './ui'

function sourceLabel(a: Alert, provider: string): string {
  const name = a.source_name || (provider === 'nutanix_pc' ? 'Prism Central' : 'Provider')
  const type = a.source_type
  return type && !name.toLowerCase().includes(type.toLowerCase()) ? `${type}: ${name}` : name
}

export function AlertsTab({ alerts, index, onOpen, alertsSupported, provider }: {
  alerts: Alert[]
  index: Index
  onOpen: (id: string) => void
  alertsSupported: boolean
  provider: string
}) {
  const [status, setStatus] = useState<'active' | 'resolved' | 'all'>('active')
  const [severity, setSeverity] = useState('all')
  const [q, setQ] = useState('')
  const counts = useMemo(() => {
    const active = alerts.filter((a) => !a.resolved)
    return {
      active: active.length,
      resolved: alerts.length - active.length,
      critical: active.filter((a) => a.severity === 'critical' || a.severity === 'error').length,
      warning: active.filter((a) => a.severity === 'warning').length,
      info: active.filter((a) => a.severity === 'info').length,
    }
  }, [alerts])
  const visible = sortAlerts(alerts).filter((a) => {
    if (status === 'active' && a.resolved) return false
    if (status === 'resolved' && !a.resolved) return false
    if (severity !== 'all' && a.severity !== severity) return false
    const resource = a.resource_id ? index.byId.get(a.resource_id) : undefined
    const hay = `${a.title} ${a.message ?? ''} ${resource?.name ?? ''} ${a.source_name ?? ''}`.toLowerCase()
    return hay.includes(q.trim().toLowerCase())
  })
  const consoleName = provider.startsWith('nutanix') ? 'Prism' : provider === 'vmware' ? 'vSphere' : 'Hyper-V'
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {([
          ['Critical', counts.critical, 'critical', 'border-danger/40 text-danger-fg'],
          ['Warning', counts.warning, 'warning', 'border-warning/40 text-warning-fg'],
          ['Info', counts.info, 'info', 'border-info/40 text-info-fg'],
        ] as const).map(([label, n, key, cls]) => (
          <button
            key={key}
            type="button"
            aria-pressed={severity === key}
            onClick={() => { setSeverity(severity === key ? 'all' : key); setStatus('active') }}
            className={`rounded-lg border bg-surface px-4 py-2 text-left transition ${severity === key ? cls + ' ring-1 ring-current' : 'border-border hover:border-primary/40'}`}
          >
            <div className="text-2xs font-semibold uppercase tracking-wider text-muted">{label}</div>
            <div className="text-xl font-semibold tabular-nums">{n}</div>
          </button>
        ))}
        <div className="rounded-lg border border-border bg-surface px-4 py-2">
          <div className="text-2xs font-semibold uppercase tracking-wider text-muted">Resolved (retained)</div>
          <div className="text-xl font-semibold tabular-nums text-text2">{counts.resolved}</div>
        </div>
      </div>
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-3 p-4">
          <Input className="max-w-sm" aria-label="Search alerts" placeholder="Search alert, resource or source" value={q} onChange={(e) => setQ(e.target.value)} />
          <select aria-label="Alert status" className={selectStyle} value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
            <option value="active">Active ({counts.active})</option>
            <option value="resolved">Resolved ({counts.resolved})</option>
            <option value="all">All ({alerts.length})</option>
          </select>
          <select aria-label="Severity" className={selectStyle} value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="all">All severities</option>
            <option value="critical">Critical</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
          <span className="ml-auto text-xs text-muted">
            Read-only. Acknowledge and resolve in {consoleName}.{provider === 'hyperv' && ' Hyper-V events cover the latest 24 hours.'}
          </span>
        </div>
        {!alertsSupported && !alerts.length ? (
          <p className="p-8 text-center text-sm text-muted">Alert coverage is not available for this connector yet.</p>
        ) : !visible.length ? (
          <div className="p-10 text-center text-sm text-muted">
            <BellOff className="mx-auto mb-2 h-6 w-6" />
            {status === 'active' && !q && severity === 'all' ? 'No active provider alerts.' : 'No alerts match these filters.'}
          </div>
        ) : (
          <ul className="divide-y divide-border border-t border-border">
            {visible.map((a) => {
              const resource = a.resource_id ? index.byId.get(a.resource_id) : undefined
              return (
                <li key={a.id} className={`grid gap-3 p-4 md:grid-cols-[6.5rem_1fr_auto] ${a.resolved ? 'opacity-70' : ''}`}>
                  <div className="flex flex-wrap items-start gap-1">
                    <SeverityBadge severity={a.severity} resolved={a.resolved} />
                  </div>
                  <div className="min-w-0">
                    <div className="font-medium">{a.title}</div>
                    {a.message && <p className="mt-0.5 text-sm text-text2">{a.message}</p>}
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                      {resource ? (
                        <button type="button" className="text-primary hover:underline" onClick={() => onOpen(resource.id)}>
                          {KIND_LABEL[resource.kind]?.one ?? resource.kind}: {resource.name}
                        </button>
                      ) : (
                        <span>{sourceLabel(a, provider)}</span>
                      )}
                      {a.created_at && (
                        <span title={regionalDate(new Date(a.created_at), 'datetime')}>Raised {relativeTime(a.created_at)}</span>
                      )}
                      {a.resolved && a.resolved_at && <span>Resolved {relativeTime(a.resolved_at)}</span>}
                      {a.impact && <span>Impact: {a.impact}</span>}
                      {a.acknowledged && <span className="rounded bg-surface2 px-1.5">Acknowledged</span>}
                    </div>
                  </div>
                  <div className="flex items-start gap-3 text-sm">
                    <ProviderLink url={a.url}>Open</ProviderLink>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </Card>
    </div>
  )
}
