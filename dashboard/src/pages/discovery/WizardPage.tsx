import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Calendar,
  CheckCircle2,
  ChevronRight,
  Clock,
  Key,
  Network,
  Plus,
  Radar,
  Save,
  Shield,
  Sliders,
  Tag,
  Terminal,
  Trash2,
  X,
} from 'lucide-react'
import { discoveryApi } from './api'
import { api } from '@/lib/api'
import { DiscoveryProtocol } from './types'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { FormField } from '@/components/ui/FormField'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, cn } from '@/lib/utils'

type ScopeType = 'single_ip' | 'ip_range' | 'cidr' | 'multi' | 'csv'
type ScheduleType = 'once_now' | 'once_future' | 'recurring' | 'cron'
type Frequency = 'hourly' | 'daily' | 'weekly' | 'monthly' | 'custom'

const PROTOCOLS: { value: DiscoveryProtocol; label: string; desc: string }[] = [
  { value: 'icmp', label: 'ICMP (Ping)', desc: 'Reachability check' },
  { value: 'snmp', label: 'SNMP v1/v2c/v3', desc: 'Device identity & inventory' },
  { value: 'ssh', label: 'SSH', desc: 'Linux/network OS detection' },
  { value: 'wmi', label: 'WMI / WinRM', desc: 'Windows device detection' },
  { value: 'http', label: 'HTTP', desc: 'Web service fingerprint' },
  { value: 'https', label: 'HTTPS', desc: 'TLS web service fingerprint' },
  { value: 'tcp', label: 'TCP port check', desc: 'Probe custom ports' },
]

const STEPS = [
  { key: 'scope', label: 'Scan Scope', icon: Network },
  { key: 'creds', label: 'Credentials & Protocols', icon: Key },
  { key: 'schedule', label: 'Schedule', icon: Calendar },
  { key: 'rules', label: 'Classification & Rules', icon: Sliders },
  { key: 'review', label: 'Review & Start', icon: Shield },
] as const

type StepKey = (typeof STEPS)[number]['key']

interface WizardState {
  name: string
  description: string
  scope_type: ScopeType
  targets: string[]
  exclusions: string[]
  protocols: DiscoveryProtocol[]
  custom_ports: number[]
  snmp_credential_ids: string[]
  windows_credential_ids: string[]
  ssh_credential_ids: string[]
  detect_lldp: boolean
  detect_mac: boolean
  detect_vendor: boolean
  max_concurrency: number
  scan_timeout_ms: number
  retry_count: number
  rate_limit_pps: number
  max_duration_sec: number
  // Schedule
  schedule_type: ScheduleType
  frequency: Frequency
  interval_minutes: number
  time_of_day: string
  day_of_week: number
  day_of_month: number
  timezone: string
  start_date: string
  end_date: string
  // Import rules
  import_mode: 'review' | 'auto_match' | 'ignore_match'
  default_group_id: string
  default_tags: string[]
  default_template_id: string
  default_location: string
  enable_monitoring: boolean
  keep_disabled: boolean
  notify_recipients: string[]
}

const DEFAULT_STATE: WizardState = {
  name: '',
  description: '',
  scope_type: 'cidr',
  targets: [''],
  exclusions: [],
  protocols: ['icmp', 'snmp'],
  custom_ports: [],
  snmp_credential_ids: [],
  windows_credential_ids: [],
  ssh_credential_ids: [],
  detect_lldp: true,
  detect_mac: true,
  detect_vendor: true,
  max_concurrency: 32,
  scan_timeout_ms: 2000,
  retry_count: 1,
  rate_limit_pps: 200,
  max_duration_sec: 1800,
  schedule_type: 'once_now',
  frequency: 'daily',
  interval_minutes: 60,
  time_of_day: '02:00',
  day_of_week: 1,
  day_of_month: 1,
  timezone: 'UTC',
  start_date: '',
  end_date: '',
  import_mode: 'review',
  default_group_id: '',
  default_tags: [],
  default_template_id: '',
  default_location: '',
  enable_monitoring: true,
  keep_disabled: false,
  notify_recipients: [],
}

export function WizardPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { id: editingId } = useParams()
  const isEditing = !!editingId

  const [step, setStep] = useState<StepKey>('scope')
  const [state, setState] = useState<WizardState>(DEFAULT_STATE)
  const [tagInput, setTagInput] = useState('')
  const [recipientInput, setRecipientInput] = useState('')

  // Lookups
  const { data: credentials = [] } = useQuery<any[]>({
    queryKey: ['snmp-credentials'],
    queryFn: async () => (await api.get('/snmp-credentials')).data,
  })
  const { data: windowsCreds = [] } = useQuery<any[]>({
    queryKey: ['windows-credentials'],
    queryFn: async () => (await api.get('/windows-credentials')).data,
  })
  const { data: ncmCredsResp } = useQuery<{ data: any[] }>({
    queryKey: ['ncm', 'credentials'],
    queryFn: async () => (await api.get('/ncm/credentials')).data,
  })
  const sshCreds = ncmCredsResp?.data ?? []
  const { data: groups = [] } = useQuery<any[]>({
    queryKey: ['device-groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
  })

  // Load existing profile when editing
  const { data: editing } = useQuery({
    queryKey: ['discovery', 'profile', editingId],
    queryFn: () => discoveryApi.getProfile(editingId!),
    enabled: isEditing,
  })
  useEffect(() => {
    if (editing) {
      setState({
        ...DEFAULT_STATE,
        name: editing.name,
        description: editing.description || '',
        scope_type: editing.scope_type,
        targets: editing.targets.length ? editing.targets : [''],
        exclusions: editing.exclusions,
        protocols: editing.protocols,
        custom_ports: editing.custom_ports,
        snmp_credential_ids: editing.snmp_credential_ids,
        windows_credential_ids: (editing as any).windows_credential_ids || [],
        ssh_credential_ids: (editing as any).ssh_credential_ids || [],
        detect_lldp: editing.detect_lldp,
        detect_mac: editing.detect_mac,
        detect_vendor: editing.detect_vendor,
        max_concurrency: editing.max_concurrency,
        scan_timeout_ms: editing.scan_timeout_ms,
        retry_count: editing.retry_count,
        rate_limit_pps: editing.rate_limit_pps,
        max_duration_sec: editing.max_duration_sec,
        import_mode: editing.import_mode,
        default_group_id: editing.default_group_id || '',
        default_tags: editing.default_tags,
        default_template_id: editing.default_template_id || '',
        default_location: editing.default_location || '',
        enable_monitoring: editing.enable_monitoring,
        keep_disabled: editing.keep_disabled,
        notify_recipients: editing.notify_recipients,
        // schedule fields default to once_now when editing — we don't rehydrate
      })
    }
  }, [editing])

  const cleanTargets = useMemo(() => state.targets.map((t) => t.trim()).filter(Boolean), [state.targets])

  // Live estimate
  const { data: estimate } = useQuery({
    queryKey: ['discovery', 'estimate', cleanTargets.join(','), state.exclusions.join(',')],
    queryFn: () => discoveryApi.estimate(cleanTargets, state.exclusions),
    enabled: cleanTargets.length > 0,
  })

  const update = (patch: Partial<WizardState>) => setState((s) => ({ ...s, ...patch }))
  const stepIndex = STEPS.findIndex((s) => s.key === step)

  const canAdvance = (): boolean => {
    if (step === 'scope') return state.name.trim().length > 0 && cleanTargets.length > 0
    if (step === 'creds') return state.protocols.length > 0
    return true
  }

  const buildPayload = () => {
    const payload: any = {
      name: state.name.trim(),
      description: state.description || null,
      enabled: true,
      scope_type: state.scope_type,
      targets: cleanTargets,
      exclusions: state.exclusions,
      protocols: state.protocols,
      custom_ports: state.custom_ports,
      snmp_credential_ids: state.snmp_credential_ids,
      windows_credential_ids: state.windows_credential_ids,
      ssh_credential_ids: state.ssh_credential_ids,
      detect_lldp: state.detect_lldp,
      detect_mac: state.detect_mac,
      detect_vendor: state.detect_vendor,
      max_concurrency: state.max_concurrency,
      scan_timeout_ms: state.scan_timeout_ms,
      retry_count: state.retry_count,
      rate_limit_pps: state.rate_limit_pps,
      max_duration_sec: state.max_duration_sec,
      import_mode: state.import_mode,
      default_group_id: state.default_group_id || null,
      default_tags: state.default_tags,
      default_template_id: state.default_template_id || null,
      default_location: state.default_location || null,
      enable_monitoring: state.enable_monitoring,
      keep_disabled: state.keep_disabled,
      notify_recipients: state.notify_recipients,
    }
    if (state.schedule_type !== 'once_now') {
      payload.schedule = {
        enabled: true,
        schedule_type: state.schedule_type,
        frequency: state.schedule_type === 'recurring' ? state.frequency : null,
        interval_minutes:
          state.schedule_type === 'recurring' && state.frequency === 'custom'
            ? state.interval_minutes
            : null,
        time_of_day:
          state.schedule_type === 'recurring' &&
          ['daily', 'weekly', 'monthly'].includes(state.frequency)
            ? state.time_of_day
            : null,
        day_of_week:
          state.schedule_type === 'recurring' && state.frequency === 'weekly'
            ? state.day_of_week
            : null,
        day_of_month:
          state.schedule_type === 'recurring' && state.frequency === 'monthly'
            ? state.day_of_month
            : null,
        timezone: state.timezone,
        start_date: state.start_date || null,
        end_date: state.end_date || null,
      }
    }
    return payload
  }

  const saveAndRunMutation = useMutation({
    mutationFn: async () => {
      const profile = isEditing
        ? await discoveryApi.updateProfile(editingId!, buildPayload())
        : await discoveryApi.createProfile(buildPayload())
      const run = await discoveryApi.startRun(profile.id)
      return { profile, run }
    },
    onSuccess: ({ run }) => {
      qc.invalidateQueries({ queryKey: ['discovery'] })
      toast.success('Scan started', 'Tracking progress now…')
      navigate(`/discovery/runs/${run.id}`)
    },
    onError: (e: any) => toast.error('Could not start scan', apiErrorMessage(e)),
  })

  const saveOnlyMutation = useMutation({
    mutationFn: async () =>
      isEditing
        ? discoveryApi.updateProfile(editingId!, buildPayload())
        : discoveryApi.createProfile(buildPayload()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery'] })
      toast.success(isEditing ? 'Profile updated' : 'Profile saved')
      navigate('/discovery')
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const saveDraftMutation = useMutation({
    mutationFn: async () => {
      const payload = { ...buildPayload(), enabled: false }
      return isEditing
        ? discoveryApi.updateProfile(editingId!, payload)
        : discoveryApi.createProfile(payload)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery'] })
      toast.success('Draft saved', 'Profile saved as disabled.')
      navigate('/discovery')
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <button
            onClick={() => navigate('/discovery')}
            className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Back to profiles
          </button>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Radar className="h-6 w-6 text-primary" />
            {isEditing ? 'Edit discovery profile' : 'New discovery scan'}
          </h1>
        </div>
      </div>

      {/* Stepper */}
      <Card>
        <CardContent className="flex flex-wrap items-center gap-1 p-3">
          {STEPS.map((s, i) => {
            const done = i < stepIndex
            const active = s.key === step
            return (
              <button
                key={s.key}
                type="button"
                onClick={() => i <= stepIndex && setStep(s.key)}
                className={cn(
                  'group flex items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors',
                  active
                    ? 'bg-primary/10 text-text'
                    : done
                      ? 'text-text hover:bg-surface2'
                      : 'text-muted',
                )}
              >
                <span
                  className={cn(
                    'flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-semibold',
                    active
                      ? 'bg-primary text-white'
                      : done
                        ? 'bg-success/20 text-success'
                        : 'bg-surface2 text-muted',
                  )}
                >
                  {done ? <CheckCircle2 className="h-3.5 w-3.5" /> : i + 1}
                </span>
                <span className="hidden sm:inline">{s.label}</span>
                {i < STEPS.length - 1 && (
                  <ChevronRight className="h-3.5 w-3.5 text-muted/50" />
                )}
              </button>
            )
          })}
        </CardContent>
      </Card>

      {/* Step body */}
      <Card>
        <CardContent className="space-y-5 p-5">
          {step === 'scope' && (
            <ScopeStep
              state={state}
              update={update}
              estimate={estimate}
            />
          )}
          {step === 'creds' && (
            <CredentialsStep
              state={state} update={update}
              credentials={credentials} windowsCreds={windowsCreds}
              sshCreds={sshCreds}
            />
          )}
          {step === 'schedule' && <ScheduleStep state={state} update={update} />}
          {step === 'rules' && (
            <RulesStep
              state={state}
              update={update}
              groups={groups}
              tagInput={tagInput}
              setTagInput={setTagInput}
              recipientInput={recipientInput}
              setRecipientInput={setRecipientInput}
            />
          )}
          {step === 'review' && <ReviewStep state={state} estimate={estimate} />}
        </CardContent>
      </Card>

      {/* Footer actions */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <Button
            variant="outline"
            onClick={() =>
              stepIndex > 0 ? setStep(STEPS[stepIndex - 1].key) : navigate('/discovery')
            }
          >
            <ArrowLeft className="h-4 w-4" /> {stepIndex === 0 ? 'Cancel' : 'Back'}
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="ghost"
            onClick={() => saveDraftMutation.mutate()}
            disabled={!state.name || cleanTargets.length === 0}
          >
            Save draft
          </Button>
          {step !== 'review' ? (
            <Button
              onClick={() =>
                canAdvance() ? setStep(STEPS[stepIndex + 1].key) : null
              }
              disabled={!canAdvance()}
            >
              Next <ArrowRight className="h-4 w-4" />
            </Button>
          ) : state.schedule_type === 'once_now' ? (
            <Button
              onClick={() => saveAndRunMutation.mutate()}
              disabled={saveAndRunMutation.isPending}
            >
              <Radar className="h-4 w-4" /> Start scan
            </Button>
          ) : (
            <Button
              onClick={() => saveOnlyMutation.mutate()}
              disabled={saveOnlyMutation.isPending}
            >
              <Save className="h-4 w-4" /> Save & schedule
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────
// Steps
// ────────────────────────────────────────────────────────────────────
function ScopeStep({
  state,
  update,
  estimate,
}: {
  state: WizardState
  update: (p: Partial<WizardState>) => void
  estimate?: any
}) {
  const updateTarget = (i: number, v: string) => {
    const copy = [...state.targets]
    copy[i] = v
    update({ targets: copy })
  }
  const addTarget = () => update({ targets: [...state.targets, ''] })
  const removeTarget = (i: number) => update({ targets: state.targets.filter((_, idx) => idx !== i) })

  const updateExclusion = (i: number, v: string) => {
    const copy = [...state.exclusions]
    copy[i] = v
    update({ exclusions: copy })
  }
  const addExclusion = () => update({ exclusions: [...state.exclusions, ''] })
  const removeExclusion = (i: number) =>
    update({ exclusions: state.exclusions.filter((_, idx) => idx !== i) })

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        <FormField label="Profile name" required hint="Used to identify the profile in lists & reports">
          <Input
            placeholder="e.g. Corporate LAN — daily sweep"
            value={state.name}
            onChange={(e) => update({ name: e.target.value })}
          />
        </FormField>
        <FormField label="Scope type" hint="How the targets below should be parsed">
          <Select
            value={state.scope_type}
            onValueChange={(v) => update({ scope_type: v as ScopeType })}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="cidr">CIDR / subnet</SelectItem>
              <SelectItem value="ip_range">IP range</SelectItem>
              <SelectItem value="single_ip">Single IP</SelectItem>
              <SelectItem value="multi">Multiple subnets</SelectItem>
              <SelectItem value="csv">CSV import</SelectItem>
            </SelectContent>
          </Select>
        </FormField>
      </div>

      <FormField label="Description" hint="Optional — purpose, owner, network notes">
        <Textarea
          rows={2}
          placeholder="Short notes about this scan…"
          value={state.description}
          onChange={(e) => update({ description: e.target.value })}
        />
      </FormField>

      <div>
        <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">
          Targets
        </div>
        <p className="mb-2 text-xs text-muted">
          Examples: <code>192.168.1.0/24</code>, <code>10.0.0.5</code>,{' '}
          <code>10.0.0.10-10.0.0.50</code>, <code>10.0.0.10-50</code>
        </p>
        <div className="space-y-2">
          {state.targets.map((t, i) => (
            <div key={i} className="flex items-center gap-2">
              <Input
                placeholder="192.168.1.0/24"
                value={t}
                onChange={(e) => updateTarget(i, e.target.value)}
              />
              {state.targets.length > 1 && (
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => removeTarget(i)}
                  className="text-muted hover:text-danger"
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              )}
            </div>
          ))}
        </div>
        <Button variant="ghost" size="sm" onClick={addTarget} className="mt-2">
          <Plus className="h-3 w-3" /> Add target
        </Button>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <div className="text-xs font-medium uppercase tracking-wider text-muted">
            Excluded IPs / ranges
          </div>
          <Button variant="ghost" size="sm" onClick={addExclusion}>
            <Plus className="h-3 w-3" /> Add exclusion
          </Button>
        </div>
        {state.exclusions.length === 0 ? (
          <p className="text-xs text-muted">No exclusions configured.</p>
        ) : (
          <div className="space-y-2">
            {state.exclusions.map((t, i) => (
              <div key={i} className="flex items-center gap-2">
                <Input
                  placeholder="10.0.0.5 or 10.0.0.10-50"
                  value={t}
                  onChange={(e) => updateExclusion(i, e.target.value)}
                />
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => removeExclusion(i)}
                  className="text-muted hover:text-danger"
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      {estimate && (
        <div
          className={cn(
            'rounded-md border px-4 py-3 text-sm',
            estimate.ip_count > 256
              ? 'border-warning/30 bg-warning/10 text-warning'
              : 'border-info/30 bg-info/10 text-info',
          )}
        >
          <div className="flex items-start gap-2">
            <Network className="mt-0.5 h-4 w-4" />
            <div>
              <div className="font-medium">
                {estimate.ip_count.toLocaleString()} address
                {estimate.ip_count === 1 ? '' : 'es'} will be scanned
              </div>
              {estimate.preview?.length > 0 && (
                <div className="mt-1 text-xs opacity-80">
                  Preview: {estimate.preview.join(', ')}
                  {estimate.ip_count > estimate.preview.length && ' …'}
                </div>
              )}
              {(estimate.warnings || []).map((w: string, i: number) => (
                <div key={i} className="mt-1 text-xs">⚠ {w}</div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function CredentialsStep({
  state,
  update,
  credentials,
  windowsCreds,
  sshCreds,
}: {
  state: WizardState
  update: (p: Partial<WizardState>) => void
  credentials: any[]
  windowsCreds: any[]
  sshCreds: any[]
}) {
  const toggleProto = (p: DiscoveryProtocol) => {
    const has = state.protocols.includes(p)
    update({
      protocols: has ? state.protocols.filter((x) => x !== p) : [...state.protocols, p],
    })
  }
  const toggleCred = (id: string) => {
    const has = state.snmp_credential_ids.includes(id)
    update({
      snmp_credential_ids: has
        ? state.snmp_credential_ids.filter((x) => x !== id)
        : [...state.snmp_credential_ids, id],
    })
  }
  const toggleWinCred = (id: string) => {
    const has = state.windows_credential_ids.includes(id)
    update({
      windows_credential_ids: has
        ? state.windows_credential_ids.filter((x) => x !== id)
        : [...state.windows_credential_ids, id],
    })
  }
  const toggleSshCred = (id: string) => {
    const has = state.ssh_credential_ids.includes(id)
    update({
      ssh_credential_ids: has
        ? state.ssh_credential_ids.filter((x) => x !== id)
        : [...state.ssh_credential_ids, id],
    })
  }
  const needsWindows = state.protocols.includes('wmi') || state.protocols.includes('winrm')
  return (
    <div className="space-y-5">
      <div>
        <div className="mb-2 text-sm font-medium">Discovery protocols</div>
        <div className="grid gap-2 md:grid-cols-2">
          {PROTOCOLS.map((p) => {
            const active = state.protocols.includes(p.value)
            return (
              <button
                key={p.value}
                type="button"
                onClick={() => toggleProto(p.value)}
                className={cn(
                  'flex items-start gap-3 rounded-md border p-3 text-left transition-colors',
                  active
                    ? 'border-primary bg-primary/5'
                    : 'border-border hover:border-primary/40',
                )}
              >
                <div
                  className={cn(
                    'mt-0.5 flex h-5 w-5 items-center justify-center rounded border',
                    active ? 'border-primary bg-primary text-white' : 'border-border',
                  )}
                >
                  {active && <CheckCircle2 className="h-3.5 w-3.5" />}
                </div>
                <div className="flex-1">
                  <div className="text-sm font-medium">{p.label}</div>
                  <div className="text-xs text-muted">{p.desc}</div>
                </div>
              </button>
            )
          })}
        </div>
      </div>

      {state.protocols.includes('tcp') && (
        <FormField
          label="Custom TCP ports"
          hint="Comma-separated list, e.g. 22, 80, 443, 8080"
        >
          <Input
            placeholder="22, 80, 443, 8080"
            value={state.custom_ports.join(', ')}
            onChange={(e) =>
              update({
                custom_ports: e.target.value
                  .split(/[,\s]+/)
                  .map((s) => parseInt(s, 10))
                  .filter((n) => !isNaN(n) && n > 0 && n < 65536),
              })
            }
          />
        </FormField>
      )}

      {state.protocols.includes('snmp') && (
        <div>
          <div className="mb-2 text-sm font-medium">SNMP credentials to try</div>
          {credentials.length === 0 ? (
            <div className="rounded-md border border-border bg-surface2/30 px-4 py-3 text-sm text-muted">
              No SNMP credentials saved yet.{' '}
              <a className="text-primary underline" href="/snmp-profiles" target="_blank">
                Manage credentials
              </a>{' '}
              first — the discovery scan will skip SNMP without them.
            </div>
          ) : (
            <div className="space-y-1.5">
              {credentials.map((c) => {
                const sel = state.snmp_credential_ids.includes(c.id)
                return (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => toggleCred(c.id)}
                    className={cn(
                      'flex w-full items-center gap-3 rounded-md border p-2.5 text-left transition-colors',
                      sel ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
                    )}
                  >
                    <div
                      className={cn(
                        'flex h-4 w-4 items-center justify-center rounded border',
                        sel ? 'border-primary bg-primary text-white' : 'border-border',
                      )}
                    >
                      {sel && <CheckCircle2 className="h-3 w-3" />}
                    </div>
                    <Key className="h-4 w-4 text-muted" />
                    <span className="text-sm font-medium">{c.name}</span>
                    <Badge variant="outline">v{c.snmp_version}</Badge>
                    <span className="ml-auto text-xs text-muted">
                      port {c.port} · {c.timeout_ms}ms
                    </span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}

      {state.protocols.includes('ssh') && (
        <div>
          <div className="mb-2 text-sm font-medium">SSH / CLI credentials to try</div>
          {sshCreds.length === 0 ? (
            <div className="rounded-md border border-border bg-surface2/30 px-4 py-3 text-sm text-muted">
              No SSH connection profiles saved yet.{' '}
              <a className="text-primary underline" href="/ncm" target="_blank">
                Manage NCM connection profiles
              </a>{' '}
              first — discovery will only grab the SSH banner without credentials.
            </div>
          ) : (
            <div className="space-y-1.5">
              {sshCreds.map((c) => {
                const sel = state.ssh_credential_ids.includes(c.id)
                return (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => toggleSshCred(c.id)}
                    className={cn(
                      'flex w-full items-center gap-3 rounded-md border p-2.5 text-left transition-colors',
                      sel ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
                    )}
                  >
                    <div
                      className={cn(
                        'flex h-4 w-4 items-center justify-center rounded border',
                        sel ? 'border-primary bg-primary text-white' : 'border-border',
                      )}
                    >
                      {sel && <CheckCircle2 className="h-3 w-3" />}
                    </div>
                    <Terminal className="h-4 w-4 text-muted" />
                    <span className="text-sm font-medium">{c.name}</span>
                    <span className="text-xs text-muted">{c.username}</span>
                    <Badge variant="outline">{(c.protocol || 'ssh').toUpperCase()}</Badge>
                    <span className="ml-auto text-xs text-muted">port {c.port || 22}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}

      {needsWindows && (
        <div>
          <div className="mb-2 text-sm font-medium">Windows credentials (WMI / WinRM)</div>
          {windowsCreds.length === 0 ? (
            <div className="rounded-md border border-border bg-surface2/30 px-4 py-3 text-sm text-muted">
              No Windows credentials saved yet.{' '}
              <a className="text-primary underline" href="/windows-credentials" target="_blank">
                Manage Windows credentials
              </a>{' '}
              first — WMI/WinRM probes will report "no credential configured" without them.
            </div>
          ) : (
            <div className="space-y-1.5">
              {windowsCreds.map((c) => {
                const sel = state.windows_credential_ids.includes(c.id)
                return (
                  <button
                    key={c.id} type="button" onClick={() => toggleWinCred(c.id)}
                    className={cn(
                      'flex w-full items-center gap-3 rounded-md border p-2.5 text-left transition-colors',
                      sel ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
                    )}
                  >
                    <div
                      className={cn(
                        'flex h-4 w-4 items-center justify-center rounded border',
                        sel ? 'border-primary bg-primary text-white' : 'border-border',
                      )}
                    >
                      {sel && <CheckCircle2 className="h-3 w-3" />}
                    </div>
                    <Key className="h-4 w-4 text-muted" />
                    <span className="text-sm font-medium">{c.name}</span>
                    <span className="text-xs text-muted">
                      {c.domain ? `${c.domain}\\${c.username}` : c.username}
                    </span>
                    <Badge variant="outline">{c.auth_method}</Badge>
                    <span className="ml-auto text-xs text-muted">
                      {c.transport.toUpperCase()} / port {c.port}
                    </span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}

      <div>
        <div className="mb-2 text-sm font-medium">Detection options</div>
        <div className="space-y-2.5">
          <ToggleRow
            label="Detect MAC address & vendor (OUI)"
            value={state.detect_mac}
            onChange={(v) => update({ detect_mac: v })}
          />
          <ToggleRow
            label="Detect LLDP / CDP neighbors"
            value={state.detect_lldp}
            onChange={(v) => update({ detect_lldp: v })}
          />
          <ToggleRow
            label="Fingerprint vendor via sysObjectID"
            value={state.detect_vendor}
            onChange={(v) => update({ detect_vendor: v })}
          />
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <FormField label="Concurrency">
          <Input
            type="number"
            min={1}
            max={256}
            value={state.max_concurrency}
            onChange={(e) => update({ max_concurrency: Number(e.target.value) || 32 })}
          />
        </FormField>
        <FormField label="Timeout (ms)">
          <Input
            type="number"
            min={200}
            max={30000}
            step={100}
            value={state.scan_timeout_ms}
            onChange={(e) => update({ scan_timeout_ms: Number(e.target.value) || 2000 })}
          />
        </FormField>
        <FormField label="Retries">
          <Input
            type="number"
            min={0}
            max={5}
            value={state.retry_count}
            onChange={(e) => update({ retry_count: Number(e.target.value) || 0 })}
          />
        </FormField>
        <FormField label="Rate limit (pps)">
          <Input
            type="number"
            min={10}
            max={10000}
            value={state.rate_limit_pps}
            onChange={(e) => update({ rate_limit_pps: Number(e.target.value) || 200 })}
          />
        </FormField>
      </div>
    </div>
  )
}

function ScheduleStep({
  state,
  update,
}: {
  state: WizardState
  update: (p: Partial<WizardState>) => void
}) {
  const choices: { value: WizardState['schedule_type']; label: string; desc: string }[] = [
    { value: 'once_now', label: 'Run once now', desc: 'Scan immediately when saved' },
    { value: 'once_future', label: 'Run once at a future time', desc: 'Schedule a one-off scan' },
    { value: 'recurring', label: 'Recurring schedule', desc: 'Hourly, daily, weekly, or monthly' },
    { value: 'cron', label: 'Custom cron expression', desc: 'Advanced — paste a cron string' },
  ]
  return (
    <div className="space-y-5">
      <div>
        <div className="mb-2 text-sm font-medium">Schedule type</div>
        <div className="grid gap-2 md:grid-cols-2">
          {choices.map((c) => {
            const active = state.schedule_type === c.value
            return (
              <button
                key={c.value}
                type="button"
                onClick={() => update({ schedule_type: c.value })}
                className={cn(
                  'flex items-start gap-3 rounded-md border p-3 text-left transition-colors',
                  active ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
                )}
              >
                <div
                  className={cn(
                    'mt-0.5 flex h-5 w-5 items-center justify-center rounded-full border',
                    active ? 'border-primary bg-primary text-white' : 'border-border',
                  )}
                >
                  {active && <span className="h-2 w-2 rounded-full bg-white" />}
                </div>
                <div>
                  <div className="text-sm font-medium">{c.label}</div>
                  <div className="text-xs text-muted">{c.desc}</div>
                </div>
              </button>
            )
          })}
        </div>
      </div>

      {state.schedule_type === 'once_future' && (
        <div className="grid gap-3 md:grid-cols-2">
          <FormField label="Run at">
            <Input
              type="datetime-local"
              value={state.start_date}
              onChange={(e) => update({ start_date: e.target.value })}
            />
          </FormField>
          <FormField label="Timezone">
            <Input
              placeholder="UTC, America/New_York, …"
              value={state.timezone}
              onChange={(e) => update({ timezone: e.target.value })}
            />
          </FormField>
        </div>
      )}

      {state.schedule_type === 'recurring' && (
        <div className="space-y-3">
          <FormField label="Frequency">
            <Select
              value={state.frequency}
              onValueChange={(v) => update({ frequency: v as Frequency })}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="hourly">Hourly</SelectItem>
                <SelectItem value="daily">Daily</SelectItem>
                <SelectItem value="weekly">Weekly</SelectItem>
                <SelectItem value="monthly">Monthly</SelectItem>
                <SelectItem value="custom">Every N minutes</SelectItem>
              </SelectContent>
            </Select>
          </FormField>

          {state.frequency === 'custom' && (
            <FormField label="Interval (minutes)" hint="Minimum 5">
              <Input
                type="number"
                min={5}
                max={10080}
                value={state.interval_minutes}
                onChange={(e) => update({ interval_minutes: Number(e.target.value) || 60 })}
              />
            </FormField>
          )}

          {['daily', 'weekly', 'monthly'].includes(state.frequency) && (
            <FormField label="Time of day">
              <Input
                type="time"
                value={state.time_of_day}
                onChange={(e) => update({ time_of_day: e.target.value })}
              />
            </FormField>
          )}
          {state.frequency === 'weekly' && (
            <FormField label="Day of week">
              <Select
                value={String(state.day_of_week)}
                onValueChange={(v) => update({ day_of_week: parseInt(v, 10) })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map(
                    (d, i) => (
                      <SelectItem key={i} value={String(i)}>
                        {d}
                      </SelectItem>
                    ),
                  )}
                </SelectContent>
              </Select>
            </FormField>
          )}
          {state.frequency === 'monthly' && (
            <FormField label="Day of month" hint="1-28">
              <Input
                type="number"
                min={1}
                max={28}
                value={state.day_of_month}
                onChange={(e) => update({ day_of_month: Number(e.target.value) || 1 })}
              />
            </FormField>
          )}

          <div className="grid gap-3 md:grid-cols-2">
            <FormField label="Start date">
              <Input
                type="datetime-local"
                value={state.start_date}
                onChange={(e) => update({ start_date: e.target.value })}
              />
            </FormField>
            <FormField label="End date (optional)">
              <Input
                type="datetime-local"
                value={state.end_date}
                onChange={(e) => update({ end_date: e.target.value })}
              />
            </FormField>
          </div>
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-3">
        <FormField label="Max duration (sec)" hint="Hard cap for a single run">
          <Input
            type="number"
            min={10}
            max={86400}
            value={state.max_duration_sec}
            onChange={(e) => update({ max_duration_sec: Number(e.target.value) || 1800 })}
          />
        </FormField>
        <FormField label="Scan timeout (ms)">
          <Input
            type="number"
            min={200}
            max={60000}
            value={state.scan_timeout_ms}
            onChange={(e) => update({ scan_timeout_ms: Number(e.target.value) || 2000 })}
          />
        </FormField>
        <FormField label="Retries">
          <Input
            type="number"
            min={0}
            max={5}
            value={state.retry_count}
            onChange={(e) => update({ retry_count: Number(e.target.value) || 1 })}
          />
        </FormField>
      </div>

      <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-4 py-3 text-xs text-warning">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        Large scans may impact network performance. Use rate limits or schedule during
        low-traffic hours.
      </div>
    </div>
  )
}

function RulesStep({
  state,
  update,
  groups,
  tagInput,
  setTagInput,
  recipientInput,
  setRecipientInput,
}: {
  state: WizardState
  update: (p: Partial<WizardState>) => void
  groups: any[]
  tagInput: string
  setTagInput: (s: string) => void
  recipientInput: string
  setRecipientInput: (s: string) => void
}) {
  const addTag = () => {
    const v = tagInput.trim()
    if (!v) return
    update({ default_tags: Array.from(new Set([...state.default_tags, v])) })
    setTagInput('')
  }
  const removeTag = (t: string) =>
    update({ default_tags: state.default_tags.filter((x) => x !== t) })

  const addRecipient = () => {
    const v = recipientInput.trim()
    if (!v) return
    update({ notify_recipients: Array.from(new Set([...state.notify_recipients, v])) })
    setRecipientInput('')
  }
  const removeRecipient = (r: string) =>
    update({ notify_recipients: state.notify_recipients.filter((x) => x !== r) })

  return (
    <div className="space-y-5">
      <div>
        <div className="mb-2 text-sm font-medium">When a device is discovered…</div>
        <div className="space-y-2">
          {[
            { v: 'review', label: 'Do not auto-import — review first (recommended)', desc: 'You manually approve each device' },
            { v: 'auto_match', label: 'Auto-import devices matching classification rules', desc: 'Apply rules to import without confirmation' },
            { v: 'ignore_match', label: 'Ignore devices matching ignore rules', desc: 'Used to filter known noise' },
          ].map((c) => {
            const active = state.import_mode === (c.v as any)
            return (
              <button
                key={c.v}
                type="button"
                onClick={() => update({ import_mode: c.v as any })}
                className={cn(
                  'flex w-full items-start gap-3 rounded-md border p-3 text-left transition-colors',
                  active ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
                )}
              >
                <div
                  className={cn(
                    'mt-0.5 flex h-5 w-5 items-center justify-center rounded-full border',
                    active ? 'border-primary bg-primary text-white' : 'border-border',
                  )}
                >
                  {active && <span className="h-2 w-2 rounded-full bg-white" />}
                </div>
                <div>
                  <div className="text-sm font-medium">{c.label}</div>
                  <div className="text-xs text-muted">{c.desc}</div>
                </div>
              </button>
            )
          })}
        </div>
      </div>

      <div>
        <div className="mb-2 text-sm font-medium">Defaults when importing</div>
        <div className="grid gap-3 md:grid-cols-2">
          <FormField label="Device group">
            <Select
              value={state.default_group_id || '__none__'}
              onValueChange={(v) => update({ default_group_id: v === '__none__' ? '' : v })}
            >
              <SelectTrigger>
                <SelectValue placeholder="No group" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">No group</SelectItem>
                {groups.map((g) => (
                  <SelectItem key={g.id} value={g.id}>
                    {g.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Location">
            <Input
              placeholder="Office HQ"
              value={state.default_location}
              onChange={(e) => update({ default_location: e.target.value })}
            />
          </FormField>
        </div>
      </div>

      <FormField label="Tags applied on import" hint="Press Enter to add">
        <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-2">
          {state.default_tags.map((t) => (
            <Badge key={t} className="gap-1.5">
              <Tag className="h-3 w-3" />
              {t}
              <button
                type="button"
                onClick={() => removeTag(t)}
                className="ml-1 text-muted hover:text-danger"
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
          <input
            value={tagInput}
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ',') {
                e.preventDefault()
                addTag()
              }
            }}
            placeholder={state.default_tags.length === 0 ? 'Add tag…' : ''}
            className="flex-1 min-w-[120px] bg-transparent text-sm outline-none"
          />
        </div>
      </FormField>

      <div className="space-y-2.5">
        <ToggleRow
          label="Enable monitoring after import"
          desc="Ping monitoring will start automatically"
          value={state.enable_monitoring}
          onChange={(v) => update({ enable_monitoring: v })}
        />
        <ToggleRow
          label="Keep imported devices disabled until reviewed"
          desc="Devices are added but paused for manual approval"
          value={state.keep_disabled}
          onChange={(v) => update({ keep_disabled: v })}
        />
      </div>

      <FormField label="Notify on completion" hint="Comma-separated emails / channel names">
        <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-2">
          {state.notify_recipients.map((r) => (
            <Badge key={r} className="gap-1.5">
              {r}
              <button
                type="button"
                onClick={() => removeRecipient(r)}
                className="ml-1 text-muted hover:text-danger"
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
          <input
            value={recipientInput}
            onChange={(e) => setRecipientInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ',') {
                e.preventDefault()
                addRecipient()
              }
            }}
            placeholder={state.notify_recipients.length === 0 ? 'ops@example.com' : ''}
            className="flex-1 min-w-[120px] bg-transparent text-sm outline-none"
          />
        </div>
      </FormField>
    </div>
  )
}

function ReviewStep({ state, estimate }: { state: WizardState; estimate?: any }) {
  return (
    <div className="space-y-5">
      <div>
        <div className="mb-2 text-sm font-medium">Review summary</div>
        <div className="grid gap-3 md:grid-cols-2">
          <ReviewBlock title="Profile">
            <Row label="Name" value={state.name} />
            {state.description && <Row label="Description" value={state.description} />}
          </ReviewBlock>
          <ReviewBlock title="Scope">
            <Row label="Type" value={state.scope_type} />
            <Row
              label="Targets"
              value={state.targets.filter(Boolean).join(', ') || '—'}
            />
            {state.exclusions.filter(Boolean).length > 0 && (
              <Row label="Exclusions" value={state.exclusions.filter(Boolean).join(', ')} />
            )}
            <Row
              label="Estimated"
              value={
                estimate ? `${estimate.ip_count.toLocaleString()} address(es)` : 'pending'
              }
            />
          </ReviewBlock>
          <ReviewBlock title="Protocols & credentials">
            <Row label="Protocols" value={state.protocols.join(', ').toUpperCase()} />
            <Row label="SNMP credentials" value={state.snmp_credential_ids.length || 'none selected'} />
            {state.protocols.includes('ssh') && (
              <Row label="SSH credentials" value={state.ssh_credential_ids.length || 'none selected'} />
            )}
            {(state.protocols.includes('wmi') || state.protocols.includes('winrm')) && (
              <Row label="Windows credentials" value={state.windows_credential_ids.length || 'none selected'} />
            )}
            {state.custom_ports.length > 0 && (
              <Row label="Custom ports" value={state.custom_ports.join(', ')} />
            )}
            <Row label="Concurrency" value={state.max_concurrency} />
            <Row label="Timeout" value={`${state.scan_timeout_ms} ms`} />
          </ReviewBlock>
          <ReviewBlock title="Schedule">
            <Row label="When" value={describeSchedule(state)} />
            <Row label="Timezone" value={state.timezone} />
            <Row label="Max duration" value={`${state.max_duration_sec} sec`} />
          </ReviewBlock>
          <ReviewBlock title="Import rules" className="md:col-span-2">
            <Row label="On discovery" value={state.import_mode.replace('_', ' ')} />
            <Row
              label="Default group"
              value={state.default_group_id ? '(selected)' : 'none'}
            />
            <Row
              label="Tags"
              value={state.default_tags.length ? state.default_tags.join(', ') : 'none'}
            />
            <Row label="Enable monitoring" value={state.enable_monitoring ? 'Yes' : 'No'} />
            <Row
              label="Keep disabled until reviewed"
              value={state.keep_disabled ? 'Yes' : 'No'}
            />
          </ReviewBlock>
        </div>
      </div>

      {estimate?.warnings?.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-warning">
          <AlertTriangle className="mt-0.5 h-4 w-4" />
          <div className="space-y-1">
            {estimate.warnings.map((w: string, i: number) => (
              <div key={i}>{w}</div>
            ))}
          </div>
        </div>
      )}

      <div className="flex items-start gap-2 rounded-md border border-info/30 bg-info/10 px-4 py-3 text-xs text-info">
        <Clock className="mt-0.5 h-4 w-4" />
        {state.schedule_type === 'once_now'
          ? 'Pressing Start will run the scan immediately. Progress will stream live on the next page.'
          : 'Pressing Save will store the profile and arm the schedule. Scans will run automatically and notify the configured recipients.'}
      </div>
    </div>
  )
}

function describeSchedule(s: WizardState): string {
  if (s.schedule_type === 'once_now') return 'Run once now'
  if (s.schedule_type === 'once_future') return `Once at ${s.start_date || 'TBD'}`
  if (s.schedule_type === 'cron') return 'Custom cron expression'
  if (s.frequency === 'custom') return `Every ${s.interval_minutes} minutes`
  if (s.frequency === 'hourly') return 'Hourly'
  if (s.frequency === 'daily') return `Daily at ${s.time_of_day}`
  if (s.frequency === 'weekly') {
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    return `Weekly on ${days[s.day_of_week] || ''} at ${s.time_of_day}`
  }
  if (s.frequency === 'monthly') return `Monthly on day ${s.day_of_month} at ${s.time_of_day}`
  return 'Recurring'
}

function ReviewBlock({
  title,
  children,
  className,
}: {
  title: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <Card className={className}>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1.5">{children}</CardContent>
    </Card>
  )
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-muted">{label}</span>
      <span className="font-medium text-text">{value}</span>
    </div>
  )
}

function ToggleRow({
  label,
  desc,
  value,
  onChange,
}: {
  label: string
  desc?: string
  value: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      className="flex w-full items-center justify-between rounded-md border border-border bg-surface px-3 py-2 text-left hover:bg-surface2"
    >
      <div>
        <div className="text-sm font-medium">{label}</div>
        {desc && <div className="text-xs text-muted">{desc}</div>}
      </div>
      <Switch checked={value} onCheckedChange={onChange} />
    </button>
  )
}
