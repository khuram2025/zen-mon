/** Server detail: one monitored server.
 *
 *  Tabs: Overview · Performance · Processes · Services · System · Events ·
 *  Alerts · Compliance · Agent. The header (identity, health, live tiles)
 *  stays above every tab; the tab bar sticks while scrolling and carries a
 *  count wherever something needs attention. */

import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CloudOff } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { DownloadAgentDialog } from '@/components/servers/DownloadAgentDialog'
import { ServerFormDialog } from '@/components/servers/ServerFormDialog'
import {
  TABS, type TabKey, useCompliance, useFilesystems, useLiveMetrics, useOpenAlertCount, useProcesses,
  useRecentDetail, useServer, useServerAgent, useServices, useTabState,
} from './data'
import { ServerHeader } from './ServerHeader'
import { CountBadge, type Tone } from './ui'
import { OverviewTab } from './tabs/OverviewTab'
import { PerformanceTab } from './tabs/PerformanceTab'
import { ProcessesTab } from './tabs/ProcessesTab'
import { ServicesTab } from './tabs/ServicesTab'
import { SystemTab } from './tabs/SystemTab'
import { EventsTab } from './tabs/EventsTab'
import { AlertsTab } from './tabs/AlertsTab'
import { ComplianceTab } from './tabs/ComplianceTab'
import { AgentTab } from './tabs/AgentTab'

function TabNav({ tab, go, badges }: {
  tab: TabKey
  go: (t: TabKey) => void
  badges: Partial<Record<TabKey, { count: number; tone: Tone }>>
}) {
  return (
    <nav
      role="tablist"
      aria-label="Server sections"
      className="sticky top-0 z-20 -mx-1 flex gap-0.5 overflow-x-auto border-b border-border bg-bg/90 px-1 backdrop-blur supports-[backdrop-filter]:bg-bg/75"
    >
      {TABS.map((t) => {
        const active = tab === t.key
        const badge = badges[t.key]
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => go(t.key)}
            className={cn(
              'relative inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap px-3.5 py-3 text-sm font-medium transition-colors',
              active ? 'text-text' : 'text-muted hover:text-text',
            )}
          >
            <t.icon className={cn('h-4 w-4', active ? 'text-primary' : '')} />
            {t.label}
            {badge && <CountBadge count={badge.count} tone={badge.tone} />}
            <span
              className={cn(
                'absolute inset-x-2 -bottom-px h-0.5 rounded-full transition-colors',
                active ? 'bg-primary' : 'bg-transparent',
              )}
            />
          </button>
        )
      })}
    </nav>
  )
}

export function ServerDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { tab, sub, go, setSub } = useTabState()

  const [deployOpen, setDeployOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [confirm, setConfirm] = useState<'delete' | 'decommission' | null>(null)

  const { data: server, isLoading, isError, error } = useServer(id)
  const live = useLiveMetrics(id)
  const { data: recent } = useRecentDetail(id)
  const { data: processes } = useProcesses(id)
  const { data: services } = useServices(id)
  const { data: filesystems } = useFilesystems(id)
  const { data: compliance } = useCompliance(id)
  const { data: agent } = useServerAgent(id, Boolean(server?.agent_id))
  const openAlerts = useOpenAlertCount(id)

  const del = useMutation({
    mutationFn: async () => (await api.delete(`/servers/${id}`)).data,
    onSuccess: () => {
      toast.success('Server deleted')
      navigate('/servers')
    },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })
  const decommission = useMutation({
    mutationFn: async () => (await api.post(`/servers/${id}/decommission`)).data,
    onSuccess: () => {
      toast.success('Server decommissioned')
      qc.invalidateQueries({ queryKey: ['servers'] })
    },
    onError: (e) => toast.error('Decommission failed', apiErrorMessage(e)),
  })

  if (isError) {
    return (
      <div className="flex flex-col items-center gap-3 py-24 text-center">
        <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface2 text-muted">
          <CloudOff className="h-6 w-6" />
        </span>
        <div className="text-sm font-medium">This server could not be loaded</div>
        <div className="max-w-sm text-xs text-muted">{apiErrorMessage(error)}</div>
        <Button variant="outline" size="sm" onClick={() => navigate('/servers/inventory')}>
          Back to servers
        </Button>
      </div>
    )
  }

  if (isLoading || !server) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-32 w-full rounded-xl" />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)}
        </div>
        <Skeleton className="h-11 w-full" />
        <Skeleton className="h-80 w-full rounded-xl" />
      </div>
    )
  }

  const fsItems = filesystems?.items ?? []
  const svcItems = services?.items ?? []
  const procItems = processes?.items ?? []
  const autoStopped = svcItems.filter((s) => {
    const st = (s.state || '').toLowerCase()
    return /auto|enabled/i.test(s.start_mode || '') && st !== '' && st !== 'running' && st !== 'not_found'
  }).length
  const fullVolumes = fsItems.filter((f) => (f.used_pct || 0) >= 90).length
  const watchedMissing = procItems.filter((p) => p.running === false && p.watchlisted).length
  const complianceFailures = compliance?.summary
    ? compliance.summary.missing + compliance.summary.outdated + compliance.summary.prohibited
    : 0
  const badges: Partial<Record<TabKey, { count: number; tone: Tone }>> = {
    processes: { count: watchedMissing, tone: 'warning' },
    services: { count: autoStopped, tone: 'warning' },
    system: { count: fullVolumes, tone: 'warning' },
    alerts: { count: openAlerts, tone: 'danger' },
    compliance: { count: complianceFailures, tone: 'warning' },
  }

  return (
    <div className="space-y-4 pb-10">
      <ServerHeader
        server={server}
        live={live}
        detail={recent}
        agent={agent}
        processes={processes ? procItems : undefined}
        services={services ? svcItems : undefined}
        filesystems={fsItems}
        activeTab={tab}
        go={go}
        onEdit={() => setEditOpen(true)}
        onDownloadAgent={() => setDeployOpen(true)}
        onDecommission={() => setConfirm('decommission')}
        onDelete={() => setConfirm('delete')}
      />

      <TabNav tab={tab} go={(t) => go(t)} badges={badges} />

      <div key={tab} className="animate-fade-in">
        {tab === 'overview' && (
          <OverviewTab
            server={server}
            agent={agent}
            recent={recent}
            processes={procItems}
            filesystems={fsItems}
            openAlerts={openAlerts}
            complianceFailures={complianceFailures}
            autoStoppedServices={autoStopped}
            watchedMissing={watchedMissing}
            go={go}
          />
        )}
        {tab === 'performance' && <PerformanceTab server={server} />}
        {tab === 'processes' && <ProcessesTab server={server} />}
        {tab === 'services' && <ServicesTab server={server} />}
        {tab === 'system' && <SystemTab server={server} sub={sub} setSub={setSub} />}
        {tab === 'events' && <EventsTab server={server} />}
        {tab === 'alerts' && <AlertsTab server={server} />}
        {tab === 'compliance' && <ComplianceTab server={server} />}
        {tab === 'agent' && (
          <AgentTab
            server={server}
            sub={sub}
            setSub={setSub}
            onEdit={() => setEditOpen(true)}
            onDecommission={() => setConfirm('decommission')}
            onDelete={() => setConfirm('delete')}
            onDownloadAgent={() => setDeployOpen(true)}
          />
        )}
      </div>

      <DownloadAgentDialog open={deployOpen} onOpenChange={setDeployOpen} />
      <ServerFormDialog open={editOpen} onOpenChange={setEditOpen} server={server} />
      <ConfirmDialog
        open={Boolean(confirm)}
        onOpenChange={(o) => { if (!o) setConfirm(null) }}
        title={confirm === 'delete' ? 'Delete server' : 'Decommission server'}
        description={
          confirm === 'delete'
            ? `Permanently remove ${server.display_name}, its inventory and alert history? This cannot be undone.`
            : `Stop monitoring and alerting for ${server.display_name}? The record is kept and can be re-enabled by editing the server.`
        }
        confirmText={confirm === 'delete' ? 'Delete' : 'Decommission'}
        destructive={confirm === 'delete'}
        onConfirm={() => {
          if (confirm === 'delete') del.mutate()
          else decommission.mutate()
          setConfirm(null)
        }}
      />
    </div>
  )
}
