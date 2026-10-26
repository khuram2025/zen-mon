import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLink, Layers, Pencil } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile } from '@/components/netflow/StatusTile'
import { useNfQuery } from '@/lib/netflow/api'
import { buildNetflowLink } from '@/lib/netflow/links'
import { displayTimeZone } from '@/lib/netflow/window'
import { formatBytes, formatBytesExact, formatCount, formatCountExact, formatPercent, ipDisplay } from '@/lib/netflow/format'
import { DIRECTION_LABELS, duplicationSentence, type CountingResult } from './model'
import { Segmented, ToneChip } from './ui'

const WINDOWS = [
  { value: '15', label: '15 min' },
  { value: '60', label: '1 h' },
] as const

/**
 * Collection-point semantics (FE-12 / CR-16): how much traffic is reported by more than one
 * exporter, what "count every observation" and "count once at the edge" give, and which
 * exporters are edge. Original records are never modified.
 */
export function CountingPanel({ onEditExporter }: { onEditExporter: (ip: string) => void }) {
  const [minutes, setMinutes] = useState<(typeof WINDOWS)[number]['value']>('15')
  const q = useNfQuery<CountingResult>('/netflow/exporters/counting', { minutes, tz: displayTimeZone() }, { refetchInterval: 120_000 })
  return (
    <Card>
      <CardHeader className="flex flex-col gap-2 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Layers className="h-4 w-4 text-primary" aria-hidden /> How traffic is counted
        </CardTitle>
        <Segmented label="Measured period" value={minutes} onChange={setMinutes} options={WINDOWS.map((w) => ({ value: w.value, label: w.label }))} />
      </CardHeader>
      <CardContent className="space-y-4">
        <QueryPanel query={q} label="the duplicate-observation estimate" skeleton="tiles" isEmpty={(r) => r.data.records === 0}
          empty="No flow records arrived in this period, so duplication cannot be estimated">
          {(r) => <CountingBody c={r.data} onEditExporter={onEditExporter} />}
        </QueryPanel>
        {q.data && <DataQualityBar meta={q.data.meta} coverage={null} />}
      </CardContent>
    </Card>
  )
}

function CountingBody({ c, onEditExporter }: { c: CountingResult; onEditExporter: (ip: string) => void }) {
  const dup = c.duplicate_observation_pct ?? 0
  const edge = c.modes.edge
  const names = new Map(c.exporters.map((e) => [e.exporter, e.display_name || ipDisplay(e.exporter)]))
  return (
    <div className="space-y-4">
      <p className="text-sm text-text">{duplicationSentence(c)}</p>
      <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-4">
        <StatusTile
          tone={dup >= 10 ? 'warning' : dup >= 1 ? 'info' : 'success'}
          word={dup >= 10 ? 'Overlap' : dup >= 1 ? 'Some overlap' : 'Unique'}
          label="Duplicate observations"
          value={formatPercent(c.duplicate_observation_pct)}
          caption={`${formatCountExact(c.records - c.flow_keys)} of ${formatCountExact(c.records)} records repeat a flow`}
        />
        <StatusTile
          tone="neutral"
          label="Flows seen at 2+ exporters"
          value={formatPercent(c.multi_point_key_pct)}
          caption={`${formatCountExact(c.multi_point_keys)} of ${formatCountExact(c.flow_keys)} flows`}
        />
        <StatusTile
          tone="neutral"
          label="Counting every observation"
          value={formatBytes(c.modes.all.bytes)}
          title={formatBytesExact(c.modes.all.bytes)}
          caption={c.byte_inflation_pct != null ? `≈ ${formatBytes(c.bytes_unique_estimate)} of distinct traffic (+${formatPercent(c.byte_inflation_pct)})` : undefined}
        />
        <StatusTile
          tone="neutral"
          label="Counted once at the edge"
          value={formatBytes(edge.bytes)}
          title={formatBytesExact(edge.bytes)}
          caption={`${formatPercent(edge.bytes_pct)} of all observations · ${formatCount(edge.records)} records${edge.direction_known ? '' : ' · directions unknown'}`}
        />
      </div>
      <div className="rounded-md border border-border bg-surface2/40 p-3 text-xs leading-relaxed text-text2">
        <p>
          <span className="font-semibold text-text">Every observation</span> (the default) adds up what each exporter reports, so
          traffic that crosses two routers is counted twice. <span className="font-semibold text-text">Counted once at the edge</span>{' '}
          keeps only exporters with the edge role and, for each, the direction set below — use it for network-wide totals.
          Pages switch mode with the Counting control; nothing stored is changed.
        </p>
        <Link to={buildNetflowLink('/netflow', { filter: { count: 'edge' } })} className="mt-1 inline-flex items-center gap-1 font-medium text-text underline-offset-2 hover:underline">
          Open the overview counted at the edge <ExternalLink className="h-3 w-3" aria-hidden />
        </Link>
      </div>
      <div>
        <h4 className="mb-1.5 text-xs font-semibold text-text">Exporters</h4>
        <div className="hidden md:block">
          <Table>
            <THead>
              <Tr>
                <Th>Exporter</Th>
                <Th>Role</Th>
                <Th>Direction counted</Th>
                <Th>Directions reported</Th>
                <Th className="text-right">Records</Th>
                <Th className="text-right">Also seen elsewhere</Th>
                <Th><span className="sr-only">Actions</span></Th>
              </Tr>
            </THead>
            <TBody>
              {c.exporters.map((e) => (
                <Tr key={e.exporter}>
                  <Td className="text-xs">
                    <div className="font-medium text-text">{e.display_name || ipDisplay(e.exporter)}</div>
                    {e.display_name && <div className="font-mono text-xs text-text2">{ipDisplay(e.exporter)}</div>}
                  </Td>
                  <Td className="text-xs">
                    <ToneChip tone={e.counted_in_edge_mode ? 'success' : 'neutral'} icon={null}>{e.role}</ToneChip>
                  </Td>
                  <Td className="text-xs">{DIRECTION_LABELS[e.count_direction]}</Td>
                  <Td className="text-xs text-text2">{directionsText(e.directions)}</Td>
                  <Td className="text-right text-xs tabular-nums">{formatCountExact(e.records)}</Td>
                  <Td className="text-right text-xs tabular-nums">{formatPercent(e.shared_pct)}</Td>
                  <Td className="text-right">
                    <button type="button" onClick={() => onEditExporter(e.exporter)}
                      className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs font-medium text-text hover:bg-surface2"
                      aria-label={`Edit counting settings of ${e.display_name || ipDisplay(e.exporter)}`}>
                      <Pencil className="h-3 w-3" aria-hidden /> Edit
                    </button>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
        <ul className="space-y-2 md:hidden">
          {c.exporters.map((e) => (
            <li key={e.exporter} className="rounded-md border border-border p-3 text-xs">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="font-medium text-text">{e.display_name || ipDisplay(e.exporter)}</div>
                  {e.display_name && <div className="font-mono text-xs text-text2">{ipDisplay(e.exporter)}</div>}
                </div>
                <ToneChip tone={e.counted_in_edge_mode ? 'success' : 'neutral'} icon={null}>{e.role}</ToneChip>
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
                <dt className="text-text2">Records</dt><dd className="text-right tabular-nums text-text">{formatCountExact(e.records)}</dd>
                <dt className="text-text2">Also seen elsewhere</dt><dd className="text-right tabular-nums text-text">{formatPercent(e.shared_pct)}</dd>
                <dt className="text-text2">Direction counted</dt><dd className="text-right text-text">{DIRECTION_LABELS[e.count_direction]}</dd>
                <dt className="text-text2">Reported</dt><dd className="text-right text-text">{directionsText(e.directions)}</dd>
              </dl>
              <button type="button" onClick={() => onEditExporter(e.exporter)}
                className="mt-2 inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs font-medium text-text hover:bg-surface2"
                aria-label={`Edit counting settings of ${e.display_name || ipDisplay(e.exporter)}`}>
                <Pencil className="h-3 w-3" aria-hidden /> Edit
              </button>
            </li>
          ))}
        </ul>
      </div>
      {c.pairs.length > 0 && (
        <div>
          <h4 className="mb-1.5 text-xs font-semibold text-text">Exporters reporting the same flows</h4>
          <ul className="grid gap-1 text-xs sm:grid-cols-2">
            {c.pairs.slice(0, 8).map((p) => (
              <li key={`${p.a}-${p.b}`} className="flex justify-between gap-2 rounded border border-border px-2 py-1">
                <span className="min-w-0 break-words text-text">{names.get(p.a) ?? ipDisplay(p.a)} ↔ {names.get(p.b) ?? ipDisplay(p.b)}</span>
                <span className="shrink-0 tabular-nums text-text2">{formatCountExact(p.flow_keys)} flows</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-xs text-text2">Method: {c.method}</p>
    </div>
  )
}

function directionsText(d: CountingResult['exporters'][number]['directions']): string {
  if (!d) return '—'
  const parts = []
  if (d.ingress) parts.push(`${formatCount(d.ingress)} ingress`)
  if (d.egress) parts.push(`${formatCount(d.egress)} egress`)
  if (d.unknown) parts.push(`${formatCount(d.unknown)} unknown`)
  return parts.join(', ') || '—'
}
