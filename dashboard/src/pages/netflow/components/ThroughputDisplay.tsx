import { formatBps } from '@/lib/utils'

function ThroughputDisplay({ bps }: { bps: number }) {
  // Split formatted "1.23 Gbps" into number + unit so the number can be huge.
  const formatted = formatBps(bps)
  const m = formatted.match(/^([\d.,]+)\s*(.*)$/)
  const num = m?.[1] || formatted
  const unit = m?.[2] || ''
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-4xl font-semibold leading-none tracking-tight text-text">{num}</span>
      {unit && <span className="text-base font-medium text-muted">{unit}</span>}
    </div>
  )
}

export { ThroughputDisplay }
