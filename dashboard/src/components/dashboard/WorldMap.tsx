import { useMemo } from 'react'

/**
 * Stylised dot-grid world map with colored location pins and arc connectors.
 * Pure inline SVG — no external assets, no data deps.
 */
export type MapLocation = {
  name: string
  x: number   // 0..100 (% of viewBox width)
  y: number   // 0..100 (% of viewBox height)
  status: 'up' | 'warn' | 'down'
}

const DEFAULT_LOCATIONS: MapLocation[] = [
  { name: 'San Francisco', x: 14, y: 38, status: 'up' },
  { name: 'New York',      x: 26, y: 35, status: 'up' },
  { name: 'São Paulo',     x: 33, y: 70, status: 'up' },
  { name: 'London',        x: 48, y: 30, status: 'warn' },
  { name: 'Frankfurt',     x: 51, y: 33, status: 'up' },
  { name: 'Riyadh',        x: 60, y: 47, status: 'up' },
  { name: 'Mumbai',        x: 67, y: 50, status: 'up' },
  { name: 'Singapore',     x: 76, y: 60, status: 'up' },
  { name: 'Tokyo',         x: 87, y: 40, status: 'up' },
  { name: 'Sydney',        x: 90, y: 78, status: 'up' },
]

const ARCS: Array<[number, number]> = [
  [1, 3], [1, 0], [3, 4], [3, 5], [5, 6], [6, 7], [7, 8], [7, 9], [4, 1], [0, 8],
]

const STATUS_COLOR: Record<MapLocation['status'], string> = {
  up: '#22c55e',
  warn: '#eab308',
  down: '#ef4444',
}

/**
 * Land-mask grid. 1 = land, 0 = water.
 * Hand-tuned 60×30 mask that approximates the major continents.
 */
const LAND_MASK: string[] = [
  '............................................................',
  '...11111111111111111....11111111111111111111111111..........',
  '..1111111111111111111...11111111111111111111111111111111....',
  '..111111111111111111111.11111111111111111111111111111111111.',
  '..1111111111111111111....1111111111111111111111111111111111.',
  '..1111111111111111111....11111111111111111111111111111111111',
  '....111111111111111........1111111111111111111111111111111..',
  '......1111111111111..........1111111111111111111111111.....',
  '........1111111111............11111111111111111111.........',
  '.........11111111...............111111111111111111.........',
  '..........11111....................11111.111111111.........',
  '...........111....................1111111.111111111........',
  '............11....................1111111111111............',
  '.............11...................1111111111..............',
  '..............11.....................11111................',
  '.............11.........................1111..............',
  '............11.........................11111111...........',
  '...........11.........................11111111111.........',
  '...........1.........................11111111111111.......',
  '..........1..........................11111111111111111....',
  '..........1.........................1111111111111111111...',
  '..........1..........................11111111111111111....',
  '...........1..........................1111111111.111......',
  '............1...........................1.111111..........',
  '.............1.............................11111..........',
  '..............1.............................1.............',
  '...........................................11.........11..',
  '...........................................1...........11.',
  '............................................1..........1..',
  '............................................................',
]

export function WorldMap({ locations = DEFAULT_LOCATIONS }: { locations?: MapLocation[] }) {
  const dots = useMemo(() => {
    const out: Array<{ x: number; y: number }> = []
    const cols = LAND_MASK[0].length
    const rows = LAND_MASK.length
    for (let r = 0; r < rows; r++) {
      const row = LAND_MASK[r]
      for (let c = 0; c < cols; c++) {
        if (row[c] === '1') {
          out.push({
            x: (c / (cols - 1)) * 100,
            y: 4 + (r / (rows - 1)) * 72,
          })
        }
      }
    }
    return out
  }, [])

  const arcs = useMemo(() => {
    return ARCS.map(([a, b]) => {
      const p1 = locations[a % locations.length]
      const p2 = locations[b % locations.length]
      if (!p1 || !p2) return null
      const mx = (p1.x + p2.x) / 2
      const my = Math.min(p1.y, p2.y) - 12
      return { d: `M ${p1.x},${p1.y} Q ${mx},${my} ${p2.x},${p2.y}`, key: `${a}-${b}` }
    }).filter(Boolean) as Array<{ d: string; key: string }>
  }, [locations])

  return (
    <div className="relative h-full w-full">
      <svg
        viewBox="0 0 100 80"
        preserveAspectRatio="xMidYMid meet"
        className="h-full w-full"
      >
        <defs>
          <radialGradient id="map-bg" cx="50%" cy="50%" r="65%">
            <stop offset="0%" stopColor="#0ea5e9" stopOpacity="0.15" />
            <stop offset="100%" stopColor="#0ea5e9" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="arc-grad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#22d3ee" stopOpacity="0" />
            <stop offset="50%" stopColor="#22d3ee" stopOpacity="0.95" />
            <stop offset="100%" stopColor="#22d3ee" stopOpacity="0" />
          </linearGradient>
          <radialGradient id="pin-glow-up" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#22c55e" stopOpacity="0.6" />
            <stop offset="100%" stopColor="#22c55e" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="pin-glow-warn" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#eab308" stopOpacity="0.6" />
            <stop offset="100%" stopColor="#eab308" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="pin-glow-down" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#ef4444" stopOpacity="0.6" />
            <stop offset="100%" stopColor="#ef4444" stopOpacity="0" />
          </radialGradient>
        </defs>

        <rect x="0" y="0" width="100" height="80" fill="url(#map-bg)" />

        {/* Dotted continents */}
        {dots.map((d, i) => (
          <circle
            key={i}
            cx={d.x}
            cy={d.y}
            r="0.5"
            fill="#22d3ee"
            fillOpacity="0.55"
          />
        ))}

        {/* Connection arcs */}
        {arcs.map((a) => (
          <path key={a.key} d={a.d} fill="none" stroke="url(#arc-grad)"
                strokeWidth="0.45" strokeLinecap="round" />
        ))}

        {/* Location pins with halo */}
        {locations.map((l, i) => {
          const glow = `url(#pin-glow-${l.status})`
          return (
            <g key={i}>
              <circle cx={l.x} cy={l.y} r="4" fill={glow} />
              <circle cx={l.x} cy={l.y} r="2.0"
                      fill={STATUS_COLOR[l.status]} fillOpacity="0.25" />
              <circle cx={l.x} cy={l.y} r="1.1"
                      fill={STATUS_COLOR[l.status]}
                      stroke="rgba(255,255,255,0.85)"
                      strokeWidth="0.18">
                <animate attributeName="r" values="1.0;1.6;1.0" dur="2.6s" repeatCount="indefinite" />
                <animate attributeName="opacity" values="0.95;0.55;0.95" dur="2.6s" repeatCount="indefinite" />
              </circle>
              <title>{l.name}</title>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
