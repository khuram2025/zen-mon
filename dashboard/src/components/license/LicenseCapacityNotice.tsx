import { Link } from 'react-router-dom'
import { AlertTriangle, KeyRound } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { isBlocked, useLicenseCapacity, type LicenseCapacity } from '@/hooks/useLicenseCapacity'

export const LICENSES_PATH = '/settings/general?tab=licenses'

const TITLES: Record<string, string> = {
  full: 'License capacity reached',
  over_limit: 'License capacity reached',
  expired: 'License expired',
  inactive: 'License inactive',
  near_limit: 'License nearly full',
}

/** Shown wherever devices or servers are added. Silent while there is room;
 * amber when nearly full; red once nothing more can be added. Pass
 * `capacity` when the caller already has it, otherwise it is fetched. */
export function LicenseCapacityNotice({
  capacity, className, showNearLimit = true,
}: {
  capacity?: LicenseCapacity
  className?: string
  showNearLimit?: boolean
}) {
  const query = useLicenseCapacity(!capacity)
  const cap = capacity ?? query.data
  const can = useCan()
  if (!cap || !cap.enforced) return null
  const blocked = isBlocked(cap)
  if (!blocked && !(showNearLimit && cap.state === 'near_limit')) return null

  return (
    <div
      role={blocked ? 'alert' : 'status'}
      data-testid="license-capacity-notice"
      className={cn(
        'flex items-start gap-3 rounded-lg border p-3 text-xs',
        blocked ? 'border-red-500/30 bg-red-500/10' : 'border-amber-500/30 bg-amber-500/10',
        className,
      )}
    >
      <AlertTriangle className={cn('mt-0.5 h-4 w-4 shrink-0', blocked ? 'text-red-400' : 'text-amber-400')} />
      <div className="min-w-0 flex-1 space-y-0.5">
        <p className="font-semibold">{TITLES[cap.state] ?? 'License'}</p>
        <p className="text-muted">{cap.message}</p>
        {cap.limit != null && (
          <p className="text-muted">
            {cap.used} of {cap.limit} nodes in use · {cap.usage.devices} devices, {cap.usage.servers} servers
          </p>
        )}
      </div>
      {can('system.admin') && (
        <Link
          to={LICENSES_PATH}
          className="flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 font-medium hover:bg-surface2"
        >
          <KeyRound className="h-3 w-3" /> Licenses
        </Link>
      )}
    </div>
  )
}

/** Title for disabled "Add" buttons. */
export function capacityBlockedTitle(cap: LicenseCapacity | undefined): string | undefined {
  return isBlocked(cap) ? cap!.message : undefined
}
