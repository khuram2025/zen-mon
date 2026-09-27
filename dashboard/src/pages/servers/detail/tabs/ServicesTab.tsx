/** Services tab — service inventory, start-mode aware problem detection, and
 *  per-service "alert when stopped" rules. */

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowDownWideNarrow, Ban, Bell, BellOff, CirclePause, CirclePlay, Search, Wrench, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import {
  ExportCsvButton, QueryError, TablePager, cmp, sortIndicator, sortableTh, usePagedRows,
} from '@/components/servers/tables'
import type { ServerItem, ServerService } from '@/types/servers'
import { useServices } from '../data'
import { CreateHostRuleDialog } from './AlertsTab'
import { EmptyState, Panel, Pill, StatTile, type Tone } from '../ui'

/* ── Helpers ─────────────────────────────────────────────────────── */

interface HostRule {
  id: string
  name: string
  enabled: boolean
  metric: string
  operator: string
  threshold: number | null
  severity: string
  min_duration: number
  server_id: string | null
  target: string | null
}

type Filter = 'all' | 'auto_stopped' | 'running' | 'stopped' | 'disabled' | 'not_found' | 'alert_enabled'
type SortKey = 'attention' | 'name' | 'service' | 'state' | 'start_mode' | 'pid' | 'updated'

const FILTERS: { value: Filter; label: string }[] = [
  { value: 'all', label: 'All states' },
  { value: 'auto_stopped', label: 'Auto-start, stopped' },
  { value: 'running', label: 'Running' },
  { value: 'stopped', label: 'Any stopped / failed' },
  { value: 'disabled', label: 'Disabled' },
  { value: 'not_found', label: 'Not installed' },
  { value: 'alert_enabled', label: 'Alert enabled' },
]

const SERVICES_CSV = [
  { header: 'Service', value: (s: ServerService) => s.service_name },
  { header: 'Display name', value: (s: ServerService) => s.display_name },
  { header: 'State', value: (s: ServerService) => s.state },
  { header: 'Start mode', value: (s: ServerService) => s.start_mode },
  { header: 'PID', value: (s: ServerService) => s.pid },
  { header: 'Updated', value: (s: ServerService) => s.updated_at },
]

function telemetryEmptyMessage(server: ServerItem): string {
  if (server.agent_status === 'online') {
    return 'The agent is online but is not reporting this data. Check that server monitoring is enabled in its monitoring profile.'
  }
  if (server.agent_id) return 'No recent telemetry from the agent.'
  return 'Install and authorize an agent to collect this data.'
}

const stateOf = (s: ServerService) => (s.state || '').toLowerCase()
const isRunning = (s: ServerService) => stateOf(s) === 'running'
const isStopped = (s: ServerService) => {
  const st = stateOf(s)
  return st !== 'running' && st !== 'not_found' && !!st
}
// Windows reports "auto"/"manual"/"disabled"; systemd-style agents report
// "enabled"/"disabled". Only an auto-start service that is not running is
// a problem — a stopped manual service is the normal idle state.
const isAutoStart = (s: ServerService) => /auto|enabled/i.test(s.start_mode || '')
const isDisabled = (s: ServerService) => /^disabled$/i.test((s.start_mode || '').trim())

const titleCase = (v: string | null) =>
  v ? v.charAt(0).toUpperCase() + v.slice(1).replace(/_/g, ' ') : '—'

function stateTone(s: ServerService): Tone {
  const st = stateOf(s)
  if (st === 'running') return 'success'
  if (st === 'not_found') return 'danger'
  if (!st) return 'muted'
  // Colour only where it means something: a stopped auto-start service.
  return isAutoStart(s) ? 'warning' : 'muted'
}

function stateLabel(s: ServerService): string {
  const st = stateOf(s)
  if (st === 'not_found') return 'Not installed'
  if (!st) return 'Unknown'
  return titleCase(s.state)
}

/* ── Tab ─────────────────────────────────────────────────────────── */

export function ServicesTab({ server }: { server: ServerItem }) {
  const serverId = server.id
  const qc = useQueryClient()
  const { data, isLoading, isError, error, refetch } = useServices(serverId)
  const items = useMemo(() => data?.items ?? [], [data])

  const [filter, setFilter] = useState<Filter>('all')
  const [q, setQ] = useState('')
  const [sortBy, setSortBy] = useState<SortKey>('attention')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [alertFor, setAlertFor] = useState<string | null>(null)
  const [removing, setRemoving] = useState<{ rule: HostRule; label: string } | null>(null)

  const { data: rulesResp } = useQuery<{ items: HostRule[] }>({
    queryKey: ['servers', serverId, 'host-rules'],
    queryFn: async () => (await api.get('/host-alert-rules', { params: { server_id: serverId } })).data,
  })
  const serviceRules = useMemo(
    () => (rulesResp?.items || []).filter((r) => r.metric === 'host_service_down' && r.enabled && r.target),
    [rulesResp],
  )
  const ruleByService = useMemo(() => {
    const map = new Map<string, HostRule>()
    for (const r of serviceRules) if (r.target) map.set(r.target.toLowerCase(), r)
    return map
  }, [serviceRules])

  const delRule = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/host-alert-rules/${id}`)).data,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'host-rules'] })
      toast.success('Alert rule removed')
      setRemoving(null)
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const hasRule = (s: ServerService) => ruleByService.has(s.service_name.toLowerCase())
  // 0 = alert rule breached, 1 = auto-start stopped, 2 = everything else.
  const attention = (s: ServerService) => (isStopped(s) ? (hasRule(s) ? 0 : isAutoStart(s) ? 1 : 2) : 2)

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const base = items.filter((s) => {
      if (needle && !(
        s.service_name.toLowerCase().includes(needle) ||
        (s.display_name || '').toLowerCase().includes(needle)
      )) return false
      switch (filter) {
        case 'running': return isRunning(s)
        case 'auto_stopped': return isAutoStart(s) && isStopped(s)
        case 'stopped': return isStopped(s)
        case 'disabled': return isDisabled(s)
        case 'not_found': return stateOf(s) === 'not_found'
        case 'alert_enabled': return hasRule(s)
        default: return true
      }
    })
    const dir = sortDir === 'asc' ? 1 : -1
    const label = (s: ServerService) => s.display_name || s.service_name
    return [...base].sort((a, b) => {
      switch (sortBy) {
        case 'attention': {
          const d = attention(a) - attention(b)
          return d !== 0 ? d : label(a).localeCompare(label(b))
        }
        case 'name': return dir * label(a).localeCompare(label(b))
        case 'service': return dir * a.service_name.localeCompare(b.service_name)
        case 'state': return dir * cmp(a.state, b.state) || label(a).localeCompare(label(b))
        case 'pid': return dir * ((a.pid || 0) - (b.pid || 0)) || label(a).localeCompare(label(b))
        case 'updated': return dir * cmp(a.updated_at, b.updated_at)
        default: return dir * cmp(a.start_mode, b.start_mode) || label(a).localeCompare(label(b))
      }
    })
  }, [items, filter, q, sortBy, sortDir, ruleByService])

  const pager = usePagedRows(filtered, 50)

  const toggleSort = (col: SortKey) => {
    if (sortBy === col) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortBy(col); setSortDir('asc') }
    pager.reset()
  }
  const applyFilter = (f: Filter) => { setFilter(f); pager.reset() }
  const toggleFilter = (f: Filter) => applyFilter(filter === f ? 'all' : f)

  // Inventory rows persist after an agent stops reporting; say so rather
  // than presenting a dead host's last snapshot as current state.
  const stale = items.some((s) => s.is_stale)
  const running = items.filter(isRunning).length
  const autoStopped = items.filter((s) => isAutoStart(s) && isStopped(s)).length
  const disabled = items.filter(isDisabled).length
  const alertBreaches = items.filter((s) => hasRule(s) && isStopped(s)).length
  const newest = items.reduce<string | null>((m, s) => (s.updated_at && (!m || s.updated_at > m) ? s.updated_at : m), null)

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          {[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-[92px] rounded-xl" />)}
        </div>
        <Skeleton className="h-[480px] rounded-xl" />
      </div>
    )
  }

  if (isError) {
    return (
      <Panel title="Services" icon={Wrench}>
        <QueryError error={error} onRetry={() => refetch()} />
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      {/* Summary — tiles double as quick filters. */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <StatTile
          icon={CirclePlay}
          label="Running"
          value={running}
          tone={running > 0 ? 'success' : 'default'}
          pct={items.length ? (running / items.length) * 100 : undefined}
          sub={items.length ? `of ${items.length} services` : 'Not reported'}
          onClick={items.length ? () => toggleFilter('running') : undefined}
          active={filter === 'running'}
        />
        <StatTile
          icon={CirclePause}
          label="Stopped · auto-start"
          value={autoStopped}
          tone={autoStopped > 0 ? 'warning' : 'default'}
          sub={autoStopped > 0 ? 'Should be running' : 'All auto-start services up'}
          onClick={items.length ? () => toggleFilter('auto_stopped') : undefined}
          active={filter === 'auto_stopped'}
        />
        <StatTile
          icon={Ban}
          label="Disabled"
          value={disabled}
          tone={disabled > 0 ? 'muted' : 'default'}
          sub="Start mode disabled"
          onClick={items.length ? () => toggleFilter('disabled') : undefined}
          active={filter === 'disabled'}
        />
        <StatTile
          icon={Bell}
          label="Alert rules"
          value={serviceRules.length}
          tone={alertBreaches > 0 ? 'danger' : 'default'}
          sub={alertBreaches > 0
            ? `${alertBreaches} firing now`
            : serviceRules.length ? 'None firing' : 'No service is alerted on'}
          onClick={serviceRules.length ? () => toggleFilter('alert_enabled') : undefined}
          active={filter === 'alert_enabled'}
        />
        <StatTile
          icon={Wrench}
          label="Total"
          value={items.length}
          sub={newest ? `Updated ${relativeTime(newest)}` : 'In inventory'}
          onClick={filter !== 'all' ? () => applyFilter('all') : undefined}
        />
      </div>

      <Panel
        flush
        icon={Wrench}
        title="Services"
        subtitle={(
          <>
            {items.length} in inventory · {serviceRules.length} alert rule{serviceRules.length === 1 ? '' : 's'}
            <span className="hidden md:inline"> · alerts fire only for services with a rule</span>
          </>
        )}
        actions={(
          <ExportCsvButton rows={filtered} columns={SERVICES_CSV} filename={`${server.display_name}-services.csv`} />
        )}
      >
        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-2 border-b border-border/70 px-4 py-2.5">
          <div className="relative min-w-[220px] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              className="h-8 pl-8 pr-8 text-[13px]"
              placeholder="Filter by display or service name…"
              value={q}
              onChange={(e) => { setQ(e.target.value); pager.reset() }}
            />
            {q && (
              <button
                type="button"
                aria-label="Clear search"
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted hover:text-text"
                onClick={() => { setQ(''); pager.reset() }}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <Select value={filter} onValueChange={(v) => applyFilter(v as Filter)}>
            <SelectTrigger className="h-8 w-[200px] text-[13px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              {FILTERS.map((f) => <SelectItem key={f.value} value={f.value}>{f.label}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button
            size="sm"
            variant={sortBy === 'attention' ? 'secondary' : 'outline'}
            className="h-8"
            title="Alert breaches first, then stopped auto-start services"
            onClick={() => { setSortBy('attention'); setSortDir('asc'); pager.reset() }}
            disabled={sortBy === 'attention'}
          >
            <ArrowDownWideNarrow className="h-3.5 w-3.5" /> Problems first
          </Button>
        </div>

        {stale && (
          <div className="flex items-start gap-2 border-b border-warning/30 bg-warning/[0.06] px-4 py-2 text-xs text-warning">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              The agent has not re-reported services recently — these are last-known states, not live ones.
            </span>
          </div>
        )}

        {filtered.length === 0 ? (
          items.length === 0 ? (
            <EmptyState icon={Wrench} title="No services reported" hint={telemetryEmptyMessage(server)} />
          ) : (
            <EmptyState
              icon={Search}
              title="No services match these filters"
              hint="Clear the search or state filter to see the full list."
              action={(
                <Button
                  size="sm" variant="outline" className="mt-1"
                  onClick={() => { setQ(''); applyFilter('all') }}
                >
                  Clear filters
                </Button>
              )}
            />
          )
        ) : (
          <div className="max-h-[680px] overflow-auto">
            <table className="w-full min-w-[900px] border-separate border-spacing-0 text-[13px]">
              <thead>
                <tr className="text-left text-[11px] font-medium uppercase tracking-wide text-muted">
                  <HeadCell className={cn('pl-4', sortableTh)} onClick={() => toggleSort('name')}>
                    Name{sortIndicator(sortBy === 'name', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-48', sortableTh)} onClick={() => toggleSort('service')}>
                    Service{sortIndicator(sortBy === 'service', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-36', sortableTh)} onClick={() => toggleSort('state')}>
                    State{sortIndicator(sortBy === 'state', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-28', sortableTh)} onClick={() => toggleSort('start_mode')}>
                    Start mode{sortIndicator(sortBy === 'start_mode', sortDir)}
                  </HeadCell>
                  <HeadCell className="w-28">Alert</HeadCell>
                  <HeadCell className={cn('w-20 text-right', sortableTh)} onClick={() => toggleSort('pid')}>
                    PID{sortIndicator(sortBy === 'pid', sortDir)}
                  </HeadCell>
                  <HeadCell className={cn('w-24', sortableTh)} onClick={() => toggleSort('updated')}>
                    Updated{sortIndicator(sortBy === 'updated', sortDir)}
                  </HeadCell>
                  <HeadCell className="w-32 pr-4 text-right"><span className="sr-only">Actions</span></HeadCell>
                </tr>
              </thead>
              <tbody>
                {pager.pageRows.map((s) => {
                  const rule = ruleByService.get(s.service_name.toLowerCase())
                  const level = attention(s)
                  const label = s.display_name || s.service_name
                  return (
                    <tr
                      key={s.service_name}
                      className={cn(
                        'group transition-colors hover:bg-surface2/40',
                        level === 0 && 'bg-danger/[0.04]',
                        level === 1 && 'bg-warning/[0.04]',
                      )}
                    >
                      <BodyCell className="max-w-0 pl-4">
                        <span
                          className="block truncate font-medium text-text"
                          title={s.description ? `${label}\n\n${s.description}` : label}
                        >
                          {label}
                        </span>
                      </BodyCell>
                      <BodyCell className="max-w-0">
                        <span className="block truncate font-mono text-xs text-muted" title={s.service_name}>
                          {s.service_name}
                        </span>
                      </BodyCell>
                      <BodyCell>
                        <Pill
                          tone={stateTone(s)}
                          title={isStopped(s) && isAutoStart(s) ? 'Auto-start service that is not running' : undefined}
                        >
                          {stateLabel(s)}
                        </Pill>
                      </BodyCell>
                      <BodyCell className="text-xs text-text2">{titleCase(s.start_mode)}</BodyCell>
                      <BodyCell>
                        {!rule ? (
                          <span className="text-xs text-muted">—</span>
                        ) : isStopped(s) ? (
                          <Pill tone="danger" pulse title={rule.name}>Firing</Pill>
                        ) : (
                          <Pill tone="info" title={rule.name}>Monitored</Pill>
                        )}
                      </BodyCell>
                      <BodyCell className="text-right text-xs tabular-nums text-muted">{s.pid || '—'}</BodyCell>
                      <BodyCell
                        className="whitespace-nowrap text-xs text-muted"
                        title={s.updated_at ? new Date(s.updated_at).toLocaleString() : undefined}
                      >
                        {relativeTime(s.updated_at)}
                      </BodyCell>
                      <BodyCell className="pr-4 text-right">
                        {rule ? (
                          rule.server_id ? (
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 px-2 text-xs text-muted hover:text-danger [&_svg]:size-3.5"
                              title="Remove the alert rule for this service"
                              disabled={delRule.isPending && delRule.variables === rule.id}
                              onClick={() => setRemoving({ rule, label })}
                            >
                              <BellOff /> Remove alert
                            </Button>
                          ) : (
                            <span className="text-[11px] text-muted" title="Defined for all servers — edit it on the Alerts page">
                              Global rule
                            </span>
                          )
                        ) : (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-7 px-2 text-xs text-muted hover:text-primary [&_svg]:size-3.5"
                            title="Create an alert rule that fires when this service is stopped"
                            onClick={() => setAlertFor(s.service_name)}
                          >
                            <Bell /> Alert
                          </Button>
                        )}
                      </BodyCell>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        <TablePager {...pager} noun="services" />
      </Panel>

      <CreateHostRuleDialog
        serverId={serverId}
        open={Boolean(alertFor)}
        onOpenChange={(o) => { if (!o) setAlertFor(null) }}
        initial={alertFor ? { metric: 'host_service_down', target: alertFor, name: `${alertFor} stopped` } : undefined}
        onCreated={() => qc.invalidateQueries({ queryKey: ['servers', serverId, 'host-rules'] })}
      />

      <ConfirmDialog
        open={Boolean(removing)}
        onOpenChange={(o) => { if (!o) setRemoving(null) }}
        title="Remove alert rule"
        description={removing
          ? `Stop alerting when ${removing.label} is stopped? The rule "${removing.rule.name}" will be deleted.`
          : undefined}
        confirmText="Remove"
        destructive
        loading={delRule.isPending}
        onConfirm={() => { if (removing) delRule.mutate(removing.rule.id) }}
      />
    </div>
  )
}

/* ── Table cells ─────────────────────────────────────────────────── */

function HeadCell({ className, children, onClick }: {
  className?: string
  children: ReactNode
  onClick?: () => void
}) {
  return (
    <th
      onClick={onClick}
      className={cn(
        'sticky top-0 z-10 h-9 border-b border-border bg-surface px-3 align-middle font-medium',
        className,
      )}
    >
      {children}
    </th>
  )
}

function BodyCell({ className, children, title }: {
  className?: string
  children: ReactNode
  title?: string
}) {
  return (
    <td title={title} className={cn('h-10 border-b border-border/60 px-3 py-1.5 align-middle', className)}>
      {children}
    </td>
  )
}

