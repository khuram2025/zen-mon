import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Loader2, Plus, Trash2 } from 'lucide-react'
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

type Cond = { metric: string; operator: string; threshold: number }

type State = {
  name: string
  description: string
  enabled: boolean
  conditions: Cond[]
  condition_logic: 'AND' | 'OR'
  duration: number
  severity: 'info' | 'warning' | 'critical'
  cooldown: number
  source: 'device' | 'service' | 'trap' | 'apm'
  device_id: string
  group_id: string
  service_check_id: string
  service_check_group_id: string
  trap_oid: string
  target: string
  notify_channels: string[]
  schedule_start: string
  schedule_end: string
  schedule_days: number[]
}

// ISO weekday order: 1=Mon … 7=Sun (matches backend schedule_days + enforcement).
const DAY_LABELS: { n: number; label: string }[] = [
  { n: 1, label: 'Mon' }, { n: 2, label: 'Tue' }, { n: 3, label: 'Wed' },
  { n: 4, label: 'Thu' }, { n: 5, label: 'Fri' }, { n: 6, label: 'Sat' }, { n: 7, label: 'Sun' },
]

const CHANNEL_TYPE_LABEL: Record<string, string> = {
  email: 'Email', sms: 'SMS', webhook: 'Webhook', slack: 'Slack',
  telegram: 'Telegram', teams: 'Teams', discord: 'Discord', pagerduty: 'PagerDuty',
}

// Network-device (SNMP) metrics, evaluated by the periodic network alert
// evaluator. Interface metrics (if_*) can be narrowed to specific interfaces
// via the optional interface filter (target).
const NETWORK_METRICS = [
  { value: 'if_util_pct', label: 'Interface utilization (%)', iface: true },
  { value: 'if_in_bps', label: 'Interface inbound (bps)', iface: true },
  { value: 'if_out_bps', label: 'Interface outbound (bps)', iface: true },
  { value: 'if_errors', label: 'Interface errors (per window)', iface: true },
  { value: 'if_discards', label: 'Interface discards (per window)', iface: true },
  { value: 'if_oper_status', label: 'Interface down (oper=2)', iface: true },
  { value: 'cpu', label: 'Device CPU (%)', iface: false },
  { value: 'memory', label: 'Device memory (%)', iface: false },
  { value: 'temperature', label: 'Device temperature (°)', iface: false },
  { value: 'session_count', label: 'Session count', iface: false },
  { value: 'uptime_reset', label: 'Reboot detected (1=yes)', iface: false },
] as const

const INTERFACE_METRICS = new Set<string>(NETWORK_METRICS.filter((m) => m.iface).map((m) => m.value))

// APM (application) metrics, evaluated per-service by the periodic APM alert
// evaluator against the RED rollups. Latency is milliseconds; error rate and
// apdex are fractions in 0–1; throughput is requests/second. Scope: the
// service picker (stored in `target`; empty = every reporting service).
const APM_METRICS = [
  { value: 'apm_latency_p95', label: 'p95 latency (ms)' },
  { value: 'apm_latency_p99', label: 'p99 latency (ms)' },
  { value: 'apm_latency_p50', label: 'p50 latency (ms)' },
  { value: 'apm_error_rate', label: 'Error rate (0.02 = 2%)' },
  { value: 'apm_throughput', label: 'Throughput (req/s)' },
  { value: 'apm_apdex', label: 'Apdex (0–1)' },
] as const

const empty: State = {
  name: '',
  description: '',
  enabled: true,
  conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }],
  condition_logic: 'AND',
  duration: 0,
  severity: 'warning',
  cooldown: 300,
  source: 'device',
  device_id: '',
  group_id: '',
  service_check_id: '',
  service_check_group_id: '',
  trap_oid: '',
  target: '',
  notify_channels: [],
  schedule_start: '',
  schedule_end: '',
  schedule_days: [],
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

  const { data: serviceChecksResp } = useQuery<any>({
    queryKey: ['service-checks', 'list-min'],
    queryFn: async () => (await api.get('/service-checks?limit=200')).data,
    enabled: open,
  })
  const serviceChecks = serviceChecksResp?.data || []

  const { data: serviceGroups = [] } = useQuery<any[]>({
    queryKey: ['service-check-groups'],
    queryFn: async () => (await api.get('/service-check-groups')).data,
    enabled: open,
  })

  const { data: channelsResp } = useQuery<any>({
    queryKey: ['channels', 'list-min'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    enabled: open,
  })

  const { data: apmServicesResp } = useQuery<any>({
    queryKey: ['apm', 'services', 'list-min'],
    queryFn: async () => (await api.get('/apm/services?range=24h')).data,
    enabled: open && s.source === 'apm',
  })
  const apmServices: string[] = (apmServicesResp?.services || []).map((x: any) => x.name)
  const channels: any[] = Array.isArray(channelsResp) ? channelsResp : channelsResp?.data || []

  const toggleChannel = (id: string) =>
    setS((st) => ({
      ...st,
      notify_channels: st.notify_channels.includes(id)
        ? st.notify_channels.filter((c) => c !== id)
        : [...st.notify_channels, id],
    }))
  const toggleDay = (n: number) =>
    setS((st) => ({
      ...st,
      schedule_days: st.schedule_days.includes(n)
        ? st.schedule_days.filter((d) => d !== n)
        : [...st.schedule_days, n].sort((a, b) => a - b),
    }))

  useEffect(() => {
    if (!open) return
    if (rule) {
      setS({
        ...empty,
        name: rule.name || '',
        description: rule.description || '',
        enabled: rule.enabled ?? true,
        conditions:
          Array.isArray(rule.conditions) && rule.conditions.length
            ? rule.conditions.map((c: any) => ({
                metric: c.metric,
                operator: c.operator,
                threshold: c.threshold ?? 0,
              }))
            : [{ metric: rule.metric || 'ping_status', operator: rule.operator || '==', threshold: rule.threshold ?? 0 }],
        condition_logic: rule.condition_logic === 'OR' ? 'OR' : 'AND',
        duration: rule.duration ?? 0,
        severity: rule.severity || 'warning',
        cooldown: rule.cooldown ?? 300,
        device_id: rule.device_id || '',
        group_id: rule.group_id || '',
        service_check_id: rule.service_check_id || '',
        service_check_group_id: rule.service_check_group_id || '',
        trap_oid: rule.trap_oid || '',
        target: rule.target || '',
        notify_channels: Array.isArray(rule.notify_channels) ? rule.notify_channels : [],
        schedule_start: rule.schedule_start || '',
        schedule_end: rule.schedule_end || '',
        schedule_days: Array.isArray(rule.schedule_days) ? rule.schedule_days : [],
        source:
          rule.metric === 'trap'
            ? 'trap'
            : String(rule.metric || '').startsWith('apm_')
            ? 'apm'
            : rule.service_check_id || rule.service_check_group_id || rule.metric === 'service_status'
            ? 'service'
            : 'device',
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

  const updateCond = (i: number, patch: Partial<Cond>) =>
    setS((st) => ({ ...st, conditions: st.conditions.map((c, idx) => (idx === i ? { ...c, ...patch } : c)) }))
  const addCond = () =>
    setS((st) => ({ ...st, conditions: [...st.conditions, { metric: 'rtt', operator: '>', threshold: 0 }] }))
  const removeCond = (i: number) =>
    setS((st) => ({
      ...st,
      conditions: st.conditions.length > 1 ? st.conditions.filter((_, idx) => idx !== i) : st.conditions,
    }))

  function submit(e: FormEvent) {
    e.preventDefault()
    const isService = s.source === 'service'
    const isTrap = s.source === 'trap'
    const isDevice = s.source === 'device'
    const isApm = s.source === 'apm'
    const first = s.conditions[0]
    // Device rules: send the full conditions array only when compound (>1);
    // a single condition stays in the legacy flat columns (conditions = null).
    const conditions = isDevice && s.conditions.length > 1 ? s.conditions : null
    save.mutate({
      name: s.name,
      description: s.description || null,
      enabled: s.enabled,
      metric: isService ? 'service_status' : isTrap ? 'trap' : first.metric,
      operator: isDevice || isApm ? first.operator : '==',
      threshold: isDevice || isApm ? first.threshold : 0,
      conditions,
      condition_logic: s.condition_logic,
      trap_oid: isTrap ? s.trap_oid.trim() || null : null,
      // target: interface filter for device interface metrics; APM service
      // name for APM rules (empty = every reporting service).
      target: isApm
        ? s.target.trim() || null
        : isDevice && INTERFACE_METRICS.has(first.metric)
        ? s.target.trim() || null
        : null,
      duration: s.duration,
      severity: s.severity,
      cooldown: s.cooldown,
      // Device + trap rules can scope to a device/group; service rules can't.
      device_id: isService ? null : s.device_id || null,
      group_id: isService ? null : s.group_id || null,
      service_check_id: isService ? s.service_check_id || null : null,
      service_check_group_id: isService ? s.service_check_group_id || null : null,
      notify_channels: s.notify_channels,
      schedule_start: s.schedule_start || null,
      schedule_end: s.schedule_end || null,
      schedule_days: s.schedule_days.length ? s.schedule_days : null,
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
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">
                {s.source === 'trap'
                  ? 'Trap match'
                  : s.source === 'device' && s.conditions.length > 1
                  ? 'Conditions'
                  : 'Condition'}
              </div>
              {s.source === 'device' && s.conditions.length > 1 && (
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted">Match</span>
                  <div className="flex rounded-md border border-border bg-surface2 p-0.5 text-xs">
                    {(['AND', 'OR'] as const).map((l) => (
                      <button
                        key={l}
                        type="button"
                        onClick={() => setS({ ...s, condition_logic: l })}
                        className={`rounded px-2.5 py-1 font-medium ${
                          s.condition_logic === l ? 'bg-primary text-white' : 'text-muted hover:text-text'
                        }`}
                      >
                        {l === 'AND' ? 'All (AND)' : 'Any (OR)'}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {s.source === 'service' ? (
              <div className="text-[11px] text-muted">
                Service-check rules fire on status transitions (set via the scope below); metric
                thresholds do not apply.
              </div>
            ) : s.source === 'trap' ? (
              <FormField
                label="Trap OID filter (optional)"
                hint="Empty = any trap. Otherwise an exact OID or dotted-prefix, e.g. 1.3.6.1.6.3.1.1.5.3 (linkDown). Fires an alert when a matching SNMP trap is received."
              >
                <Input
                  value={s.trap_oid}
                  onChange={(e) => setS({ ...s, trap_oid: e.target.value })}
                  placeholder="1.3.6.1.6.3.1.1.5.3"
                />
              </FormField>
            ) : (
              <div className="space-y-2">
                {s.conditions.map((c, i) => (
                  <div key={i} className="flex items-end gap-2">
                    <div className="grid flex-1 grid-cols-3 gap-2">
                      <FormField label={i === 0 ? 'Metric' : ' '}>
                        <Select value={c.metric} onValueChange={(v) => updateCond(i, { metric: v })}>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {s.source === 'apm' ? (
                              APM_METRICS.map((m) => (
                                <SelectItem key={m.value} value={m.value}>
                                  {m.label}
                                </SelectItem>
                              ))
                            ) : (
                              <>
                                <SelectItem value="ping_status">Ping status (0/1)</SelectItem>
                                <SelectItem value="rtt">Round-trip time (ms)</SelectItem>
                                <SelectItem value="packet_loss">Packet loss (%)</SelectItem>
                                <SelectItem value="jitter">Jitter (ms)</SelectItem>
                                {NETWORK_METRICS.map((m) => (
                                  <SelectItem key={m.value} value={m.value}>
                                    {m.label}
                                  </SelectItem>
                                ))}
                              </>
                            )}
                          </SelectContent>
                        </Select>
                      </FormField>
                      <FormField label={i === 0 ? 'Operator' : ' '}>
                        <Select value={c.operator} onValueChange={(v) => updateCond(i, { operator: v })}>
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
                      <FormField label={i === 0 ? 'Threshold' : ' '}>
                        <Input
                          type="number"
                          step="any"
                          value={c.threshold}
                          onChange={(e) => updateCond(i, { threshold: Number(e.target.value) })}
                        />
                      </FormField>
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-9 w-9 text-muted hover:text-danger"
                      disabled={s.conditions.length === 1}
                      onClick={() => removeCond(i)}
                      title="Remove condition"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
                {s.source !== 'apm' && (
                  <Button type="button" variant="outline" size="sm" onClick={addCond}>
                    <Plus className="h-3.5 w-3.5" />
                    Add condition
                  </Button>
                )}
                {s.conditions.length > 1 && (
                  <p className="text-[11px] text-muted">
                    Rule fires when{' '}
                    <span className="font-semibold text-text">
                      {s.condition_logic === 'AND' ? 'all' : 'any'}
                    </span>{' '}
                    of the {s.conditions.length} conditions match.
                  </p>
                )}
                {s.conditions.some((c) => INTERFACE_METRICS.has(c.metric)) && (
                  <FormField
                    label="Interface filter (optional)"
                    hint="Empty = all monitored interfaces on the in-scope device(s). Otherwise an interface name/description/alias substring (e.g. GigabitEthernet0/1, WAN) or an exact if_index."
                  >
                    <Input
                      value={s.target}
                      onChange={(e) => setS({ ...s, target: e.target.value })}
                      placeholder="All interfaces"
                    />
                  </FormField>
                )}
              </div>
            )}

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
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">Scope</div>
              <div className="flex rounded-md border border-border bg-surface2 p-0.5 text-xs">
                <button
                  type="button"
                  onClick={() =>
                    setS({
                      ...s,
                      source: 'device',
                      service_check_id: '',
                      service_check_group_id: '',
                      // Leaving APM scope: reset APM conditions/target to device defaults.
                      ...(s.source === 'apm'
                        ? { target: '', conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }] }
                        : {}),
                    })
                  }
                  className={`rounded px-2.5 py-1 font-medium ${
                    s.source === 'device' ? 'bg-primary text-white' : 'text-muted hover:text-text'
                  }`}
                >
                  Device
                </button>
                <button
                  type="button"
                  onClick={() => setS({ ...s, source: 'service', device_id: '', group_id: '' })}
                  className={`rounded px-2.5 py-1 font-medium ${
                    s.source === 'service' ? 'bg-primary text-white' : 'text-muted hover:text-text'
                  }`}
                >
                  Service check
                </button>
                <button
                  type="button"
                  onClick={() => setS({ ...s, source: 'trap', service_check_id: '', service_check_group_id: '' })}
                  className={`rounded px-2.5 py-1 font-medium ${
                    s.source === 'trap' ? 'bg-primary text-white' : 'text-muted hover:text-text'
                  }`}
                >
                  SNMP Trap
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setS({
                      ...s,
                      source: 'apm',
                      device_id: '',
                      group_id: '',
                      service_check_id: '',
                      service_check_group_id: '',
                      target: '',
                      conditions: [{ metric: 'apm_latency_p95', operator: '>', threshold: 800 }],
                    })
                  }
                  className={`rounded px-2.5 py-1 font-medium ${
                    s.source === 'apm' ? 'bg-primary text-white' : 'text-muted hover:text-text'
                  }`}
                >
                  APM
                </button>
              </div>
            </div>
            {s.source === 'apm' ? (
              <div className="grid grid-cols-1 gap-3">
                <FormField
                  label="Application service"
                  hint="Empty = every service reporting traces. Evaluated per service every minute against the RED rollups."
                >
                  <Select
                    value={s.target || '__all__'}
                    onValueChange={(v) => setS({ ...s, target: v === '__all__' ? '' : v })}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Any service" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__all__">Any service</SelectItem>
                      {apmServices.map((name) => (
                        <SelectItem key={name} value={name}>{name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormField>
              </div>
            ) : s.source !== 'service' ? (
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
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Service check">
                  <Select
                    value={s.service_check_id || '__all__'}
                    onValueChange={(v) =>
                      setS({
                        ...s,
                        service_check_id: v === '__all__' ? '' : v,
                        service_check_group_id: '',
                      })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Any service check" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__all__">Any service check</SelectItem>
                      {serviceChecks.slice(0, 100).map((c: any) => (
                        <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormField>
                <FormField label="Service group">
                  <Select
                    value={s.service_check_group_id || '__all__'}
                    onValueChange={(v) =>
                      setS({
                        ...s,
                        service_check_group_id: v === '__all__' ? '' : v,
                        service_check_id: '',
                      })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Any group" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__all__">Any group</SelectItem>
                      {serviceGroups.map((g: any) => (
                        <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormField>
                <div className="col-span-2 text-[11px] text-muted">
                  Rule fires on service check status transitions. Metric & threshold
                  below are ignored — trigger is set via the "Trigger on" control.
                </div>
              </div>
            )}
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

          {/* Notify channels — which channels this rule fires to */}
          <div className="space-y-3 rounded-md border border-border p-3">
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">Notify channels</div>
              <a href="/channels" className="text-[11px] text-primary hover:underline">Manage channels</a>
            </div>
            {channels.length === 0 ? (
              <p className="text-[11px] text-muted">
                No channels configured yet. Create one under{' '}
                <a href="/channels" className="text-primary hover:underline">Channels</a> to deliver notifications.
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-2">
                {channels.map((c) => {
                  const checked = s.notify_channels.includes(c.id)
                  return (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => toggleChannel(c.id)}
                      className={`flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                        checked ? 'border-primary bg-primary/10 text-text' : 'border-border text-muted hover:text-text'
                      } ${c.enabled === false ? 'opacity-50' : ''}`}
                    >
                      <span className="min-w-0 truncate">
                        <span className="font-medium">{c.name}</span>
                        <span className="ml-1 text-[10px] text-muted">
                          {CHANNEL_TYPE_LABEL[c.type] || c.type}{c.enabled === false ? ' · off' : ''}
                        </span>
                      </span>
                      {checked && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                    </button>
                  )
                })}
              </div>
            )}
            {channels.length > 0 && s.notify_channels.length === 0 && (
              <p className="text-[11px] text-warning">
                No channels selected — this rule will record alerts but won't send notifications.
              </p>
            )}
          </div>

          {/* Schedule — quiet hours; outside the window alerts are recorded but not sent */}
          <div className="space-y-3 rounded-md border border-border p-3">
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wider text-muted">Schedule (optional)</div>
              <span className="text-[11px] text-muted">Quiet hours — notify only in-window</span>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <FormField label="Start time">
                <Input
                  type="time"
                  value={s.schedule_start}
                  onChange={(e) => setS({ ...s, schedule_start: e.target.value })}
                />
              </FormField>
              <FormField label="End time">
                <Input
                  type="time"
                  value={s.schedule_end}
                  onChange={(e) => setS({ ...s, schedule_end: e.target.value })}
                />
              </FormField>
            </div>
            <div>
              <div className="mb-1.5 text-[11px] font-medium text-muted">Active days</div>
              <div className="flex flex-wrap gap-1.5">
                {DAY_LABELS.map((d) => {
                  const active = s.schedule_days.includes(d.n)
                  return (
                    <button
                      key={d.n}
                      type="button"
                      onClick={() => toggleDay(d.n)}
                      className={`h-8 w-11 rounded-md text-xs font-medium transition-colors ${
                        active ? 'bg-primary text-white' : 'bg-surface2 text-muted hover:text-text'
                      }`}
                    >
                      {d.label}
                    </button>
                  )
                })}
              </div>
              <p className="mt-1.5 text-[11px] text-muted">
                Leave times empty and no days selected to notify 24/7. Times use the appliance timezone.
                Recovery (“resolved”) notices are always sent.
              </p>
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
