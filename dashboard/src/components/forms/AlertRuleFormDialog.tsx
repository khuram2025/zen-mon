import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'

type State = {
  name: string
  description: string
  enabled: boolean
  metric: string
  operator: string
  threshold: number
  duration: number
  severity: 'info' | 'warning' | 'critical'
  cooldown: number
  device_id: string
  group_id: string
}

const empty: State = {
  name: '',
  description: '',
  enabled: true,
  metric: 'ping_status',
  operator: '==',
  threshold: 0,
  duration: 0,
  severity: 'warning',
  cooldown: 300,
  device_id: '',
  group_id: '',
}

export function AlertRuleFormDialog({
  open,
  onOpenChange,
  rule,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  rule?: any
}) {
  const isEdit = !!rule?.id
  const qc = useQueryClient()
  const [s, setS] = useState<State>(empty)

  const { data: devicesResp } = useQuery<any>({
    queryKey: ['devices', 'list-min'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    enabled: open,
  })
  const devices = devicesResp?.data || []

  const { data: groups } = useQuery<any[]>({
    queryKey: ['devices', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    enabled: open,
  })

  useEffect(() => {
    if (!open) return
    if (rule) {
      setS({
        ...empty,
        name: rule.name || '',
        description: rule.description || '',
        enabled: rule.enabled ?? true,
        metric: rule.metric || 'ping_status',
        operator: rule.operator || '==',
        threshold: rule.threshold ?? 0,
        duration: rule.duration ?? 0,
        severity: rule.severity || 'warning',
        cooldown: rule.cooldown ?? 300,
        device_id: rule.device_id || '',
        group_id: rule.group_id || '',
      })
    } else {
      setS(empty)
    }
  }, [open, rule])

  const save = useMutation({
    mutationFn: async (payload: any) => {
      if (isEdit) return (await api.put(`/alert-rules/${rule.id}`, payload)).data
      return (await api.post('/alert-rules', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Alert rule updated' : 'Alert rule created')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate({
      name: s.name,
      description: s.description || null,
      enabled: s.enabled,
      metric: s.metric,
      operator: s.operator,
      threshold: s.threshold,
      duration: s.duration,
      severity: s.severity,
      cooldown: s.cooldown,
      device_id: s.device_id || null,
      group_id: s.group_id || null,
      notify_channels: [],
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit alert rule' : 'New alert rule'}</DialogTitle>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <FormField label="Name" required>
            <Input
              required
              value={s.name}
              onChange={(e) => setS({ ...s, name: e.target.value })}
              placeholder="High RTT on core switches"
            />
          </FormField>
          <FormField label="Description">
            <Textarea
              value={s.description}
              onChange={(e) => setS({ ...s, description: e.target.value })}
              placeholder="Optional description"
            />
          </FormField>

          <div className="space-y-3 rounded-md border border-border p-3">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Condition</div>
            <div className="grid grid-cols-3 gap-3">
              <FormField label="Metric">
                <Select value={s.metric} onValueChange={(v) => setS({ ...s, metric: v })}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ping_status">Ping status (0/1)</SelectItem>
                    <SelectItem value="rtt">Round-trip time (ms)</SelectItem>
                    <SelectItem value="packet_loss">Packet loss (%)</SelectItem>
                    <SelectItem value="jitter">Jitter (ms)</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Operator">
                <Select value={s.operator} onValueChange={(v) => setS({ ...s, operator: v })}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value=">">greater than</SelectItem>
                    <SelectItem value=">=">greater or equal</SelectItem>
                    <SelectItem value="<">less than</SelectItem>
                    <SelectItem value="<=">less or equal</SelectItem>
                    <SelectItem value="==">equals</SelectItem>
                    <SelectItem value="!=">not equal</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Threshold">
                <Input
                  type="number"
                  step="any"
                  value={s.threshold}
                  onChange={(e) => setS({ ...s, threshold: Number(e.target.value) })}
                />
              </FormField>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <FormField label="Duration (s)" hint="Hold for N seconds before firing">
                <Input
                  type="number"
                  min={0}
                  value={s.duration}
                  onChange={(e) => setS({ ...s, duration: Number(e.target.value) })}
                />
              </FormField>
              <FormField label="Cooldown (s)" hint="Minimum gap between fires">
                <Input
                  type="number"
                  min={60}
                  value={s.cooldown}
                  onChange={(e) => setS({ ...s, cooldown: Number(e.target.value) })}
                />
              </FormField>
            </div>
          </div>

          <div className="space-y-3 rounded-md border border-border p-3">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Scope</div>
            <div className="grid grid-cols-2 gap-3">
              <FormField label="Device">
                <Select
                  value={s.device_id || '__all__'}
                  onValueChange={(v) => setS({ ...s, device_id: v === '__all__' ? '' : v, group_id: '' })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Any device" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">Any device</SelectItem>
                    {devices.slice(0, 100).map((d: any) => (
                      <SelectItem key={d.id} value={d.id}>{d.hostname}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Group">
                <Select
                  value={s.group_id || '__all__'}
                  onValueChange={(v) => setS({ ...s, group_id: v === '__all__' ? '' : v, device_id: '' })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Any group" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">Any group</SelectItem>
                    {(groups || []).map((g: any) => (
                      <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <FormField label="Severity">
              <Select value={s.severity} onValueChange={(v: any) => setS({ ...s, severity: v })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="info">Info</SelectItem>
                  <SelectItem value="warning">Warning</SelectItem>
                  <SelectItem value="critical">Critical</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            <div className="flex items-center justify-between rounded-md border border-border px-3">
              <span className="text-xs font-medium uppercase tracking-wider text-muted">Enabled</span>
              <Switch checked={s.enabled} onCheckedChange={(v) => setS({ ...s, enabled: v })} />
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create rule'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
