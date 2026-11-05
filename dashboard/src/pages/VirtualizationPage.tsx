import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  Boxes,
  Database,
  Layers,
  LayoutDashboard,
  Monitor,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { regionalDate } from '@/lib/regionalTime'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { buildIndex, providers, type Connector } from './virtualization/model'
import { OverviewTab, type TabKey } from './virtualization/OverviewTab'
import { ClustersTab, HostsTab, StorageTab, VmsTab } from './virtualization/InventoryTabs'
import { AlertsTab } from './virtualization/AlertsTab'
import { ResourceDetail } from './virtualization/ResourceDetail'
import { ConnectorDialog } from './virtualization/ConnectorDialog'

const TABS: TabKey[] = ['overview', 'vms', 'hosts', 'clusters', 'storage', 'alerts']

function connectorState(c: Connector): { label: string; tone: 'success' | 'warning' | 'danger' | 'outline' } {
  if (!c.enabled) return { label: 'Paused', tone: 'outline' }
  if (c.status === 'error') return { label: c.stale ? 'Collection failing' : 'Last poll failed', tone: 'danger' }
  if (c.stale) return { label: 'Stale', tone: 'warning' }
  if (c.status === 'partial') return { label: 'Partial coverage', tone: 'warning' }
  if (c.status === 'ok') return { label: 'Collecting', tone: 'success' }
  return { label: 'Pending first poll', tone: 'outline' }
}

export function VirtualizationPage() {
  const canManage = useCan()('system.admin')
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Connector | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [remove, setRemove] = useState(false)

  const list = useQuery({
    queryKey: ['virtualization'],
    queryFn: () => api.get<Connector[]>('/virtualization/connectors').then((r) => r.data),
    refetchInterval: 15000,
  })
  const requested = params.get('c') || ''
  const selectedId = list.data?.some((c) => c.id === requested) ? requested : list.data?.[0]?.id || ''
  const tabParam = params.get('tab') as TabKey | null
  const tab: TabKey = tabParam && TABS.includes(tabParam) ? tabParam : 'overview'
  const resourceId = params.get('r') || ''

  const detail = useQuery({
    queryKey: ['virtualization', selectedId],
    queryFn: () => api.get<Connector>(`/virtualization/connectors/${selectedId}`).then((r) => r.data),
    enabled: !!selectedId,
    refetchInterval: 15000,
  })
  const current = detail.data?.id === selectedId ? detail.data : undefined
  const resources = useMemo(() => current?.resources ?? [], [current])
  const alerts = useMemo(() => current?.alerts ?? [], [current])
  const index = useMemo(() => buildIndex(resources, alerts), [resources, alerts])
  const byKind = useMemo(() => {
    const out: Record<string, typeof resources> = { cluster: [], host: [], vm: [], storage: [] }
    for (const r of resources) (out[r.kind] ??= []).push(r)
    return out
  }, [resources])
  const activeAlerts = alerts.filter((a) => !a.resolved)
  const criticalAlerts = activeAlerts.filter((a) => a.severity === 'critical' || a.severity === 'error').length

  function update(next: Record<string, string | null>, replace = false) {
    const p = new URLSearchParams(params)
    for (const [k, v] of Object.entries(next)) {
      if (v) p.set(k, v)
      else p.delete(k)
    }
    setParams(p, { replace })
  }
  const setTab = (t: string) => update({ tab: t === 'overview' ? null : t })
  const openResource = (id: string) => update({ r: id })
  const closeResource = () => update({ r: null })
  const refresh = () => qc.invalidateQueries({ queryKey: ['virtualization'] })

  async function act(label: string, action: () => Promise<unknown>) {
    setBusy(label)
    setError('')
    try {
      await action()
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy('')
    }
  }

  const state = current ? connectorState(current) : undefined
  const tabMeta: { key: TabKey; label: string; icon: React.ReactNode; count?: number; alert?: boolean }[] = [
    { key: 'overview', label: 'Overview', icon: <LayoutDashboard className="h-4 w-4" /> },
    { key: 'vms', label: 'Virtual machines', icon: <Monitor className="h-4 w-4" />, count: byKind.vm.length },
    { key: 'hosts', label: 'Hosts', icon: <Server className="h-4 w-4" />, count: byKind.host.length },
    { key: 'clusters', label: 'Clusters', icon: <Boxes className="h-4 w-4" />, count: byKind.cluster.length },
    { key: 'storage', label: 'Storage', icon: <Database className="h-4 w-4" />, count: byKind.storage.length },
    { key: 'alerts', label: 'Alerts', icon: <AlertTriangle className="h-4 w-4" />, count: activeAlerts.length, alert: criticalAlerts > 0 },
  ]

  return (
    <div className="space-y-5 p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Link to="/servers/inventory" className="mb-1 inline-block text-xs text-muted hover:text-primary">All Servers</Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Layers className="h-6 w-6" />
            Virtualization
          </h1>
          <p className="mt-1 text-sm text-muted">Clusters, hypervisors, virtual machines and storage from authenticated provider APIs.</p>
        </div>
        {canManage && (
          <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
            <Plus />
            Add connector
          </Button>
        )}
      </div>

      {(error || list.error || detail.error) && (
        <div role="alert" className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
          {error || apiErrorMessage(list.error || detail.error)}
        </div>
      )}
      {list.isLoading && <div className="h-40 animate-pulse rounded-lg bg-surface2" />}
      {!list.isLoading && !list.error && !list.data?.length && (
        <Card className="p-10 text-center">
          <ShieldCheck className="mx-auto mb-3 h-10 w-10 text-primary" />
          <h2 className="font-semibold">Connect your virtualization platform</h2>
          <p className="mt-2 text-sm text-muted">
            Add Prism Central, Prism Element, VMware or Hyper-V. Test API access and certificate trust before saving.
          </p>
        </Card>
      )}

      {!!list.data?.length && (
        <>
          {list.data.length > 1 && (
            <div className="flex flex-wrap gap-2" role="tablist" aria-label="Connectors">
              {list.data.map((c) => {
                const s = connectorState(c)
                return (
                  <button
                    key={c.id}
                    type="button"
                    role="tab"
                    aria-selected={selectedId === c.id}
                    onClick={() => update({ c: c.id, r: null })}
                    className={cn(
                      'flex items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition',
                      selectedId === c.id ? 'border-primary bg-primary/5' : 'border-border bg-surface hover:border-primary/40',
                    )}
                  >
                    <span className={cn('h-2 w-2 rounded-full', s.tone === 'success' ? 'bg-success' : s.tone === 'danger' ? 'bg-danger' : s.tone === 'warning' ? 'bg-warning' : 'bg-border-strong')} />
                    <span className="font-medium">{c.name}</span>
                    <span className="text-xs text-muted">{providers[c.provider]}</span>
                  </button>
                )
              })}
            </div>
          )}

          {!current && detail.isLoading && <div className="h-64 animate-pulse rounded-lg bg-surface2" />}

          {current && state && (
            <>
              <Card className="p-4">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-lg font-semibold">{current.name}</h2>
                      <Badge variant={state.tone}>{state.label}</Badge>
                      <span className="text-xs text-muted">{providers[current.provider]}</span>
                    </div>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
                      <span className="font-mono">{current.endpoint}</span>
                      <span>{current.summary.version || 'Awaiting first collection'}</span>
                      <span title={current.last_success_at ? regionalDate(new Date(current.last_success_at), 'datetime') : undefined}>
                        Last collected {current.last_success_at ? relativeTime(current.last_success_at) : 'never'}
                      </span>
                      <span>Every {Math.round(current.interval_seconds / 60)} min</span>
                      {current.tags.length > 0 && <span>Tags: {current.tags.join(', ')}</span>}
                    </div>
                  </div>
                  {canManage && (
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!!busy || !current.enabled}
                        onClick={() => act('poll', async () => { await api.post(`/virtualization/connectors/${current.id}/poll`); await refresh() })}
                      >
                        <RefreshCw className={cn(busy === 'poll' && 'animate-spin')} />
                        Poll now
                      </Button>
                      <Button variant="outline" size="sm" disabled={!!busy} onClick={() => { setEditing(current); setFormOpen(true) }}>
                        <Pencil />
                        Edit
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!!busy}
                        onClick={() => act('toggle', async () => { await api.patch(`/virtualization/connectors/${current.id}`, { enabled: !current.enabled }); await refresh() })}
                      >
                        {current.enabled ? <Pause /> : <Play />}
                        {current.enabled ? 'Pause' : 'Resume'}
                      </Button>
                      <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => setRemove(true)}>
                        <Trash2 />
                        Delete
                      </Button>
                    </div>
                  )}
                </div>
                {current.stale && (
                  <p role="status" className="mt-3 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning-fg">
                    Data is stale or unavailable. Values shown are from the last successful collection
                    {current.last_success_at ? ` (${regionalDate(new Date(current.last_success_at), 'datetime')})` : ''}.
                  </p>
                )}
                {current.last_error && <p className="mt-3 text-sm text-danger">{current.last_error}</p>}
                {!!current.summary.warnings?.length && (
                  <details className="mt-3">
                    <summary className="cursor-pointer text-sm text-warning-fg">Coverage notes ({current.summary.warnings.length})</summary>
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted">
                      {current.summary.warnings.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                  </details>
                )}
              </Card>

              <Tabs value={tab} onValueChange={setTab}>
                <div className="overflow-x-auto">
                  <TabsList className="h-auto flex-wrap">
                    {tabMeta.map((t) => (
                      <TabsTrigger key={t.key} value={t.key} className="gap-1.5">
                        {t.icon}
                        {t.label}
                        {t.count !== undefined && (
                          <span className={cn(
                            'ml-0.5 rounded-full px-1.5 text-2xs font-semibold tabular-nums',
                            t.alert ? 'bg-danger/15 text-danger-fg' : 'bg-surface3 text-text2',
                          )}>
                            {t.count}
                          </span>
                        )}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                </div>
                <TabsContent value="overview">
                  <OverviewTab connector={current} resources={resources} alerts={alerts} index={index} onOpen={openResource} onTab={setTab} />
                </TabsContent>
                <TabsContent value="vms">
                  <VmsTab rows={byKind.vm} index={index} stale={current.stale} onOpen={openResource} />
                </TabsContent>
                <TabsContent value="hosts">
                  <HostsTab rows={byKind.host} index={index} stale={current.stale} onOpen={openResource} />
                </TabsContent>
                <TabsContent value="clusters">
                  <ClustersTab rows={byKind.cluster} index={index} stale={current.stale} onOpen={openResource} connectorId={current.id} />
                </TabsContent>
                <TabsContent value="storage">
                  <StorageTab rows={byKind.storage} index={index} stale={current.stale} onOpen={openResource} provider={current.provider} />
                </TabsContent>
                <TabsContent value="alerts">
                  <AlertsTab
                    alerts={alerts}
                    index={index}
                    onOpen={openResource}
                    provider={current.provider}
                    alertsSupported={!!current.summary.capabilities?.alerts}
                  />
                </TabsContent>
              </Tabs>

              <ResourceDetail
                connectorId={current.id}
                resource={resourceId ? index.byId.get(resourceId) : undefined}
                index={index}
                alerts={alerts}
                stale={current.stale}
                onOpen={openResource}
                onClose={closeResource}
              />
            </>
          )}
        </>
      )}

      <ConnectorDialog
        open={formOpen}
        editing={editing}
        onClose={() => setFormOpen(false)}
        onSaved={async (id) => {
          setFormOpen(false)
          update({ c: id, r: null })
          await refresh()
        }}
      />
      <ConfirmDialog
        open={remove}
        onOpenChange={setRemove}
        title="Delete virtualization connector?"
        description="Stops collection and deletes its stored credential and current inventory. Metric history expires after 30 days."
        destructive
        loading={!!busy}
        onConfirm={() =>
          act('delete', async () => {
            await api.delete(`/virtualization/connectors/${selectedId}`)
            setRemove(false)
            update({ c: null, r: null, tab: null })
            await refresh()
          })
        }
      />
    </div>
  )
}
