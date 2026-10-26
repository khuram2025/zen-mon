import { FormEvent, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Building2, Download, HardDrive, Info, KeyRound, LayoutTemplate, LifeBuoy, Loader2, Mail, Palette, Plug, Save, Send, Settings, ShieldCheck, Upload, User as UserIcon, Users } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import {
  TIMING_FLAP_LABEL, defaultsForm, effectivePolicy, flapDetectionLimitText, fmt3, fmtSeconds, formFromSettings, isLegacyPolicy,
  legacyPresetForm, lossGranularityNote, policyFromForm, policySentences, settingsPayload, timingRows, validateMonitoringForm,
  type MonitoringForm, type MonitoringFormField, type MonitoringSettingsDoc,
} from '@/components/devices/deviceStatus'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { useTheme } from '@/stores/theme'
import { DENSITIES, useDisplay } from '@/stores/display'
import { useAuth, useCan } from '@/stores/auth'
import { toast } from '@/components/ui/Toast'
import { UpdatesTabContent } from '@/components/UpdatesTabContent'
import { LicensesTabContent } from '@/components/LicensesTabContent'
import { SensorsCard } from '@/components/SensorsCard'
import { SupportTabContent } from '@/components/SupportTabContent'
import { StorageTabContent } from '@/components/StorageTabContent'
import { SecurityTabContent } from '@/components/SecurityTabContent'
import { SshTerminalCard } from '@/components/settings/SshTerminalCard'
import { CompanySettings } from '@/components/settings/CompanySettings'
import { MibsTabContent } from '@/components/MibsTabContent'
import { TemplatesTabContent } from '@/components/TemplatesTabContent'
import { AccessTabContent, ACCESS_TAB_PERMISSIONS } from '@/components/access/AccessTabContent'
import { MfaPanel } from '@/components/security/MfaPanel'
import { NewPasswordFields, usePasswordCheck } from '@/components/security/PasswordFields'
import { QueryBoundary } from '@/components/ui/QueryState'
import { ContactMethodsCard } from '@/components/settings/ContactMethodsCard'

// `permission`: hidden unless the user's role grants it (system.admin
// always passes). Tabs without one are visible to everyone.
const TABS = [
  { value: 'company', label: 'Company', icon: Building2, permission: 'settings.manage' },
  { value: 'monitoring', label: 'Monitoring', icon: Activity, permission: 'settings.manage' },
  { value: 'smtp', label: 'SMTP / Email', icon: Mail, permission: 'settings.manage' },
  { value: 'appearance', label: 'Appearance', icon: Palette },
  { value: 'users', label: 'Users & Access', icon: Users, permission: ACCESS_TAB_PERMISSIONS },
  { value: 'licenses', label: 'Licenses', icon: KeyRound, permission: 'system.admin' },
  { value: 'updates', label: 'Updates', icon: Download, permission: 'system.admin' },
  { value: 'storage', label: 'Storage', icon: HardDrive, permission: 'system.admin' },
  { value: 'security', label: 'Security', icon: ShieldCheck, permission: 'system.admin' },
  { value: 'sensors', label: 'Sensors', icon: Plug, permission: 'settings.manage' },
  { value: 'templates', label: 'Monitoring Templates', icon: LayoutTemplate, permission: 'settings.manage' },
  { value: 'mibs', label: 'MIB Library', icon: Upload, permission: 'settings.manage' },
  { value: 'support', label: 'Support', icon: LifeBuoy },
  { value: 'profile', label: 'Profile', icon: UserIcon },
] as const

type TabValue = typeof TABS[number]['value']

export function GeneralSettingsPage() {
  const can = useCan()
  const visibleTabs = TABS.filter((t) => {
    if (!('permission' in t) || !t.permission) return true
    const needed = (Array.isArray(t.permission) ? t.permission : [t.permission]) as readonly string[]
    return needed.some((p) => can(p))
  })
  const [searchParams, setSearchParams] = useSearchParams()
  const requested = searchParams.get('tab') as TabValue | null
  const fallback: TabValue = visibleTabs.some((t) => t.value === 'company') ? 'company' : (visibleTabs[0]?.value ?? 'profile')
  const active: TabValue = visibleTabs.some((t) => t.value === requested) ? (requested as TabValue) : fallback

  function setActive(v: string) {
    const next = new URLSearchParams(searchParams)
    if (v === 'company') next.delete('tab')
    else next.set('tab', v)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Settings className="h-5 w-5 text-primary" /> General Settings
        </h1>
        <p className="text-xs text-muted">
          Company information, email, appearance, users &amp; access control, licenses, updates, sensors, monitoring templates, MIBs, and your profile
        </p>
      </div>

      <Tabs value={active} onValueChange={setActive}>
        <TabsList className="h-auto flex-wrap gap-1 bg-surface2/50 p-1">
          {visibleTabs.map((t) => (
            <TabsTrigger key={t.value} value={t.value} className="gap-1.5">
              <t.icon className="h-3.5 w-3.5" />
              <span>{t.label}</span>
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="company">
          <CompanySettings />
        </TabsContent>
        <TabsContent value="monitoring" className="space-y-4">
          <MonitoringCard />
          <SshTerminalCard />
        </TabsContent>
        <TabsContent value="users">
          <AccessTabContent />
        </TabsContent>
        <TabsContent value="smtp">
          <SmtpCard />
        </TabsContent>
        <TabsContent value="appearance">
          <AppearanceCard />
        </TabsContent>
        <TabsContent value="licenses">
          <LicensesTabContent />
        </TabsContent>
        <TabsContent value="updates">
          <UpdatesTabContent />
        </TabsContent>
        <TabsContent value="storage">
          <StorageTabContent />
        </TabsContent>
        <TabsContent value="security">
          <SecurityTabContent />
        </TabsContent>
        <TabsContent value="sensors">
          <SensorsCard />
        </TabsContent>
        <TabsContent value="templates">
          <TemplatesTabContent />
        </TabsContent>
        <TabsContent value="mibs">
          <MibsTabContent />
        </TabsContent>
        <TabsContent value="support">
          <SupportTabContent />
        </TabsContent>
        <TabsContent value="profile">
          <ProfileCard />
        </TabsContent>
      </Tabs>
    </div>
  )
}

/**
 * Device status policy (system_settings 'monitoring'; docs/device-status-policy.md). The primary rules read as
 * sentences, the tuning values sit under Advanced, and a live preview states what the poller will do (the server's
 * own loader, components/devices/deviceStatus.ts). Save sends the FULL document: every field the card shows.
 */
export function MonitoringCard() {
  const qc = useQueryClient()
  const { data, isError, error, refetch } = useQuery<Record<string, unknown>>({
    queryKey: ['settings', 'monitoring'],
    queryFn: async () => (await api.get('/settings/monitoring')).data,
  })
  // The form follows the stored document: filled from it (also from the cache on the first render) and reset to it
  // after every save; the user's edits stay until then.
  const [form, setForm] = useState<MonitoringForm | null>(() => (data ? formFromSettings(data) : null))
  useEffect(() => { if (data) setForm(formFromSettings(data)) }, [data])

  const save = useMutation({
    mutationFn: async (payload: MonitoringSettingsDoc) => (await api.put('/settings/monitoring', payload)).data,
    onSuccess: (res: { message?: unknown } | undefined) => {
      toast.success('Device status settings saved', typeof res?.message === 'string' ? res.message : 'The poller applies them within about a minute.')
      qc.invalidateQueries({ queryKey: ['settings', 'monitoring'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (!form) {
    return (
      <Card>
        <CardHeader><CardTitle>Device status</CardTitle></CardHeader>
        <CardContent>
          {isError
            ? <div className="flex items-center gap-3 text-sm text-danger">Monitoring settings could not be loaded: {apiErrorMessage(error)} <Button size="sm" variant="outline" onClick={() => refetch()}>Retry</Button></div>
            : <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />Loading monitoring settings…</div>}
        </CardContent>
      </Card>
    )
  }

  const f = form
  const set = (patch: Partial<MonitoringForm>) => setForm({ ...f, ...patch })
  const errors = validateMonitoringForm(f)
  const invalid = Object.keys(errors).length > 0
  const dirty = !!data && JSON.stringify(settingsPayload(f)) !== JSON.stringify(settingsPayload(formFromSettings(data)))
  const policy = policyFromForm(f)
  const auto = effectivePolicy({ degraded_rtt_ms: policy.degraded_rtt_ms, degraded_loss_pct: policy.degraded_loss_pct })
  const rules = policySentences(policy)
  const timing = timingRows(policy)
  const field = (key: MonitoringFormField, label: string, opts: { disabled?: boolean; step?: string; width?: string } = {}) => (
    <NumberInput label={label} value={f[key] as string} error={errors[key]} disabled={opts.disabled} step={opts.step} width={opts.width}
      onChange={(value) => set({ [key]: value } as Partial<MonitoringForm>)} />
  )

  return (
    <Card>
      <CardHeader><CardTitle>Device status</CardTitle></CardHeader>
      <CardContent className="space-y-5">
        <p className="text-sm text-muted">
          A device that answers ping is <span className="font-medium text-text">Degraded</span> when enough recent checks have latency
          OR packet loss strictly above the limits below (equal values do not count), and <span className="font-medium text-text">Down</span> when
          enough consecutive checks get no answer. Chart averages do not determine the status. Changes apply after the controller
          configuration refresh (normally within a minute) and a subsequent check; remote sensor results use these settings when
          received. CPU, memory and hardware alerts use their own rules.
        </p>
        <div role="note" className="flex gap-2 rounded-lg border border-info/30 bg-info/5 p-3 text-xs text-text2">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" aria-hidden />
          <p>
            <span className="font-medium text-text">What changed:</span> a status change is now confirmed over several checks. A single slow,
            lossy or unanswered check stays in the ping history but no longer changes the status. Degraded alerts therefore arrive after
            confirmation (plus any rule hold time), device latency and packet-loss alert rules are evaluated when the confirmed status
            changes, a flapping device's status notifications are held only for the kind of change that flaps (up/down or
            latency/loss), and status-change counts in reports drop from the upgrade onwards.
            The Legacy single-check preset closely restores the previous behaviour.
          </p>
        </div>

        <form onSubmit={(e: FormEvent) => { e.preventDefault(); if (!invalid) save.mutate(settingsPayload(f)) }} className="space-y-5" noValidate>
          <section className="space-y-3">
            <h3 className="text-sm font-semibold">Degraded limits</h3>
            <div className="grid gap-3 sm:grid-cols-2">
              <FormField label="Round-trip time above" hint="Latency beyond which a check counts as slow (default 100 ms)" error={errors.degraded_rtt_ms}>
                <div className="flex items-center gap-2">
                  <Input type="number" min={0} max={10000} step="any" aria-label="Round-trip time above (ms)" aria-invalid={!!errors.degraded_rtt_ms || undefined} value={f.degraded_rtt_ms} onChange={(e) => set({ degraded_rtt_ms: e.target.value })} className="w-32" />
                  <span className="text-sm text-muted">ms</span>
                </div>
              </FormField>
              <FormField label="Packet loss above" hint="Loss beyond which a check counts as lossy (default 10%)" error={errors.degraded_loss_pct}>
                <div className="flex items-center gap-2">
                  <Input type="number" min={1} max={100} step="any" aria-label="Packet loss above (%)" aria-invalid={!!errors.degraded_loss_pct || undefined} value={f.degraded_loss_pct} onChange={(e) => set({ degraded_loss_pct: e.target.value })} className="w-32" />
                  <span className="text-sm text-muted">%</span>
                </div>
              </FormField>
            </div>
            <p className="text-xs text-muted">{lossGranularityNote(policy)}</p>
          </section>

          <section className="space-y-2.5">
            <h3 className="text-sm font-semibold">Confirmation</h3>
            <Sentence errors={[errors.degraded_confirm_checks, errors.degraded_window_checks]}>
              Degraded when {field('degraded_confirm_checks', 'Checks over the limits for Degraded')} of the last {field('degraded_window_checks', 'Checks in the Degraded window')} checks exceed them or get no answer.
            </Sentence>
            <Sentence errors={[errors.down_confirm_checks]}>
              Down after {field('down_confirm_checks', 'Consecutive failed checks for Down')} consecutive failed checks.
            </Sentence>
            <Sentence errors={[errors.flap_start_changes, errors.flap_window_minutes]}>
              <span className="inline-flex items-center gap-2">
                Detect flapping
                <Switch checked={f.flap_detection_enabled} onCheckedChange={(on) => set({ flap_detection_enabled: on })} aria-label="Detect flapping" />
                <span className="text-muted">{f.flap_detection_enabled ? 'on' : 'off'}</span>
              </span>
              {f.flap_detection_enabled && <>: {field('flap_start_changes', 'Status changes that start flapping')} status changes within {field('flap_window_minutes', 'Flap window in minutes', { width: 'w-20' })} min.</>}
            </Sentence>
          </section>

          <details className="group rounded-lg border border-border">
            <summary className="cursor-pointer px-3 py-2 text-sm font-medium">Advanced: recovery and flapping</summary>
            <div className="space-y-3 border-t border-border px-3 py-3">
              <ClearLimit
                label="Recovery latency" unit="ms" autoValue={auto.degraded_rtt_clear_ms} override={f.rtt_clear_override}
                value={f.degraded_rtt_clear_ms} error={errors.degraded_rtt_clear_ms}
                onOverride={(on) => set({ rtt_clear_override: on, degraded_rtt_clear_ms: on && !f.degraded_rtt_clear_ms ? String(auto.degraded_rtt_clear_ms) : f.degraded_rtt_clear_ms })}
                onChange={(value) => set({ degraded_rtt_clear_ms: value })}
              />
              <ClearLimit
                label="Recovery packet loss" unit="%" autoValue={auto.degraded_loss_clear_pct} override={f.loss_clear_override}
                value={f.degraded_loss_clear_pct} error={errors.degraded_loss_clear_pct}
                onOverride={(on) => set({ loss_clear_override: on, degraded_loss_clear_pct: on && !f.degraded_loss_clear_pct ? String(auto.degraded_loss_clear_pct) : f.degraded_loss_clear_pct })}
                onChange={(value) => set({ degraded_loss_clear_pct: value })}
              />
              <Sentence errors={[errors.degraded_clear_checks]}>
                Back to Up from Degraded after {field('degraded_clear_checks', 'Consecutive checks within the recovery limits')} consecutive checks within the recovery limits (or a full window without a slow, lossy or unanswered check).
              </Sentence>
              <Sentence errors={[errors.up_confirm_checks]}>
                Back to Up from Down after {field('up_confirm_checks', 'Responding checks to confirm recovery')} responding check(s).
              </Sentence>
              <Sentence errors={[errors.sensor_down_confirm_checks]}>
                Remote sensors: Down after {field('sensor_down_confirm_checks', 'Consecutive failed sensor checks for Down')} consecutive failed check(s). A sensor check already retries its ping within the cycle.
              </Sentence>
              <Sentence errors={[errors.flap_stop_changes, errors.flap_stable_minutes]}>
                Flapping ends when at most {field('flap_stop_changes', 'Changes left in the window to stop flapping', { disabled: !f.flap_detection_enabled })} change(s) remain in the window, or after {field('flap_stable_minutes', 'Minutes without a change to stop flapping', { disabled: !f.flap_detection_enabled })} min without a change. While up/down or latency/loss changes flap, a change of that kind notifies only once its status has lasted that long; the other kind still notifies at once.
              </Sentence>
            </div>
          </details>

          <section aria-label="What these settings do" className="space-y-2 rounded-lg border border-border bg-surface2/30 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold">With these settings</h3>
              {isLegacyPolicy(policy) && <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted">Legacy single-check behaviour</span>}
            </div>
            <ul className="list-disc space-y-1 pl-4 text-xs text-muted">
              {[rules.degraded, rules.recovery, rules.down, rules.flapping].map((r) => <li key={r}>{r}</li>)}
            </ul>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[520px] text-left text-xs">
                <caption className="sr-only">Time to confirm a change at common ping intervals</caption>
                <thead className="text-muted">
                  <tr><th className="py-1 pr-3 font-medium">Ping interval</th><th className="py-1 pr-3 font-medium">Down confirmed</th><th className="py-1 pr-3 font-medium">Up confirmed</th><th className="py-1 pr-3 font-medium">Degraded confirmed</th><th className="py-1 pr-3 font-medium">Degraded cleared</th><th className="py-1 font-medium">Flapping can start</th></tr>
                </thead>
                <tbody>
                  {timing.map((row) => (
                    <tr key={row.intervalSeconds} className="border-t border-border/60">
                      <td className="py-1 pr-3 font-medium">{fmtSeconds(row.intervalSeconds)}</td>
                      <td className="py-1 pr-3">{row.down}</td>
                      <td className="py-1 pr-3">{row.up}</td>
                      <td className="py-1 pr-3">{row.degraded}</td>
                      <td className="py-1 pr-3">{row.degradedClear}</td>
                      <td className="py-1">{TIMING_FLAP_LABEL[row.flap]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-muted">Times run from the moment the condition begins on a sustained problem. {flapDetectionLimitText(policy)}</p>
          </section>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" onClick={() => setForm(defaultsForm())}>Restore defaults</Button>
              <Button type="button" variant="outline" size="sm" onClick={() => setForm(legacyPresetForm(f))} title="One check confirms and clears Degraded, recovery limits equal the Degraded limits, flap detection off. Down and Up counts are kept.">Legacy single-check preset</Button>
            </div>
            <div className="flex items-center gap-3">
              {invalid
                ? <span role="alert" className="text-xs text-danger">Fix the highlighted values to save.</span>
                : dirty && <span className="text-xs text-muted">Unsaved changes</span>}
              <Button type="submit" disabled={save.isPending || invalid}>
                {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save
              </Button>
            </div>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

/** A rule written as a sentence with inline number inputs; its fields' errors follow it. */
function Sentence({ children, errors }: { children: React.ReactNode; errors: Array<string | undefined> }) {
  const shown = errors.filter(Boolean)
  return (
    <div>
      <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm">{children}</p>
      {shown.map((e) => <p key={e} className="mt-0.5 text-xs text-danger">{e}</p>)}
    </div>
  )
}

function NumberInput({ label, value, error, onChange, disabled, step = '1', width = 'w-16' }: {
  label: string
  value: string
  error?: string
  onChange: (value: string) => void
  disabled?: boolean
  step?: string
  width?: string
}) {
  return (
    <Input
      type="number" step={step} inputMode={step === '1' ? 'numeric' : 'decimal'} aria-label={label} title={label} aria-invalid={!!error || undefined}
      value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}
      className={cn('inline-block h-8 px-2 text-sm', width, error && 'border-danger')}
    />
  )
}

/** A recovery limit: derived (80% of the Degraded limit, as the server computes it) unless set manually. */
function ClearLimit({ label, unit, autoValue, override, value, error, onOverride, onChange }: {
  label: string
  unit: string
  autoValue: number
  override: boolean
  value: string
  error?: string
  onOverride: (on: boolean) => void
  onChange: (value: string) => void
}) {
  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <span>{label} at or below</span>
        {override
          ? <><NumberInput label={`${label} (${unit})`} value={value} error={error} onChange={onChange} step="any" width="w-24" /><span className="text-muted">{unit}</span></>
          : <span className="font-medium">{fmt3(autoValue)} {unit} <span className="font-normal text-muted">(auto: 80% of the Degraded limit)</span></span>}
        <label className="ml-1 inline-flex items-center gap-1.5 text-xs text-muted">
          <input type="checkbox" className="h-3.5 w-3.5 accent-primary" checked={override} onChange={(e) => onOverride(e.target.checked)} />
          Set manually
        </label>
      </div>
      {error && <p className="mt-0.5 text-xs text-danger">{error}</p>}
    </div>
  )
}

function AppearanceCard() {
  const { theme, set } = useTheme()
  const { density, setDensity, setKiosk } = useDisplay()
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Theme</CardTitle>
          <CardDescription>Dark suits a NOC room and wall screens; light suits a bright office.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-3">
            <button onClick={() => set('dark')} aria-pressed={theme === 'dark'} className={`rounded-lg border-2 p-3 text-left transition ${theme === 'dark' ? 'border-primary' : 'border-border hover:border-border-strong'}`}>
              <div className="mb-2 flex h-14 gap-1.5 rounded-md bg-[rgb(10,13,19)] p-1.5">
                <div className="w-1/4 rounded bg-[rgb(17,21,30)]" />
                <div className="flex-1 rounded bg-[rgb(17,21,30)]" />
              </div>
              <div className="text-sm font-medium">Dark (NOC)</div>
              <div className="text-xs text-muted">Low glare, high contrast</div>
            </button>
            <button onClick={() => set('light')} aria-pressed={theme === 'light'} className={`rounded-lg border-2 p-3 text-left transition ${theme === 'light' ? 'border-primary' : 'border-border hover:border-border-strong'}`}>
              <div className="mb-2 flex h-14 gap-1.5 rounded-md bg-[rgb(243,245,248)] p-1.5">
                <div className="w-1/4 rounded bg-white" />
                <div className="flex-1 rounded bg-white" />
              </div>
              <div className="text-sm font-medium">Light</div>
              <div className="text-xs text-muted">Daylight reading</div>
            </button>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Display density</CardTitle>
          <CardDescription>Scales text, spacing and charts together. Saved in this browser only.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" role="radiogroup" aria-label="Display density">
            {DENSITIES.map((d) => (
              <button
                key={d.value}
                role="radio"
                aria-checked={density === d.value}
                onClick={() => setDensity(d.value)}
                className={`rounded-lg border-2 px-3 py-2.5 text-left transition ${density === d.value ? 'border-primary bg-primary/5' : 'border-border hover:border-border-strong'}`}
              >
                <div className="flex h-7 items-end font-semibold leading-none text-text" style={{ fontSize: `${d.rootPx + 4}px` }} aria-hidden>Aa</div>
                <div className="mt-1.5 whitespace-nowrap text-sm font-medium">{d.label}</div>
                <div className="mt-0.5 text-2xs text-muted">{d.hint}</div>
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-surface2/50 px-3 py-2.5">
            <div className="min-w-0">
              <div className="text-sm font-medium">NOC wall mode</div>
              <div className="text-xs text-muted">
                Full screen without the sidebar and header. For a wall screen, open any page with
                <code className="mx-1 rounded bg-surface3 px-1 font-mono text-2xs">?density=wall&amp;kiosk=1</code>
                · press Esc to leave.
              </div>
            </div>
            <Button size="sm" variant="outline" onClick={() => setKiosk(true)}>Enter wall mode</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function ProfileCard() {
  const { user, logout } = useAuth()
  const qc = useQueryClient()
  const [currentPass, setCurrentPass] = useState('')
  const [newPass, setNewPass] = useState('')
  const [confirmPass, setConfirmPass] = useState('')
  const isLocal = (user?.auth_source || 'local') === 'local'
  const check = usePasswordCheck(newPass, confirmPass, { username: user?.username, email: user?.email })

  const changePw = useMutation({
    mutationFn: async () =>
      (await api.post('/users/me/change-password', { current_password: currentPass, new_password: newPass })).data,
    onSuccess: () => {
      // The API signs every session out on a password change, this one included.
      toast.success('Password changed', 'Sign in again with your new password.')
      setCurrentPass(''); setNewPass(''); setConfirmPass('')
      logout()
    },
    onError: (e: any) => toast.error('Change failed', apiErrorMessage(e)),
  })

  type MySession = { id: string; created_at: string; last_seen_at: string; ip_address: string | null; user_agent: string | null; auth_method: string; mfa_verified: boolean }
  const sessions = useQuery<{ data: MySession[] }>({
    queryKey: ['users', 'me', 'sessions'],
    queryFn: async () => (await api.get('/users/me/sessions')).data,
  })
  const revokeSession = useMutation({
    mutationFn: async (id: string) => api.delete(`/users/me/sessions/${id}`),
    onSuccess: () => { toast.success('Session revoked'); qc.invalidateQueries({ queryKey: ['users', 'me', 'sessions'] }) },
    onError: (e: any) => toast.error('Could not revoke', apiErrorMessage(e)),
  })

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader><CardTitle>Your Profile</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          {[
            ['Username', user?.username || '—'],
            ['Email', user?.email || '—'],
            ['Full name', user?.full_name || '—'],
            ['Role', user?.role || '—'],
            ['Sign-in', (user?.auth_source || 'local').toUpperCase()],
            ['Visibility', (() => {
              if ((user?.permissions || []).includes('system.admin')) return 'All resources (full administration)'
              const own = user?.scope_tags || []
              const fromRole = (user?.role_scope_tags || []).filter((t) => !own.some((o) => o.toLowerCase() === t.toLowerCase()))
              const all = [...fromRole, ...own]
              return all.length ? `Tags: ${all.join(', ')}${fromRole.length ? ' (role tags included)' : ''}` : 'All resources your role permits'
            })()],
          ].map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-4 border-b border-border pb-2 last:border-0 last:pb-0">
              <span className="text-xs uppercase tracking-wider text-muted">{k}</span>
              <span className="text-right">{v}</span>
            </div>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Change Password</CardTitle></CardHeader>
        <CardContent>
          {isLocal ? (
            <form
              onSubmit={(e: FormEvent) => { e.preventDefault(); if (check.ok && currentPass) changePw.mutate() }}
              className="space-y-3"
            >
              <FormField label="Current password" required>
                <Input required type="password" value={currentPass} onChange={(e) => setCurrentPass(e.target.value)} autoComplete="current-password" />
              </FormField>
              <NewPasswordFields password={newPass} onPassword={setNewPass} confirm={confirmPass} onConfirm={setConfirmPass} ctx={{ username: user?.username, email: user?.email }} />
              <p className="text-xs text-muted">Changing your password signs you out of every session, including this one.</p>
              <div className="flex justify-end">
                <Button type="submit" disabled={changePw.isPending || !currentPass || !check.ok}>
                  {changePw.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Change password
                </Button>
              </div>
            </form>
          ) : (
            <p className="text-sm text-muted">
              Your account signs in via {(user?.auth_source || '').toUpperCase()}; your password is managed by that directory.
            </p>
          )}
        </CardContent>
      </Card>
      <ContactMethodsCard />
      <Card>
        <CardHeader><CardTitle>Two-step verification</CardTitle></CardHeader>
        <CardContent><MfaPanel /></CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Where you are signed in</CardTitle></CardHeader>
        <CardContent>
          <QueryBoundary query={sessions} what="your sessions" skeleton={<div className="h-16 animate-pulse rounded bg-surface2/50" />}>
            {(s) => s.data.length === 0 ? (
              <p className="text-sm text-muted">No tracked sessions. (Sessions started before this release are not listed.)</p>
            ) : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {s.data.map((row) => {
                  const current = row.id === user?.session_id
                  return (
                    <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs">
                      <div className="min-w-0">
                        <div className="font-medium">{row.ip_address || 'unknown address'} {current && <span className="ml-1 rounded bg-primary/10 px-1.5 py-0.5 text-2xs text-primary">This session</span>}</div>
                        <div className="truncate text-muted" title={row.user_agent || ''}>{row.user_agent || 'unknown client'}</div>
                        <div className="text-muted">Started {new Date(row.created_at).toLocaleString()} · last seen {relativeTime(row.last_seen_at)}</div>
                      </div>
                      {!current && <Button size="sm" variant="outline" disabled={revokeSession.isPending} onClick={() => revokeSession.mutate(row.id)}>Revoke</Button>}
                    </li>
                  )
                })}
              </ul>
            )}
          </QueryBoundary>
        </CardContent>
      </Card>
    </div>
  )
}

interface SmtpForm {
  enabled: boolean
  host: string
  port: number | ''
  encryption: 'tls' | 'ssl' | 'none'
  username: string
  password: string
  from_email: string
  from_name: string
}

const DEFAULT_SMTP: SmtpForm = {
  enabled: false,
  host: '',
  port: 587,
  encryption: 'tls',
  username: '',
  password: '',
  from_email: '',
  from_name: 'ZenPlus Alerts',
}

function SmtpCard() {
  const qc = useQueryClient()
  const { user } = useAuth()
  const { data, isLoading } = useQuery<{ smtp?: Partial<SmtpForm> }>({
    queryKey: ['settings', 'gateways'],
    queryFn: async () => (await api.get('/settings/gateways')).data,
  })
  const [form, setForm] = useState<SmtpForm>(DEFAULT_SMTP)
  const [testRecipient, setTestRecipient] = useState('')

  useEffect(() => {
    if (data?.smtp) {
      setForm({
        ...DEFAULT_SMTP,
        ...data.smtp,
        port: (data.smtp.port as number) ?? 587,
        encryption: (data.smtp.encryption as 'tls' | 'ssl' | 'none') ?? 'tls',
      })
    }
  }, [data])

  useEffect(() => {
    if (!testRecipient && user?.email) setTestRecipient(user.email)
  }, [user, testRecipient])

  const save = useMutation({
    mutationFn: async () => {
      const payload = { ...form, port: Number(form.port) || 587 }
      return (await api.put('/settings/gateways/smtp', payload)).data
    },
    onSuccess: () => {
      toast.success('SMTP settings saved')
      qc.invalidateQueries({ queryKey: ['settings', 'gateways'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const test = useMutation({
    mutationFn: async () => {
      if (!testRecipient) throw new Error('Recipient required')
      return (await api.post('/settings/gateways/smtp/test', { recipient: testRecipient })).data
    },
    onSuccess: (d: any) => toast.success('Test email sent', d?.message ?? `Check inbox for ${testRecipient}`),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-primary" />
          SMTP / Email
        </CardTitle>
        <p className="text-xs text-muted">
          Outbound email server used for alerts, scheduled reports, password resets, and other notifications.
          Also configurable from the Gateways page.
        </p>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate() }}
          className="grid grid-cols-1 gap-3 md:grid-cols-2"
        >
          <div className="md:col-span-2 flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
            <div>
              <p className="text-sm font-medium">Enabled</p>
              <p className="text-xs text-muted">When off, the system won't attempt to send any email.</p>
            </div>
            <Switch
              checked={form.enabled}
              onCheckedChange={(v) => setForm((f) => ({ ...f, enabled: !!v }))}
            />
          </div>

          <FormField label="SMTP host" className="md:col-span-2">
            <Input
              value={form.host}
              onChange={(e) => setForm({ ...form, host: e.target.value })}
              placeholder="smtp.gmail.com"
            />
          </FormField>

          <FormField label="Port">
            <Input
              type="number"
              value={form.port === '' ? '' : form.port}
              onChange={(e) => setForm({ ...form, port: e.target.value === '' ? '' : Number(e.target.value) })}
              placeholder="587"
            />
          </FormField>

          <FormField label="Encryption">
            <Select
              value={form.encryption}
              onValueChange={(v) => setForm({ ...form, encryption: v as 'tls' | 'ssl' | 'none' })}
            >
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="tls">STARTTLS (587)</SelectItem>
                <SelectItem value="ssl">SSL (465)</SelectItem>
                <SelectItem value="none">None (25)</SelectItem>
              </SelectContent>
            </Select>
          </FormField>

          <FormField label="Username">
            <Input
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              placeholder="alerts@example.com"
              autoComplete="off"
            />
          </FormField>

          <FormField label="Password / App password">
            <Input
              type="password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              placeholder={data?.smtp?.password ? '••••••••' : ''}
              autoComplete="new-password"
            />
          </FormField>

          <FormField label="From address">
            <Input
              type="email"
              value={form.from_email}
              onChange={(e) => setForm({ ...form, from_email: e.target.value })}
              placeholder="alerts@example.com"
            />
          </FormField>

          <FormField label="From display name">
            <Input
              value={form.from_name}
              onChange={(e) => setForm({ ...form, from_name: e.target.value })}
              placeholder="ZenPlus Alerts"
            />
          </FormField>

          <div className="md:col-span-2 mt-2 flex flex-col gap-2 border-t border-border pt-3 md:flex-row md:items-end md:justify-between">
            <div className="flex flex-1 items-end gap-2">
              <FormField label="Send test email to" className="flex-1">
                <Input
                  type="email"
                  value={testRecipient}
                  onChange={(e) => setTestRecipient(e.target.value)}
                  placeholder="you@example.com"
                />
              </FormField>
              <Button
                type="button"
                variant="outline"
                disabled={test.isPending || isLoading}
                onClick={() => test.mutate()}
                className="mb-[1px]"
              >
                {test.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                Send test
              </Button>
            </div>
            <Button type="submit" disabled={save.isPending || isLoading}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Save
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
