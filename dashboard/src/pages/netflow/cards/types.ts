/**
 * Overview composition contract (docs/netflow-review-contract.md §14).
 *
 * The overview page (B1) renders self-fetching cards that this folder (B2) provides. A card gets
 * the applied page filter and window, and reports clicks back through `setFilter` (click-to-filter,
 * written to the URL) or links out through `sectionLink` (drill-down that keeps filter + window).
 */
import type { FlowFilter } from '@/lib/netflow/filters'
import type { NfWindow } from '@/lib/netflow/window'

export interface OverviewContext {
  /** Applied page filter (from the URL). */
  filter: FlowFilter
  /** Resolved page window. */
  window: NfWindow
  /** Exporter deep-dive page. */
  exporter?: string
  /** Click-to-filter (writes the URL). */
  setFilter: (patch: Partial<FlowFilter>) => void
  sectionLink: (section: string, extra?: Record<string, string>) => string
}

export interface OverviewCardProps {
  ctx: OverviewContext
  compact?: boolean
}
