/**
 * Response targets: acknowledgement minutes per priority (system setting
 * alert_response_targets). An open, unacknowledged incident past its target is
 * "Ack overdue" in the Alert Center. Empty = no target.
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, RotateCcw, Save } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { useResponseTargets } from '@/features/escalation/api'
import { DEFAULT_RESPONSE_TARGETS, PRIORITIES, PRIORITY_META, fmtMinutes, minutesOrNull, validateTargetMinutes } from '@/features/escalation/model'
import { MinutesField, PriorityBadge } from '@/features/escalation/ui'
import type { Priority, ResponseTargets } from '@/features/escalation/types'

type Form = Record<Priority, string>
const toForm = (t: ResponseTargets): Form => Object.fromEntries(PRIORITIES.map((p) => [p, t[p] == null ? '' : String(t[p])])) as Form

export function ResponseTargetsTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useResponseTargets()
  // null = showing the stored values; the first edit copies them into the form.
  const [edited, setForm] = useState<Form | null>(null)
  const form = edited ?? (q.data ? toForm(q.data) : null)

  const save = useMutation({
    mutationFn: async (f: Form) => (await api.put('/alerts/response-targets', Object.fromEntries(PRIORITIES.map((p) => [p, minutesOrNull(f[p])])))).data,
    onSuccess: () => {
      toast.success('Response targets saved', 'Open incidents use the new targets on their next refresh.')
      setForm(null)
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (q.isLoading) return <Skeleton className="h-56 w-full" />
  if (q.isError && !q.data) return <QueryFailure error={q.error} what="response targets" onRetry={() => void q.refetch()} retrying={q.isFetching} />
  if (!form || !q.data) return null
  const errors = Object.fromEntries(PRIORITIES.map((p) => [p, validateTargetMinutes(form[p])])) as Record<Priority, string | null>
  const invalid = PRIORITIES.some((p) => errors[p])
  const dirty = PRIORITIES.some((p) => minutesOrNull(form[p]) !== q.data![p])

  return (
    <Card>
      <CardContent className="space-y-4 p-4">
        <div>
          <h2 className="text-sm font-semibold">Acknowledgement targets</h2>
          <p className="text-xs text-muted">How quickly someone must acknowledge an incident of each priority. Incidents past the target show as “Ack overdue”; escalation timing itself is set on each policy.</p>
        </div>
        <ul className="divide-y divide-border rounded-md border border-border">
          {PRIORITIES.map((p) => (
            <li key={p} className="grid gap-2 px-3 py-3 sm:grid-cols-[180px_minmax(0,1fr)_auto] sm:items-center">
              <div className="flex items-center gap-2"><PriorityBadge priority={p} /></div>
              <p className="text-xs text-muted">{PRIORITY_META[p].trigger}</p>
              <div>
                <MinutesField
                  ariaLabel={`${PRIORITY_META[p].label} acknowledgement target in minutes`}
                  value={form[p]}
                  disabled={!canManage}
                  invalid={!!errors[p]}
                  placeholder="none"
                  onChange={(v) => setForm({ ...form, [p]: v })}
                />
                {errors[p] && <p className="mt-1 text-xs text-danger">{errors[p]}</p>}
              </div>
            </li>
          ))}
        </ul>
        <p className="text-xs text-muted">Recommended: {PRIORITIES.map((p) => `${p} ${DEFAULT_RESPONSE_TARGETS[p] ? fmtMinutes(DEFAULT_RESPONSE_TARGETS[p]) : 'no target'}`).join(' · ')}.</p>
        {canManage && (
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => setForm(toForm(DEFAULT_RESPONSE_TARGETS))}><RotateCcw className="h-3.5 w-3.5" aria-hidden /> Use recommended</Button>
            <Button size="sm" onClick={() => save.mutate(form)} disabled={!dirty || invalid || save.isPending}>
              {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Save className="h-3.5 w-3.5" aria-hidden />} Save targets
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
