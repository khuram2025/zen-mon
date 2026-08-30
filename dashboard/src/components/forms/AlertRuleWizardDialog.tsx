/**
 * SolarWinds-style alert rule wizard — properties, trigger, reset, scope,
 * trigger actions, reset actions, schedule, and review.
 */
import { FormEvent, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ChevronsUp,
  Clock,
  Copy,
  Eye,
  Loader2,
  Plus,
  RotateCcw,
  Settings2,
  Target,
  Trash2,
  Zap,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, copyText } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import { Badge } from '@/components/ui/Badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { useTags, tagColor, tagColorMap } from '@/hooks/useTags'

type Cond = { metric: string; operator: string; threshold: number }

// One SLA escalation tier: if the alert is still active and unacknowledged
// after_minutes after triggering, page notify_channels. repeat_every_minutes
// re-pages the tier on that cadence until ack/resolve (null = no repeat).
type EscLevel = { after_minutes: number; notify_channels: string[]; repeat_every_minutes: number | null }

type WizardState = {
  name: string
  description: string
  enabled: boolean
  severity: 'info' | 'warning' | 'critical'
  source: 'device' | 'service' | 'trap' | 'apm'
  conditions: Cond[]
  condition_logic: 'AND' | 'OR'
  trigger_on: 'any' | 'down' | 'up' | 'degraded'
  min_duration: number
  recovery_alert: boolean
  reset_mode: 'auto' | 'none'
  device_id: string
  group_id: string
  device_type: string
  location: string
  scope_tag: string
  service_check_id: string
  service_check_group_id: string
  trap_oid: string
  target: string
  notify_channels: string[]
  cooldown: number
  max_repeat: number
  escalation_levels: EscLevel[]
  schedule_start: string
  schedule_end: string
  schedule_days: number[]
  email_subject: string
  email_body: string
  sms_template: string
  recovery_email_subject: string
  recovery_email_body: string
  recovery_sms_template: string
}

const STEPS = [
  { id: 'properties', label: 'Properties', icon: Settings2 },
  { id: 'trigger', label: 'Trigger', icon: Zap },
  { id: 'reset', label: 'Reset', icon: RotateCcw },
  { id: 'scope', label: 'Scope', icon: Target },
  { id: 'actions', label: 'Trigger Actions', icon: Bell },
  { id: 'escalation', label: 'Escalation', icon: ChevronsUp },
  { id: 'reset_actions', label: 'Reset Actions', icon: CheckCircle2 },
  { id: 'schedule', label: 'Schedule', icon: Clock },
  { id: 'review', label: 'Review', icon: Eye },
] as const

type StepId = (typeof STEPS)[number]['id']

const DAY_LABELS = [
  { n: 1, label: 'Mon' }, { n: 2, label: 'Tue' }, { n: 3, label: 'Wed' },
  { n: 4, label: 'Thu' }, { n: 5, label: 'Fri' }, { n: 6, label: 'Sat' }, { n: 7, label: 'Sun' },
]

const CHANNEL_TYPE_LABEL: Record<string, string> = {
  email: 'Email', sms: 'SMS', webhook: 'Webhook', slack: 'Slack',
  telegram: 'Telegram', teams: 'Teams', discord: 'Discord', pagerduty: 'PagerDuty',
}

const NETWORK_METRICS = [
  { value: 'if_util_pct', label: 'Interface utilization (%)', iface: true },
  { value: 'if_in_bps', label: 'Interface inbound (bps)', iface: true },
  { value: 'if_out_bps', label: 'Interface outbound (bps)', iface: true },
  { value: 'if_errors', label: 'Interface errors', iface: true },
  { value: 'if_discards', label: 'Interface discards', iface: true },
  { value: 'if_oper_status', label: 'Interface down (oper=2)', iface: true },
  { value: 'cpu', label: 'Device CPU (%)', iface: false },
  { value: 'memory', label: 'Device memory (%)', iface: false },
  { value: 'temperature', label: 'Device temperature', iface: false },
  { value: 'session_count', label: 'Session count', iface: false },
  { value: 'uptime_reset', label: 'Reboot detected', iface: false },
] as const

const INTERFACE_METRICS = new Set(NETWORK_METRICS.filter((m) => m.iface).map((m) => m.value))

// Server APM metrics are evaluated per service; browser RUM metrics are
// evaluated per application ID. Rates are fractions in 0–1.
const APM_METRICS = [
  { value: 'apm_latency_p95', label: 'p95 latency (ms)' },
  { value: 'apm_latency_p99', label: 'p99 latency (ms)' },
  { value: 'apm_latency_p50', label: 'p50 latency (ms)' },
  { value: 'apm_error_rate', label: 'Error rate (0.02 = 2%)' },
  { value: 'apm_throughput', label: 'Throughput (req/s)' },
  { value: 'apm_apdex', label: 'Apdex (0–1)' },
  { value: 'apm_rum_lcp_p75', label: 'Browser RUM — LCP p75 (ms)' },
  { value: 'apm_rum_inp_p75', label: 'Browser RUM — INP p75 (ms)' },
  { value: 'apm_rum_cls_p75', label: 'Browser RUM — CLS p75' },
  { value: 'apm_rum_error_session_rate', label: 'Browser RUM — error-affected sessions (0.05 = 5%)' },
  { value: 'apm_rum_resource_failure_rate', label: 'Browser RUM — failed resources (0.05 = 5%)' },
] as const

// Ordered by how often a template needs them. The phrasing variables at the
// top render finished English ("Interface utilisation on core-router-01 rose
// above 80%"); the raw ones below are the rule's stored values.
const TEMPLATE_VARS = [
  '{event_sentence}', '{condition}', '{condition_sentence}', '{reading}',
  '{duration}', '{duration_sentence}', '{duration_suffix}',
  '{hostname}', '{ip_address}', '{status}', '{severity}', '{rule_name}',
  '{group}', '{location}', '{device_type}', '{timestamp}',
  '{metric_label}', '{threshold_value}', '{metric}', '{operator}', '{threshold}',
  '{rtt}', '{packet_loss}',
]

// Mirrors the defaults in server/app/services/alert_phrasing.py. Shown as
// placeholders only — an empty field is stored as NULL and rendered by the
// server, so these never go stale in the database.
const DEFAULTS = {
  email_subject: '[{severity}] {status}: {rule_name}',
  email_body: '{event_sentence}',
  sms_template: 'ZenPlus {severity} — {rule_name}: {event_sentence}',
  recovery_email_subject: '[{severity}] Resolved: {rule_name}',
  recovery_email_body: '{event_sentence}{duration_sentence}',
  recovery_sms_template: 'ZenPlus resolved — {rule_name}: {event_sentence}{duration_suffix}',
}

const DEFAULT_STATE: WizardState = {
  name: '',
  description: '',
  enabled: true,
  severity: 'critical',
  source: 'device',
  conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }],
  condition_logic: 'AND',
  trigger_on: 'down',
  min_duration: 0,
  recovery_alert: true,
  reset_mode: 'auto',
  device_id: '',
  group_id: '',
  device_type: '',
  location: '',
  scope_tag: '',
  service_check_id: '',
  service_check_group_id: '',
  trap_oid: '',
  target: '',
  notify_channels: [],
  cooldown: 300,
  max_repeat: 0,
  escalation_levels: [],
  schedule_start: '',
  schedule_end: '',
  schedule_days: [],
  // Blank means "use the built-in default", which the server writes in the
  // house style and keeps in step with the recovery/duration phrasing. Seeding
  // these with a field dump baked a copy of it onto every rule ever created,
  // and a stored template never picks up later improvements.
  email_subject: '',
  email_body: '',
  sms_template: '',
  recovery_email_subject: '',
  recovery_email_body: '',
  recovery_sms_template: '',
}

function ruleToState(rule: any): WizardState {
  const source =
    rule.metric === 'trap'
      ? 'trap'
      : String(rule.metric || '').startsWith('apm_')
      ? 'apm'
      : rule.service_check_id || rule.service_check_group_id || rule.metric === 'service_status'
      ? 'service'
      : 'device'
  return {
    ...DEFAULT_STATE,
    name: rule.name || '',
    description: rule.description || '',
    enabled: rule.enabled ?? true,
    severity: rule.severity || 'warning',
    source,
    conditions:
      Array.isArray(rule.conditions) && rule.conditions.length
        ? rule.conditions.map((c: any) => ({
            metric: c.metric,
            operator: c.operator,
            threshold: c.threshold ?? 0,
          }))
        : [{ metric: rule.metric || 'ping_status', operator: rule.operator || '==', threshold: rule.threshold ?? 0 }],
    condition_logic: rule.condition_logic === 'OR' ? 'OR' : 'AND',
    trigger_on: rule.trigger_on || 'down',
    min_duration: rule.min_duration ?? rule.duration ?? 0,
    recovery_alert: rule.recovery_alert ?? false,
    reset_mode: rule.recovery_alert ? 'auto' : 'none',
    device_id: rule.device_id || '',
    group_id: rule.group_id || '',
    device_type: rule.device_type || '',
    location: rule.location || '',
    scope_tag: rule.scope_tag || '',
    service_check_id: rule.service_check_id || '',
    service_check_group_id: rule.service_check_group_id || '',
    trap_oid: rule.trap_oid || '',
    target: rule.target || '',
    notify_channels: Array.isArray(rule.notify_channels) ? rule.notify_channels : [],
    cooldown: rule.cooldown ?? 300,
    max_repeat: rule.max_repeat ?? 0,
    escalation_levels: Array.isArray(rule.escalation_levels)
      ? rule.escalation_levels.map((lv: any) => ({
          after_minutes: lv.after_minutes ?? 15,
          notify_channels: Array.isArray(lv.notify_channels) ? lv.notify_channels : [],
          repeat_every_minutes: lv.repeat_every_minutes ?? null,
        }))
      : [],
    schedule_start: rule.schedule_start || '',
    schedule_end: rule.schedule_end || '',
    schedule_days: Array.isArray(rule.schedule_days) ? rule.schedule_days : [],
    email_subject: rule.email_subject || '',
    email_body: rule.email_body || '',
    sms_template: rule.sms_template || '',
    recovery_email_subject: rule.recovery_email_subject || DEFAULT_STATE.recovery_email_subject,
    recovery_email_body: rule.recovery_email_body || '',
    recovery_sms_template: rule.recovery_sms_template || '',
  }
}

function stateToPayload(s: WizardState) {
  const isService = s.source === 'service'
  const isTrap = s.source === 'trap'
  const isDevice = s.source === 'device'
  const isApm = s.source === 'apm'
  const first = s.conditions[0]
  const conditions = isDevice && s.conditions.length > 1 ? s.conditions : null
  const recovery = s.reset_mode === 'auto'

  return {
    name: s.name,
    description: s.description || null,
    enabled: s.enabled,
    metric: isService ? 'service_status' : isTrap ? 'trap' : first.metric,
    operator: isDevice || isApm ? first.operator : '==',
    threshold: isDevice || isApm ? first.threshold : 0,
    conditions,
    condition_logic: s.condition_logic,
    trigger_on: isService ? s.trigger_on : isDevice && first.metric === 'ping_status' ? s.trigger_on : 'any',
    recovery_alert: recovery,
    trap_oid: isTrap ? s.trap_oid.trim() || null : null,
    // target: interface filter for device interface metrics; APM service name
    // for APM rules (empty = every reporting service).
    target: isApm
      ? s.target.trim() || null
      : isDevice && s.conditions.some((c) => INTERFACE_METRICS.has(c.metric))
      ? s.target.trim() || null
      : null,
    min_duration: s.min_duration,
    severity: s.severity,
    cooldown: s.cooldown,
    max_repeat: s.max_repeat,
    device_id: isService || isApm ? null : s.device_id || null,
    group_id: isService || isApm ? null : s.group_id || null,
    device_type: isService || isApm ? null : s.device_type || null,
    location: isService || isApm ? null : s.location || null,
    scope_tag: isService || isApm ? null : s.scope_tag || null,
    service_check_id: isService ? s.service_check_id || null : null,
    service_check_group_id: isService ? s.service_check_group_id || null : null,
    notify_channels: s.notify_channels,
    escalation_levels: s.escalation_levels.length
      ? s.escalation_levels.map((lv) => ({
          after_minutes: lv.after_minutes,
          notify_channels: lv.notify_channels,
          repeat_every_minutes: lv.repeat_every_minutes || null,
        }))
      : null,
    schedule_start: s.schedule_start || null,
    schedule_end: s.schedule_end || null,
    schedule_days: s.schedule_days.length ? s.schedule_days : null,
    email_subject: s.email_subject || null,
    email_body: s.email_body || null,
    sms_template: s.sms_template || null,
    recovery_email_subject: recovery ? s.recovery_email_subject || null : null,
    recovery_email_body: recovery ? s.recovery_email_body || null : null,
    recovery_sms_template: recovery ? s.recovery_sms_template || null : null,
  }
}

export function AlertRuleWizardDialog({
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
  const [step, setStep] = useState<StepId>('properties')
  const [s, setS] = useState<WizardState>(DEFAULT_STATE)
  const [scopeMode, setScopeMode] = useState<'all' | 'device' | 'group' | 'type' | 'location' | 'tag'>('all')
  const [preview, setPreview] = useState<any>(null)
  const isRumMetric = s.source === 'apm' && s.conditions[0]?.metric.startsWith('apm_rum_')

  const stepIdx = STEPS.findIndex((st) => st.id === step)

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

  const { data: locations = [] } = useQuery<string[]>({
    queryKey: ['devices', 'locations'],
    queryFn: async () => (await api.get('/devices/locations')).data,
    enabled: open,
  })

  const { data: tags = [] } = useTags(open)
  const tagColors = useMemo(() => tagColorMap(tags), [tags])

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
  const channels: any[] = Array.isArray(channelsResp) ? channelsResp : channelsResp?.data || []

  const { data: apmServicesResp } = useQuery<any>({
    queryKey: ['apm', 'services', 'list-min'],
    queryFn: async () => (await api.get('/apm/services?range=24h')).data,
    enabled: open && s.source === 'apm',
  })
  const apmServices: string[] = (apmServicesResp?.services || []).map((x: any) => x.name)
  const { data: rumScopeRows } = useQuery<any>({
    queryKey: ['apm', 'rum', 'views', 'alert-scope'],
    queryFn: async () => (await api.get('/apm/rum/views?range=24h&page=1&page_size=100&sort=last_seen&order=desc')).data,
    enabled: open && isRumMetric,
  })
  const rumApplications: string[] = Array.from(new Set(
    (rumScopeRows?.items || []).map((item: any) => `${item.application_id} @ ${item.env || 'unknown'}`),
  ))

  useEffect(() => {
    if (!open) return
    setStep('properties')
    setPreview(null)
    if (rule) {
      const st = ruleToState(rule)
      setS(st)
      setScopeMode(
        st.group_id ? 'group' : st.device_type ? 'type' : st.location ? 'location'
          : st.scope_tag ? 'tag' : st.device_id ? 'device' : 'all',
      )
    } else {
      setS(DEFAULT_STATE)
      setScopeMode('all')
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

  const previewMut = useMutation({
    mutationFn: async () => {
      if (isEdit) return (await api.post(`/alert-rules/${rule.id}/preview`)).data
      // No id yet: preview via a disabled throwaway rule, deleted even when
      // the preview call fails (it used to leak into the rules list).
      const created = (await api.post('/alert-rules', { ...stateToPayload(s), enabled: false })).data
      try {
        return (await api.post(`/alert-rules/${created.id}/preview`)).data
      } finally {
        await api.delete(`/alert-rules/${created.id}`).catch(() => {})
      }
    },
    onSuccess: (data) => setPreview(data),
    onError: (e: any) => toast.error('Preview failed', apiErrorMessage(e)),
  })

  const simulateMut = useMutation({
    mutationFn: async () => (await api.post(`/alert-rules/${rule.id}/simulate`)).data,
    onSuccess: (data: any) => {
      const details = (data.results || []).map((r: any) => `${r.channel}: ${r.status}`).join(', ')
      toast.success(data.message || 'Test sent', details)
    },
    onError: (e: any) => toast.error('Simulate failed', apiErrorMessage(e)),
  })

  const updateCond = (i: number, patch: Partial<Cond>) =>
    setS((st) => ({ ...st, conditions: st.conditions.map((c, idx) => (idx === i ? { ...c, ...patch } : c)) }))
  const addCond = () =>
    setS((st) => ({ ...st, conditions: [...st.conditions, { metric: 'rtt', operator: '>', threshold: 100 }] }))
  const removeCond = (i: number) =>
    setS((st) => ({
      ...st,
      conditions: st.conditions.length > 1 ? st.conditions.filter((_, idx) => idx !== i) : st.conditions,
    }))
  const toggleChannel = (id: string) =>
    setS((st) => ({
      ...st,
      notify_channels: st.notify_channels.includes(id)
        ? st.notify_channels.filter((c) => c !== id)
        : [...st.notify_channels, id],
    }))
  const updateEsc = (i: number, patch: Partial<EscLevel>) =>
    setS((st) => ({
      ...st,
      escalation_levels: st.escalation_levels.map((lv, idx) => (idx === i ? { ...lv, ...patch } : lv)),
    }))
  const toggleEscChannel = (i: number, id: string) =>
    setS((st) => ({
      ...st,
      escalation_levels: st.escalation_levels.map((lv, idx) =>
        idx === i
          ? {
              ...lv,
              notify_channels: lv.notify_channels.includes(id)
                ? lv.notify_channels.filter((c) => c !== id)
                : [...lv.notify_channels, id],
            }
          : lv,
      ),
    }))
  const addEscLevel = () =>
    setS((st) => {
      const last = st.escalation_levels[st.escalation_levels.length - 1]
      return {
        ...st,
        escalation_levels: [
          ...st.escalation_levels,
          { after_minutes: last ? last.after_minutes * 2 : 15, notify_channels: [], repeat_every_minutes: null },
        ],
      }
    })
  const removeEscLevel = (i: number) =>
    setS((st) => ({ ...st, escalation_levels: st.escalation_levels.filter((_, idx) => idx !== i) }))
  const toggleDay = (n: number) =>
    setS((st) => ({
      ...st,
      schedule_days: st.schedule_days.includes(n)
        ? st.schedule_days.filter((d) => d !== n)
        : [...st.schedule_days, n].sort((a, b) => a - b),
    }))

  const copyTriggerToReset = () => {
    setS((st) => ({
      ...st,
      recovery_email_subject: st.recovery_email_subject || `RESOLVED: ${st.email_subject}`,
      recovery_email_body: st.recovery_email_body || st.email_body,
      recovery_sms_template: st.recovery_sms_template || st.sms_template,
    }))
    toast.success('Trigger templates copied to reset actions')
  }

  const summary = useMemo(() => {
    const first = s.conditions[0]
    const condText =
      s.source === 'trap'
        ? s.trap_oid ? `Trap OID prefix ${s.trap_oid}` : 'Any SNMP trap'
        : s.source === 'service'
        ? `Service status → ${s.trigger_on}`
        : s.conditions.length > 1
        ? s.conditions.map((c) => `${c.metric} ${c.operator} ${c.threshold}`).join(` ${s.condition_logic} `)
        : `${first.metric} ${first.operator} ${first.threshold}`
    return { condText }
  }, [s])

  function submit(e?: FormEvent) {
    e?.preventDefault()
    if (!s.name.trim()) {
      toast.error('Name required')
      setStep('properties')
      return
    }
    // A scope mode was chosen but nothing selected — saving now would silently
    // apply the rule to every device, which is never what was meant.
    const scopeEmpty =
      (scopeMode === 'device' && !s.device_id) ||
      (scopeMode === 'group' && !s.group_id) ||
      (scopeMode === 'type' && !s.device_type) ||
      (scopeMode === 'location' && !s.location) ||
      (scopeMode === 'tag' && !s.scope_tag)
    if (!isService && s.source !== 'apm' && scopeEmpty) {
      toast.error('Scope incomplete', `Select a ${scopeMode === 'type' ? 'device type' : scopeMode}, or choose "All devices".`)
      setStep('scope')
      return
    }
    for (let i = 0; i < s.escalation_levels.length; i++) {
      const lv = s.escalation_levels[i]
      if (!lv.notify_channels.length) {
        toast.error(`Escalation level ${i + 1} has no channels`, 'Pick at least one channel or remove the level.')
        setStep('escalation')
        return
      }
      if (!(lv.after_minutes >= 1)) {
        toast.error(`Escalation level ${i + 1} needs a delay of at least 1 minute`)
        setStep('escalation')
        return
      }
      if (i > 0 && lv.after_minutes <= s.escalation_levels[i - 1].after_minutes) {
        toast.error('Escalation delays must increase', `Level ${i + 1} must fire later than level ${i}.`)
        setStep('escalation')
        return
      }
    }
    save.mutate(stateToPayload(s))
  }
  const isService = s.source === 'service'

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={() => onOpenChange(false)} />
      <div className="relative ml-auto flex h-full w-full max-w-3xl flex-col border-l border-border bg-surface shadow-2xl">
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold">{isEdit ? 'Edit Alert' : 'Create Alert'}</h2>
            <p className="text-xs text-muted">SolarWinds-style alert wizard — step {stepIdx + 1} of {STEPS.length}</p>
          </div>
          <button type="button" onClick={() => onOpenChange(false)} className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-text">
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Step nav */}
        <div className="shrink-0 overflow-x-auto border-b border-border bg-surface2/40 px-3 py-2">
          <div className="flex min-w-max gap-1">
            {STEPS.map((st, i) => {
              const Icon = st.icon
              const active = step === st.id
              const done = i < stepIdx
              return (
                <button
                  key={st.id}
                  type="button"
                  onClick={() => setStep(st.id)}
                  className={cn(
                    'flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-medium transition-colors',
                    active ? 'bg-primary text-white' : done ? 'text-primary hover:bg-primary/10' : 'text-muted hover:bg-surface2 hover:text-text',
                  )}
                >
                  <Icon className="h-3.5 w-3.5" />
                  {st.label}
                </button>
              )
            })}
          </div>
        </div>

        {/* Body */}
        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {step === 'properties' && (
              <div className="space-y-4">
                <SectionTitle title="Alert Properties" hint="Name, severity, and enable/disable — like SolarWinds alert definition properties." />
                <FormField label="Alert name" required>
                  <Input required value={s.name} onChange={(e) => setS({ ...s, name: e.target.value })} placeholder="Email me when a core router goes down" />
                </FormField>
                <FormField label="Description">
                  <Textarea value={s.description} onChange={(e) => setS({ ...s, description: e.target.value })} placeholder="Optional description for operators" rows={2} />
                </FormField>
                <FormField label="Severity">
                  <div className="flex gap-2">
                    {(['info', 'warning', 'critical'] as const).map((sev) => (
                      <button
                        key={sev}
                        type="button"
                        onClick={() => setS({ ...s, severity: sev })}
                        className={cn(
                          'flex-1 rounded-lg border px-3 py-2 text-xs font-semibold capitalize',
                          s.severity === sev ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted hover:text-text',
                        )}
                      >
                        {sev}
                      </button>
                    ))}
                  </div>
                </FormField>
                <div className="flex items-center justify-between rounded-lg border border-border px-4 py-3">
                  <div>
                    <div className="text-sm font-medium">Enabled</div>
                    <div className="text-xs text-muted">Rule evaluates immediately when enabled</div>
                  </div>
                  <Switch checked={s.enabled} onCheckedChange={(v) => setS({ ...s, enabled: v })} />
                </div>
              </div>
            )}

            {step === 'trigger' && (
              <div className="space-y-4">
                <SectionTitle title="Trigger Conditions" hint="Define when this alert fires. Add multiple child conditions with AND/OR logic." />
                <div className="flex rounded-lg border border-border bg-surface2 p-1 text-xs">
                  {(['device', 'service', 'trap', 'apm'] as const).map((src) => (
                    <button
                      key={src}
                      type="button"
                      onClick={() => {
                        if (src === s.source) return
                        // Entering/leaving APM swaps the condition metric domain,
                        // and `target` changes meaning (service name vs interface).
                        if (src === 'apm') {
                          setS({ ...s, source: src, target: '', conditions: [{ metric: 'apm_latency_p95', operator: '>', threshold: 800 }] })
                        } else if (s.source === 'apm') {
                          setS({ ...s, source: src, target: '', conditions: [{ metric: 'ping_status', operator: '==', threshold: 0 }] })
                        } else {
                          setS({ ...s, source: src })
                        }
                      }}
                      className={cn('flex-1 rounded-md py-2 font-medium', s.source === src ? 'bg-primary text-white' : 'text-muted')}
                    >
                      {src === 'device' ? 'Device / SNMP' : src === 'service' ? 'Service Check' : src === 'trap' ? 'SNMP Trap' : 'APM & RUM'}
                    </button>
                  ))}
                </div>

                {s.source === 'service' && (
                  <FormField label="Trigger on status" hint="Fires when a service check transitions to this state">
                    <Select value={s.trigger_on} onValueChange={(v: any) => setS({ ...s, trigger_on: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="down">Service goes down</SelectItem>
                        <SelectItem value="up">Service comes up</SelectItem>
                        <SelectItem value="degraded">Service degraded</SelectItem>
                        <SelectItem value="any">Any status change</SelectItem>
                      </SelectContent>
                    </Select>
                  </FormField>
                )}

                {s.source === 'trap' && (
                  <FormField label="Trap OID filter (optional)" hint="Empty = any trap. Prefix match supported.">
                    <Input value={s.trap_oid} onChange={(e) => setS({ ...s, trap_oid: e.target.value })} placeholder="1.3.6.1.6.3.1.1.5.3" />
                  </FormField>
                )}

                {s.source === 'device' && (
                  <>
                    {s.conditions.some((c) => c.metric === 'ping_status') && (
                      <FormField label="Status transition" hint="For ping/status rules — when device changes state">
                        <Select value={s.trigger_on} onValueChange={(v: any) => setS({ ...s, trigger_on: v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="down">Device goes down</SelectItem>
                            <SelectItem value="up">Device comes up</SelectItem>
                            <SelectItem value="degraded">Device degraded</SelectItem>
                            <SelectItem value="any">Any status change</SelectItem>
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-muted">Child conditions</span>
                      {s.conditions.length > 1 && (
                        <div className="flex rounded-md border border-border bg-surface2 p-0.5 text-xs">
                          {(['AND', 'OR'] as const).map((l) => (
                            <button key={l} type="button" onClick={() => setS({ ...s, condition_logic: l })}
                              className={cn('rounded px-2.5 py-1 font-medium', s.condition_logic === l ? 'bg-primary text-white' : 'text-muted')}>
                              {l === 'AND' ? 'All (AND)' : 'Any (OR)'}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    {s.conditions.map((c, i) => (
                      <div key={i} className="flex items-end gap-2 rounded-lg border border-border/60 bg-surface2/30 p-3">
                        <div className="grid flex-1 grid-cols-3 gap-2">
                          <FormField label={i === 0 ? 'Field' : ' '}>
                            <Select value={c.metric} onValueChange={(v) => updateCond(i, { metric: v })}>
                              <SelectTrigger><SelectValue /></SelectTrigger>
                              <SelectContent>
                                <SelectItem value="ping_status">Ping status</SelectItem>
                                <SelectItem value="rtt">RTT (ms)</SelectItem>
                                <SelectItem value="packet_loss">Packet loss (%)</SelectItem>
                                <SelectItem value="jitter">Jitter (ms)</SelectItem>
                                {NETWORK_METRICS.map((m) => (
                                  <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </FormField>
                          <FormField label={i === 0 ? 'Operator' : ' '}>
                            <Select value={c.operator} onValueChange={(v) => updateCond(i, { operator: v })}>
                              <SelectTrigger><SelectValue /></SelectTrigger>
                              <SelectContent>
                                {['>', '>=', '<', '<=', '==', '!='].map((op) => (
                                  <SelectItem key={op} value={op}>{op}</SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </FormField>
                          <FormField label={i === 0 ? 'Value' : ' '}>
                            <Input type="number" step="any" value={c.threshold} onChange={(e) => updateCond(i, { threshold: Number(e.target.value) })} />
                          </FormField>
                        </div>
                        <Button type="button" variant="ghost" size="icon" className="h-9 w-9 text-muted hover:text-danger" disabled={s.conditions.length === 1} onClick={() => removeCond(i)}>
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    ))}
                    <Button type="button" variant="outline" size="sm" onClick={addCond}><Plus className="h-3.5 w-3.5" />Add condition</Button>
                    {s.conditions.some((c) => INTERFACE_METRICS.has(c.metric)) && (
                      <FormField label="Interface filter" hint="Name substring or if_index. Empty = all interfaces.">
                        <Input value={s.target} onChange={(e) => setS({ ...s, target: e.target.value })} placeholder="GigabitEthernet0/1" />
                      </FormField>
                    )}
                  </>
                )}

                {s.source === 'apm' && (
                  <div className="flex items-end gap-2 rounded-lg border border-border/60 bg-surface2/30 p-3">
                    <div className="grid flex-1 grid-cols-3 gap-2">
                      <FormField label="Metric">
                        <Select value={s.conditions[0]?.metric} onValueChange={(v) => {
                          const switchesDataPlane = v.startsWith('apm_rum_') !== isRumMetric
                          setS({
                            ...s,
                            target: switchesDataPlane ? '' : s.target,
                            conditions: s.conditions.map((condition, index) => index === 0 ? { ...condition, metric: v } : condition),
                          })
                        }}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {APM_METRICS.map((m) => (
                              <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                      <FormField label="Operator">
                        <Select value={s.conditions[0]?.operator} onValueChange={(v) => updateCond(0, { operator: v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {['>', '>=', '<', '<=', '==', '!='].map((op) => (
                              <SelectItem key={op} value={op}>{op}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                      <FormField label="Threshold">
                        <Input type="number" step="any" value={s.conditions[0]?.threshold} onChange={(e) => updateCond(0, { threshold: Number(e.target.value) })} />
                      </FormField>
                    </div>
                  </div>
                )}

                <FormField label="Condition must exist for" hint="Hold time before firing — prevents flapping alerts (SolarWinds 'condition must exist for more than').">
                  <div className="flex items-center gap-3">
                    <Input type="number" min={0} value={s.min_duration} onChange={(e) => setS({ ...s, min_duration: Number(e.target.value) })} className="w-28" />
                    <span className="text-sm text-muted">seconds (0 = fire immediately)</span>
                  </div>
                </FormField>
              </div>
            )}

            {step === 'reset' && (
              <div className="space-y-4">
                <SectionTitle title="Reset Conditions" hint="When should this alert clear from Active Alerts and optionally notify recovery?" />
                <div className="space-y-2">
                  {[
                    { id: 'auto' as const, title: 'Reset when trigger condition is no longer true', desc: 'Recommended — alert auto-resolves when metrics recover or device comes back up.' },
                    { id: 'none' as const, title: 'No reset notification', desc: 'Alert still resolves in the system but no recovery message is sent.' },
                  ].map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() => setS({ ...s, reset_mode: opt.id, recovery_alert: opt.id === 'auto' })}
                      className={cn(
                        'w-full rounded-lg border p-4 text-left transition-colors',
                        s.reset_mode === opt.id ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
                      )}
                    >
                      <div className="flex items-center gap-2">
                        {s.reset_mode === opt.id && <Check className="h-4 w-4 text-primary" />}
                        <span className="text-sm font-semibold">{opt.title}</span>
                      </div>
                      <p className="mt-1 pl-6 text-xs text-muted">{opt.desc}</p>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {step === 'scope' && (
              <div className="space-y-4">
                <SectionTitle title="Scope" hint="Which objects this alert monitors." />
                {s.source === 'apm' ? (
                  <FormField
                    label={isRumMetric ? 'Browser application / environment' : 'Application service'}
                    hint={isRumMetric
                      ? 'Empty = every application and environment reporting browser events. Each environment is evaluated independently once per minute.'
                      : 'Empty = every service reporting traces. Evaluated per service every minute against the RED rollups.'}
                  >
                    <Select
                      value={s.target || '__all__'}
                      onValueChange={(v) => setS({ ...s, target: v === '__all__' ? '' : v })}
                    >
                      <SelectTrigger><SelectValue placeholder={isRumMetric ? 'All browser app environments' : 'All services'} /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__all__">{isRumMetric ? 'All browser app environments' : 'All services'}</SelectItem>
                        {(isRumMetric ? rumApplications : apmServices).map((name) => (
                          <SelectItem key={name} value={name}>{name}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormField>
                ) : s.source === 'service' ? (
                  <div className="grid grid-cols-2 gap-3">
                    <FormField label="Service check">
                      <Select value={s.service_check_id || '__all__'} onValueChange={(v) => setS({ ...s, service_check_id: v === '__all__' ? '' : v, service_check_group_id: '' })}>
                        <SelectTrigger><SelectValue placeholder="Any" /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__all__">Any service check</SelectItem>
                          {serviceChecks.map((c: any) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </FormField>
                    <FormField label="Service group">
                      <Select value={s.service_check_group_id || '__all__'} onValueChange={(v) => setS({ ...s, service_check_group_id: v === '__all__' ? '' : v, service_check_id: '' })}>
                        <SelectTrigger><SelectValue placeholder="Any" /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__all__">Any group</SelectItem>
                          {serviceGroups.map((g: any) => <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </FormField>
                  </div>
                ) : (
                  <>
                    <FormField label="Apply to">
                      <Select value={scopeMode} onValueChange={(v: any) => {
                        setScopeMode(v)
                        setS({ ...s, device_id: '', group_id: '', device_type: '', location: '', scope_tag: '' })
                      }}>
                        <SelectTrigger><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="all">All devices</SelectItem>
                          <SelectItem value="device">Specific device</SelectItem>
                          <SelectItem value="group">Device group</SelectItem>
                          <SelectItem value="type">Device type</SelectItem>
                          <SelectItem value="location">Location</SelectItem>
                          <SelectItem value="tag">Tag</SelectItem>
                        </SelectContent>
                      </Select>
                    </FormField>
                    {scopeMode === 'device' && (
                      <FormField label="Device">
                        <Select value={s.device_id || '__pick__'} onValueChange={(v) => setS({ ...s, device_id: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue placeholder="Select device" /></SelectTrigger>
                          <SelectContent>
                            {devices.map((d: any) => <SelectItem key={d.id} value={d.id}>{d.hostname}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'group' && (
                      <FormField label="Group">
                        <Select value={s.group_id || '__pick__'} onValueChange={(v) => setS({ ...s, group_id: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {(groups || []).map((g: any) => <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'type' && (
                      <FormField label="Device type">
                        <Select value={s.device_type || '__pick__'} onValueChange={(v) => setS({ ...s, device_type: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {['router', 'switch', 'firewall', 'server', 'access_point', 'printer', 'other'].map((t) => (
                              <SelectItem key={t} value={t}>{t.replace('_', ' ')}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'location' && (
                      <FormField label="Location">
                        <Select value={s.location || '__pick__'} onValueChange={(v) => setS({ ...s, location: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {locations.map((l) => <SelectItem key={l} value={l}>{l}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                    {scopeMode === 'tag' && (
                      <FormField
                        label="Tag"
                        hint="Applies to every device carrying this tag — devices tagged later are covered automatically."
                      >
                        <Select value={s.scope_tag || '__pick__'} onValueChange={(v) => setS({ ...s, scope_tag: v === '__pick__' ? '' : v })}>
                          <SelectTrigger><SelectValue placeholder="Select tag" /></SelectTrigger>
                          <SelectContent>
                            {tags.map((t) => (
                              <SelectItem key={t.id} value={t.name}>
                                <span className="inline-flex items-center gap-1.5">
                                  <span
                                    className="h-2 w-2 shrink-0 rounded-full"
                                    style={{ background: tagColor(t.name, tagColors) }}
                                  />
                                  {t.name}
                                </span>
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </FormField>
                    )}
                  </>
                )}
              </div>
            )}

            {step === 'actions' && (
              <div className="space-y-4">
                <SectionTitle title="Trigger Actions" hint="Notifications sent when the alert fires. Select channels and configure anti-flap settings." />
                <div className="grid grid-cols-2 gap-2">
                  {channels.length === 0 ? (
                    <p className="col-span-2 text-xs text-muted">No channels — <a href="/channels" className="text-primary hover:underline">create one</a></p>
                  ) : channels.map((c) => {
                    const checked = s.notify_channels.includes(c.id)
                    return (
                      <button key={c.id} type="button" onClick={() => toggleChannel(c.id)}
                        className={cn('flex items-center justify-between rounded-lg border px-3 py-2.5 text-left text-xs', checked ? 'border-primary bg-primary/10' : 'border-border')}>
                        <span><span className="font-medium">{c.name}</span> <span className="text-muted">{CHANNEL_TYPE_LABEL[c.type]}</span></span>
                        {checked && <Check className="h-3.5 w-3.5 text-primary" />}
                      </button>
                    )
                  })}
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <FormField label="Cooldown (seconds)" hint="Anti-flap: after a notification goes out, repeat triggers for the same object stay quiet this long">
                    <Input type="number" min={0} value={s.cooldown} onChange={(e) => setS({ ...s, cooldown: Number(e.target.value) })} />
                  </FormField>
                  <FormField label="Max escalation repeats" hint="Cap on repeat pages from an escalation level (0 = unlimited)">
                    <Input type="number" min={0} value={s.max_repeat} onChange={(e) => setS({ ...s, max_repeat: Number(e.target.value) })} />
                  </FormField>
                </div>
                <TemplateVars />
                <p className="text-[11px] text-muted">
                  Leave a field empty to use the built-in wording, which names the metric and
                  threshold in plain English. Open <span className="text-text">Preview message</span> on
                  the rule to see exactly what is sent.
                </p>
                <FormField label="Email subject">
                  <Input value={s.email_subject} onChange={(e) => setS({ ...s, email_subject: e.target.value })} className="font-mono text-xs" placeholder={DEFAULTS.email_subject} />
                </FormField>
                <FormField label="Email body">
                  <Textarea value={s.email_body} onChange={(e) => setS({ ...s, email_body: e.target.value })} rows={5} className="font-mono text-xs" placeholder={DEFAULTS.email_body} />
                </FormField>
                <FormField label="SMS template">
                  <Textarea value={s.sms_template} onChange={(e) => setS({ ...s, sms_template: e.target.value })} rows={2} className="font-mono text-xs" placeholder={DEFAULTS.sms_template} />
                </FormField>
              </div>
            )}

            {step === 'escalation' && (
              <div className="space-y-4">
                <SectionTitle
                  title="Escalation Policy"
                  hint="SLA tiers: if the alert stays active and nobody acknowledges it, page the next level. Acknowledging the alert stops escalation; resolving sends an all-clear to every level that was paged."
                />
                <div className="rounded-lg border border-border bg-surface2/40 px-4 py-3 text-xs">
                  <span className="font-semibold text-text">Immediately on trigger:</span>{' '}
                  <span className="text-muted">
                    {s.notify_channels.length
                      ? `${s.notify_channels.length} trigger action channel${s.notify_channels.length > 1 ? 's' : ''} (previous step)`
                      : 'no channels selected on the Trigger Actions step'}
                  </span>
                </div>
                {s.escalation_levels.map((lv, i) => (
                  <div key={i} className="space-y-3 rounded-lg border border-border/60 bg-surface2/30 p-4">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold uppercase tracking-wider text-primary">
                        Escalation level {i + 1}
                      </span>
                      <Button type="button" variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                        onClick={() => removeEscLevel(i)}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                    <div className="flex items-center gap-2 text-sm">
                      <span className="text-muted">If still unacknowledged after</span>
                      <Input type="number" min={1} className="w-24" value={lv.after_minutes}
                        onChange={(e) => updateEsc(i, { after_minutes: Number(e.target.value) })} />
                      <span className="text-muted">minutes, notify:</span>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {channels.length === 0 ? (
                        <p className="col-span-2 text-xs text-muted">
                          No channels — <a href="/channels" className="text-primary hover:underline">create one</a>
                        </p>
                      ) : channels.map((c) => {
                        const checked = lv.notify_channels.includes(c.id)
                        return (
                          <button key={c.id} type="button" onClick={() => toggleEscChannel(i, c.id)}
                            className={cn('flex items-center justify-between rounded-lg border px-3 py-2 text-left text-xs', checked ? 'border-primary bg-primary/10' : 'border-border')}>
                            <span><span className="font-medium">{c.name}</span> <span className="text-muted">{CHANNEL_TYPE_LABEL[c.type]}</span></span>
                            {checked && <Check className="h-3.5 w-3.5 text-primary" />}
                          </button>
                        )
                      })}
                    </div>
                    <div className="flex items-center gap-2 text-xs">
                      <Switch
                        checked={!!lv.repeat_every_minutes}
                        onCheckedChange={(v) => updateEsc(i, { repeat_every_minutes: v ? Math.max(lv.after_minutes, 15) : null })}
                      />
                      <span className="text-muted">Repeat this level every</span>
                      <Input type="number" min={1} className="w-20" disabled={!lv.repeat_every_minutes}
                        value={lv.repeat_every_minutes ?? ''}
                        onChange={(e) => updateEsc(i, { repeat_every_minutes: Number(e.target.value) || null })} />
                      <span className="text-muted">minutes until acknowledged or resolved</span>
                    </div>
                  </div>
                ))}
                {s.escalation_levels.length < 4 && (
                  <Button type="button" variant="outline" size="sm" onClick={addEscLevel}>
                    <Plus className="h-3.5 w-3.5" />Add escalation level
                  </Button>
                )}
                <p className="text-[11px] text-muted">
                  Escalation emails and SMS state the level, how long the alert has been active, and
                  that it is unacknowledged — e.g.&nbsp;
                  <span className="font-mono text-text">[CRITICAL] Escalation L2: Core router down — core-router-01</span>.
                  &quot;Max escalation repeats&quot; on the Trigger Actions step caps repeats.
                </p>
              </div>
            )}

            {step === 'reset_actions' && (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <SectionTitle title="Reset Actions" hint="Notifications when alert clears (recovery)." />
                  <Button type="button" variant="outline" size="sm" onClick={copyTriggerToReset}>
                    <Copy className="h-3.5 w-3.5" />Copy from trigger
                  </Button>
                </div>
                {s.reset_mode !== 'auto' ? (
                  <p className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
                    Reset notifications are disabled — enable &quot;Reset when trigger clears&quot; on the Reset step.
                  </p>
                ) : (
                  <>
                    <p className="text-[11px] text-muted">
                      The built-in reset wording states how long the condition was active
                      &mdash; <span className="text-text">{'{duration_sentence}'}</span> renders as
                      &ldquo;The condition was active for 12 minutes.&rdquo;
                    </p>
                    <FormField label="Recovery email subject">
                      <Input value={s.recovery_email_subject} onChange={(e) => setS({ ...s, recovery_email_subject: e.target.value })} className="font-mono text-xs" placeholder={DEFAULTS.recovery_email_subject} />
                    </FormField>
                    <FormField label="Recovery email body">
                      <Textarea value={s.recovery_email_body} onChange={(e) => setS({ ...s, recovery_email_body: e.target.value })} rows={4} className="font-mono text-xs" placeholder={DEFAULTS.recovery_email_body} />
                    </FormField>
                    <FormField label="Recovery SMS">
                      <Textarea value={s.recovery_sms_template} onChange={(e) => setS({ ...s, recovery_sms_template: e.target.value })} rows={2} className="font-mono text-xs" placeholder={DEFAULTS.recovery_sms_template} />
                    </FormField>
                  </>
                )}
              </div>
            )}

            {step === 'schedule' && (
              <div className="space-y-4">
                <SectionTitle title="Schedule" hint="Limit when notifications are sent (quiet hours). Alerts are still recorded 24/7." />
                <div className="grid grid-cols-2 gap-3">
                  <FormField label="Notify from">
                    <Input type="time" value={s.schedule_start} onChange={(e) => setS({ ...s, schedule_start: e.target.value })} />
                  </FormField>
                  <FormField label="Notify until">
                    <Input type="time" value={s.schedule_end} onChange={(e) => setS({ ...s, schedule_end: e.target.value })} />
                  </FormField>
                </div>
                <div>
                  <div className="mb-1.5 text-xs font-medium text-muted">Active days</div>
                  <div className="flex flex-wrap gap-1.5">
                    {DAY_LABELS.map((d) => (
                      <button key={d.n} type="button" onClick={() => toggleDay(d.n)}
                        className={cn('h-8 w-11 rounded-md text-xs font-medium', s.schedule_days.includes(d.n) ? 'bg-primary text-white' : 'bg-surface2 text-muted')}>
                        {d.label}
                      </button>
                    ))}
                  </div>
                  <p className="mt-2 text-[11px] text-muted">Leave empty for 24/7 notifications. Recovery alerts always send.</p>
                </div>
              </div>
            )}

            {step === 'review' && (
              <div className="space-y-4">
                <SectionTitle title="Review" hint="Confirm settings before saving." />
                <ReviewRow label="Name" value={s.name || '—'} />
                <ReviewRow label="Severity" value={<Badge variant={s.severity === 'critical' ? 'danger' : s.severity === 'warning' ? 'warning' : 'info'}>{s.severity}</Badge>} />
                <ReviewRow label="Trigger" value={summary.condText} />
                <ReviewRow label="Hold time" value={s.min_duration ? `${s.min_duration}s` : 'Immediate'} />
                <ReviewRow label="Reset" value={s.reset_mode === 'auto' ? 'Auto + recovery notify' : 'No recovery notify'} />
                <ReviewRow label="Channels" value={s.notify_channels.length ? `${s.notify_channels.length} selected` : 'None (alert only)'} />
                <ReviewRow
                  label="Escalation"
                  value={s.escalation_levels.length
                    ? `${s.escalation_levels.length} level${s.escalation_levels.length > 1 ? 's' : ''} (${s.escalation_levels.map((lv) => `${lv.after_minutes}m`).join(' → ')})`
                    : 'None'}
                />
                <ReviewRow label="Enabled" value={s.enabled ? 'Yes' : 'No'} />
                <div className="flex gap-2 pt-2">
                  <Button type="button" variant="outline" size="sm" onClick={() => previewMut.mutate()} disabled={previewMut.isPending}>
                    {previewMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Eye className="h-3.5 w-3.5" />}
                    Preview message
                  </Button>
                  {isEdit && (
                    <Button type="button" variant="outline" size="sm" onClick={() => simulateMut.mutate()} disabled={simulateMut.isPending}>
                      {simulateMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
                      Send test
                    </Button>
                  )}
                </div>
                {preview && (
                  <div className="space-y-3">
                    <EmailPreviewCard title="Alert email" msg={preview.alert} />
                    {preview.recovery && (
                      <EmailPreviewCard title="Recovery email" msg={preview.recovery} accent />
                    )}
                    <p className="text-[11px] text-muted">
                      Sample device and readings. Use the preview button on the alert list to edit
                      these templates against a live preview.
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex shrink-0 items-center justify-between border-t border-border px-5 py-4">
            <Button type="button" variant="outline" onClick={() => stepIdx > 0 ? setStep(STEPS[stepIdx - 1].id) : onOpenChange(false)}>
              <ChevronLeft className="h-4 w-4" />
              {stepIdx > 0 ? 'Back' : 'Cancel'}
            </Button>
            <div className="flex gap-2">
              {stepIdx < STEPS.length - 1 ? (
                <Button type="button" onClick={() => setStep(STEPS[stepIdx + 1].id)}>
                  Next
                  <ChevronRight className="h-4 w-4" />
                </Button>
              ) : (
                <Button type="submit" disabled={save.isPending}>
                  {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                  {isEdit ? 'Save alert' : 'Create alert'}
                </Button>
              )}
            </div>
          </div>
        </form>
      </div>
    </div>
  )
}

function SectionTitle({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="mb-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </div>
  )
}

function ReviewRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border/50 py-2 text-sm">
      <span className="text-muted">{label}</span>
      <span className="max-w-[65%] text-right font-medium">{value}</span>
    </div>
  )
}

function EmailPreviewCard({ title, msg, accent }: { title: string; msg: any; accent?: boolean }) {
  if (!msg) return null
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="border-b border-border bg-surface2/40 px-4 py-2.5">
        <div className={cn('text-xs font-semibold', accent ? 'text-emerald-600' : 'text-text')}>{title}</div>
        <div className="mt-0.5 break-words text-xs text-muted">Subject: {msg.subject}</div>
      </div>
      {/* The real HTML mail, sandboxed — scripts off, no navigation out. */}
      {msg.email_html ? (
        <iframe title={title} sandbox="" srcDoc={msg.email_html} className="h-72 w-full bg-white" />
      ) : (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap p-4 text-xs text-muted">{msg.email_body}</pre>
      )}
    </div>
  )
}

function TemplateVars() {
  return (
    <div className="rounded-lg border border-primary/20 bg-primary/5 p-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-primary">Template variables</p>
      <div className="mt-2 flex flex-wrap gap-1">
        {TEMPLATE_VARS.map((v) => (
          <code key={v} className="cursor-pointer rounded bg-surface px-1.5 py-0.5 text-[10px] text-muted hover:text-primary"
            onClick={() => { void copyText(v) }} title="Click to copy">{v}</code>
        ))}
      </div>
    </div>
  )
}
