import { useEffect, useId, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import type { UseQueryResult } from '@tanstack/react-query'
import { Check, ClipboardCopy, Link2, PlugZap, Radar, Router, Send } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile } from '@/components/netflow/StatusTile'
import { ExporterStateBadge } from '@/components/netflow/ExporterStateBadge'
import { nfKeys, useNfQuery } from '@/lib/netflow/api'
import { formatDateTime, ipDisplay } from '@/lib/netflow/format'
import { isIpAddress } from '@/lib/netflow/ip'
import type { NfResult } from '@/lib/netflow/types'
import { displayTimeZone } from '@/lib/netflow/window'
import { cn } from '@/lib/utils'
import { ExporterDeviceDialog } from './components/ExporterDeviceDialog'
import { OVERALL_TEXT, checkPollMs, preferredCollectorAddress, sortExporters, type ExporterCheckResult, type HealthStatus, type SetupInfo } from './health/model'
import {
  RECOMMENDED_FIELDS,
  VENDORS,
  buildSnippet,
  defaultSnippetOptions,
  vendorInfo,
  type ExportFormat,
  type SnippetOptions,
  type VendorId,
} from './health/snippets'
import { CheckIcon, ToneChip } from './health/ui'

const FORMAT_LABEL: Record<ExportFormat, string> = { ipfix: 'IPFIX', netflow9: 'NetFlow v9', sflow: 'sFlow v5' }
const inputCls = 'h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text focus:outline-none focus:ring-2 focus:ring-primary/40'

/**
 * Exporter onboarding (CR-06): where to send flows, a configuration template per platform with
 * the recommended timers and fields, and a live check that polls the collector for the first
 * datagram, templates, stored records, required fields, sampling and the exporter clock.
 */
export function NetflowSetupPage() {
  const [sp, setSp] = useSearchParams()
  const vendor = (VENDORS.some((v) => v.id === sp.get('vendor')) ? sp.get('vendor') : 'cisco-iosxe') as VendorId
  const checkIp = sp.get('check') ?? ''
  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(sp)
    if (v) next.set(k, v)
    else next.delete(k)
    setSp(next, { replace: true })
  }
  const info = useNfQuery<SetupInfo>('/netflow/setup/info', null, { refetchInterval: false, staleTime: 60_000 })
  const addresses = info.data?.data.addresses
  const preferred = useMemo(
    () => (addresses ? preferredCollectorAddress(addresses, typeof window !== 'undefined' ? window.location.hostname : '') : ''),
    [addresses],
  )
  const [collectorAddr, setCollectorAddr] = useState('')
  useEffect(() => {
    if (!collectorAddr && preferred) setCollectorAddr(preferred)
  }, [preferred, collectorAddr])
  const netflowPort = info.data?.data.collector.netflow_port ?? 2055
  const sflowPort = info.data?.data.collector.sflow_port ?? 6343

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title="Add an exporter"
        icon={PlugZap}
        parents={[{ label: 'Collector health', to: '/netflow/health' }]}
        description="Point a router, switch or firewall at this appliance, then confirm here that its flows arrive and carry what ZenPlus needs."
      />
      <DestinationCard info={info} collectorAddr={collectorAddr} setCollectorAddr={setCollectorAddr} />
      <ConfigCard vendor={vendor} setVendor={(v) => setParam('vendor', v)} collector={collectorAddr || '<appliance address>'} netflowPort={netflowPort} sflowPort={sflowPort} />
      <CheckCard ip={checkIp} setIp={(ip) => setParam('check', ip || null)} />
      <RecentCard onCheck={(ip) => setParam('check', ip)} />
    </div>
  )
}

/* ─── 1. Destination ───────────────────────────────────────────────────── */

function DestinationCard({ info, collectorAddr, setCollectorAddr }: {
  info: UseQueryResult<NfResult<SetupInfo>>
  collectorAddr: string
  setCollectorAddr: (v: string) => void
}) {
  const id = useId()
  const listId = useId()
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Send className="h-4 w-4 text-primary" aria-hidden /> 1. Where the device sends flows
        </CardTitle>
      </CardHeader>
      <CardContent>
        <QueryPanel query={info} label="the collector details" skeleton="rows" compact>
          {(r) => {
            const c = r.data.collector
            return (
              <div className="grid gap-4 md:grid-cols-3">
                <div className="space-y-1">
                  <label htmlFor={id} className="text-xs font-medium text-text">Appliance address</label>
                  <input id={id} list={listId} className={inputCls} value={collectorAddr} onChange={(e) => setCollectorAddr(e.target.value.trim())}
                    aria-describedby={`${id}-h`}
                    aria-invalid={!!collectorAddr && !isIpAddress(collectorAddr) && !/^[a-z0-9.-]+$/i.test(collectorAddr)} />
                  <datalist id={listId}>
                    {r.data.addresses.map((a) => <option key={a.address} value={a.address}>{a.interface}{a.virtual ? ' (virtual)' : ''}</option>)}
                  </datalist>
                  <p id={`${id}-h`} className="text-[11px] text-text2">
                    The address the device can reach. This appliance has {r.data.addresses.filter((a) => !a.virtual).map((a) => a.address).join(', ') || 'no global address'}.
                  </p>
                </div>
                <div className="space-y-1">
                  <div className="text-xs font-medium text-text">NetFlow v5 / v9 / IPFIX</div>
                  <div className="text-lg font-semibold tabular-nums text-text">UDP {c.netflow_port}</div>
                  <p className="text-[11px] text-text2">{c.port_source === 'collector' ? `Reported by the collector (${c.listen})` : 'Default port; the collector did not report its own'}</p>
                </div>
                <div className="space-y-1">
                  <div className="text-xs font-medium text-text">sFlow v5</div>
                  <div className="text-lg font-semibold tabular-nums text-text">UDP {c.sflow_port}</div>
                  <div className="flex flex-wrap items-center gap-2 text-[11px] text-text2">
                    Collector
                    <ToneChip tone={!c.reachable ? 'unknown' : c.status === 'ok' ? 'success' : c.status === 'down' ? 'danger' : 'warning'}>
                      {!c.reachable ? 'not answering' : c.status}
                    </ToneChip>
                  </div>
                </div>
              </div>
            )
          }}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}

/* ─── 2. Device configuration ──────────────────────────────────────────── */

function ConfigCard({ vendor, setVendor, collector, netflowPort, sflowPort }: {
  vendor: VendorId
  setVendor: (v: VendorId) => void
  collector: string
  netflowPort: number
  sflowPort: number
}) {
  const v = vendorInfo(vendor)
  const [opts, setOpts] = useState<SnippetOptions>(() => defaultSnippetOptions(vendor, collector, netflowPort, sflowPort))
  useEffect(() => {
    setOpts((o) => ({ ...defaultSnippetOptions(vendor, collector, netflowPort, sflowPort), interfaces: o.interfaces, source: o.source }))
  }, [vendor, collector, netflowPort, sflowPort])
  const snippet = useMemo(() => buildSnippet(vendor, opts), [vendor, opts])
  const [copied, setCopied] = useState(false)
  const ids = { ifs: useId(), src: useId(), rate: useId(), fmt: useId(), v6: useId() }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(snippet)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      toast.error('Could not copy', 'Select the text and copy it manually.')
    }
  }
  const showRate = opts.format === 'sflow' || vendor === 'juniper' || vendor === 'huawei'
  const showV6 = vendor === 'cisco-iosxe' || vendor === 'juniper'
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Router className="h-4 w-4 text-primary" aria-hidden /> 2. Configure the device
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div role="radiogroup" aria-label="Platform" className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {VENDORS.map((x) => (
            <button
              key={x.id}
              type="button"
              role="radio"
              aria-checked={x.id === vendor}
              onClick={() => setVendor(x.id)}
              className={cn(
                'rounded-md border px-3 py-2 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                x.id === vendor ? 'border-primary/60 bg-primary/10' : 'border-border bg-surface hover:bg-surface2',
              )}
            >
              <div className="font-semibold text-text">{x.label}</div>
              <div className="hidden text-[11px] text-text2 min-[480px]:block">{x.platform}</div>
            </button>
          ))}
        </div>

        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
          <div className="space-y-1">
            <label htmlFor={ids.ifs} className="text-xs font-medium text-text">Interfaces</label>
            <input id={ids.ifs} className={inputCls} placeholder={v.interfaceHint} aria-describedby={`${ids.ifs}-h`}
              value={opts.interfaces.join(', ')}
              onChange={(e) => setOpts((o) => ({ ...o, interfaces: e.target.value.split(',').map((s) => s.trim()).filter((s, i, a) => s || i === a.length - 1) }))} />
            <p id={`${ids.ifs}-h`} className="text-[11px] text-text2">Comma-separated; the traffic you want to see (WAN / uplinks).</p>
          </div>
          <div className="space-y-1">
            <label htmlFor={ids.src} className="text-xs font-medium text-text">Export source {v.sourceIsAddress ? 'address' : 'interface'}</label>
            <input id={ids.src} className={inputCls} placeholder={v.sourceHint} value={opts.source} aria-describedby={`${ids.src}-h`}
              onChange={(e) => setOpts((o) => ({ ...o, source: e.target.value }))} />
            <p id={`${ids.src}-h`} className="text-[11px] text-text2">A stable source (loopback): its address is the exporter address ZenPlus sees.</p>
          </div>
          {v.formats.length > 1 && (
            <div className="space-y-1">
              <label htmlFor={ids.fmt} className="text-xs font-medium text-text">Export format</label>
              <select id={ids.fmt} className={inputCls} value={opts.format}
                onChange={(e) => setOpts((o) => ({ ...o, format: e.target.value as ExportFormat, samplingRate: e.target.value === 'sflow' ? 1000 : o.samplingRate }))}>
                {v.formats.map((f) => <option key={f} value={f}>{FORMAT_LABEL[f]}</option>)}
              </select>
            </div>
          )}
          {showRate && (
            <div className="space-y-1">
              <label htmlFor={ids.rate} className="text-xs font-medium text-text">Sampling 1 in N</label>
              <input id={ids.rate} className={inputCls} inputMode="numeric" value={String(opts.samplingRate)} aria-describedby={`${ids.rate}-h`}
                onChange={(e) => setOpts((o) => ({ ...o, samplingRate: Math.max(1, Number(e.target.value.replace(/\D/g, '')) || 1) }))} />
              <p id={`${ids.rate}-h`} className="text-[11px] text-text2">1 = every packet where the platform allows it.</p>
            </div>
          )}
          {showV6 && (
            <label htmlFor={ids.v6} className="flex items-center gap-2 self-center text-xs text-text">
              <input id={ids.v6} type="checkbox" className="h-4 w-4" checked={opts.includeIpv6}
                onChange={(e) => setOpts((o) => ({ ...o, includeIpv6: e.target.checked }))} />
              Include IPv6 flows
            </label>
          )}
        </div>

        <div className="relative">
          <div className="flex items-center justify-between rounded-t-md border border-b-0 border-border bg-surface2/60 px-3 py-1.5 text-[11px] text-text2">
            <span>{v.label} · {FORMAT_LABEL[opts.format] ?? ''} → {collector}:{opts.format === 'sflow' ? sflowPort : netflowPort}</span>
            <button type="button" onClick={copy} className="inline-flex items-center gap-1 font-medium text-text hover:underline" aria-label="Copy the configuration">
              {copied ? <Check className="h-3.5 w-3.5 text-success-fg" aria-hidden /> : <ClipboardCopy className="h-3.5 w-3.5" aria-hidden />}
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="max-h-96 overflow-auto rounded-b-md border border-border bg-surface2/30 p-3 font-mono text-[12px] leading-relaxed text-text" tabIndex={0} aria-label="Device configuration">
            {snippet}
          </pre>
        </div>

        <ul className="list-disc space-y-1 pl-5 text-xs text-text2">
          <li>Timers: active timeout {opts.activeTimeoutS} s, inactive {opts.inactiveTimeoutS} s, template refresh {opts.templateRefreshS} s — long-lived flows report every minute and a restarted collector relearns templates within a minute.</li>
          {v.notes.map((n, i) => <li key={i}>{n}</li>)}
          <li>This is a template: check it against your platform release before applying it.</li>
        </ul>
        <details className="rounded-md border border-border px-3 py-2 text-xs">
          <summary className="cursor-pointer font-semibold text-text">Fields ZenPlus uses</summary>
          <ul className="mt-2 grid gap-1 sm:grid-cols-2">
            {RECOMMENDED_FIELDS.map((f) => (
              <li key={f.field} className="flex items-start gap-2">
                <ToneChip tone={f.required ? 'info' : 'neutral'} icon={null}>{f.required ? 'Needed' : 'Optional'}</ToneChip>
                <span className="text-text">{f.field} <span className="text-text2">— {f.why}</span></span>
              </li>
            ))}
          </ul>
        </details>
      </CardContent>
    </Card>
  )
}

/* ─── 3. Live check ────────────────────────────────────────────────────── */

function CheckCard({ ip, setIp }: { ip: string; setIp: (ip: string) => void }) {
  const [draft, setDraft] = useState(ip)
  useEffect(() => setDraft(ip), [ip])
  const valid = isIpAddress(draft.trim())
  const [mapOpen, setMapOpen] = useState(false)
  const inputId = useId()
  // Quick while waiting for flows; once they arrive the field checks change slowly (the API
  // also reuses its field scan for 30 s).
  const [pollMs, setPollMs] = useState(5_000)
  const check = useNfQuery<ExporterCheckResult>(`/netflow/exporters/${encodeURIComponent(ip)}/check`, null, {
    enabled: !!ip && isIpAddress(ip),
    refetchInterval: pollMs,
    keepPrevious: false,
  })
  const res = check.data?.data
  useEffect(() => setPollMs(checkPollMs(res)), [res])
  const overall = res ? OVERALL_TEXT[res.overall] : null
  const received = res?.checks.some((c) => c.id === 'datagrams' && c.status === 'pass')
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Radar className="h-4 w-4 text-primary" aria-hidden /> 3. Check that flows arrive
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <form
          className="flex flex-col gap-2 sm:flex-row sm:items-end"
          onSubmit={(e) => {
            e.preventDefault()
            if (valid) setIp(draft.trim())
          }}
        >
          <div className="flex-1 space-y-1">
            <label htmlFor={inputId} className="text-xs font-medium text-text">Exporter address (the device&apos;s export source)</label>
            <input id={inputId} className={inputCls} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="e.g. 10.0.0.1"
              aria-invalid={!!draft && !valid} aria-describedby={`${inputId}-h`} />
            <p id={`${inputId}-h`} className="text-[11px] text-text2">
              {draft && !valid ? <span className="text-danger-fg">Enter an IPv4 or IPv6 address</span> : `Checked every ${pollMs / 1000} seconds against what the collector has received.`}
            </p>
          </div>
          <Button type="submit" disabled={!valid} className="sm:mb-5">Check</Button>
        </form>
        {ip && isIpAddress(ip) && (
          <QueryPanel query={check} label="the exporter check" skeleton="rows">
            {(r) => (
              <div className="space-y-3">
                {overall && (
                  <StatusTile tone={overall.tone} word={overall.word} label={`Exporter ${ipDisplay(r.data.exporter)}`}
                    value={overall.word} caption={overall.text} />
                )}
                <ul className="divide-y divide-border rounded-md border border-border" aria-label="Checks">
                  {r.data.checks.map((c) => (
                    <li key={c.id} className="flex flex-col gap-1 px-3 py-2 text-xs sm:flex-row sm:items-start sm:gap-3">
                      <CheckIcon status={c.status} />
                      <div className="min-w-0 flex-1">
                        <div className="font-medium text-text">{c.label}</div>
                        <div className="text-text2">{c.detail}</div>
                        {c.hint && c.status !== 'pass' && <div className="mt-0.5 text-text">{c.hint}</div>}
                      </div>
                    </li>
                  ))}
                </ul>
                <div className="flex flex-wrap gap-2">
                  {received && (
                    <Button size="sm" variant={r.data.status.device_id ? 'outline' : 'default'} onClick={() => setMapOpen(true)}>
                      <Link2 aria-hidden /> {r.data.status.device_id ? 'Review the device link' : 'Link to its device'}
                    </Button>
                  )}
                  <Link to={`/netflow/health?exporter=${encodeURIComponent(r.data.exporter)}`}
                    className="inline-flex h-8 items-center rounded-md border border-border px-3 text-xs font-medium text-text hover:bg-surface2">
                    Open in Collector health
                  </Link>
                  <Link to={`/netflow/health?exporter=${encodeURIComponent(r.data.exporter)}&detail=settings`}
                    className="inline-flex h-8 items-center rounded-md border border-border px-3 text-xs font-medium text-text hover:bg-surface2">
                    Name, role and interval
                  </Link>
                </div>
              </div>
            )}
          </QueryPanel>
        )}
        <ExporterDeviceDialog open={mapOpen} onOpenChange={setMapOpen} exporter={ip} />
      </CardContent>
    </Card>
  )
}

/* ─── Recently heard ───────────────────────────────────────────────────── */

const RECENT_LIMIT = 8

function RecentCard({ onCheck }: { onCheck: (ip: string) => void }) {
  const [showAll, setShowAll] = useState(false)
  const status = useNfQuery<HealthStatus>('/netflow/exporters/status', null, {
    queryKey: nfKeys.exporterStatus(),
    refetchInterval: 15_000,
    keepPrevious: false,
  })
  const tz = displayTimeZone()
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm">Exporters the collector knows</CardTitle>
        <p className="text-xs text-text2">Problems and exporters without a device first.</p>
      </CardHeader>
      <CardContent>
        <QueryPanel
          query={status}
          label="exporters"
          skeleton="rows"
          compact
          isEmpty={(r) => r.data.exporters.length === 0}
          empty="No flows have arrived from any exporter yet"
        >
          {(r) => {
            const all = sortExporters(r.data.exporters).sort((a, b) => Number(!!a.device_id) - Number(!!b.device_id))
            const rows = showAll ? all : all.slice(0, RECENT_LIMIT)
            return (
              <>
              <ul className="divide-y divide-border">
                {rows.map((e) => (
                  <li key={e.exporter} className="flex flex-wrap items-center justify-between gap-2 py-2 text-xs">
                    <span className="min-w-0">
                      <span className="font-medium text-text">{e.display_name || ipDisplay(e.exporter)}</span>
                      {e.display_name && <span className="ml-1.5 font-mono text-text2">{ipDisplay(e.exporter)}</span>}
                      <span className="block text-[11px] text-text2">
                        {e.device_hostname ? `Device ${e.device_hostname}` : 'No device linked'}
                        {e.last_received_at ? ` · last flow ${formatDateTime(e.last_received_at, tz, { seconds: false })}` : ''}
                      </span>
                    </span>
                    <span className="flex items-center gap-2">
                      <ExporterStateBadge state={e.state} />
                      <Button size="sm" variant="outline" className="h-7" onClick={() => onCheck(e.exporter)}
                        aria-label={`Check ${e.display_name || ipDisplay(e.exporter)}`}>Check</Button>
                    </span>
                  </li>
                ))}
              </ul>
              {all.length > RECENT_LIMIT && (
                <button type="button" onClick={() => setShowAll((v) => !v)} aria-expanded={showAll}
                  className="mt-2 text-xs font-medium text-text underline-offset-2 hover:underline">
                  {showAll ? 'Show fewer' : `Show all ${all.length} exporters`}
                </button>
              )}
              </>
            )
          }}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}
