import { ChevronRight, Link2 } from 'lucide-react'
import { useServerNowTick } from '@/components/services/detail/clock'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { ExporterStateBadge } from '@/components/netflow/ExporterStateBadge'
import { formatCount, formatCountExact, formatDateTime, formatPercent, ipDisplay, tzOffsetLabel, zonedDate } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import {
  clockTone,
  currentAgeSeconds,
  formatAge,
  formatClockOffset,
  protocolList,
  samplingLabel,
  worstIssue,
  type HealthExporterRow,
} from './model'
import type { DetailTab } from './ExporterDetailDialog'
import { ToneChip, issueTone } from './ui'

function Issues({ row }: { row: HealthExporterRow }) {
  const issues = (row.issues ?? []).filter((i) => i.code !== 'unmapped')
  if (!issues.length) return <span className="text-text2">None</span>
  const worst = worstIssue({ issues }) ?? 'info'
  return (
    <ToneChip tone={issueTone(worst)} title={issues.map((i) => i.message).join('\n')}>
      {issues.length === 1 ? shortIssue(issues[0].code) : `${issues.length} issues`}
    </ToneChip>
  )
}

const SHORT: Record<string, string> = {
  rejected_allowlist: 'Rejected',
  waiting_template: 'No template',
  template_gaps: 'Template gaps',
  parse_errors: 'Parse errors',
  clock_clamped: 'Re-timed',
  sampling_ambiguous: 'Sampling unknown',
  sequence_loss: 'Loss',
  clock_skew: 'Clock skew',
  no_tcp_flags: 'No TCP flags',
}

function shortIssue(code: string): string {
  return SHORT[code] ?? code.replace(/_/g, ' ')
}

/** A table timestamp without the zone (the column header names it); time only on the server's day. */
function shortTime(iso: string, serverTime: string, tz: string, seconds = true): string {
  const sameDay = zonedDate(iso, tz) === zonedDate(serverTime, tz)
  return formatDateTime(iso, tz, { zone: 'none', seconds, timeOnly: sameDay })
}

/** The owner device's own polled status, worded so it is never read as the exporter's state. */
function deviceStatusText(status: string): string {
  return status === 'unknown' ? 'device status unknown' : `device ${status}`
}

function StateCell({ row, age, tz, serverTime }: { row: HealthExporterRow; age: number | null; tz: string; serverTime: string }) {
  return (
    <div className="space-y-0.5">
      <ExporterStateBadge state={row.state} />
      {row.state_since && row.state !== 'receiving' && (
        <div className="whitespace-nowrap text-[11px] text-text2">since {shortTime(row.state_since, serverTime, tz, false)}</div>
      )}
      {row.state === 'receiving' && age != null && <div className="text-[11px] text-text2">{formatAge(age)} ago</div>}
    </div>
  )
}

/**
 * Exporters with their receipt state (appliance clock), volume, loss, clock and issues. A table
 * from md up, stacked cards below so no deciding value is truncated at 390 px (NF-30).
 */
export function ExporterTable({ rows, serverTime, onOpen }: {
  rows: HealthExporterRow[]
  serverTime: string
  onOpen: (exporter: string, tab?: DetailTab) => void
}) {
  const now = useServerNowTick(5_000)
  const tz = displayTimeZone()
  const zone = tzOffsetLabel(tz, serverTime)
  return (
    <>
      <div className="hidden md:block">
        <Table>
          <THead>
            <Tr>
              <Th>Exporter</Th>
              <Th>State</Th>
              <Th title={`Times in ${tz}`}>Last received ({zone})</Th>
              <Th className="text-right">Records 15 min</Th>
              <Th className="text-right">Loss 1 h</Th>
              <Th>Clock</Th>
              <Th>Export</Th>
              <Th>Issues</Th>
              <Th><span className="sr-only">Actions</span></Th>
            </Tr>
          </THead>
          <TBody>
            {rows.map((r) => {
              const age = currentAgeSeconds(r, serverTime, now)
              return (
                <Tr key={r.exporter} className="cursor-pointer" onClick={() => onOpen(r.exporter)}>
                  <Td className="min-w-[11rem] text-xs">
                    <div className="whitespace-nowrap font-medium text-text">{r.display_name || ipDisplay(r.exporter)}</div>
                    {r.display_name && <div className="whitespace-nowrap font-mono text-[11px] text-text2">{ipDisplay(r.exporter)}</div>}
                    <div className="text-[11px] text-text2">
                      {r.device_hostname
                        ? <>{r.device_hostname}{r.device_status ? ` · ${deviceStatusText(r.device_status)}` : ''}</>
                        : 'No device linked'}
                    </div>
                  </Td>
                  <Td><StateCell row={r} age={age} tz={tz} serverTime={serverTime} /></Td>
                  <Td className="whitespace-nowrap text-xs tabular-nums" title={r.last_received_at ? formatDateTime(r.last_received_at, tz) : undefined}>
                    {r.last_received_at ? shortTime(r.last_received_at, serverTime, tz) : <span className="text-text2">Never</span>}
                    {r.state !== 'receiving' && age != null && <div className="text-[11px] text-text2">{formatAge(age)} ago</div>}
                  </Td>
                  <Td className="text-right text-xs tabular-nums" title={r.records_15m != null ? formatCountExact(r.records_15m) : 'Unknown'}>
                    {formatCount(r.records_15m)}
                  </Td>
                  <Td className="text-right text-xs tabular-nums">{r.loss_pct_1h == null ? '—' : formatPercent(r.loss_pct_1h, 2)}</Td>
                  <Td className="whitespace-nowrap text-xs">
                    {r.clock_offset_ms == null ? '—' : clockTone(r.clock_offset_ms) === 'success'
                      ? formatClockOffset(r.clock_offset_ms)
                      : <ToneChip tone={clockTone(r.clock_offset_ms)}>{formatClockOffset(r.clock_offset_ms)}</ToneChip>}
                  </Td>
                  <Td className="text-xs">
                    <div>{protocolList(r.export_protocols)}</div>
                    {r.sampling_interval != null && <div className="text-[11px] text-text2">{samplingLabel(r.sampling_interval)}</div>}
                  </Td>
                  <Td className="text-xs"><Issues row={r} /></Td>
                  <Td className="text-right" onClick={(e) => e.stopPropagation()}>
                    <div className="flex justify-end gap-1">
                      {!r.device_id && (
                        <button type="button" onClick={() => onOpen(r.exporter, 'device')}
                          className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2 text-[11px] font-medium text-text hover:bg-surface2"
                          aria-label={`Link ${r.display_name || ipDisplay(r.exporter)} to a device`}>
                          <Link2 className="h-3 w-3" aria-hidden /> Link
                        </button>
                      )}
                      <button type="button" onClick={() => onOpen(r.exporter)}
                        className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2 text-[11px] font-medium text-text hover:bg-surface2"
                        aria-label={`Details of ${r.display_name || ipDisplay(r.exporter)}`}>
                        Details <ChevronRight className="h-3 w-3" aria-hidden />
                      </button>
                    </div>
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      </div>
      <ul className="space-y-2 md:hidden">
        {rows.map((r) => {
          const age = currentAgeSeconds(r, serverTime, now)
          return (
            <li key={r.exporter} className="rounded-lg border border-border p-3 text-xs">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="break-words font-medium text-text">{r.display_name || ipDisplay(r.exporter)}</div>
                  {r.display_name && <div className="font-mono text-[11px] text-text2">{ipDisplay(r.exporter)}</div>}
                  <div className="text-[11px] text-text2">{r.device_hostname ?? 'No device linked'}</div>
                </div>
                <ExporterStateBadge state={r.state} />
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
                <dt className="text-text2">Last received</dt>
                <dd className="text-right tabular-nums text-text">{age != null ? `${formatAge(age)} ago` : 'Never'}</dd>
                <dt className="text-text2">Records 15 min</dt>
                <dd className="text-right tabular-nums text-text">{formatCount(r.records_15m)}</dd>
                <dt className="text-text2">Loss 1 h</dt>
                <dd className="text-right tabular-nums text-text">{r.loss_pct_1h == null ? '—' : formatPercent(r.loss_pct_1h, 2)}</dd>
                <dt className="text-text2">Clock</dt>
                <dd className="text-right text-text">{formatClockOffset(r.clock_offset_ms)}</dd>
                <dt className="text-text2">Export</dt>
                <dd className="text-right text-text">{protocolList(r.export_protocols)}</dd>
                <dt className="text-text2">Issues</dt>
                <dd className="text-right"><Issues row={r} /></dd>
              </dl>
              <div className="mt-2 flex gap-2">
                <button type="button" onClick={() => onOpen(r.exporter)}
                  className="inline-flex h-8 flex-1 items-center justify-center gap-1 rounded-md border border-border text-[11px] font-medium text-text hover:bg-surface2"
                  aria-label={`Details of ${r.display_name || ipDisplay(r.exporter)}`}>
                  Details <ChevronRight className="h-3 w-3" aria-hidden />
                </button>
                {!r.device_id && (
                  <button type="button" onClick={() => onOpen(r.exporter, 'device')}
                    className="inline-flex h-8 flex-1 items-center justify-center gap-1 rounded-md border border-border text-[11px] font-medium text-text hover:bg-surface2"
                    aria-label={`Link ${r.display_name || ipDisplay(r.exporter)} to a device`}>
                    <Link2 className="h-3 w-3" aria-hidden /> Link device
                  </button>
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </>
  )
}
