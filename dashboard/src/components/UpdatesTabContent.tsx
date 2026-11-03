import { regionalDate } from '@/lib/regionalTime'
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Clock,
  Download,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  RefreshCw,
  Server,
  Terminal,
  XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/Dialog'

// ─── Types ──────────────────────────────────────────────────────────────────
interface UpdateHistoryRecord {
  version: string
  from_version: string
  status: string
  changelog: string
  severity: string
  error: string
  started_at: string
  updated_at: string
  completed_at: string
}

interface RemoteSubscription {
  id: string
  name: string
  plan: string
  max_appliances: number
  max_devices: number
  used_slots: number
  available_slots: number
  is_active: boolean
  is_expired: boolean
  expires_at: string | null
  days_remaining: number | null
}

interface SchemaHealth {
  ok: boolean | null
  checked_at: string
  problem_count: number
  problems: string[]
  summary: string
}

interface UpdateStatus {
  current_version: string
  schema_health: SchemaHealth | null
  installed_at: string
  appliance_id: string
  server_url: string
  auto_update: boolean
  check_interval_hours: number
  maintenance_window_start: string
  maintenance_window_end: string
  last_check: string
  next_check: string
  timer_active: boolean
  updater_running: boolean
  last_update: UpdateHistoryRecord | null
  active_update: UpdateHistoryRecord | null
  history: UpdateHistoryRecord[]
  recent_log: string[]
  subscription: RemoteSubscription | null
  manual_install?: { state: string; target_version: string; message: string; updated_at: string } | null
  maintenance_timezone?: string
}

interface UpdatePlan {
  from_version: string
  checked_at: string
  plan_id: string
  steps: { id: string; version: string; min_version: string | null; changelog: string; severity: string }[]
}

// ─── Helpers ────────────────────────────────────────────────────────────────
function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    success: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    failed: 'bg-red-500/10 text-red-400 border-red-500/20',
    rolled_back: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
    downloading: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    applying: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
  }
  return (
    <span
      className={cn(
        'rounded-full border px-2 py-0.5 text-2xs font-semibold uppercase',
        colors[status] || 'bg-surface2 text-muted border-border',
      )}
    >
      {status.replace('_', ' ')}
    </span>
  )
}

function fmtDate(d: string | null | undefined) {
  if (!d) return '—'
  try {
    return regionalDate(new Date(d), 'datetime')
  } catch {
    return d
  }
}

/**
 * During an update the appliance stops zenplus-api while it applies code and
 * restarts services, so the status poll briefly fails with a 502/503/504 from
 * nginx (no upstream) or a no-response network error. That is expected progress,
 * not a failure — surface it as "applying / reconnecting" instead of an error.
 */
function isApiRestarting(error: unknown): boolean {
  const resp = (error as any)?.response
  if (!resp) return true // no response at all → API process is down mid-restart
  return resp.status === 502 || resp.status === 503 || resp.status === 504
}

// ─── Main component ─────────────────────────────────────────────────────────
export function UpdatesTabContent() {
  const queryClient = useQueryClient()
  const [showLog, setShowLog] = useState(false)
  const [plan, setPlan] = useState<UpdatePlan | null>(null)
  const [targetVersion, setTargetVersion] = useState('')
  const [reviewInstall, setReviewInstall] = useState(false)

  // Status query — refetches more often when an update is in flight.
  const { data: status, isLoading, isError, error } = useQuery<UpdateStatus>({
    queryKey: ['system-update-status'],
    queryFn: async () => (await api.get<UpdateStatus>('/system/update-status')).data,
    // Keep prior data on the screen while a refetch fails (the API restarts
    // mid-update), so we can show "applying / reconnecting" rather than blanking out.
    placeholderData: (prev) => prev,
    retry: false,
    refetchInterval: (query) => {
      // While the API is unreachable mid-update, retry quickly so the page
      // recovers as soon as services come back.
      if (query.state.status === 'error') return 5000
      const d = query.state.data
      return d?.updater_running || d?.active_update ? 5000 : 30000
    },
  })

  // Local form state mirrors the persisted config.
  const [config, setConfig] = useState({
    auto_update: true,
    check_interval_hours: 4,
    maintenance_window_start: '',
    maintenance_window_end: '',
  })

  useEffect(() => {
    if (status) {
      setConfig({
        auto_update: status.auto_update,
        check_interval_hours: status.check_interval_hours,
        maintenance_window_start: status.maintenance_window_start || '',
        maintenance_window_end: status.maintenance_window_end || '',
      })
    }
  }, [status])

  // Mutations
  const checkNow = useMutation({
    mutationFn: async () => (await api.post<UpdatePlan>('/system/check-update', {}, { timeout: 100000 })).data,
    onMutate: () => { setPlan(null); setReviewInstall(false) },
    onSuccess: (data) => {
      setPlan(data)
      setTargetVersion(data.steps.at(-1)?.version || '')
      toast.success(data.steps.length ? 'Update available' : 'Check complete', 'No update was installed.')
    },
    onError: (e: any) => toast.error('Check failed', apiErrorMessage(e)),
  })

  const installNow = useMutation({
    mutationFn: async () => (await api.post('/system/install-update', {
      plan_id: plan?.plan_id, target_version: targetVersion,
    }, { timeout: 110000 })).data,
    onSuccess: () => {
      setReviewInstall(false)
      setPlan(null)
      toast.success('Installation queued', `Installing the required path through v${targetVersion}.`)
      queryClient.invalidateQueries({ queryKey: ['system-update-status'] })
    },
    onError: (e: any) => {
      toast.error('Installation not started', apiErrorMessage(e))
      setReviewInstall(false)
      setPlan(null)
    },
  })

  const saveConfig = useMutation({
    mutationFn: async () => (await api.put('/system/update-config', config)).data,
    onSuccess: (data: any) => {
      if (data?.status === 'partial') {
        toast.error('Saved with warning', data.message || 'Timer override could not be applied')
      } else {
        toast.success('Update settings saved')
      }
      queryClient.invalidateQueries({ queryKey: ['system-update-status'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
      </div>
    )
  }

  const restarting = isError && isApiRestarting(error)

  // Hard failure: only when there's no status to show AND it isn't the expected
  // mid-update restart window. A genuine error (auth, 500 with a body, …) still surfaces.
  if (isError && !status && !restarting) {
    return (
      <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-6 text-sm text-red-300">
        Failed to load update status: {apiErrorMessage(error)}
      </div>
    )
  }

  // No prior status yet and the API is unreachable — expected while an update is
  // applied and services restart. Reassure rather than show an error.
  if (!status) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-blue-500/30 bg-blue-500/10 py-16 text-center">
        <Loader2 className="h-7 w-7 animate-spin text-blue-400" />
        <div>
          <p className="text-sm font-semibold text-blue-300">Update in progress — reconnecting…</p>
          <p className="mx-auto mt-1 max-w-md text-xs text-blue-400/70">
            The appliance is applying an update and briefly restarting its services.
            This page will reconnect automatically — no need to refresh.
          </p>
        </div>
      </div>
    )
  }

  const isRegistered = !!status?.appliance_id

  return (
    <div className="space-y-5">
      {status.manual_install?.state === 'failed' && !status.updater_running && (
        <div role="status" className="rounded-xl border border-red-500/30 bg-red-500/10 p-4">
          <p className="text-sm font-semibold">Requested installation stopped</p>
          <p className="mt-1 text-xs">{status.manual_install.message}</p>
          <p className="mt-1 text-xs text-muted">{fmtDate(status.manual_install.updated_at)}</p>
        </div>
      )}
      {/* Services restarting mid-update: the status poll is briefly failing (502).
          Show progress instead of an error; the page recovers on its own. */}
      {restarting && (
        <div className="flex items-center gap-3 rounded-xl border border-blue-500/30 bg-blue-500/10 p-4">
          <Loader2 className="h-5 w-5 flex-shrink-0 animate-spin text-blue-400" />
          <div>
            <p className="text-sm font-medium text-blue-300">Update is being applied — reconnecting…</p>
            <p className="text-xs text-blue-400/70">
              Services are restarting as part of the update. Showing the last known status; this page refreshes automatically.
            </p>
          </div>
        </div>
      )}

      {/* Registration prompt — defers full registration UI to the Licenses tab. */}
      {!isRegistered && (
        <div className="flex items-center gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
          <KeyRound className="h-5 w-5 flex-shrink-0 text-amber-400" />
          <div className="flex-1">
            <p className="text-sm font-medium text-amber-300">Appliance not registered</p>
            <p className="text-xs text-muted">
              Updates require a registered appliance. Enter your license key on the{' '}
              <a href="?tab=licenses" className="text-primary hover:underline">Licenses</a> tab.
            </p>
          </div>
        </div>
      )}

      {/* Active update banner */}
      {status?.active_update && (
        <div className="flex items-center gap-4 rounded-xl border border-blue-500/30 bg-blue-500/10 p-5">
          <Loader2 className="h-6 w-6 flex-shrink-0 animate-spin text-blue-400" />
          <div className="flex-1">
            <p className="text-sm font-semibold text-blue-300">
              Update in progress: v{status.active_update.from_version} → v{status.active_update.version}
            </p>
            <p className="mt-0.5 text-xs text-blue-400/70">
              Status: {status.active_update.status} · started {fmtDate(status.active_update.started_at)}
            </p>
            {status.active_update.changelog && (
              <p className="mt-1 text-xs text-muted">{status.active_update.changelog}</p>
            )}
          </div>
        </div>
      )}

      {/* Last update result */}
      {status?.last_update?.status === 'failed' && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-5">
          <div className="flex items-start gap-3">
            <XCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" />
            <div className="flex-1">
              <p className="text-sm font-semibold text-red-300">
                Last update failed: v{status.last_update.from_version} → v{status.last_update.version}
              </p>
              {status.last_update.error && (
                <p className="mt-1 break-all rounded-lg bg-red-500/5 p-2.5 font-mono text-xs text-red-400/80">
                  {status.last_update.error}
                </p>
              )}
              <p className="mt-2 text-2xs text-muted">{fmtDate(status.last_update.completed_at)}</p>
            </div>
          </div>
        </div>
      )}

      {status?.last_update?.status === 'success' && !status?.active_update && (
        <div className="flex items-center gap-3 rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4">
          <CheckCircle2 className="h-5 w-5 flex-shrink-0 text-emerald-400" />
          <div>
            <p className="text-sm font-medium text-emerald-300">
              Last update successful: v{status.last_update.version}
            </p>
            <p className="text-2xs text-muted">{fmtDate(status.last_update.completed_at)}</p>
          </div>
        </div>
      )}

      {/* Schema drift. A version number on its own is not proof the appliance
          can run that version — this appliance reported a successful update
          while its ClickHouse SNMP tables were missing, so CPU and memory read
          as "—" indefinitely with nothing on this page to say why. */}
      {status?.schema_health?.ok === false && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" />
            <div className="flex-1">
              <p className="text-sm font-semibold text-amber-300">
                Database schema does not match the installed version
              </p>
              <p className="mt-1 text-xs text-amber-200/80">
                The appliance is running v{status.current_version}, but{' '}
                {status.schema_health.problem_count} migration problem
                {status.schema_health.problem_count === 1 ? '' : 's'} remain unresolved.
                Metrics that depend on the missing tables will not be stored.
              </p>
              <ul className="mt-2 space-y-1 rounded-lg bg-amber-500/5 p-2.5">
                {status.schema_health.problems.map((problem, i) => (
                  <li key={i} className="break-all font-mono text-xs text-amber-200/80">
                    {problem}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-amber-200/70">
                The next update retries automatically. To fix it now, run{' '}
                <code className="rounded bg-amber-500/10 px-1">
                  sudo /opt/zenplus/scripts/sync-schema.py
                </code>{' '}
                on the appliance.
              </p>
              {status.schema_health.checked_at && (
                <p className="mt-2 text-2xs text-muted">
                  Checked {fmtDate(status.schema_health.checked_at)}
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* System status grid */}
      <div className="rounded-xl border border-border bg-surface p-6">
        <div className="mb-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
              <Server className="h-4 w-4 text-primary" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">System Status</h3>
              <p className="text-xs text-muted">Current appliance version and update agent state</p>
            </div>
          </div>
          <button
            onClick={() => checkNow.mutate()}
            disabled={checkNow.isPending || installNow.isPending || status?.updater_running || !!status?.active_update || !isRegistered}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            title={!isRegistered ? 'Register first' : ''}
          >
            {checkNow.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
            {checkNow.isPending ? 'Checking…' : 'Check for Updates'}
          </button>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-lg bg-surface2 p-4">
            <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Current Version</p>
            <p className="text-lg font-bold text-primary">{status?.current_version || 'Unknown'}</p>
            {status?.installed_at && (
              <p className="mt-0.5 text-2xs text-muted">Installed {fmtDate(status.installed_at)}</p>
            )}
            {status?.schema_health && (
              <p
                className={cn(
                  'mt-1 flex items-center gap-1 text-2xs',
                  status.schema_health.ok === true && 'text-emerald-400',
                  status.schema_health.ok === false && 'text-amber-400',
                  status.schema_health.ok === null && 'text-muted',
                )}
                title={status.schema_health.summary}
              >
                {status.schema_health.ok === true && <Check className="h-3 w-3" />}
                {status.schema_health.ok === false && <AlertTriangle className="h-3 w-3" />}
                {status.schema_health.ok === true
                  ? 'Schema verified'
                  : status.schema_health.ok === false
                    ? 'Schema drift'
                    : 'Schema not verified'}
              </p>
            )}
          </div>
          <div className="rounded-lg bg-surface2 p-4">
            <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Appliance ID</p>
            <p className="break-all font-mono text-xs">{status?.appliance_id || 'Not registered'}</p>
          </div>
          <div className="rounded-lg bg-surface2 p-4">
            <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Update Server</p>
            <p className="text-xs">{status?.server_url || '—'}</p>
            <p className="mt-0.5 text-2xs text-muted">Next check: {fmtDate(status?.next_check)}</p>
          </div>
          <div className="rounded-lg bg-surface2 p-4">
            <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Auto-Update Timer</p>
            <div className="flex items-center gap-2">
              <span
                className={cn('h-2 w-2 rounded-full', status?.timer_active ? 'bg-emerald-400' : 'bg-red-400')}
              />
              <p className="text-sm font-medium">
                {status?.timer_active
                  ? `Active · every ${status.check_interval_hours}h`
                  : 'Inactive'}
              </p>
            </div>
            {status?.last_check && (
              <p className="mt-0.5 text-2xs text-muted">Last: {fmtDate(status.last_check)}</p>
            )}
          </div>
        </div>
      </div>

      {plan && (
        <section aria-label="Available upgrade path" className="rounded-xl border border-border bg-surface p-6">
          <h3 className="text-sm font-semibold">{plan.steps.length ? 'Available upgrade path' : 'No eligible updates available'}</h3>
          <p className="mt-1 text-xs text-muted">Checked {fmtDate(plan.checked_at)}. Checking does not download or install updates.</p>
          {plan.from_version !== status.current_version ? (
            <p className="mt-3 text-sm text-amber-500">The installed version changed. Check again to refresh the upgrade path.</p>
          ) : plan.steps.length > 0 && (
            <>
              <ol className="my-4 space-y-3">
                <li className="text-sm text-muted">Installed: v{plan.from_version}</li>
                {plan.steps.map((step, index) => (
                  <li key={step.id} className="rounded-lg bg-surface2 p-3">
                    <p className="text-sm font-medium">{index + 1}. v{step.version} <span className="ml-2 text-xs text-muted">{index < plan.steps.length - 1 ? 'Required intermediate version' : 'Latest eligible version'}</span></p>
                    <p className="mt-1 whitespace-pre-wrap text-xs text-muted">{step.changelog}</p>
                  </li>
                ))}
              </ol>
              <p className="mb-3 text-xs text-muted">Required intermediate versions are installed in order. Each step must pass verification and health checks before the next starts.</p>
              <label htmlFor="update-target" className="block text-xs font-medium">Install through version</label>
              <select id="update-target" value={targetVersion} onChange={e => setTargetVersion(e.target.value)}
                disabled={installNow.isPending}
                className="mt-1 rounded-lg border border-border bg-surface2 px-3 py-2 text-sm">
                {plan.steps.map((step, index) => <option key={step.id} value={step.version}>
                  v{step.version}{index === plan.steps.length - 1 ? ' — complete required path' : ' — stop after this step'}
                </option>)}
              </select>
              <p className="mt-3 text-xs text-muted">{status.auto_update
                ? `Automatic installation is enabled. Scheduled runs may install eligible updates within the configured maintenance window (${status.maintenance_timezone || 'appliance local time'}).`
                : 'Automatic installation is off. Scheduled checks leave installation to you.'}</p>
              <button className="mt-4 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
                disabled={installNow.isPending || status.updater_running || !!status.active_update || !targetVersion}
                onClick={() => setReviewInstall(true)}>Review installation</button>
            </>
          )}
        </section>
      )}

      <Dialog open={reviewInstall} onOpenChange={open => { if (!installNow.isPending) setReviewInstall(open) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Install updates now?</DialogTitle>
            <DialogDescription>Monitoring services will restart. Installation begins now, even outside the automatic maintenance window, and stops if a step fails.</DialogDescription>
          </DialogHeader>
          <p className="text-sm">v{plan?.from_version} → {plan?.steps.slice(0, plan.steps.findIndex(s => s.version === targetVersion) + 1).map(s => `v${s.version}`).join(' → ')}</p>
          <p className="text-xs text-muted">Installation stops at v{targetVersion}. A newly published release will require a new check and selection.</p>
          <DialogFooter>
            <button className="rounded-lg border border-border px-4 py-2 text-sm" disabled={installNow.isPending} onClick={() => setReviewInstall(false)}>Cancel</button>
            <button className="rounded-lg bg-primary px-4 py-2 text-sm text-white disabled:opacity-50"
              disabled={installNow.isPending || plan?.from_version !== status.current_version || status.updater_running || !!status.active_update}
              onClick={() => installNow.mutate()}>{installNow.isPending ? 'Queuing…' : 'Install now'}</button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* History */}
      {status?.history && status.history.length > 0 && (
        <div className="rounded-xl border border-border bg-surface p-6">
          <div className="mb-4 flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-purple-500/10">
              <Clock className="h-4 w-4 text-purple-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">Update History</h3>
              <p className="text-xs text-muted">Recent update attempts and results</p>
            </div>
          </div>
          <div className="space-y-2">
            {status.history.map((r, i) => (
              <div key={i} className="flex items-center gap-4 rounded-lg bg-surface2 p-3.5">
                <div className="flex-shrink-0">
                  {r.status === 'success' && <CheckCircle2 className="h-5 w-5 text-emerald-400" />}
                  {r.status === 'failed' && <XCircle className="h-5 w-5 text-red-400" />}
                  {r.status === 'rolled_back' && (
                    <AlertTriangle className="h-5 w-5 text-orange-400" />
                  )}
                  {(r.status === 'downloading' || r.status === 'applying') && (
                    <Loader2 className="h-5 w-5 animate-spin text-blue-400" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">
                      v{r.from_version} → v{r.version}
                    </span>
                    <StatusBadge status={r.status} />
                    {r.severity && r.severity !== 'normal' && (
                      <span className="text-2xs font-semibold uppercase text-orange-400">
                        {r.severity}
                      </span>
                    )}
                  </div>
                  {r.changelog && (
                    <p className="mt-0.5 truncate text-xs text-muted">{r.changelog}</p>
                  )}
                  {r.error && (
                    <p className="mt-1 break-all rounded bg-red-500/5 px-2 py-1 font-mono text-xs text-red-400">
                      {r.error}
                    </p>
                  )}
                </div>
                <div className="flex-shrink-0 whitespace-nowrap text-right text-2xs text-muted">
                  <p>{fmtDate(r.started_at)}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Configuration */}
      <div className="rounded-xl border border-border bg-surface p-6">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-500/10">
            <Download className="h-4 w-4 text-blue-400" />
          </div>
          <div>
            <h3 className="text-sm font-semibold">Update Settings</h3>
            <p className="text-xs text-muted">Configure automatic update behavior</p>
          </div>
        </div>

        <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="flex items-center justify-between rounded-lg bg-surface2 p-4">
            <div>
              <p className="text-sm font-medium">Automatic Updates</p>
              <p className="text-xs text-muted">Apply updates as soon as they are available</p>
            </div>
            <Switch
              checked={config.auto_update}
              onCheckedChange={(v: boolean) => setConfig((p) => ({ ...p, auto_update: v }))}
            />
          </div>

          <div>
            <label className="mb-1.5 block text-xs uppercase tracking-wider text-muted">
              Check Interval
            </label>
            <select
              className="w-full rounded-lg border border-border bg-surface2 px-3 py-2.5 text-sm focus:border-primary focus:outline-none"
              value={config.check_interval_hours}
              onChange={(e) =>
                setConfig((p) => ({ ...p, check_interval_hours: parseInt(e.target.value, 10) }))
              }
            >
              <option value={1}>Every 1 hour</option>
              <option value={2}>Every 2 hours</option>
              <option value={4}>Every 4 hours</option>
              <option value={6}>Every 6 hours</option>
              <option value={12}>Every 12 hours</option>
              <option value={24}>Every 24 hours</option>
            </select>
          </div>

          <div>
            <label className="mb-1.5 block text-xs uppercase tracking-wider text-muted">
              Maintenance Window Start
            </label>
            <input
              type="time"
              className="w-full rounded-lg border border-border bg-surface2 px-3 py-2.5 text-sm focus:border-primary focus:outline-none"
              value={config.maintenance_window_start}
              onChange={(e) =>
                setConfig((p) => ({ ...p, maintenance_window_start: e.target.value }))
              }
            />
            <p className="mt-1 text-2xs text-muted">Empty = anytime</p>
          </div>

          <div>
            <label className="mb-1.5 block text-xs uppercase tracking-wider text-muted">
              Maintenance Window End
            </label>
            <input
              type="time"
              className="w-full rounded-lg border border-border bg-surface2 px-3 py-2.5 text-sm focus:border-primary focus:outline-none"
              value={config.maintenance_window_end}
              onChange={(e) =>
                setConfig((p) => ({ ...p, maintenance_window_end: e.target.value }))
              }
            />
            <p className="mt-1 text-2xs text-muted">Updates only applied within window</p>
          </div>
        </div>

        <div className="flex justify-end">
          <button
            onClick={() => saveConfig.mutate()}
            disabled={saveConfig.isPending}
            className="flex items-center gap-2 rounded-lg bg-primary px-5 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {saveConfig.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Check className="h-4 w-4" />
            )}
            Save Settings
          </button>
        </div>
      </div>

      {/* Agent log */}
      <div className="rounded-xl border border-border bg-surface p-6">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500/10">
              <Terminal className="h-4 w-4 text-emerald-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">Agent Log</h3>
              <p className="text-xs text-muted">Tail of /opt/zenplus/updater/logs/update.log</p>
            </div>
          </div>
          <button
            onClick={() => setShowLog((s) => !s)}
            className="flex items-center gap-1.5 text-xs text-primary hover:underline"
          >
            {showLog ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            {showLog ? 'Hide' : 'Show'} log
          </button>
        </div>
        {showLog && (
          <div className="max-h-80 overflow-y-auto rounded-lg bg-[#0d1117] p-4 font-mono text-xs text-emerald-400">
            {status?.recent_log && status.recent_log.length > 0 ? (
              status.recent_log.map((line, i) => (
                <div
                  key={i}
                  className={cn(
                    'whitespace-pre-wrap break-all py-0.5',
                    line.includes('[ERROR]') && 'text-red-400',
                    line.includes('[WARNING]') && 'text-yellow-400',
                  )}
                >
                  {line}
                </div>
              ))
            ) : (
              <p className="text-muted">No log entries yet</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
