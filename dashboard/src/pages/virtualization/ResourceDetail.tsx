import { useEffect, useMemo, useState } from 'react'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { relativeTime } from '@/lib/utils'
import {
  ATTRIBUTE_LABEL,
  CHART_ORDER,
  KIND_LABEL,
  clusterOf,
  fmtBytes,
  fmtLatency,
  fmtNumber,
  isOn,
  memoryFigure,
  metricLabel,
  metricValue,
  sortAlerts,
  type Alert,
  type Index,
  type Resource,
} from './model'
import {
  HISTORY_RANGES,
  HealthBadge,
  HistoryChart,
  HistoryState,
  Meter,
  PowerBadge,
  ProviderLink,
  RangeSwitch,
  SeverityBadge,
  useHistory,
  type HistoryRange,
} from './ui'

/** Metrics already shown as meters or headline facts are not repeated in the raw counter grid. */
const HEADLINE = new Set(['cpu_usage_percent', 'memory_usage_percent', 'storage_usage_percent'])

export function ResourceDetail({ connectorId, resource, index, alerts, stale, onOpen, onClose }: {
  connectorId: string
  resource?: Resource
  index: Index
  alerts: Alert[]
  stale: boolean
  onOpen: (id: string) => void
  onClose: () => void
}) {
  const [tab, setTab] = useState('summary')
  const [range, setRange] = useState<HistoryRange>('24h')
  const hours = HISTORY_RANGES.find((r) => r.key === range)!.hours
  const history = useHistory(connectorId, tab === 'performance' ? resource?.id : undefined, range)
  const r = resource
  useEffect(() => setTab('summary'), [r?.id])
  const parent = r?.parent_id ? index.byId.get(r.parent_id) : undefined
  const cluster = r ? clusterOf(r, index) : undefined
  const related = r ? index.children.get(r.id) ?? [] : []
  const resourceAlerts = useMemo(() => sortAlerts(alerts.filter((a) => a.resource_id === r?.id)), [alerts, r?.id])
  const charted = useMemo(() => {
    const present = new Set((history.data ?? []).map((p) => p.metric))
    // A VM whose memory is only hypervisor-reserved has no utilization now; older samples stored the reserved 100%.
    if (r && memoryFigure(r).reserved) present.delete('memory_usage_percent')
    return [...CHART_ORDER.filter((m) => present.has(m)), ...[...present].filter((m) => !CHART_ORDER.includes(m) && m.endsWith('_percent'))]
  }, [history.data, r])

  return (
    <Dialog open={!!r} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-5xl">
        {r && (
          <>
            <DialogHeader className="pr-8">
              <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-wider text-muted">
                {KIND_LABEL[r.kind]?.one ?? r.kind}
                {cluster && cluster.id !== r.id && <span className="normal-case tracking-normal">· {cluster.name}</span>}
                {parent && parent.kind === 'host' && <span className="normal-case tracking-normal">· on {parent.name}</span>}
              </div>
              <DialogTitle className="flex flex-wrap items-center gap-3">
                {r.name}
                <HealthBadge health={r.health} stale={stale} />
                {r.power_state && <PowerBadge power={r.power_state} />}
              </DialogTitle>
              <DialogDescription className="flex flex-wrap gap-x-4 gap-y-1">
                <span className="font-mono text-xs">{r.external_id}</span>
                <ProviderLink url={r.url}>Provider API record</ProviderLink>
              </DialogDescription>
            </DialogHeader>
            <Tabs value={tab} onValueChange={setTab}>
              <TabsList>
                <TabsTrigger value="summary">Summary</TabsTrigger>
                <TabsTrigger value="performance">Performance</TabsTrigger>
                <TabsTrigger value="alerts">Alerts{resourceAlerts.filter((a) => !a.resolved).length ? ` (${resourceAlerts.filter((a) => !a.resolved).length})` : ''}</TabsTrigger>
                {related.length > 0 && <TabsTrigger value="related">{r.kind === 'host' ? 'VMs' : r.kind === 'cluster' ? 'Members' : 'Related'} ({related.length})</TabsTrigger>}
              </TabsList>

              <TabsContent value="summary" className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-3">
                  <Stat label="CPU">
                    <Meter value={r.metrics.cpu_usage_percent} detail={r.metrics.cpu_cores !== undefined ? `${fmtNumber(r.metrics.cpu_cores)} ${r.kind === 'vm' ? 'vCPU' : 'cores'}` : undefined} />
                  </Stat>
                  <Stat label="Memory">
                    {(() => {
                      const m = memoryFigure(r)
                      return m.percent !== undefined ? (
                        <Meter value={m.percent} detail={m.detail} />
                      ) : (
                        <div>
                          <div className="font-medium">{m.detail || '—'}</div>
                          {m.reserved && (
                            <p className="mt-1 text-xs text-muted">
                              The hypervisor reserves the full assignment ({metricValue('hypervisor_memory_usage_percent', r.metrics.hypervisor_memory_usage_percent)}).
                              Install guest tools for in-guest usage.
                            </p>
                          )}
                        </div>
                      )
                    })()}
                  </Stat>
                  <Stat label={r.kind === 'vm' ? 'Disk' : 'Storage'}>
                    {r.metrics.storage_usage_percent !== undefined ? (
                      <Meter value={r.metrics.storage_usage_percent} detail={`${fmtBytes(r.metrics.storage_used_bytes)} of ${fmtBytes(r.metrics.storage_capacity_bytes)}`} />
                    ) : (
                      <div className="font-medium">{r.metrics.disk_provisioned_bytes !== undefined ? `${fmtBytes(r.metrics.disk_provisioned_bytes)} provisioned` : '—'}</div>
                    )}
                  </Stat>
                </div>
                <div className="grid gap-4 lg:grid-cols-2">
                  <section>
                    <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Configuration</h4>
                    <dl className="divide-y divide-border rounded-md border border-border text-sm">
                      {parent && (
                        <Row label={parent.kind === 'host' ? 'Host' : 'Parent'}>
                          <button type="button" className="text-primary hover:underline" onClick={() => onOpen(parent.id)}>{parent.name}</button>
                        </Row>
                      )}
                      {cluster && cluster.id !== r.id && cluster.id !== parent?.id && (
                        <Row label="Cluster">
                          <button type="button" className="text-primary hover:underline" onClick={() => onOpen(cluster.id)}>{cluster.name}</button>
                        </Row>
                      )}
                      {Object.entries(r.attributes ?? {}).map(([k, v]) => (
                        <Row key={k} label={ATTRIBUTE_LABEL[k] ?? k.replaceAll('_', ' ')}>
                          {Array.isArray(v) ? (
                            <span className="flex flex-wrap justify-end gap-1">{v.map((x) => <span key={x} className="rounded bg-surface2 px-1.5 font-mono text-xs">{x}</span>)}</span>
                          ) : k === 'created' ? relativeTime(v) : v}
                        </Row>
                      ))}
                      {!parent && !Object.keys(r.attributes ?? {}).length && <p className="p-3 text-muted">No configuration details reported.</p>}
                    </dl>
                  </section>
                  <section>
                    <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Current counters</h4>
                    <div className="grid grid-cols-2 gap-2">
                      {Object.entries(r.metrics)
                        .filter(([k]) => !HEADLINE.has(k))
                        .map(([k, v]) => (
                          <div key={k} className="rounded-md bg-surface2 px-3 py-2">
                            <div className="truncate text-xs text-muted">{metricLabel(k)}</div>
                            <div className="font-medium tabular-nums">{metricValue(k, v)}</div>
                          </div>
                        ))}
                    </div>
                  </section>
                </div>
              </TabsContent>

              <TabsContent value="performance" className="space-y-3">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs text-muted">Averaged samples from each collection; history is retained for 30 days.</p>
                  <RangeSwitch value={range} onChange={setRange} />
                </div>
                <HistoryState query={history}>
                  {charted.length ? (
                    <div className="grid gap-4 md:grid-cols-2">
                      {charted.map((m) => {
                        const latest = r.metrics[m]
                        return (
                          <div key={m} className="rounded-md border border-border p-3">
                            <div className="mb-1 flex items-baseline justify-between gap-2">
                              <span className="text-sm font-medium">{metricLabel(m)}</span>
                              {latest !== undefined && <span className="text-xs tabular-nums text-muted">now {metricValue(m, latest)}</span>}
                            </div>
                            <HistoryChart data={history.data ?? []} hours={hours} height={170} series={[{ metric: m }]} />
                          </div>
                        )
                      })}
                    </div>
                  ) : (
                    <p className="py-8 text-center text-sm text-muted">No historical samples in this range.</p>
                  )}
                </HistoryState>
              </TabsContent>

              <TabsContent value="alerts">
                {resourceAlerts.length ? (
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {resourceAlerts.map((a) => (
                      <li key={a.id} className={`flex items-start gap-3 p-3 ${a.resolved ? 'opacity-70' : ''}`}>
                        <SeverityBadge severity={a.severity} resolved={a.resolved} />
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-medium">{a.title}</div>
                          {a.message && <div className="text-sm text-text2">{a.message}</div>}
                          <div className="text-xs text-muted">
                            {a.created_at ? `Raised ${relativeTime(a.created_at)}` : ''}
                            {a.acknowledged ? ' · Acknowledged' : ''}
                          </div>
                        </div>
                        <ProviderLink url={a.url}>Open</ProviderLink>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="py-8 text-center text-sm text-muted">No provider alerts for this {KIND_LABEL[r.kind]?.one ?? 'resource'}.</p>
                )}
              </TabsContent>

              {related.length > 0 && (
                <TabsContent value="related">
                  <div className="overflow-x-auto rounded-md border border-border">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-surface2/60 text-2xs uppercase tracking-wider text-muted">
                        <tr>
                          <th className="px-3 py-2">Name</th>
                          <th className="px-3 py-2">Type</th>
                          <th className="px-3 py-2">State</th>
                          <th className="px-3 py-2">CPU</th>
                          <th className="px-3 py-2">Memory</th>
                          <th className="px-3 py-2">IOPS / latency</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...related]
                          .sort((a, b) => a.kind.localeCompare(b.kind) || Number(isOn(b.power_state)) - Number(isOn(a.power_state)) || a.name.localeCompare(b.name))
                          .map((c) => (
                            <tr key={c.id} className="cursor-pointer border-t border-border hover:bg-surface2/60" onClick={() => onOpen(c.id)}>
                              <td className="px-3 py-2 font-medium text-primary">{c.name}</td>
                              <td className="px-3 py-2 text-muted">{KIND_LABEL[c.kind]?.one ?? c.kind}</td>
                              <td className="px-3 py-2">{c.power_state ? <PowerBadge power={c.power_state} /> : <HealthBadge health={c.health} />}</td>
                              <td className="px-3 py-2"><Meter value={c.metrics.cpu_usage_percent} /></td>
                              <td className="px-3 py-2">{(() => { const m = memoryFigure(c); return m.percent !== undefined ? <Meter value={m.percent} /> : <span className="text-xs">{m.detail || '—'}</span> })()}</td>
                              <td className="whitespace-nowrap px-3 py-2 tabular-nums">{fmtNumber(c.metrics.iops)} · {fmtLatency(c.metrics.io_latency_us)}</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                </TabsContent>
              )}
            </Tabs>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border p-3">
      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted">{label}</div>
      {children}
    </div>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 px-3 py-2">
      <dt className="shrink-0 text-muted">{label}</dt>
      <dd className="min-w-0 break-words text-right">{children}</dd>
    </div>
  )
}
