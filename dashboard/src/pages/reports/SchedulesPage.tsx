import { Fragment, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  Bell,
  CalendarClock,
  ChevronDown,
  ChevronRight,
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
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { ReportScheduleFormDialog, type ReportSchedule } from '@/components/forms/ReportScheduleFormDialog'
import { REPORT_TYPE_LABELS, useReportCatalog, type ReportCatalog } from '@/hooks/useReportCatalog'

type Channel = { id: string; name: string; type: string; enabled: boolean }

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

function typeLabel(s: ReportSchedule, catalog?: ReportCatalog): string {
  if (s.report_type === 'custom') {
    const name = s.custom_report_id
      ? catalog?.custom.find((c) => c.id === s.custom_report_id)?.name
      : undefined
    return name ? `Custom · ${name}` : 'Custom'
  }
  return REPORT_TYPE_LABELS[s.report_type] || s.report_type
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

export default function SchedulesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ReportSchedule | null>(null)
  const [deleting, setDeleting] = useState<ReportSchedule | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const { data: catalog } = useReportCatalog()

  // NOTE: shared query key with ChannelsPage/RoutingTab — must keep the
  // normalized array shape (React Query caches by key).
  const { data: channels = [] } = useQuery<Channel[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => {
      const r = (await api.get('/settings/channels')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })
  const channelMap = useMemo(() => Object.fromEntries(channels.map((c) => [c.id, c])), [channels])

  const { data: schedulesResp, isLoading } = useQuery<any>({
    queryKey: ['report-schedules'],
    queryFn: async () => (await api.get('/report-schedules')).data,
  })
  const schedules: ReportSchedule[] = schedulesResp?.data || []

  const toggleSchedule = useMutation({
    mutationFn: async (id: string) => api.post(`/report-schedules/${id}/toggle`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['report-schedules'] }),
    onError: (e: any) => toast.error('Toggle failed', apiErrorMessage(e)),
  })
  const runNow = useMutation({
    mutationFn: async (id: string) => (await api.post(`/report-schedules/${id}/run-now`)).data,
    onSuccess: (data: any) => {
      const delivered = data?.delivered?.length || 0
      toast.success(
        'Report generated',
        delivered ? `Delivered to ${delivered} channel(s)` : 'No channels linked — view via the report link',
      )
      qc.invalidateQueries({ queryKey: ['report-schedules'] })
      qc.invalidateQueries({ queryKey: ['reports', 'runs'] })
    },
    onError: (e: any) => toast.error('Run failed', apiErrorMessage(e)),
  })
  const delSchedule = useMutation({
    mutationFn: async (id: string) => api.delete(`/report-schedules/${id}`),
    onSuccess: () => {
      toast.success('Schedule deleted')
      setDeleting(null)
      qc.invalidateQueries({ queryKey: ['report-schedules'] })
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <Link
            to="/reports"
            className="inline-flex items-center gap-1 text-xs font-medium text-muted transition-colors hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Report library
          </Link>
          <h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <CalendarClock className="h-6 w-6 text-primary" />
            Scheduled reports
          </h1>
          <p className="mt-1 text-sm text-muted">
            Generate reports automatically and deliver them to your notification channels.
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => {
            setEditing(null)
            setFormOpen(true)
          }}
        >
          <Plus className="h-4 w-4" /> New schedule
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
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
              {isLoading ? (
                <Tr>
                  <Td colSpan={7} className="py-10 text-center text-sm text-muted">
                    <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" />
                    Loading
                  </Td>
                </Tr>
              ) : schedules.length === 0 ? (
                <Tr>
                  <Td colSpan={7} className="py-12">
                    <div className="mx-auto flex max-w-md flex-col items-center gap-3 text-center">
                      <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-info/10 text-info">
                        <FileText className="h-6 w-6" />
                      </div>
                      <div>
                        <div className="text-sm font-semibold">No scheduled reports</div>
                        <div className="mt-1 text-xs text-muted">
                          Schedule a report to be generated and delivered automatically.
                        </div>
                      </div>
                      <Button
                        size="sm"
                        onClick={() => {
                          setEditing(null)
                          setFormOpen(true)
                        }}
                      >
                        <Plus className="h-4 w-4" /> New schedule
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ) : (
                schedules.map((s) => (
                  <Fragment key={s.id}>
                    <Tr className="align-top">
                      <Td>
                        <button
                          className="flex items-start gap-2 text-left"
                          onClick={() => setExpanded(expanded === s.id ? null : s.id)}
                        >
                          {expanded === s.id ? (
                            <ChevronDown className="mt-1 h-4 w-4 text-muted" />
                          ) : (
                            <ChevronRight className="mt-1 h-4 w-4 text-muted" />
                          )}
                          <span>
                            <span className="font-medium text-text">{s.name}</span>
                            <span className="mt-1 block">
                              <Badge variant="outline">{typeLabel(s, catalog)}</Badge>
                            </span>
                          </span>
                        </button>
                      </Td>
                      <Td>
                        <div className="flex items-center gap-1.5 text-sm">
                          <CalendarClock className="h-3.5 w-3.5 text-muted" />
                          {scheduleSummary(s)}
                        </div>
                        <div className="mt-1 text-[11px] uppercase text-muted">{s.period.replace('last_', 'last ')}</div>
                      </Td>
                      <Td>
                        <ChannelBadges ids={s.notify_channels} map={channelMap} />
                      </Td>
                      <Td>
                        <div className="text-sm text-text2">{s.last_run_at ? relativeTime(s.last_run_at) : 'Never'}</div>
                      </Td>
                      <Td>
                        <div className="text-sm text-text2">
                          {s.next_run_at ? relativeTime(s.next_run_at) : s.enabled ? '—' : 'Disabled'}
                        </div>
                      </Td>
                      <Td>{statusBadge(s.last_status)}</Td>
                      <Td>
                        <div className="flex items-center justify-end gap-1">
                          <Switch checked={s.enabled} onCheckedChange={() => toggleSchedule.mutate(s.id)} />
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => runNow.mutate(s.id)}
                            disabled={runNow.isPending}
                            title="Generate & send now"
                          >
                            {runNow.isPending ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              <Play className="h-3.5 w-3.5" />
                            )}
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => {
                              setEditing(s)
                              setFormOpen(true)
                            }}
                            title="Edit"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-muted hover:text-danger"
                            onClick={() => setDeleting(s)}
                            title="Delete"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
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

      <ReportScheduleFormDialog open={formOpen} onOpenChange={setFormOpen} schedule={editing} />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete scheduled report"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>? Generated report links are kept.
          </>
        }
        confirmText="Delete"
        destructive
        loading={delSchedule.isPending}
        onConfirm={() => {
          if (deleting) delSchedule.mutate(deleting.id)
        }}
      />
    </div>
  )
}

function ScheduleDetail({ schedule }: { schedule: ReportSchedule }) {
  const { data, isLoading } = useQuery<any>({
    queryKey: ['report-schedules', schedule.id, 'runs'],
    queryFn: async () => (await api.get(`/report-schedules/${schedule.id}/runs`)).data,
  })
  const runs: any[] = data?.data || []
  return (
    <div className="space-y-3 p-4">
      {schedule.description && <p className="text-sm text-text2">{schedule.description}</p>}
      <div className="flex flex-wrap gap-4 text-xs text-muted">
        <span>
          Attachment: <span className="font-medium text-text">{schedule.format.toUpperCase()}</span>
        </span>
        <span>
          Period: <span className="font-medium text-text">{schedule.period}</span>
        </span>
        {schedule.last_error && <span className="text-danger">Last error: {schedule.last_error}</span>}
      </div>
      <div>
        <div className="mb-1 text-xs font-semibold uppercase text-muted">Recent runs</div>
        {isLoading ? (
          <div className="text-xs text-muted">
            <Loader2 className="inline h-3 w-3 animate-spin" /> Loading runs
          </div>
        ) : runs.length === 0 ? (
          <div className="text-xs text-muted">No runs yet — use the ▶ action to generate one now.</div>
        ) : (
          <div className="space-y-1">
            {runs.map((run) => (
              <div
                key={run.id}
                className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg px-3 py-2"
              >
                <div className="flex items-center gap-2 text-sm">
                  {statusBadge(run.status)}
                  <span className="text-text2">{run.generated_at ? relativeTime(run.generated_at) : ''}</span>
                  {run.delivered_to?.length > 0 && (
                    <span className="text-xs text-muted">→ {run.delivered_to.join(', ')}</span>
                  )}
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
