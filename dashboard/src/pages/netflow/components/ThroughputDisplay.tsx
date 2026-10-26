import { formatBps, formatBpsExact } from '@/lib/netflow/format'

/**
 * A large bit rate with its unit kept visible ("39.1 Mbps"). `null` renders "—" (unknown), never
 * "0 bps"; with `unknownReason` it says "Unknown" and why (no records received, expired). The label
 * says which rate it is (recent vs end of a historical window).
 */
function ThroughputDisplay({ bps, label, hint, unknownReason }: { bps: number | null | undefined; label?: string; hint?: string; unknownReason?: string | null }) {
  if (bps == null && unknownReason) {
    return (
      <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5" title={hint}>
        <span className="text-3xl font-semibold leading-none text-text2">Unknown</span>
        <span className="text-xs text-text2">— {unknownReason.charAt(0).toLowerCase() + unknownReason.slice(1)}</span>
        {label && <span className="text-xs font-medium uppercase tracking-wide text-text2">{label}</span>}
      </div>
    )
  }
  const formatted = formatBps(bps)
  const m = /^([<\d.,]+)\s*(.*)$/.exec(formatted)
  const num = m?.[1] || formatted
  const unit = m?.[2] || ''
  return (
    <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5" title={bps == null ? hint : `${formatBpsExact(bps)}${hint ? ` — ${hint}` : ''}`}>
      <span className="text-3xl font-semibold leading-none tabular-nums text-text">{num}</span>
      {unit && <span className="text-base font-medium text-text2">{unit}</span>}
      {label && <span className="text-xs font-medium uppercase tracking-wide text-text2">{label}</span>}
    </div>
  )
}

export { ThroughputDisplay }
