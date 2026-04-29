/**
 * Tiny SVG sparkline used inside KPI cards.
 */
export function Sparkline({
  values,
  color = '#22d3ee',
  width = 90,
  height = 28,
  strokeWidth = 1.6,
  fill = true,
}: {
  values: number[]
  color?: string
  width?: number
  height?: number
  strokeWidth?: number
  fill?: boolean
}) {
  if (!values || values.length < 2) {
    values = [0, 0]
  }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const stepX = width / (values.length - 1)
  const pts = values.map((v, i) => {
    const x = i * stepX
    const y = height - ((v - min) / span) * (height - 4) - 2
    return `${x},${y}`
  })
  const line = `M ${pts.join(' L ')}`
  const area = `${line} L ${width},${height} L 0,${height} Z`
  const id = `spark-${color.replace('#', '')}`

  return (
    <svg width={width} height={height} className="overflow-visible">
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.45" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill && <path d={area} fill={`url(#${id})`} />}
      <path d={line} stroke={color} strokeWidth={strokeWidth}
            strokeLinejoin="round" strokeLinecap="round" fill="none" />
    </svg>
  )
}
