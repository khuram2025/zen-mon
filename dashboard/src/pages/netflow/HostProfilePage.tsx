/**
 * Host profile (CR-14, FE-09): one address as the appliance knows it — name, groups, location,
 * switch port and user (UDT), threat-intelligence and findings — and its traffic in the page
 * window: in/out rate, peers, applications, service ports, exporters and interfaces.
 *
 * Every section has its own state: a part that failed or is not permitted says so instead of
 * showing zeros, and quiet periods before collection started are gaps, not zero traffic.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import {
  Activity,
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  Boxes,
  Cable,
  Clock,
  Copy,
  Globe2,
  Loader2,
  MonitorSmartphone,
  Network,
  Router,
  Search,
  ShieldAlert,
  Table2,
  Tag,
  UserSearch,
  Users,
} from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { TimeRangePicker } from '@/components/TimeRangePicker'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { PanelMessage, QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile } from '@/components/netflow/StatusTile'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { FilterChips } from '@/components/netflow/FilterChips'
import { useNfQuery, nfParams } from '@/lib/netflow/api'
import { removeFlowFilterKeys, type FlowFilter } from '@/lib/netflow/filters'
import {
  formatBps,
  formatBpsAxis,
  formatBpsExact,
  formatBytes,
  formatBytesExact,
  formatCount,
  formatCountExact,
  formatDateTime,
  formatInterval,
  ipDisplay,
} from '@/lib/netflow/format'
import { parseIpOrCidr } from '@/lib/netflow/ip'
import { buildNetflowLink, exporterPath, hostPath } from '@/lib/netflow/links'
import { apiErrorMessage, apiErrorRef, httpStatus } from '@/lib/netflow/status'
import type { NfMeta } from '@/lib/netflow/types'
import { useFlowFilter } from '@/lib/netflow/useFlowFilter'
import { displayTimeZone, useNfWindow, type NfWindow } from '@/lib/netflow/window'
import { cn, copyText, relativeTime } from '@/lib/utils'
import { chartFont } from '@/lib/chartTheme'

/* ─── Response shape ─────────────────────────────────────────────────────────────────────── */

type SectionState = { status: string; error_id?: string | null; message?: string | null }

export interface HostPoint {
  t: string
  bytes_in: number | null
  bytes_out: number | null
  packets_in: number | null
  packets_out: number | null
  flows: number | null
  covered: boolean
  partial: boolean
}

export interface HostProfile {
  ip: string
  version: 4 | 6
  netclass: string
  seen: { first_seen: string | null; last_seen: string | null; flows: number; retention_days: number | null; source: string } | null
  timeseries: {
    bucket_s: number
    points: HostPoint[]
    totals: { bytes_in: number; bytes_out: number; packets_in: number; packets_out: number; flows: number }
  } | null
  peers: {
    items: { ip: string; bytes_in: number; bytes_out: number; bytes: number; flows: number; group: string | null; country: string | null; asn: number | null; as_name: string | null }[]
    total: number
    truncated: boolean
  } | null
  exporters: { items: { exporter: string; device_id: string | null; device_hostname: string | null; bytes: number; flows: number; first_seen: string | null; last_seen: string | null }[] } | null
  applications: { items: { application: string; category: string; bytes: number; flows: number; bytes_in: number; bytes_out: number }[] } | null
  ports: { items: { protocol: number; protocol_name: string; port: number; service: string | null; role: 'server' | 'client' | 'unknown'; application: string; bytes: number; flows: number }[] } | null
  interfaces: {
    items: { exporter: string; ifindex: number; direction: 'in' | 'out'; name: string; if_alias: string | null; device_hostname: string | null; bytes: number; flows: number }[]
    source: string
    from: string | null
    to: string | null
  } | null
  rdns: { status: 'ok' | 'no_record' | 'pending' | 'disabled' | 'failed'; hostname: string | null; resolved_at?: string | null; message?: string; ttl_s?: number }
  geoip: {
    status: 'ok' | 'not_applicable' | 'no_database' | 'no_match' | 'failed'
    country?: string | null
    country_name?: string | null
    asn?: number | null
    as_name?: string | null
    message?: string
    database?: { country_built: string | null; asn_built: string | null }
  }
  groups: { primary: string | null; items: { id: string | null; name: string; color: string | null; cidr: string | null; via_subgroup: boolean }[] }
  device: { id: string; hostname: string; status: string | null } | null
  identity: {
    status: 'ok' | 'forbidden' | 'not_found' | 'unavailable'
    message?: string
    endpoint?: {
      id: string
      mac: string
      vendor: string | null
      hostname: string | null
      type: string | null
      randomized_mac: boolean
      authorized: boolean | null
      watched: boolean
      first_seen: string | null
      last_seen: string | null
      current_ip: boolean
      ip_last_seen: string | null
    }
    location?: { switch_id: string; switch: string; if_index: number; if_name: string; if_alias: string | null; vlan_id: number | null; direct: boolean; active: boolean; last_seen: string | null } | null
    location_hidden?: boolean
    user?: { status: 'ok' | 'forbidden' | 'none'; user_name?: string; user_domain?: string | null; at?: string | null; source?: string; message?: string }
    other_macs?: { mac: string; vendor: string | null; last_seen: string | null }[]
  }
  threat_intel: {
    status: 'ok' | 'no_feeds' | 'unavailable'
    message?: string
    feeds_enabled?: number
    refreshed_at?: string | null
    host_hits: { feed: string; category: string; prefix: string; refreshed_at?: string | null }[]
    peer_hits: { ip: string; feed: string; category: string; prefix: string }[]
  }
  findings: {
    status: 'ok' | 'unavailable'
    message?: string
    counts?: { open: number; acknowledged: number; suppressed: number; resolved: number }
    items?: { id: string; kind: string; severity: string; confidence: string; status: string; title: string; src_ip: string | null; dst_ip: string | null; exporter_ip: string | null; first_seen: string | null; last_seen: string | null; occurrences: number; role: string }[]
  }
  sections: Record<string, SectionState>
}

/* ─── Pure helpers (exported for scripts/test-netflow-settings.mjs) ──────────────────────── */

export interface ChartPoint {
  t: number
  in_bps: number | null
  out_bps: number | null
  covered: boolean
}

/**
 * Bit rates per bucket. A bucket cut by the window edge is divided by the part of it inside the
 * effective window, not the whole bucket, so edge buckets are not understated. Uncovered buckets
 * stay null (a gap), never zero.
 */
export function toChartPoints(points: HostPoint[], bucketS: number, effFrom?: string | null, effTo?: string | null): ChartPoint[] {
  const ef = effFrom ? Date.parse(effFrom) : NaN
  const et = effTo ? Date.parse(effTo) : NaN
  return points.map((p) => {
    const t = Date.parse(p.t)
    let seconds = bucketS
    if (p.partial && Number.isFinite(ef) && Number.isFinite(et)) {
      const lo = Math.max(t, ef)
      const hi = Math.min(t + bucketS * 1000, et)
      seconds = Math.max(1, (hi - lo) / 1000)
    }
    const rate = (b: number | null) => (b == null ? null : (b * 8) / seconds)
    return { t, in_bps: rate(p.bytes_in), out_bps: rate(p.bytes_out), covered: p.covered }
  })
}

/** The page filter without the host dimension (the host is the path, not a filter). */
export function hostContextFilter(filter: FlowFilter): FlowFilter {
  return removeFlowFilterKeys(filter, ['talker'])
}

/** Filter for "this host's flows": the page filter plus talker = host. */
export function hostFlowFilter(filter: FlowFilter, ip: string): FlowFilter {
  return { ...hostContextFilter(filter), talker: ip }
}

export function roleLabel(role: 'server' | 'client' | 'unknown'): string {
  return role === 'server' ? 'Serves' : role === 'client' ? 'Uses' : 'Unclear side'
}

/** Why a section has no data, in words (null when it is fine). */
export function sectionProblem(s: SectionState | undefined): string | null {
  if (!s || s.status === 'ok') return null
  if (s.status === 'timeout') return 'This part took too long and was stopped; narrow the time range.'
  if (s.status === 'unavailable') return 'Flow data is temporarily unavailable.'
  return s.message?.replace(/\s*\(ref nf-[0-9a-f]{8}\)\s*$/i, '') || 'This part could not be loaded.'
}

/* ─── Small pieces ───────────────────────────────────────────────────────────────────────── */

const IN_COLOR = '[--nf-in:#2a78d6] dark:[--nf-in:#3987e5]'
const OUT_COLOR = '[--nf-out:#eb6834] dark:[--nf-out:#d95926]'

function Section({ title, icon: Icon, children, action, className }: { title: string; icon: typeof Activity; children: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <Card className={className}>
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Icon className="h-4 w-4 text-text2" aria-hidden />
          {title}
        </CardTitle>
        {action}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function SectionFailure({ state, label }: { state: SectionState | undefined; label: string }) {
  const problem = sectionProblem(state)
  if (!problem) return null
  return (
    <PanelMessage
      className="py-6"
      icon={AlertTriangle}
      iconClass="text-danger-fg"
      title={`Could not load ${label}`}
      detail={problem}
      errorRef={state?.error_id ?? null}
    />
  )
}

function Fact({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-text2">{label}</dt>
      <dd className="break-words text-sm text-text" title={title}>
        {children}
      </dd>
    </div>
  )
}

function Muted({ children }: { children: ReactNode }) {
  return <span className="text-xs text-text2">{children}</span>
}

function IdentityCard({ title, icon: Icon, children }: { title: string; icon: typeof Activity; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-lg border border-border bg-surface p-3 shadow-card" aria-label={title}>
      <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-text2">
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {title}
      </h2>
      <dl className="space-y-2">{children}</dl>
    </section>
  )
}

function Bar({ value, max, className }: { value: number; max: number; className?: string }) {
  const pct = max > 0 ? Math.max(1, Math.min(100, (100 * value) / max)) : 0
  return (
    <span className="mt-1 block h-1.5 w-full overflow-hidden rounded-full bg-surface2" aria-hidden>
      <span className={cn('block h-full rounded-full', className ?? 'bg-primary/70')} style={{ width: `${pct}%` }} />
    </span>
  )
}

/* ─── Page ───────────────────────────────────────────────────────────────────────────────── */

export function NetflowHostProfilePage() {
  const { ip = '' } = useParams<{ ip: string }>()
  const parsed = parseIpOrCidr(ip)
  const valid = !!parsed && parsed.prefix === null
  const { window: w, rangeIdx, isCustom, setPreset, setCustom, range } = useNfWindow()
  const { filter, invalid, errors, removeKeys, clearAll } = useFlowFilter()
  const ctxFilter = useMemo(() => hostContextFilter(filter), [filter])
  const params = nfParams(w, ctxFilter)
  const q = useNfQuery<HostProfile>(`/netflow/hosts/${encodeURIComponent(ip)}`, params, { window: w, enabled: valid })
  const shown = ipDisplay(ip)
  const data = q.data?.data
  const hostname = data?.rdns?.status === 'ok' ? data.rdns.hostname : null

  // A reverse lookup that missed the deadline keeps running on the appliance: ask again shortly.
  const [rdnsRetries, setRdnsRetries] = useState(0)
  useEffect(() => {
    if (data?.rdns?.status !== 'pending' || rdnsRetries >= 3) return
    const h = window.setTimeout(() => {
      setRdnsRetries((n) => n + 1)
      void q.refetch()
    }, 4000)
    return () => window.clearTimeout(h)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.rdns?.status, rdnsRetries])

  const flowsLink = buildNetflowLink('/netflow/forensics', { filter: hostFlowFilter(ctxFilter, ip), window: w, extra: { run: true } })
  const overviewLink = buildNetflowLink('/netflow', { filter: hostFlowFilter(ctxFilter, ip), window: w })
  const [copied, setCopied] = useState(false)

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title={
          <span className="min-w-0">
            <span className="font-mono">{shown}</span>
            {hostname && <span className="ml-2 break-all text-base font-normal text-text2">{hostname}</span>}
          </span>
        }
        crumb={shown}
        icon={UserSearch}
        description="Everything the appliance knows about this address, and its traffic in the selected period as seen by the exporters you can access."
        actions={
          <TimeRangePicker
            rangeIdx={rangeIdx}
            isCustom={isCustom}
            customFrom={range.fromISO}
            customTo={range.toISO}
            onPreset={setPreset}
            onCustom={setCustom}
          />
        }
      />
      {!valid ? (
        <Card>
          <CardContent className="pt-5">
            <PanelMessage
              className="py-8"
              icon={Search}
              title={`"${ip}" is not a single IP address`}
              detail="Host profiles are addressed by a single IPv4 or IPv6 address, e.g. /netflow/hosts/10.0.0.5."
            />
            {parsed && (
              <p className="pb-4 text-center text-sm">
                <Link
                  to={buildNetflowLink('/netflow', { window: w, filter: { ...ctxFilter, talker: ip } })}
                  className="text-primary underline-offset-2 hover:underline"
                >
                  Traffic of the range {ip} in the overview
                </Link>
              </p>
            )}
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Button asChild variant="outline" size="sm">
              <Link to={flowsLink}>
                <Table2 aria-hidden />
                Raw flows for this host
              </Link>
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to={overviewLink}>
                <Activity aria-hidden />
                Overview filtered to this host
              </Link>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={async () => {
                setCopied(await copyText(window.location.href))
                window.setTimeout(() => setCopied(false), 2000)
              }}
              aria-label="Copy a link to this profile"
            >
              <Copy aria-hidden />
              {copied ? 'Copied' : 'Copy link'}
            </Button>
          </div>
          <FilterChips filter={ctxFilter} invalid={invalid} errors={errors} onRemove={removeKeys} onClearAll={clearAll} tz={w.tz} />
          {q.status === 'error' && httpStatus(q.error) === 404 ? (
            <Card>
              <CardContent className="pt-5">
                <PanelMessage
                  className="py-8"
                  icon={Search}
                  title={`No profile for ${shown}`}
                  detail="None of the exporters you can access has seen this address within the retention period, or it is outside your access."
                />
              </CardContent>
            </Card>
          ) : (
            <QueryPanel query={q} label="the host profile" skeleton="tiles" isEmpty={() => false}>
              {(r) => <HostProfileBody p={r.data} meta={r.meta} window={w} filter={ctxFilter} />}
            </QueryPanel>
          )}
        </>
      )}
    </div>
  )
}

function HostProfileBody({ p, meta, window: w, filter }: { p: HostProfile; meta: NfMeta | null; window: NfWindow; filter: FlowFilter }) {
  const tz = displayTimeZone()
  const totals = p.timeseries?.totals
  const tiHits = p.threat_intel.host_hits ?? []
  const openFindings = (p.findings.counts?.open ?? 0) + (p.findings.counts?.acknowledged ?? 0)
  const peerTi = new Map<string, string[]>()
  for (const h of p.threat_intel.peer_hits ?? []) peerTi.set(h.ip, [...(peerTi.get(h.ip) ?? []), `${h.feed} (${h.category})`])
  const link = (path: string, extra?: Partial<FlowFilter>, more?: Record<string, string | number | boolean>) =>
    buildNetflowLink(path, { window: w, filter: { ...hostFlowFilter(filter, p.ip), ...(extra ?? {}) }, extra: more })
  const quiet = p.seen && p.seen.flows === 0
  const [allExporters, setAllExporters] = useState(false)
  const [allIfaces, setAllIfaces] = useState(false)

  return (
    <div className="space-y-4">
      {(tiHits.length > 0 || openFindings > 0) && (
        <div role="alert" className="flex flex-wrap items-start gap-2 rounded-lg border border-l-4 border-danger/35 border-l-danger bg-danger/10 p-3 text-sm text-text">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-danger-fg" aria-hidden />
          <div className="min-w-0">
            <span className="font-semibold text-danger-fg">Needs attention: </span>
            {tiHits.length > 0 && <span>listed by {tiHits.map((h) => h.feed).join(', ')}. </span>}
            {openFindings > 0 && (
              <Link to={link('/netflow/anomalies')} className="underline-offset-2 hover:underline">
                {openFindings} open finding{openFindings === 1 ? '' : 's'}.
              </Link>
            )}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <IdentityCard title="Name and network" icon={Network}>
          <Fact label="Reverse DNS" title={p.rdns.resolved_at ? `Resolved ${formatDateTime(p.rdns.resolved_at, tz)}` : undefined}>
            {p.rdns.status === 'ok' ? (
              <>
                <span className="break-all font-medium">{p.rdns.hostname}</span>
                {p.rdns.resolved_at && <Muted> · resolved {relativeTime(p.rdns.resolved_at)}</Muted>}
              </>
            ) : p.rdns.status === 'pending' ? (
              <span className="inline-flex items-center gap-1 text-text2">
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden /> Still resolving…
              </span>
            ) : p.rdns.status === 'no_record' ? (
              <Muted>No PTR record{p.rdns.resolved_at ? ` (checked ${relativeTime(p.rdns.resolved_at)})` : ''}</Muted>
            ) : p.rdns.status === 'disabled' ? (
              <Muted>Lookups are turned off in NetFlow settings</Muted>
            ) : (
              <Muted>Lookup failed</Muted>
            )}
          </Fact>
          <Fact label="Address class">
            {p.netclass} · IPv{p.version}
          </Fact>
          <Fact label="IP groups">
            {p.groups.items.length === 0 ? (
              <Muted>In no group</Muted>
            ) : (
              <span className="flex flex-wrap gap-1">
                {p.groups.items.map((g) => (
                  <Link
                    key={g.name}
                    to={buildNetflowLink('/netflow', { window: w, filter: { ...filter, group: g.name } })}
                    className={cn(
                      'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs hover:bg-surface2',
                      g.name === p.groups.primary ? 'border-primary/40 font-medium text-text' : 'border-border text-text2',
                    )}
                    title={g.cidr ? `Matched ${g.cidr}` : 'Through a sub-group'}
                  >
                    <span className="h-2 w-2 rounded-full border border-border-strong" style={{ background: g.color ?? 'transparent' }} aria-hidden />
                    {g.name}
                  </Link>
                ))}
              </span>
            )}
          </Fact>
          {p.device && (
            <Fact label="Managed device">
              <Link to={`/devices/${p.device.id}`} className="font-medium hover:underline">
                {p.device.hostname}
              </Link>
              {p.device.status && <Muted> · {p.device.status}</Muted>}
            </Fact>
          )}
        </IdentityCard>

        <IdentityCard title="Location" icon={Globe2}>
          {p.geoip.status === 'ok' ? (
            <>
              <Fact label="Country">{p.geoip.country_name ?? p.geoip.country ?? <Muted>Unknown</Muted>}</Fact>
              <Fact label="Autonomous system">
                {p.geoip.asn != null ? (
                  <>
                    AS{p.geoip.asn}
                    {p.geoip.as_name && <Muted> · {p.geoip.as_name}</Muted>}
                  </>
                ) : (
                  <Muted>Unknown</Muted>
                )}
              </Fact>
              <p className="text-xs text-text2">
                GeoIP database built {p.geoip.database?.country_built ? formatDateTime(p.geoip.database.country_built, tz, { seconds: false, zone: 'none' }).slice(0, 10) : 'on an unknown date'}
              </p>
            </>
          ) : (
            <Fact label="GeoIP">
              <Muted>
                {p.geoip.status === 'no_match'
                  ? 'Not in the GeoIP database'
                  : p.geoip.message ?? 'Unavailable'}
              </Muted>
            </Fact>
          )}
        </IdentityCard>

        <IdentityCard title="Endpoint (User Device Tracker)" icon={MonitorSmartphone}>
          <EndpointFacts p={p} />
        </IdentityCard>

        <IdentityCard title="History" icon={Clock}>
          {p.seen ? (
            p.seen.flows > 0 ? (
              <>
                <Fact label="First seen" title={formatDateTime(p.seen.first_seen, tz)}>
                  {formatDateTime(p.seen.first_seen, tz, { seconds: false })}
                </Fact>
                <Fact label="Last seen" title={formatDateTime(p.seen.last_seen, tz)}>
                  {formatDateTime(p.seen.last_seen, tz, { seconds: false })} <Muted>({relativeTime(p.seen.last_seen)})</Muted>
                </Fact>
                <p className="text-xs text-text2">
                  Within the {p.seen.retention_days ?? '?'}-day rollup retention, exporters you can access.
                </p>
              </>
            ) : (
              <Fact label="Flows within retention">
                <Muted>None from the exporters you can access in the last {p.seen.retention_days ?? '?'} days</Muted>
              </Fact>
            )
          ) : (
            <SectionFailure state={p.sections.seen} label="first and last seen" />
          )}
        </IdentityCard>

        <IdentityCard title="Security" icon={ShieldAlert}>
          <Fact label="Threat intelligence">
            {p.threat_intel.status === 'ok' ? (
              tiHits.length ? (
                <span className="font-medium text-danger-fg">
                  Listed: {tiHits.map((h) => `${h.feed} (${h.category}, ${h.prefix})`).join('; ')}
                </span>
              ) : (
                <span>
                  Not on any of {p.threat_intel.feeds_enabled} enabled feed{p.threat_intel.feeds_enabled === 1 ? '' : 's'}
                  {p.threat_intel.refreshed_at && <Muted> · updated {relativeTime(p.threat_intel.refreshed_at)}</Muted>}
                </span>
              )
            ) : (
              <Muted>{p.threat_intel.message ?? 'Unavailable'}</Muted>
            )}
          </Fact>
          <Fact label="Detection findings">
            {p.findings.status === 'ok' ? (
              <span>
                {p.findings.counts?.open ?? 0} open · {p.findings.counts?.acknowledged ?? 0} acknowledged
                <Muted>
                  {' '}
                  · {p.findings.counts?.resolved ?? 0} resolved, {p.findings.counts?.suppressed ?? 0} suppressed
                </Muted>
              </span>
            ) : (
              <Muted>{p.findings.message ?? 'Unavailable'}</Muted>
            )}
          </Fact>
        </IdentityCard>
      </div>

      {quiet && (
        <PanelMessage
          className="py-4"
          icon={Search}
          title="No flow records for this address within retention"
          detail="Identity details above come from the rest of ZenPlus. Traffic sections stay empty until an exporter you can access reports it."
        />
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatusTile tone="neutral" label="Received" icon={ArrowDownLeft} word={null} value={totals ? formatBytes(totals.bytes_in) : null} title={totals ? formatBytesExact(totals.bytes_in) : undefined} caption="Host as destination" />
        <StatusTile tone="neutral" label="Sent" icon={ArrowUpRight} word={null} value={totals ? formatBytes(totals.bytes_out) : null} title={totals ? formatBytesExact(totals.bytes_out) : undefined} caption="Host as source" />
        <StatusTile tone="neutral" label="Flow records" value={totals ? formatCount(totals.flows) : null} title={totals ? formatCountExact(totals.flows) : undefined} caption="Either direction" />
        <StatusTile tone="neutral" label="Peers" value={p.peers ? formatCount(p.peers.total) : null} caption="Distinct addresses it talked to" />
      </div>

      <TrafficChart p={p} meta={meta} />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Section title="Top peers" icon={Users}>
          {p.peers ? (
            p.peers.items.length ? (
              <PeersTable p={p} window={w} filter={filter} peerTi={peerTi} />
            ) : (
              <PanelMessage className="py-6" icon={Search} title="No peers in this period" />
            )
          ) : (
            <SectionFailure state={p.sections.peers} label="peers" />
          )}
        </Section>

        <div className="space-y-4">
          <Section title="Applications" icon={Boxes}>
            {p.applications ? (
              p.applications.items.length ? (
                <ul className="space-y-2">
                  {p.applications.items.slice(0, 10).map((a) => (
                    <li key={a.application} className="text-sm">
                      <div className="flex items-baseline justify-between gap-2">
                        <Link to={link('/netflow/forensics', { app: a.application }, { run: true })} className="min-w-0 break-words font-medium text-text hover:underline">
                          {a.application}
                        </Link>
                        <span className="shrink-0 tabular-nums text-text" title={formatBytesExact(a.bytes)}>{formatBytes(a.bytes)}</span>
                      </div>
                      <div className="flex justify-between gap-2 text-xs text-text2">
                        <span>{a.category}</span>
                        <span>
                          ↓ {formatBytes(a.bytes_in)} · ↑ {formatBytes(a.bytes_out)} · {formatCount(a.flows)} flows
                        </span>
                      </div>
                      <Bar value={a.bytes} max={p.applications!.items[0].bytes} />
                    </li>
                  ))}
                </ul>
              ) : (
                <PanelMessage className="py-6" icon={Search} title="No traffic in this period" />
              )
            ) : (
              <SectionFailure state={p.sections.applications} label="applications" />
            )}
          </Section>

          <Section title="Service ports" icon={Tag}>
            {p.ports ? (
              p.ports.items.length ? (
                <ul className="divide-y divide-border">
                  {p.ports.items.slice(0, 12).map((s, i) => (
                    <li key={`${s.protocol}-${s.port}-${s.role}-${i}`} className="flex items-center justify-between gap-2 py-1.5 text-sm">
                      <span className="min-w-0">
                        <Link
                          to={link('/netflow/forensics', { protocol: s.protocol, service_port: s.port }, { run: true })}
                          className="font-medium text-text hover:underline"
                        >
                          {s.service ?? `${s.protocol_name}/${s.port}`}
                        </Link>
                        <span className="ml-1.5 font-mono text-xs text-text2">
                          {s.port}/{s.protocol_name.toLowerCase()}
                        </span>
                        <span
                          className={cn(
                            'ml-1.5 rounded px-1 py-px text-2xs font-medium',
                            s.role === 'server' ? 'bg-info/10 text-info-fg' : 'bg-surface2 text-text2',
                          )}
                        >
                          {roleLabel(s.role)}
                        </span>
                      </span>
                      <span className="shrink-0 text-right tabular-nums">
                        {formatBytes(s.bytes)}
                        <span className="block text-xs text-text2">{formatCount(s.flows)} flows</span>
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <PanelMessage className="py-6" icon={Search} title="No port-carrying traffic in this period" />
              )
            ) : (
              <SectionFailure state={p.sections.ports} label="service ports" />
            )}
            <p className="mt-2 text-xs text-text2">"Serves" means the service port was on this host's side; "Uses" means it connected to that service elsewhere.</p>
          </Section>
        </div>

        <Section title="Exporters that saw it" icon={Router}>
          {p.exporters ? (
            p.exporters.items.length ? (
              <ul className="divide-y divide-border">
                {(allExporters ? p.exporters.items : p.exporters.items.slice(0, 8)).map((e) => (
                  <li key={e.exporter} className="flex items-center justify-between gap-2 py-1.5 text-sm">
                    <span className="min-w-0">
                      <Link to={buildNetflowLink(exporterPath(e.exporter), { window: w, filter: hostFlowFilter(filter, p.ip) })} className="font-medium text-text hover:underline">
                        {e.device_hostname ?? e.exporter}
                      </Link>
                      {e.device_hostname && <span className="ml-1.5 font-mono text-xs text-text2">{e.exporter}</span>}
                      <span className="block text-xs text-text2">Last seen {relativeTime(e.last_seen)}</span>
                    </span>
                    <span className="shrink-0 text-right tabular-nums">
                      {formatBytes(e.bytes)}
                      <span className="block text-xs text-text2">{formatCount(e.flows)} flows</span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <PanelMessage className="py-6" icon={Search} title="No exporter reported it in this period" />
            )
          ) : (
            <SectionFailure state={p.sections.exporters} label="exporters" />
          )}
          {(p.exporters?.items.length ?? 0) > 8 && (
            <Button variant="ghost" size="sm" onClick={() => setAllExporters((v) => !v)} aria-expanded={allExporters}>
              {allExporters ? 'Show 8' : `Show all ${p.exporters!.items.length}`}
            </Button>
          )}
          <p className="mt-2 text-xs text-text2">The same flow seen by several exporters is counted once per exporter.</p>
        </Section>

        <Section title="Interfaces" icon={Cable}>
          {p.interfaces ? (
            p.interfaces.items.length ? (
              <ul className="divide-y divide-border">
                {(allIfaces ? p.interfaces.items : p.interfaces.items.slice(0, 8)).map((i, k) => (
                  <li key={`${i.exporter}-${i.ifindex}-${i.direction}-${k}`} className="flex items-center justify-between gap-2 py-1.5 text-sm">
                    <span className="min-w-0">
                      <Link
                        to={buildNetflowLink(exporterPath(i.exporter), {
                          window: w,
                          filter: { ...hostFlowFilter(filter, p.ip), iface: `${i.ifindex}@${i.exporter}`, iface_dir: i.direction },
                        })}
                        className="font-medium text-text hover:underline"
                      >
                        {i.name}
                      </Link>
                      <span className="block text-xs text-text2">
                        {i.device_hostname ?? i.exporter} · {i.direction === 'in' ? 'flows entered here' : 'flows left here'}
                      </span>
                    </span>
                    <span className="shrink-0 text-right tabular-nums">
                      {formatBytes(i.bytes)}
                      <span className="block text-xs text-text2">{formatCount(i.flows)} flows</span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <PanelMessage className="py-6" icon={Search} title="No interface information in raw records for this period" />
            )
          ) : (
            <SectionFailure state={p.sections.interfaces} label="interfaces" />
          )}
          {(p.interfaces?.items.length ?? 0) > 8 && (
            <Button variant="ghost" size="sm" onClick={() => setAllIfaces((v) => !v)} aria-expanded={allIfaces}>
              {allIfaces ? 'Show 8' : `Show all ${p.interfaces!.items.length}`}
            </Button>
          )}
          {p.interfaces?.from && (
            <p className="mt-2 text-xs text-text2">
              From raw flow records, {formatDateTime(p.interfaces.from, tz, { seconds: false })} → {formatDateTime(p.interfaces.to, tz, { seconds: false })} (at most the last 7 days of the period).
            </p>
          )}
        </Section>
      </div>

      {p.findings.status === 'ok' && (p.findings.items?.length ?? 0) > 0 && (
        <Section title="Open findings" icon={ShieldAlert}>
          <ul className="divide-y divide-border">
            {p.findings.items!.map((f) => (
              <li key={f.id} className="py-2 text-sm">
                <Link to={link('/netflow/anomalies', undefined, { finding: f.id })} className="font-medium text-text hover:underline">
                  {f.title}
                </Link>
                <div className="text-xs text-text2">
                  {f.severity} · {f.status} · host is the {f.role} · {f.occurrences} occurrence{f.occurrences === 1 ? '' : 's'} · last {relativeTime(f.last_seen)}
                </div>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <DataQualityBar meta={meta} window={w} filter={hostFlowFilter(filter, p.ip)} />
    </div>
  )
}

function EndpointFacts({ p }: { p: HostProfile }) {
  const id = p.identity
  if (id.status !== 'ok' || !id.endpoint) {
    return (
      <Fact label="Identity">
        <Muted>{id.message ?? 'Unavailable'}</Muted>
      </Fact>
    )
  }
  const e = id.endpoint
  return (
    <>
      <Fact label="MAC address">
        <Link to={`/udt/endpoints/${e.id}`} className="font-mono hover:underline">
          {e.mac}
        </Link>
        {e.vendor && <Muted> · {e.vendor}</Muted>}
        {e.randomized_mac && <Muted> · randomized</Muted>}
      </Fact>
      {!e.current_ip && (
        <p className="text-xs text-warning-fg">
          This MAC used the address earlier{e.ip_last_seen ? ` (last ${relativeTime(e.ip_last_seen)})` : ''}; it has another address now.
        </p>
      )}
      <Fact label="Switch port">
        {id.location ? (
          <>
            <Link to={`/devices/${id.location.switch_id}`} className="font-medium hover:underline">
              {id.location.switch}
            </Link>{' '}
            {id.location.if_name}
            {id.location.vlan_id != null && <Muted> · VLAN {id.location.vlan_id}</Muted>}
            {!id.location.active && <Muted> · last seen {relativeTime(id.location.last_seen)}</Muted>}
          </>
        ) : id.location_hidden ? (
          <Muted>On a switch outside your access</Muted>
        ) : (
          <Muted>Not located on a switch port</Muted>
        )}
      </Fact>
      <Fact label="Last user">
        {id.user?.status === 'ok' ? (
          <>
            {id.user.user_domain ? `${id.user.user_domain}\\` : ''}
            {id.user.user_name}
            {id.user.at && <Muted> · {relativeTime(id.user.at)}</Muted>}
          </>
        ) : (
          <Muted>{id.user?.message ?? 'Unknown'}</Muted>
        )}
      </Fact>
      {(id.other_macs?.length ?? 0) > 0 && (
        <p className="text-xs text-text2">
          {id.other_macs!.length} other MAC{id.other_macs!.length === 1 ? '' : 's'} used this address: {id.other_macs!.map((m) => m.mac).join(', ')}
        </p>
      )}
    </>
  )
}

function PeersTable({ p, window: w, filter, peerTi }: { p: HostProfile; window: NfWindow; filter: FlowFilter; peerTi: Map<string, string[]> }) {
  const [all, setAll] = useState(false)
  const loaded = p.peers!.items
  const items = all ? loaded : loaded.slice(0, 10)
  const max = loaded[0]?.bytes ?? 0
  const peerLink = (ip: string) => buildNetflowLink(hostPath(ip), { window: w, filter })
  return (
    <div className="space-y-2">
      <ul className="space-y-2">
        {items.map((x) => {
          const ti = peerTi.get(x.ip)
          return (
            <li key={x.ip} className="text-sm">
              <div className="flex items-baseline justify-between gap-2">
                <span className="min-w-0">
                  <Link to={peerLink(x.ip)} className="break-all font-mono font-medium text-text hover:underline">
                    {ipDisplay(x.ip)}
                  </Link>
                  {ti && (
                    <span className="ml-1.5 inline-flex items-center gap-0.5 rounded bg-danger/10 px-1 py-px text-2xs font-medium text-danger-fg" title={ti.join('\n')}>
                      <ShieldAlert className="h-3 w-3" aria-hidden /> Threat list
                    </span>
                  )}
                </span>
                <span className="shrink-0 tabular-nums text-text" title={formatBytesExact(x.bytes)}>{formatBytes(x.bytes)}</span>
              </div>
              <div className="flex flex-wrap justify-between gap-x-2 text-xs text-text2">
                <span>
                  {[x.group, x.country, x.asn != null ? `AS${x.asn}${x.as_name ? ` ${x.as_name}` : ''}` : null].filter(Boolean).join(' · ') || '—'}
                </span>
                <span>
                  ↓ {formatBytes(x.bytes_in)} · ↑ {formatBytes(x.bytes_out)} · {formatCount(x.flows)} flows
                </span>
              </div>
              <Bar value={x.bytes} max={max} />
            </li>
          )
        })}
      </ul>
      {loaded.length > 10 && (
        <Button variant="ghost" size="sm" onClick={() => setAll((v) => !v)} aria-expanded={all}>
          {all ? 'Show the busiest 10' : `Show all ${loaded.length} loaded peers`}
        </Button>
      )}
      {p.peers!.truncated && (
        <p className="text-xs text-text2">
          The profile lists the {loaded.length} busiest of {formatCount(p.peers!.total)} peers.{' '}
          <Link to={buildNetflowLink('/netflow/forensics', { window: w, filter: hostFlowFilter(filter, p.ip), extra: { run: true } })} className="underline-offset-2 hover:underline">
            All flows
          </Link>
        </p>
      )}
      <p className="text-xs text-text2">↓ received by this host · ↑ sent by this host</p>
    </div>
  )
}

function TrafficChart({ p, meta }: { p: HostProfile; meta: NfMeta | null }) {
  const [asTable, setAsTable] = useState(false)
  const tz = displayTimeZone()
  const ts = p.timeseries
  const points = useMemo(
    () => (ts ? toChartPoints(ts.points, ts.bucket_s, meta?.window?.effective_from, meta?.window?.effective_to) : []),
    [ts, meta?.window?.effective_from, meta?.window?.effective_to],
  )
  const gaps = points.filter((x) => !x.covered).length
  const tick = (v: number) => {
    const span = points.length ? points[points.length - 1].t - points[0].t : 0
    return formatDateTime(v, tz, { seconds: false, zone: 'none', timeOnly: span <= 36 * 3_600_000 })
  }
  return (
    <Section
      title="Traffic rate"
      icon={Activity}
      action={
        ts && (
          <Button variant="ghost" size="sm" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}>
            <Table2 aria-hidden />
            {asTable ? 'Chart' : 'Table'}
          </Button>
        )
      }
    >
      {!ts ? (
        <SectionFailure state={p.sections.timeseries} label="the traffic rate" />
      ) : asTable ? (
        <div className="max-h-80 overflow-auto">
          <table className="w-full text-xs">
            <caption className="sr-only">Traffic rate per {formatInterval(ts.bucket_s)} bucket</caption>
            <thead className="sticky top-0 bg-surface">
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text2">
                <th scope="col" className="px-2 py-1.5 font-medium">Bucket start ({tz})</th>
                <th scope="col" className="px-2 py-1.5 text-right font-medium">Received</th>
                <th scope="col" className="px-2 py-1.5 text-right font-medium">Sent</th>
              </tr>
            </thead>
            <tbody>
              {points.map((x) => (
                <tr key={x.t} className="border-b border-border last:border-0">
                  <td className="px-2 py-1 tabular-nums">{formatDateTime(x.t, tz, { seconds: false, zone: 'none' })}</td>
                  <td className="px-2 py-1 text-right tabular-nums">{x.covered ? formatBps(x.in_bps) : 'No data'}</td>
                  <td className="px-2 py-1 text-right tabular-nums">{x.covered ? formatBps(x.out_bps) : 'No data'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className={cn('h-[240px]', IN_COLOR, OUT_COLOR)}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.5} vertical={false} />
              <XAxis
                dataKey="t"
                type="number"
                scale="time"
                domain={['dataMin', 'dataMax']}
                tickFormatter={tick}
                tick={{ fontSize: chartFont(11), fill: 'rgb(var(--text2))' }}
                axisLine={false}
                tickLine={false}
                minTickGap={40}
              />
              <YAxis
                tickFormatter={formatBpsAxis}
                tick={{ fontSize: chartFont(11), fill: 'rgb(var(--text2))' }}
                axisLine={false}
                tickLine={false}
                width={48}
                label={{ value: 'bit/s', angle: -90, position: 'insideLeft', fontSize: chartFont(11), fill: 'rgb(var(--text2))', dy: 16 }}
              />
              <Tooltip
                labelFormatter={(v: number) => `${formatDateTime(v, tz, { seconds: false })} · ${formatInterval(ts.bucket_s)} bucket`}
                formatter={(value, name) => [typeof value === 'number' ? `${formatBps(value)} (${formatBpsExact(value)})` : 'No data', String(name)]}
                contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: chartFont(12), color: 'rgb(var(--text))' }}
                cursor={{ stroke: 'rgb(var(--border-strong))' }}
              />
              <Legend wrapperStyle={{ fontSize: chartFont(12) }} formatter={(v: string) => <span style={{ color: 'rgb(var(--text))' }}>{v}</span>} />
              <Line type="linear" dataKey="in_bps" name="Received" stroke="var(--nf-in)" strokeWidth={2} dot={false} activeDot={{ r: 4 }} connectNulls={false} isAnimationActive={false} />
              <Line type="linear" dataKey="out_bps" name="Sent" stroke="var(--nf-out)" strokeWidth={2} dot={false} activeDot={{ r: 4 }} connectNulls={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      {ts && (
        <p className="mt-2 text-xs text-text2">
          Average rate per {formatInterval(ts.bucket_s)} bucket.
          {gaps > 0 && ` ${gaps} bucket${gaps === 1 ? '' : 's'} without any records from the exporters in scope are gaps (unknown), not zero.`}
        </p>
      )}
    </Section>
  )
}

