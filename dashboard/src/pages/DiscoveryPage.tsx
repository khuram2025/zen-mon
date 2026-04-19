import { FormEvent, Fragment, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Download,
  Eye,
  Key,
  Loader2,
  Play,
  Radar,
  Trash2,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { FormField } from '@/components/ui/FormField'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'

type Credential = {
  id: string
  name: string
  snmp_version: string
  community: string | null
  v3_username: string | null
  port: number
  timeout_ms: number
  retries: number
}

type DiscoveryResult = {
  id: number
  job_id: string
  ip_address: string
  is_reachable: boolean
  snmp_responded: boolean
  sys_object_id: string | null
  sys_descr: string | null
  sys_name: string | null
  hostname_guess: string | null
  matched_profile_id: string | null
  matched_vendor: string | null
  matched_model: string | null
  matched_os_version: string | null
  already_known: boolean
  imported: boolean
  imported_device_id: string | null
  error_message: string | null
  scanned_at: string
}

type DiscoveryJob = {
  id: string
  cidr: string
  community: string
  snmp_version: string
  snmp_port: number
  timeout_ms: number
  status: 'pending' | 'running' | 'completed' | 'failed'
  total_hosts: number
  scanned_hosts: number
  responding_hosts: number
  error_message: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
}

type ResultFilter = 'all' | 'snmp' | 'importable'

export function DiscoveryPage() {
  const qc = useQueryClient()
  const [cidr, setCidr] = useState('10.0.0.0/24')
  const [credentialId, setCredentialId] = useState('__manual__')
  const [community, setCommunity] = useState('public')
  const [version, setVersion] = useState('2c')
  const [port, setPort] = useState(161)
  const [timeoutMs, setTimeoutMs] = useState(2000)
  const [activeJob, setActiveJob] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [filter, setFilter] = useState<ResultFilter>('snmp')

  const { data: credentials } = useQuery<Credential[]>({
    queryKey: ['snmp-credentials'],
    queryFn: async () => (await api.get('/snmp-credentials')).data,
  })

  const { data: jobs } = useQuery<DiscoveryJob[]>({
    queryKey: ['discover', 'list'],
    queryFn: async () => (await api.get('/snmp/discover')).data,
    refetchInterval: 5_000,
  })

  const selectedCred = credentials?.find((c) => c.id === credentialId)

  const createJob = useMutation({
    mutationFn: async () => {
      const payload: any = { cidr }
      if (credentialId !== '__manual__' && selectedCred) {
        payload.credential_id = selectedCred.id
      } else {
        payload.community = community
        payload.snmp_version = version
        payload.snmp_port = port
        payload.timeout_ms = timeoutMs
      }
      return (await api.post('/snmp/discover', payload)).data as DiscoveryJob
    },
    onSuccess: (job) => {
      setActiveJob(job.id)
      setSelected(new Set())
      setExpanded(new Set())
      qc.invalidateQueries({ queryKey: ['discover', 'list'] })
      toast.success('Discovery started', `Scanning ${job.cidr}`)
    },
    onError: (e: any) => toast.error('Discovery failed', apiErrorMessage(e)),
  })

  const deleteJob = useMutation({
    mutationFn: async (id: string) => api.delete(`/snmp/discover/${id}`),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ['discover', 'list'] })
      if (id === activeJob) setActiveJob(null)
      toast.success('Job deleted')
    },
  })

  const { data: jobDetail } = useQuery<DiscoveryJob>({
    queryKey: ['discover', activeJob],
    queryFn: async () => (await api.get(`/snmp/discover/${activeJob}`)).data,
    enabled: !!activeJob,
    refetchInterval: (q) => {
      const s = q.state.data?.status
      return s === 'running' || s === 'pending' ? 2_000 : false
    },
  })

  const { data: rawResults } = useQuery<DiscoveryResult[]>({
    queryKey: ['discover', activeJob, 'results'],
    queryFn: async () => (await api.get(`/snmp/discover/${activeJob}/results`)).data,
    enabled: !!activeJob && (jobDetail?.status === 'completed' || jobDetail?.status === 'failed'),
  })

  // Dedupe by id — belt-and-braces against repeat rows from the API
  // (the old UI would occasionally render duplicates when re-expanding a job).
  const results = useMemo<DiscoveryResult[]>(() => {
    if (!rawResults) return []
    const seen = new Map<number, DiscoveryResult>()
    for (const r of rawResults) if (!seen.has(r.id)) seen.set(r.id, r)
    return Array.from(seen.values())
  }, [rawResults])

  const respondingResults = useMemo(() => results.filter((r) => r.snmp_responded), [results])
  const importableResults = useMemo(
    () => results.filter((r) => r.snmp_responded && !r.already_known && !r.imported),
    [results],
  )
  const visibleResults = useMemo(() => {
    if (filter === 'snmp') return respondingResults
    if (filter === 'importable') return importableResults
    return results
  }, [filter, results, respondingResults, importableResults])

  const importJob = useMutation({
    mutationFn: async (ids: number[]) =>
      (await api.post(`/snmp/discover/${activeJob}/import`, { result_ids: ids })).data as {
        created: number
        skipped: number
        errors: string[]
      },
    onSuccess: (d) => {
      setSelected(new Set())
      qc.invalidateQueries({ queryKey: ['discover', activeJob, 'results'] })
      qc.invalidateQueries({ queryKey: ['devices'] })
      const msg = `Imported ${d.created} device${d.created === 1 ? '' : 's'}${
        d.skipped ? `, ${d.skipped} skipped` : ''
      }`
      if (d.errors.length) toast.error(msg, `${d.errors.length} error(s)`)
      else toast.success(msg)
    },
    onError: (e: any) => toast.error('Import failed', apiErrorMessage(e)),
  })

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    createJob.mutate()
  }

  function openJob(id: string) {
    if (id === activeJob) return
    setActiveJob(id)
    setSelected(new Set())
    setExpanded(new Set())
  }

  function toggleExpand(id: number) {
    const n = new Set(expanded)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    setExpanded(n)
  }

  function toggleRow(id: number) {
    const n = new Set(selected)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    setSelected(n)
  }

  const progressPct =
    jobDetail && jobDetail.total_hosts > 0
      ? Math.round((jobDetail.scanned_hosts / jobDetail.total_hosts) * 100)
      : 0

  const allVisibleImportable = visibleResults.filter(
    (r) => r.snmp_responded && !r.already_known && !r.imported,
  )
  const allVisibleSelected =
    allVisibleImportable.length > 0 &&
    allVisibleImportable.every((r) => selected.has(r.id))

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Radar className="h-6 w-6 text-primary" /> Network Discovery
          </h1>
          <p className="mt-1 text-xs text-muted">
            Sweep a CIDR range to find SNMP-enabled devices, then import them into your monitored inventory.
          </p>
        </div>
      </div>

      {/* Discovery form */}
      <Card>
        <CardHeader>
          <CardTitle>New Discovery Sweep</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <FormField label="CIDR Range" required hint="Max /20 (≤ 1024 hosts)">
                <Input
                  required
                  placeholder="10.0.0.0/24"
                  value={cidr}
                  onChange={(e) => setCidr(e.target.value)}
                />
              </FormField>
              <FormField label="SNMP Credential" hint="Saved profile or manual entry">
                <Select value={credentialId} onValueChange={setCredentialId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select credential…" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__manual__">Manual entry</SelectItem>
                    {(credentials || []).map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        <span className="flex items-center gap-2">
                          <Key className="h-3 w-3" /> {c.name}
                          <span className="text-muted text-xs">v{c.snmp_version}</span>
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            </div>

            {credentialId !== '__manual__' && selectedCred ? (
              <div className="rounded-md border border-primary/20 bg-primary/5 px-4 py-3">
                <div className="flex items-center gap-2 text-sm">
                  <Key className="h-4 w-4 text-primary" />
                  <span className="font-medium">{selectedCred.name}</span>
                  <Badge variant="info">v{selectedCred.snmp_version}</Badge>
                  <span className="text-xs text-muted">
                    Port {selectedCred.port} · Timeout {selectedCred.timeout_ms}ms
                  </span>
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <FormField label="Community">
                  <Input
                    value={community}
                    onChange={(e) => setCommunity(e.target.value)}
                    placeholder="public"
                  />
                </FormField>
                <FormField label="Version">
                  <Select value={version} onValueChange={setVersion}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="2c">v2c</SelectItem>
                      <SelectItem value="1">v1</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
                <FormField label="Port">
                  <Input
                    type="number"
                    value={port}
                    onChange={(e) => setPort(Number(e.target.value) || 161)}
                    min={1}
                    max={65535}
                  />
                </FormField>
                <FormField label="Timeout (ms)">
                  <Input
                    type="number"
                    value={timeoutMs}
                    onChange={(e) => setTimeoutMs(Number(e.target.value) || 2000)}
                    min={200}
                    max={30000}
                    step={100}
                  />
                </FormField>
              </div>
            )}

            <div className="flex items-center justify-end gap-2">
              <Button type="submit" disabled={createJob.isPending}>
                {createJob.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Play className="h-4 w-4" />
                )}
                Start Sweep
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* Active job progress + results */}
      {activeJob && jobDetail && (
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <span className="font-mono">{jobDetail.cidr}</span>
                <Badge
                  variant={
                    jobDetail.status === 'completed'
                      ? 'success'
                      : jobDetail.status === 'failed'
                        ? 'danger'
                        : 'info'
                  }
                >
                  {jobDetail.status}
                </Badge>
                <Badge variant="outline">v{jobDetail.snmp_version}</Badge>
              </CardTitle>
              <div className="flex items-center gap-2">
                <StatPill label="scanned" value={jobDetail.scanned_hosts} total={jobDetail.total_hosts} />
                <StatPill label="SNMP" value={respondingResults.length} tone="success" />
                <StatPill label="importable" value={importableResults.length} tone="primary" />
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setActiveJob(null)}
                  title="Close"
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {(jobDetail.status === 'running' || jobDetail.status === 'pending') && (
              <div className="flex items-center gap-3">
                <div className="flex-1">
                  <div className="h-2 overflow-hidden rounded-full bg-surface2">
                    <div
                      className="h-full bg-primary transition-all"
                      style={{ width: `${progressPct}%` }}
                    />
                  </div>
                </div>
                <div className="font-mono text-xs text-muted">
                  {jobDetail.scanned_hosts}/{jobDetail.total_hosts} · {progressPct}%
                </div>
              </div>
            )}

            {jobDetail.status === 'failed' && (
              <div className="rounded-md bg-danger/10 px-4 py-3 text-sm text-danger">
                {jobDetail.error_message || 'Discovery failed'}
              </div>
            )}

            {results.length > 0 && (
              <>
                {/* Filter tabs */}
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
                    {([
                      { value: 'snmp', label: `SNMP (${respondingResults.length})` },
                      { value: 'importable', label: `Importable (${importableResults.length})` },
                      { value: 'all', label: `All (${results.length})` },
                    ] as { value: ResultFilter; label: string }[]).map((f) => (
                      <button
                        key={f.value}
                        type="button"
                        onClick={() => setFilter(f.value)}
                        className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                          filter === f.value
                            ? 'bg-surface text-text shadow-sm'
                            : 'text-muted hover:text-text'
                        }`}
                      >
                        {f.label}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Results table */}
                <div className="overflow-hidden rounded-md border border-border">
                  <Table>
                    <THead className="bg-surface2/50">
                      <Tr>
                        <Th className="w-8"></Th>
                        <Th className="w-8">
                          <input
                            type="checkbox"
                            aria-label="Select all importable on this view"
                            checked={allVisibleSelected}
                            onChange={(e) => {
                              const next = new Set(selected)
                              if (e.target.checked) {
                                allVisibleImportable.forEach((r) => next.add(r.id))
                              } else {
                                allVisibleImportable.forEach((r) => next.delete(r.id))
                              }
                              setSelected(next)
                            }}
                          />
                        </Th>
                        <Th>IP Address</Th>
                        <Th>Reachable</Th>
                        <Th>SNMP</Th>
                        <Th>sysName</Th>
                        <Th>Vendor / Model</Th>
                        <Th>Status</Th>
                      </Tr>
                    </THead>
                    <TBody>
                      {visibleResults.map((r) => {
                        const isOpen = expanded.has(r.id)
                        const canImport = r.snmp_responded && !r.already_known && !r.imported
                        return (
                          <Fragment key={r.id}>
                            <Tr className={r.already_known ? 'opacity-60' : ''}>
                              <Td>
                                <button
                                  type="button"
                                  onClick={() => toggleExpand(r.id)}
                                  className="rounded p-0.5 text-muted hover:bg-surface2 hover:text-text"
                                  aria-label={isOpen ? 'Collapse' : 'Expand'}
                                >
                                  {isOpen ? (
                                    <ChevronDown className="h-4 w-4" />
                                  ) : (
                                    <ChevronRight className="h-4 w-4" />
                                  )}
                                </button>
                              </Td>
                              <Td>
                                <input
                                  type="checkbox"
                                  disabled={!canImport}
                                  checked={selected.has(r.id)}
                                  onChange={() => toggleRow(r.id)}
                                />
                              </Td>
                              <Td className="font-mono text-xs font-medium">{r.ip_address}</Td>
                              <Td>
                                {r.is_reachable ? (
                                  <CheckCircle className="h-4 w-4 text-success" />
                                ) : (
                                  <span className="text-xs text-muted">—</span>
                                )}
                              </Td>
                              <Td>
                                {r.snmp_responded ? (
                                  <CheckCircle className="h-4 w-4 text-success" />
                                ) : r.is_reachable ? (
                                  <AlertTriangle
                                    className="h-4 w-4 text-warning"
                                    aria-label={r.error_message || 'SNMP failed'}
                                  />
                                ) : (
                                  <span className="text-xs text-muted">—</span>
                                )}
                              </Td>
                              <Td className="text-sm">
                                {r.sys_name || <span className="text-muted">—</span>}
                              </Td>
                              <Td className="text-sm">
                                {r.matched_vendor || r.matched_model ? (
                                  <div>
                                    <div className="font-medium leading-tight">
                                      {r.matched_vendor || '—'}
                                    </div>
                                    {r.matched_model && (
                                      <div className="text-[11px] text-muted">{r.matched_model}</div>
                                    )}
                                  </div>
                                ) : (
                                  <span className="text-muted">—</span>
                                )}
                              </Td>
                              <Td>
                                {r.imported ? (
                                  <Badge variant="success">imported</Badge>
                                ) : r.already_known ? (
                                  <Badge variant="outline">existing</Badge>
                                ) : canImport ? (
                                  <Badge variant="info">new</Badge>
                                ) : r.error_message ? (
                                  <Badge variant="danger">error</Badge>
                                ) : (
                                  <span className="text-xs text-muted">—</span>
                                )}
                              </Td>
                            </Tr>
                            {isOpen && (
                              <tr className="bg-surface2/40">
                                <td colSpan={8} className="px-6 py-3">
                                  <ResultDetails row={r} />
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        )
                      })}
                      {visibleResults.length === 0 && (
                        <Tr>
                          <Td colSpan={8} className="py-6 text-center text-sm text-muted">
                            No results match this filter
                          </Td>
                        </Tr>
                      )}
                    </TBody>
                  </Table>
                </div>

                {/* Import bar */}
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-surface2/40 px-4 py-3">
                  <p className="text-xs text-muted">
                    <span className="font-medium text-text">{selected.size}</span> selected ·{' '}
                    <span className="font-medium text-text">{importableResults.length}</span> importable ·{' '}
                    <span className="font-medium text-text">{respondingResults.length}</span> SNMP
                  </p>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      disabled={importableResults.length === 0 || importJob.isPending}
                      onClick={() => importJob.mutate(importableResults.map((r) => r.id))}
                      title="Import every discovered importable device"
                    >
                      {importJob.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                      <Download className="h-4 w-4" />
                      Import all ({importableResults.length})
                    </Button>
                    <Button
                      disabled={selected.size === 0 || importJob.isPending}
                      onClick={() => importJob.mutate(Array.from(selected))}
                    >
                      {importJob.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                      <Download className="h-4 w-4" />
                      Import selected ({selected.size})
                    </Button>
                  </div>
                </div>
              </>
            )}

            {jobDetail.status === 'completed' && results.length === 0 && (
              <div className="py-8 text-center text-sm text-muted">
                No hosts responded in this range
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Job history */}
      <Card>
        <CardHeader>
          <CardTitle>Recent Jobs</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>CIDR</Th>
                <Th>Status</Th>
                <Th>Scanned</Th>
                <Th>SNMP</Th>
                <Th>Version</Th>
                <Th>Created</Th>
                <Th className="w-28 text-right"></Th>
              </Tr>
            </THead>
            <TBody>
              {(jobs || []).map((j) => (
                <Tr
                  key={j.id}
                  className={j.id === activeJob ? 'bg-primary/5' : ''}
                >
                  <Td className="font-mono text-xs font-medium">{j.cidr}</Td>
                  <Td>
                    <Badge
                      variant={
                        j.status === 'completed'
                          ? 'success'
                          : j.status === 'failed'
                            ? 'danger'
                            : 'info'
                      }
                    >
                      {j.status}
                    </Badge>
                  </Td>
                  <Td className="text-xs">
                    {j.scanned_hosts}/{j.total_hosts}
                  </Td>
                  <Td className="text-xs font-medium text-success">{j.responding_hosts}</Td>
                  <Td>
                    <Badge variant="outline">v{j.snmp_version}</Badge>
                  </Td>
                  <Td className="text-xs text-muted">{relativeTime(j.created_at)}</Td>
                  <Td>
                    <div className="flex justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => openJob(j.id)}
                        className="h-7"
                      >
                        <Eye className="h-3.5 w-3.5" />
                        View
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-muted hover:text-danger"
                        onClick={() => deleteJob.mutate(j.id)}
                        title="Delete"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ))}
              {(!jobs || jobs.length === 0) && (
                <Tr>
                  <Td colSpan={7} className="py-6 text-center text-muted">
                    No discovery jobs yet
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}

function StatPill({
  label,
  value,
  total,
  tone,
}: {
  label: string
  value: number
  total?: number
  tone?: 'success' | 'primary'
}) {
  const color =
    tone === 'success'
      ? 'text-success'
      : tone === 'primary'
        ? 'text-primary'
        : 'text-text'
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-[11px]">
      <span className={`font-semibold tabular-nums ${color}`}>
        {value}
        {total != null && <span className="text-muted">/{total}</span>}
      </span>
      <span className="text-muted">{label}</span>
    </span>
  )
}

function ResultDetails({ row }: { row: DiscoveryResult }) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <DetailField label="sysObjectID" value={row.sys_object_id} mono />
      <DetailField label="hostname guess" value={row.hostname_guess} />
      <DetailField
        label="matched profile"
        value={row.matched_profile_id ? row.matched_profile_id : '—'}
        mono={!!row.matched_profile_id}
      />
      <DetailField label="matched OS" value={row.matched_os_version} />
      <DetailField
        label="scanned at"
        value={row.scanned_at ? new Date(row.scanned_at).toLocaleString() : null}
      />
      {row.imported_device_id && (
        <DetailField label="device id" value={row.imported_device_id} mono />
      )}
      {row.sys_descr && (
        <div className="md:col-span-2">
          <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted">
            sysDescr
          </div>
          <pre className="whitespace-pre-wrap rounded bg-surface px-3 py-2 font-mono text-[11px] text-text/80">
            {row.sys_descr}
          </pre>
        </div>
      )}
      {row.error_message && (
        <div className="md:col-span-2 rounded bg-danger/10 px-3 py-2 text-[11px] text-danger">
          {row.error_message}
        </div>
      )}
    </div>
  )
}

function DetailField({
  label,
  value,
  mono,
}: {
  label: string
  value: string | null | undefined
  mono?: boolean
}) {
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
      <div className={`text-xs ${mono ? 'font-mono' : ''} ${value ? 'text-text' : 'text-muted'}`}>
        {value || '—'}
      </div>
    </div>
  )
}
