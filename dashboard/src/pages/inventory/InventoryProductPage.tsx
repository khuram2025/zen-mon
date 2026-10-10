import { useMemo } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useBreadcrumbDetail } from '@/components/layout/breadcrumbDetail'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { ProductEvidenceTab } from '@/components/inventory/ProductEvidenceTab'
import { ProductHeader, ProductHeaderSkeleton } from '@/components/inventory/ProductHeader'
import { ProductInstalledTab } from '@/components/inventory/ProductInstalledTab'
import { ProductNotFound } from '@/components/inventory/ProductNotFound'
import { ProductTimelineTab } from '@/components/inventory/ProductTimelineTab'
import { ProductVersionsTab } from '@/components/inventory/ProductVersionsTab'
import { isNotFound, PRODUCT_TABS, TAB_LABELS, parseProductTab, withProductTab, type ProductTab } from '@/components/inventory/productModel'
import { useSoftwareHistoryStart } from '@/components/inventory/useSoftwareHistoryStart'
import { QueryFailure } from '@/components/ui/QueryState'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { useCan } from '@/stores/auth'
import { softwareApi, softwareKeys } from './softwareSettings'

/**
 * /inventory/software/:productKey (doc 03 section 3.5): one product on the whole canvas (the layout shows the
 * back link). The header and the four figures, then the tabs bound to `?tab=`: Versions, Installed on, Timeline
 * and Evidence. A product that is unknown, or has no host in the caller's scope, answers 404 with one body for
 * both, and the page shows one designed not-found state.
 */
export function InventoryProductPage() {
  const { productKey = '' } = useParams()
  const access = useInventoryAccess()
  const allowed = access === 'allowed'
  const can = useCan()
  const [params, setParams] = useSearchParams()
  const tab = useMemo(() => parseProductTab(params.get('tab')), [params])

  const product = useQuery({
    queryKey: softwareKeys.product(productKey),
    queryFn: () => softwareApi.product(productKey),
    staleTime: 60_000,
    refetchInterval: 300_000,
    enabled: allowed && productKey !== '',
    // A product that is not there will not appear on a retry.
    retry: (count, error) => !isNotFound(error) && count < 1,
  })

  const historyStart = useSoftwareHistoryStart()
  // The header's last crumb names the product ("Software > Google Chrome"), not "Detail" (finding (m)).
  useBreadcrumbDetail(product.data?.header.name)

  if (access === 'denied') return null // the layout's gate has already said so
  if (isNotFound(product.error)) return <ProductNotFound productKey={productKey} />
  if (product.isError && !product.data) {
    return <QueryFailure error={product.error} what="this product" onRetry={() => void product.refetch()} retrying={product.isFetching} />
  }
  const data = product.data
  if (!data) return <ProductHeaderSkeleton />

  return (
    <div className="space-y-4" data-page="product" data-product={data.header.product_key}>
      {product.isError && (
        <div>
          <QueryFailure compact what="this product" error={product.error} onRetry={() => void product.refetch()} retrying={product.isFetching} />
          <p className="mt-1 text-[11px] text-muted">Showing the last data that loaded; it may be out of date.</p>
        </div>
      )}
      <ProductHeader data={data} historyStart={historyStart} />
      <Tabs value={tab} onValueChange={(t) => setParams(withProductTab(params, t as ProductTab))}>
        <div className="overflow-x-auto">
          <TabsList aria-label="Product sections">
            {PRODUCT_TABS.map((t) => <TabsTrigger key={t} value={t}>{TAB_LABELS[t]}</TabsTrigger>)}
          </TabsList>
        </div>
        <TabsContent value="versions"><ProductVersionsTab productKey={data.header.product_key} data={data} /></TabsContent>
        <TabsContent value="installed">
          <ProductInstalledTab productKey={data.header.product_key} product={data} canExport={can('inventory.export')} enabled={allowed} historyStart={historyStart} />
        </TabsContent>
        <TabsContent value="timeline"><ProductTimelineTab productKey={data.header.product_key} product={data} enabled={allowed} /></TabsContent>
        <TabsContent value="evidence"><ProductEvidenceTab data={data} /></TabsContent>
      </Tabs>
    </div>
  )
}
