/**
 * Event alert expiry (alerting audit G-7): trap, syslog, Device Tracker and
 * configuration-change alerts that see no new event within the window close
 * by themselves, without a notification. On by default (7 days).
 * Logic: app.services.alert_expiry; model: features/escalation/eventExpiry.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Hourglass, Info, Loader2, Save } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import {
  EVENT_EXPIRY_HOURS, eventExpiryFromApi, eventExpirySentence, expiryWindowLabel, validateEventExpiry,
  type EventExpiryPolicy,
} from '@/features/escalation/eventExpiry'

const KEY = ['alerts', 'event-expiry']

export function EventExpiryCard({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({
    queryKey: KEY,
    queryFn: async () => {
      const d = (await api.get('/alerts/event-expiry')).data
      return { policy: eventExpiryFromApi(d), due: typeof d?.due_now === 'number' ? d.due_now as number : null }
    },
  })
  const [edited, setEdited] = useState<EventExpiryPolicy | null>(null)
  const save = useMutation({
    mutationFn: async (p: EventExpiryPolicy) => (await api.put('/alerts/event-expiry', p)).data,
    onSuccess: (_d, p) => {
      toast.success(p.enabled ? 'Event alert expiry saved' : 'Event alert expiry turned off')
      setEdited(null)
      qc.invalidateQueries({ queryKey: KEY })
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (q.isLoading) return <Skeleton className="h-40 w-full" />
  if (q.isError && !q.data) return <QueryFailure error={q.error} what="event alert expiry" onRetry={() => void q.refetch()} retrying={q.isFetching} />
  if (!q.data) return null
  const form = edited ?? q.data.policy
  const errors = validateEventExpiry(form)
  const dirty = JSON.stringify(form) !== JSON.stringify(q.data.policy)
  const set = (patch: Partial<EventExpiryPolicy>) => setEdited({ ...form, ...patch })
  const ro = !canManage
  const [lo, hi] = EVENT_EXPIRY_HOURS

  return (
    <Card>
      <CardContent className="space-y-4 p-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold"><Hourglass className="h-4 w-4 text-primary" aria-hidden /> Event alert expiry</h2>
            <p className="text-xs text-muted">{eventExpirySentence(form)}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Switch checked={form.enabled} disabled={ro} onCheckedChange={(v) => set({ enabled: v })} aria-label="Close event alerts automatically" />
            <span className="text-sm">{form.enabled ? 'On' : 'Off'}</span>
          </div>
        </div>

        <div className="flex items-start gap-2 rounded-md border border-border bg-surface2/50 px-3 py-2 text-xs text-text2" role="note">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
          <span>
            SNMP trap, syslog, Device Tracker (new, rogue, watched and moved endpoint) and configuration-change alerts
            have no recovery of their own. A repeat of the same event updates the open alert; once no new event
            arrives for the window, the alert is closed as “Expired — no new event within the window”. Metric,
            status, baseline and port-capacity alerts are never expired.
            {q.data.due !== null && !dirty && form.enabled
              ? ` ${q.data.due.toLocaleString()} open event alert${q.data.due === 1 ? '' : 's'} will close on the next pass.`
              : ''}
          </span>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <FormField label="Close after (hours without a new event)" error={errors.hours}
            hint={Number.isFinite(form.hours) ? `= ${expiryWindowLabel(form.hours)}` : undefined}>
            <Input type="number" min={lo} max={hi} disabled={ro || !form.enabled}
              value={Number.isFinite(form.hours) ? form.hours : ''}
              onChange={(e) => set({ hours: e.target.value.trim() === '' ? NaN : Number(e.target.value) })} />
          </FormField>
        </div>

        {canManage && (
          <div className="flex flex-wrap justify-end gap-2">
            {dirty && <Button variant="ghost" size="sm" onClick={() => setEdited(null)}>Discard</Button>}
            <Button size="sm" onClick={() => save.mutate(form)} disabled={!dirty || Object.keys(errors).length > 0 || save.isPending}>
              {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Save className="h-3.5 w-3.5" aria-hidden />} Save
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
