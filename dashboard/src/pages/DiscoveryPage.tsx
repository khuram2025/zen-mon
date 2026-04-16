import { FormEvent, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle, Key, Loader2, Play, Radar, Trash2 } from 'lucide-react'
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
  id: string; name: string; snmp_version: string;
  community: string | null; v3_username: string | null;
  port: number; timeout_ms: number; retries: number
}

export function DiscoveryPage() {
  const qc = useQueryClient()
  const [cidr, setCidr] = useState('10.0.0.0/24')
  const [credentialId, setCredentialId] = useState('__manual__')
  const [community, setCommunity] = useState('public')
  const [version, setVersion] = useState('2c')
  const [port, setPort] = useState(161)
  const [timeoutMs, setTimeoutMs] = useState(2000)
  const [activeJob, setActiveJob] = useState<string | null>(null)

  // Fetch saved credentials
  const { data: credentials } = useQuery<Credential[]>({
    queryKey: ['snmp-credentials'],
    queryFn: async () => (await api.get('/snmp-credentials')).data,
  })

  const { data: jobs } = useQuery<any[]>({
    queryKey: ['discover', 'list'],
    queryFn: async () => (await api.get('/snmp/discover')).data,
    refetchInterval: 5_000,
  })

  const selectedCred = credentials?.find((c) => c.id === credentialId)

  const createJob = useMutation({
    mutationFn: async () => {
      const payload: any = { cidr }
      if (credentialId !== '__manual__' && selectedCred) {
        payload.community = selectedCred.community || 'public'
        payload.snmp_version = selectedCred.snmp_version
        payload.snmp_port = selectedCred.port
        payload.timeout_ms = selectedCred.timeout_ms
      } else {
        payload.community = community
        payload.snmp_version = version
        payload.snmp_port = port
        payload.timeout_ms = timeoutMs
      }
      return (await api.post('/snmp/discover', payload)).data
    },
    onSuccess: (job) => {
      setActiveJob(job.id)
      qc.invalidateQueries({ queryKey: ['discover', 'list'] })
      toast.success('Discovery started')
    },
    onError: (e: any) => toast.error('Discovery failed', apiErrorMessage(e)),
  })

  const deleteJob = useMutation({
    mutationFn: async (id: string) => api.delete(`/snmp/discover/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discover', 'list'] })
      toast.success('Job deleted')
    },
  })

  const { data: jobDetail } = useQuery<any>({
    queryKey: ['discover', activeJob],
    queryFn: async () => (await api.get(`/snmp/discover/${activeJob}`)).data,
    enabled: !!activeJob,
    refetchInterval: 2_000,
  })

  const { data: results } = useQuery<any[]>({
    queryKey: ['discover', activeJob, 'results'],
    queryFn: async () => (await api.get(`/snmp/discover/${activeJob}/results`)).data,
    enabled: !!activeJob && (jobDetail?.status === 'completed' || jobDetail?.status === 'failed'),
  })

  const [selected, setSelected] = useState<Set<number>>(new Set())
  const importJob = useMutation({
    mutationFn: async () =>
      (await api.post(`/snmp/discover/${activeJob}/import`, { result_ids: Array.from(selected) })).data,
    onSuccess: (d) => {
      setSelected(new Set())
      qc.invalidateQueries({ queryKey: ['discover', activeJob, 'results'] })
      qc.invalidateQueries({ queryKey: ['devices'] })
      toast.success(`Imported ${d.created} device(s)${d.skipped ? `, ${d.skipped} skipped` : ''}`)
    },
    onError: (e: any) => toast.error('Import failed', apiErrorMessage(e)),
  })

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    createJob.mutate()
  }

  const progressPct =
    jobDetail && jobDetail.total_hosts > 0
      ? Math.round((jobDetail.scanned_hosts / jobDetail.total_hosts) * 100)
      : 0

  const respondingResults = results?.filter((r) => r.snmp_responded) || []
  const importableResults = results?.filter((r) => r.snmp_responded && !r.already_known && !r.imported) || []

  return (
    <div className="space-y-5">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Radar className="h-5 w-5 text-primary" /> Network Discovery
        </h1>
        <p className="text-xs text-muted">Sweep a CIDR range to find and classify SNMP-enabled devices</p>
      </div>

      {/* Discovery form */}
      <Card>
        <CardHeader><CardTitle>New Discovery Sweep</CardTitle></CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <FormField label="CIDR Range" required hint="Max /20 (1024 hosts)">
                <Input required placeholder="10.0.0.0/24" value={cidr} onChange={(e) => setCidr(e.target.value)} />
              </FormField>

              <FormField label="SNMP Credential" hint="Select a saved credential or enter manually">
                <Select value={credentialId} onValueChange={setCredentialId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select credential..." />
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

            {/* Show selected credential summary or manual fields */}
            {credentialId !== '__manual__' && selectedCred ? (
              <div className="rounded-md border border-primary/20 bg-primary/5 px-4 py-3">
                <div className="flex items-center gap-2 text-sm">
                  <Key className="h-4 w-4 text-primary" />
                  <span className="font-medium">{selectedCred.name}</span>
                  <Badge variant="info">v{selectedCred.snmp_version}</Badge>
                  <span className="text-xs text-muted">Port {selectedCred.port} · Timeout {selectedCred.timeout_ms}ms</span>
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <FormField label="Community">
                  <Input value={community} onChange={(e) => setCommunity(e.target.value)} placeholder="public" />
                </FormField>
                <FormField label="Version">
                  <Select value={version} onValueChange={setVersion}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="2c">v2c</SelectItem>
                      <SelectItem value="1">v1</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
                <FormField label="Port">
                  <Input type="number" value={port} onChange={(e) => setPort(Number(e.target.value) || 161)} min={1} max={65535} />
                </FormField>
                <FormField label="Timeout (ms)">
                  <Input type="number" value={timeoutMs} onChange={(e) => setTimeoutMs(Number(e.target.value) || 2000)} min={200} max={30000} step={100} />
                </FormField>
              </div>
            )}

            <div className="flex justify-end">
              <Button type="submit" disabled={createJob.isPending}>
                {createJob.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
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
            <div className="flex items-center justify-between">
              <CardTitle className="flex items-center gap-2">
                {jobDetail.cidr}
                <Badge variant={jobDetail.status === 'completed' ? 'success' : jobDetail.status === 'failed' ? 'danger' : 'info'}>
                  {jobDetail.status}
                </Badge>
              </CardTitle>
              {jobDetail.status === 'completed' && results && (
                <div className="flex gap-3 text-xs">
                  <span className="text-muted">{results.length} scanned</span>
                  <span className="text-success">{respondingResults.length} SNMP</span>
                  <span className="text-primary">{importableResults.length} importable</span>
                </div>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Progress bar */}
            {(jobDetail.status === 'running' || jobDetail.status === 'pending') && (
              <div className="flex items-center gap-3">
                <div className="flex-1">
                  <div className="h-2 overflow-hidden rounded-full bg-surface2">
                    <div className="h-full bg-primary transition-all" style={{ width: `${progressPct}%` }} />
                  </div>
                </div>
                <div className="font-mono text-xs text-muted">
                  {jobDetail.scanned_hosts}/{jobDetail.total_hosts} · {jobDetail.responding_hosts} responding
                </div>
              </div>
            )}

            {/* Results table */}
            {results && results.length > 0 && (
              <>
                <Table>
                  <THead className="bg-surface2/50">
                    <Tr>
                      <Th className="w-8">
                        <input
                          type="checkbox"
                          checked={importableResults.length > 0 && selected.size === importableResults.length}
                          onChange={(e) => {
                            if (e.target.checked) setSelected(new Set(importableResults.map((r) => r.id)))
                            else setSelected(new Set())
                          }}
                        />
                      </Th>
                      <Th>IP Address</Th>
                      <Th>Ping</Th>
                      <Th>SNMP</Th>
                      <Th>sysName</Th>
                      <Th>Vendor</Th>
                      <Th>Model</Th>
                      <Th>sysObjectID</Th>
                      <Th>Status</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {results.map((r) => (
                      <Tr key={r.id} className={r.already_known ? 'opacity-50' : ''}>
                        <Td>
                          <input
                            type="checkbox"
                            disabled={!r.snmp_responded || r.already_known || r.imported}
                            checked={selected.has(r.id)}
                            onChange={(e) => {
                              const next = new Set(selected)
                              if (e.target.checked) next.add(r.id)
                              else next.delete(r.id)
                              setSelected(next)
                            }}
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
                            <AlertTriangle className="h-4 w-4 text-warning" title={r.error_message || 'SNMP failed'} />
                          ) : (
                            <span className="text-xs text-muted">—</span>
                          )}
                        </Td>
                        <Td className="text-sm">{r.sys_name || <span className="text-muted">—</span>}</Td>
                        <Td className="text-sm">{r.matched_vendor || <span className="text-muted">—</span>}</Td>
                        <Td className="text-xs">{r.matched_model || <span className="text-muted">—</span>}</Td>
                        <Td className="font-mono text-[11px] text-muted max-w-[200px] truncate" title={r.sys_object_id || ''}>
                          {r.sys_object_id || '—'}
                        </Td>
                        <Td>
                          {r.imported ? (
                            <Badge variant="success">imported</Badge>
                          ) : r.already_known ? (
                            <Badge variant="outline">existing</Badge>
                          ) : r.error_message ? (
                            <span className="text-xs text-danger" title={r.error_message}>error</span>
                          ) : null}
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>

                {/* Import bar */}
                <div className="flex items-center justify-between rounded-md bg-surface2/50 px-4 py-2">
                  <p className="text-xs text-muted">{selected.size} of {importableResults.length} importable devices selected</p>
                  <Button disabled={selected.size === 0 || importJob.isPending} onClick={() => importJob.mutate()}>
                    {importJob.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                    Import {selected.size} device{selected.size === 1 ? '' : 's'}
                  </Button>
                </div>
              </>
            )}

            {/* No results */}
            {jobDetail.status === 'completed' && results && results.length === 0 && (
              <div className="py-8 text-center text-sm text-muted">No hosts responded in this range</div>
            )}

            {jobDetail.status === 'failed' && (
              <div className="rounded-md bg-danger/10 px-4 py-3 text-sm text-danger">{jobDetail.error_message || 'Discovery failed'}</div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Job history */}
      <Card>
        <CardHeader><CardTitle>Recent Jobs</CardTitle></CardHeader>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr><Th>CIDR</Th><Th>Status</Th><Th>Scanned</Th><Th>SNMP</Th><Th>Version</Th><Th>Created</Th><Th className="w-24" /></Tr>
            </THead>
            <TBody>
              {(jobs || []).map((j) => (
                <Tr key={j.id}>
                  <Td className="font-mono text-xs font-medium">{j.cidr}</Td>
                  <Td><Badge variant={j.status === 'completed' ? 'success' : j.status === 'failed' ? 'danger' : 'info'}>{j.status}</Badge></Td>
                  <Td className="text-xs">{j.scanned_hosts}/{j.total_hosts}</Td>
                  <Td className="text-xs font-medium text-success">{j.responding_hosts}</Td>
                  <Td><Badge variant="outline">v{j.snmp_version}</Badge></Td>
                  <Td className="text-xs text-muted">{relativeTime(j.created_at)}</Td>
                  <Td>
                    <div className="flex gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setActiveJob(j.id)}>View</Button>
                      <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" onClick={() => deleteJob.mutate(j.id)}><Trash2 className="h-3.5 w-3.5" /></Button>
                    </div>
                  </Td>
                </Tr>
              ))}
              {(!jobs || jobs.length === 0) && <Tr><Td colSpan={7} className="py-6 text-center text-muted">No discovery jobs yet</Td></Tr>}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
