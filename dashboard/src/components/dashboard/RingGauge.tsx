/**
 * Animated SVG ring gauge — used for "Device Availability" tiles
 * and the central Health Status donut.
 */
type Color = 'success' | 'warning' | 'danger' | 'info' | 'primary' | 'accent'

const COLOR_STOPS: Record<Color, [string, string]> = {
  success: ['#22c55e', '#10b981'],
  warning: ['#f59e0b', '#f97316'],
  danger:  ['#ef4444', '#f43f5e'],
  info:    ['#22d3ee', '#0ea5e9'],
  primary: ['#3b82f6', '#6366f1'],
  accent:  ['#a855f7', '#ec4899'],
}

export function RingGauge({
  value,
  size = 130,
  stroke = 11,
  color = 'success',
  label,
  sub,
  centerLabel,
}: {
  value: number          // 0..100
  size?: number
  stroke?: number
  color?: Color
  label?: string
  sub?: string
  centerLabel?: string
}) {
  const v = Math.max(0, Math.min(100, value))
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const offset = c * (1 - v / 100)
  const [c1, c2] = COLOR_STOPS[color]
  const id = `ring-${color}-${size}-${stroke}`

  return (
    <div className="flex flex-col items-center gap-1.5">
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor={c1} />
              <stop offset="100%" stopColor={c2} />
            </linearGradient>
          </defs>
          <circle cx={size / 2} cy={size / 2} r={r}
                  fill="none"
                  stroke="rgb(var(--surface3))"
                  strokeWidth={stroke} />
          <circle cx={size / 2} cy={size / 2} r={r}
                  fill="none"
                  stroke={`url(#${id})`}
                  strokeWidth={stroke}
                  strokeLinecap="round"
                  strokeDasharray={c}
                  strokeDashoffset={offset}
                  transform={`rotate(-90 ${size / 2} ${size / 2})`}
                  style={{ transition: 'stroke-dashoffset 800ms ease' }} />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <div className="text-xl font-semibold tabular-nums">
            {centerLabel ?? `${v.toFixed(1).replace(/\.0$/, '')}%`}
          </div>
          {sub && <div className="mt-0.5 text-[10px] uppercase tracking-wider text-muted">{sub}</div>}
        </div>
      </div>
      {label && <div className="text-xs font-medium text-text2">{label}</div>}
    </div>
  )
}
