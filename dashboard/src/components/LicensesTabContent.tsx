import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'

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
    },
    onError: (e: any) => toast.error('Registration failed', apiErrorMessage(e)),
  })

  const refreshSub = useMutation({
    mutationFn: async () => (await api.post('/system/refresh-subscription', {})).data,
    onSuccess: () => {
      toast.success('Subscription refreshed from zentryc.com')
      qc.invalidateQueries({ queryKey: ['system-update-status'] })
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
              <p className="mb-1 text-[10px] uppercase tracking-wider text-muted">Appliance ID</p>
              <p className="break-all font-mono text-xs">{status?.appliance_id}</p>
            </div>
            <div className="rounded-lg bg-surface2 p-4">
              <p className="mb-1 text-[10px] uppercase tracking-wider text-muted">Registered Server</p>
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
                <p className="mb-1 text-[10px] uppercase tracking-wider text-muted">Plan</p>
                <p className="text-sm font-semibold">{sub.name || sub.plan || '—'}</p>
                {sub.plan && <p className="text-[10px] text-muted">{sub.plan}</p>}
              </div>
              <div className="rounded-lg bg-surface2 p-4">
                <p className="mb-1 text-[10px] uppercase tracking-wider text-muted">Nodes</p>
                <p className="text-sm font-semibold">
                  {(sub.license?.used_node_count ?? 0)}/{(sub.license?.total_node_cap ?? 0)}
                </p>
                <p className="text-[10px] text-muted">
                  {(sub.license?.available_nodes ?? 0)} available
                </p>
              </div>
              <div className="rounded-lg bg-surface2 p-4">
                <p className="mb-1 text-[10px] uppercase tracking-wider text-muted">Status</p>
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
                <p className="mb-1 text-[10px] uppercase tracking-wider text-muted">Expires</p>
                <p className="text-sm font-medium">{sub.expires_at ? fmtDate(sub.expires_at) : 'Never'}</p>
                {sub.days_remaining != null && (
                  <p className="text-[10px] text-muted">{sub.days_remaining} days remaining</p>
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
