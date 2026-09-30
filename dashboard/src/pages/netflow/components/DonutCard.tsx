import { Link } from 'react-router-dom'
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import { formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { EmptyState } from './EmptyState'

// ─────────────────────────────────────────────────────────────────
// Donut card (Top Talkers / Apps / Protocols / Conversations)
// ─────────────────────────────────────────────────────────────────

function DonutCard({
  title,
  subtitle,
  data,
  colors,
  labelMode,
  compactNames,
  onSelect,
  activeName,
  viewAllTo,
}: {
  title: string
  subtitle: string
  data: { name: string; value: number }[]
  colors: string[]
  labelMode?: 'ip'
  compactNames?: boolean
  onSelect?: (name: string) => void
  activeName?: string | null
  viewAllTo?: string
}) {
  const total = data.reduce((acc, d) => acc + (d.value || 0), 0)
  return (
    <Card className="flex h-full flex-col">
      <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
        <div>
          <CardTitle className="text-sm">{title}</CardTitle>
          <p className="text-[11px] text-muted">{subtitle}</p>
        </div>
        {viewAllTo && (
          <Link to={viewAllTo} className="shrink-0 text-[11px] font-medium text-primary hover:underline">
            View all
          </Link>
        )}
      </CardHeader>
      <CardContent className="flex flex-1 flex-col pb-4">
        <div className="flex items-center gap-3">
          <div className="relative h-[120px] w-[120px] shrink-0">
            {data.length === 0 ? (
              <EmptyState compact />
            ) : (
              <>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={data}
                      dataKey="value"
                      nameKey="name"
                      innerRadius={38}
                      outerRadius={56}
                      paddingAngle={2}
                      stroke="rgb(var(--surface))"
                      strokeWidth={2}
                      onClick={onSelect ? (slice: any) => onSelect(slice?.name) : undefined}
                      cursor={onSelect ? 'pointer' : 'default'}
                    >
                      {data.map((d, i) => (
                        <Cell
                          key={i}
                          fill={colors[i % colors.length]}
                          opacity={activeName && activeName !== d.name ? 0.3 : 1}
                        />
                      ))}
                    </Pie>
                    <Tooltip
                      formatter={(value: any) => formatBytes(Number(value))}
                      contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8 }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                  <div className="text-[8px] uppercase tracking-wider text-muted">Total</div>
                  <div className="text-[11px] font-semibold leading-tight">{formatBytes(total)}</div>
                </div>
              </>
            )}
          </div>
          <div className="min-w-0 flex-1 space-y-1 text-[11px]">
            {data.slice(0, 5).map((d, i) => {
              const active = activeName === d.name
              const Row = onSelect ? 'button' : 'div'
              return (
                <Row
                  key={`${d.name}-${i}`}
                  {...(onSelect ? { onClick: () => onSelect(d.name), type: 'button' as const } : {})}
                  className={`flex w-full items-center justify-between gap-2 rounded px-1 py-0.5 text-left transition-colors ${onSelect ? 'cursor-pointer hover:bg-primary/5' : ''} ${active ? 'bg-primary/10 ring-1 ring-primary/40' : ''}`}
                >
                  <span className="flex min-w-0 items-center gap-1.5 truncate" title={d.name}>
                    <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: colors[i % colors.length] }} />
                    <span className={`truncate ${labelMode === 'ip' || compactNames ? 'font-mono text-[10px]' : ''}`}>{d.name}</span>
                  </span>
                  <span className="shrink-0 text-muted">{total ? ((d.value / total) * 100).toFixed(1) : '0.0'}%</span>
                </Row>
              )
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export { DonutCard }
