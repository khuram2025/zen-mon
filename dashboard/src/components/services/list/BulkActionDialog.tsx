import { useEffect, useMemo, useState } from 'react'
import { CheckCircle2, EyeOff, Loader2, XCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { serverNow } from '@/lib/serverClock'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import type { ServiceCheckGroup } from '@/types'
import { isRouteMissing } from './model'

export type BulkAction = 'pause' | 'resume' | 'test' | 'add_tags' | 'remove_tags' | 'set_group' | 'maintenance' | 'delete'
export type BulkResult = { id: string; name: string; ok: boolean; error?: string }
export type BulkItem = { id: string; name: string; visible: boolean }

export const BULK_TEST_CAP = 25

const ACTION_META: Record<BulkAction, { verb: string; confirm: string; explain: string; destructive?: boolean }> = {
  pause: {
    verb: 'Pause',
    confirm: 'Pause monitors',
    explain: 'Paused monitors stop probing and alerting until they are resumed. Their configuration and history are kept.',
  },
  resume: {
    verb: 'Resume',
    confirm: 'Resume monitors',
    explain: 'Monitors return to their configured schedule and alerting.',
  },
  test: {
    verb: 'Test',
    confirm: 'Run tests',
    explain: `Runs one probe per monitor now, one after another (at most ${BULK_TEST_CAP}). Each result is recorded; schedules and the SLA confirmation policy are not changed.`,
  },
  add_tags: {
    verb: 'Add tags to',
    confirm: 'Add tags',
    explain: 'Tags are added to each monitor; existing tags are kept. Tags also decide which scoped users can see a monitor.',
  },
  remove_tags: {
    verb: 'Remove tags from',
    confirm: 'Remove tags',
    explain: 'Only the listed tags are removed. Removing a scope tag can hide a monitor from users scoped to that tag.',
  },
  set_group: {
    verb: 'Move',
    confirm: 'Move monitors',
    explain: 'Each monitor is moved to the chosen group (or out of any group). Group maintenance windows then apply by the new membership.',
  },
  maintenance: {
    verb: 'Start maintenance for',
    confirm: 'Start maintenance',
    explain: 'A maintenance window starts now (appliance time) for each monitor: alerts are suppressed and the window is excluded from availability.',
  },
  delete: {
    verb: 'Delete',
    confirm: 'Delete monitors',
    explain: 'Deletes the monitors together with their results and history. This cannot be undone.',
    destructive: true,
  },
}

const DURATIONS = [
  { value: '30', label: '30 minutes' },
  { value: '60', label: '1 hour' },
  { value: '120', label: '2 hours' },
  { value: '240', label: '4 hours' },
  { value: '480', label: '8 hours' },
  { value: '1440', label: '24 hours' },
]

type Extra = { tags?: string[]; group_id?: string | null; maintenance?: { duration_minutes: number; reason: string } }

function parseTags(raw: string): string[] {
  return Array.from(new Set(raw.split(/[,\n]/).map((t) => t.trim()).filter(Boolean)))
}

/** Older appliances have no /service-checks/bulk: apply the action one monitor at a time. */
async function runLegacy(
  action: BulkAction, items: BulkItem[], extra: Extra, onProgress: (done: number) => void,
): Promise<BulkResult[]> {
  if (action === 'delete') {
    const res = (await api.post('/service-checks/bulk-delete', { check_ids: items.map((i) => i.id) })).data as { deleted?: number }
    onProgress(items.length)
    const allGone = res?.deleted == null || res.deleted >= items.length
    return items.map((i) => ({ id: i.id, name: i.name, ok: true, error: allGone ? undefined : 'Delete count did not match the selection; refresh to verify.' }))
  }
  const results: BulkResult[] = []
  for (const [n, it] of items.entries()) {
    try {
      if (action === 'pause' || action === 'resume') {
        await api.put(`/service-checks/${it.id}`, { enabled: action === 'resume' })
        results.push({ id: it.id, name: it.name, ok: true })
      } else if (action === 'test') {
        const d = (await api.post(`/service-checks/${it.id}/test`)).data
        const up = d?.status === 'up' || d?.is_up
        results.push({ id: it.id, name: it.name, ok: true, error: up ? undefined : `Probe failed: ${d?.error || 'no response'}` })
      } else if (action === 'add_tags' || action === 'remove_tags') {
        const cur = ((await api.get(`/service-checks/${it.id}`)).data?.tags || []) as string[]
        const tags = extra.tags || []
        const next = action === 'add_tags'
          ? Array.from(new Set([...cur, ...tags]))
          : cur.filter((t) => !tags.includes(t))
        await api.put(`/service-checks/${it.id}`, { tags: next })
        results.push({ id: it.id, name: it.name, ok: true })
      } else if (action === 'set_group') {
        await api.put(`/service-checks/${it.id}`, { group_id: extra.group_id ?? null })
        results.push({ id: it.id, name: it.name, ok: true })
      } else if (action === 'maintenance' && extra.maintenance) {
        const start = serverNow()
        await api.post('/service-check-maintenance', {
          scope_type: 'check',
          scope_check_id: it.id,
          starts_at: new Date(start).toISOString(),
          ends_at: new Date(start + extra.maintenance.duration_minutes * 60_000).toISOString(),
          reason: extra.maintenance.reason || null,
        })
        results.push({ id: it.id, name: it.name, ok: true })
      }
    } catch (e) {
      results.push({ id: it.id, name: it.name, ok: false, error: apiErrorMessage(e) })
    }
    onProgress(n + 1)
  }
  return results
}

async function runBulk(
  action: BulkAction, items: BulkItem[], extra: Extra, onProgress: (done: number) => void,
): Promise<BulkResult[]> {
  const names = new Map(items.map((i) => [i.id, i.name]))
  try {
    const res = (await api.post('/service-checks/bulk', { ids: items.map((i) => i.id), action, ...extra }, { timeout: 180_000 })).data
    const rows: any[] = Array.isArray(res?.results) ? res.results : []
    onProgress(items.length)
    return rows.map((r) => ({
      id: String(r.id),
      name: r.name || names.get(String(r.id)) || String(r.id),
      ok: !!r.ok,
      error: r.error || undefined,
    }))
  } catch (e) {
    if (isRouteMissing(e)) return runLegacy(action, items, extra, onProgress)
    throw e
  }
}

export function BulkActionDialog({
  action, items, groups, knownTags, onOpenChange, onFinished, onSelectOnly,
}: {
  action: BulkAction | null
  items: BulkItem[]
  groups: ServiceCheckGroup[]
  knownTags: string[]
  onOpenChange: (open: boolean) => void
  onFinished: (action: BulkAction, results: BulkResult[]) => void
  onSelectOnly: (ids: string[]) => void
}) {
  const open = action != null
  const [phase, setPhase] = useState<'review' | 'running' | 'results'>('review')
  const [results, setResults] = useState<BulkResult[]>([])
  const [runError, setRunError] = useState<string | null>(null)
  const [progress, setProgress] = useState(0)
  const [tagText, setTagText] = useState('')
  const [groupId, setGroupId] = useState<string>('none')
  const [duration, setDuration] = useState('60')
  const [reason, setReason] = useState('')
  // Snapshot the selection when the dialog opens so the results match what was reviewed.
  const [snapshot, setSnapshot] = useState<BulkItem[]>([])

  useEffect(() => {
    if (open) {
      setPhase('review')
      setResults([])
      setRunError(null)
      setProgress(0)
      setTagText('')
      setReason('')
      setSnapshot(items)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, action])

  const meta = action ? ACTION_META[action] : null
  const hidden = snapshot.filter((i) => !i.visible).length
  const tags = parseTags(tagText)
  const overCap = action === 'test' && snapshot.length > BULK_TEST_CAP

  const invalid = useMemo(() => {
    if (!action || snapshot.length === 0) return 'Nothing selected.'
    if (overCap) return `Test runs at most ${BULK_TEST_CAP} monitors at a time. Narrow the selection.`
    if ((action === 'add_tags' || action === 'remove_tags') && tags.length === 0) return 'Enter at least one tag.'
    if (action === 'maintenance' && !reason.trim()) return 'Give a reason so the window is identifiable in reports.'
    return null
  }, [action, snapshot.length, overCap, tags.length, reason])

  async function run() {
    if (!action || invalid) return
    const extra: Extra = {}
    if (action === 'add_tags' || action === 'remove_tags') extra.tags = tags
    if (action === 'set_group') extra.group_id = groupId === 'none' ? null : groupId
    if (action === 'maintenance') extra.maintenance = { duration_minutes: Number(duration), reason: reason.trim() }
    setPhase('running')
    setProgress(0)
    setRunError(null)
    try {
      const res = await runBulk(action, snapshot, extra, setProgress)
      setResults(res)
      setPhase('results')
      onFinished(action, res)
    } catch (e) {
      // A rejected request (4xx, e.g. an id is no longer visible) changed nothing;
      // a timeout or server error may have applied part of the action.
      const status = (e as any)?.response?.status
      const rejected = typeof status === 'number' && status >= 400 && status < 500
      setRunError(rejected
        ? `Nothing was changed: ${apiErrorMessage(e)}`
        : `The request did not complete (${apiErrorMessage(e)}). Some monitors may have been changed — refresh the list to verify.`)
      setPhase('review')
      onFinished(action, [])
    }
  }

  const succeeded = results.filter((r) => r.ok && !r.error).length
  const warned = results.filter((r) => r.ok && r.error).length
  const failed = results.filter((r) => !r.ok).length
  const count = snapshot.length
  const plural = count === 1 ? 'monitor' : 'monitors'

  return (
    <Dialog open={open} onOpenChange={(o) => { if (phase !== 'running') onOpenChange(o) }}>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle className={cn(meta?.destructive && 'text-danger')}>
            {phase === 'results' ? `${meta?.confirm} — results` : `${meta?.verb} ${count} ${plural}`}
          </DialogTitle>
          <DialogDescription>{phase === 'results' ? `${count} ${plural} processed.` : meta?.explain}</DialogDescription>
        </DialogHeader>

        {phase !== 'results' ? (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium text-text">{count} {plural} affected</span>
              {hidden > 0 && (
                <span className="inline-flex items-center gap-1 rounded bg-warning/10 px-1.5 py-0.5 text-warning">
                  <EyeOff className="h-3 w-3" /> {hidden} not visible in the current view
                </span>
              )}
            </div>
            <ul className="max-h-48 divide-y divide-border/60 overflow-y-auto rounded-md border border-border text-xs" aria-label="Affected monitors">
              {snapshot.map((i) => (
                <li key={i.id} className="flex items-center justify-between gap-2 px-2.5 py-1.5">
                  <span className="truncate">{i.name}</span>
                  {!i.visible && <span className="shrink-0 text-[10.5px] text-muted">hidden by filters/page</span>}
                </li>
              ))}
            </ul>

            {(action === 'add_tags' || action === 'remove_tags') && (
              <div className="space-y-1">
                <label className="text-xs font-medium" htmlFor="bulk-tags">Tags (comma-separated)</label>
                <Input id="bulk-tags" list="bulk-tag-options" value={tagText} onChange={(e) => setTagText(e.target.value)} placeholder="e.g. production, payments" />
                <datalist id="bulk-tag-options">
                  {knownTags.map((t) => <option key={t} value={t} />)}
                </datalist>
              </div>
            )}

            {action === 'set_group' && (
              <div className="space-y-1">
                <span className="text-xs font-medium">Destination group</span>
                <Select value={groupId} onValueChange={setGroupId}>
                  <SelectTrigger className="h-9"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">No group</SelectItem>
                    {groups.map((g) => <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            )}

            {action === 'maintenance' && (
              <div className="grid gap-3 sm:grid-cols-[160px_1fr]">
                <div className="space-y-1">
                  <span className="text-xs font-medium">Duration</span>
                  <Select value={duration} onValueChange={setDuration}>
                    <SelectTrigger className="h-9"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {DURATIONS.map((d) => <SelectItem key={d.value} value={d.value}>{d.label}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium" htmlFor="bulk-reason">Reason</label>
                  <Textarea id="bulk-reason" rows={2} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Change CHG-1234: load balancer upgrade" />
                </div>
              </div>
            )}

            {invalid && snapshot.length > 0 && <p className="text-xs text-warning">{invalid}</p>}
            {runError && (
              <p className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
                {runError}
              </p>
            )}
            {phase === 'running' && (
              <p className="flex items-center gap-2 text-xs text-muted">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Working… {progress > 0 ? `${progress}/${count}` : ''}
              </p>
            )}
          </div>
        ) : (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap gap-3 text-xs">
              <span className="text-success">{succeeded} succeeded</span>
              {warned > 0 && <span className="text-warning">{warned} completed with a failed probe</span>}
              <span className={failed ? 'text-danger' : 'text-muted'}>{failed} failed</span>
            </div>
            <ul className="max-h-64 divide-y divide-border/60 overflow-y-auto rounded-md border border-border text-xs" aria-label="Per-monitor results">
              {results.map((r) => (
                <li key={r.id} className="flex items-start gap-2 px-2.5 py-1.5">
                  {!r.ok
                    ? <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
                    : <CheckCircle2 className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', r.error ? 'text-warning' : 'text-success')} />}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{r.name}</span>
                    {r.error && <span className="block break-words text-muted">{r.error}</span>}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <DialogFooter className="flex-wrap">
          {phase === 'results' ? (
            <>
              {failed > 0 && action !== 'delete' && (
                <Button variant="outline" onClick={() => { onSelectOnly(results.filter((r) => !r.ok).map((r) => r.id)); onOpenChange(false) }}>
                  Keep failed selected
                </Button>
              )}
              <Button onClick={() => onOpenChange(false)}>Close</Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={phase === 'running'}>Cancel</Button>
              <Button
                variant={meta?.destructive ? 'destructive' : 'default'}
                onClick={run}
                disabled={!!invalid || phase === 'running'}
              >
                {phase === 'running' && <Loader2 className="animate-spin" />}
                {meta?.confirm} ({count})
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
