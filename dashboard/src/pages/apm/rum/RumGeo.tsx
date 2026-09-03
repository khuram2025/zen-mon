import { useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import ReactECharts from 'echarts-for-react'
import * as echarts from 'echarts'
import { Globe2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { fmtPct } from '@/components/apm/shared'
import { fmtCount } from '@/components/apm/viz'
import { EXPLORER_HEAD, EXPLORER_ROWS } from '@/components/apm/explorer'
import { useTheme } from '@/stores/theme'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import type { RumFilters, RumGeo, RumGeoCountry } from '@/types/apm'
import { QueryErrorPanel, RumEmptyState, RumMetricCell, RumSectionHeader, RumTableCard } from './RumUi'
import { countryFlag, countryLabel, countryMapName } from './countries'

type GeoMetric = 'sessions' | 'error_session_rate' | 'lcp_p75' | 'lcp_poor_pct'
const METRICS: Array<{ value: GeoMetric; label: string; fmt: (row: RumGeoCountry) => string; lowerIsBetter: boolean }> = [
  { value: 'sessions', label: 'Sessions', fmt: (row) => fmtCount(row.sessions), lowerIsBetter: false },
  { value: 'error_session_rate', label: 'Errored sessions', fmt: (row) => fmtPct(row.error_session_rate), lowerIsBetter: true },
  { value: 'lcp_p75', label: 'LCP p75', fmt: (row) => (row.lcp_p75 == null ? '—' : `${(row.lcp_p75 / 1000).toFixed(2)} s`), lowerIsBetter: true },
  { value: 'lcp_poor_pct', label: 'Poor LCP share', fmt: (row) => (row.lcp_poor_pct == null ? '—' : `${row.lcp_poor_pct.toFixed(0)}%`), lowerIsBetter: true },
]
const INTERACTIVE_ROW = 'cursor-pointer focus:outline-none focus-visible:bg-surface2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40'

let worldRegistered: Promise<boolean> | null = null
/** Registers the bundled world GeoJSON once per page load. */
function ensureWorldMap(): Promise<boolean> {
  if (!worldRegistered) {
    worldRegistered = fetch('/maps/world.json')
      .then((response) => response.json())
      .then((geo) => { echarts.registerMap('world', geo); return true })
      .catch(() => false)
  }
  return worldRegistered
}

function onRowKey(event: KeyboardEvent<HTMLTableRowElement>, open: () => void) {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  open()
}

export function RumGeoPanel({ data, loading, error, onRetry, onFilter }: {
  data?: RumGeo
  loading?: boolean
  error?: unknown
  onRetry?: () => void
  onFilter: (key: keyof RumFilters, value: string) => void
}) {
  const [metric, setMetric] = useState<GeoMetric>('sessions')
  const [mapReady, setMapReady] = useState(false)
  const theme = useTheme((state) => state.theme)
  useEffect(() => { ensureWorldMap().then(setMapReady) }, [])
  const spec = METRICS.find((item) => item.value === metric) ?? METRICS[0]
  const countries = data?.countries ?? []

  const option = useMemo(() => {
    const values = countries
      .map((row) => ({ name: countryMapName(row.country) ?? row.country, code: row.country, value: row[metric] == null ? null : metric === 'error_session_rate' ? (row[metric] as number) * 100 : row[metric], row }))
      .filter((item) => item.value != null && Number.isFinite(item.value as number))
    // Countries sharing one polygon (Hong Kong, Macao, Taiwan → China) merge by max.
    const merged = new Map<string, typeof values[number]>()
    for (const item of values) {
      const existing = merged.get(item.name)
      if (!existing || (item.value as number) > (existing.value as number)) merged.set(item.name, item)
    }
    const points = [...merged.values()]
    const max = Math.max(1, ...points.map((item) => item.value as number))
    const dark = theme === 'dark'
    const good = spec.lowerIsBetter ? ['#10b981', '#f59e0b', '#ef4444'] : ['#1d4ed8', '#3b82f6', '#93c5fd']
    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item',
        formatter: (params: { data?: { row?: RumGeoCountry; code?: string } }) => {
          const row = params.data?.row
          if (!row) return 'No traffic'
          return `<b>${countryFlag(row.country)} ${countryLabel(row.country)}</b><br/>${fmtCount(row.sessions)} sessions · ${fmtCount(row.views)} views<br/>Errored sessions ${fmtPct(row.error_session_rate)}<br/>LCP p75 ${row.lcp_p75 == null ? '—' : `${(row.lcp_p75 / 1000).toFixed(2)} s`} · INP p75 ${row.inp_p75 == null ? '—' : `${Math.round(row.inp_p75)} ms`}`
        },
      },
      visualMap: {
        min: 0, max, left: 12, bottom: 12, orient: 'horizontal', calculable: false,
        text: [spec.label, ''], textStyle: { color: dark ? '#94a3b8' : '#475569', fontSize: 10 },
        inRange: { color: spec.lowerIsBetter ? good : [...good].reverse() },
      },
      series: [{
        type: 'map', map: 'world', roam: true, zoom: 1.15, selectedMode: false,
        itemStyle: { areaColor: dark ? '#1e293b' : '#e2e8f0', borderColor: dark ? '#0f172a' : '#ffffff', borderWidth: 0.6 },
        emphasis: { label: { show: false }, itemStyle: { areaColor: '#f59e0b' } },
        data: points,
      }],
    }
  }, [countries, metric, spec, theme])

  if (error) return <QueryErrorPanel label="geography" error={error} onRetry={onRetry} />
  return (
    <div className="space-y-4">
      <section aria-labelledby="rum-geo-map">
        <RumSectionHeader id="rum-geo-map" title="Experience by country" description="Visitor country from the CDN header or the controller's GeoIP database. Select a country to filter every RUM page."
          action={(
            <Select value={metric} onValueChange={(value) => setMetric(value as GeoMetric)}>
              <SelectTrigger className="h-7 w-[170px] text-[11px]" aria-label="Map metric"><SelectValue /></SelectTrigger>
              <SelectContent>{METRICS.map((item) => <SelectItem key={item.value} value={item.value}>Colour by {item.label.toLowerCase()}</SelectItem>)}</SelectContent>
            </Select>
          )} />
        <Card className="overflow-hidden">
          {!countries.length && !loading ? (
            <RumEmptyState icon={Globe2} title="No resolved countries in this window" description={data && !data.geoip_available ? 'The GeoIP database is not installed on the controller and no CDN country header was received. Run scripts/fetch-geoip.py, or forward CF-IPCountry / X-Country-Code from your edge.' : 'Every session in this window came from a private or reserved address (lab traffic), which has no country.'} />
          ) : mapReady ? (
            <ReactECharts option={option} style={{ height: 420 }} notMerge lazyUpdate />
          ) : (
            <div className="flex h-[420px] items-center justify-center text-xs text-muted">{loading ? 'Loading geography…' : 'Map data unavailable'}</div>
          )}
        </Card>
        {data?.unresolved && data.unresolved.sessions > 0 && (
          <p className="mt-1.5 text-[11px] text-muted">{fmtCount(data.unresolved.sessions)} session{data.unresolved.sessions === 1 ? '' : 's'} from private or reserved addresses have no country and are not on the map.</p>
        )}
      </section>

      <RumTableCard title="Countries" description="Ranked by sessions. Vitals are p75 of one sample per page view.">
        <Table>
          <THead className={EXPLORER_HEAD}><Tr><Th>Country</Th><Th className="text-right">Sessions</Th><Th className="text-right">Views</Th><Th className="text-right">Errored sessions</Th><Th className="text-right">LCP p75</Th><Th className="text-right">INP p75</Th><Th className="text-right">CLS p75</Th><Th className="text-right">Poor LCP</Th></Tr></THead>
          <TBody className={EXPLORER_ROWS}>
            {countries.map((row) => {
              const open = () => onFilter('country', row.country)
              return (
                <Tr key={row.country} className={INTERACTIVE_ROW} tabIndex={0} onClick={open} onKeyDown={(event) => onRowKey(event, open)} title={`Filter by ${countryLabel(row.country)}`}>
                  <Td><span className="mr-1.5">{countryFlag(row.country)}</span><span className="text-xs text-text">{countryLabel(row.country)}</span><span className="ml-1.5 font-mono text-[10px] text-muted">{row.country}</span></Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(row.sessions)}</Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(row.views)}</Td>
                  <Td className="text-right"><span className={cn('font-mono text-xs tabular-nums', (row.error_session_rate ?? 0) >= 0.05 ? 'text-danger' : '')}>{fmtPct(row.error_session_rate)}</span></Td>
                  <Td><RumMetricCell name="lcp" value={row.lcp_p75} samples={row.lcp_samples} /></Td>
                  <Td><RumMetricCell name="inp" value={row.inp_p75} samples={row.inp_samples} /></Td>
                  <Td><RumMetricCell name="cls" value={row.cls_p75} samples={row.cls_samples} /></Td>
                  <Td className="text-right"><span className={cn('font-mono text-xs tabular-nums', (row.lcp_poor_pct ?? 0) >= 25 ? 'text-danger' : (row.lcp_poor_pct ?? 0) > 0 ? 'text-warning' : 'text-success')}>{row.lcp_poor_pct == null ? '—' : `${row.lcp_poor_pct.toFixed(0)}%`}</span></Td>
                </Tr>
              )
            })}
            {!countries.length && <Tr><Td colSpan={8} className="py-8 text-center text-xs text-muted">{loading ? 'Loading…' : 'No countries in this window.'}</Td></Tr>}
          </TBody>
        </Table>
      </RumTableCard>
    </div>
  )
}
