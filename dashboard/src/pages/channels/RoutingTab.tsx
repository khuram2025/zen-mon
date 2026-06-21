import { Fragment, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Bell,
  CalendarClock,
  ChevronDown,
  ChevronRight,
  Clock,
  ExternalLink,
  FileText,
  Loader2,
  Mail,
  Pencil,
  Play,
  Plus,
  Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { ReportScheduleFormDialog, type ReportSchedule } from '@/components/forms/ReportScheduleFormDialog'

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

const REPORT_TYPE_LABELS: Record<string, string> = {
  executive_summary: 'Executive Summary',
  device_health: 'Device Health',
  service_health: 'Service Health',
  alert_analysis: 'Alert Analysis',
  full_report: 'Full Report',
}
const DAY_ABBR = ['', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function pad(n: number) {
  return String(n).padStart(2, '0')
}

function scheduleSummary(s: ReportSchedule): string {
  const t = `${pad(s.hour)}:${pad(s.minute)}`
  if (s.frequency === 'daily') return `Daily at ${t}`
  if (s.frequency === 'weekly') return `Weekly · ${DAY_ABBR[s.day_of_week || 1]} at ${t}`
  if (s.frequency === 'monthly') return `Monthly · day ${s.day_of_month || 1} at ${t}`
  return t
}

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

function statusBadge(status?: string | null) {
  if (!status) return <span className="text-xs text-muted">—</span>
  const v = status === 'success' ? 'success' : status === 'partial' ? 'warning' : 'danger'
  return <Badge variant={v as any}>{status}</Badge>
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
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ReportSchedule | null>(null)
  const [deletingSchedule, setDeletingSchedule] = useState<ReportSchedule | null>(null)
  const [deletingRule, setDeletingRule] = useState<AlertRule | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

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

  const { data: schedulesResp, isLoading: loadingSchedules } = useQuery<any>({
    queryKey: ['report-schedules'],
    queryFn: async () => (await api.get('/report-schedules')).data,
  })
  const schedules: ReportSchedule[] & any[] = schedulesResp?.data || []

  const { data: rules = [], isLoading: loadingRules } = useQuery<AlertRule[]>({
    queryKey: ['alert-rules'],
    queryFn: async () => {
      const r = (await api.get('/alert-rules')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const toggleSchedule = useMutation({
    mutationFn: async (id: string) => api.post(`/report-schedules/${id}/toggle`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['report-schedules'] }),
    onError: (e: any) => toast.error('Toggle failed', apiErrorMessage(e)),
  })
  const runNow = useMutation({
    mutationFn: async (id: string) => (await api.post(`/report-schedules/${id}/run-now`)).data,
    onSuccess: (data: any) => {
      const delivered = data?.delivered?.length || 0
      toast.success('Report generated', delivered ? `Delivered to ${delivered} channel(s)` : 'No channels linked — view via the report link')
      qc.invalidateQueries({ queryKey: ['report-schedules'] })
    },
    onError: (e: any) => toast.error('Run failed', apiErrorMessage(e)),
  })
  const delSchedule = useMutation({
    mutationFn: async (id: string) => api.delete(`/report-schedules/${id}`),
    onSuccess: () => {
      toast.success('Schedule deleted')
      setDeletingSchedule(null)
      qc.invalidateQueries({ queryKey: ['report-schedules'] })
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
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
      {/* ── Scheduled reports ── */}
      <Card>
        <CardContent className="p-0">
          <div className="flex flex-col gap-2 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-info/10 text-info">
                <FileText className="h-4 w-4" />
              </div>
              <div>
                <h2 className="text-sm font-semibold">Scheduled Reports</h2>
                <p className="text-xs text-muted">
                  {loadingSchedules ? 'Loading…' : `${schedules.length} report schedule${schedules.length === 1 ? '' : 's'} delivering to channels`}
                </p>
              </div>
            </div>
            <Button size="sm" onClick={() => { setEditing(null); setFormOpen(true) }}>
              <Plus className="h-4 w-4" />
              New scheduled report
            </Button>
          </div>

          <Table>
            <THead className="bg-surface2/60">
              <Tr>
                <Th>Report</Th>
                <Th>Schedule</Th>
                <Th>Channels</Th>
                <Th>Last run</Th>
                <Th>Next run</Th>
                <Th>Status</Th>
                <Th className="w-48 text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {loadingSchedules ? (
                <Tr><Td colSpan={7} className="py-10 text-center text-sm text-muted"><Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" />Loading</Td></Tr>
              ) : schedules.length === 0 ? (
                <Tr>
                  <Td colSpan={7} className="py-12">
                    <div className="mx-auto flex max-w-md flex-col items-center gap-3 text-center">
                      <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-info/10 text-info"><FileText className="h-6 w-6" /></div>
                      <div>
                        <div className="text-sm font-semibold">No scheduled reports</div>
                        <div className="mt-1 text-xs text-muted">Schedule a report to be generated and emailed automatically.</div>
                      </div>
                      <Button size="sm" onClick={() => { setEditing(null); setFormOpen(true) }}><Plus className="h-4 w-4" />New scheduled report</Button>
                    </div>
                  </Td>
                </Tr>
              ) : (
                schedules.map((s) => (
                  <Fragment key={s.id}>
                    <Tr className="align-top">
                      <Td>
                        <button className="flex items-start gap-2 text-left" onClick={() => setExpanded(expanded === s.id ? null : s.id)}>
                          {expanded === s.id ? <ChevronDown className="mt-1 h-4 w-4 text-muted" /> : <ChevronRight className="mt-1 h-4 w-4 text-muted" />}
                          <span>
                            <span className="font-medium text-text">{s.name}</span>
                            <span className="mt-1 block">
                              <Badge variant="outline">{REPORT_TYPE_LABELS[s.report_type] || s.report_type}</Badge>
                            </span>
                          </span>
                        </button>
                      </Td>
                      <Td>
                        <div className="flex items-center gap-1.5 text-sm"><CalendarClock className="h-3.5 w-3.5 text-muted" />{scheduleSummary(s)}</div>
                        <div className="mt-1 text-[11px] uppercase text-muted">{s.period.replace('last_', 'last ')}</div>
                      </Td>
                      <Td><ChannelBadges ids={s.notify_channels} map={channelMap} /></Td>
                      <Td><div className="text-sm text-text2">{s.last_run_at ? relativeTime(s.last_run_at) : 'Never'}</div></Td>
                      <Td><div className="text-sm text-text2">{s.next_run_at ? relativeTime(s.next_run_at) : (s.enabled ? '—' : 'Disabled')}</div></Td>
                      <Td>{statusBadge(s.last_status)}</Td>
                      <Td>
                        <div className="flex items-center justify-end gap-1">
                          <Switch checked={s.enabled} onCheckedChange={() => toggleSchedule.mutate(s.id)} />
                          <Button size="sm" variant="outline" onClick={() => runNow.mutate(s.id)} disabled={runNow.isPending} title="Generate & send now">
                            {runNow.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => { setEditing(s); setFormOpen(true) }} title="Edit"><Pencil className="h-3.5 w-3.5" /></Button>
                          <Button size="sm" variant="ghost" className="text-muted hover:text-danger" onClick={() => setDeletingSchedule(s)} title="Delete"><Trash2 className="h-3.5 w-3.5" /></Button>
                        </div>
                      </Td>
                    </Tr>
                    {expanded === s.id && (
                      <Tr>
                        <Td colSpan={7} className="bg-surface2/40">
                          <ScheduleDetail schedule={s} />
                        </Td>
                      </Tr>
                    )}
                  </Fragment>
                ))
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>

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

      <ReportScheduleFormDialog open={formOpen} onOpenChange={setFormOpen} schedule={editing} />

      <ConfirmDialog
        open={!!deletingSchedule}
        onOpenChange={(o) => !o && setDeletingSchedule(null)}
        title="Delete scheduled report"
        description={<>Delete <span className="font-semibold text-text">{deletingSchedule?.name}</span>? Generated report links are kept.</>}
        confirmText="Delete"
        destructive
        loading={delSchedule.isPending}
        onConfirm={() => { if (deletingSchedule) delSchedule.mutate(deletingSchedule.id) }}
      />
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

function ScheduleDetail({ schedule }: { schedule: ReportSchedule & any }) {
  const { data, isLoading } = useQuery<any>({
    queryKey: ['report-schedules', schedule.id, 'runs'],
    queryFn: async () => (await api.get(`/report-schedules/${schedule.id}/runs`)).data,
  })
  const runs: any[] = data?.data || []
  return (
    <div className="space-y-3 p-4">
      {schedule.description && <p className="text-sm text-text2">{schedule.description}</p>}
      <div className="flex flex-wrap gap-4 text-xs text-muted">
        <span>Attachment: <span className="font-medium text-text">{schedule.format.toUpperCase()}</span></span>
        <span>Period: <span className="font-medium text-text">{schedule.period}</span></span>
        {schedule.last_error && <span className="text-danger">Last error: {schedule.last_error}</span>}
      </div>
      <div>
        <div className="mb-1 text-xs font-semibold uppercase text-muted">Recent runs</div>
        {isLoading ? (
          <div className="text-xs text-muted"><Loader2 className="inline h-3 w-3 animate-spin" /> Loading runs</div>
        ) : runs.length === 0 ? (
          <div className="text-xs text-muted">No runs yet — use the ▶ action to generate one now.</div>
        ) : (
          <div className="space-y-1">
            {runs.map((run) => (
              <div key={run.id} className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg px-3 py-2">
                <div className="flex items-center gap-2 text-sm">
                  {statusBadge(run.status)}
                  <span className="text-text2">{run.generated_at ? relativeTime(run.generated_at) : ''}</span>
                  {run.delivered_to?.length > 0 && <span className="text-xs text-muted">→ {run.delivered_to.join(', ')}</span>}
                </div>
                <a
                  href={`${window.location.origin}/api/v1/reports/shared/${run.token}`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                >
                  <ExternalLink className="h-3 w-3" /> View report
                </a>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
