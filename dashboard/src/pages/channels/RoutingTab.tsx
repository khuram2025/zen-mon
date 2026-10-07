import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertOctagon,
  AlertTriangle,
  ArrowRight,
  Bell,
  CalendarClock,
  Clock,
  ExternalLink,
  Loader2,
  Mail,
  Pencil,
  Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { useReportSchedules } from '@/features/reports/schedules/api'
import { isFailing, relativeTo, soonestRun } from '@/features/reports/schedules/model'
import { useNow } from '@/features/reports/schedules/ui'

type Channel = { id: string; name: string; type: string; enabled: boolean }
type AlertRule = {
  id: string
  name: string
  enabled: boolean
  severity: string
  metric: string
  operator: string
  threshold: number | null
  notify_channels: string[]
  schedule_start?: string | null
  schedule_end?: string | null
  schedule_days?: number[]
}

const DAY_ABBR = ['', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function quietHours(r: AlertRule): string {
  if (!r.schedule_start && !r.schedule_end && (!r.schedule_days || r.schedule_days.length === 0)) {
    return 'Always active'
  }
  const window = r.schedule_start && r.schedule_end ? `${r.schedule_start}–${r.schedule_end}` : 'All day'
  const days =
    r.schedule_days && r.schedule_days.length && r.schedule_days.length < 7
      ? ' · ' + r.schedule_days.map((d) => DAY_ABBR[d]).join(',')
      : ''
  return window + days
}

function ChannelBadges({ ids, map }: { ids: string[]; map: Record<string, Channel> }) {
  if (!ids || ids.length === 0) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-warning">
        <AlertTriangle className="h-3 w-3" /> No channel
      </span>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {ids.map((id) => {
        const ch = map[id]
        return (
          <Badge key={id} variant={ch ? 'outline' : 'danger'} className="gap-1">
            {ch?.type === 'email' ? <Mail className="h-3 w-3" /> : <Bell className="h-3 w-3" />}
            {ch ? ch.name : `${id.slice(0, 8)} (missing)`}
          </Badge>
        )
      })}
    </div>
  )
}

export function RoutingTab() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [deletingRule, setDeletingRule] = useState<AlertRule | null>(null)

  // NOTE: these query keys are shared with other pages (ChannelsPage,
  // AlertRulesPage), which normalize the response to a plain array. We MUST
  // return the same shape here — React Query caches by key, so a mismatched
  // shape would corrupt the other consumers' data.
  const { data: channels = [] } = useQuery<Channel[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => {
      const r = (await api.get('/settings/channels')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })
  const channelMap = useMemo(() => Object.fromEntries(channels.map((c) => [c.id, c])), [channels])

  const { data: rules = [], isLoading: loadingRules } = useQuery<AlertRule[]>({
    queryKey: ['alert-rules'],
    queryFn: async () => {
      const r = (await api.get('/alert-rules')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const toggleRule = useMutation({
    mutationFn: async (id: string) => api.post(`/alert-rules/${id}/toggle`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alert-rules'] }),
    onError: (e: any) => toast.error('Toggle failed', apiErrorMessage(e)),
  })
  const delRule = useMutation({
    mutationFn: async (id: string) => api.delete(`/alert-rules/${id}`),
    onSuccess: () => {
      toast.success('Rule deleted')
      setDeletingRule(null)
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-5">
      {/* ── Scheduled reports (managed in Reports → Scheduled reports) ── */}
      <ScheduledReportsSummary channelMap={channelMap} />

      {/* ── Alert rules routing ── */}
      <Card>
        <CardContent className="p-0">
          <div className="flex flex-col gap-2 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-warning/10 text-warning">
                <Bell className="h-4 w-4" />
              </div>
              <div>
                <h2 className="text-sm font-semibold">Alert Rules</h2>
                <p className="text-xs text-muted">
                  {loadingRules ? 'Loading…' : `${rules.length} alert rule${rules.length === 1 ? '' : 's'} and their delivery channels`}
                </p>
              </div>
            </div>
            <Button size="sm" variant="outline" onClick={() => navigate('/alert-rules')}>
              <ExternalLink className="h-4 w-4" />
              Manage alert rules
            </Button>
          </div>

          <Table>
            <THead className="bg-surface2/60">
              <Tr>
                <Th>Rule</Th>
                <Th>Severity</Th>
                <Th>Active window</Th>
                <Th>Channels</Th>
                <Th className="w-40 text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {loadingRules ? (
                <Tr><Td colSpan={5} className="py-10 text-center text-sm text-muted"><Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" />Loading</Td></Tr>
              ) : rules.length === 0 ? (
                <Tr><Td colSpan={5} className="py-12 text-center text-sm text-muted">No alert rules configured.</Td></Tr>
              ) : (
                rules.map((r) => (
                  <Tr key={r.id} className="align-top">
                    <Td>
                      <div className="font-medium text-text">{r.name}</div>
                      <div className="mt-1 text-[11px] text-muted">{r.metric} {r.operator} {r.threshold}</div>
                    </Td>
                    <Td>
                      <Badge variant={r.severity === 'critical' ? 'danger' : r.severity === 'warning' ? 'warning' : 'info'}>{r.severity}</Badge>
                    </Td>
                    <Td><div className="flex items-center gap-1.5 text-sm"><Clock className="h-3.5 w-3.5 text-muted" />{quietHours(r)}</div></Td>
                    <Td><ChannelBadges ids={r.notify_channels} map={channelMap} /></Td>
                    <Td>
                      <div className="flex items-center justify-end gap-1">
                        <Switch checked={r.enabled} onCheckedChange={() => toggleRule.mutate(r.id)} />
                        <Button size="sm" variant="ghost" onClick={() => navigate('/alert-rules')} title="Edit in Alert Rules"><Pencil className="h-3.5 w-3.5" /></Button>
                        <Button size="sm" variant="ghost" className="text-muted hover:text-danger" onClick={() => setDeletingRule(r)} title="Delete"><Trash2 className="h-3.5 w-3.5" /></Button>
                      </div>
                    </Td>
                  </Tr>
                ))
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <ConfirmDialog
        open={!!deletingRule}
        onOpenChange={(o) => !o && setDeletingRule(null)}
        title="Delete alert rule"
        description={<>Delete <span className="font-semibold text-text">{deletingRule?.name}</span>?</>}
        confirmText="Delete"
        destructive
        loading={delRule.isPending}
        onConfirm={() => { if (deletingRule) delRule.mutate(deletingRule.id) }}
      />
    </div>
  )
}

/**
 * Report schedules live in Reports → Scheduled reports. Routing shows how many
 * deliver to channels, the next delivery, failures and which channels they
 * use, with a link to manage them there.
 */
function ScheduledReportsSummary({ channelMap }: { channelMap: Record<string, Channel> }) {
  const can = useCan()
  const allowed = can('reports.view')
  const q = useReportSchedules({ enabled: allowed })
  const now = useNow(60_000)
  const list = q.data ?? []
  const active = list.filter((s) => s.enabled)
  const failing = list.filter(isFailing)
  const soonest = soonestRun(list, now)
  const usage = useMemo(() => {
    const counts = new Map<string, number>()
    for (const s of list) for (const id of s.notify_channels ?? []) counts.set(id, (counts.get(id) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [list])

  if (!allowed) return null
  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-2">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-info/10 text-info">
              <CalendarClock className="h-4 w-4" aria-hidden />
            </div>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold">Scheduled Reports</h2>
              <p className="text-xs text-muted">Report schedules that deliver to these channels are managed in Reports.</p>
            </div>
          </div>
          <Button asChild size="sm" variant="outline" className="shrink-0">
            <Link to="/reports/schedules">
              Manage schedules <ArrowRight className="h-4 w-4" aria-hidden />
            </Link>
          </Button>
        </div>
        <div className="border-t border-border px-4 py-3">
          {q.isLoading ? (
            <div className="flex gap-3" aria-busy="true" aria-label="Loading scheduled reports">
              <Skeleton className="h-5 w-28" /><Skeleton className="h-5 w-24" /><Skeleton className="h-5 w-40" />
            </div>
          ) : q.isError && !q.data ? (
            <QueryFailure compact error={q.error} what="scheduled reports" onRetry={() => void q.refetch()} retrying={q.isFetching} />
          ) : list.length === 0 ? (
            <p className="text-sm text-muted">No scheduled reports yet.</p>
          ) : (
            <div className="space-y-2">
              <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" aria-label="Scheduled reports summary">
                <li className="text-text2"><span className="font-semibold tabular-nums text-text">{list.length}</span> schedule{list.length === 1 ? '' : 's'}</li>
                <li className="text-text2"><span className="font-semibold tabular-nums text-text">{active.length}</span> active</li>
                {failing.length > 0 && (
                  <li className="inline-flex items-center gap-1 text-danger-fg">
                    <AlertOctagon className="h-3.5 w-3.5" aria-hidden />
                    <span className="font-semibold tabular-nums">{failing.length}</span> failing
                  </li>
                )}
                {soonest && (
                  <li className="inline-flex min-w-0 items-center gap-1 text-text2">
                    <Clock className="h-3.5 w-3.5 text-muted" aria-hidden />
                    Next: <span className="truncate font-medium text-text">{soonest.schedule.name}</span>
                    <span className="tabular-nums text-muted">{relativeTo(soonest.at, now)}</span>
                  </li>
                )}
              </ul>
              {usage.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
                  <span>Delivering to</span>
                  {usage.slice(0, 6).map(([id, n]) => {
                    const ch = channelMap[id]
                    return (
                      <Badge key={id} variant={ch ? 'outline' : 'danger'} className="gap-1">
                        {ch?.type === 'email' ? <Mail className="h-3 w-3" aria-hidden /> : <Bell className="h-3 w-3" aria-hidden />}
                        {ch ? ch.name : 'Deleted channel'}
                        <span className="tabular-nums text-muted">×{n}</span>
                      </Badge>
                    )
                  })}
                  {usage.length > 6 && <span>and {usage.length - 6} more</span>}
                </div>
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
