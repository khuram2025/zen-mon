import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronsDownUp, ChevronsUpDown, Network } from 'lucide-react'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { InventoryEmpty } from '@/components/inventory/InventoryEmpty'
import { fmtInt } from '@/components/inventory/model'
import { ExportDatasetButton } from '@/components/inventory/OsExportButton'
import { OsKindSwitch } from '@/components/inventory/OsKindSwitch'
import { OsCurrencyPanel, OsFamilyPanel } from '@/components/inventory/OsPanels'
import { QueryGate } from '@/components/inventory/OsQueryGate'
import { OsTree } from '@/components/inventory/OsTree'
import {
  OS_KIND_OPTIONS, allExpandable, buildOsRows, defaultExpanded, nextOsSort, osKindParam, parseOsKind, parseOsSort, toggleExpanded, withOsSort,
  type OsSortKey,
} from '@/components/inventory/osTreeModel'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { useCan } from '@/stores/auth'
import { exportOs, fetchOs, OSHW_KEYS } from './osHardwareChanges'
import { SUMMARY_KEY } from './useInventoryMeta'
import { inventoryApi } from './api'
import type { OsResponse } from './types'

const MINUTE = 60_000

/**
 * /inventory/os (doc 03 section 3.3): the operating systems and firmware of servers and network devices as a
 * family > product > release > version tree, a family donut and the build currency per release. Every count is a
 * link to the assets behind it and the drilled list's total equals it; unknown is a row. `?kind=` and the sort live
 * in the address bar; the expansion of the tree is a view preference and stays on the page.
 */
export function InventoryOsPage() {
  const access = useInventoryAccess()
  const allowed = access === 'allowed'
  const can = useCan()
  const [params, setParams] = useSearchParams()
  const qs = params.toString()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const { kind, rejected } = useMemo(() => parseOsKind(new URLSearchParams(qs)), [qs])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const sort = useMemo(() => parseOsSort(new URLSearchParams(qs)), [qs])

  // Same key as the Overview's OS panels (['inventory','os', kind]): one request serves both pages.
  const query = useQuery({
    queryKey: OSHW_KEYS.os(kind),
    queryFn: () => fetchOs(osKindParam(kind)),
    refetchInterval: 5 * MINUTE,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    enabled: allowed,
  })
  // Only to tell "nothing collected yet" from "your scope hides it"; shared with the meta-line.
  const summary = useQuery({ queryKey: SUMMARY_KEY, queryFn: inventoryApi.summary, refetchInterval: MINUTE, staleTime: 30_000, enabled: allowed })

  const [opened, setOpened] = useState<Set<string> | null>(null)
  const data = query.data
  const rows = useMemo(() => (data ? buildOsRows(data, kind, sort) : []), [data, kind, sort])
  const expanded = opened ?? defaultExpanded(rows)
  const noun = OS_KIND_OPTIONS.find((o) => o.value === kind)?.noun ?? 'assets'

  const onSort = (key: OsSortKey) => setParams(withOsSort(new URLSearchParams(qs), nextOsSort(sort, key)), { replace: true })

  if (access === 'denied') return null // the layout's gate has already said so

  return (
    <div className="space-y-4" data-page="os">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <OsKindSwitch kind={kind} params={new URLSearchParams(qs)} label="Kind of asset" />
        <p className="min-w-[14rem] flex-1 text-xs text-muted">
          Family, release and version. Every count opens the assets behind it; unknown is a row of its own.
        </p>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" size="sm" className="h-9" disabled={rows.length === 0} onClick={() => setOpened(allExpandable(rows))}>
            <ChevronsUpDown className="h-4 w-4" aria-hidden /> <span className="hidden sm:inline">Expand all</span><span className="sr-only sm:hidden">Expand all</span>
          </Button>
          <Button type="button" variant="outline" size="sm" className="h-9" disabled={rows.length === 0} onClick={() => setOpened(new Set())}>
            <ChevronsDownUp className="h-4 w-4" aria-hidden /> <span className="hidden sm:inline">Collapse all</span><span className="sr-only sm:hidden">Collapse all</span>
          </Button>
          {can('inventory.export') && (
            <ExportDatasetButton run={() => exportOs(osKindParam(kind))} filename={`inventory-os-${kind}`} noun="versions" />
          )}
        </div>
      </div>
      {rejected && (
        <p role="status" className="flex items-start gap-1.5 rounded-md border border-warning/35 bg-warning/10 px-3 py-1.5 text-xs text-warning-fg">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>Ignored in the address: {rejected}. Showing all assets.</span>
        </p>
      )}
      <QueryGate query={query} what="the operating system breakdown" skeleton={<OsSkeleton />}>
        {(os: OsResponse) => {
          if (os.total === 0) {
            const scoped = summary.data?.scope.restricted
            return (
              <Card>
                {scoped ? (
                  <InventoryEmpty kind="no-scope" title={`No ${noun} in your scope`}>
                    Your role limits Inventory to assets tagged {summary.data?.scope.tags.join(', ') || 'for your team'}, and none of those {noun} has reported an operating system yet.
                  </InventoryEmpty>
                ) : (
                  <InventoryEmpty kind="no-data" title={kind === 'device' ? 'No network devices yet' : kind === 'server' ? 'No servers yet' : 'No assets yet'}>
                    {kind === 'device'
                      ? 'Devices appear here once the poller has read their firmware. This page fills in by itself.'
                      : kind === 'server'
                        ? 'Servers appear here once their agent reports an operating system. This page fills in by itself.'
                        : 'Servers appear once their agent reports, and network devices once the poller has read them. This page fills in by itself.'}
                  </InventoryEmpty>
                )}
              </Card>
            )
          }
          return (
            <div className={query.isPlaceholderData ? 'opacity-60 transition-opacity' : 'transition-opacity'} aria-busy={query.isFetching}>
              <div className="grid items-start gap-4 lg:grid-cols-3">
                <Card className="min-w-0 overflow-hidden lg:col-span-2">
                  <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
                    <h2 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
                      <Network className="h-4 w-4 text-muted" aria-hidden /> Operating systems and firmware
                    </h2>
                    <span className="text-xs text-muted" role="status">
                      <strong className="font-semibold tabular-nums text-text2">{fmtInt(os.total)}</strong> {os.total === 1 ? noun.replace(/s$/, '') : noun}
                    </span>
                  </div>
                  <OsTree
                    rows={rows}
                    expanded={expanded}
                    onToggle={(key) => setOpened(toggleExpanded(expanded, key))}
                    sort={sort}
                    onSort={onSort}
                    total={os.total}
                    noun={noun}
                    caption={`Operating systems of ${fmtInt(os.total)} ${noun}: family, product, release and version, with the number of assets on each`}
                  />
                </Card>
                <div className="min-w-0 space-y-4">
                  <OsFamilyPanel os={os} kind={kind} />
                  <OsCurrencyPanel os={os} kind={kind} />
                </div>
              </div>
            </div>
          )
        }}
      </QueryGate>
    </div>
  )
}

function OsSkeleton() {
  return (
    <div className="grid items-start gap-4 lg:grid-cols-3" aria-busy="true" aria-label="Loading the operating system breakdown">
      <Card className="space-y-2 p-4 lg:col-span-2">
        {Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="h-7 w-full" />)}
      </Card>
      <div className="space-y-4">
        <Skeleton className="h-64 w-full rounded-lg" />
        <Skeleton className="h-48 w-full rounded-lg" />
      </div>
    </div>
  )
}
