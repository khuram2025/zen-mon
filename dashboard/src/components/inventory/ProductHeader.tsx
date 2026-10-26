import { Link } from 'react-router-dom'
import { Card } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'
import type { ProductResponse } from '@/pages/inventory/softwareSettings'
import { productFacts, productKpis, tabHref, type ProductKpi } from './productModel'
import { categoryLabel, firstSeenView, normStatusMeta, shortDate } from './softwareModel'

const focus = 'outline-none focus-visible:ring-2 focus-visible:ring-primary'

/** One figure of the product page. A figure with a list behind it is a link (the whole tile); one without says why in its tooltip. */
function KpiTile({ kpi }: { kpi: ProductKpi }) {
  const body = (
    <>
      <div className="text-xs font-semibold uppercase tracking-wider text-muted">{kpi.label}</div>
      <div className={cn('mt-1 truncate font-bold leading-none tabular-nums text-text', kpi.mono ? 'font-mono text-xl' : 'text-2xl')} title={kpi.mono ? kpi.value : undefined}>
        {kpi.href ? (
          <Link
            to={kpi.href}
            data-drill={kpi.id === 'versions' ? 'page' : 'assets'}
            data-drill-count={kpi.count ?? undefined}
            aria-label={`${kpi.label}: ${kpi.value}. ${kpi.sub}`}
            className={cn('rounded-sm after:absolute after:inset-0 after:content-[""] hover:no-underline group-hover:underline', focus)}
          >
            {kpi.value}
          </Link>
        ) : (
          kpi.value
        )}
      </div>
      <div className="mt-1.5 text-xs leading-4 text-muted">{kpi.sub}</div>
    </>
  )
  return (
    <Card
      data-kpi={kpi.id}
      title={kpi.href ? undefined : kpi.note}
      className={cn('group relative min-w-0 px-4 py-3', kpi.href && 'transition hover:border-primary/40 focus-within:border-primary/60')}
    >
      {body}
    </Card>
  )
}

/** Title, normalisation status, publisher and category, the fact line and the four figures. */
export function ProductHeader({ data, historyStart }: { data: Pick<ProductResponse, 'header' | 'versions' | 'unversioned'>; historyStart?: string | null }) {
  const { header } = data
  const status = normStatusMeta(header.norm_status)
  const kpis = productKpis(data)
  const facts = productFacts(header, shortDate, historyStart)
  // A product that was there when recording began has no known first-seen day: the tooltip says what the date means.
  const firstSeenTitle = firstSeenView(header.first_seen, historyStart).title
  return (
    <header className="space-y-3" data-product-header="">
      <div className="space-y-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{header.name}</h1>
          <span title={status.meaning} className={cn('inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium', status.tone)}>{status.label}</span>
        </div>
        <p className="text-sm text-text2">
          {header.publisher ?? <span className="italic text-muted">Unknown publisher</span>}
          <span aria-hidden className="mx-1.5 text-muted/60">·</span>
          {categoryLabel(header.category)}
          <span aria-hidden className="mx-1.5 text-muted/60">·</span>
          <code className="rounded bg-surface2 px-1 py-0.5 font-mono text-xs text-text2" title="The product key: the address of this page and the value of the product filter">{header.product_key}</code>
        </p>
        <p className="text-xs text-muted">
          <span title={firstSeenTitle} data-first-seen={firstSeenTitle && historyStart ? 'note' : undefined}>{facts.firstSeen}</span> ·{' '}
          {facts.lastChange ? (
            <Link to={tabHref(header.product_key, 'timeline')} className={cn('rounded font-medium text-primary hover:underline', focus)}>{facts.lastChange}</Link>
          ) : (
            'no change recorded yet'
          )}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {kpis.map((k) => <KpiTile key={k.id} kpi={k} />)}
      </div>
    </header>
  )
}

export function ProductHeaderSkeleton() {
  return (
    <div className="space-y-3" aria-busy="true" aria-label="Loading product">
      <div className="space-y-2">
        <Skeleton className="h-7 w-72 max-w-full" />
        <Skeleton className="h-4 w-96 max-w-full" />
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[92px] w-full" />)}
      </div>
      <Skeleton className="h-10 w-80 max-w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  )
}
