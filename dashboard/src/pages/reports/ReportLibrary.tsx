import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  AppWindow,
  BarChart3,
  Boxes,
  Briefcase,
  CalendarClock,
  Clock,
  ExternalLink,
  FileText,
  Gauge,
  HardDrive,
  Network,
  Pencil,
  PieChart,
  Plus,
  Trash2,
  Users,
  Wrench,
  type LucideIcon,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { ReportSection } from '@/components/reports/ReportSection'
import { useReportCatalog, type CatalogType, type CustomReport } from '@/hooks/useReportCatalog'

/* ------------------------------------------------------------------ */
/*  Icon & link maps                                                   */
/* ------------------------------------------------------------------ */

const TYPE_ICONS: Record<string, LucideIcon> = {
  executive_summary: BarChart3,
  device_health: Wrench,
  service_health: Briefcase,
  availability: Gauge,
  performance: Activity,
  traffic: Network,
  alerts: AlertTriangle,
  capacity: HardDrive,
  apm_performance: AppWindow,
  usage: PieChart,
  inventory: Boxes,
}

const CATEGORY_ORDER = ['Personas', 'Operations', 'Applications', 'Assets']
const CATEGORY_ICONS: Record<string, LucideIcon> = {
  Personas: Users,
  Operations: Gauge,
  Applications: AppWindow,
  Assets: Boxes,
}
const CATEGORY_BLURBS: Record<string, string> = {
  Personas: 'Live interactive dashboards tailored to each audience',
  Operations: 'Network availability, performance, traffic, alerting and capacity',
  Applications: 'Application performance and usage from the APM pipeline',
  Assets: 'Inventory and lifecycle of monitored equipment',
}

/** Where a catalog card should navigate. null = no card for this type. */
function typeLink(t: CatalogType): string | null {
  if (t.engine === 'legacy') {
    const legacyTabs: Record<string, string> = {
      executive_summary: '/reports/executive',
      device_health: '/reports/technical',
      service_health: '/reports/business',
    }
    // alert_analysis is superseded by the section-based alerts report and
    // full_report is an export-only bundle — neither gets a card.
    return legacyTabs[t.key] ?? null
  }
  return `/reports/view/${t.key}`
}

type ReportRun = {
  id: string
  title: string
  report_type: string
  period: string
  token: string
  status: string
  delivered_to: string[]
  generated_at: string | null
  schedule_name?: string | null
}

function runStatusBadge(status?: string | null) {
  if (!status) return <span className="text-xs text-muted">—</span>
  const v = status === 'success' ? 'success' : status === 'partial' ? 'warning' : 'danger'
  return <Badge variant={v as any}>{status}</Badge>
}

/* ------------------------------------------------------------------ */
/*  Cards                                                              */
/* ------------------------------------------------------------------ */

function TypeCard({ type }: { type: CatalogType }) {
  const to = typeLink(type)
  if (!to) return null
  const Icon = TYPE_ICONS[type.key] || FileText
  return (
    <Link
      to={to}
      className="group flex items-start gap-3 rounded-lg border border-border bg-surface p-4 shadow-card transition-all hover:border-primary/40 dark:shadow-card-dark"
    >
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
        <Icon className="h-[18px] w-[18px]" />
      </div>
      <div className="min-w-0">
        <p className="text-sm font-semibold tracking-tight text-text group-hover:text-primary">
          {type.title}
        </p>
        <p className="mt-0.5 line-clamp-2 text-xs text-muted">{type.description}</p>
        {type.formats.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {type.formats.map((f) => (
              <span
                key={f}
                className="rounded border border-border bg-surface2 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-muted"
              >
                {f}
              </span>
            ))}
          </div>
        )}
      </div>
    </Link>
  )
}

function CustomCard({ report, onDelete }: { report: CustomReport; onDelete: () => void }) {
  return (
    <div className="flex flex-col rounded-lg border border-border bg-surface p-4 shadow-card transition-all hover:border-primary/40 dark:shadow-card-dark">
      <Link to={`/reports/view/custom?custom_id=${report.id}`} className="group flex flex-1 items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-info/10 text-info">
          <FileText className="h-[18px] w-[18px]" />
        </div>
        <div className="min-w-0">
          <p className="text-sm font-semibold tracking-tight text-text group-hover:text-primary">{report.name}</p>
          {report.description && <p className="mt-0.5 line-clamp-2 text-xs text-muted">{report.description}</p>}
          <p className="mt-1 text-[11px] text-muted">
            {report.sections.length} section{report.sections.length === 1 ? '' : 's'}
          </p>
        </div>
      </Link>
      <div className="mt-3 flex items-center gap-1 border-t border-border pt-2">
        <Button size="sm" variant="ghost" asChild>
          <Link to={`/reports/view/custom?custom_id=${report.id}`}>
            <ExternalLink className="h-3.5 w-3.5" /> Open
          </Link>
        </Button>
        <Button size="sm" variant="ghost" asChild>
          <Link to={`/reports/builder?edit=${report.id}`}>
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Link>
        </Button>
        <Button size="sm" variant="ghost" className="ml-auto text-muted hover:text-danger" onClick={onDelete} title="Delete">
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function ReportLibrary() {
  const qc = useQueryClient()
  const { data: catalog, isLoading, error } = useReportCatalog()
  const [deleting, setDeleting] = useState<CustomReport | null>(null)

  const { data: runs = [], isLoading: loadingRuns } = useQuery<ReportRun[]>({
    queryKey: ['reports', 'runs'],
    queryFn: async () => {
      const r = (await api.get('/reports/runs', { params: { limit: 20 } })).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const delCustom = useMutation({
    mutationFn: async (id: string) => api.delete(`/reports/custom/${id}`),
    onSuccess: () => {
      toast.success('Custom report deleted')
      setDeleting(null)
      qc.invalidateQueries({ queryKey: ['reports', 'catalog'] })
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const categories = (() => {
    if (!catalog) return []
    const seen = catalog.types.map((t) => t.category)
    const extras = [...new Set(seen)].filter((c) => !CATEGORY_ORDER.includes(c))
    return [...CATEGORY_ORDER, ...extras].filter((c) => seen.includes(c))
  })()

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileText className="h-6 w-6 text-primary" />
            Report library
          </h1>
          <p className="mt-1 text-sm text-muted">
            Ready-made and custom reports — view live, export as PDF/HTML, or schedule delivery to your channels.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to="/reports/schedules">
              <CalendarClock className="h-4 w-4" /> Scheduled reports
            </Link>
          </Button>
          <Button size="sm" asChild>
            <Link to="/reports/builder">
              <Plus className="h-4 w-4" /> New custom report
            </Link>
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load the report catalog: {apiErrorMessage(error)}
        </div>
      )}

      {isLoading && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-[110px] rounded-lg" />
          ))}
        </div>
      )}

      {/* Preset report groups */}
      {categories.map((cat) => {
        const CatIcon = CATEGORY_ICONS[cat] || FileText
        const types = catalog!.types.filter((t) => t.category === cat && typeLink(t))
        if (types.length === 0) return null
        return (
          <section key={cat}>
            <div className="mb-2 flex items-center gap-2">
              <CatIcon className="h-4 w-4 text-muted" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-text2">{cat}</h2>
              {CATEGORY_BLURBS[cat] && <span className="hidden text-xs text-muted sm:inline">· {CATEGORY_BLURBS[cat]}</span>}
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {types.map((t) => (
                <TypeCard key={t.key} type={t} />
              ))}
            </div>
          </section>
        )
      })}

      {/* Custom reports */}
      {catalog && (
        <section>
          <div className="mb-2 flex items-center gap-2">
            <Pencil className="h-4 w-4 text-muted" />
            <h2 className="text-sm font-semibold uppercase tracking-wider text-text2">Custom reports</h2>
            <span className="hidden text-xs text-muted sm:inline">· Your own combinations of report sections</span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {catalog.custom.map((c) => (
              <CustomCard key={c.id} report={c} onDelete={() => setDeleting(c)} />
            ))}
            <Link
              to="/reports/builder"
              className={cn(
                'flex min-h-[110px] flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-border bg-surface/40 p-4 text-muted',
                'transition-colors hover:border-primary/50 hover:text-primary',
              )}
            >
              <Plus className="h-5 w-5" />
              <span className="text-sm font-medium">New custom report</span>
            </Link>
          </div>
        </section>
      )}

      {/* Recent generated reports */}
      <ReportSection
        title="Recent reports"
        description="Reports generated by schedules or on demand — links open the shared HTML copy"
        icon={<Clock className="h-4 w-4" />}
        padded={false}
      >
        {loadingRuns ? (
          <div className="px-5 py-8 text-center text-sm text-muted">Loading recent runs…</div>
        ) : runs.length === 0 ? (
          <div className="px-5 py-8 text-center text-sm text-muted">
            No reports generated yet — schedule one or use "Run now" on a schedule.
          </div>
        ) : (
          <Table>
            <THead className="bg-surface2/60">
              <Tr>
                <Th>Report</Th>
                <Th>Schedule</Th>
                <Th>Status</Th>
                <Th>Generated</Th>
                <Th className="w-24 text-right">Link</Th>
              </Tr>
            </THead>
            <TBody>
              {runs.map((run) => (
                <Tr key={run.id}>
                  <Td>
                    <span className="font-medium text-text">{run.title}</span>
                  </Td>
                  <Td>
                    <span className="text-sm text-text2">{run.schedule_name || '—'}</span>
                  </Td>
                  <Td>{runStatusBadge(run.status)}</Td>
                  <Td>
                    <span className="text-sm text-text2">{run.generated_at ? relativeTime(run.generated_at) : '—'}</span>
                  </Td>
                  <Td className="text-right">
                    <a
                      href={`${window.location.origin}/api/v1/reports/shared/${run.token}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                    >
                      <ExternalLink className="h-3 w-3" /> View
                    </a>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </ReportSection>

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete custom report"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>? Schedules pointing at it will
            stop delivering.
          </>
        }
        confirmText="Delete"
        destructive
        loading={delCustom.isPending}
        onConfirm={() => {
          if (deleting) delCustom.mutate(deleting.id)
        }}
      />
    </div>
  )
}
