import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'
import { api } from '@/lib/api'
import { describeSkew, serverNow, useClockOffset } from '@/lib/serverClock'
import type { SystemTime } from './types'
import { exactTime, isUnsupported } from './format'

/** Appliance "now", re-evaluated every `intervalMs` and whenever calibration moves. */
export function useServerNowTick(intervalMs = 1000): number {
  const { offsetMs } = useClockOffset()
  const [now, setNow] = useState(() => serverNow())
  useEffect(() => {
    setNow(serverNow())
    const h = window.setInterval(() => setNow(serverNow()), intervalMs)
    return () => window.clearInterval(h)
  }, [intervalMs, offsetMs])
  return now
}

/** Appliance time diagnostics. Older appliances have no endpoint (404 → null). */
export function useSystemTime(enabled: boolean) {
  return useQuery<SystemTime | null>({
    queryKey: ['system-time'],
    queryFn: async () => {
      try {
        return (await api.get('/system/time')).data as SystemTime
      } catch (e) {
        if (isUnsupported(e)) return null
        throw e
      }
    },
    enabled,
    retry: false,
    staleTime: 60_000,
    refetchInterval: enabled ? 120_000 : false,
  })
}

/**
 * States a browser/appliance clock disagreement (B01). This is separate from
 * sensor staleness: freshness is always judged on the appliance clock, so a
 * skewed browser no longer makes fresh results look overdue.
 */
export function ClockSkewBanner() {
  const { offsetMs, skewed } = useClockOffset()
  const sys = useSystemTime(skewed)
  if (!skewed) return null
  const t = sys.data
  return (
    <div role="status" className="flex items-start gap-3 rounded-lg border border-info/40 bg-info/10 px-3 py-2.5 text-[12.5px]">
      <Clock className="mt-0.5 h-4 w-4 shrink-0 text-info" />
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold uppercase tracking-wider text-info">Clock disagreement</div>
        <p className="mt-0.5 leading-relaxed">
          This browser's clock is {describeSkew(offsetMs)} the appliance. Times, ranges and freshness on this page use the
          appliance clock ({exactTime(serverNow(), false)}); this is not a monitoring fault.
        </p>
        {t && (
          <p className="mt-1 text-[11px] text-muted">
            {t.ntp_synchronized === false
              ? `The appliance reports its clock is not NTP-synchronized${t.ntp_service === 'inactive' ? ' (time synchronization is disabled)' : ''}. Correct the appliance time through a planned operational change; existing history is not rewritten.`
              : t.ntp_synchronized === true
                ? 'The appliance clock is NTP-synchronized, so the browser device clock is the likely source of the difference.'
                : 'The appliance could not report its time-synchronization status.'}
            {t.timezone ? ` Appliance timezone: ${t.timezone}.` : ''}
          </p>
        )}
      </div>
    </div>
  )
}
