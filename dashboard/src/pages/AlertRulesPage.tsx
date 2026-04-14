import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, Pencil, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Switch } from '@/components/ui/Switch'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { AlertRuleFormDialog } from '@/components/forms/AlertRuleFormDialog'
import { toast } from '@/components/ui/Toast'

export function AlertRulesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [deleting, setDeleting] = useState<any>(null)

  const { data: rules, isError, error } = useQuery<any[]>({
    queryKey: ['alert-rules'],
    queryFn: async () => {
      const r = (await api.get('/alert-rules')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/alert-rules/${id}`),
    onSuccess: () => {
      toast.success('Rule deleted')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const toggle = useMutation({
    mutationFn: async (id: string) => api.post(`/alert-rules/${id}/toggle`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
    },
    onError: (e: any) => toast.error('Toggle failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Bell className="h-5 w-5 text-primary" />
            Alert Rules
          </h1>
          <p className="text-xs text-muted">
            {rules?.length || 0} rules defined
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" />
          New rule
        </Button>
      </div>

      {isError && (
        <Card>
          <CardContent className="py-6">
            <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
              <div className="font-medium">Backend error loading alert rules</div>
              <div className="text-xs opacity-80">{apiErrorMessage(error as any)}</div>
              <div className="mt-2 text-xs opacity-70">
                This is a pre-existing schema gap (missing email_subject columns) unrelated to the UI.
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-12">On</Th>
                  <Th>Name</Th>
                  <Th>Metric</Th>
                  <Th>Condition</Th>
                  <Th>Severity</Th>
                  <Th>Created</Th>
                  <Th className="w-20 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {(rules || []).map((r) => (
                  <Tr key={r.id}>
                    <Td>
                      <Switch
                        checked={r.enabled}
                        onCheckedChange={() => toggle.mutate(r.id)}
                      />
                    </Td>
                    <Td>
                      <div className="font-medium">{r.name}</div>
                      {r.description && <div className="text-xs text-muted">{r.description}</div>}
                    </Td>
                    <Td className="font-mono text-xs">{r.metric}</Td>
                    <Td className="font-mono text-xs">{r.operator} {r.threshold}</Td>
                    <Td>
                      <Badge variant={r.severity === 'critical' ? 'danger' : r.severity === 'warning' ? 'warning' : 'info'}>
                        {r.severity}
                      </Badge>
                    </Td>
                    <Td className="text-xs text-muted">{relativeTime(r.created_at)}</Td>
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button
                          variant="ghost" size="icon" className="h-7 w-7"
                          onClick={() => { setEditing(r); setFormOpen(true) }}
                          title="Edit"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                          onClick={() => setDeleting(r)}
                          title="Delete"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {(!rules || rules.length === 0) && !isError && (
                  <Tr>
                    <Td colSpan={7} className="py-12 text-center text-muted">
                      No alert rules yet. Click "New rule" to create one.
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <AlertRuleFormDialog open={formOpen} onOpenChange={setFormOpen} rule={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete alert rule"
        description={<>Delete <span className="font-semibold text-text">{deleting?.name}</span>?</>}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}
