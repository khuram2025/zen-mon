/**
 * NCCM archive tools: fleet configuration search, cross-device version
 * comparison (F09) and the collector support matrix.
 *
 * Search and compare run on redacted text unless the caller may export raw
 * configurations; secrets are never matched or shown.
 */
import { Fragment, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, CircleSlash, Database, ExternalLink, FlaskConical,
  GitCompare, Info, Search, ShieldCheck, Wrench, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import { Switch } from '@/components/ui/Switch'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { absoluteUtc, ncmError } from './model'
import { Ago, EmptyState, SectionError } from './ui'
import { DiffView } from './DiffView'

const TYPE_LABEL: Record<string, string> = { running: 'Running configuration', startup: 'Startup configuration' }

function status(e: unknown): number | undefined {
  return (e as any)?.response?.status
}

function ConfigTypeSelect({ id, value, onChange, label = 'Configuration type', className }: {
  id: string; value: string; onChange: (v: string) => void; label?: string; className?: string
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={id}>{label}</Label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id={id} aria-label={label} className="h-9"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="running">{TYPE_LABEL.running}</SelectItem>
          <SelectItem value="startup">{TYPE_LABEL.startup}</SelectItem>
        </SelectContent>
      </Select>
    </div>
  )
}

function ValidatedBadge({ validated }: { validated?: boolean | null }) {
  return validated
    ? <Badge variant="success"><CheckCircle2 className="h-3 w-3" aria-hidden /> Validated</Badge>
    : <Badge variant="warning"><AlertTriangle className="h-3 w-3" aria-hidden /> Unvalidated</Badge>
}

// --------------------------------------------------------------------------- //
// Archive search
// --------------------------------------------------------------------------- //
type SearchParams = { q: string; regex: boolean; config_type: string }

/** Highlight every case-insensitive occurrence of a plain-text needle. */
function highlight(text: string, needle: string): ReactNode {
  const n = needle.toLowerCase()
  if (!n) return text
  const hay = text.toLowerCase()
  const out: ReactNode[] = []
  let last = 0
  let at = hay.indexOf(n)
  let k = 0
  while (at !== -1 && k < 50) {
    if (at > last) out.push(text.slice(last, at))
    out.push(<mark key={k++} className="rounded-sm bg-warning/30 text-text">{text.slice(at, at + n.length)}</mark>)
    last = at + n.length
    at = hay.indexOf(n, last)
  }
  if (last < text.length) out.push(text.slice(last))
  return out.length ? out : text
}

export function ArchiveSearch() {
  const can = useCan()
  const canRegex = can('ncm.manage')
  const [input, setInput] = useState('')
  const [configType, setConfigType] = useState('running')
  const [regex, setRegex] = useState(false)
  const [params, setParams] = useState<SearchParams | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)

  const q = useQuery<any>({
    queryKey: ['ncm', 'search', params],
    queryFn: async () => (await api.get('/ncm/search', { params: { ...params, limit: 50 } })).data,
    enabled: !!params,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: (n, e) => { const s = status(e); return !(s && s >= 400 && s < 500) && n < 1 },
  })

  const submit = (e?: FormEvent) => {
    e?.preventDefault()
    const text = input.trim()
    if (text.length < 2) { setLocalError('Enter at least 2 characters.'); return }
    setLocalError(null)
    const next = { q: text, regex: canRegex && regex, config_type: configType }
    if (params && params.q === next.q && params.regex === next.regex && params.config_type === next.config_type) q.refetch()
    else setParams(next)
  }

  const code = status(q.error)
  const inlineError = localError
    || (q.isError && code === 422 ? `Invalid search pattern: ${ncmError(q.error)}` : null)
    || (q.isError && code === 403 ? ncmError(q.error) || 'Regular-expression search requires NCM manage permission.' : null)
  const results: any[] = q.data?.data || []
  const plain = params && !params.regex ? params.q : ''

  return (
    <div className="min-w-0 space-y-3">
      <Card className="min-w-0">
        <CardContent className="pt-4">
          <form onSubmit={submit} role="search" aria-label="Search stored configurations" className="flex flex-wrap items-end gap-3">
            <div className="min-w-0 flex-1 basis-60 space-y-1.5">
              <Label htmlFor="ncm-archive-q">Search text</Label>
              <div className="relative">
                <Search aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                <Input id="ncm-archive-q" value={input} onChange={(e) => setInput(e.target.value)} maxLength={200}
                  placeholder={regex && canRegex ? 'Regular expression, e.g. ^snmp-server community' : 'e.g. ntp server 10.0.0.1'}
                  aria-invalid={!!inlineError} aria-describedby={inlineError ? 'ncm-archive-error' : undefined}
                  className={cn('pl-8', (regex && canRegex) && 'font-mono')} />
              </div>
            </div>
            <ConfigTypeSelect id="ncm-archive-type" value={configType} onChange={setConfigType} className="w-full sm:w-56" />
            {canRegex && (
              <div className="flex h-9 items-center gap-2">
                <Switch id="ncm-archive-regex" checked={regex} onCheckedChange={setRegex} />
                <Label htmlFor="ncm-archive-regex" className="normal-case tracking-normal">Regular expression</Label>
              </div>
            )}
            <Button type="submit" disabled={q.isFetching}><Search /> {q.isFetching ? 'Searching…' : 'Search'}</Button>
          </form>
          {inlineError && <p id="ncm-archive-error" role="alert" className="mt-2 text-xs text-danger">{inlineError}</p>}
          <p className="mt-2 text-xs text-muted">Searches the latest stored version of each device you can see. Matching is case-insensitive.</p>
        </CardContent>
      </Card>

      {!params ? (
        <EmptyState>Enter text to search the latest stored configuration of every device.</EmptyState>
      ) : q.isPending || (q.isFetching && !q.data) ? (
        <p role="status" className="py-6 text-center text-sm text-muted">Searching stored configurations…</p>
      ) : q.isError && (code === 422 || code === 403) ? null : q.isError ? (
        <SectionError what="Search results" onRetry={() => q.refetch()} />
      ) : (
        <div className="min-w-0 space-y-3">
          {q.data?.redacted && (
            <div role="note" className="flex items-start gap-2 rounded-md border border-info/30 bg-info/10 px-3 py-2 text-xs text-info">
              <ShieldCheck aria-hidden className="mt-0.5 h-4 w-4 shrink-0" />
              Search runs on redacted text; secrets are never matched or shown.
            </div>
          )}
          <p aria-live="polite" className="text-sm">
            <span className="font-medium">{q.data?.devices_matched ?? 0}</span> of {q.data?.devices_searched ?? 0} devices searched matched
            {' '}· <span className="font-medium">{q.data?.total_matches ?? 0}</span> matching line{q.data?.total_matches === 1 ? '' : 's'}
            {' '}<span className="text-xs text-muted">({TYPE_LABEL[params.config_type]}{params.regex ? ', regular expression' : ''})</span>
          </p>
          {(q.data?.devices_matched || 0) > results.length && (
            <p className="text-xs text-muted">Showing the first {results.length} matching devices. Refine the search to narrow the results.</p>
          )}
          {results.length === 0 ? (
            <EmptyState>
              {q.data?.devices_searched ? 'No stored configuration matches this search.' : `No ${TYPE_LABEL[params.config_type].toLowerCase()} has been stored for any visible device.`}
            </EmptyState>
          ) : results.map((r) => (
            <Card key={r.device_id} className="min-w-0">
              <CardContent className="space-y-2 pt-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Link to={`/ncm/${r.device_id}?tab=versions`} className="font-medium text-primary hover:underline">{r.hostname || r.ip || r.device_id}</Link>
                  {r.ip && <span className="font-mono text-xs text-muted">{r.ip}</span>}
                  <Badge variant="info">{r.match_count} match{r.match_count === 1 ? '' : 'es'}</Badge>
                  <ValidatedBadge validated={r.validated} />
                  <span className="text-xs text-muted">captured <Ago iso={r.captured_at} /></span>
                  <Link to={`/ncm/${r.device_id}?tab=versions`} className="ml-auto inline-flex items-center gap-1 text-xs text-primary hover:underline">
                    Versions <ExternalLink aria-hidden className="h-3 w-3" />
                  </Link>
                </div>
                <div className="overflow-x-auto rounded-md border border-border bg-surface2/40">
                  <ol aria-label={`Matching lines on ${r.hostname || r.ip}`} className="min-w-max py-1 font-mono text-xs">
                    {(r.matches || []).slice(0, 20).map((m: any) => (
                      <li key={m.line} className="flex gap-3 px-2 py-0.5">
                        <span className="w-12 shrink-0 select-none text-right tabular-nums text-muted" aria-label={`line ${m.line}`}>{m.line}</span>
                        <span className="whitespace-pre">{plain ? highlight(m.text, plain) : m.text}</span>
                      </li>
                    ))}
                  </ol>
                </div>
                {r.match_count > Math.min(20, (r.matches || []).length) && (
                  <p className="text-xs text-muted">Showing the first {Math.min(20, (r.matches || []).length)} of {r.match_count} matching lines.</p>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Cross-device compare (F09)
// --------------------------------------------------------------------------- //
type Side = { device: string; type: string; version: string }

function useDevicePicker() {
  return useQuery<any[]>({
    queryKey: ['ncm', 'overview', 'picker'],
    queryFn: async () => {
      const rows: any[] = (await api.get('/ncm/overview', { params: { page_size: 1000 } })).data?.data || []
      return rows.filter((d) => (d.versions || 0) > 0)
        .sort((a, b) => String(a.hostname || a.ip || '').localeCompare(String(b.hostname || b.ip || '')))
    },
    staleTime: 60_000,
  })
}

function SidePicker({ label, idPrefix, side, onChange, devices }: {
  label: string; idPrefix: string; side: Side; onChange: (s: Side) => void; devices: any[]
}) {
  const versions = useQuery<any>({
    queryKey: ['ncm', 'configs', side.device, side.type, 'picker'],
    queryFn: async () => (await api.get(`/devices/${side.device}/configs`, { params: { config_type: side.type, limit: 100 } })).data,
    enabled: !!side.device,
  })
  const list: any[] = versions.data?.data || []

  // Default to the newest version whenever the device/type changes.
  useEffect(() => {
    if (!versions.data) return
    if (!list.some((v) => v.id === side.version)) onChange({ ...side, version: list[0]?.id || '' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [versions.data])

  const device = devices.find((d) => d.device_id === side.device)
  const deviceKnown = !side.device || !!device

  return (
    <fieldset className="min-w-0 space-y-3 rounded-md border border-border p-3">
      <legend className="px-1 text-sm font-semibold">{label}</legend>
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-device`}>Device</Label>
        <Select value={side.device} onValueChange={(v) => onChange({ ...side, device: v, version: '' })}>
          <SelectTrigger id={`${idPrefix}-device`} aria-label={`${label} device`}><SelectValue placeholder="Choose a device" /></SelectTrigger>
          <SelectContent>
            {devices.map((d) => (
              <SelectItem key={d.device_id} value={d.device_id}>
                {d.hostname || d.ip}{d.ip && d.hostname ? ` (${d.ip})` : ''}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {device && <p className="text-xs text-muted">{[device.vendor, device.platform].filter(Boolean).join(' · ') || 'Platform not set'} · {device.versions} stored version{device.versions === 1 ? '' : 's'}</p>}
        {!deviceKnown && <p className="text-xs text-warning">This device has no stored versions you can compare.</p>}
      </div>
      <ConfigTypeSelect id={`${idPrefix}-type`} value={side.type} onChange={(v) => onChange({ ...side, type: v, version: '' })} />
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-version`}>Version</Label>
        {!side.device ? (
          <p className="text-xs text-muted">Choose a device first.</p>
        ) : versions.isPending ? (
          <p role="status" className="text-xs text-muted">Loading versions…</p>
        ) : versions.isError ? (
          <SectionError what="Versions" onRetry={() => versions.refetch()} />
        ) : list.length === 0 ? (
          <p className="text-xs text-muted">No {TYPE_LABEL[side.type].toLowerCase()} stored for this device.</p>
        ) : (
          <Select value={side.version} onValueChange={(v) => onChange({ ...side, version: v })}>
            <SelectTrigger id={`${idPrefix}-version`} aria-label={`${label} version`}><SelectValue placeholder="Choose a version" /></SelectTrigger>
            <SelectContent>
              {list.map((v, i) => (
                <SelectItem key={v.id} value={v.id}>
                  {absoluteUtc(v.captured_at)} · {v.hash}{v.validated ? ' · validated' : ' · unvalidated'}{i === 0 ? ' (latest)' : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>
    </fieldset>
  )
}

export function CrossDeviceCompare({ initialDeviceId }: { initialDeviceId?: string }) {
  const can = useCan()
  const canRaw = can('ncm.export')
  const devices = useDevicePicker()
  const [a, setA] = useState<Side>({ device: initialDeviceId || '', type: 'running', version: '' })
  const [b, setB] = useState<Side>({ device: '', type: 'running', version: '' })
  const [normalized, setNormalized] = useState(true)
  const [raw, setRaw] = useState(false)

  const compare = useMutation({
    mutationFn: async () => (await api.get('/ncm/compare', {
      params: { device_a: a.device, version_a: a.version, device_b: b.device, version_b: b.version, normalized, raw: canRaw && raw },
    })).data,
  })
  const ready = !!(a.device && a.version && b.device && b.version)
  const same = ready && a.version === b.version
  const r: any = compare.data

  // Picking different inputs invalidates the shown result.
  useEffect(() => { compare.reset() }, [a.version, b.version, normalized, raw]) // eslint-disable-line react-hooks/exhaustive-deps

  const list = devices.data || []
  return (
    <div className="min-w-0 space-y-3">
      <Card className="min-w-0">
        <CardContent className="space-y-3 pt-4">
          {devices.isPending ? (
            <p role="status" className="py-4 text-center text-sm text-muted">Loading devices…</p>
          ) : devices.isError ? (
            <SectionError what="The device list" onRetry={() => devices.refetch()} />
          ) : list.length === 0 ? (
            <EmptyState>No device has a stored configuration yet, so there is nothing to compare.</EmptyState>
          ) : (
            <>
              <div className="grid min-w-0 gap-3 md:grid-cols-2">
                <SidePicker label="Side A (before)" idPrefix="ncm-cmp-a" side={a} onChange={setA} devices={list} />
                <SidePicker label="Side B (after)" idPrefix="ncm-cmp-b" side={b} onChange={setB} devices={list} />
              </div>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
                <div className="flex items-center gap-2">
                  <Switch id="ncm-cmp-normalized" checked={normalized} onCheckedChange={setNormalized} aria-describedby="ncm-cmp-normalized-help" />
                  <div>
                    <Label htmlFor="ncm-cmp-normalized" className="normal-case tracking-normal">Normalized</Label>
                    <p id="ncm-cmp-normalized-help" className="text-xs text-muted">Ignores runtime noise such as timestamps.</p>
                  </div>
                </div>
                {canRaw && (
                  <div className="flex items-center gap-2">
                    <Switch id="ncm-cmp-raw" checked={raw} onCheckedChange={setRaw} aria-describedby="ncm-cmp-raw-help" />
                    <div>
                      <Label htmlFor="ncm-cmp-raw" className="normal-case tracking-normal">Show raw values</Label>
                      <p id="ncm-cmp-raw-help" className="text-xs text-muted">Unredacted secrets; the comparison is audited.</p>
                    </div>
                  </div>
                )}
                <Button className="ml-auto" onClick={() => compare.mutate()} disabled={!ready || same || compare.isPending}>
                  <GitCompare /> {compare.isPending ? 'Comparing…' : 'Compare'}
                </Button>
              </div>
              {same && <p className="text-xs text-warning">Both sides are the same version. Choose a different version on one side.</p>}
              {!ready && <p className="text-xs text-muted">Choose a device and a version on both sides to compare.</p>}
            </>
          )}
        </CardContent>
      </Card>

      {compare.isPending ? (
        <p role="status" className="py-6 text-center text-sm text-muted">Comparing configurations…</p>
      ) : compare.isError ? (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
          <span>The comparison failed: {ncmError(compare.error)}</span>
          <Button size="sm" variant="outline" onClick={() => compare.mutate()}>Retry</Button>
        </div>
      ) : r ? (
        <Card className="min-w-0">
          <CardContent className="space-y-3 pt-4">
            {(r.warnings || []).map((w: string) => (
              <div key={w} role="note" className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                <AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 shrink-0" /> {w}
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono tabular-nums"><span className="text-success">+{r.added ?? 0}</span> added · <span className="text-danger">−{r.removed ?? 0}</span> removed</span>
              {r.identical && <Badge variant="outline"><CheckCircle2 className="h-3 w-3" aria-hidden /> Identical</Badge>}
              {r.sensitive_changed && (
                <Badge variant="warning"><ShieldCheck className="h-3 w-3" aria-hidden /> Sensitive lines changed (redacted)</Badge>
              )}
            </div>
            {r.identical ? (
              <EmptyState>The two versions are identical{r.normalized ? ' after normalization' : ''}.</EmptyState>
            ) : (
              <DiffView diff={r.diff || ''} added={r.added} removed={r.removed} identical={r.identical}
                sensitiveChanged={false} redacted={r.redacted} normalized={r.normalized} />
            )}
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Support matrix
// --------------------------------------------------------------------------- //
const EVIDENCE: Record<string, { label: string; variant: 'outline' | 'info' | 'success' | 'danger'; icon: any }> = {
  not_captured: { label: 'Not captured here', variant: 'outline', icon: FlaskConical },
  captured: { label: 'Captured here', variant: 'info', icon: Database },
  restore_tested: { label: 'Restore-tested', variant: 'success', icon: ShieldCheck },
  none: { label: 'Not supported', variant: 'danger', icon: XCircle },
}
const EVIDENCE_ORDER = ['restore_tested', 'captured', 'not_captured', 'none']

function EvidenceBadge({ evidence }: { evidence: string }) {
  const e = EVIDENCE[evidence] || { label: evidence || 'Unknown', variant: 'outline' as const, icon: CircleSlash }
  const Icon = e.icon
  return <Badge variant={e.variant}><Icon className="h-3 w-3" aria-hidden />{e.label}</Badge>
}

export function SupportMatrix() {
  const q = useQuery<any>({
    queryKey: ['ncm', 'support-matrix'],
    queryFn: async () => (await api.get('/ncm/support-matrix')).data,
    staleTime: 60_000,
  })
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (k: string) => setOpen((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n })

  const rows = useMemo(() => {
    const data: any[] = q.data?.data || []
    return [...data].sort((x, y) =>
      (x.status === 'unsupported' ? 1 : 0) - (y.status === 'unsupported' ? 1 : 0)
      || String(x.vendor || '').localeCompare(String(y.vendor || ''))
      || String(x.label || '').localeCompare(String(y.label || '')))
  }, [q.data])
  const levels: Record<string, string> = q.data?.evidence_levels || {}

  return (
    <div className="min-w-0 space-y-3">
      <div role="note" className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">
        <Info aria-hidden className="mt-0.5 h-4 w-4 shrink-0" />
        Implemented does not mean certified: recovery is demonstrated only by a recorded restore test.
      </div>
      {q.isPending ? (
        <p role="status" className="py-6 text-center text-sm text-muted">Loading collector support…</p>
      ) : q.isError ? (
        <SectionError what="The support matrix" onRetry={() => q.refetch()} />
      ) : rows.length === 0 ? (
        <EmptyState>No collectors are registered on this appliance.</EmptyState>
      ) : (
        <Card className="min-w-0">
          <CardContent className="space-y-3 pt-4">
            {Object.keys(levels).length > 0 && (
              <dl aria-label="Evidence levels" className="grid gap-2 text-xs sm:grid-cols-2">
                {[...EVIDENCE_ORDER.filter((k) => levels[k]), ...Object.keys(levels).filter((k) => !EVIDENCE_ORDER.includes(k))].map((k) => (
                  <div key={k} className="flex min-w-0 items-start gap-2">
                    <dt className="shrink-0"><EvidenceBadge evidence={k} /></dt>
                    <dd className="min-w-0 text-muted">{levels[k]}</dd>
                  </div>
                ))}
              </dl>
            )}
            <div className="overflow-x-auto">
              <Table>
                <THead>
                  <Tr>
                    <Th className="w-10"><span className="sr-only">Details</span></Th>
                    <Th>Vendor</Th>
                    <Th>Driver</Th>
                    <Th>Status</Th>
                    <Th>Capture types</Th>
                    <Th>Evidence</Th>
                    <Th className="text-right">Validated captures</Th>
                    <Th className="text-right">Failed attempts</Th>
                    <Th>Last capture</Th>
                    <Th>Restore tests</Th>
                  </Tr>
                </THead>
                <TBody>
                  {rows.map((r) => {
                    const key = r.value || `unsupported:${r.vendor}`
                    const expanded = open.has(key)
                    const hasDetail = !!(r.scope || r.omissions)
                    return (
                      <Fragment key={key}>
                        <Tr>
                          <Td className="p-1.5">
                            {hasDetail && (
                              <Button variant="ghost" size="icon" className="h-8 w-8" aria-expanded={expanded}
                                aria-label={`${expanded ? 'Hide' : 'Show'} scope and omissions for ${r.label}`} onClick={() => toggle(key)}>
                                {expanded ? <ChevronDown /> : <ChevronRight />}
                              </Button>
                            )}
                          </Td>
                          <Td className="whitespace-nowrap">{r.vendor || '—'}</Td>
                          <Td>
                            <div className="text-sm">{r.label}</div>
                            {r.value && <div className="font-mono text-xs text-muted">{r.value}</div>}
                          </Td>
                          <Td>
                            {r.status === 'implemented'
                              ? <Badge variant="default"><Wrench className="h-3 w-3" aria-hidden /> Implemented</Badge>
                              : <Badge variant="danger"><XCircle className="h-3 w-3" aria-hidden /> Unsupported</Badge>}
                          </Td>
                          <Td className="text-xs">{(r.config_types || []).length ? r.config_types.map((t: string) => TYPE_LABEL[t] || t).join(', ') : '—'}</Td>
                          <Td><EvidenceBadge evidence={r.evidence} /></Td>
                          <Td className="text-right tabular-nums">{r.status === 'implemented' ? r.validated_captures ?? 0 : '—'}</Td>
                          <Td className={cn('text-right tabular-nums', r.failed_attempts ? 'text-danger' : '')}>{r.status === 'implemented' ? r.failed_attempts ?? 0 : '—'}</Td>
                          <Td className="whitespace-nowrap text-xs">{r.status === 'implemented' ? <Ago iso={r.last_capture} empty="never" /> : '—'}</Td>
                          <Td className="whitespace-nowrap text-xs">
                            {r.status !== 'implemented' ? '—'
                              : r.restore_tests_passed ? <span>{r.restore_tests_passed} passed · <Ago iso={r.last_restore_test} /></span>
                              : <span className="text-muted">none recorded</span>}
                          </Td>
                        </Tr>
                        {expanded && (
                          <Tr className="hover:bg-transparent">
                            <Td colSpan={10} className="bg-surface2/40">
                              <dl className="grid gap-3 text-xs sm:grid-cols-2">
                                <div className="min-w-0">
                                  <dt className="font-medium text-muted">{r.status === 'unsupported' ? 'Why unsupported' : 'Scope'}</dt>
                                  <dd className="mt-0.5 whitespace-pre-line break-words">{r.scope || '—'}</dd>
                                </div>
                                <div className="min-w-0">
                                  <dt className="font-medium text-muted">Omissions</dt>
                                  <dd className="mt-0.5 whitespace-pre-line break-words">{r.omissions || 'None documented.'}</dd>
                                </div>
                              </dl>
                            </Td>
                          </Tr>
                        )}
                      </Fragment>
                    )
                  })}
                </TBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
