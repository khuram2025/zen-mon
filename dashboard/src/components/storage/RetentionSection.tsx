import { Fragment, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ChevronDown, ChevronRight, Clock, Eraser, Loader2, RefreshCw, Save, Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { bytes } from './storageMath'
import type { AutoPurge, Overview } from './types'
import { STORAGE_KEY, useOverview } from './useStorage'

export function RetentionSection() {
  const { data: overview } = useOverview()
  if (!overview) {
    return (
      <Card><CardContent className="flex items-center gap-2 py-6 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading retention policies…
      </CardContent></Card>
    )
  }
  return (
    <div className="space-y-4">
      <RetentionCard overview={overview} />
      <PurgeCard overview={overview} />
      <OsCleanupCard overview={overview} />
    </div>
  )
}

function retentionError(raw: string, min: number): string | null {
  if (raw === '') return null
  const n = Number(raw)
  if (!Number.isInteger(n)) return 'Whole days only'
  if (n < min) return `Minimum ${min} days`
  if (n > 3650) return 'Maximum 3650 days'
  return null
}

function autoPurgeError(a: AutoPurge): string | null {
  if (!a.enabled) return null
  if (!(a.threshold_pct >= 50 && a.threshold_pct <= 98)) return 'Threshold must be 50–98%'
  if (!(a.target_pct >= 40 && a.target_pct <= 95)) return 'Target must be 40–95%'
  if (a.target_pct >= a.threshold_pct) return 'Target must be below the threshold'
  if (!(a.min_keep_days >= 1 && a.min_keep_days <= 365)) return 'Minimum keep must be 1–365 days'
  if (!(a.min_free_gib >= 0.5 && a.min_free_gib <= 1024)) return 'Minimum free space must be 0.5–1024 GiB'
  return null
}

function RetentionCard({ overview }: { overview: Overview }) {
  const qc = useQueryClient()
  const [days, setDays] = useState<Record<string, string>>({})
  const [auto, setAuto] = useState<AutoPurge | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const effectiveAuto = auto ?? overview.auto_purge
  const value = (id: string, configured: number | null) => days[id] ?? (configured != null ? String(configured) : '')
  const errors = overview.categories
    .map((c) => retentionError(value(c.id, c.configured_days), c.min_days))
    .filter(Boolean)
  const autoError = autoPurgeError(effectiveAuto)
  const dirty = Object.keys(days).length > 0 || auto !== null

  const save = useMutation({
    mutationFn: async () => {
      const categories: Record<string, number | null> = {}
      for (const c of overview.categories) {
        const raw = value(c.id, c.configured_days)
        categories[c.id] = raw === '' ? null : Number(raw)
      }
      return (await api.put('/system/storage/retention', {
        categories,
        table_overrides: overview.table_overrides || {},
        auto_purge: effectiveAuto,
      })).data
    },
    onSuccess: (data: any) => {
      const changed = (data.ttl_changes || []).filter((c: any) => !c.error).length
      const failed = (data.errors || []).length
      if (failed) toast.error(`Saved with ${failed} TTL error(s)`, 'See the Activity log')
      else toast.success('Retention policy saved', changed ? `${changed} table TTL(s) updated` : undefined)
      setDays({})
      setAuto(null)
      qc.invalidateQueries({ queryKey: STORAGE_KEY })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const setAutoField = (field: keyof AutoPurge, raw: string) =>
    setAuto({ ...effectiveAuto, [field]: raw === '' ? NaN : Number(raw) })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Clock className="h-4 w-4 text-primary" /> Data retention
        </CardTitle>
        <p className="text-xs text-muted">
          How long each category is kept, applied as ClickHouse table TTLs (expired data is removed in
          the background). Leave a field blank to keep each table's built-in retention. The projection
          shows where each category levels off at the current ingest rate.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <Table>
          <THead>
            <Tr>
              <Th>Category</Th>
              <Th className="text-right">Size now</Th>
              <Th className="text-right">Per day</Th>
              <Th className="text-right">Projected</Th>
              <Th className="text-right">Keep (days)</Th>
            </Tr>
          </THead>
          <TBody>
            {overview.categories.map((c) => {
              const raw = value(c.id, c.configured_days)
              const err = retentionError(raw, c.min_days)
              const open = expanded === c.id
              return (
                <Fragment key={c.id}>
                  <Tr className="cursor-pointer" onClick={() => setExpanded(open ? null : c.id)}>
                    <Td>
                      <div className="flex items-center gap-1.5 font-medium">
                        {open ? <ChevronDown className="h-3.5 w-3.5 text-muted" /> : <ChevronRight className="h-3.5 w-3.5 text-muted" />}
                        {c.label}
                      </div>
                      <div className="pl-5 text-[11px] text-muted">{c.description} · {c.tables.length} table{c.tables.length === 1 ? '' : 's'}</div>
                    </Td>
                    <Td className="text-right tabular-nums">{bytes(c.size_bytes)}</Td>
                    <Td className="text-right tabular-nums text-text2">{c.daily_bytes ? bytes(c.daily_bytes) : '—'}</Td>
                    <Td className="text-right tabular-nums text-text2">
                      {c.unbounded
                        ? <span className="text-warning" title="At least one table has no retention limit">unbounded</span>
                        : c.projected_bytes ? bytes(c.projected_bytes) : '—'}
                    </Td>
                    <Td className="text-right" onClick={(e) => e.stopPropagation()}>
                      <Input
                        type="number"
                        min={c.min_days}
                        max={3650}
                        placeholder={String(c.default_days)}
                        title={`Blank keeps the built-in retention (${c.default_days} days by default)`}
                        value={raw}
                        onChange={(e) => setDays({ ...days, [c.id]: e.target.value })}
                        className={cn('ml-auto h-8 w-28 text-right tabular-nums', err && 'border-danger')}
                        aria-label={`${c.label} retention in days`}
                        aria-invalid={!!err}
                      />
                      {err && <div className="mt-0.5 text-[11px] text-danger">{err}</div>}
                    </Td>
                  </Tr>
                  {open && c.tables.map((t) => (
                    <Tr key={`${c.id}-${t.table}`} className="bg-surface2/20">
                      <Td className="pl-9">
                        <span className="font-mono text-xs">{t.table}</span>
                        <span className="ml-2 text-[10px] uppercase text-muted">{t.partition_granularity} partitions</span>
                      </Td>
                      <Td className="text-right font-mono text-xs tabular-nums">{bytes(t.size_bytes)}</Td>
                      <Td className="text-right font-mono text-xs tabular-nums text-text2">{t.rows.toLocaleString()} rows</Td>
                      <Td className="text-right text-xs text-muted">
                        {t.oldest ? `since ${new Date(t.oldest).toLocaleDateString()}` : ''}
                      </Td>
                      <Td className="text-right text-xs text-muted">
                        {t.current_ttl_days != null ? `TTL ${t.current_ttl_days}d` : 'no TTL'}
                      </Td>
                    </Tr>
                  ))}
                </Fragment>
              )
            })}
          </TBody>
        </Table>

        <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-medium">
                <Eraser className="h-4 w-4 text-warning" /> Low-space protection
              </div>
              <p className="mt-0.5 max-w-2xl text-[11px] text-muted">
                Applied by the free-space watchdog, which keeps running when the API or database is down.
                Above the threshold, or below the minimum free space, it frees space down to the target:
                ClickHouse diagnostic logs first, then the least valuable monitoring data older than the
                minimum keep window. Monitoring data is only deleted when that can reach the target, or
                from 95% usage. When nothing more is eligible, host-agent and APM uploads pause (agents
                keep them) instead of filling the disk.
              </p>
            </div>
            <Switch
              checked={effectiveAuto.enabled}
              onCheckedChange={(v) => setAuto({ ...effectiveAuto, enabled: v })}
              aria-label="Enable emergency auto-purge"
            />
          </div>
          {effectiveAuto.enabled && (
            <div className="mt-3 grid gap-3 sm:grid-cols-4">
              {([
                ['threshold_pct', 'Purge when usage above', '%', 50, 98],
                ['target_pct', 'Purge down to', '%', 40, 95],
                ['min_keep_days', 'Always keep at least', 'days', 1, 365],
                ['min_free_gib', 'Also act below', 'GiB free', 0.5, 1024],
              ] as const).map(([field, label, unit, min, max]) => (
                <label key={field} className="text-xs text-text2">
                  {label}
                  <div className="mt-1 flex items-center gap-2">
                    <Input type="number" min={min} max={max} className="h-8 w-20 text-right tabular-nums"
                      value={Number.isNaN(effectiveAuto[field]) ? '' : effectiveAuto[field]}
                      onChange={(e) => setAutoField(field, e.target.value)} />
                    <span className="text-muted">{unit}</span>
                  </div>
                </label>
              ))}
            </div>
          )}
          {autoError && <div className="mt-2 text-xs text-danger">{autoError}</div>}
        </div>

        <div className="flex items-center justify-end gap-3">
          {dirty && <span className="text-xs text-muted">Unsaved changes</span>}
          {dirty && (
            <Button variant="ghost" onClick={() => { setDays({}); setAuto(null) }} disabled={save.isPending}>
              Discard
            </Button>
          )}
          <Button onClick={() => save.mutate()} disabled={save.isPending || errors.length > 0 || !!autoError || !dirty}>
            {save.isPending ? <Loader2 className="animate-spin" /> : <Save />}
            Save retention policy
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function PurgeCard({ overview }: { overview: Overview }) {
  const qc = useQueryClient()
  const [category, setCategory] = useState<string>('')
  const [olderThan, setOlderThan] = useState('90')
  const [plan, setPlan] = useState<any | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const olderValid = Number.isInteger(Number(olderThan)) && Number(olderThan) >= 1 && Number(olderThan) <= 3650

  const request = (dryRun: boolean) => api.post('/system/storage/purge', {
    category, older_than_days: Number(olderThan), dry_run: dryRun,
  })

  const preview = useMutation({
    mutationFn: async () => (await request(true)).data,
    onSuccess: (data) => setPlan(data),
    onError: (e: any) => toast.error('Preview failed', apiErrorMessage(e)),
  })

  const purge = useMutation({
    mutationFn: async () => (await request(false)).data,
    onSuccess: (data: any) => {
      setConfirmOpen(false)
      setPlan(null)
      toast.success('Purge scheduled',
        `${bytes(data.scheduled_bytes ?? data.freed_bytes)} in ${data.dropped_partitions.length} partitions; ClickHouse returns the space within about 10 minutes`)
      qc.invalidateQueries({ queryKey: STORAGE_KEY })
    },
    onError: (e: any) => { setConfirmOpen(false); toast.error('Purge failed', apiErrorMessage(e)) },
  })

  const selected = overview.categories.find((c) => c.id === category)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Trash2 className="h-4 w-4 text-primary" /> Free up space now
        </CardTitle>
        <p className="text-xs text-muted">
          Delete old metrics immediately. Preview first — whole partitions are dropped and this cannot be undone.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-text2">
            Data category
            <div className="mt-1 w-60">
              <Select value={category} onValueChange={(v) => { setCategory(v); setPlan(null) }}>
                <SelectTrigger className="h-9"><SelectValue placeholder="Select category…" /></SelectTrigger>
                <SelectContent>
                  {overview.categories.map((c) => (
                    <SelectItem key={c.id} value={c.id}>{c.label} ({bytes(c.size_bytes)})</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </label>
          <label className="text-xs text-text2">
            Delete data older than
            <div className="mt-1 flex items-center gap-2">
              <Input type="number" min={1} max={3650} className="h-9 w-24 text-right tabular-nums"
                value={olderThan} onChange={(e) => { setOlderThan(e.target.value); setPlan(null) }} />
              <span className="text-muted">days</span>
            </div>
          </label>
          <Button variant="outline" disabled={!category || !olderValid || preview.isPending} onClick={() => preview.mutate()}>
            {preview.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            Preview
          </Button>
        </div>

        {plan && (
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            {plan.partitions.length === 0 ? (
              <p className="text-sm text-muted">
                Nothing to purge — no complete partitions of {selected?.label.toLowerCase()} are older than {olderThan} days.
              </p>
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm">
                    <span className="font-semibold text-warning">{bytes(plan.total_bytes)}</span> can be reclaimed by dropping{' '}
                    <span className="font-semibold">{plan.partitions.length}</span> partition{plan.partitions.length === 1 ? '' : 's'}.
                  </p>
                  <Button variant="destructive" size="sm" onClick={() => setConfirmOpen(true)}>
                    <Trash2 /> Purge now
                  </Button>
                </div>
                <div className="mt-2 max-h-40 overflow-y-auto">
                  <table className="w-full text-xs">
                    <tbody>
                      {plan.partitions.map((p: any) => (
                        <tr key={`${p.table}-${p.partition_id}`} className="border-t border-border/40">
                          <td className="py-1 font-mono">{p.table}</td>
                          <td className="py-1 font-mono text-muted">{p.partition_id}</td>
                          <td className="py-1 text-right tabular-nums">{bytes(p.size_bytes)}</td>
                          <td className="py-1 text-right tabular-nums text-muted">{p.rows.toLocaleString()} rows</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        )}

        <ConfirmDialog
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          title="Purge old data?"
          description={`This permanently deletes ${plan ? bytes(plan.total_bytes) : ''} of ${selected?.label || ''} older than ${olderThan} days. This cannot be undone.`}
          confirmText="Purge data"
          destructive
          loading={purge.isPending}
          onConfirm={() => purge.mutate()}
        />
      </CardContent>
    </Card>
  )
}

function OsCleanupCard({ overview }: { overview: Overview }) {
  const qc = useQueryClient()
  const [journalMb, setJournalMb] = useState('500')
  const journalValid = Number(journalMb) >= 50 && Number(journalMb) <= 5000

  const cleanup = useMutation({
    mutationFn: async (actions: string[]) =>
      (await api.post('/system/storage/os-cleanup', { actions, journal_max_mb: Number(journalMb) })).data,
    onSuccess: (data: any) => {
      if (data.errors?.length) toast.error('Cleanup finished with errors', data.errors.join('; '))
      else toast.success('OS cleanup complete', `Freed ${bytes(data.freed_bytes)} on the system disk`)
      qc.invalidateQueries({ queryKey: STORAGE_KEY })
    },
    onError: (e: any) => toast.error('Cleanup failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Eraser className="h-4 w-4 text-primary" /> System disk cleanup
        </CardTitle>
        <p className="text-xs text-muted">
          Shrink the systemd journal and clear the package cache. Safe to run at any time.
        </p>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-text2">
            Shrink journal ({bytes(overview.journal_bytes)}) to
            <div className="mt-1 flex items-center gap-2">
              <Input type="number" min={50} max={5000} className="h-9 w-24 text-right tabular-nums"
                value={journalMb} onChange={(e) => setJournalMb(e.target.value)} />
              <span className="text-muted">MB</span>
            </div>
          </label>
          <Button variant="outline" disabled={cleanup.isPending || !journalValid} onClick={() => cleanup.mutate(['vacuum_journal'])}>
            {cleanup.isPending ? <Loader2 className="animate-spin" /> : <Eraser />}
            Vacuum journal
          </Button>
          <Button variant="outline" disabled={cleanup.isPending || overview.apt_cache_bytes <= 0} onClick={() => cleanup.mutate(['apt_clean'])}>
            {cleanup.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
            Clear package cache ({bytes(overview.apt_cache_bytes)})
          </Button>
        </div>
        {!journalValid && (
          <div className="mt-2 flex items-center gap-1.5 text-xs text-danger">
            <AlertTriangle className="h-3.5 w-3.5" /> Journal size must be 50–5000 MB
          </div>
        )}
      </CardContent>
    </Card>
  )
}
