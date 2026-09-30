import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'
import { api } from '@/lib/api'
import { describeSkew, useClockOffset } from '@/lib/serverClock'

type SystemTime = {
  server_time?: string
  timezone?: string | null
  ntp_synchronized?: boolean | null
  ntp_service?: 'active' | 'inactive' | null
  source?: string
}

/** B01: states a browser/appliance clock disagreement instead of silently correcting it. */
export function ClockSkewBanner() {
  const { offsetMs, skewed } = useClockOffset()
  const { data: sys } = useQuery<SystemTime | null>({
    queryKey: ['system', 'time'],
    queryFn: async () => {
      try {
        return (await api.get('/system/time')).data
      } catch (e: any) {
        // Older appliances have no /system/time; the banner still works without it.
        if (e?.response?.status === 404 || e?.response?.status === 405) return null
        throw e
      }
    },
    enabled: skewed,
    retry: false,
    staleTime: 5 * 60_000,
  })
  if (!skewed) return null

  let ntp: string | null = null
  if (sys?.ntp_synchronized === false) {
    ntp = `The appliance reports its clock is not NTP-synchronized${sys.ntp_service === 'inactive' ? ' (NTP service inactive)' : ''} — ask an administrator to correct the appliance time.`
  } else if (sys?.ntp_synchronized === true) {
    ntp = "The appliance reports NTP synchronization, so this computer's clock is the likely cause."
  }

  return (
    <div role="status" className="flex items-start gap-2.5 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-text">
      <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
      <div className="min-w-0">
        <span className="font-medium">
          This browser's clock is {describeSkew(offsetMs)} the appliance. Times use the appliance clock.
        </span>
        {ntp && <span className="text-muted"> {ntp}</span>}
        {sys?.timezone && <span className="text-muted"> Appliance timezone: {sys.timezone}.</span>}
      </div>
    </div>
  )
}
