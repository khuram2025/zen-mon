import { Link } from 'react-router-dom'
import { ChevronLeft, ChevronRight, LineChart, Wrench } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { cn } from '@/lib/utils'
import { formatBps, formatCount, formatPercent, ipDisplay } from '@/lib/netflow/format'
import {
  changeText,
  crossCheckText,
  overrideShadowNote,
  peakNote,
  RESOLUTION_SHORT,
  resolutionAction,
  samplesShort,
  samplesText,
  SOURCE_LABELS,
  SOURCE_SHORT,
  statisticLabel,
  stateTextClass,
  type CapacityPermissions,
  type CapacityRow,
  type Direction,
} from './model'
import { DirectionBadge, Headroom, RateCell, RoleBadge, StateBadge, speedText } from './shared'

export interface TableActions {
  onDetail: (row: CapacityRow) => void
  onResolve: (row: CapacityRow) => void
}

function dirValue(row: CapacityRow, d: Direction) {
  const s = row[d]
  const usingP = s.p_bps != null
  return {
    bps: usingP ? s.p_bps : s.max_bps,
    util: usingP ? s.utilization_p_pct : s.utilization_max_pct,
  }
}

function CapacityText({ row, perms, actions }: { row: CapacityRow; perms?: CapacityPermissions; actions?: TableActions }) {
  const { in_bps, out_bps, source_in, source_out } = row.speed
  if (!in_bps && !out_bps) {
    const act = perms ? resolutionAction(row, perms) : null
    return (
      <div className="flex flex-col items-start gap-1">
        <span className="text-sm text-text2">Unknown</span>
        <span className="text-xs leading-snug text-text2">{RESOLUTION_SHORT[row.resolution]}</span>
        {act && actions && act.kind !== 'none' && (
          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => actions.onResolve(row)} title={act.hint}
            aria-label={`${act.label} for ${row.display_name}`}>
            <Wrench aria-hidden />
            {act.label}
          </Button>
        )}
      </div>
    )
  }
  const same = in_bps === out_bps && source_in === source_out
  const shadow = overrideShadowNote(row.speed)
  // One direction's speed unknown (NF-16): its next action stays on the row.
  const partialAct = perms && actions && (!in_bps || !out_bps) ? resolutionAction(row, perms) : null
  return (
    <div className="flex flex-col text-sm tabular-nums text-text">
      {same ? (
        <span className="whitespace-nowrap">{speedText(in_bps, source_in)}</span>
      ) : (
        <span className="whitespace-nowrap">
          {speedText(in_bps, source_in)} <span className="text-text2">/</span> {speedText(out_bps, source_out)}
        </span>
      )}
      <span className="text-xs text-text2" title={same ? SOURCE_LABELS[source_in] : `in: ${SOURCE_LABELS[source_in]}, out: ${SOURCE_LABELS[source_out]}`}>
        {same ? SOURCE_SHORT[source_in] : source_in === source_out ? `in / out ${SOURCE_SHORT[source_in]}` : `in ${SOURCE_SHORT[source_in]} · out ${SOURCE_SHORT[source_out]}`}
      </span>
      {shadow && (
        <span className="text-xs leading-snug text-warning-fg" title={shadow}>
          Override not in use
        </span>
      )}
      {partialAct && actions && partialAct.kind !== 'none' && (
        <Button size="sm" variant="outline" className="mt-1 h-7 self-start px-2 text-xs" onClick={() => actions.onResolve(row)} title={partialAct.hint}
          aria-label={`${partialAct.label} for ${row.display_name}`}>
          <Wrench aria-hidden />
          {partialAct.label}
        </Button>
      )}
    </div>
  )
}

function InterfaceName({ row, onDetail }: { row: CapacityRow; onDetail: () => void }) {
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          onClick={onDetail}
          className="text-left text-sm font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        >
          {row.display_name}
        </button>
        <RoleBadge role={row.role} />
      </div>
      {row.if_alias && row.if_alias !== row.display_name && <div className="text-xs text-text2">{row.if_alias}</div>}
      <div className="text-xs text-text2">
        {row.device_id ? (
          <Link to={`/devices/${row.device_id}`} className="hover:underline">{row.device_hostname ?? 'device'}</Link>
        ) : (
          <span>No device</span>
        )}
      </div>
      <div className="font-mono text-xs text-text2">
        <span className="whitespace-nowrap">{ipDisplay(row.exporter)}</span> <span className="whitespace-nowrap">· ifIndex {row.ifindex}</span>
      </div>
    </div>
  )
}

function ActionButtons({ row, perms, actions, compact }: { row: CapacityRow; perms: CapacityPermissions; actions: TableActions; compact?: boolean }) {
  const act = resolutionAction(row, perms)
  return (
    <div className="flex flex-wrap items-center justify-end gap-1.5">
      {compact && act.kind !== 'none' && (
        <Button size="sm" variant="outline" onClick={() => actions.onResolve(row)} title={act.hint} aria-label={`${act.label} for ${row.display_name}`}>
          <Wrench aria-hidden />
          {act.label}
        </Button>
      )}
      <Button
        size={compact ? 'sm' : 'icon'}
        variant={compact ? 'outline' : 'ghost'}
        className={compact ? undefined : 'h-8 w-8'}
        onClick={() => actions.onDetail(row)}
        aria-label={`Trend and details for ${row.display_name}`}
        title="Trend, forecast and details"
      >
        <LineChart aria-hidden />
        {compact && 'Trend'}
      </Button>
    </div>
  )
}

function UtilizationCell({ row, size = 'lg' }: { row: CapacityRow; size?: 'lg' | 'xl' }) {
  const h = row.headline
  const note = peakNote(h)
  const cc = crossCheckText(row.snmp, row.counters)
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-1.5">
        <span className={cn(size === 'xl' ? 'text-xl' : 'text-lg', 'font-semibold tabular-nums', stateTextClass(h.state))}>
          {h.utilization_pct != null ? formatPercent(h.utilization_pct) : '—'}
        </span>
        {h.utilization_pct != null && <DirectionBadge direction={h.direction} />}
      </div>
      <StateBadge state={h.state} />
      {note && <span className="text-xs leading-snug text-text2">{note}</span>}
      {cc && <span className="text-xs text-text2" title={cc.title}>{cc.text}</span>}
    </div>
  )
}

const cell = 'px-2 align-top'

export function CapacityTable({
  rows,
  statistic,
  perms,
  actions,
  compare,
}: {
  rows: CapacityRow[]
  statistic: string
  perms: CapacityPermissions
  actions: TableActions
  compare: boolean
}) {
  return (
    <>
      <div className="hidden xl:block">
        <Table>
          <THead>
            <Tr>
              <Th className="px-2">Interface</Th>
              <Th className="px-2">Capacity</Th>
              <Th className="px-2 text-right">In {statistic}</Th>
              <Th className="px-2 text-right">Out {statistic}</Th>
              <Th className="px-2">Utilization</Th>
              <Th className="px-2">Headroom</Th>
              {compare && <Th className="px-2 text-right" title="Change of the headline value against the previous period of the same length">Change</Th>}
              <Th className="px-2 text-right">Samples</Th>
              <Th className="px-2"><span className="sr-only">Actions</span></Th>
            </Tr>
          </THead>
          <TBody>
            {rows.map((r) => {
              const h = r.headline
              const i = dirValue(r, 'in')
              const o = dirValue(r, 'out')
              const smp = samplesShort(r)
              return (
                <Tr key={r.key}>
                  <Td className={cn(cell, 'min-w-[11rem] max-w-[16rem]')}><InterfaceName row={r} onDetail={() => actions.onDetail(r)} /></Td>
                  <Td className={cn(cell, 'min-w-[7.5rem]')}><CapacityText row={r} perms={perms} actions={actions} /></Td>
                  <Td className={cell}><RateCell bps={i.bps} util={i.util} state={h.direction === 'in' ? h.state : undefined} /></Td>
                  <Td className={cell}><RateCell bps={o.bps} util={o.util} state={h.direction === 'out' ? h.state : undefined} /></Td>
                  <Td className={cn(cell, 'min-w-[8.5rem]')}><UtilizationCell row={r} /></Td>
                  <Td className={cell}><Headroom headline={h} /></Td>
                  {compare && (
                    <Td className={cn(cell, 'text-right')}>
                      <span
                        className="whitespace-nowrap text-sm tabular-nums text-text"
                        title={r.previous?.value_bps != null ? `Previous period ${statisticLabel(h.statistic)}: ${formatBps(r.previous.value_bps)}` : 'No comparable value in the previous period'}
                      >
                        {changeText(r.previous?.change_pct)}
                      </span>
                    </Td>
                  )}
                  <Td className={cn(cell, 'text-right')}>
                    <div className="flex flex-col items-end" title={samplesText(r)}>
                      <span className="whitespace-nowrap text-xs tabular-nums text-text">{smp.count}</span>
                      <span className="whitespace-nowrap text-xs tabular-nums text-text2">{smp.coverage}</span>
                    </div>
                  </Td>
                  <Td className={cn(cell, 'w-10 pl-0')}><ActionButtons row={r} perms={perms} actions={actions} /></Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      </div>

      {/* Narrow screens: one card per interface, every deciding value on its own line (NF-30). */}
      <ul className="grid grid-cols-1 gap-2 lg:grid-cols-2 xl:hidden" aria-label="Interfaces">
        {rows.map((r) => {
          const h = r.headline
          const i = dirValue(r, 'in')
          const o = dirValue(r, 'out')
          return (
            <li key={r.key} className="rounded-lg border border-border bg-surface p-3">
              <div className="flex items-start justify-between gap-2">
                <InterfaceName row={r} onDetail={() => actions.onDetail(r)} />
                <div className="flex shrink-0 flex-col items-end">
                  <span className={cn('text-xl font-semibold tabular-nums', stateTextClass(h.state))}>
                    {h.utilization_pct != null ? formatPercent(h.utilization_pct) : '—'}
                  </span>
                  <StateBadge state={h.state} />
                </div>
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                <div>
                  <dt className="text-text2">In {statisticLabel(r.in.p_bps != null ? statistic : 'max')}</dt>
                  <dd><RateCell bps={i.bps} util={i.util} align="left" state={h.direction === 'in' ? h.state : undefined} /></dd>
                </div>
                <div>
                  <dt className="text-text2">Out {statisticLabel(r.out.p_bps != null ? statistic : 'max')}</dt>
                  <dd><RateCell bps={o.bps} util={o.util} align="left" state={h.direction === 'out' ? h.state : undefined} /></dd>
                </div>
                <div>
                  <dt className="text-text2">Capacity</dt>
                  <dd><CapacityText row={r} /></dd>
                </div>
                <div>
                  <dt className="text-text2">Headroom{h.value_bps != null ? ` (${h.direction})` : ''}</dt>
                  <dd><Headroom headline={h} compact /></dd>
                </div>
                {compare && (
                  <div>
                    <dt className="text-text2">vs previous period</dt>
                    <dd className="text-sm tabular-nums text-text">{changeText(r.previous?.change_pct)}</dd>
                  </div>
                )}
                {(() => {
                  const cc = crossCheckText(r.snmp, r.counters)
                  return cc ? (
                    <div>
                      <dt className="text-text2">Cross-check</dt>
                      <dd className="text-sm text-text" title={cc.title}>{cc.text}</dd>
                    </div>
                  ) : null
                })()}
              </dl>
              <p className="mt-2 text-xs text-text2">{samplesText(r)}</p>
              <div className="mt-2"><ActionButtons row={r} perms={perms} actions={actions} compact /></div>
            </li>
          )
        })}
      </ul>
    </>
  )
}

export function Pager({
  offset,
  limit,
  matched,
  onPage,
}: {
  offset: number
  limit: number
  matched: number
  onPage: (offset: number) => void
}) {
  if (matched <= limit && offset === 0) {
    return <p className="text-xs text-text2">Showing all {formatCount(matched)} matching interface{matched === 1 ? '' : 's'}</p>
  }
  const from = matched ? offset + 1 : 0
  const to = Math.min(matched, offset + limit)
  return (
    <nav aria-label="Pages" className="flex flex-wrap items-center justify-between gap-2 text-xs text-text2">
      <span>
        Showing {formatCount(from)}–{formatCount(to)} of {formatCount(matched)}
      </span>
      <div className="flex gap-1.5">
        <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - limit))} aria-label="Previous page">
          <ChevronLeft aria-hidden />
          Previous
        </Button>
        <Button size="sm" variant="outline" disabled={to >= matched} onClick={() => onPage(offset + limit)} aria-label="Next page">
          Next
          <ChevronRight aria-hidden />
        </Button>
      </div>
    </nav>
  )
}
