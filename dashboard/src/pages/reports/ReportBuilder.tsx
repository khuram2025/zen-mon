/**
 * Report template designer: /reports/builder
 *
 *   /reports/builder               a blank template
 *   /reports/builder?from=<key>    customise a built-in template (a new organisation template based on it)
 *   /reports/builder?edit=<uuid>   edit an organisation template (can_edit / can_change_visibility apply)
 *   /reports/builder?clone=<uuid>  a private copy of an organisation template
 *
 * Needs reports.manage. Saving opens the result in the viewer
 * (/reports/view/custom?custom_id=<id>).
 */
import { useMemo, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowLeft, FileQuestion, FilePlus2, LockKeyhole } from 'lucide-react'
import { describeFailure } from '@/lib/access'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { useReportBranding, useReportCatalog, useTemplate } from '@/features/reports/api'
import {
  draftFromBuiltin, draftFromCustom, emptyDraft, type DesignerMode, type Draft,
} from '@/features/reports/designer/model'
import { TemplateDesigner } from '@/features/reports/designer/TemplateDesigner'

function StateCard({ icon: Icon, title, children, actions }: {
  icon: typeof FileQuestion
  title: string
  children: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="mx-auto mt-6 max-w-lg rounded-xl border border-border bg-surface px-6 py-12 text-center shadow-card dark:shadow-card-dark">
      <span className="mx-auto flex h-11 w-11 items-center justify-center rounded-full bg-surface2 text-muted">
        <Icon className="h-5 w-5" aria-hidden />
      </span>
      <h1 className="mt-3 text-lg font-semibold text-text">{title}</h1>
      <div className="mx-auto mt-1.5 max-w-md text-sm leading-relaxed text-muted">{children}</div>
      <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
        {actions}
        <Button asChild variant={actions ? 'outline' : 'default'}>
          <Link to="/reports"><ArrowLeft aria-hidden /> Back to the report library</Link>
        </Button>
      </div>
    </div>
  )
}

function DesignerSkeleton() {
  return (
    <div role="status" className="mx-auto flex w-full max-w-[1920px] flex-col gap-3">
      <span className="sr-only">Loading the template designer…</span>
      <div aria-hidden className="rounded-xl border border-border bg-surface px-4 py-3">
        <Skeleton className="h-3 w-40" />
        <Skeleton className="mt-2 h-7 w-72" />
        <Skeleton className="mt-2 h-3 w-56" />
      </div>
      <Skeleton className="h-11 rounded-xl" />
      <div aria-hidden className="grid gap-3 xl:grid-cols-[17rem_minmax(0,1fr)_20rem]">
        <div className="hidden space-y-2 rounded-xl border border-border bg-surface p-3 xl:block">
          <Skeleton className="h-8" />
          {Array.from({ length: 9 }).map((_, i) => <Skeleton key={i} className="h-7" />)}
        </div>
        <div className="space-y-2 rounded-xl border border-border bg-surface p-3">
          {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-14 rounded-lg" />)}
        </div>
        <div className="hidden space-y-3 rounded-xl border border-border bg-surface p-4 xl:block">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-8" />
          <Skeleton className="h-8 w-40" />
          <Skeleton className="h-8" />
        </div>
      </div>
    </div>
  )
}

export default function ReportBuilder() {
  const [sp] = useSearchParams()
  const editId = sp.get('edit')
  const cloneId = editId ? null : sp.get('clone')
  const fromKey = editId || cloneId ? null : sp.get('from')
  const mode: DesignerMode = editId ? 'edit' : cloneId ? 'clone' : fromKey ? 'from' : 'new'
  const templateId = editId || cloneId

  const can = useCan()
  const catalogQ = useReportCatalog()
  const catalog = catalogQ.data
  const templateQ = useTemplate(templateId)
  const { branding } = useReportBranding()

  const builtin = mode === 'from' ? catalog?.types.find((t) => t.key === fromKey) ?? null : null
  const template = templateId ? templateQ.data ?? null : null

  // The draft to start from; computed once per entry (the designer keeps its own copy).
  const initial = useMemo<Draft | null>(() => {
    if (!catalog) return null
    if (mode === 'new') return emptyDraft()
    if (mode === 'from') return builtin && builtin.engine === 'sections' ? draftFromBuiltin(builtin) : null
    return template ? draftFromCustom(template, mode === 'edit' ? 'edit' : 'clone') : null
    // Recompute only when the entry changes, not when a query refetches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, fromKey, templateId, !!catalog, !!builtin, !!template])

  if (!can('reports.manage') || catalog?.can.manage === false) {
    return (
      <StateCard icon={LockKeyhole} title="Designing templates needs report management access">
        Your role can run and view reports, but creating or changing templates needs the
        {' '}<span className="font-medium text-text">Reports · Manage</span> permission (<code className="font-mono text-xs">reports.manage</code>).
        Ask an administrator, or open a built-in template from the library.
      </StateCard>
    )
  }

  if (catalogQ.isError && !catalog) {
    return (
      <div className="mx-auto mt-6 max-w-2xl">
        <QueryFailure error={catalogQ.error} what="the report catalog" onRetry={() => void catalogQ.refetch()} retrying={catalogQ.isFetching} />
      </div>
    )
  }

  if (templateId && templateQ.isError && !template) {
    const f = describeFailure(templateQ.error)
    if (f.status === 404) {
      return (
        <StateCard
          icon={FileQuestion}
          title="Template not found"
          actions={<Button asChild><Link to="/reports/builder"><FilePlus2 aria-hidden /> Start a new template</Link></Button>}
        >
          It may have been deleted, or it is private to its owner.
        </StateCard>
      )
    }
    return (
      <div className="mx-auto mt-6 max-w-2xl">
        <QueryFailure error={templateQ.error} what="this template" onRetry={() => void templateQ.refetch()} retrying={templateQ.isFetching} />
      </div>
    )
  }

  if (!catalog || (templateId && !template)) return <DesignerSkeleton />

  if (mode === 'from' && (!builtin || builtin.engine !== 'sections')) {
    return (
      <StateCard
        icon={FileQuestion}
        title={builtin ? 'This report cannot be customised' : 'Built-in template not found'}
        actions={<Button asChild><Link to="/reports/builder"><FilePlus2 aria-hidden /> Start a blank template</Link></Button>}
      >
        {builtin
          ? `“${builtin.title}” is a classic report with a fixed layout. Start a blank template and add the sections you need.`
          : 'The link names a template that is not on this appliance. Start a blank template instead.'}
      </StateCard>
    )
  }

  if (!initial) return <DesignerSkeleton />

  return (
    <TemplateDesigner
      key={`${mode}:${templateId ?? fromKey ?? ''}`}
      mode={mode}
      initial={initial}
      catalog={catalog}
      branding={branding}
      template={template}
      builtin={builtin}
    />
  )
}
