import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLink, Radio } from 'lucide-react'
import { useServerNowTick } from '@/components/services/detail/clock'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile } from '@/components/netflow/StatusTile'
import { ExporterStateBadge } from '@/components/netflow/ExporterStateBadge'
import { useNfQuery } from '@/lib/netflow/api'
import { formatBps, formatBytes, formatCount, formatCountExact, formatDateTime, formatPercent, ipDisplay } from '@/lib/netflow/format'
import { buildNetflowLink } from '@/lib/netflow/links'
import { displayTimeZone } from '@/lib/netflow/window'
import { ExporterMapping } from '../components/ExporterDeviceDialog'
import { ExporterSettingsForm } from './ExporterSettingsForm'
import { SeriesChart } from './SeriesChart'
import {
  clockTone,
  currentAgeSeconds,
  droppedRecords,
  exporterLabel,
  formatAge,
  formatClockOffset,
  intervalBasisLabel,
  protocolList,
  samplingLabel,
  stateTone,
  type Capability,
  type ExporterHealthDetail,
  type ExporterTemplate,
} from './model'
import { Metric, Segmented, ToneChip, issueTone } from './ui'

export type DetailTab = 'overview' | 'fields' | 'interfaces' | 'settings' | 'device'

const WINDOWS = [
  { value: '1', label: '1 h' },
  { value: '24', label: '24 h' },
  { value: '168', label: '7 d' },
] as const

/** Everything about one exporter (FE-01): receipt, decode, loss, clock, fields, templates, interfaces, settings, device link. */
export function ExporterDetailDialog({ exporter, tab, onTabChange, onClose }: {
  exporter: string
  tab: DetailTab
  onTabChange: (t: DetailTab) => void
  onClose: () => void
}) {
  const [hours, setHours] = useState<(typeof WINDOWS)[number]['value']>('1')
  const detail = useNfQuery<ExporterHealthDetail>(`/netflow/exporters/${encodeURIComponent(exporter)}/health`, { hours, tz: displayTimeZone() }, {
    refetchInterval: 30_000,
  })
  const row = detail.data?.data.status
  const tabsRef = useRef<HTMLDivElement>(null)
  const scrolledFor = useRef<string | null>(null)
  // On a narrow screen the tab strip scrolls: keep the open tab (e.g. from a deep link) in view.
  // Runs after every render because the dialog's portal mounts its content a render later.
  useEffect(() => {
    const strip = tabsRef.current
    if (!strip || scrolledFor.current === tab) return
    const active = strip.querySelector<HTMLElement>('[role="tab"][data-state="active"]')
    if (!active) return
    const a = active.getBoundingClientRect()
    const b = strip.getBoundingClientRect()
    strip.scrollLeft = Math.max(0, strip.scrollLeft + (a.left - b.left) - (b.width - a.width) / 2)
    scrolledFor.current = tab
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        className="max-h-[94vh] w-[calc(100vw-1rem)] max-w-4xl overflow-y-auto overflow-x-hidden p-4 focus:outline-none sm:p-6"
        // Focus the dialog itself on open (not the first tab panel), so nothing shows a stray focus ring.
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement | null)?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2 pr-6">
            <Radio className="h-4 w-4 shrink-0 text-primary" aria-hidden />
            <span className="break-all">{row ? exporterLabel(row) : ipDisplay(exporter)}</span>
            {row?.display_name && <span className="font-mono text-sm font-normal text-text2">{ipDisplay(exporter)}</span>}
            {row && <ExporterStateBadge state={row.state} />}
          </DialogTitle>
          <DialogDescription>
            What the collector receives from this exporter and what its records can support. Receipt times use the
            appliance clock.
          </DialogDescription>
        </DialogHeader>
        <Tabs value={tab} onValueChange={(v) => onTabChange(v as DetailTab)} className="min-w-0">
          <div ref={tabsRef} className="-mx-1 overflow-x-auto px-1">
            <TabsList aria-label="Exporter detail sections">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="fields">Fields &amp; templates</TabsTrigger>
              <TabsTrigger value="interfaces">Interfaces</TabsTrigger>
              <TabsTrigger value="settings">Settings</TabsTrigger>
              <TabsTrigger value="device">Device link</TabsTrigger>
            </TabsList>
          </div>
          <TabsContent value="overview">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <Segmented label="Statistics period" value={hours} onChange={setHours} options={WINDOWS.map((w) => ({ value: w.value, label: w.label }))} />
              <Link
                to={buildNetflowLink('/netflow', { filter: { exporter } })}
                className="inline-flex items-center gap-1 text-xs font-medium text-text underline-offset-2 hover:underline"
              >
                Traffic from this exporter <ExternalLink className="h-3 w-3" aria-hidden />
              </Link>
            </div>
            <QueryPanel query={detail} label="exporter health" skeleton="tiles">
              {(r) => <OverviewTab d={r.data} />}
            </QueryPanel>
            {detail.data && <DataQualityBar meta={detail.data.meta} coverage={null} className="mt-3" />}
          </TabsContent>
          <TabsContent value="fields">
            <QueryPanel query={detail} label="field availability" skeleton="rows">
              {(r) => <FieldsTab d={r.data} />}
            </QueryPanel>
          </TabsContent>
          <TabsContent value="interfaces">
            <QueryPanel query={detail} label="interfaces" skeleton="rows" isEmpty={(r) => !(r.data.interfaces?.length)}
              empty="No interface indexes reported in the last 24 hours">
              {(r) => <InterfacesTab d={r.data} />}
            </QueryPanel>
          </TabsContent>
          <TabsContent value="settings">
            <QueryPanel query={detail} label="settings" skeleton="rows">
              {(r) => <ExporterSettingsForm exporter={exporter} settings={r.data.settings} row={r.data.status} />}
            </QueryPanel>
          </TabsContent>
          <TabsContent value="device">
            <ExporterMapping exporter={exporter} />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}

function OverviewTab({ d }: { d: ExporterHealthDetail }) {
  const now = useServerNowTick(5_000)
  const tz = displayTimeZone()
  const r = d.status
  const age = currentAgeSeconds(r, d.server_time, now)
  const streams = d.streams ?? []
  const totals = streams.reduce(
    (a, s) => {
      a.datagrams += s.datagrams
      a.decoded += s.records_decoded
      a.written += s.records_written
      a.dropped += droppedRecords(s)
      a.waiting += s.data_sets_waiting_template
      a.parse += s.parse_errors
      return a
    },
    { datagrams: 0, decoded: 0, written: 0, dropped: 0, waiting: 0, parse: 0 },
  )
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-4">
        <StatusTile
          tone={stateTone(r.state)}
          label="Last received"
          value={age != null ? `${formatAge(age)} ago` : 'Never'}
          word={r.state === 'receiving' ? 'Receiving' : r.state === 'late' ? 'Late' : r.state === 'silent' ? 'Silent' : 'Never'}
          caption={r.last_received_at ? formatDateTime(r.last_received_at, tz) : 'No datagram or record on file'}
        />
        <StatusTile
          tone="neutral"
          label="Expected interval"
          value={`${r.expected_interval_s} s`}
          caption={`${intervalBasisLabel(r.interval_basis)}; late after ${formatAge(r.late_after_s)}, silent after ${formatAge(r.silent_after_s)}`}
        />
        <StatusTile
          tone={r.loss_pct_1h == null ? 'unknown' : r.loss_pct_1h >= 1 ? 'warning' : 'success'}
          label="Export loss (1 h)"
          value={r.loss_pct_1h == null ? '—' : formatPercent(r.loss_pct_1h, 2)}
          word={r.loss_pct_1h == null ? 'Unknown' : r.loss_pct_1h >= 1 ? 'Loss' : 'OK'}
          caption="From sequence-number gaps"
        />
        <StatusTile
          tone={clockTone(r.clock_offset_ms)}
          label="Exporter clock"
          value={formatClockOffset(r.clock_offset_ms)}
          word={r.clock_offset_ms == null ? 'Unknown' : Math.abs(r.clock_offset_ms) <= 5000 ? 'OK' : r.clock_suspect ? 'Skewed' : 'Drift'}
          caption="Receipt time minus export header time"
        />
      </div>
      {(r.issues ?? []).length > 0 && (
        <ul className="space-y-1.5" aria-label="Issues">
          {(r.issues ?? []).map((i) => (
            <li key={i.code} className="flex items-start gap-2 text-xs text-text">
              <ToneChip tone={issueTone(i.severity)}>{i.severity === 'danger' ? 'Problem' : i.severity === 'warning' ? 'Warning' : 'Note'}</ToneChip>
              <span className="pt-0.5">{i.message}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Metric label="Datagrams" value={formatCount(totals.datagrams)} title={formatCountExact(totals.datagrams)} />
        <Metric label="Records decoded" value={formatCount(totals.decoded)} title={formatCountExact(totals.decoded)} />
        <Metric label="Records stored" value={formatCount(totals.written)} title={formatCountExact(totals.written)} />
        <Metric label="Dropped" value={formatCount(totals.dropped)} title={formatCountExact(totals.dropped)} />
        <Metric label="Waiting for template" value={formatCount(totals.waiting)} hint="data sets" />
        <Metric label="Parse errors" value={formatCount(totals.parse)} />
      </div>
      <SeriesChart series={d.series} from={d.window.from} to={d.window.to} granularityS={d.window.granularity_s} title="Records stored" />
      <div>
        <h4 className="mb-1.5 text-xs font-semibold text-text">Export streams</h4>
        {streams.length === 0 ? (
          <p className="text-xs text-text2">No collector statistics for this exporter in the period.</p>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>Protocol / domain</Th>
                <Th className="text-right">Datagrams</Th>
                <Th className="text-right">Stored</Th>
                <Th className="text-right">Loss</Th>
                <Th>Sampling</Th>
                <Th className="text-right">Templates</Th>
                <Th>Last datagram</Th>
              </Tr>
            </THead>
            <TBody>
              {streams.map((s) => (
                <Tr key={`${s.observation_domain}-${s.export_protocol}`}>
                  <Td className="whitespace-nowrap text-xs">{protocolList(s.export_protocol ? [s.export_protocol] : [])} · {s.observation_domain}</Td>
                  <Td className="text-right text-xs tabular-nums">{formatCountExact(s.datagrams)}</Td>
                  <Td className="text-right text-xs tabular-nums">{formatCountExact(s.records_written)}</Td>
                  <Td className="text-right text-xs tabular-nums">{s.loss_pct == null ? '—' : formatPercent(s.loss_pct, 2)}</Td>
                  <Td className="whitespace-nowrap text-xs">{samplingLabel(s.sampling_interval)}</Td>
                  <Td className="text-right text-xs tabular-nums">{s.templates_active || '—'}</Td>
                  <Td className="whitespace-nowrap text-xs tabular-nums">{formatDateTime(s.last_datagram_at, tz)}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>
      <p className="text-xs text-text2">
        {protocolList(r.export_protocols)} · {samplingLabel(r.sampling_interval)} · observation domains {(r.observation_domains ?? []).join(', ') || '—'}
        {r.device_hostname ? ` · device ${r.device_hostname}${r.device_status ? ` (polled status: ${r.device_status})` : ''}` : ''}
      </p>
    </div>
  )
}

const CAP_TONE: Record<Capability['status'], { tone: 'success' | 'warning' | 'danger' | 'info' | 'unknown'; word: string }> = {
  all: { tone: 'success', word: 'Every record' },
  some: { tone: 'warning', word: 'Some records' },
  none: { tone: 'danger', word: 'Not sent' },
  'n/a': { tone: 'info', word: 'Not applicable yet' },
  unknown: { tone: 'unknown', word: 'Unknown' },
}

function FieldsTab({ d }: { d: ExporterHealthDetail }) {
  const f = d.fields
  return (
    <div className="space-y-4">
      <p className="text-xs text-text2">
        What the {formatCountExact(f.records)} records received in the last {Math.round(f.window_s / 60)} minutes carried
        {f.legacy ? ` (${formatCountExact(f.legacy)} older records predate field tracking)` : ''}. Protocols: {protocolList(f.protocols)}
        {f.max_sampling != null ? ` · ${samplingLabel(f.max_sampling)}` : ''}
        {f.direction && (f.direction.ingress || f.direction.egress) ? ` · direction: ${formatCountExact(f.direction.ingress)} ingress, ${formatCountExact(f.direction.egress)} egress` : ''}.
      </p>
      <ul className="grid gap-2 sm:grid-cols-2">
        {f.capabilities.map((c) => {
          const t = CAP_TONE[c.status]
          return (
            <li key={c.key} className="flex items-start justify-between gap-2 rounded-md border border-border px-3 py-2 text-xs">
              <div className="min-w-0">
                <div className="font-medium text-text">{c.label}{c.recommended && <span className="ml-1 text-xs font-normal text-text2">(recommended)</span>}</div>
                <div className="text-xs text-text2">Used for {c.used_for}</div>
              </div>
              <ToneChip tone={c.status === 'none' && !c.recommended ? 'unknown' : t.tone}>
                {t.word}{c.status === 'some' && c.pct != null ? ` (${formatPercent(c.pct)})` : ''}
              </ToneChip>
            </li>
          )
        })}
      </ul>
      <div>
        <h4 className="mb-1.5 text-xs font-semibold text-text">Template inventory</h4>
        {d.templates == null ? (
          <p className="text-xs text-text2">The template inventory could not be read.</p>
        ) : d.templates.length === 0 ? (
          <p className="text-xs text-text2">No templates recorded (NetFlow v5 and sFlow need none).</p>
        ) : (
          <ul className="space-y-2">
            {d.templates.map((t) => <TemplateItem key={`${t.observation_domain}-${t.export_protocol}-${t.template_id}`} t={t} />)}
          </ul>
        )}
      </div>
    </div>
  )
}

function TemplateItem({ t }: { t: ExporterTemplate }) {
  const tz = displayTimeZone()
  const ignored = t.fields.filter((f) => !f.decoded).length
  return (
    <li className="rounded-md border border-border text-xs">
      <details>
        <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2">
          <span className="font-semibold text-text">Template {t.template_id}</span>
          <span className="text-text2">{t.is_options ? 'options' : 'data'} · {protocolList([t.export_protocol])} · domain {t.observation_domain} · {t.fields.length} fields{ignored ? ` (${ignored} not used)` : ''}</span>
          <span className="ml-auto text-text2">last seen {formatDateTime(t.last_seen, tz, { seconds: false })}</span>
        </summary>
        <div className="border-t border-border px-3 py-2">
          <Table>
            <THead>
              <Tr><Th>ID</Th><Th>Field</Th><Th className="text-right">Length</Th><Th>Used</Th></Tr>
            </THead>
            <TBody>
              {t.fields.map((f, i) => (
                <Tr key={i}>
                  <Td className="text-xs tabular-nums">{f.enterprise ? `${f.enterprise}/${f.id}` : f.id}</Td>
                  <Td className="text-xs">{f.name}</Td>
                  <Td className="text-right text-xs tabular-nums">{f.length ?? '—'}</Td>
                  <Td className="text-xs">{f.decoded ? 'Yes' : 'Ignored'}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </details>
    </li>
  )
}

const SPEED_SOURCE: Record<string, string> = {
  configured: 'Circuit speed configured on the device interface',
  override: 'NetFlow interface capacity override',
  snmp: 'Interface speed polled by SNMP',
}

function InterfacesTab({ d }: { d: ExporterHealthDetail }) {
  const rows = d.interfaces ?? []
  const unresolved = rows.filter((i) => !i.resolved).length
  return (
    <div className="space-y-2">
      {unresolved > 0 && (
        <p className="text-xs text-text2">
          {unresolved} of {rows.length} interface indexes have no name:{' '}
          {d.status.device_id
            ? <><Link to={`/devices/${d.status.device_id}`} className="font-medium text-text underline underline-offset-2">{d.status.device_hostname}</Link> has no SNMP inventory for them (check its SNMP polling).</>
            : 'link the exporter to its device (Device link tab) so names and speeds resolve.'}
        </p>
      )}
      <Table>
        <THead>
          <Tr>
            <Th>ifIndex</Th>
            <Th>Name</Th>
            <Th className="text-right">In (24 h)</Th>
            <Th className="text-right">Out (24 h)</Th>
            <Th className="text-right">Speed</Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((i) => (
            <Tr key={i.ifindex}>
              <Td className="text-xs tabular-nums">{i.ifindex}</Td>
              <Td className="text-xs">{i.if_name ?? <span className="text-text2">Unknown</span>}</Td>
              <Td className="text-right text-xs tabular-nums">{formatBytes(i.rx_bytes)}</Td>
              <Td className="text-right text-xs tabular-nums">{formatBytes(i.tx_bytes)}</Td>
              <Td className="text-right text-xs tabular-nums" title={i.speed_source && i.speed_source !== 'unknown' ? SPEED_SOURCE[i.speed_source] : 'No speed known'}>
                {i.if_speed ? formatBps(i.if_speed) : '—'}
                {i.if_speed && i.speed_source && i.speed_source !== 'snmp' && <div className="text-xs text-text2">{i.speed_source === 'configured' ? 'circuit' : 'override'}</div>}
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  )
}
