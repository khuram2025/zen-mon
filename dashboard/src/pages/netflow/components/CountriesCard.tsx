import { RadioTower } from 'lucide-react'
import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import type { Country } from '../types'
import { flagEmoji } from '../model'

function CountriesCard({ data }: { data: Country[] }) {
  // Endpoint returns [] when no GeoIP database is provisioned — hide entirely.
  if (data.length === 0) return null
  const top = data.slice(0, 6)
  const max = Math.max(1, ...top.map((c) => c.bytes))
  const total = Math.max(1, data.reduce((a, b) => a + b.bytes, 0))
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <RadioTower className="h-4 w-4 text-primary" />
        <div>
          <CardTitle className="text-sm">Top Countries</CardTitle>
          <p className="text-[11px] text-muted">GeoIP on top endpoint IPs · src + dst bytes</p>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {top.map((c) => (
          <div key={c.country || 'unknown'}>
            <div className="flex items-center justify-between gap-2 text-[11px]">
              <span className="flex min-w-0 items-center gap-1.5 truncate">
                <span>{flagEmoji(c.country)}</span>
                <span className="truncate">{c.country_name || 'Private / Unknown'}</span>
              </span>
              <span className="shrink-0 text-muted">{formatBytes(c.bytes)} · {((c.bytes / total) * 100).toFixed(1)}%</span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface">
              <div className="h-full rounded-full bg-primary" style={{ width: `${Math.max(3, (c.bytes / max) * 100)}%` }} />
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

export { CountriesCard }
