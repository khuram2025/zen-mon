/**
 * Settings -> Storage: disk usage, retention & purge, disk expansion,
 * OS cleanup, and appliance backup/restore.
 *
 * Backend: GET /system/storage (disks/LVM/expansion, existing endpoint) plus
 * the /system/storage/* management endpoints (overview, retention, purge,
 * os-cleanup, backups).
 */
import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Archive, CheckCircle2, Clock, Database, Download, Eraser,
  HardDrive, Loader2, Plus, RefreshCw, RotateCcw, Save, Trash2, Upload,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, formatBytes, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'

// ─── Types (mirror the API responses) ───────────────────────────────────────

interface MountInfo {
  mount: string
  label: string
  total_bytes: number
  used_bytes: number
  free_bytes: number
  usage_percent: number
}

interface ManagedTable {
  table: string
  category: string
  time_column: string
  partition_granularity: string
  size_bytes: number
  size_human: string
  rows: number
  oldest: string | null
  newest: string | null
  current_ttl_days: number | null
}

interface RetentionCategory {
  id: string
  label: string
  description: string
  default_days: number
  min_days: number
  configured_days: number | null
  size_bytes: number
  size_human: string
  rows: number
  tables: ManagedTable[]
}

interface AutoPurge {
  enabled: boolean
  threshold_pct: number
  target_pct: number
  min_keep_days: number
}

interface BackupSchedule {
  enabled: boolean
  frequency: 'daily' | 'weekly'
  weekday: number
  hour_utc: number
  include_clickhouse: boolean
  keep_last: number
}

interface Overview {
  mounts: MountInfo[]
  journal_bytes: number
  apt_cache_bytes: number
  categories: RetentionCategory[]
  auto_purge: AutoPurge
  table_overrides: Record<string, number>
  backup_schedule: BackupSchedule
}

interface StorageStatus {
  total_bytes: number
  used_bytes: number
  free_bytes: number
  usage_percent: number
  mount_point: string
  vg_name: string
  vg_free_bytes: number
  unclaimed_vg_bytes: number
  pv_details: { name: string; size_human: string; free_human: string }[]
  clickhouse_total_bytes: number
  clickhouse_storage: { ok: boolean; message: string }
  available_disks: { name: string; size_bytes: number; size_human: string }[]
  health: 'ok' | 'warning' | 'critical'
  health_message: string
}

interface Backup {
  id: string
  created_at: string
  created_by: string
  kind: 'config' | 'full'
  status: 'running' | 'completed' | 'failed'
  include_clickhouse: boolean
  size_bytes: number
  size_human: string
  note: string | null
  error: string | null
  last_restore_at: string | null
  last_restore_status: 'running' | 'completed' | 'failed' | null
  last_restore_error: string | null
}

interface StorageEvent {
  id: number
  created_at: string
  event_type: string
  actor: string
  freed_bytes: number
  freed_human: string
  details: Record<string, any>
}

// ─── Small helpers ──────────────────────────────────────────────────────────

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

function usageTone(pct: number) {
  return pct >= 95 ? 'from-danger to-danger/70'
    : pct >= 85 ? 'from-warning to-warning/70'
    : 'from-primary to-info'
}

function UsageRow({ m }: { m: MountInfo }) {
  const pct = Math.min(100, m.usage_percent || 0)
  return (
    <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="text-sm font-medium">{m.label}</span>
          <span className="font-mono text-[11px] text-muted">{m.mount}</span>
        </div>
        <span className="text-xs tabular-nums text-text2">
          {formatBytes(m.used_bytes)} / {formatBytes(m.total_bytes)}
          <span className={cn('ml-2 font-semibold', pct >= 95 ? 'text-danger' : pct >= 85 ? 'text-warning' : 'text-text')}>
            {pct.toFixed(1)}%
          </span>
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-surface2">
        <div className={cn('h-full rounded-full bg-gradient-to-r transition-all', usageTone(pct))} style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-[11px] text-muted">{formatBytes(m.free_bytes)} free</div>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { variant: 'success' | 'warning' | 'danger' | 'info' | 'default'; icon?: any }> = {
    completed: { variant: 'success', icon: CheckCircle2 },
    running: { variant: 'info', icon: Loader2 },
    failed: { variant: 'danger', icon: AlertTriangle },
  }
  const meta = map[status] || { variant: 'default' as const }
  const Icon = meta.icon
  return (
    <Badge variant={meta.variant}>
      {Icon && <Icon className={cn('h-3 w-3', status === 'running' && 'animate-spin')} />}
      {status}
    </Badge>
  )
}

// ─── Main component ─────────────────────────────────────────────────────────

export function StorageTabContent() {
  return (
    <div className="space-y-4">
      <OverviewCard />
      <RetentionCard />
      <PurgeCard />
      <ExpansionCard />
      <OsCleanupCard />
      <BackupsCard />
      <EventsCard />
    </div>
  )
}

function useOverview() {
  return useQuery<Overview>({
    queryKey: ['system', 'storage', 'overview'],
    queryFn: async () => (await api.get('/system/storage/overview')).data,
    refetchInterval: 30_000,
  })
}

function useStorageStatus() {
  return useQuery<StorageStatus>({
    queryKey: ['system', 'storage'],
    queryFn: async () => (await api.get('/system/storage')).data,
    refetchInterval: 30_000,
  })
}

// ─── 1. Overview ────────────────────────────────────────────────────────────

function OverviewCard() {
  const { data: overview } = useOverview()
  const { data: status } = useStorageStatus()

  const totalManaged = useMemo(
    () => (overview?.categories || []).reduce((a, c) => a + c.size_bytes, 0),
    [overview],
  )

  const health = status?.health || 'ok'
  const healthTone = health === 'critical' ? 'border-danger/40 bg-danger/10 text-danger'
    : health === 'warning' ? 'border-warning/40 bg-warning/10 text-warning'
    : 'border-success/30 bg-success/10 text-success'

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HardDrive className="h-4 w-4 text-primary" /> Storage Overview
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {status && (
          <div className={cn('flex items-center gap-2 rounded-lg border px-3 py-2 text-sm', healthTone)}>
            {health === 'ok' ? <CheckCircle2 className="h-4 w-4 shrink-0" /> : <AlertTriangle className="h-4 w-4 shrink-0" />}
            {status.health_message}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          {(overview?.mounts || []).map((m) => <UsageRow key={m.mount} m={m} />)}
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Metrics database</div>
            <div className="mt-0.5 text-lg font-semibold tabular-nums">{formatBytes(status?.clickhouse_total_bytes || totalManaged)}</div>
            <div className="text-[11px] text-muted">ClickHouse tables on /data</div>
          </div>
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="text-[11px] font-medium uppercase tracking-wide text-muted">System journal</div>
            <div className="mt-0.5 text-lg font-semibold tabular-nums">{formatBytes(overview?.journal_bytes || 0)}</div>
            <div className="text-[11px] text-muted">Reclaimable via OS cleanup below</div>
          </div>
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Package cache</div>
            <div className="mt-0.5 text-lg font-semibold tabular-nums">{formatBytes(overview?.apt_cache_bytes || 0)}</div>
            <div className="text-[11px] text-muted">apt archives on the OS disk</div>
          </div>
        </div>

        {overview && overview.categories.length > 0 && (
          <div>
            <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
              Data breakdown ({formatBytes(totalManaged)})
            </div>
            <div className="flex h-3 w-full overflow-hidden rounded-full bg-surface2">
              {overview.categories.filter((c) => c.size_bytes > 0).map((c, i) => (
                <div
                  key={c.id}
                  title={`${c.label}: ${c.size_human}`}
                  className={cn('h-full', ['bg-primary', 'bg-info', 'bg-accent', 'bg-warning', 'bg-success', 'bg-danger'][i % 6])}
                  style={{ width: `${Math.max(1, (c.size_bytes / Math.max(1, totalManaged)) * 100)}%` }}
                />
              ))}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
              {overview.categories.filter((c) => c.size_bytes > 0).map((c, i) => (
                <span key={c.id} className="flex items-center gap-1.5 text-[11px] text-text2">
                  <span className={cn('h-2 w-2 rounded-full', ['bg-primary', 'bg-info', 'bg-accent', 'bg-warning', 'bg-success', 'bg-danger'][i % 6])} />
                  {c.label} <span className="tabular-nums text-muted">{c.size_human}</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ─── 2. Retention ───────────────────────────────────────────────────────────

function RetentionCard() {
  const qc = useQueryClient()
  const { data: overview } = useOverview()
  const [days, setDays] = useState<Record<string, string>>({})
  const [auto, setAuto] = useState<AutoPurge | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const effectiveAuto = auto ?? overview?.auto_purge ?? null

  const save = useMutation({
    mutationFn: async () => {
      const categories: Record<string, number | null> = {}
      for (const c of overview?.categories || []) {
        const raw = days[c.id] ?? (c.configured_days != null ? String(c.configured_days) : '')
        categories[c.id] = raw === '' ? null : Number(raw)
      }
      return (await api.put('/system/storage/retention', {
        categories,
        table_overrides: overview?.table_overrides || {},
        auto_purge: effectiveAuto,
      })).data
    },
    onSuccess: (data: any) => {
      const changed = (data.ttl_changes || []).filter((c: any) => !c.error).length
      const failed = (data.errors || []).length
      if (failed) toast.error(`Saved with ${failed} TTL error(s)`, 'Check the events log below')
      else toast.success('Retention policy saved', changed ? `${changed} table TTL(s) updated` : undefined)
      qc.invalidateQueries({ queryKey: ['system', 'storage'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (!overview) {
    return (
      <Card><CardContent className="flex items-center gap-2 py-6 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading retention policies…
      </CardContent></Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Clock className="h-4 w-4 text-primary" /> Data Retention
        </CardTitle>
        <p className="text-xs text-muted">
          How long each data category is kept. Applied as ClickHouse table TTLs —
          expired data is removed automatically in the background. Leave blank to keep
          the built-in default for that table.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <Table>
          <THead>
            <Tr>
              <Th>Category</Th>
              <Th className="text-right">Size</Th>
              <Th className="text-right">Rows</Th>
              <Th className="text-right">Keep (days)</Th>
            </Tr>
          </THead>
          <TBody>
            {overview.categories.map((c) => (
              <>
                <Tr key={c.id} className="cursor-pointer" onClick={() => setExpanded(expanded === c.id ? null : c.id)}>
                  <Td>
                    <div className="font-medium">{c.label}</div>
                    <div className="text-[11px] text-muted">{c.description} · {c.tables.length} table{c.tables.length === 1 ? '' : 's'}</div>
                  </Td>
                  <Td className="text-right tabular-nums">{c.size_human}</Td>
                  <Td className="text-right tabular-nums text-text2">{c.rows.toLocaleString()}</Td>
                  <Td className="text-right" onClick={(e) => e.stopPropagation()}>
                    <Input
                      type="number"
                      min={c.min_days}
                      max={3650}
                      placeholder={String(c.default_days)}
                      value={days[c.id] ?? (c.configured_days != null ? String(c.configured_days) : '')}
                      onChange={(e) => setDays({ ...days, [c.id]: e.target.value })}
                      className="ml-auto h-8 w-24 text-right tabular-nums"
                    />
                  </Td>
                </Tr>
                {expanded === c.id && c.tables.map((t) => (
                  <Tr key={`${c.id}-${t.table}`} className="bg-surface2/20">
                    <Td className="pl-8">
                      <span className="font-mono text-xs">{t.table}</span>
                      <span className="ml-2 text-[10px] uppercase text-muted">{t.partition_granularity} partitions</span>
                    </Td>
                    <Td className="text-right font-mono text-xs tabular-nums">{t.size_human}</Td>
                    <Td className="text-right font-mono text-xs tabular-nums text-text2">{t.rows.toLocaleString()}</Td>
                    <Td className="text-right text-xs text-muted">
                      {t.current_ttl_days != null ? `TTL ${t.current_ttl_days}d` : 'no TTL'}
                    </Td>
                  </Tr>
                ))}
              </>
            ))}
          </TBody>
        </Table>

        {effectiveAuto && (
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Eraser className="h-4 w-4 text-warning" /> Emergency auto-purge
                </div>
                <p className="mt-0.5 max-w-xl text-[11px] text-muted">
                  When the data volume passes the threshold, the oldest data is purged
                  automatically (oldest partitions first, never newer than the minimum keep
                  window) until usage falls back under the target.
                </p>
              </div>
              <Switch
                checked={effectiveAuto.enabled}
                onCheckedChange={(v) => setAuto({ ...effectiveAuto, enabled: v })}
              />
            </div>
            {effectiveAuto.enabled && (
              <div className="mt-3 grid gap-3 sm:grid-cols-3">
                <label className="text-xs text-text2">
                  Purge when usage above
                  <div className="mt-1 flex items-center gap-2">
                    <Input type="number" min={50} max={98} className="h-8 w-20 text-right tabular-nums"
                      value={effectiveAuto.threshold_pct}
                      onChange={(e) => setAuto({ ...effectiveAuto, threshold_pct: Number(e.target.value) })} />
                    <span className="text-muted">%</span>
                  </div>
                </label>
                <label className="text-xs text-text2">
                  Purge down to
                  <div className="mt-1 flex items-center gap-2">
                    <Input type="number" min={40} max={95} className="h-8 w-20 text-right tabular-nums"
                      value={effectiveAuto.target_pct}
                      onChange={(e) => setAuto({ ...effectiveAuto, target_pct: Number(e.target.value) })} />
                    <span className="text-muted">%</span>
                  </div>
                </label>
                <label className="text-xs text-text2">
                  Always keep at least
                  <div className="mt-1 flex items-center gap-2">
                    <Input type="number" min={1} max={365} className="h-8 w-20 text-right tabular-nums"
                      value={effectiveAuto.min_keep_days}
                      onChange={(e) => setAuto({ ...effectiveAuto, min_keep_days: Number(e.target.value) })} />
                    <span className="text-muted">days</span>
                  </div>
                </label>
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end">
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save retention policy
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// ─── 3. Manual purge ────────────────────────────────────────────────────────

function PurgeCard() {
  const qc = useQueryClient()
  const { data: overview } = useOverview()
  const [category, setCategory] = useState<string>('')
  const [olderThan, setOlderThan] = useState('90')
  const [plan, setPlan] = useState<any | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const preview = useMutation({
    mutationFn: async () =>
      (await api.post('/system/storage/purge', {
        category, older_than_days: Number(olderThan), dry_run: true,
      })).data,
    onSuccess: (data) => setPlan(data),
    onError: (e: any) => toast.error('Preview failed', apiErrorMessage(e)),
  })

  const purge = useMutation({
    mutationFn: async () =>
      (await api.post('/system/storage/purge', {
        category, older_than_days: Number(olderThan), dry_run: false,
      })).data,
    onSuccess: (data: any) => {
      setConfirmOpen(false)
      setPlan(null)
      toast.success('Purge complete', `Freed ${data.freed_human} (${data.dropped_partitions.length} partitions)`)
      qc.invalidateQueries({ queryKey: ['system', 'storage'] })
    },
    onError: (e: any) => { setConfirmOpen(false); toast.error('Purge failed', apiErrorMessage(e)) },
  })

  const selected = overview?.categories.find((c) => c.id === category)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Trash2 className="h-4 w-4 text-primary" /> Free Up Space
        </CardTitle>
        <p className="text-xs text-muted">
          Immediately reclaim disk space by deleting old data. Preview first — the purge
          drops whole partitions and cannot be undone.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-text2">
            Data category
            <div className="mt-1 w-56">
              <Select value={category} onValueChange={(v) => { setCategory(v); setPlan(null) }}>
                <SelectTrigger className="h-9"><SelectValue placeholder="Select category…" /></SelectTrigger>
                <SelectContent>
                  {(overview?.categories || []).map((c) => (
                    <SelectItem key={c.id} value={c.id}>{c.label} ({c.size_human})</SelectItem>
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
          <Button variant="outline" disabled={!category || preview.isPending} onClick={() => preview.mutate()}>
            {preview.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Preview
          </Button>
        </div>

        {plan && (
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            {plan.partitions.length === 0 ? (
              <p className="text-sm text-muted">
                Nothing to purge — no complete partitions of {selected?.label.toLowerCase()} are older
                than {olderThan} days.
              </p>
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm">
                    <span className="font-semibold text-warning">{plan.total_human}</span> can be
                    reclaimed by dropping <span className="font-semibold">{plan.partitions.length}</span> partition{plan.partitions.length === 1 ? '' : 's'}.
                  </p>
                  <Button variant="destructive" size="sm" onClick={() => setConfirmOpen(true)}>
                    <Trash2 className="h-3.5 w-3.5" /> Purge now
                  </Button>
                </div>
                <div className="mt-2 max-h-40 overflow-y-auto">
                  <table className="w-full text-xs">
                    <tbody>
                      {plan.partitions.map((p: any) => (
                        <tr key={`${p.table}-${p.partition_id}`} className="border-t border-border/40">
                          <td className="py-1 font-mono">{p.table}</td>
                          <td className="py-1 font-mono text-muted">{p.partition_id}</td>
                          <td className="py-1 text-right tabular-nums">{p.size_human}</td>
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
          description={`This permanently deletes ${plan?.total_human || ''} of ${selected?.label || ''} older than ${olderThan} days. This cannot be undone.`}
          confirmText="Purge data"
          destructive
          loading={purge.isPending}
          onConfirm={() => purge.mutate()}
        />
      </CardContent>
    </Card>
  )
}

// ─── 4. Disk expansion ──────────────────────────────────────────────────────

function ExpansionCard() {
  const qc = useQueryClient()
  const { data: status, isLoading } = useStorageStatus()
  const [addDisk, setAddDisk] = useState<string | null>(null)

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['system', 'storage'] })
    qc.invalidateQueries({ queryKey: ['system', 'storage', 'overview'] })
  }

  const rescan = useMutation({
    mutationFn: async () => (await api.post('/system/storage/rescan')).data,
    onSuccess: (data: any) => {
      toast.success('Disk rescan complete',
        data.available_disks?.length
          ? `${data.available_disks.length} unused disk(s) detected`
          : 'No new disks detected — attach a disk to the VM first')
      invalidate()
    },
    onError: (e: any) => toast.error('Rescan failed', apiErrorMessage(e)),
  })

  const add = useMutation({
    mutationFn: async (disk: string) =>
      (await api.post('/system/storage/add-disk', { disk })).data,
    onSuccess: (data: any) => { setAddDisk(null); toast.success('Disk added', data.message); invalidate() },
    onError: (e: any) => { setAddDisk(null); toast.error('Add disk failed', apiErrorMessage(e)) },
  })

  const grow = useMutation({
    mutationFn: async () => (await api.post('/system/storage/grow')).data,
    onSuccess: (data: any) => {
      data.grew ? toast.success('Volume grown', data.message) : toast.info('Nothing to grow', data.message)
      invalidate()
    },
    onError: (e: any) => toast.error('Grow failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Database className="h-4 w-4 text-primary" /> Storage Expansion
        </CardTitle>
        <p className="text-xs text-muted">
          Attach a new virtual disk to the appliance, rescan, then add it — the data volume
          (ClickHouse metrics, flows, APM) grows online with no downtime.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {status?.clickhouse_storage && (
          <div className={cn(
            'flex items-start gap-2 rounded-lg border px-3 py-2 text-xs',
            status.clickhouse_storage.ok
              ? 'border-success/30 bg-success/10 text-success'
              : 'border-warning/40 bg-warning/10 text-warning',
          )}>
            {status.clickhouse_storage.ok ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
            {status.clickhouse_storage.message}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Volume group</div>
            <div className="mt-0.5 font-mono text-sm">{status?.vg_name || '—'}</div>
            <div className="text-[11px] text-muted">{status?.pv_details?.length || 0} physical volume(s)</div>
          </div>
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Unclaimed space</div>
            <div className="mt-0.5 text-sm font-semibold tabular-nums">{formatBytes(status?.unclaimed_vg_bytes || 0)}</div>
            <div className="text-[11px] text-muted">Free in VG, not yet added to /data</div>
          </div>
          <div className="flex items-center justify-end gap-2 p-1">
            <Button variant="outline" size="sm" onClick={() => rescan.mutate()} disabled={rescan.isPending}>
              {rescan.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              Rescan disks
            </Button>
            <Button size="sm" onClick={() => grow.mutate()}
              disabled={grow.isPending || (status?.unclaimed_vg_bytes || 0) < 1024 * 1024 * 1024}>
              {grow.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
              Grow volume
            </Button>
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">Detected unused disks</div>
          {isLoading ? (
            <div className="text-sm text-muted">Loading…</div>
          ) : (status?.available_disks || []).length === 0 ? (
            <div className="rounded-lg border border-dashed border-border/60 p-4 text-center text-xs text-muted">
              No unused disks detected. Attach a new disk to the VM (e.g. in VMware), then click
              “Rescan disks”.
            </div>
          ) : (
            <div className="space-y-2">
              {status!.available_disks.map((d) => (
                <div key={d.name} className="flex items-center justify-between rounded-lg border border-border/50 bg-surface2/20 px-3 py-2">
                  <div>
                    <span className="font-mono text-sm">{d.name}</span>
                    <span className="ml-2 text-xs text-muted">{d.size_human}</span>
                  </div>
                  <Button size="sm" onClick={() => setAddDisk(d.name)} disabled={add.isPending}>
                    <Plus className="h-3.5 w-3.5" /> Add to storage
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>

        <ConfirmDialog
          open={addDisk !== null}
          onOpenChange={(o) => !o && setAddDisk(null)}
          title={`Add ${addDisk} to storage?`}
          description="The disk will be initialized for LVM and permanently joined to the data volume. All existing data on the disk will be erased. This cannot be undone."
          confirmText="Add disk"
          destructive
          loading={add.isPending}
          onConfirm={() => addDisk && add.mutate(addDisk)}
        />
      </CardContent>
    </Card>
  )
}

// ─── 5. OS cleanup ──────────────────────────────────────────────────────────

function OsCleanupCard() {
  const qc = useQueryClient()
  const { data: overview } = useOverview()
  const [journalMb, setJournalMb] = useState('500')

  const cleanup = useMutation({
    mutationFn: async (actions: string[]) =>
      (await api.post('/system/storage/os-cleanup', {
        actions, journal_max_mb: Number(journalMb) || 500,
      })).data,
    onSuccess: (data: any) => {
      if (data.errors?.length) toast.error('Cleanup finished with errors', data.errors.join('; '))
      else toast.success('OS cleanup complete', `Freed ${data.freed_human} on the system disk`)
      qc.invalidateQueries({ queryKey: ['system', 'storage', 'overview'] })
    },
    onError: (e: any) => toast.error('Cleanup failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Eraser className="h-4 w-4 text-primary" /> OS Disk Cleanup
        </CardTitle>
        <p className="text-xs text-muted">
          Reclaim space on the system disk by shrinking the systemd journal and clearing the
          package cache. Safe to run at any time.
        </p>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-text2">
            Shrink journal ({formatBytes(overview?.journal_bytes || 0)}) to
            <div className="mt-1 flex items-center gap-2">
              <Input type="number" min={50} max={5000} className="h-9 w-24 text-right tabular-nums"
                value={journalMb} onChange={(e) => setJournalMb(e.target.value)} />
              <span className="text-muted">MB</span>
            </div>
          </label>
          <Button variant="outline" disabled={cleanup.isPending} onClick={() => cleanup.mutate(['vacuum_journal'])}>
            {cleanup.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Eraser className="h-4 w-4" />}
            Vacuum journal
          </Button>
          <Button variant="outline" disabled={cleanup.isPending} onClick={() => cleanup.mutate(['apt_clean'])}>
            {cleanup.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            Clear package cache ({formatBytes(overview?.apt_cache_bytes || 0)})
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// ─── 6. Backups ─────────────────────────────────────────────────────────────

function BackupsCard() {
  const qc = useQueryClient()
  const { data: overview } = useOverview()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [includeCh, setIncludeCh] = useState(false)
  const [note, setNote] = useState('')
  const [schedule, setSchedule] = useState<BackupSchedule | null>(null)
  const [restoreTarget, setRestoreTarget] = useState<Backup | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Backup | null>(null)
  const [restoreComponents, setRestoreComponents] = useState<string[]>(['postgres', 'config'])
  const [restartOpen, setRestartOpen] = useState(false)

  const effectiveSchedule = schedule ?? overview?.backup_schedule ?? null

  const { data: backups } = useQuery<Backup[]>({
    queryKey: ['system', 'storage', 'backups'],
    queryFn: async () => (await api.get('/system/storage/backups')).data,
    refetchInterval: (q) => {
      const rows = q.state.data as Backup[] | undefined
      const busy = rows?.some((b) => b.status === 'running' || b.last_restore_status === 'running')
      return busy ? 2500 : 30_000
    },
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['system', 'storage', 'backups'] })

  const create = useMutation({
    mutationFn: async () =>
      (await api.post('/system/storage/backups', {
        include_clickhouse: includeCh, note: note || null,
      })).data,
    onSuccess: (data: any) => { setNote(''); toast.success('Backup started', data.message); invalidate() },
    onError: (e: any) => toast.error('Backup failed to start', apiErrorMessage(e)),
  })

  const restore = useMutation({
    mutationFn: async () =>
      (await api.post(`/system/storage/backups/${restoreTarget!.id}/restore`, {
        components: restoreComponents,
      })).data,
    onSuccess: () => {
      setRestoreTarget(null)
      toast.success('Restore started', 'Track progress in the table — restart services once it completes')
      invalidate()
    },
    onError: (e: any) => toast.error('Restore failed to start', apiErrorMessage(e)),
  })

  const remove = useMutation({
    mutationFn: async () => (await api.delete(`/system/storage/backups/${deleteTarget!.id}`)).data,
    onSuccess: () => { setDeleteTarget(null); toast.success('Backup deleted'); invalidate() },
    onError: (e: any) => { setDeleteTarget(null); toast.error('Delete failed', apiErrorMessage(e)) },
  })

  const saveSchedule = useMutation({
    mutationFn: async () => (await api.put('/system/storage/backups/schedule', effectiveSchedule)).data,
    onSuccess: () => {
      toast.success('Backup schedule saved')
      qc.invalidateQueries({ queryKey: ['system', 'storage', 'overview'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const restart = useMutation({
    mutationFn: async () => (await api.post('/system/storage/restart-services')).data,
    onSuccess: () => { setRestartOpen(false); toast.success('Services restarting', 'The dashboard may briefly disconnect') },
    onError: (e: any) => { setRestartOpen(false); toast.error('Restart failed', apiErrorMessage(e)) },
  })

  async function download(b: Backup) {
    try {
      toast.info('Preparing download…', b.kind === 'full' ? 'Full backups can be large' : undefined)
      const res = await api.get(`/system/storage/backups/${b.id}/download`, { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `zenplus-backup-${b.id}.tar.gz`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error('Download failed', apiErrorMessage(e))
    }
  }

  async function upload(file: File) {
    const fd = new FormData()
    fd.append('file', file)
    try {
      const res = await api.post('/system/storage/backups/upload', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      toast.success('Backup imported', res.data.message)
      invalidate()
    } catch (e) {
      toast.error('Import failed', apiErrorMessage(e))
    }
  }

  const anyRestoreDone = backups?.some((b) => b.last_restore_status === 'completed')

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Archive className="h-4 w-4 text-primary" /> Backup &amp; Restore
        </CardTitle>
        <p className="text-xs text-muted">
          Configuration backups capture the PostgreSQL database (devices, users, settings,
          alert rules), the ClickHouse schema and appliance config files. Full backups also
          snapshot all metrics data in ClickHouse.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Create + upload */}
        <div className="flex flex-wrap items-end gap-3">
          <label className="grow text-xs text-text2">
            Note (optional)
            <Input className="mt-1 h-9" placeholder="e.g. before network re-design"
              value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} />
          </label>
          <label className="flex h-9 items-center gap-2 text-xs text-text2">
            <Switch checked={includeCh} onCheckedChange={setIncludeCh} />
            Include metrics data
          </label>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>
            {create.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Archive className="h-4 w-4" />}
            Create backup
          </Button>
          <input ref={fileInputRef} type="file" accept=".tar.gz,.tgz,application/gzip" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = '' }} />
          <Button variant="outline" type="button" onClick={() => fileInputRef.current?.click()}>
            <Upload className="h-4 w-4" /> Import
          </Button>
        </div>

        {/* Schedule */}
        {effectiveSchedule && (
          <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Clock className="h-4 w-4 text-info" /> Scheduled backups
              </div>
              <Switch checked={effectiveSchedule.enabled}
                onCheckedChange={(v) => setSchedule({ ...effectiveSchedule, enabled: v })} />
            </div>
            {effectiveSchedule.enabled && (
              <div className="mt-3 flex flex-wrap items-end gap-3">
                <label className="text-xs text-text2">
                  Frequency
                  <div className="mt-1 w-28">
                    <Select value={effectiveSchedule.frequency}
                      onValueChange={(v) => setSchedule({ ...effectiveSchedule, frequency: v as 'daily' | 'weekly' })}>
                      <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="daily">Daily</SelectItem>
                        <SelectItem value="weekly">Weekly</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </label>
                {effectiveSchedule.frequency === 'weekly' && (
                  <label className="text-xs text-text2">
                    Day
                    <div className="mt-1 w-32">
                      <Select value={String(effectiveSchedule.weekday)}
                        onValueChange={(v) => setSchedule({ ...effectiveSchedule, weekday: Number(v) })}>
                        <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {WEEKDAYS.map((d, i) => <SelectItem key={d} value={String(i)}>{d}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </div>
                  </label>
                )}
                <label className="text-xs text-text2">
                  At (UTC)
                  <div className="mt-1 flex items-center gap-1">
                    <Input type="number" min={0} max={23} className="h-8 w-16 text-right tabular-nums"
                      value={effectiveSchedule.hour_utc}
                      onChange={(e) => setSchedule({ ...effectiveSchedule, hour_utc: Number(e.target.value) })} />
                    <span className="text-muted">:00</span>
                  </div>
                </label>
                <label className="text-xs text-text2">
                  Keep last
                  <Input type="number" min={1} max={30} className="mt-1 h-8 w-16 text-right tabular-nums"
                    value={effectiveSchedule.keep_last}
                    onChange={(e) => setSchedule({ ...effectiveSchedule, keep_last: Number(e.target.value) })} />
                </label>
                <label className="flex h-8 items-center gap-2 text-xs text-text2">
                  <Switch checked={effectiveSchedule.include_clickhouse}
                    onCheckedChange={(v) => setSchedule({ ...effectiveSchedule, include_clickhouse: v })} />
                  Include metrics data
                </label>
              </div>
            )}
            <div className="mt-2 flex justify-end">
              <Button size="sm" variant="outline" onClick={() => saveSchedule.mutate()} disabled={saveSchedule.isPending}>
                {saveSchedule.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                Save schedule
              </Button>
            </div>
          </div>
        )}

        {/* Backup list */}
        {(backups || []).length === 0 ? (
          <div className="rounded-lg border border-dashed border-border/60 p-6 text-center text-xs text-muted">
            No backups yet. Create one above — it takes a few seconds for a configuration
            backup.
          </div>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>Created</Th>
                <Th>Type</Th>
                <Th>Status</Th>
                <Th className="text-right">Size</Th>
                <Th>Restore</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {backups!.map((b) => (
                <Tr key={b.id}>
                  <Td>
                    <div className="text-sm">{new Date(b.created_at).toLocaleString()}</div>
                    <div className="text-[11px] text-muted">
                      {relativeTime(b.created_at)} · by {b.created_by}
                      {b.note && <> · {b.note}</>}
                    </div>
                    {b.error && <div className="mt-0.5 max-w-md truncate text-[11px] text-danger" title={b.error}>{b.error}</div>}
                  </Td>
                  <Td>
                    <Badge variant={b.kind === 'full' ? 'info' : 'default'}>
                      {b.kind === 'full' ? 'Full' : 'Config'}
                    </Badge>
                  </Td>
                  <Td><StatusBadge status={b.status} /></Td>
                  <Td className="text-right tabular-nums">{b.status === 'completed' ? b.size_human : '—'}</Td>
                  <Td>
                    {b.last_restore_status ? (
                      <div>
                        <StatusBadge status={b.last_restore_status} />
                        {b.last_restore_error && (
                          <div className="mt-0.5 max-w-[16rem] truncate text-[11px] text-danger" title={b.last_restore_error}>
                            {b.last_restore_error}
                          </div>
                        )}
                      </div>
                    ) : <span className="text-xs text-muted">—</span>}
                  </Td>
                  <Td>
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" title="Download" disabled={b.status !== 'completed'}
                        onClick={() => download(b)}>
                        <Download className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" title="Restore" disabled={b.status !== 'completed' || b.last_restore_status === 'running'}
                        onClick={() => {
                          setRestoreComponents(b.include_clickhouse ? ['postgres', 'config', 'clickhouse_data'] : ['postgres', 'config'])
                          setRestoreTarget(b)
                        }}>
                        <RotateCcw className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" title="Delete" disabled={b.status === 'running'}
                        onClick={() => setDeleteTarget(b)}>
                        <Trash2 className="h-4 w-4 text-danger" />
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}

        {anyRestoreDone && (
          <div className="flex items-center justify-between rounded-lg border border-info/30 bg-info/10 px-3 py-2">
            <p className="text-xs text-info">
              A restore has completed. Restart the application services so every component
              picks up the restored data and configuration.
            </p>
            <Button size="sm" variant="outline" onClick={() => setRestartOpen(true)}>
              <RefreshCw className="h-3.5 w-3.5" /> Restart services
            </Button>
          </div>
        )}

        {/* Restore dialog */}
        <Dialog open={restoreTarget !== null} onOpenChange={(o) => !o && setRestoreTarget(null)}>
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <AlertTriangle className="h-5 w-5 text-danger" /> Restore backup
              </DialogTitle>
            </DialogHeader>
            <DialogDescription>
              Restoring overwrites current data with the backup from{' '}
              <span className="font-medium text-text">{restoreTarget && new Date(restoreTarget.created_at).toLocaleString()}</span>.
              Select what to restore:
            </DialogDescription>
            <div className="space-y-2">
              {[
                { id: 'postgres', label: 'Database (devices, users, settings, alert rules)', always: true },
                { id: 'config', label: 'Appliance configuration (.env, poller config)', always: true },
                { id: 'clickhouse_schema', label: 'ClickHouse schema (recreate missing tables)', always: true },
                { id: 'clickhouse_data', label: 'Metrics data (replaces all ClickHouse tables)', always: false },
              ].filter((c) => c.always || restoreTarget?.include_clickhouse).map((c) => (
                <label key={c.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[var(--primary,#3b82f6)]"
                    checked={restoreComponents.includes(c.id)}
                    onChange={(e) =>
                      setRestoreComponents(e.target.checked
                        ? [...restoreComponents, c.id]
                        : restoreComponents.filter((x) => x !== c.id))
                    }
                  />
                  {c.label}
                </label>
              ))}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setRestoreTarget(null)}>Cancel</Button>
              <Button variant="destructive" disabled={restoreComponents.length === 0 || restore.isPending}
                onClick={() => restore.mutate()}>
                {restore.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
                Restore
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <ConfirmDialog
          open={deleteTarget !== null}
          onOpenChange={(o) => !o && setDeleteTarget(null)}
          title="Delete backup?"
          description={`The backup from ${deleteTarget ? new Date(deleteTarget.created_at).toLocaleString() : ''} (${deleteTarget?.size_human || ''}) will be permanently removed.`}
          confirmText="Delete"
          destructive
          loading={remove.isPending}
          onConfirm={() => remove.mutate()}
        />

        <ConfirmDialog
          open={restartOpen}
          onOpenChange={setRestartOpen}
          title="Restart application services?"
          description="The API, poller and NetFlow collector restart. Monitoring pauses for a few seconds and the dashboard may briefly disconnect."
          confirmText="Restart"
          loading={restart.isPending}
          onConfirm={() => restart.mutate()}
        />
      </CardContent>
    </Card>
  )
}

// ─── 7. Activity log ────────────────────────────────────────────────────────

const EVENT_LABELS: Record<string, string> = {
  auto_purge: 'Automatic purge',
  manual_purge: 'Manual purge',
  retention_applied: 'Retention policy applied',
  backup_created: 'Backup created',
  backup_failed: 'Backup failed',
  backup_deleted: 'Backup deleted',
  restore_started: 'Restore started',
  restore_completed: 'Restore completed',
  restore_failed: 'Restore failed',
  os_cleanup: 'OS cleanup',
}

function EventsCard() {
  const { data: events } = useQuery<StorageEvent[]>({
    queryKey: ['system', 'storage', 'events'],
    queryFn: async () => (await api.get('/system/storage/events?limit=30')).data,
    refetchInterval: 30_000,
  })

  if (!events?.length) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Clock className="h-4 w-4 text-primary" /> Storage Activity
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-1.5">
          {events.map((e) => (
            <div key={e.id} className="flex items-baseline justify-between gap-3 rounded-md px-2 py-1 text-xs hover:bg-surface2/40">
              <div className="flex min-w-0 items-baseline gap-2">
                <span className={cn(
                  'font-medium',
                  e.event_type.includes('failed') ? 'text-danger'
                    : e.event_type === 'auto_purge' ? 'text-warning' : 'text-text',
                )}>
                  {EVENT_LABELS[e.event_type] || e.event_type}
                </span>
                <span className="truncate text-muted">
                  {e.freed_bytes > 0 && <>freed {e.freed_human} · </>}
                  by {e.actor}
                </span>
              </div>
              <span className="shrink-0 tabular-nums text-muted">{relativeTime(e.created_at)}</span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
