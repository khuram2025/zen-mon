import { useEffect, useId, useState } from 'react'
import { CheckCircle2, Download, FileText, Info } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { cn } from '@/lib/utils'
import { nfParams, useNfQuery, type NfParams } from '@/lib/netflow/api'
import type { FlowFilter } from '@/lib/netflow/filters'
import { formatBps, formatCount, formatDateTime, formatPercent, ipDisplay } from '@/lib/netflow/format'
import { businessHoursText, pReasonText, type BillingData, type BillingItem, type CapacitySettings } from './model'
import { downloadCsv, selectCls } from './shared'

function GradeCell({ item }: { item: BillingItem }) {
  if (item.billing_grade) {
    return (
      <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold text-success-fg">
        <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
        Billing-grade
      </span>
    )
  }
  return (
    <div className="flex flex-col gap-0.5">
      <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold text-warning-fg">
        <Info className="h-3.5 w-3.5" aria-hidden />
        Indicative
      </span>
      <span className="text-xs leading-snug text-text2">{item.notes.join('; ')}</span>
    </div>
  )
}

/**
 * Percentile billing report for a calendar month or custom period (CR-08). Every figure carries
 * the methodology and is only called billing-grade for a complete past period with sufficient
 * collection coverage; anything else is labelled indicative with the reason.
 */
export function BillingReport({
  filter,
  tz,
  settings,
  month,
  onMonth,
  percentile,
  onPercentile,
  mode,
  onMode,
  businessHours,
  onBusinessHours,
  defaultMonth,
}: {
  filter: FlowFilter
  tz: string
  settings: CapacitySettings | null
  month: string
  onMonth: (m: string) => void
  percentile: number | null
  onPercentile: (p: number | null) => void
  mode: string | null
  onMode: (m: string | null) => void
  businessHours: boolean | null
  onBusinessHours: (b: boolean | null) => void
  defaultMonth: string
}) {
  const monthId = useId()
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(q.trim()), 350)
    return () => window.clearTimeout(t)
  }, [q])
  const params: NfParams = nfParams(null, filter, {
    month,
    tz,
    percentile: percentile ?? undefined,
    direction_mode: mode ?? undefined,
    business_hours: businessHours == null ? undefined : businessHours ? '1' : '0',
    q: query || undefined,
  })
  const report = useNfQuery<BillingData>('/netflow/capacity/billing', params, { refetchInterval: false })
  const effectiveP = percentile ?? settings?.percentile ?? 95
  const effectiveMode = mode ?? settings?.direction_mode ?? 'separate'
  const bh = businessHours ?? settings?.business_hours.enabled ?? false

  return (
    <section aria-labelledby="billing-title" className="space-y-3">
      <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4 shadow-card">
        <h2 id="billing-title" className="flex items-center gap-2 text-sm font-semibold text-text">
          <FileText className="h-4 w-4 text-primary" aria-hidden />
          Billing report
        </h2>
        <div className="grid grid-cols-1 gap-3 min-[520px]:flex min-[520px]:flex-wrap min-[520px]:items-end">
          <label htmlFor={monthId} className="space-y-1 text-xs font-medium text-text">
            Month ({tz})
            <Input id={monthId} type="month" value={month} max={defaultMonth}
              onChange={(e) => e.target.value && onMonth(e.target.value)} className="w-full min-[520px]:w-44" />
          </label>
          <label className="space-y-1 text-xs font-medium text-text">
            Percentile
            <select className={cn(selectCls, 'block w-full min-[520px]:w-44')} value={effectiveP}
              onChange={(e) => onPercentile(Number(e.target.value) === settings?.percentile ? null : Number(e.target.value))}>
              {[95, 90, 85, 80, 75].map((p) => <option key={p} value={p}>{p}th{p === settings?.percentile ? ' (configured)' : ''}</option>)}
            </select>
          </label>
          <label className="space-y-1 text-xs font-medium text-text">
            Directions
            <select className={cn(selectCls, 'block w-full min-[520px]:w-72')} value={effectiveMode}
              onChange={(e) => onMode(e.target.value === settings?.direction_mode ? null : e.target.value)}>
              <option value="separate">Separate: higher direction{settings?.direction_mode === 'separate' ? ' (configured)' : ''}</option>
              <option value="merged">Merged: in + out{settings?.direction_mode === 'merged' ? ' (configured)' : ''}</option>
            </select>
          </label>
          <label className="inline-flex h-9 items-center gap-2 text-xs font-medium text-text">
            <input type="checkbox" checked={bh}
              onChange={(e) => onBusinessHours(e.target.checked === (settings?.business_hours.enabled ?? false) ? null : e.target.checked)} />
            Business hours only
          </label>
          <label className="space-y-1 text-xs font-medium text-text">
            Search
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Interface, device, exporter" className="w-full min-[520px]:w-56" />
          </label>
          <Button
            size="sm"
            variant="outline"
            className="min-[520px]:ml-auto"
            onClick={() => downloadCsv('/netflow/capacity/billing', params, `netflow-billing-${month}.csv`)}
          >
            <Download aria-hidden />
            Export CSV
          </Button>
        </div>
        {bh && settings && <p className="text-xs text-text2">Business hours: {businessHoursText(settings.business_hours)}</p>}
      </div>

      <QueryPanel
        query={report}
        label="the billing report"
        skeleton="rows"
        isEmpty={(r) => r.data.items.length === 0}
        empty="No interface carried flow traffic in this period"
      >
        {(r) => {
          const b = r.data
          return (
            <div className="space-y-3">
              <DataQualityBar meta={r.meta} coverage={null} />
              <div className="rounded-md border border-border bg-surface2/40 p-3 text-xs leading-relaxed text-text">
                <p>
                  <strong>Period:</strong> {formatDateTime(b.period.from, b.period.tz, { zone: 'name', seconds: false })} to{' '}
                  {formatDateTime(b.period.to, b.period.tz, { zone: 'none', seconds: false })}
                  {b.period.complete ? '' : ' (not yet complete)'} · {formatCount(b.period.expected_slots)} five-minute samples expected per interface.
                </p>
                <p>
                  <strong>Method:</strong> {b.method.percentile_definition} {b.method.direction_mode_definition}. Idle slots count as zero;
                  collection gaps are excluded.
                </p>
                <p>
                  <strong>Billing-grade:</strong> {b.method.billing_grade_rule}. {formatCount(b.totals.billing_grade)} of {formatCount(b.totals.interfaces)} interfaces qualify;
                  the rest are indicative and say why.
                </p>
              </div>
              <ul className="space-y-2 md:hidden" aria-label="Billing lines">
                {b.items.map((i) => (
                  <li key={`${i.exporter}|${i.ifindex}`} className="rounded-lg border border-border bg-surface p-3">
                    <div className="text-sm font-medium text-text">{i.display_name}</div>
                    <div className="text-xs text-text2">{i.device_hostname ?? 'No device'}</div>
                    <div className="font-mono text-xs text-text2">{ipDisplay(i.exporter)} · ifIndex {i.ifindex}</div>
                    <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <dt className="text-text2">Billable ({i.billable_direction === 'merged' ? 'in + out' : i.billable_direction ?? '—'})</dt>
                        <dd className="text-base font-semibold tabular-nums text-text">{i.billable_bps != null ? formatBps(i.billable_bps) : '—'}</dd>
                      </div>
                      <div>
                        <dt className="text-text2">Samples</dt>
                        <dd className="tabular-nums text-text">{formatCount(i.samples)} · {formatPercent(i.coverage_pct)}</dd>
                      </div>
                      <div>
                        <dt className="text-text2">In p{b.method.percentile}</dt>
                        <dd className="tabular-nums text-text">{formatBps(i.in_p_bps)}</dd>
                      </div>
                      <div>
                        <dt className="text-text2">Out p{b.method.percentile}</dt>
                        <dd className="tabular-nums text-text">{formatBps(i.out_p_bps)}</dd>
                      </div>
                    </dl>
                    <div className="mt-2"><GradeCell item={i} /></div>
                  </li>
                ))}
              </ul>
              <div className="hidden overflow-x-auto md:block">
                <Table>
                  <THead>
                    <Tr>
                      <Th>Interface</Th>
                      <Th className="text-right">Samples</Th>
                      <Th className="text-right">In p{b.method.percentile}</Th>
                      <Th className="text-right">Out p{b.method.percentile}</Th>
                      <Th className="text-right">In + out p{b.method.percentile}</Th>
                      <Th className="text-right">Billable</Th>
                      <Th>Status</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {b.items.map((i) => (
                      <Tr key={`${i.exporter}|${i.ifindex}`}>
                        <Td className="min-w-[12rem]">
                          <div className="text-sm font-medium text-text">{i.display_name}</div>
                          <div className="text-xs text-text2">{i.device_hostname ?? 'No device'}</div>
                          <div className="font-mono text-xs text-text2">
                            <span className="whitespace-nowrap">{ipDisplay(i.exporter)}</span> <span className="whitespace-nowrap">· ifIndex {i.ifindex}</span>
                          </div>
                        </Td>
                        <Td className="whitespace-nowrap text-right text-xs tabular-nums">
                          {formatCount(i.samples)} · {formatPercent(i.coverage_pct)}
                        </Td>
                        <Td className="whitespace-nowrap text-right tabular-nums">{i.in_p_bps != null ? formatBps(i.in_p_bps) : <span className="text-text2" title={pReasonText(i.p_reason) ?? undefined}>—</span>}</Td>
                        <Td className="whitespace-nowrap text-right tabular-nums">{i.out_p_bps != null ? formatBps(i.out_p_bps) : <span className="text-text2">—</span>}</Td>
                        <Td className="whitespace-nowrap text-right tabular-nums">{i.combined_p_bps != null ? formatBps(i.combined_p_bps) : <span className="text-text2">—</span>}</Td>
                        <Td className="whitespace-nowrap text-right">
                          <div className="text-sm font-semibold tabular-nums text-text">{i.billable_bps != null ? formatBps(i.billable_bps) : '—'}</div>
                          <div className="text-xs text-text2">
                            {i.billable_direction === 'merged' ? 'in + out' : i.billable_direction ?? ''}
                            {i.billable_util_pct != null ? ` · ${formatPercent(i.billable_util_pct)} of speed` : ''}
                          </div>
                        </Td>
                        <Td><GradeCell item={i} /></Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </div>
            </div>
          )
        }}
      </QueryPanel>
    </section>
  )
}
