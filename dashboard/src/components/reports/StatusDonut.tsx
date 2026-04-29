import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts'

interface DonutSlice {
  label: string
  value: number
  color: string
}

interface StatusDonutProps {
  data: DonutSlice[]
  centerValue?: string
  centerLabel?: string
  height?: number
}

export function StatusDonut({ data, centerValue, centerLabel, height = 220 }: StatusDonutProps) {
  const total = data.reduce((s, d) => s + d.value, 0)
  if (total === 0) {
    return (
      <div className="flex items-center justify-center text-sm text-muted" style={{ height }}>
        No data
      </div>
    )
  }
  return (
    <div className="relative" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            dataKey="value"
            nameKey="label"
            innerRadius="60%"
            outerRadius="85%"
            paddingAngle={2}
            stroke="transparent"
          >
            {data.map((d, i) => (
              <Cell key={i} fill={d.color} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={{
              background: 'var(--bg-surface, #1A1D27)',
              border: '1px solid var(--bg-elevated, #2D3140)',
              borderRadius: 6,
              fontSize: 12,
              padding: '6px 10px',
            }}
            formatter={(value: number, name: string) => [`${value} (${((value / total) * 100).toFixed(1)}%)`, name]}
          />
        </PieChart>
      </ResponsiveContainer>
      {(centerValue || centerLabel) && (
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          {centerValue && <span className="text-2xl font-bold tracking-tight">{centerValue}</span>}
          {centerLabel && <span className="mt-0.5 text-[10px] font-medium uppercase tracking-wider text-muted">{centerLabel}</span>}
        </div>
      )}
    </div>
  )
}

export function DonutLegend({ data }: { data: DonutSlice[] }) {
  const total = data.reduce((s, d) => s + d.value, 0)
  return (
    <div className="grid grid-cols-2 gap-2 text-xs">
      {data.map((d) => (
        <div key={d.label} className="flex items-center gap-1.5">
          <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: d.color }} />
          <span className="truncate text-muted">{d.label}</span>
          <span className="ml-auto font-medium">{d.value}</span>
          <span className="text-[10px] text-muted">{total ? `${((d.value / total) * 100).toFixed(0)}%` : ''}</span>
        </div>
      ))}
    </div>
  )
}
