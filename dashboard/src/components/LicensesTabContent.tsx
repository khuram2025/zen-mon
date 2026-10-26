import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Boxes, KeyRound, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import {
  invalidateLicenseCapacity, useLicenseCapacity, type LicenseCapacity,
} from '@/hooks/useLicenseCapacity'

interface NodeLicense {
  total_node_cap: number
  used_node_count: number
  available_nodes: number
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
  license: NodeLicense | null
}

interface UpdateStatus {
  appliance_id: string
  current_version: string
  installed_at: string
  server_url: string
  subscription: RemoteSubscription | null
}

function fmtDate(d: string | null | undefined) {
  if (!d) return '—'
  try { return new Date(d).toLocaleString() } catch { return d }
}

export function LicensesTabContent() {
  const qc = useQueryClient()
  const [licenseKey, setLicenseKey] = useState('')

  // Shares the React Query cache key with UpdatesTabContent — fetched once.
  const { data: status, isLoading, isError, error } = useQuery<UpdateStatus>({
    queryKey: ['system-update-status'],
    queryFn: async () => (await api.get<UpdateStatus>('/system/update-status')).data,
    refetchInterval: 30000,
  })

  const register = useMutation({
    mutationFn: async () => {
      const trimmed = licenseKey.trim()
      if (!trimmed) throw new Error('License key is required')
      return (await api.post<{ appliance_id: string }>('/system/register', { license_key: trimmed })).data
    },
    onSuccess: (data: any) => {
      toast.success('Appliance registered', `ID: ${data?.appliance_id || ''}`)
      setLicenseKey('')
      qc.invalidateQueries({ queryKey: ['system-update-status'] })
      invalidateLicenseCapacity(qc)
    },
    onError: (e: any) => toast.error('Registration failed', apiErrorMessage(e)),
  })

  const refreshSub = useMutation({
    mutationFn: async () => (await api.post('/system/refresh-subscription', {})).data,
    onSuccess: () => {
      toast.success('Subscription refreshed from zentryc.com')
      qc.invalidateQueries({ queryKey: ['system-update-status'] })
      invalidateLicenseCapacity(qc)
    },
    onError: (e: any) => toast.error('Refresh failed', apiErrorMessage(e)),
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
      </div>
    )
  }

  if (isError) {
    return (
      <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-6 text-sm text-red-300">
        Failed to load license status: {apiErrorMessage(error)}
      </div>
    )
  }

  const isRegistered = !!status?.appliance_id
  const sub = status?.subscription

  return (
    <div className="space-y-5">
      <NodeCapacityCard />

      {/* Registration */}
      {!isRegistered ? (
        <div className="rounded-xl border border-amber-500/30 bg-surface p-6">
          <div className="mb-4 flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-amber-500/10">
              <KeyRound className="h-4 w-4 text-amber-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">Appliance Not Registered</h3>
              <p className="text-xs text-muted">
                Enter your license key to register this appliance with zentryc.com
              </p>
            </div>
          </div>
          <div className="flex gap-3">
            <input
              type="text"
              className="flex-1 rounded-lg border border-border bg-surface2 px-4 py-2.5 font-mono text-sm focus:border-amber-400 focus:outline-none"
              placeholder="Enter license key"
              value={licenseKey}
              onChange={(e) => setLicenseKey(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && register.mutate()}
            />
            <button
              onClick={() => register.mutate()}
              disabled={register.isPending || !licenseKey.trim()}
              className="flex items-center gap-2 rounded-lg bg-amber-500 px-5 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {register.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
              Register
            </button>
          </div>
        </div>
      ) : (
        <div className="rounded-xl border border-border bg-surface p-6">
          <div className="mb-4 flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500/10">
              <KeyRound className="h-4 w-4 text-emerald-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">Appliance Registered</h3>
              <p className="text-xs text-muted">
                This appliance is linked to zentryc.com under the appliance ID below.
              </p>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="rounded-lg bg-surface2 p-4">
              <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Appliance ID</p>
              <p className="break-all font-mono text-xs">{status?.appliance_id}</p>
            </div>
            <div className="rounded-lg bg-surface2 p-4">
              <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Registered Server</p>
              <p className="text-xs">{status?.server_url || '—'}</p>
            </div>
          </div>
        </div>
      )}

      {/* Subscription card */}
      {isRegistered && (
        <div className="rounded-xl border border-border bg-surface p-6">
          <div className="mb-4 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-purple-500/10">
                <KeyRound className="h-4 w-4 text-purple-400" />
              </div>
              <div>
                <h3 className="text-sm font-semibold">Subscription</h3>
                <p className="text-xs text-muted">
                  Latest snapshot from zentryc.com — refreshes on each check-in
                </p>
              </div>
            </div>
            <button
              onClick={() => refreshSub.mutate()}
              disabled={refreshSub.isPending}
              className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-surface2 disabled:opacity-50"
            >
              {refreshSub.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )}
              Refresh
            </button>
          </div>
          {sub ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-lg bg-surface2 p-4">
                <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Plan</p>
                <p className="text-sm font-semibold">{sub.name || sub.plan || '—'}</p>
                {sub.plan && <p className="text-2xs text-muted">{sub.plan}</p>}
              </div>
              <SubscriptionNodesTile sub={sub} />
              <div className="rounded-lg bg-surface2 p-4">
                <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Status</p>
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      'h-2 w-2 rounded-full',
                      sub.is_active && !sub.is_expired ? 'bg-emerald-400' : 'bg-red-400',
                    )}
                  />
                  <p className="text-sm font-medium">
                    {sub.is_expired ? 'Expired' : sub.is_active ? 'Active' : 'Inactive'}
                  </p>
                </div>
              </div>
              <div className="rounded-lg bg-surface2 p-4">
                <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Expires</p>
                <p className="text-sm font-medium">{sub.expires_at ? fmtDate(sub.expires_at) : 'Never'}</p>
                {sub.days_remaining != null && (
                  <p className="text-2xs text-muted">{sub.days_remaining} days remaining</p>
                )}
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">
              No subscription data cached yet. Click <em>Refresh</em> to query zentryc.com.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

const STATE_LABEL: Record<LicenseCapacity['state'], string> = {
  ok: 'Within license',
  near_limit: 'Nearly full',
  full: 'Capacity reached',
  over_limit: 'Over capacity',
  expired: 'License expired',
  inactive: 'License inactive',
  unmetered: 'Not metered',
}

function stateTone(cap: LicenseCapacity) {
  if (!cap.enforced) return 'muted'
  if (!cap.can_add) return 'red'
  return cap.state === 'near_limit' ? 'amber' : 'emerald'
}

const TONES = {
  red: { badge: 'bg-red-500/10 text-red-400 border-red-500/30', bar: 'bg-red-500', box: 'border-red-500/30' },
  amber: { badge: 'bg-amber-500/10 text-amber-400 border-amber-500/30', bar: 'bg-amber-500', box: 'border-amber-500/30' },
  emerald: { badge: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30', bar: 'bg-emerald-500', box: 'border-border' },
  muted: { badge: 'bg-surface2 text-muted border-border', bar: 'bg-primary', box: 'border-border' },
} as const

/** Local license usage: devices + servers against the licensed node count. */
function NodeCapacityCard() {
  const { data: cap, isLoading, isError, error } = useLicenseCapacity()

  if (isLoading) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-border bg-surface p-6">
        <Loader2 className="h-5 w-5 animate-spin text-primary" />
      </div>
    )
  }
  if (isError || !cap) {
    return (
      <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
        Failed to load license capacity: {apiErrorMessage(error)}
      </div>
    )
  }

  const tone = TONES[stateTone(cap)]
  const pct = cap.limit ? Math.min(100, Math.round((cap.used / Math.max(1, cap.limit)) * 100)) : 0

  return (
    <div className={cn('rounded-xl border bg-surface p-6', tone.box)} data-testid="license-capacity-card">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
            <Boxes className="h-4 w-4 text-primary" />
          </div>
          <div>
            <h3 className="text-sm font-semibold">Node capacity</h3>
            <p className="text-xs text-muted">
              {cap.source === 'evaluation'
                ? 'Evaluation allowance. Register a license to raise it.'
                : 'Devices and servers monitored by this appliance'}
            </p>
          </div>
        </div>
        <span className={cn('rounded-full border px-2.5 py-0.5 text-xs font-medium', tone.badge)}>
          {STATE_LABEL[cap.state]}
        </span>
      </div>

      {cap.limit != null ? (
        <div className="mb-4">
          <div className="mb-1.5 flex items-baseline justify-between gap-3">
            <p className="text-2xl font-semibold tabular-nums">
              {cap.used.toLocaleString()}
              <span className="text-sm font-normal text-muted"> / {cap.limit.toLocaleString()} nodes</span>
            </p>
            <p className="text-xs text-muted tabular-nums">
              {(cap.available ?? 0).toLocaleString()} available
            </p>
          </div>
          <div
            className="h-2 overflow-hidden rounded-full bg-surface2"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={cap.limit}
            aria-valuenow={cap.used}
            aria-label="Licensed nodes in use"
          >
            <div className={cn('h-full rounded-full transition-all', tone.bar)} style={{ width: `${pct}%` }} />
          </div>
        </div>
      ) : (
        <p className="mb-4 text-2xl font-semibold tabular-nums">
          {cap.used.toLocaleString()}
          <span className="text-sm font-normal text-muted"> nodes in use</span>
        </p>
      )}

      <p className={cn('mb-4 text-sm', cap.enforced && !cap.can_add ? 'text-red-300' : 'text-muted')}>
        {cap.message}
      </p>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <UsageTile label="Devices" value={cap.usage.devices} hint="one node each" />
        <UsageTile label="Servers" value={cap.usage.servers} hint="one node each" />
        <UsageTile label="Linked servers" value={cap.usage.linked_servers} hint="share their device's node" />
        <UsageTile label="Controller-managed" value={cap.usage.controller_children} hint="not counted" />
      </div>

      {!!cap.pool_other_usage && cap.pool_total != null && (
        <p className="mt-3 text-xs text-muted">
          Shared subscription pool: {cap.pool_total.toLocaleString()} nodes, of which{' '}
          {cap.pool_other_usage.toLocaleString()} are used by other appliances.
        </p>
      )}

      <p className="mt-3 text-xs text-muted">
        One node is one device or one server; a server linked to its device counts once. When the
        license is full, adding devices or servers, discovery imports and new agent enrollments are
        refused until the license is extended. Everything already monitored keeps running.
        {cap.snapshot_at && <> License data from {new Date(cap.snapshot_at).toLocaleString()}.</>}
      </p>
    </div>
  )
}

function UsageTile({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div className="rounded-lg bg-surface2 p-3">
      <p className="mb-1 text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p className="text-sm font-semibold tabular-nums">{value.toLocaleString()}</p>
      <p className="text-2xs text-muted">{hint}</p>
    </div>
  )
}

/** Subscription-wide nodes, from the same calculation as Node capacity: this
 * appliance's live devices + servers plus the other appliances' share from the
 * last check-in. zentryc.com's own used_node_count only moves on a check-in,
 * so it is the fallback while the capacity has not loaded. */
function SubscriptionNodesTile({ sub }: { sub: RemoteSubscription }) {
  const { data: cap } = useLicenseCapacity()
  const live = cap && cap.pool_total != null && cap.pool_used != null
  const used = live ? cap!.pool_used! : (sub.license?.used_node_count ?? 0)
  const total = live ? cap!.pool_total! : (sub.license?.total_node_cap ?? 0)
  const available = live ? (cap!.pool_available ?? 0) : (sub.license?.available_nodes ?? 0)
  return (
    <div className="rounded-lg bg-surface2 p-4" data-testid="subscription-nodes">
      <p className="mb-1 text-2xs uppercase tracking-wider text-muted">Subscription nodes</p>
      <p className="text-sm font-semibold tabular-nums">{used}/{total}</p>
      <p className="text-2xs text-muted">{available} available</p>
      {live && (
        <p className="text-2xs text-muted">
          {cap!.pool_other_usage
            ? `${cap!.used} here · ${cap!.pool_other_usage} on other appliances`
            : `${cap!.usage.devices} devices + ${cap!.usage.servers} servers`}
        </p>
      )}
    </div>
  )
}
