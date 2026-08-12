import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, Copy, Eye, Pencil, Plus, Trash2, Zap } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Switch } from '@/components/ui/Switch'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { AlertRuleWizardDialog } from '@/components/forms/AlertRuleWizardDialog'
import { AlertMessagePreviewDialog } from '@/components/alerts/AlertMessagePreviewDialog'
import { toast } from '@/components/ui/Toast'

const TRIGGER_LABELS: Record<string, string> = {
  down: 'Goes down',
  up: 'Comes up',
  degraded: 'Degraded',
  any: 'Any change',
}

function scopeLabel(r: any): string {
  if (r.service_check_id) return 'Service'
  if (r.service_check_group_id) return 'Svc group'
  if (r.group_id) return 'Group'
  if (r.device_type) return r.device_type
  if (r.location) return r.location
  if (r.device_id) return 'Device'
  if (r.metric === 'trap') return 'Traps'
  return 'All'
}

export function AlertRulesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [deleting, setDeleting] = useState<any>(null)
  const [previewing, setPreviewing] = useState<any>(null)

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
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alert-rules'] }),
    onError: (e: any) => toast.error('Toggle failed', apiErrorMessage(e)),
  })

  const duplicate = useMutation({
    mutationFn: async (rule: any) => {
      const { id, created_at, updated_at, created_by, ...rest } = rule
      return api.post('/alert-rules', {
        ...rest,
        name: `${rule.name} (copy)`,
        enabled: false,
      })
    },
    onSuccess: () => {
      toast.success('Rule duplicated')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
    },
    onError: (e: any) => toast.error('Duplicate failed', apiErrorMessage(e)),
  })

  const simulate = useMutation({
    mutationFn: async (id: string) => (await api.post(`/alert-rules/${id}/simulate`)).data,
    onSuccess: (data: any) => {
      const details = (data.results || []).map((r: any) => `${r.channel}: ${r.status}`).join(', ')
      toast.success(data.message || 'Test notification sent', details)
    },
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Bell className="h-5 w-5 text-primary" />
            Alert Manager
          </h1>
          <p className="mt-1 max-w-2xl text-xs text-muted">
            Define trigger conditions, reset behavior, notification actions, and schedules —
            SolarWinds-style alert definitions for devices, services, and SNMP traps.
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" />
          Create alert
        </Button>
      </div>

      {isError && (
        <Card>
          <CardContent className="py-6">
            <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
              <div className="font-medium">Backend error loading alert rules</div>
              <div className="text-xs opacity-80">{apiErrorMessage(error as any)}</div>
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
                  <Th>Trigger</Th>
                  <Th>Scope</Th>
                  <Th>Severity</Th>
                  <Th>Reset</Th>
                  <Th>Created</Th>
                  <Th className="w-36 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {(rules || []).map((r) => (
                  <Tr key={r.id}>
                    <Td>
                      <Switch checked={r.enabled} onCheckedChange={() => toggle.mutate(r.id)} />
                    </Td>
                    <Td>
                      <div className="font-medium">{r.name}</div>
                      {r.description && <div className="max-w-xs truncate text-xs text-muted">{r.description}</div>}
                    </Td>
                    <Td className="text-xs">
                      <div className="font-medium text-text">{TRIGGER_LABELS[r.trigger_on] || r.trigger_on || '—'}</div>
                      <div className="font-mono text-[10px] text-muted">
                        {r.metric === 'trap'
                          ? r.trap_oid ? `trap ${r.trap_oid}` : 'any trap'
                          : Array.isArray(r.conditions) && r.conditions.length > 1
                          ? `${r.conditions.length} conditions (${r.condition_logic})`
                          : r.metric}
                      </div>
                    </Td>
                    <Td>
                      <Badge variant="outline" className="text-[10px]">{scopeLabel(r)}</Badge>
                    </Td>
                    <Td>
                      <Badge variant={r.severity === 'critical' ? 'danger' : r.severity === 'warning' ? 'warning' : 'info'}>
                        {r.severity}
                      </Badge>
                    </Td>
                    <Td className="text-xs text-muted">
                      {r.recovery_alert ? (
                        <span className="text-emerald-600">Auto + notify</span>
                      ) : (
                        <span>Silent</span>
                      )}
                    </Td>
                    <Td className="text-xs text-muted">{relativeTime(r.created_at)}</Td>
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Preview & edit message"
                          onClick={() => setPreviewing(r)}>
                          <Eye className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Send test notification"
                          onClick={() => {
                            if (confirm(`Send test notification for "${r.name}"?`)) simulate.mutate(r.id)
                          }}>
                          <Zap className="h-3.5 w-3.5 text-amber-500" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate"
                          onClick={() => duplicate.mutate(r)}>
                          <Copy className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit"
                          onClick={() => { setEditing(r); setFormOpen(true) }}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" title="Delete"
                          onClick={() => setDeleting(r)}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {(!rules || rules.length === 0) && !isError && (
                  <Tr>
                    <Td colSpan={8} className="py-12 text-center text-muted">
                      No alert rules yet. Click &quot;Create alert&quot; to open the wizard.
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <AlertRuleWizardDialog open={formOpen} onOpenChange={setFormOpen} rule={editing} />
      <AlertMessagePreviewDialog
        open={!!previewing}
        onOpenChange={(o) => !o && setPreviewing(null)}
        rule={previewing}
      />
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
