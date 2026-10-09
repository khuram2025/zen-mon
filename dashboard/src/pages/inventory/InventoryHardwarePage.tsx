import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'

import { chooseMatrixVendor, matrixVendors, modelCountText } from '@/components/inventory/hardwareModel'
import { HardwareMatrixPanel, HardwareModulesPanel, HardwareNetworkPanel, HardwareServersPanel } from '@/components/inventory/HardwarePanels'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { InventoryEmpty } from '@/components/inventory/InventoryEmpty'
import { fmtInt } from '@/components/inventory/model'
import { ExportDatasetButton } from '@/components/inventory/OsExportButton'
import { QueryGate } from '@/components/inventory/OsQueryGate'
import { DrillLink } from '@/components/inventory/DrillLink'
import { Card } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { useCan } from '@/stores/auth'
import { inventoryApi } from './api'
import { exportHardware, fetchHardware, OSHW_KEYS } from './osHardwareChanges'
import { SUMMARY_KEY } from './useInventoryMeta'
import type { HardwareResponse } from './types'

const MINUTE = 60_000

/**
 * /inventory/hardware (doc 03 section 3.6): network vendors and models with serial coverage, the models x
 * firmware matrix of one vendor (`?vendor=`), ENTITY-MIB module groups, and the server CPU, memory and disk
 * distributions. Every count links to the devices or servers behind it. What arrives with the agent's hardware
 * identity report in a later phase is said in words, not faked. The matrix has its own request, so it can fail
 * or load without taking the rest of the page with it.
 */
export function InventoryHardwarePage() {
  const access = useInventoryAccess()
  const allowed = access === 'allowed'
  const can = useCan()
  const [params, setParams] = useSearchParams()

  // Same key as the Overview's hardware panel: one request serves both pages.
  const base = useQuery({
    queryKey: OSHW_KEYS.hardware,
    queryFn: () => fetchHardware(),
    refetchInterval: 5 * MINUTE,
    staleTime: 60_000,
    enabled: allowed,
  })
  const summary = useQuery({ queryKey: SUMMARY_KEY, queryFn: inventoryApi.summary, refetchInterval: MINUTE, staleTime: 30_000, enabled: allowed })

  const vendors = useMemo(() => (base.data ? matrixVendors(base.data) : []), [base.data])
  const vendor = chooseMatrixVendor(params.get('vendor'), base.data)
  const matrix = useQuery({
    queryKey: OSHW_KEYS.hardwareVendor(vendor ?? ''),
    queryFn: () => fetchHardware(vendor as string),
    refetchInterval: 5 * MINUTE,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    enabled: allowed && base.data !== undefined && vendor !== null,
  })
  const pickVendor = (v: string) => {
    const next = new URLSearchParams(params)
    next.set('vendor', v)
    setParams(next, { replace: false })
  }

  if (access === 'denied') return null // the layout's gate has already said so

  return (
    <div className="space-y-4" data-page="hardware">
      <QueryGate query={base} what="the hardware breakdown" skeleton={<HardwareSkeleton />}>
        {(hw: HardwareResponse) => {
          const empty = hw.network.devices === 0 && hw.servers.total === 0
          if (empty) {
            return (
              <Card>
                {summary.data?.scope.restricted ? (
                  <InventoryEmpty kind="no-scope" title="No hardware in your scope">Your role limits Inventory to assets tagged {summary.data.scope.tags.join(', ') || 'for your team'}, and none of them has reported hardware yet.</InventoryEmpty>
                ) : (
                  <InventoryEmpty kind="no-data" title="No hardware reported yet">Network devices appear once the poller has read them, and server CPU, memory and disks once an agent reports. This page fills in by itself.</InventoryEmpty>
                )}
              </Card>
            )
          }
          return (
            <>
              <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
                <p className="text-xs text-muted" role="status">
                  <DrillLink filters={{ kind: 'device' }} count={hw.network.devices} className="font-medium text-text2">{fmtInt(hw.network.devices)} network devices</DrillLink>
                  {' · '}{fmtInt(hw.network.vendors.filter((v) => v.vendor !== null).length)} vendors, {modelCountText(hw.network.models_known, hw.network.unknown_model_devices)}
                  {' · '}<DrillLink filters={{ kind: 'server' }} count={hw.servers.total} className="font-medium text-text2">{fmtInt(hw.servers.total)} servers</DrillLink>
                </p>
                {can('inventory.export') && <ExportDatasetButton run={() => exportHardware()} filename="inventory-hardware" noun="rows" />}
              </div>
              <HardwareNetworkPanel hw={hw} onMatrix={pickVendor} matrixVendor={vendor} />
              {/* Side by side only where the matrix (about 560 px with eight releases) fits its half; below that it gets the full row. */}
              <div className="grid items-start gap-4 2xl:grid-cols-2">
                <HardwareMatrixPanel query={matrix} vendors={vendors} vendor={vendor} onVendor={pickVendor} />
                <HardwareModulesPanel hw={hw} />
              </div>
              <HardwareServersPanel hw={hw} />
            </>
          )
        }}
      </QueryGate>
    </div>
  )
}

function HardwareSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading the hardware breakdown">
      <Card className="space-y-2 p-4">{Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-7 w-full" />)}</Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-64 w-full rounded-lg" />
        <Skeleton className="h-64 w-full rounded-lg" />
      </div>
      <Skeleton className="h-48 w-full rounded-lg" />
    </div>
  )
}

