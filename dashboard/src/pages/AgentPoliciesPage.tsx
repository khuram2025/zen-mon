/**
 * AgentPoliciesPage — list and edit agent collection policies.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  CheckCircle2,
  Edit3,
  Lock,
  Plus,
  Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { SkeletonTable } from '@/components/ui/Skeleton'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import type { AgentPolicy } from '@/types/server'

type FormState = {
  name: string
  description: string
  platform: 'windows' | 'linux' | 'any'
  metric_interval_s: number
  upload_interval_s: number
  process_top_n: number
  service_watchlist: string
  process_watchlist: string
  update_ring: 'canary' | 'beta' | 'stable' | 'pinned'
}

const emptyForm = (): FormState => ({
  name: '',
  description: '',
  platform: 'windows',
  metric_interval_s: 30,
  upload_interval_s: 60,
  process_top_n: 25,
  service_watchlist: '',
  process_watchlist: '',
  update_ring: 'stable',
})

export default function AgentPoliciesPage() {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<AgentPolicy | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState<FormState>(emptyForm())
  const [deleteId, setDeleteId] = useState<string | null>(null)

  const listQ = useQuery<{ items: AgentPolicy[] }>({
    queryKey: ['agent-policies'],
    queryFn: async () => (await api.get('/agent-policies')).data,
  })

  const openCreate = () => {
    setForm(emptyForm())
    setEditing(null)
    setCreating(true)
  }
  const openEdit = (p: AgentPolicy) => {
    setForm({
      name: p.name,
      description: p.description ?? '',
      platform: p.platform,
      metric_interval_s: p.metric_interval_s,
      upload_interval_s: p.upload_interval_s,
      process_top_n: p.process_top_n,
      service_watchlist: (p.service_watchlist || []).join('\n'),
      process_watchlist: (p.process_watchlist || []).join('\n'),
      update_ring: p.update_ring,
    })
    setEditing(p)
    setCreating(false)
  }

  const toBody = (f: FormState) => ({
    name: f.name,
    description: f.description || null,
    platform: f.platform,
    metric_interval_s: f.metric_interval_s,
    upload_interval_s: f.upload_interval_s,
    process_top_n: f.process_top_n,
    service_watchlist: f.service_watchlist
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
    process_watchlist: f.process_watchlist
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
    update_ring: f.update_ring,
  })

  const saveM = useMutation({
    mutationFn: async () => {
      if (editing)
        return (await api.patch(`/agent-policies/${editing.id}`, toBody(form))).data
      return (await api.post(`/agent-policies`, toBody(form))).data
    },
    onSuccess: () => {
      toast.success(editing ? 'Policy updated' : 'Policy created')
      qc.invalidateQueries({ queryKey: ['agent-policies'] })
      setEditing(null)
      setCreating(false)
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const deleteM = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/agent-policies/${id}`)).data,
    onSuccess: () => {
      toast.success('Policy deleted')
      qc.invalidateQueries({ queryKey: ['agent-policies'] })
      setDeleteId(null)
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const open = creating || editing !== null

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Agent policies</h1>
          <p className="mt-0.5 text-xs text-muted">
            Define what host agents collect, how often, and at what cardinality.
          </p>
        </div>
        <Button size="sm" onClick={openCreate}>
          <Plus className="h-3.5 w-3.5" /> New policy
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          {listQ.isLoading ? (
            <SkeletonTable rows={4} cols={5} />
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Name</Th>
                  <Th>Platform</Th>
                  <Th>Collection</Th>
                  <Th>Upload</Th>
                  <Th>Ring</Th>
                  <Th>Agents</Th>
                  <Th />
                </Tr>
              </THead>
              <TBody>
                {(listQ.data?.items ?? []).map((p) => (
                  <Tr key={p.id} className="hover:bg-surface2/60">
                    <Td>
                      <div className="flex items-center gap-2 font-medium">
                        {p.name}
                        {p.is_builtin ? (
                          <Badge variant="info">
                            <Lock className="h-3 w-3" /> built-in
                          </Badge>
                        ) : null}
                      </div>
                      {p.description ? (
                        <div className="text-xs text-muted">{p.description}</div>
                      ) : null}
                    </Td>
                    <Td className="capitalize">{p.platform}</Td>
                    <Td>{p.metric_interval_s}s</Td>
                    <Td>{p.upload_interval_s}s</Td>
                    <Td className="capitalize">{p.update_ring}</Td>
                    <Td>
                      <Badge variant={p.agent_count > 0 ? 'success' : 'outline'}>
                        <Activity className="h-3 w-3" />
                        {p.agent_count}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="flex items-center justify-end gap-1">
                        <Button size="sm" variant="ghost" onClick={() => openEdit(p)}>
                          <Edit3 className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={p.is_builtin}
                          onClick={() => setDeleteId(p.id)}
                          title={p.is_builtin ? 'Built-in policies cannot be deleted' : 'Delete'}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={open}
        onOpenChange={(o) => {
          if (!o) {
            setEditing(null)
            setCreating(false)
          }
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{editing ? 'Edit policy' : 'New policy'}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <FormField label="Name">
                <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </FormField>
              <FormField label="Platform">
                <Select
                  value={form.platform}
                  onValueChange={(v) => setForm({ ...form, platform: v as FormState['platform'] })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="windows">Windows</SelectItem>
                    <SelectItem value="linux">Linux</SelectItem>
                    <SelectItem value="any">Any</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            </div>
            <FormField label="Description">
              <Textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                rows={2}
              />
            </FormField>
            <div className="grid gap-3 sm:grid-cols-3">
              <FormField label="Collection interval (s)">
                <Input
                  type="number"
                  value={form.metric_interval_s}
                  onChange={(e) =>
                    setForm({ ...form, metric_interval_s: Number(e.target.value) || 30 })
                  }
                />
              </FormField>
              <FormField label="Upload interval (s)">
                <Input
                  type="number"
                  value={form.upload_interval_s}
                  onChange={(e) =>
                    setForm({ ...form, upload_interval_s: Number(e.target.value) || 60 })
                  }
                />
              </FormField>
              <FormField label="Process top N">
                <Input
                  type="number"
                  value={form.process_top_n}
                  onChange={(e) =>
                    setForm({ ...form, process_top_n: Number(e.target.value) || 25 })
                  }
                />
              </FormField>
            </div>
            <FormField label="Update ring">
              <Select
                value={form.update_ring}
                onValueChange={(v) => setForm({ ...form, update_ring: v as FormState['update_ring'] })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="canary">Canary</SelectItem>
                  <SelectItem value="beta">Beta</SelectItem>
                  <SelectItem value="stable">Stable</SelectItem>
                  <SelectItem value="pinned">Pinned</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            <div className="grid gap-3 sm:grid-cols-2">
              <FormField label="Service watchlist (one per line)">
                <Textarea
                  value={form.service_watchlist}
                  onChange={(e) => setForm({ ...form, service_watchlist: e.target.value })}
                  rows={4}
                  placeholder="MSSQLSERVER\nW3SVC\nSpooler"
                />
              </FormField>
              <FormField label="Process watchlist (one per line)">
                <Textarea
                  value={form.process_watchlist}
                  onChange={(e) => setForm({ ...form, process_watchlist: e.target.value })}
                  rows={4}
                  placeholder="sqlservr.exe\nnginx.exe"
                />
              </FormField>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => {
                setEditing(null)
                setCreating(false)
              }}
            >
              Cancel
            </Button>
            <Button onClick={() => saveM.mutate()} disabled={!form.name || saveM.isPending}>
              <CheckCircle2 className="h-3.5 w-3.5" />
              {editing ? 'Save changes' : 'Create policy'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={!!deleteId}
        onOpenChange={(o) => !o && setDeleteId(null)}
        title="Delete this policy?"
        description="Agents currently using this policy will be reset to the default for their platform."
        destructive
        confirmText="Delete"
        onConfirm={() => {
          if (deleteId) deleteM.mutate(deleteId)
        }}
      />
    </div>
  )
}
