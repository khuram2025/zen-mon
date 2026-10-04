import { TalkersCardBody } from './TopTalkersCard'
import type { OverviewCardProps } from './types'

/**
 * Endpoints by all the traffic they took part in, with the sent/received split. A flow counts
 * for both of its endpoints, so the shares overlap: ranked bars, never a pie (NF-25).
 */
export function TopEndpointsCard({ ctx, compact }: OverviewCardProps) {
  return <TalkersCardBody ctx={ctx} compact={compact} by="total" section="top-endpoints" title="Top endpoints" />
}
