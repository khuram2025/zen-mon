/**
 * AgentFleetPage — manage agents at scale.
 *
 * Columns: hostname · OS · version · status · heartbeat · queue · spool · policy ·
 *          update ring · cert expiry. Bulk actions: change policy, change ring,
 *          request diagnostics, rotate cert, trigger upgrade, enable/disable.
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  Bug,
  CheckCircle2,
  HelpCircle,
  KeySquare,
  Power,
  RefreshCw,
  Search,
  Settings as SettingsIcon,
  Slash,
  Upload,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { SkeletonTable } from '@/components/ui/Skeleton'
import type { Agent, AgentPolicy } from '@/types/server'

const STATUS_VARIANT: Record<string, 'success' | 'warning' | 'danger' | 'outline'> = {
  online: 'success',
  enrolling: 'warning',
  stale: 'warning',
  offline: 'outline',
  disabled: 'outline',
  updating: 'warning',
  error: 'danger',
}
const STATUS_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  online: CheckCircle2,
  enrolling: Activity,
  stale: AlertTriangle,
  offline: HelpCircle,
  disabled: Slash,
  updating: Upload,
  error: AlertCircle,
}

function fmtBytes(b: number) {
  if (!b) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let n = b
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(1)} ${units[i]}`
}

export default function AgentFleetPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkOpen, setBulkOpen] = useState<null | 'policy' | 'ring' | 'upgrade'>(null)
  const [bulkPolicy, setBulkPolicy] = useState('')
  const [bulkRing, setBulkRing] = useState('')
  const [bulkVersion, setBulkVersion] = useState('')

  const q = params.get('q') || ''
  const status = params.get('status') || ''
  const platform = params.get('platform') || ''
  const ring = params.get('ring') || ''

  const update = (k: string, v: string) => {
    const next = new URLSearchParams(params)
    if (v) next.set(k, v)
    else next.delete(k)
    setParams(next, { replace: true })
  }

  const fleetQ = useQuery<{ items: Agent[]; total: number }>({
    queryKey: ['agent-fleet', { q, status, platform, ring }],
    queryFn: async () => {
      const qp: Record<string, string> = { page_size: '200' }
      if (q) qp.q = q
      if (status) qp.status = status
      if (platform) qp.platform = platform
      if (ring) qp.update_ring = ring
      return (await api.get(`/agent-fleet?${new URLSearchParams(qp)}`)).data
    },
    refetchInterval: 15_000,
  })

  const policiesQ = useQuery<{ items: AgentPolicy[] }>({
    queryKey: ['agent-policies'],
    queryFn: async () => (await api.get('/agent-policies')).data,
  })

  const bulkM = useMutation({
    mutationFn: async (body: any) => (await api.post('/agent-fleet/bulk', body)).data,
    onSuccess: () => {
      toast.success('Action queued for selected agents')
      setSelected(new Set())
      setBulkOpen(null)
      qc.invalidateQueries({ queryKey: ['agent-fleet'] })
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const items = fleetQ.data?.items ?? []
  const allSelected = items.length > 0 && items.every((a) => selected.has(a.id))

  const toggleAll = () => {
    if (allSelected) setSelected(new Set())
    else setSelected(new Set(items.map((a) => a.id)))
  }
  const toggleOne = (id: string) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Agent fleet</h1>
          <p className="mt-0.5 text-xs text-muted">
            Operate installed host agents — bulk change policy, ring, or trigger upgrades.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={() => fleetQ.refetch()}>
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </Button>
      </div>

      <Card>
        <CardContent className="p-4">
          {/* Filters */}
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[220px]">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input
                placeholder="Search agent UID, hostname, version…"
                value={q}
                onChange={(e) => update('q', e.target.value)}
                className="pl-8"
              />
            </div>
            <Select value={status || '__all'} onValueChange={(v) => update('status', v === '__all' ? '' : v)}>
              <SelectTrigger className="w-[140px]">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">Any status</SelectItem>
                <SelectItem value="online">Online</SelectItem>
                <SelectItem value="enrolling">Enrolling</SelectItem>
                <SelectItem value="stale">Stale</SelectItem>
                <SelectItem value="offline">Offline</SelectItem>
                <SelectItem value="updating">Updating</SelectItem>
                <SelectItem value="error">Error</SelectItem>
                <SelectItem value="disabled">Disabled</SelectItem>
              </SelectContent>
            </Select>
            <Select value={platform || '__all'} onValueChange={(v) => update('platform', v === '__all' ? '' : v)}>
              <SelectTrigger className="w-[140px]">
                <SelectValue placeholder="Platform" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">Any platform</SelectItem>
                <SelectItem value="windows">Windows</SelectItem>
                <SelectItem value="linux">Linux</SelectItem>
                <SelectItem value="macos">macOS</SelectItem>
              </SelectContent>
            </Select>
            <Select value={ring || '__all'} onValueChange={(v) => update('ring', v === '__all' ? '' : v)}>
              <SelectTrigger className="w-[140px]">
                <SelectValue placeholder="Update ring" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">Any ring</SelectItem>
                <SelectItem value="canary">Canary</SelectItem>
                <SelectItem value="beta">Beta</SelectItem>
                <SelectItem value="stable">Stable</SelectItem>
                <SelectItem value="pinned">Pinned</SelectItem>
              </SelectContent>
            </Select>
            <div className="ml-auto text-xs text-muted">
              {fleetQ.data?.total ?? 0} agents
            </div>
          </div>

          {/* Bulk action bar */}
          {selected.size > 0 ? (
            <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs">
              <span className="font-medium">{selected.size} selected</span>
              <span className="text-muted">·</span>
              <Button size="sm" variant="outline" onClick={() => setBulkOpen('policy')}>
                Change policy
              </Button>
              <Button size="sm" variant="outline" onClick={() => setBulkOpen('ring')}>
                Change update ring
              </Button>
              <Button size="sm" variant="outline" onClick={() => setBulkOpen('upgrade')}>
                <Upload className="h-3.5 w-3.5" />
                Trigger upgrade
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  bulkM.mutate({ agent_ids: Array.from(selected), action: 'request_diagnostics' })
                }
              >
                <Bug className="h-3.5 w-3.5" /> Request diagnostics
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  bulkM.mutate({ agent_ids: Array.from(selected), action: 'rotate_certificate' })
                }
              >
                <KeySquare className="h-3.5 w-3.5" /> Rotate cert
              </Button>
              <Button
                size="sm"
                variant="destructive"
                onClick={() =>
                  bulkM.mutate({ agent_ids: Array.from(selected), action: 'disable' })
                }
              >
                <Power className="h-3.5 w-3.5" /> Disable
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
                Clear
              </Button>
            </div>
          ) : null}

          {fleetQ.isLoading ? (
            <SkeletonTable rows={6} cols={9} />
          ) : items.length === 0 ? (
            <div className="flex flex-col items-center py-10 text-center">
              <Activity className="h-7 w-7 text-muted/60" />
              <div className="mt-2 text-sm font-medium">No agents enrolled yet</div>
              <p className="mt-1 text-xs text-muted">
                Add a server and install the agent to populate the fleet.
              </p>
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th className="w-8">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      className="h-3.5 w-3.5"
                    />
                  </Th>
                  <Th>Host</Th>
                  <Th>Platform</Th>
                  <Th>Version</Th>
                  <Th>Status</Th>
                  <Th>Last heartbeat</Th>
                  <Th>Queue</Th>
                  <Th>Spool</Th>
                  <Th>Policy</Th>
                  <Th>Ring</Th>
                </Tr>
              </THead>
              <TBody>
                {items.map((a) => {
                  const Icon = STATUS_ICON[a.status] || HelpCircle
                  return (
                    <Tr key={a.id} className="hover:bg-surface2/60">
                      <Td>
                        <input
                          type="checkbox"
                          checked={selected.has(a.id)}
                          onChange={() => toggleOne(a.id)}
                          onClick={(e) => e.stopPropagation()}
                          className="h-3.5 w-3.5"
                        />
                      </Td>
                      <Td
                        onClick={() =>
                          a.server_id ? navigate(`/servers/${a.server_id}`) : undefined
                        }
                        className="cursor-pointer"
                      >
                        <div className="flex flex-col gap-0.5">
                          <span className="font-medium">{a.hostname || a.agent_uid}</span>
                          <span className="font-mono text-[10px] text-muted">
                            {a.agent_uid.slice(0, 18)}
                          </span>
                        </div>
                      </Td>
                      <Td className="text-sm capitalize">{a.platform}</Td>
                      <Td className="font-mono text-xs">{a.version || '—'}</Td>
                      <Td>
                        <Badge variant={STATUS_VARIANT[a.status] || 'outline'}>
                          <Icon className="h-3 w-3" />
                          {a.status}
                        </Badge>
                      </Td>
                      <Td className="text-xs text-muted">
                        {a.last_heartbeat_at ? relativeTime(a.last_heartbeat_at) : '—'}
                      </Td>
                      <Td className="font-mono text-xs">{a.queue_depth}</Td>
                      <Td className="font-mono text-xs">{fmtBytes(a.spool_bytes)}</Td>
                      <Td className="text-xs">{a.policy_name || '—'}</Td>
                      <Td className="text-xs capitalize">{a.update_ring}</Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Bulk dialogs */}
      <Dialog open={bulkOpen === 'policy'} onOpenChange={(o) => !o && setBulkOpen(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Change policy for {selected.size} agent(s)</DialogTitle>
          </DialogHeader>
          <Select value={bulkPolicy} onValueChange={setBulkPolicy}>
            <SelectTrigger>
              <SelectValue placeholder="Select policy" />
            </SelectTrigger>
            <SelectContent>
              {(policiesQ.data?.items ?? []).map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name} · {p.platform}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setBulkOpen(null)}>
              Cancel
            </Button>
            <Button
              disabled={!bulkPolicy}
              onClick={() =>
                bulkM.mutate({
                  agent_ids: Array.from(selected),
                  action: 'change_policy',
                  policy_id: bulkPolicy,
                })
              }
            >
              Apply
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={bulkOpen === 'ring'} onOpenChange={(o) => !o && setBulkOpen(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Change update ring for {selected.size} agent(s)</DialogTitle>
          </DialogHeader>
          <Select value={bulkRing} onValueChange={setBulkRing}>
            <SelectTrigger>
              <SelectValue placeholder="Select ring" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="canary">Canary</SelectItem>
              <SelectItem value="beta">Beta</SelectItem>
              <SelectItem value="stable">Stable</SelectItem>
              <SelectItem value="pinned">Pinned</SelectItem>
            </SelectContent>
          </Select>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setBulkOpen(null)}>
              Cancel
            </Button>
            <Button
              disabled={!bulkRing}
              onClick={() =>
                bulkM.mutate({
                  agent_ids: Array.from(selected),
                  action: 'change_update_ring',
                  update_ring: bulkRing,
                })
              }
            >
              Apply
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={bulkOpen === 'upgrade'} onOpenChange={(o) => !o && setBulkOpen(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Trigger upgrade for {selected.size} agent(s)</DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            <label className="text-xs text-muted">Target version (leave blank for latest)</label>
            <Input
              value={bulkVersion}
              onChange={(e) => setBulkVersion(e.target.value)}
              placeholder="0.2.0"
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setBulkOpen(null)}>
              Cancel
            </Button>
            <Button
              onClick={() =>
                bulkM.mutate({
                  agent_ids: Array.from(selected),
                  action: 'trigger_upgrade',
                  target_version: bulkVersion || null,
                })
              }
            >
              Queue upgrade
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
