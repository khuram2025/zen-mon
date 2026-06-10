import { useMemo, useState } from 'react'
import { useNavigate, useParams, Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  Ban,
  CheckCircle2,
  Clock,
  Copy,
  Download,
  Eye,
  Loader2,
  Network,
  Radar,
  RefreshCcw,
  ScanLine,
  SlashSquare,
  Square,
  Upload,
  XCircle,
} from 'lucide-react'
import { discoveryApi } from './api'
import { DiscoveryResult, DiscoveryRun, ResultStatus } from './types'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import {
  CredentialStatusBadge,
  PhaseLabel,
  ProtocolPill,
  ResultStatusBadge,
  RunStatusBadge,
} from './helpers'

type Filter = 'all' | 'new' | 'existing' | 'changed' | 'unknown' | 'failed' | 'ignored' | 'imported'

export function RunPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [filter, setFilter] = useState<Filter>('new')
  const [search, setSearch] = useState('')
  const [importOpen, setImportOpen] = useState(false)
  const [cancelOpen, setCancelOpen] = useState(false)

  const { data: run, isLoading } = useQuery<DiscoveryRun>({
    queryKey: ['discovery', 'run', id],
    queryFn: () => discoveryApi.getRun(id!),
    enabled: !!id,
    refetchInterval: (q) => {
      const status = (q.state.data as any)?.status
      return status === 'running' || status === 'queued' ? 1200 : 8000
    },
  })

  const isRunning = run?.status === 'running' || run?.status === 'queued'

  const { data: results = [] } = useQuery<DiscoveryResult[]>({
    queryKey: ['discovery', 'run', id, 'results'],
    queryFn: () => discoveryApi.listResults(id!),
    enabled: !!id,
    // Poll faster while the run is in flight so partial results stream in,
    // then back off once it's complete.
    refetchInterval: isRunning ? 2500 : 12_000,
  })

  const cancelMutation = useMutation({
    mutationFn: () => discoveryApi.cancelRun(id!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery', 'run', id] })
      toast.success('Cancellation requested')
      setCancelOpen(false)
    },
    onError: (e: any) => toast.error('Cancel failed', apiErrorMessage(e)),
  })

  const ignoreMutation = useMutation({
    mutationFn: (ids: number[]) => discoveryApi.ignoreResults({ result_ids: ids }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery', 'run', id, 'results'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'ignored'] })
      toast.success('Devices ignored')
      setSelected(new Set())
    },
    onError: (e: any) => toast.error('Ignore failed', apiErrorMessage(e)),
  })

  // Filter + search
  const filtered = useMemo(() => {
    const term = search.toLowerCase()
    return results.filter((r) => {
      if (filter !== 'all' && r.status !== filter) return false
      if (!term) return true
      return (
        r.ip_address.includes(term) ||
        (r.hostname || '').toLowerCase().includes(term) ||
        (r.vendor || '').toLowerCase().includes(term) ||
        (r.model || '').toLowerCase().includes(term) ||
        (r.device_type || '').toLowerCase().includes(term)
      )
    })
  }, [results, filter, search])

  const counts = useMemo(() => {
    const c = {
      all: results.length,
      new: 0,
      existing: 0,
      changed: 0,
      unknown: 0,
      ignored: 0,
      failed: 0,
      imported: 0,
    }
    for (const r of results) (c as any)[r.status] = ((c as any)[r.status] || 0) + 1
    return c
  }, [results])

  const importableSelected = useMemo(() => {
    const ids = Array.from(selected)
    return results
      .filter((r) => ids.includes(r.id))
      .filter((r) => r.import_ready && !r.imported)
      .map((r) => r.id)
  }, [selected, results])

  const exportCSV = () => {
    if (!results.length) return
    const cols = [
      'ip_address',
      'hostname',
      'mac_address',
      'vendor',
      'device_type',
      'model',
      'os',
      'os_version',
      'open_ports',
      'protocols_detected',
      'status',
      'credential_status',
      'confidence_score',
    ]
    const lines = [cols.join(',')]
    for (const r of results) {
      lines.push(
        cols
          .map((c) => {
            const v = (r as any)[c]
            if (v == null) return ''
            if (Array.isArray(v)) return `"${v.join(';')}"`
            const s = String(v)
            return s.includes(',') ? `"${s.replace(/"/g, '""')}"` : s
          })
          .join(','),
      )
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `discovery-${id?.slice(0, 8)}.csv`
    a.click()
  }

  const toggle = (rid: number) => {
    const n = new Set(selected)
    n.has(rid) ? n.delete(rid) : n.add(rid)
    setSelected(n)
  }
  const selectAllVisible = () => {
    const ids = filtered.filter((r) => r.import_ready && !r.imported).map((r) => r.id)
    setSelected(new Set([...Array.from(selected), ...ids]))
  }
  const clearSelection = () => setSelected(new Set())

  if (isLoading || !run) {
    return (
      <Card>
        <CardContent className="py-16 text-center">
          <Loader2 className="mx-auto h-6 w-6 animate-spin text-primary" />
          <p className="mt-3 text-sm text-muted">Loading run…</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <button
            onClick={() => navigate('/discovery')}
            className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Back to profiles
          </button>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Radar className="h-6 w-6 text-primary" />
            {run.profile_name || 'Discovery run'}
          </h1>
          <p className="mt-1 flex items-center gap-2 text-xs text-muted">
            Run ID <span className="font-mono">{run.id.slice(0, 8)}</span>
            <span>·</span>
            Started {relativeTime(run.started_at || run.created_at)}
            <span>·</span>
            Trigger <Badge variant="outline">{run.trigger_type}</Badge>
            <RunStatusBadge status={run.status} />
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {isRunning ? (
            <Button variant="destructive" onClick={() => setCancelOpen(true)}>
              <Square className="h-4 w-4" /> Stop scan
            </Button>
          ) : (
            <>
              <Button variant="outline" onClick={exportCSV} disabled={results.length === 0}>
                <Download className="h-4 w-4" /> Export CSV
              </Button>
              <Button
                onClick={() => setImportOpen(true)}
                disabled={importableSelected.length === 0}
              >
                <Upload className="h-4 w-4" /> Import selected ({importableSelected.length})
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Live progress */}
      {isRunning && <RunProgress run={run} />}

      {/* Summary cards (after completion) */}
      {!isRunning && <SummaryCards run={run} />}

      {/* Activity log */}
      {(isRunning || (run.activity_log && run.activity_log.length > 0)) && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <ScanLine className="h-4 w-4 text-primary" />
              Activity log
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="max-h-48 overflow-y-auto rounded-md border border-border bg-surface2/40 px-3 py-2 font-mono text-[11px]">
              {(run.activity_log || []).slice().reverse().map((e, i) => (
                <div key={i} className="flex gap-3">
                  <span className="text-muted">
                    {new Date(e.ts).toLocaleTimeString()}
                  </span>
                  <span
                    className={cn(
                      e.level === 'warning' ? 'text-warning' : '',
                      e.level === 'error' ? 'text-danger' : '',
                    )}
                  >
                    {e.msg}
                  </span>
                </div>
              ))}
              {(run.activity_log || []).length === 0 && (
                <div className="text-muted">No activity yet…</div>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Results — only after run is no longer pending */}
      {!isRunning && (
        <>
          <Card>
            <CardContent className="space-y-3 p-3">
              <div className="flex flex-wrap items-center gap-1 overflow-x-auto">
                {(
                  [
                    ['new', 'New', counts.new, 'info'],
                    ['existing', 'Existing', counts.existing, 'default'],
                    ['changed', 'Changed', counts.changed, 'warning'],
                    ['unknown', 'Unknown', counts.unknown, 'default'],
                    ['imported', 'Imported', counts.imported, 'success'],
                    ['ignored', 'Ignored', counts.ignored, 'outline'],
                    ['failed', 'Failed', counts.failed, 'danger'],
                    ['all', 'All', counts.all, 'default'],
                  ] as const
                ).map(([key, label, count]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => {
                      setFilter(key as Filter)
                      setSelected(new Set())
                    }}
                    className={cn(
                      'whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-medium',
                      filter === key
                        ? 'bg-primary/10 text-text'
                        : 'text-muted hover:bg-surface2 hover:text-text',
                    )}
                  >
                    {label}
                    <span className="ml-1.5 rounded-full bg-surface2 px-1.5 py-0.5 text-[10px] tabular-nums text-muted">
                      {count}
                    </span>
                  </button>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  placeholder="Search IP, hostname, vendor, model…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="max-w-md"
                />
                {selected.size > 0 && (
                  <>
                    <Button variant="outline" size="sm" onClick={clearSelection}>
                      Clear ({selected.size})
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => ignoreMutation.mutate(Array.from(selected))}
                    >
                      <SlashSquare className="h-3.5 w-3.5" /> Ignore selected
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => setImportOpen(true)}
                      disabled={importableSelected.length === 0}
                    >
                      <Upload className="h-3.5 w-3.5" />
                      Import ({importableSelected.length})
                    </Button>
                  </>
                )}
                {filter === 'new' && counts.new > 0 && selected.size === 0 && (
                  <Button size="sm" variant="outline" onClick={selectAllVisible}>
                    Select all new
                  </Button>
                )}
                <span className="ml-auto text-xs text-muted">
                  {filtered.length} shown · {counts.all} total
                </span>
              </div>
            </CardContent>
          </Card>

          {filtered.length === 0 ? (
            <EmptyResults run={run} filter={filter} />
          ) : (
            <Card>
              <CardContent className="p-0">
                <Table>
                  <THead className="bg-surface2/50">
                    <Tr>
                      <Th className="w-8">
                        <input
                          type="checkbox"
                          aria-label="Select all visible importable rows"
                          checked={
                            filtered.length > 0 &&
                            filtered
                              .filter((r) => r.import_ready && !r.imported)
                              .every((r) => selected.has(r.id))
                          }
                          onChange={(e) => {
                            const ids = filtered
                              .filter((r) => r.import_ready && !r.imported)
                              .map((r) => r.id)
                            const next = new Set(selected)
                            if (e.target.checked) ids.forEach((i) => next.add(i))
                            else ids.forEach((i) => next.delete(i))
                            setSelected(next)
                          }}
                        />
                      </Th>
                      <Th>Status</Th>
                      <Th>Device</Th>
                      <Th>IP</Th>
                      <Th>MAC</Th>
                      <Th>Vendor / Model</Th>
                      <Th>Protocols</Th>
                      <Th>Ports</Th>
                      <Th>Credentials</Th>
                      <Th className="text-right">Confidence</Th>
                      <Th className="w-12"></Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {filtered.map((r) => (
                      <Tr key={r.id} className={r.ignored ? 'opacity-60' : ''}>
                        <Td>
                          <input
                            type="checkbox"
                            disabled={!r.import_ready || r.imported}
                            checked={selected.has(r.id)}
                            onChange={() => toggle(r.id)}
                          />
                        </Td>
                        <Td>
                          <ResultStatusBadge status={r.status} />
                          {r.conflict_type && (
                            <div className="text-[10px] text-warning">{r.conflict_type}</div>
                          )}
                        </Td>
                        <Td>
                          <div className="font-medium">{r.hostname || r.sys_name || '—'}</div>
                          {r.fqdn && r.fqdn !== r.hostname && (
                            <div className="text-[10px] text-muted">{r.fqdn}</div>
                          )}
                        </Td>
                        <Td className="font-mono text-xs">{r.ip_address}</Td>
                        <Td className="font-mono text-[10px] text-muted">
                          {r.mac_address || '—'}
                        </Td>
                        <Td>
                          {r.vendor ? (
                            <>
                              <div className="text-sm">{r.vendor}</div>
                              {r.model && <div className="text-[10px] text-muted">{r.model}</div>}
                            </>
                          ) : (
                            <span className="text-muted">—</span>
                          )}
                        </Td>
                        <Td>
                          <div className="flex flex-wrap gap-1">
                            {(r.protocols_detected || []).map((p) => (
                              <ProtocolPill key={p} p={p} />
                            ))}
                          </div>
                        </Td>
                        <Td className="text-[10px] text-muted">
                          {(r.open_ports || []).join(', ') || '—'}
                        </Td>
                        <Td>
                          <CredentialStatusBadge status={r.credential_status} />
                        </Td>
                        <Td className="text-right tabular-nums text-xs">
                          {r.confidence_score ? `${r.confidence_score}%` : '—'}
                        </Td>
                        <Td>
                          {r.imported_device_id ? (
                            <Link
                              to={`/devices/${r.imported_device_id}`}
                              className="text-primary hover:underline"
                              title="View device"
                            >
                              <Eye className="h-3.5 w-3.5" />
                            </Link>
                          ) : null}
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </CardContent>
            </Card>
          )}
        </>
      )}

      {/* Cancel confirm */}
      <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-warning" />
              Stop this scan?
            </DialogTitle>
          </DialogHeader>
          <p className="text-sm">
            The scan will stop at the next safe checkpoint. Any results discovered so far
            will remain available.
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCancelOpen(false)}>
              Keep scanning
            </Button>
            <Button
              variant="destructive"
              onClick={() => cancelMutation.mutate()}
              disabled={cancelMutation.isPending}
            >
              Stop scan
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Import drawer */}
      <ImportDrawer
        open={importOpen}
        onClose={() => setImportOpen(false)}
        runId={id!}
        selectedIds={importableSelected}
        results={results}
        onImported={() => {
          setSelected(new Set())
          setImportOpen(false)
        }}
      />
    </div>
  )
}

function RunProgress({ run }: { run: DiscoveryRun }) {
  const pct = run.progress_pct || 0
  const completion =
    run.total_targets > 0
      ? `${run.completed_targets}/${run.total_targets}`
      : `${run.completed_targets}`
  return (
    <Card>
      <CardContent className="space-y-4 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
            <div>
              <div className="text-sm font-semibold">
                <PhaseLabel phase={run.phase} />
              </div>
              <div className="text-xs text-muted">
                {completion} targets · {run.responding_targets} responding
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-xl font-semibold tabular-nums">{pct}%</div>
            <div className="text-xs text-muted">
              {run.failed_targets} failed · {run.credential_failures} cred fail
            </div>
          </div>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-surface2">
          <div
            className="h-full bg-primary transition-all duration-300"
            style={{ width: `${Math.max(2, pct)}%` }}
          />
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Total" value={run.total_targets} />
          <Stat label="Completed" value={run.completed_targets} />
          <Stat label="Responding" value={run.responding_targets} tone="success" />
          <Stat label="Failed" value={run.failed_targets} tone="danger" />
        </div>
      </CardContent>
    </Card>
  )
}

function SummaryCards({ run }: { run: DiscoveryRun }) {
  const tiles = [
    { label: 'New devices', value: run.new_devices, tone: 'info', icon: Network },
    { label: 'Existing', value: run.existing_devices, tone: 'default', icon: Eye },
    { label: 'Changed', value: run.changed_devices, tone: 'warning', icon: RefreshCcw },
    { label: 'Unknown', value: run.unknown_devices, tone: 'default', icon: AlertTriangle },
    { label: 'Failed', value: run.failed_targets, tone: 'danger', icon: XCircle },
    { label: 'Ready to import', value: run.ready_to_import, tone: 'success', icon: Upload },
  ]
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      {tiles.map((t) => (
        <Card key={t.label}>
          <CardContent className="p-4">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">
              <t.icon className="h-3.5 w-3.5" />
              {t.label}
            </div>
            <div
              className={cn(
                'mt-1 text-2xl font-semibold tabular-nums',
                t.tone === 'info' && 'text-info',
                t.tone === 'success' && 'text-success',
                t.tone === 'warning' && 'text-warning',
                t.tone === 'danger' && 'text-danger',
              )}
            >
              {t.value}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'success' | 'danger' | 'warning'
}) {
  return (
    <div className="rounded-md border border-border bg-surface2/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div
        className={cn(
          'mt-0.5 text-base font-semibold tabular-nums',
          tone === 'success' && 'text-success',
          tone === 'danger' && 'text-danger',
          tone === 'warning' && 'text-warning',
        )}
      >
        {value}
      </div>
    </div>
  )
}

function EmptyResults({ run, filter }: { run: DiscoveryRun; filter: Filter }) {
  if (run.responding_targets === 0 && filter === 'all') {
    return (
      <Card>
        <CardContent className="py-16 text-center">
          <AlertTriangle className="mx-auto h-10 w-10 text-warning opacity-60" />
          <h3 className="mt-3 text-lg font-semibold">No devices found in this scan</h3>
          <p className="mx-auto mt-1 max-w-md text-sm text-muted">
            Check the scan range, credentials, firewall rules, and protocol settings,
            then rerun the discovery profile.
          </p>
        </CardContent>
      </Card>
    )
  }
  if (filter === 'new' && run.new_devices === 0) {
    return (
      <Card>
        <CardContent className="py-16 text-center">
          <CheckCircle2 className="mx-auto h-10 w-10 text-success" />
          <h3 className="mt-3 text-lg font-semibold">Scan completed successfully</h3>
          <p className="mx-auto mt-1 max-w-md text-sm text-muted">
            No new devices were found. All responding devices already exist in your inventory.
          </p>
        </CardContent>
      </Card>
    )
  }
  if (filter === 'failed' && run.failed_targets === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted">
          No failed targets in this scan.
        </CardContent>
      </Card>
    )
  }
  if (run.credential_failures > 0 && filter === 'unknown') {
    return (
      <Card>
        <CardContent className="py-16 text-center">
          <AlertTriangle className="mx-auto h-10 w-10 text-warning opacity-70" />
          <h3 className="mt-3 text-lg font-semibold">Credentials failed for some devices</h3>
          <p className="mx-auto mt-1 max-w-md text-sm text-muted">
            {run.credential_failures} device(s) responded but credentials failed.
            Update credentials and rerun discovery.
          </p>
        </CardContent>
      </Card>
    )
  }
  return (
    <Card>
      <CardContent className="py-12 text-center text-sm text-muted">
        No results match this filter.
      </CardContent>
    </Card>
  )
}

// ────────────────────────────────────────────────────────────────────
// Import drawer (Step 9 — import confirmation flow)
// ────────────────────────────────────────────────────────────────────
function ImportDrawer({
  open,
  onClose,
  runId,
  selectedIds,
  results,
  onImported,
}: {
  open: boolean
  onClose: () => void
  runId: string
  selectedIds: number[]
  results: DiscoveryResult[]
  onImported: () => void
}) {
  const qc = useQueryClient()
  const [groupId, setGroupId] = useState('')
  const [tagsInput, setTagsInput] = useState('discovered')
  const [enableMonitoring, setEnableMonitoring] = useState(true)
  const [pingInterval, setPingInterval] = useState(60)
  const [location, setLocation] = useState('')
  const [environment, setEnvironment] = useState('')
  const [importAs, setImportAs] = useState<'auto' | 'device' | 'server' | 'both'>('auto')
  const [conflictStrategy, setConflictStrategy] = useState<'skip' | 'update' | 'import_as_new'>(
    'skip',
  )

  const { data: groups = [] } = useQuery<any[]>({
    queryKey: ['device-groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    enabled: open,
  })

  const selectedRows = results.filter((r) => selectedIds.includes(r.id))
  const conflicts = selectedRows.filter((r) => r.matched_device_id || r.conflict_type).length
  const newOnes = selectedRows.length - conflicts

  // Mirrors the backend's auto-routing predicate (server-class host?)
  const isServerClass = (r: DiscoveryResult) => {
    if ((r.device_type || '').toLowerCase() === 'server') return true
    const os = (r.os || '').toLowerCase()
    return ['windows', 'linux', 'ubuntu', 'debian', 'centos', 'rhel', 'esxi', 'macos']
      .some((h) => os.includes(h))
  }
  const serverClassCount = selectedRows.filter(isServerClass).length
  const toServers =
    importAs === 'server' || importAs === 'both'
      ? selectedRows.length
      : importAs === 'auto' ? serverClassCount : 0
  const toDevices =
    importAs === 'device' || importAs === 'both'
      ? selectedRows.length
      : importAs === 'auto'
        ? selectedRows.length - serverClassCount + (enableMonitoring ? serverClassCount : 0)
        : 0

  const importMutation = useMutation({
    mutationFn: () =>
      discoveryApi.importResults(runId, {
        result_ids: selectedIds,
        group_id: groupId || null,
        tags: tagsInput
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        enable_monitoring: enableMonitoring,
        ping_interval: pingInterval,
        location: location || null,
        environment: environment || null,
        import_as: importAs,
        conflict_strategy: conflictStrategy,
      }),
    onSuccess: (resp) => {
      qc.invalidateQueries({ queryKey: ['discovery', 'run', runId, 'results'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] })
      qc.invalidateQueries({ queryKey: ['devices'] })
      qc.invalidateQueries({ queryKey: ['servers'] })
      const parts = []
      if (resp.devices_created) parts.push(`${resp.devices_created} device${resp.devices_created === 1 ? '' : 's'}`)
      if (resp.servers_created) parts.push(`${resp.servers_created} server${resp.servers_created === 1 ? '' : 's'}`)
      toast.success(
        parts.length ? `Imported ${parts.join(' + ')}` : `${resp.successful} result(s) imported`,
        resp.conflicts ? `${resp.conflicts} skipped due to conflicts` : 'Monitoring will begin shortly',
      )
      onImported()
    },
    onError: (e: any) => toast.error('Import failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Upload className="h-5 w-5 text-primary" />
            Import {selectedRows.length} result{selectedRows.length === 1 ? '' : 's'}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          {/* Summary tiles */}
          <div className="grid grid-cols-5 gap-2">
            <Tile label="Total" value={selectedRows.length} />
            <Tile label="New" value={newOnes} tone="info" />
            <Tile label="Conflicts" value={conflicts} tone={conflicts ? 'warning' : 'default'} />
            <Tile label="→ Servers" value={toServers} tone={toServers ? 'info' : 'default'} />
            <Tile label="→ Devices" value={toDevices} tone={toDevices ? 'info' : 'default'} />
          </div>

          <FormField label="Import as">
            <Select value={importAs} onValueChange={(v) => setImportAs(v as any)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">
                  Auto-route — Windows/Linux hosts → Servers, network gear → Devices
                </SelectItem>
                <SelectItem value="device">Network devices only</SelectItem>
                <SelectItem value="server">Servers only (server monitoring)</SelectItem>
                <SelectItem value="both">Both — server inventory + monitored device</SelectItem>
              </SelectContent>
            </Select>
          </FormField>

          {conflicts > 0 && (
            <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2.5 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5" />
              <div>
                <div className="font-medium">Some devices conflict with existing inventory.</div>
                <div className="mt-0.5">
                  Choose what to do per below. By default, conflicts are skipped to avoid
                  silent duplicates.
                </div>
              </div>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2">
            <FormField label="Device group">
              <Select
                value={groupId || '__none__'}
                onValueChange={(v) => setGroupId(v === '__none__' ? '' : v)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="No group" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">No group</SelectItem>
                  {groups.map((g) => (
                    <SelectItem key={g.id} value={g.id}>
                      {g.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Location">
              <Input
                placeholder="Office HQ"
                value={location}
                onChange={(e) => setLocation(e.target.value)}
              />
            </FormField>
            {importAs !== 'device' && (
              <FormField label="Environment (servers)">
                <Input
                  placeholder="production"
                  value={environment}
                  onChange={(e) => setEnvironment(e.target.value)}
                />
              </FormField>
            )}
            <FormField label="Tags (comma-separated)">
              <Input value={tagsInput} onChange={(e) => setTagsInput(e.target.value)} />
            </FormField>
            <FormField label="Ping interval (sec)">
              <Input
                type="number"
                min={10}
                max={3600}
                value={pingInterval}
                onChange={(e) => setPingInterval(Number(e.target.value) || 60)}
              />
            </FormField>
            <FormField label="Conflict strategy">
              <Select
                value={conflictStrategy}
                onValueChange={(v) => setConflictStrategy(v as any)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="skip">Skip — do not import (safe default)</SelectItem>
                  <SelectItem value="update">Update existing device</SelectItem>
                  <SelectItem value="import_as_new">Import as new (force)</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Monitoring">
              <button
                type="button"
                onClick={() => setEnableMonitoring(!enableMonitoring)}
                className="flex w-full items-center justify-between rounded-md border border-border bg-surface px-3 py-2 hover:bg-surface2"
              >
                <span className="text-sm">Enable monitoring after import</span>
                <Switch checked={enableMonitoring} onCheckedChange={setEnableMonitoring} />
              </button>
            </FormField>
          </div>

          <div className="max-h-40 overflow-y-auto rounded-md border border-border bg-surface2/30 p-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted">
              Hosts to import
            </div>
            <div className="flex flex-wrap gap-1">
              {selectedRows.map((r) => (
                <Badge key={r.id} variant="outline" className="font-mono">
                  {r.ip_address}
                </Badge>
              ))}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => importMutation.mutate()}
            disabled={selectedRows.length === 0 || importMutation.isPending}
          >
            {importMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            <Upload className="h-4 w-4" />
            Confirm import
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Tile({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'info' | 'warning' | 'default'
}) {
  return (
    <div className="rounded-md border border-border bg-surface px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div
        className={cn(
          'text-lg font-semibold tabular-nums',
          tone === 'info' && 'text-info',
          tone === 'warning' && 'text-warning',
        )}
      >
        {value}
      </div>
    </div>
  )
}
