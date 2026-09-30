/**
 * Suppression workspace (F06, A04, A08): every silence the user can see, with
 * the condition, reason, owner, expiry and the open alerts it currently mutes.
 * Removal needs alerts.acknowledge (indefinite silences: alerts.manage) and a
 * confirmation stating what will start notifying again.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { BellOff, Infinity as InfinityIcon, Loader2, RefreshCw, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { ENTITY_ICON, ENTITY_KIND_LABEL, focusRing } from './AlertBits'
import { fmtAgo, fmtAlertDate, parseTs, useApplianceNow } from './format'
import type { EntityKind, Silence } from './types'

function entityHref(kind: EntityKind, id: string | null): string | null {
  if (!id) return null
  switch (kind) {
    case 'device': return `/devices/${id}`
    case 'server': return `/servers/${id}`
    case 'service_check': return `/services/${id}`
    case 'sensor': return `/sensors/${id}`
    default: return null
  }
}

export function SilencesPanel() {
  const can = useCan()
  const qc = useQueryClient()
  const [includeExpired, setIncludeExpired] = useState(false)
  const [target, setTarget] = useState<Silence | null>(null)

  const q = useQuery<{ items: Silence[] }>({
    queryKey: ['alerts', 'silences', includeExpired],
    queryFn: async () => (await api.get('/alerts/silences', { params: includeExpired ? { include_expired: true } : {} })).data,
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  })
  const now = useApplianceNow(null)
  const items = q.data?.items ?? []

  const remove = useMutation({
    mutationFn: async (id: string) => api.delete(`/alerts/silences/${id}`),
    onSuccess: () => {
      toast.success('Silence removed', 'The condition will notify again on its next evaluation.')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      setTarget(null)
    },
    onError: (e) => toast.error('Could not remove silence', apiErrorMessage(e)),
  })

  const canRemove = (s: Silence) => can('alerts.acknowledge') && (!s.forever || can('alerts.manage'))

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-3xl text-sm text-muted">
          Silences suppress notifications for a condition. They never resolve incidents — muted alerts stay open as
          “Suppressed — condition still active”. Maintenance windows and quiet hours are managed separately on devices and channels.
        </p>
        <div className="flex items-center gap-2">
          <label className="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" checked={includeExpired} onChange={(e) => setIncludeExpired(e.target.checked)} className="h-4 w-4" />
            Include expired
          </label>
          <Button size="sm" variant="outline" className={focusRing} onClick={() => q.refetch()} disabled={q.isFetching} aria-label="Refresh silences">
            <RefreshCw className={cn('h-3.5 w-3.5', q.isFetching && 'animate-spin')} /> Refresh
          </Button>
        </div>
      </div>

      {q.isError && q.data && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          Refresh failed — showing the last loaded list. <button className="underline" onClick={() => q.refetch()}>Retry</button>
        </div>
      )}

      {q.isError && !q.data ? (
        <div className="rounded-md border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
          Could not load silences: {apiErrorMessage(q.error)}{' '}
          <Button size="sm" variant="outline" className="ml-2" onClick={() => q.refetch()}>Retry</Button>
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border border-border">
          <Table className="min-w-[960px]">
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Entity</Th>
                <Th>Condition</Th>
                <Th>Reason</Th>
                <Th>Owner</Th>
                <Th>Created</Th>
                <Th>Expires</Th>
                <Th className="text-right">Open alerts muted</Th>
                <Th className="text-right"><span className="sr-only">Actions</span></Th>
              </Tr>
            </THead>
            <TBody>
              {q.isLoading ? (
                <Tr><Td colSpan={8} className="py-10 text-center text-muted"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></Td></Tr>
              ) : items.length === 0 ? (
                <Tr>
                  <Td colSpan={8} className="py-10 text-center text-sm text-muted">
                    <BellOff className="mx-auto mb-2 h-5 w-5" aria-hidden />
                    No {includeExpired ? '' : 'active '}silences in your visible scope.
                  </Td>
                </Tr>
              ) : items.map((s) => {
                const Icon = ENTITY_ICON[s.entity.kind] ?? BellOff
                const href = entityHref(s.entity.kind, s.entity.id)
                const untilMs = parseTs(s.until)
                const expired = !s.forever && untilMs != null && untilMs <= now
                return (
                  <Tr key={s.id} className={cn(expired && 'opacity-60')}>
                    <Td>
                      <div className="flex items-start gap-2">
                        <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted" aria-hidden />
                        <div className="min-w-0">
                          {href ? (
                            <Link to={href} className={cn('block truncate text-sm font-medium hover:text-primary hover:underline', focusRing)}>{s.entity.name}</Link>
                          ) : (
                            <span className="block truncate text-sm font-medium">{s.entity.name}</span>
                          )}
                          <span className="text-xs text-muted">{ENTITY_KIND_LABEL[s.entity.kind] ?? s.entity.kind}</span>
                        </div>
                      </div>
                    </Td>
                    <Td className="max-w-[260px]">
                      <div className="text-sm">{s.condition_label || s.dedupe || '—'}</div>
                      {s.rule_name && <div className="text-xs text-muted">Rule: {s.rule_name}</div>}
                    </Td>
                    <Td className="max-w-[240px] text-sm">{s.reason || <span className="text-muted">No reason given</span>}</Td>
                    <Td className="text-sm">{s.created_by?.name ?? <span className="text-muted">Unknown</span>}</Td>
                    <Td className="whitespace-nowrap text-sm" title={fmtAlertDate(s.created_at, { seconds: true, year: 'always' })}>
                      {fmtAgo(s.created_at, now)}
                    </Td>
                    <Td className="whitespace-nowrap text-sm">
                      {s.forever ? (
                        <span className="inline-flex items-center gap-1 rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-xs font-medium text-warning">
                          <InfinityIcon className="h-3.5 w-3.5" aria-hidden /> Indefinite
                        </span>
                      ) : expired ? (
                        <span className="text-muted">Expired {fmtAlertDate(s.until)}</span>
                      ) : (
                        <span title={fmtAlertDate(s.until, { seconds: true, year: 'always' })}>{fmtAlertDate(s.until)}</span>
                      )}
                    </Td>
                    <Td className="text-right">
                      {s.open_alerts > 0 && s.alert_id ? (
                        <Link to={`/alerts/${s.alert_id}`} className={cn('font-semibold tabular-nums text-primary hover:underline', focusRing)} aria-label={`${s.open_alerts} open alerts muted — open the alert`}>
                          {s.open_alerts.toLocaleString()}
                        </Link>
                      ) : (
                        <span className="tabular-nums">{s.open_alerts.toLocaleString()}</span>
                      )}
                    </Td>
                    <Td className="text-right">
                      {canRemove(s) && !expired && (
                        <Button
                          size="sm" variant="outline" className={focusRing}
                          onClick={() => setTarget(s)}
                          aria-label={`Remove silence for ${s.entity.name}: ${s.condition_label ?? ''}`}
                        >
                          <Trash2 className="h-3.5 w-3.5" /> Remove
                        </Button>
                      )}
                    </Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        </div>
      )}

      <ConfirmDialog
        open={!!target}
        onOpenChange={(v) => { if (!v) setTarget(null) }}
        title="Remove silence?"
        description={target ? (
          <>
            Notifications for <strong className="text-text">{target.condition_label || 'this condition'}</strong> on{' '}
            <strong className="text-text">{target.entity.name}</strong> will resume.{' '}
            {target.open_alerts > 0 ? `${target.open_alerts} open alert${target.open_alerts === 1 ? '' : 's'} will return to the active backlog.` : ''}
            {' '}The removal is audited with your name.
          </>
        ) : undefined}
        confirmText="Remove silence"
        destructive
        loading={remove.isPending}
        onConfirm={() => { if (target) remove.mutate(target.id) }}
      />
    </div>
  )
}
