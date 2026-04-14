import { FormEvent, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Play, Radar, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { relativeTime } from '@/lib/utils'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/Select'

export function DiscoveryPage() {
  const qc = useQueryClient()
  const [cidr, setCidr] = useState('10.0.0.0/24')
  const [community, setCommunity] = useState('public')
  const [version, setVersion] = useState('2c')
  const [activeJob, setActiveJob] = useState<string | null>(null)

  const { data: jobs } = useQuery<any[]>({
    queryKey: ['discover', 'list'],
    queryFn: async () => (await api.get('/snmp/discover')).data,
    refetchInterval: 5_000,
  })

  const createJob = useMutation({
    mutationFn: async () => {
      return (
        await api.post('/snmp/discover', {
          cidr,
          community,
          snmp_version: version,
          snmp_port: 161,
          timeout_ms: 2000,
        })
      ).data
    },
    onSuccess: (job) => {
      setActiveJob(job.id)
      qc.invalidateQueries({ queryKey: ['discover', 'list'] })
    },
  })

  const deleteJob = useMutation({
    mutationFn: async (id: string) => api.delete(`/snmp/discover/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['discover', 'list'] }),
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
    enabled: !!activeJob && jobDetail?.status === 'completed',
  })

  const [selected, setSelected] = useState<Set<number>>(new Set())
  const importJob = useMutation({
    mutationFn: async () =>
      (
        await api.post(`/snmp/discover/${activeJob}/import`, {
          result_ids: Array.from(selected),
        })
      ).data,
    onSuccess: () => {
      setSelected(new Set())
      qc.invalidateQueries({ queryKey: ['discover', activeJob, 'results'] })
      qc.invalidateQueries({ queryKey: ['devices', 'list'] })
    },
  })

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    createJob.mutate()
  }

  const progressPct =
    jobDetail && jobDetail.total_hosts > 0
      ? Math.round((jobDetail.scanned_hosts / jobDetail.total_hosts) * 100)
      : 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Radar className="h-6 w-6 text-primary" />
          Discovery
        </h1>
        <p className="text-sm text-muted">
          Sweep a CIDR range to find and classify new SNMP devices.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>New discovery job</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="grid grid-cols-1 gap-4 md:grid-cols-4">
            <div className="md:col-span-2 space-y-2">
              <label className="text-xs font-medium uppercase tracking-wider text-muted">CIDR range</label>
              <Input
                required
                placeholder="10.0.0.0/24"
                value={cidr}
                onChange={(e) => setCidr(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wider text-muted">Community</label>
              <Input
                required
                placeholder="public"
                value={community}
                onChange={(e) => setCommunity(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wider text-muted">SNMP version</label>
              <Select value={version} onValueChange={setVersion}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="2c">v2c</SelectItem>
                  <SelectItem value="1">v1</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="md:col-span-4 flex justify-end gap-2">
              <Button type="submit" disabled={createJob.isPending}>
                {createJob.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                Start sweep
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* Active job progress */}
      {activeJob && jobDetail && (
        <Card>
          <CardHeader>
            <CardTitle>
              Job {jobDetail.cidr} —{' '}
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
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-3 text-sm">
              <div className="flex-1">
                <div className="h-2 overflow-hidden rounded-full bg-surface2">
                  <div
                    className="h-full bg-primary transition-all"
                    style={{ width: `${progressPct}%` }}
                  />
                </div>
              </div>
              <div className="font-mono text-xs text-muted">
                {jobDetail.scanned_hosts} / {jobDetail.total_hosts} • {jobDetail.responding_hosts} responding
              </div>
            </div>

            {jobDetail.status === 'completed' && results && (
              <>
                <Table>
                  <THead>
                    <Tr>
                      <Th className="w-8">
                        <input
                          type="checkbox"
                          onChange={(e) => {
                            if (e.target.checked) {
                              setSelected(
                                new Set(
                                  results
                                    .filter((r) => r.snmp_responded && !r.already_known)
                                    .map((r) => r.id),
                                ),
                              )
                            } else {
                              setSelected(new Set())
                            }
                          }}
                        />
                      </Th>
                      <Th>IP</Th>
                      <Th>Ping</Th>
                      <Th>SNMP</Th>
                      <Th>Vendor</Th>
                      <Th>Model</Th>
                      <Th>sysName</Th>
                      <Th></Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {results.map((r) => (
                      <Tr key={r.id}>
                        <Td>
                          <input
                            type="checkbox"
                            disabled={!r.snmp_responded || r.already_known}
                            checked={selected.has(r.id)}
                            onChange={(e) => {
                              const next = new Set(selected)
                              if (e.target.checked) next.add(r.id)
                              else next.delete(r.id)
                              setSelected(next)
                            }}
                          />
                        </Td>
                        <Td className="font-mono text-xs">{r.ip_address}</Td>
                        <Td>
                          <Badge variant={r.is_reachable ? 'success' : 'outline'}>
                            {r.is_reachable ? 'yes' : 'no'}
                          </Badge>
                        </Td>
                        <Td>
                          <Badge variant={r.snmp_responded ? 'success' : 'outline'}>
                            {r.snmp_responded ? 'yes' : 'no'}
                          </Badge>
                        </Td>
                        <Td>{r.matched_vendor || '—'}</Td>
                        <Td className="text-xs">{r.matched_model || '—'}</Td>
                        <Td className="text-sm">{r.sys_name || '—'}</Td>
                        <Td>
                          {r.already_known && (
                            <Badge variant="outline">already monitored</Badge>
                          )}
                          {r.imported && <Badge variant="success">imported</Badge>}
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
                <div className="flex items-center justify-between">
                  <p className="text-xs text-muted">
                    {selected.size} selected
                  </p>
                  <Button
                    disabled={selected.size === 0 || importJob.isPending}
                    onClick={() => importJob.mutate()}
                  >
                    {importJob.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : null}
                    Import {selected.size} device{selected.size === 1 ? '' : 's'}
                  </Button>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {/* Job history */}
      <Card>
        <CardHeader>
          <CardTitle>Recent discovery jobs</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <Tr>
                <Th>CIDR</Th>
                <Th>Status</Th>
                <Th>Scanned</Th>
                <Th>Responding</Th>
                <Th>Created</Th>
                <Th></Th>
              </Tr>
            </THead>
            <TBody>
              {(jobs || []).map((j) => (
                <Tr key={j.id}>
                  <Td className="font-mono text-xs">{j.cidr}</Td>
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
                  <Td className="text-xs">{j.scanned_hosts} / {j.total_hosts}</Td>
                  <Td className="text-xs">{j.responding_hosts}</Td>
                  <Td className="text-xs text-muted">{relativeTime(j.created_at)}</Td>
                  <Td>
                    <div className="flex gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setActiveJob(j.id)}>
                        Open
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => deleteJob.mutate(j.id)}
                        title="Delete"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ))}
              {(!jobs || jobs.length === 0) && (
                <Tr>
                  <Td colSpan={6} className="text-center text-muted">
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
