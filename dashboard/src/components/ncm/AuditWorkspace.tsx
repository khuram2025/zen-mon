/**
 * NCCM audit trail: who did what, to which device, and when. Fleet-wide when
 * no device is given, otherwise scoped to that device (with an opt-out).
 * A 403 is a permission statement, not an error.
 */
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Download, Lock, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Label } from '@/components/ui/Label'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { absoluteUtc, download, ncmError, toCsv } from './model'
import { Ago, EmptyState, Pagination, SectionError } from './ui'

const PAGE = 50
const CHIP_MAX = 80
const TITLE_MAX = 200

function useDebounced<T>(value: T, ms = 350): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

function clip(s: string, n: number) {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}

function valueText(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

/** Compact key=value chips; long values are truncated with the (bounded) full text on hover. */
function MetadataChips({ metadata, resourceId }: { metadata: unknown; resourceId?: string | null }) {
  let pairs: [string, string][] = []
  if (metadata && typeof metadata === 'object' && !Array.isArray(metadata)) {
    pairs = Object.entries(metadata as Record<string, unknown>).map(([k, v]) => [k, valueText(v)])
  } else if (metadata != null && metadata !== '') {
    pairs = [['value', valueText(metadata)]]
  }
  if (resourceId) pairs = [['resource', String(resourceId)], ...pairs]
  if (!pairs.length) return <span className="text-xs text-muted">—</span>
  return (
    <div className="flex max-w-md flex-wrap gap-1">
      {pairs.map(([k, v]) => {
        const full = `${k}=${v}`
        return (
          <span key={k} title={full.length > CHIP_MAX ? clip(full, TITLE_MAX) : undefined}
            className="inline-block max-w-full truncate rounded border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-xs">
            <span className="text-muted">{k}=</span>{clip(v, CHIP_MAX - k.length)}
          </span>
        )
      })}
    </div>
  )
}

/** Action choices: every distinct action plus "family.*" wildcards. */
function actionOptions(actions: string[]): { value: string; label: string }[] {
  const families = [...new Set(actions.filter((a) => a.includes('.')).map((a) => a.split('.')[0]))].sort()
  return [
    ...families.map((f) => ({ value: `${f}.*`, label: `${f}.* (all ${f} actions)` })),
    ...actions.map((a) => ({ value: a, label: a })),
  ]
}

export function AuditWorkspace({ deviceId }: { deviceId?: string }) {
  const [action, setAction] = useState('all')
  const [actorInput, setActorInput] = useState('')
  const actor = useDebounced(actorInput.trim())
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [onlyDevice, setOnlyDevice] = useState(true)
  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1) }, [action, actor, since, until, onlyDevice, deviceId])

  const rangeInvalid = !!since && !!until && since > until
  const params = {
    device_id: deviceId && onlyDevice ? deviceId : undefined,
    action: action === 'all' ? undefined : action,
    actor: actor || undefined,
    since: since ? `${since}T00:00:00Z` : undefined,
    until: until ? `${until}T23:59:59Z` : undefined,
    limit: PAGE,
    offset: (page - 1) * PAGE,
  }
  const q = useQuery<any>({
    queryKey: ['ncm', 'audit', params],
    queryFn: async () => (await api.get('/ncm/audit', { params })).data,
    enabled: !rangeInvalid,
    placeholderData: keepPreviousData,
    retry: (n, e: any) => e?.response?.status !== 403 && n < 2,
  })
  const forbidden = (q.error as any)?.response?.status === 403
  const rows: any[] = q.data?.data || []
  const total: number = q.data?.total || 0
  const options = useMemo(() => actionOptions(q.data?.actions || []), [q.data?.actions])
  const filtered = action !== 'all' || !!actor || !!since || !!until

  const exportCsv = () => {
    const header = ['time_utc', 'actor', 'action', 'device', 'device_id', 'resource_id', 'metadata']
    const body = rows.map((r) => [
      absoluteUtc(r.created_at), r.actor_name, r.action, r.hostname, r.device_id, r.resource_id,
      r.metadata == null ? '' : valueText(r.metadata),
    ])
    download(`nccm-audit${deviceId && onlyDevice ? `-${deviceId.slice(0, 8)}` : ''}-page${page}.csv`, toCsv([header, ...body]), 'text/csv')
  }

  if (forbidden) {
    return (
      <div role="note" className="flex items-center gap-2 rounded-md border border-border px-4 py-6 text-sm text-muted">
        <Lock aria-hidden className="h-4 w-4 shrink-0" />
        You don&apos;t have permission to view the audit trail.
      </div>
    )
  }

  return (
    <Card className="min-w-0">
      <CardContent className="space-y-3 pt-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-full space-y-1.5 sm:w-60">
            <Label htmlFor="ncm-audit-action">Action</Label>
            <Select value={action} onValueChange={setAction}>
              <SelectTrigger id="ncm-audit-action" aria-label="Action" className="h-8 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All actions</SelectItem>
                {options.map((o) => <SelectItem key={o.value} value={o.value} className="font-mono text-xs">{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <FormField label="Actor" className="w-full sm:w-44">
            <Input value={actorInput} onChange={(e) => setActorInput(e.target.value)} placeholder="Name contains" className="h-8 text-xs" />
          </FormField>
          <FormField label="Since (UTC)" className="w-full sm:w-40">
            <Input type="date" value={since} max={until || undefined} onChange={(e) => setSince(e.target.value)} className="h-8 text-xs" />
          </FormField>
          <FormField label="Until (UTC)" className="w-full sm:w-40">
            <Input type="date" value={until} min={since || undefined} onChange={(e) => setUntil(e.target.value)} className="h-8 text-xs" />
          </FormField>
          {deviceId && (
            <button type="button" aria-pressed={onlyDevice} onClick={() => setOnlyDevice((v) => !v)}
              className={cn('h-8 rounded-full border px-3 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                onlyDevice ? 'border-primary bg-primary/10 text-text' : 'border-border text-muted hover:bg-surface2')}>
              Only this device
            </button>
          )}
          <div className="ml-auto flex flex-wrap gap-2">
            {filtered && (
              <Button size="sm" variant="ghost" onClick={() => { setAction('all'); setActorInput(''); setSince(''); setUntil('') }}>Clear filters</Button>
            )}
            <Button size="sm" variant="outline" onClick={() => q.refetch()} disabled={q.isFetching || rangeInvalid}>
              <RefreshCw className={cn(q.isFetching && 'animate-spin')} /> Refresh
            </Button>
            <Button size="sm" variant="outline" onClick={exportCsv} disabled={!rows.length}>
              <Download /> Export page (CSV)
            </Button>
          </div>
        </div>

        {rangeInvalid ? (
          <p role="alert" className="text-xs text-danger">The start date is after the end date.</p>
        ) : q.isError && q.data ? (
          <p role="alert" className="text-xs text-warning">Could not refresh the audit trail: {ncmError(q.error)}. Showing the last loaded page.</p>
        ) : null}

        {rangeInvalid ? null : q.isPending ? (
          <p role="status" className="py-6 text-center text-sm text-muted">Loading audit trail…</p>
        ) : q.isError && !q.data ? (
          <SectionError what="The audit trail" onRetry={() => q.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState>{filtered ? 'No audit records match these filters.' : deviceId && onlyDevice ? 'No audit records for this device yet.' : 'No audit records yet.'}</EmptyState>
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead>
                  <Tr>
                    <Th>Time</Th>
                    <Th>Actor</Th>
                    <Th>Action</Th>
                    <Th>Device</Th>
                    <Th>Details</Th>
                  </Tr>
                </THead>
                <TBody>
                  {rows.map((r) => (
                    <Tr key={r.id}>
                      <Td className="whitespace-nowrap">
                        <div className="text-xs">{absoluteUtc(r.created_at)}</div>
                        <div className="text-xs text-muted"><Ago iso={r.created_at} /></div>
                      </Td>
                      <Td className="max-w-[10rem] truncate text-sm" title={r.actor_name || undefined}>{r.actor_name || <span className="text-muted">system</span>}</Td>
                      <Td className="whitespace-nowrap font-mono text-xs">{r.action}</Td>
                      <Td className="max-w-[10rem]">
                        {r.device_id
                          ? <Link to={`/ncm/${r.device_id}`} className="block truncate text-sm text-primary hover:underline">{r.hostname || String(r.device_id).slice(0, 8)}</Link>
                          : <span className="text-xs text-muted">Fleet</span>}
                      </Td>
                      <Td><MetadataChips metadata={r.metadata} resourceId={r.resource_id} /></Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            </div>
            <Pagination page={page} pageCount={Math.max(1, Math.ceil(total / PAGE))} onPage={setPage} total={total} pageSize={PAGE} label="records" />
          </>
        )}
      </CardContent>
    </Card>
  )
}
