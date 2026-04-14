import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Pencil, Play, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { StatusDot } from '@/components/ui/StatusDot'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { ServiceCheckFormDialog } from '@/components/forms/ServiceCheckFormDialog'
import { toast } from '@/components/ui/Toast'

export function ServicesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [deleting, setDeleting] = useState<any>(null)

  const { data: summary } = useQuery<any>({
    queryKey: ['service-checks', 'summary'],
    queryFn: async () => (await api.get('/service-checks/summary')).data,
    refetchInterval: 15_000,
  })

  const { data: checks } = useQuery<any[]>({
    queryKey: ['service-checks', 'list'],
    queryFn: async () => {
      const r = (await api.get('/service-checks?limit=200')).data
      return Array.isArray(r) ? r : r?.data || []
    },
    refetchInterval: 15_000,
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/service-checks/${id}`),
    onSuccess: () => {
      toast.success('Service check deleted')
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const testNow = useMutation({
    mutationFn: async (id: string) => (await api.post(`/service-checks/${id}/test`)).data,
    onSuccess: (data: any) => {
      toast.success('Test complete', data?.is_up ? 'Service is up' : `Down: ${data?.error || 'no response'}`)
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  const kindFor = (status: string) =>
    status === 'up' ? 'up' : status === 'down' ? 'down' : status === 'warning' ? 'warn' : 'idle'

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Activity className="h-5 w-5 text-primary" />
            Services
          </h1>
          <p className="text-xs text-muted">
            {summary?.total || 0} checks • {summary?.up || 0} up • {summary?.down || 0} down
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" />
          Add check
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Total" value={summary?.total || 0} />
        <Stat label="Up" value={summary?.up || 0} tone="success" />
        <Stat label="Down" value={summary?.down || 0} tone={(summary?.down || 0) > 0 ? 'danger' : undefined} />
        <Stat label="Warning" value={summary?.warning || 0} tone={(summary?.warning || 0) > 0 ? 'warning' : undefined} />
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-8"></Th>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th>Target</Th>
                  <Th>Response</Th>
                  <Th>Last check</Th>
                  <Th className="w-24 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {(checks || []).map((c) => (
                  <Tr key={c.id}>
                    <Td><StatusDot status={kindFor(c.status)} pulse={c.status === 'up'} /></Td>
                    <Td className="font-medium">{c.name}</Td>
                    <Td><Badge variant="outline" className="uppercase">{c.check_type}</Badge></Td>
                    <Td className="max-w-[300px] truncate font-mono text-xs">
                      {c.target_url || `${c.target_host}${c.target_port ? `:${c.target_port}` : ''}`}
                    </Td>
                    <Td className="font-mono text-xs">
                      {c.last_response_ms != null ? `${c.last_response_ms.toFixed(0)}ms` : '—'}
                    </Td>
                    <Td className="text-xs text-muted">{relativeTime(c.last_check_at)}</Td>
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Test now"
                          onClick={() => testNow.mutate(c.id)}
                        >
                          <Play className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Edit"
                          onClick={() => { setEditing(c); setFormOpen(true) }}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-muted hover:text-danger"
                          title="Delete"
                          onClick={() => setDeleting(c)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {(!checks || checks.length === 0) && (
                  <Tr>
                    <Td colSpan={7} className="py-12 text-center text-muted">
                      No service checks configured yet
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <ServiceCheckFormDialog open={formOpen} onOpenChange={setFormOpen} check={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete service check"
        description={<>Delete <span className="font-semibold text-text">{deleting?.name}</span>?</>}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
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
  tone?: 'success' | 'warning' | 'danger'
}) {
  const color =
    tone === 'success' ? 'text-success' :
    tone === 'warning' ? 'text-warning' :
    tone === 'danger' ? 'text-danger' : 'text-text'
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  )
}
