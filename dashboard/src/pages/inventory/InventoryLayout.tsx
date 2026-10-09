import { Suspense, useEffect, useRef } from 'react'
import { ArrowLeft, PackageSearch } from 'lucide-react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { DataStatusBanner } from '@/components/inventory/DataStatusBanner'
import { InventoryKbLink, kbArticleForPath } from '@/components/inventory/InventoryKbLink'
import { InventoryMetaLine } from '@/components/inventory/InventoryMetaLine'
import { NAV_GROUPS, type NavNode } from '@/components/layout/navigation'
import { filterNavGroups, type Can } from '@/components/layout/navAccess'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/Skeleton'
import { useCan } from '@/stores/auth'
import { useInventoryMeta, useRefreshWhenNormalised } from './useInventoryMeta'

/**
 * The tab strip mirrors the sidebar rows of the Inventory group, so the two never drift: same labels,
 * same order, and a role only gets the tabs it may open (Inventory Settings needs inventory.view; saving needs inventory.manage).
 */
export function inventoryTabs(can: Can): NavNode[] {
  return filterNavGroups(NAV_GROUPS, can).find((g) => g.id === 'inventory')?.items ?? []
}

/** The product page (`/inventory/software/:productKey`) owns the full canvas: no heading, no tab strip. */
export function isProductPath(pathname: string): boolean {
  return /^\/inventory\/software\/[^/]+/.test(pathname)
}

/**
 * The key of the Suspense boundary around the page: one per page, so moving to another page mounts a NEW boundary.
 * A boundary that stays mounted keeps showing the old page while the next page's chunk downloads (a router
 * transition never hides content that is already on screen), with the address and nothing else changed; a new
 * boundary shows the skeleton at once (review finding NF-09). Products share one key: moving between two
 * products is not a page change.
 */
export function pageBoundaryKey(pathname: string): string {
  const path = pathname.replace(/\/+$/, '').toLowerCase() || '/'
  return isProductPath(path) ? '/inventory/software/:product' : path
}

/** What stands in the page's place while its chunk downloads: the shape of a page (a row of figures and two panels). */
export function PageFallback() {
  return (
    <div className="space-y-4" role="status" aria-busy="true" aria-label="Loading the page" data-page-fallback="">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-[104px] w-full rounded-lg" />)}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-64 w-full rounded-lg" />
        <Skeleton className="h-64 w-full rounded-lg" />
      </div>
    </div>
  )
}

/**
 * Shell of every /inventory page: heading, documentation link, tab strip, the meta-line with the age
 * of the data, then the page. Follows UdtLayout.
 */
export function InventoryLayout() {
  const { pathname, search } = useLocation()
  const can = useCan()
  const meta = useInventoryMeta()
  // The data pages refetch only every few minutes: when the backlog clears, refetch them now.
  useRefreshWhenNormalised(meta.summary?.data_status?.normalising)
  const product = isProductPath(pathname)
  const tabs = inventoryTabs(can)
  const article = kbArticleForPath(pathname)

  // On a narrow screen the strip scrolls sideways; bring the current tab into view so a deep link
  // (or a Back) never lands on a strip that hides which page this is. Horizontal only: the page
  // itself must not jump.
  const strip = useRef<HTMLElement>(null)
  useEffect(() => {
    const nav = strip.current
    const active = nav?.querySelector<HTMLElement>('[aria-current="page"]')
    if (!nav || !active) return
    const a = active.getBoundingClientRect()
    const n = nav.getBoundingClientRect()
    nav.scrollLeft += a.left - n.left - (n.width - a.width) / 2
  }, [pathname, tabs.length])

  const metaLine = (
    <InventoryMetaLine
      summary={meta.summary}
      loading={meta.loading}
      failed={meta.failed}
      onRetry={meta.refetch}
      scopeTags={meta.scopeTags}
    />
  )

  return (
    <div className="space-y-4">
      {product ? (
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <Link
            to="/inventory/software"
            className="inline-flex items-center gap-1.5 rounded text-sm font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden /> Software
          </Link>
          {metaLine}
        </div>
      ) : (
        <>
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <PackageSearch className="h-6 w-6 text-primary" aria-hidden />
              Inventory
              <InventoryKbLink article={article} label="Inventory documentation" />
            </h1>
            <p className="mt-1 text-xs text-muted">
              Every operating system, software title and hardware model across servers and network devices.
            </p>
          </div>
          <nav ref={strip} aria-label="Inventory sections" className="-mb-px flex items-center gap-1 overflow-x-auto border-b border-border">
            {tabs.map((t) => (
              <NavLink
                key={t.to}
                to={t.to}
                end={t.end}
                className={({ isActive }) =>
                  cn(
                    'inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary',
                    isActive ? 'border-primary text-text' : 'border-transparent text-muted hover:text-text',
                  )
                }
              >
                <t.icon className="h-4 w-4" aria-hidden />
                {t.label}
              </NavLink>
            ))}
          </nav>
          {metaLine}
        </>
      )}
      {/* While existing software lists are still being normalised every software-derived figure is partial: say so on every page. */}
      <DataStatusBanner status={meta.summary?.data_status} />
      {/* The pages are lazy chunks (App.tsx): the shell above stays on screen while one loads. */}
      {/* A page whose chunk cannot be downloaded fails here, with the tabs still usable (and its own recovery notice). */}
      <ErrorBoundary resetKey={`${pathname}${search}`}>
        <Suspense key={pageBoundaryKey(pathname)} fallback={<PageFallback />}>
          <Outlet />
        </Suspense>
      </ErrorBoundary>
    </div>
  )
}
