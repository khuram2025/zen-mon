import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bell,
  BellOff,
  BellRing,
  CheckCircle2,
  ChevronRight,
  Gauge,
  Layers,
  Loader2,
  Network,
  Pencil,
  Plus,
  Search,
  Star,
  Trash2,
  X,
  XCircle,
} from 'lucide-react'
import {
  Area, AreaChart, CartesianGrid, Legend, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import {
  apiErrorMessage, axisRightPad, cn, formatBps, formatBpsAxis, formatBytes,
  timeAxisTickFormatter, timeTicks, timeTooltipLabelFormatter,
} from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { toast } from '@/components/ui/Toast'

type LinkRow = {
  device_id: string
  hostname: string
  device_ip: string
  if_index: number
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_speed: number | null
  configured_speed_bps: number | null
  effective_speed_bps: number
  oper_status: string | null
  in_bps: number
  out_bps: number
  total_bps: number
  avg_in_bps: number
  avg_out_bps: number
  max_in_bps: number
  max_out_bps: number
  util_pct: number | null
  peak_util_pct: number | null
  has_netflow: boolean
  /** Starred by the signed-in user; the server pins these to the top. */
  is_favorite: boolean
  // Interface health — counter increases over the window, not raw readings.
  in_errors: number
  out_errors: number
  in_discards: number
  out_discards: number
  errors: number
  discards: number
  in_pkts: number
  out_pkts: number
  in_pps: number
  out_pps: number
  /** Errors per million frames. null when the device reports no packet counters. */
  error_ppm: number | null
  discard_ppm: number | null
  /** ifOperStatus transitions in the window. */
  flaps: number
  availability_pct: number | null
  health: 'ok' | 'warning' | 'critical'
  issues: string[]
  /** false on SPAN/mirror ports, where ifOperStatus contradicts the traffic. */
  oper_status_reliable?: boolean
}

const ISSUE_META: Record<string, { short: string; label: string }> = {
  errors: { short: 'ERR', label: 'Errored frames' },
  discards: { short: 'DSC', label: 'Discarded frames' },
  flapping: { short: 'FLAP', label: 'Link flapping' },
}

/** Compact count: 397823 → 397.8k. Error counts run to seven figures and the
 *  table column is ~70px, so the raw number would wrap or truncate.
 *  Tolerates undefined so a dashboard built ahead of the API doesn't print
 *  "NaNM" while the older backend is still serving. */
function formatCount(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`
  return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`
}

/** Errors-per-million as a percentage when it's large enough to read that way. */
function formatPpm(ppm: number | null): string {
  if (ppm == null) return 'no packet counters'
  if (ppm >= 10_000) return `${(ppm / 10_000).toFixed(1)}% of frames`
  if (ppm >= 1) return `${Math.round(ppm)} per million`
  if (ppm > 0) return '<1 per million'
  return 'none'
}

function healthTone(h: string | undefined): string {
  if (h === 'critical') return 'text-danger'
  if (h === 'warning') return 'text-warning'
  return 'text-muted'
}

/** Window-average utilisation for a row, in percent. */
function rowAvgUtil(row: LinkRow): number | null {
  if (!row.effective_speed_bps) return null
  return (Math.max(row.avg_in_bps || 0, row.avg_out_bps || 0) / row.effective_speed_bps) * 100
}

type LinkKey = { device_id: string; if_index: number }

type AlertRule = {
  id: string
  name: string
  metric: string
  operator: string
  threshold: number | null
  severity: string
  enabled: boolean
  min_duration: number
  target: string | null
  device_id: string | null
  group_id: string | null
  device_type: string | null
  location: string | null
  conditions: { metric: string; operator: string; threshold: number }[] | null
}

const IF_METRIC_LABELS: Record<string, string> = {
  if_util_pct: 'Utilization',
  if_in_bps: 'Inbound',
  if_out_bps: 'Outbound',
  if_errors: 'Errors',
  if_discards: 'Discards',
  if_oper_status: 'Link state',
}

/** Which existing rules cover this link? Mirrors the server's scope test
 *  (network_alert_service._iface_matches_target): target is an if_index or a
 *  name/descr/alias substring; empty target = every interface on the device.
 *  Rules pinned to another device — or scoped by group/type/location we can't
 *  resolve client-side — are left out rather than shown as a guess. */
function ruleMatchesLink(rule: AlertRule, link: LinkRow): boolean {
  if (!rule.metric?.startsWith('if_')) return false
  if (rule.device_id) {
    if (rule.device_id !== link.device_id) return false
  } else if (rule.group_id || rule.device_type || rule.location) {
    return false
  }
  const t = (rule.target || '').trim().toLowerCase()
  if (!t) return true
  if (/^\d+$/.test(t) && Number(t) === link.if_index) return true
  return [link.if_name, link.if_descr, link.if_alias]
    .some((s) => (s || '').toLowerCase().includes(t))
}

function ruleThresholdLabel(rule: AlertRule): string {
  if (rule.threshold == null) return ''
  if (rule.metric === 'if_util_pct') return `${rule.threshold}%`
  if (rule.metric.includes('bps')) return formatBps(rule.threshold)
  return String(rule.threshold)
}

function formatBucket(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds % 60 === 0) return `${seconds / 60}-min`
  return `${seconds}s`
}

const SORT_LABELS: Record<string, string> = {
  util: 'utilization',
  peak: 'peak utilization',
  traffic: 'total traffic',
  in: 'inbound',
  out: 'outbound',
  name: 'name',
  errors: 'error count',
  discards: 'discard count',
  error_rate: 'error rate',
  flaps: 'flap count',
}

/** Health chips for the table. Renders nothing for a clean link rather than a
 *  green "OK" badge on 3,000 rows, so the eye lands on the exceptions. */
function HealthChips({ row }: { row: LinkRow }) {
  if (!row.issues?.length) {
    return <span className="text-[11px] text-muted">—</span>
  }
  const tone = row.health === 'critical'
    ? 'border-danger/40 bg-danger/10 text-danger'
    : 'border-warning/40 bg-warning/10 text-warning'
  return (
    <div className="flex flex-wrap gap-1">
      {row.issues.map((key) => {
        const meta = ISSUE_META[key]
        if (!meta) return null
        const detail =
          key === 'errors' ? `${row.errors.toLocaleString()} errored frames · ${formatPpm(row.error_ppm)}`
          : key === 'discards' ? `${row.discards.toLocaleString()} discarded frames · ${formatPpm(row.discard_ppm)}`
          : `${row.flaps} link-state change${row.flaps === 1 ? '' : 's'}${row.availability_pct != null ? ` · ${row.availability_pct}% up` : ''}`
        return (
          <span
            key={key}
            title={`${meta.label}: ${detail}`}
            className={cn('rounded border px-1 py-px text-[9px] font-semibold tracking-wide', tone)}
          >
            {meta.short}
          </span>
        )
      })}
    </div>
  )
}

const PROTO_NAMES: Record<number, string> = {
  1: 'ICMP', 6: 'TCP', 17: 'UDP', 47: 'GRE', 50: 'ESP',
}

/** Keep the previous page of results on screen while the next one loads. */
const keepPrev = <T,>(prev: T | undefined) => prev

/** Search is a server-side filter, so every keystroke would otherwise be its
 *  own fleet-wide query. Hold off until typing pauses. */
function useDebounced<T>(value: T, delay = 350): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(id)
  }, [value, delay])
  return debounced
}

function utilTone(pct: number | null | undefined): string {
  if (pct == null) return 'text-muted'
  if (pct >= 90) return 'text-danger'
  if (pct >= 75) return 'text-warning'
  if (pct >= 50) return 'text-info'
  return 'text-success'
}

function utilBarColor(pct: number): string {
  if (pct >= 90) return 'bg-danger'
  if (pct >= 75) return 'bg-warning'
  if (pct >= 50) return 'bg-info'
  return 'bg-success'
}

function UtilBar({ pct, avg, peak, className }: {
  pct: number
  /** Window average %, shown in the tooltip for context. */
  avg?: number | null
  /** Window peak %, drawn as a notch so a bursty link doesn't read as idle. */
  peak?: number | null
  className?: string
}) {
  const w = Math.min(100, Math.max(pct > 0 ? 2 : 0, pct))
  const title = [
    `Current ${pct.toFixed(1)}%`,
    avg != null ? `Avg ${avg.toFixed(1)}%` : null,
    peak != null ? `Peak ${peak.toFixed(1)}%` : null,
  ].filter(Boolean).join(' · ')
  return (
    <div className={cn('flex items-center gap-2', className)} title={title}>
      <div className="relative h-2 flex-1 rounded-full bg-surface2">
        <div className={cn('h-full rounded-full transition-all', utilBarColor(pct))} style={{ width: `${w}%` }} />
        {peak != null && peak > pct + 0.5 && (
          <span
            className="absolute top-1/2 h-3 w-0.5 -translate-y-1/2 rounded-full bg-text/60"
            style={{ left: `${Math.min(100, peak)}%` }}
          />
        )}
      </div>
      <span className={cn('w-11 text-right font-mono text-[11px] tabular-nums', utilTone(pct))}>
        {pct.toFixed(0)}%
      </span>
    </div>
  )
}

/** Star/unstar a link for the signed-in user.
 *
 *  Updates the cached rows before the request lands: the list query is a
 *  fleet-wide aggregate over ClickHouse, so waiting for a refetch would leave
 *  the star visibly dead for a second. The refetch in `onSettled` is what
 *  actually re-pins the row to the top and refreshes the favourites count. */
function useFavoriteToggle() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ link, next }: { link: LinkKey; next: boolean }) => {
      const path = `/link-utilization/favorites/${link.device_id}/${link.if_index}`
      await (next ? api.put(path) : api.delete(path))
      return next
    },
    onMutate: async ({ link, next }) => {
      await qc.cancelQueries({ queryKey: ['link-utilization'] })
      const prev = qc.getQueriesData({ queryKey: ['link-utilization'] })
      // The drill-down query shares the ['link-utilization'] prefix and has no
      // `items`, so leave anything without a row list untouched.
      qc.setQueriesData({ queryKey: ['link-utilization'] }, (old: any) => {
        if (!old?.items) return old
        return {
          ...old,
          items: old.items.map((i: LinkRow) =>
            i.device_id === link.device_id && i.if_index === link.if_index
              ? { ...i, is_favorite: next }
              : i,
          ),
        }
      })
      return { prev }
    },
    onError: (e, _vars, ctx) => {
      ctx?.prev?.forEach(([key, data]) => qc.setQueryData(key, data))
      toast.error(apiErrorMessage(e))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['link-utilization'] }),
  })
}

function FavoriteStar({ row, className }: { row: LinkRow; className?: string }) {
  const toggle = useFavoriteToggle()
  const on = !!row.is_favorite
  return (
    <button
      type="button"
      aria-pressed={on}
      aria-label={on ? 'Remove from favorites' : 'Add to favorites'}
      title={on ? 'Favorite — pinned to the top. Click to unpin.' : 'Add to favorites — pins this link to the top'}
      disabled={toggle.isPending}
      // Rows are clickable (they open the drill-down), so the star must not
      // also select the link.
      onClick={(e) => {
        e.stopPropagation()
        toggle.mutate({ link: { device_id: row.device_id, if_index: row.if_index }, next: !on })
      }}
      className={cn(
        'rounded p-0.5 transition-colors',
        // Kept faint until hovered: at 200 rows a column of bright outlines
        // would compete with the health chips for attention.
        on ? 'text-warning' : 'text-muted/40 hover:text-warning',
        className,
      )}
    >
      <Star className={cn('h-3.5 w-3.5', on && 'fill-current')} />
    </button>
  )
}

function KpiCard({
  label, value, sub, tone, onClick, active,
}: {
  label: string
  value: string
  sub?: string
  tone?: string
  /** Makes the tile a filter toggle. */
  onClick?: () => void
  active?: boolean
}) {
  return (
    <Card
      className={cn(
        onClick && 'cursor-pointer transition-colors hover:border-primary/50',
        active && 'border-primary bg-primary/5',
      )}
      {...(onClick
        ? {
            onClick,
            role: 'button' as const,
            tabIndex: 0,
            'aria-pressed': !!active,
            onKeyDown: (e: React.KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() }
            },
          }
        : {})}
    >
      <CardContent className="p-4">
        <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-muted">
          {label}
          {active && <X className="h-3 w-3 text-primary" />}
        </div>
        <div className={cn('mt-1 text-2xl font-bold tabular-nums', tone)}>{value}</div>
        {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
      </CardContent>
    </Card>
  )
}

export function LinkUtilizationPage() {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'all' | 'up' | 'down'>('all')
  const [minUtil, setMinUtil] = useState<string>('')
  const [issue, setIssue] = useState<string>('')
  const [favoritesOnly, setFavoritesOnly] = useState(false)
  const [sort, setSort] = useState('util')
  const [selected, setSelected] = useState<LinkKey | null>(null)
  const [alertOpen, setAlertOpen] = useState(false)
  /** Rule being edited in the alert dialog; null = creating a new one. */
  const [editRule, setEditRule] = useState<AlertRule | null>(null)

  const debouncedSearch = useDebounced(search)

  const qs = useMemo(() => {
    const p = new URLSearchParams({
      hours: String(range.hours),
      limit: '200',
      sort,
    })
    // A custom window is absolute — without from/to the API would silently
    // substitute "the last N hours".
    if (range.isCustom) {
      p.set('from', range.fromISO)
      p.set('to', range.toISO)
    }
    if (debouncedSearch.trim()) p.set('search', debouncedSearch.trim())
    if (status !== 'all') p.set('status', status)
    if (minUtil) p.set('min_util', minUtil)
    if (issue) p.set('issue', issue)
    if (favoritesOnly) p.set('favorites_only', 'true')
    return p.toString()
  }, [range.hours, range.isCustom, range.fromISO, range.toISO, debouncedSearch, status, minUtil, issue, favoritesOnly, sort])

  const { data, isLoading, isFetching } = useQuery<{
    items: LinkRow[]
    summary: {
      total: number
      high_util: number
      warning_util: number
      with_netflow: number
      /** Favourites present in the current result set. */
      favorites: number
      /** Every star this user holds, whether or not it reported in the window. */
      total_favorites: number
      avg_util: number | null
      /** How many of `total` are actually in `items` (server caps at 200). */
      returned?: number
      with_errors: number
      with_discards: number
      flapping: number
      critical_health: number
      unhealthy: number
      total_errors: number
      total_discards: number
    }
  }>({
    queryKey: ['link-utilization', qs],
    queryFn: async () => (await api.get(`/link-utilization?${qs}`)).data,
    // A pinned historical window never changes — don't re-poll it.
    refetchInterval: range.isCustom ? false : 30_000,
    // Changing a filter or sort should not blank the table behind a spinner.
    placeholderData: keepPrev,
  })

  const items = data?.items || []
  const summary = data?.summary

  const selectedRow = useMemo(
    () => items.find((i) => i.device_id === selected?.device_id && i.if_index === selected?.if_index),
    [items, selected],
  )

  // Drop a selection that the current filters exclude, so the layout doesn't
  // hold a column open for a panel that can no longer render.
  useEffect(() => {
    if (selected && data && !isFetching && !selectedRow) setSelected(null)
  }, [selected, data, isFetching, selectedRow])

  // Link, Status, In, Out, Utilization, Health (+ Errors, Discards, Flow when
  // the detail panel is closed and the table has the full row width).
  const colCount = selectedRow ? 6 : 9

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Gauge className="h-6 w-6 text-primary" />
            Link Utilization
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Fleet-wide interface bandwidth ranked by SNMP utilization. Select a link for traffic charts,
            NetFlow breakdown, and usage alerts.
          </p>
        </div>
        <TimeRangePicker
          rangeIdx={rangeIdx}
          isCustom={isCustom}
          customFrom={range.fromISO}
          customTo={range.toISO}
          onPreset={setPreset}
          onCustom={setCustom}
        />
      </div>

      {/* KPI strip — capacity on the left, health on the right. The health
          tiles double as filters: clicking one scopes the table to those links
          (and clicking again clears it), since "19 links discarding" is only
          useful if you can get to them. */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <KpiCard
          label="Monitored links"
          value={String(summary?.total ?? '—')}
          sub={summary ? `${range.label} · ${summary.with_netflow} with NetFlow` : range.label}
        />
        <KpiCard
          label="High util (≥80%)"
          value={String(summary?.high_util ?? '—')}
          sub={summary ? `${summary.warning_util} elevated (50–79%)` : undefined}
          tone={(summary?.high_util || 0) > 0 ? 'text-danger' : undefined}
        />
        <KpiCard label="Fleet avg util" value={summary?.avg_util != null ? `${summary.avg_util}%` : '—'} />
        <KpiCard
          label="Links with errors"
          value={String(summary?.with_errors ?? '—')}
          sub={summary ? `${formatCount(summary.total_errors)} errored frames` : undefined}
          tone={(summary?.with_errors || 0) > 0 ? 'text-danger' : undefined}
          active={issue === 'errors'}
          onClick={() => setIssue(issue === 'errors' ? '' : 'errors')}
        />
        <KpiCard
          label="Links discarding"
          value={String(summary?.with_discards ?? '—')}
          sub={summary ? `${formatCount(summary.total_discards)} dropped frames` : undefined}
          tone={(summary?.with_discards || 0) > 0 ? 'text-warning' : undefined}
          active={issue === 'discards'}
          onClick={() => setIssue(issue === 'discards' ? '' : 'discards')}
        />
        <KpiCard
          label="Flapping links"
          value={String(summary?.flapping ?? '—')}
          sub="link-state changes"
          tone={(summary?.flapping || 0) > 0 ? 'text-warning' : undefined}
          active={issue === 'flapping'}
          onClick={() => setIssue(issue === 'flapping' ? '' : 'flapping')}
        />
      </div>

      {/* Filters */}
      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 pt-4">
          <div className="relative min-w-[200px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              className="h-9 pl-8"
              placeholder="Search device, interface, alias, IP…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          {/* Favourites are already pinned to the top of every sort; this
              narrows the table to just them. */}
          <Button
            variant={favoritesOnly ? 'default' : 'outline'}
            className="h-9"
            aria-pressed={favoritesOnly}
            title={favoritesOnly ? 'Show all links' : 'Show only your favorites'}
            onClick={() => setFavoritesOnly((v) => !v)}
          >
            <Star className={cn('h-3.5 w-3.5', favoritesOnly && 'fill-current')} />
            Favorites
            {summary?.total_favorites != null && summary.total_favorites > 0 && (
              <span className={cn('text-xs', favoritesOnly ? 'text-white/80' : 'text-muted')}>
                {summary.total_favorites}
              </span>
            )}
          </Button>
          <Select value={status} onValueChange={(v) => setStatus(v as typeof status)}>
            <SelectTrigger className="h-9 w-[130px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All status</SelectItem>
              <SelectItem value="up">Up only</SelectItem>
              <SelectItem value="down">Down only</SelectItem>
            </SelectContent>
          </Select>
          <Select value={issue || 'all'} onValueChange={(v) => setIssue(v === 'all' ? '' : v)}>
            <SelectTrigger className="h-9 w-[150px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All health</SelectItem>
              <SelectItem value="any">Any issue</SelectItem>
              <SelectItem value="errors">Errors only</SelectItem>
              <SelectItem value="discards">Discards only</SelectItem>
              <SelectItem value="flapping">Flapping only</SelectItem>
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={setSort}>
            <SelectTrigger className="h-9 w-[160px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="util">Sort: Utilization</SelectItem>
              <SelectItem value="peak">Sort: Peak util</SelectItem>
              <SelectItem value="traffic">Sort: Total traffic</SelectItem>
              <SelectItem value="in">Sort: Inbound</SelectItem>
              <SelectItem value="out">Sort: Outbound</SelectItem>
              <SelectItem value="name">Sort: Name</SelectItem>
              <SelectItem value="error_rate">Sort: Error rate</SelectItem>
              <SelectItem value="errors">Sort: Error count</SelectItem>
              <SelectItem value="discards">Sort: Discard count</SelectItem>
              <SelectItem value="flaps">Sort: Flap count</SelectItem>
            </SelectContent>
          </Select>
          <Select value={minUtil || 'any'} onValueChange={(v) => setMinUtil(v === 'any' ? '' : v)}>
            <SelectTrigger className="h-9 w-[140px]"><SelectValue placeholder="Min util" /></SelectTrigger>
            <SelectContent>
              {/* Matches on the window's peak — a bursty link that idles
                  between spikes is what this filter exists to find. */}
              <SelectItem value="any">Any util</SelectItem>
              <SelectItem value="50">Peak ≥ 50%</SelectItem>
              <SelectItem value="75">Peak ≥ 75%</SelectItem>
              <SelectItem value="90">Peak ≥ 90%</SelectItem>
            </SelectContent>
          </Select>
          {isFetching && !isLoading && (
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-5">
        {/* Link table */}
        {/* Width must follow the same condition as the panel below, or a
            filtered-out selection leaves 3/5 of the row empty. */}
        <Card className={cn('overflow-hidden', selectedRow ? 'xl:col-span-2' : 'xl:col-span-5')}>
          <CardContent className="p-0">
            <div className="border-b border-border/60 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                <Network className="h-4 w-4 text-primary" />
                Interface links
                {/* The server caps the page — say so rather than let the table
                    look like the complete set. */}
                {summary && summary.returned != null && summary.returned < summary.total && (
                  <span className="text-xs font-normal text-muted">
                    · showing top {summary.returned.toLocaleString()} of {summary.total.toLocaleString()} by {SORT_LABELS[sort] ?? sort}
                  </span>
                )}
                {!favoritesOnly && (summary?.favorites || 0) > 0 && (
                  <span className="inline-flex items-center gap-1 text-xs font-normal text-muted">
                    · <Star className="h-3 w-3 fill-current text-warning" /> favorites pinned to the top
                  </span>
                )}
                <span className="text-xs font-normal text-muted">· click a row for drill-down</span>
              </div>
            </div>
            <div className="max-h-[calc(100vh-22rem)] overflow-auto">
              <Table>
                <THead className="sticky top-0 z-10 bg-surface2/95 backdrop-blur">
                  <Tr>
                    <Th className="pl-4">Link</Th>
                    <Th>Status</Th>
                    <Th className="text-right">In</Th>
                    <Th className="text-right">Out</Th>
                    <Th className="min-w-[140px]">
                      <span title="Bar and number are the latest sample · notch marks the peak in the selected window · hover a bar for avg/peak">
                        Utilization
                      </span>
                    </Th>
                    {/* The detail panel takes 3/5 of the row; drop the numeric
                        health columns there and keep only the compact chips. */}
                    {!selectedRow && (
                      <>
                        <Th className="text-right">
                          <span title="Errored frames in the window, and the rate per million frames. Counter increase, not the raw counter.">
                            Errors
                          </span>
                        </Th>
                        <Th className="text-right">
                          <span title="Discarded frames in the window — usually congestion or policing rather than a fault.">
                            Discards
                          </span>
                        </Th>
                      </>
                    )}
                    <Th className={selectedRow ? 'pr-4' : undefined}>
                      <span title="ERR = errored frames · DSC = discards · FLAP = link-state changes. Hover a chip for detail.">
                        Health
                      </span>
                    </Th>
                    {/* Flow is a one-word badge and the open panel already
                        shows it in its header — drop it before the link name
                        is squeezed into a six-line wrap. */}
                    {!selectedRow && <Th className="pr-4">Flow</Th>}
                  </Tr>
                </THead>
                <TBody>
                  {isLoading && (
                    <Tr><Td colSpan={colCount} className="py-12 text-center text-sm text-muted">
                      <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" /> Loading links…
                    </Td></Tr>
                  )}
                  {!isLoading && items.length === 0 && (
                    <Tr><Td colSpan={colCount} className="py-12 text-center text-sm text-muted">
                      {favoritesOnly
                        ? (summary?.total_favorites
                            ? 'None of your favorites reported traffic in the selected window or match the other filters.'
                            : 'No favorites yet — click the ☆ next to a link to pin it to the top of this table.')
                        : issue
                          ? 'No links with this health problem in the selected window.'
                          : 'No interfaces match — ensure SNMP polling is active and interfaces are monitored.'}
                    </Td></Tr>
                  )}
                  {items.map((row) => {
                    const isSel = selected?.device_id === row.device_id && selected?.if_index === row.if_index
                    const isUp = row.oper_status === 'up'
                    const label = row.if_alias || row.if_name || row.if_descr || `if${row.if_index}`
                    return (
                      <Tr
                        key={`${row.device_id}-${row.if_index}`}
                        onClick={() => setSelected({ device_id: row.device_id, if_index: row.if_index })}
                        className={cn(
                          'cursor-pointer transition-colors',
                          isSel ? 'bg-primary/10' : 'hover:bg-surface2/40',
                          !isUp && 'opacity-70',
                        )}
                      >
                        <Td className="py-2.5 pl-4">
                          <div className="flex items-center gap-1.5">
                            <FavoriteStar row={row} />
                            <ChevronRight className={cn('h-3.5 w-3.5 text-muted transition-transform', isSel && 'rotate-90 text-primary')} />
                            <div>
                              <div className="text-sm font-medium">{label}</div>
                              <div className="text-[11px] text-muted">
                                <Link
                                  to={`/devices/${row.device_id}`}
                                  className="hover:text-primary"
                                  onClick={(e) => e.stopPropagation()}
                                >
                                  {row.hostname}
                                </Link>
                                {row.effective_speed_bps > 0 && (
                                  <span className="ml-1.5">· {formatBps(row.effective_speed_bps)}</span>
                                )}
                              </div>
                            </div>
                          </div>
                        </Td>
                        <Td>
                          {isUp ? (
                            <Badge variant="success" className="gap-1"><CheckCircle2 className="h-3 w-3" /> Up</Badge>
                          ) : (
                            <Badge variant="danger" className="gap-1"><XCircle className="h-3 w-3" /> Down</Badge>
                          )}
                        </Td>
                        <Td className="text-right font-mono text-xs tabular-nums">
                          <span
                            className="inline-flex items-center gap-0.5 text-success"
                            title={`Latest sample · avg ${formatBps(row.avg_in_bps || 0)} · max ${formatBps(row.max_in_bps || 0)} in window`}
                          >
                            <ArrowDown className="h-3 w-3" />{row.in_bps > 0 ? formatBps(row.in_bps) : '—'}
                          </span>
                        </Td>
                        <Td className="text-right font-mono text-xs tabular-nums">
                          <span
                            className="inline-flex items-center gap-0.5 text-info"
                            title={`Latest sample · avg ${formatBps(row.avg_out_bps || 0)} · max ${formatBps(row.max_out_bps || 0)} in window`}
                          >
                            <ArrowUp className="h-3 w-3" />{row.out_bps > 0 ? formatBps(row.out_bps) : '—'}
                          </span>
                        </Td>
                        <Td>
                          {row.util_pct != null ? (
                            <UtilBar pct={row.util_pct} avg={rowAvgUtil(row)} peak={row.peak_util_pct} />
                          ) : (
                            <span className="text-xs text-muted">—</span>
                          )}
                        </Td>
                        {!selectedRow && (
                          <>
                            <Td className="text-right font-mono text-xs tabular-nums">
                              {row.errors > 0 ? (
                                <span
                                  className={row.issues?.includes('errors') ? 'text-danger' : 'text-muted'}
                                  title={`${row.errors.toLocaleString()} errored frames · ${formatPpm(row.error_ppm)}`}
                                >
                                  {formatCount(row.errors)}
                                </span>
                              ) : (
                                <span className="text-muted">—</span>
                              )}
                            </Td>
                            <Td className="text-right font-mono text-xs tabular-nums">
                              {row.discards > 0 ? (
                                <span
                                  className={row.issues?.includes('discards') ? 'text-warning' : 'text-muted'}
                                  title={`${row.discards.toLocaleString()} discarded frames · ${formatPpm(row.discard_ppm)}`}
                                >
                                  {formatCount(row.discards)}
                                </span>
                              ) : (
                                <span className="text-muted">—</span>
                              )}
                            </Td>
                          </>
                        )}
                        <Td className={selectedRow ? 'pr-4' : undefined}><HealthChips row={row} /></Td>
                        {!selectedRow && (
                          <Td className="pr-4">
                            {row.has_netflow ? (
                              <Badge variant="info" className="text-[10px]">NetFlow</Badge>
                            ) : (
                              <span className="text-xs text-muted">SNMP</span>
                            )}
                          </Td>
                        )}
                      </Tr>
                    )
                  })}
                </TBody>
              </Table>
            </div>
          </CardContent>
        </Card>

        {/* Detail panel */}
        {selected && selectedRow && (
          <div className="xl:col-span-3">
            <LinkDetailPanel
              key={`${selected.device_id}-${selected.if_index}`}
              link={selectedRow}
              range={range}
              onClose={() => setSelected(null)}
              onAlert={() => { setEditRule(null); setAlertOpen(true) }}
              onEditAlert={(rule) => { setEditRule(rule); setAlertOpen(true) }}
            />
          </div>
        )}
      </div>

      {selectedRow && (
        <LinkAlertDialog
          open={alertOpen}
          onOpenChange={(v) => { setAlertOpen(v); if (!v) setEditRule(null) }}
          link={selectedRow}
          rule={editRule}
        />
      )}
    </div>
  )
}

function LinkDetailPanel({
  link, range, onClose, onAlert, onEditAlert,
}: {
  link: LinkRow
  range: { hours: number; fromISO: string; toISO: string; isCustom: boolean; label: string }
  onClose: () => void
  onAlert: () => void
  onEditAlert: (rule: AlertRule) => void
}) {
  const label = link.if_alias || link.if_name || link.if_descr || `if${link.if_index}`
  const { hours, label: rangeLabel } = range
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)

  const detailQs = useMemo(() => {
    const p = new URLSearchParams({ hours: String(range.hours) })
    if (range.isCustom) {
      p.set('from', range.fromISO)
      p.set('to', range.toISO)
    }
    return p.toString()
  }, [range.hours, range.isCustom, range.fromISO, range.toISO])

  const { data, isLoading } = useQuery<{
    traffic: { ts: number; in_bps: number; out_bps: number; in_peak_bps?: number; out_peak_bps?: number }[]
    /** Per-sample/per-bucket counter increases — not the raw counter readings. */
    errors: { ts: number; in_errors: number; out_errors: number; in_discards: number; out_discards: number }[]
    summary: Record<string, number>
    health?: {
      errors: number; discards: number
      in_errors: number; out_errors: number; in_discards: number; out_discards: number
      error_ppm: number | null; discard_ppm: number | null
      in_pps: number; out_pps: number
      flaps: number; availability_pct: number | null
      health: 'ok' | 'warning' | 'critical'; issues: string[]
      /** false on SPAN/mirror ports, where ifOperStatus contradicts the traffic. */
      oper_status_reliable?: boolean
    }
    /** 0 = raw samples; otherwise the plotted line is a per-bucket average. */
    bucket_seconds?: number
    netflow: {
      has_flows: boolean
      timeseries: { ts: number; in_bps: number; out_bps: number; bytes: number }[]
      top_talkers: { ip: string; bytes: number; packets: number; flows: number }[]
      protocols: { protocol: number; bytes: number }[]
    }
  }>({
    queryKey: ['link-utilization', 'detail', link.device_id, link.if_index, detailQs],
    queryFn: async () =>
      (await api.get(`/link-utilization/${link.device_id}/${link.if_index}?${detailQs}`)).data,
    refetchInterval: range.isCustom ? false : 30_000,
    // Range switches on an already-open panel keep the old chart in place
    // instead of collapsing to a spinner. Link switches remount (keyed), so
    // stale data never appears under another interface's name.
    placeholderData: keepPrev,
  })

  const traffic = data?.traffic || []
  const nf = data?.netflow
  const sum = data?.summary || {}
  const health = data?.health
  const speed = link.effective_speed_bps

  /* Errors and discards per bucket. Kept separate from the bandwidth series so
     a flat-zero error line doesn't imply "no data" on a link that simply has
     no faults — the section states that explicitly instead. */
  const errorSeries = data?.errors || []
  const hasErrorEvents = useMemo(
    () => errorSeries.some((p) => p.in_errors || p.out_errors || p.in_discards || p.out_discards),
    [errorSeries],
  )
  // HH:mm alone repeats itself once the window spans more than a day.
  const tickFormatter = useMemo(() => timeAxisTickFormatter(hours), [hours])
  const ticks = useMemo(() => timeTicks(fromTs, toTs, hours), [fromTs, toTs, hours])

  /* Wide windows bucket the samples, so the plotted average hides the bursts
     that "IN MAX" reports. Draw the per-bucket peak behind the average so the
     chart's ceiling matches the stat above it. */
  const bucketSeconds = data?.bucket_seconds ?? 0
  const isAveraged = bucketSeconds > 0

  const chartData = useMemo(() => {
    const nfMap = new Map((nf?.timeseries || []).map((p) => [p.ts, p]))
    return traffic.map((p) => {
      const nfPt = nfMap.get(p.ts)
      const peakIn = p.in_peak_bps ?? p.in_bps
      const peakOut = p.out_peak_bps ?? p.out_bps
      return {
        ...p,
        in_peak_bps: peakIn,
        out_peak_bps: peakOut,
        // Utilisation follows the peak: a burst to 100% matters even if the
        // five-minute average around it looks calm.
        util_in: speed > 0 ? (peakIn / speed) * 100 : 0,
        util_out: speed > 0 ? (peakOut / speed) * 100 : 0,
        nf_in: nfPt?.in_bps,
        nf_out: nfPt?.out_bps,
      }
    })
  }, [traffic, nf, speed])

  /* Throughput above the reported link speed means ifSpeed is wrong, not that
     the link is 686% loaded — some ports report a stale 10 Mbps after
     renegotiating. Give the axis a rounded ceiling so the 50/80% guide lines
     stay readable, and say what the reader is looking at. */
  const peakUtil = useMemo(
    () => chartData.reduce((m, p) => Math.max(m, p.util_in || 0, p.util_out || 0), 0),
    [chartData],
  )
  const overCapacity = peakUtil > 100
  const utilAxisMax = overCapacity ? Math.ceil(peakUtil / 50) * 50 : 100

  /* Show the capacity line once traffic reaches half the link speed: the axis
     extends to 100%, making remaining headroom visible. Below that, scaling a
     quiet link's chart to capacity would flatten the traffic into a ribbon. */
  const maxTrafficBps = useMemo(
    () => chartData.reduce((m, p) => Math.max(m, p.in_peak_bps || 0, p.out_peak_bps || 0), 0),
    [chartData],
  )
  const showCapacityLine = speed > 0 && maxTrafficBps >= speed * 0.5

  const avgUtil =
    speed > 0 && (sum.in_avg_bps != null || sum.out_avg_bps != null)
      ? (Math.max(sum.in_avg_bps || 0, sum.out_avg_bps || 0) / speed) * 100
      : null

  return (
    <Card className="sticky top-4">
      <CardContent className="space-y-4 pt-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="flex items-center gap-2">
              <FavoriteStar row={link} className="p-0" />
              <h2 className="text-lg font-semibold">{label}</h2>
              {link.has_netflow && <Badge variant="info">NetFlow</Badge>}
              {link.oper_status === 'up' ? (
                <Badge variant="success">Up</Badge>
              ) : (
                <Badge variant="danger">Down</Badge>
              )}
            </div>
            <p className="mt-0.5 text-sm text-muted">
              <Link to={`/devices/${link.device_id}`} className="hover:text-primary">{link.hostname}</Link>
              {' · '}{link.device_ip.replace(/\/\d+$/, '')}
              {speed > 0 && <> · {formatBps(speed)} capacity</>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="outline" onClick={onAlert}>
              <Bell className="h-3.5 w-3.5" /> Create alert
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close">
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Mini stats — current + window average + window peak, so a bursty
            link reads as bursty rather than whatever the last sample was. */}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          <MiniStat label="Current util" value={sum.util_pct != null ? `${sum.util_pct.toFixed(1)}%` : '—'} tone={utilTone(sum.util_pct)} />
          <MiniStat label="Avg util" value={avgUtil != null ? `${avgUtil.toFixed(1)}%` : '—'} tone={utilTone(avgUtil)} />
          <MiniStat label="Peak util" value={sum.peak_util_pct != null ? `${sum.peak_util_pct.toFixed(1)}%` : '—'} tone={utilTone(sum.peak_util_pct)} />
          <MiniStat
            label="In max"
            value={sum.in_max_bps ? formatBps(sum.in_max_bps) : '—'}
            sub={sum.in_avg_bps != null ? `avg ${formatBps(sum.in_avg_bps)}` : undefined}
          />
          <MiniStat
            label="Out max"
            value={sum.out_max_bps ? formatBps(sum.out_max_bps) : '—'}
            sub={sum.out_avg_bps != null ? `avg ${formatBps(sum.out_avg_bps)}` : undefined}
          />
          {/* Unicast only — ifInUcastPkts excludes broadcast/multicast, and a
              frame that errored or was discarded never reaches this counter.
              Saying so keeps "0 pps" next to live bandwidth from reading as a
              bug: on a fully-errored port that is exactly the right answer. */}
          <MiniStat
            label="Unicast rate"
            value={health ? `${Math.round(health.in_pps + health.out_pps).toLocaleString()} pps` : '—'}
            sub={health ? `${Math.round(health.in_pps).toLocaleString()} in · ${Math.round(health.out_pps).toLocaleString()} out` : undefined}
          />
        </div>

        {/* Interface health */}
        <div>
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3">
            <h3 className="text-sm font-medium">Interface health · {rangeLabel}</h3>
            <span className="text-[11px] text-muted">
              counter increase over the window, reset-safe
            </span>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <MiniStat
              label="Errored frames"
              value={health ? health.errors.toLocaleString() : '—'}
              sub={health
                ? (health.errors > 0
                    ? `${formatPpm(health.error_ppm)} · ${health.in_errors.toLocaleString()} in / ${health.out_errors.toLocaleString()} out`
                    : 'clean')
                : undefined}
              tone={health?.issues?.includes('errors') ? healthTone(health.health) : undefined}
            />
            <MiniStat
              label="Discarded frames"
              value={health ? health.discards.toLocaleString() : '—'}
              sub={health
                ? (health.discards > 0
                    ? `${formatPpm(health.discard_ppm)} · ${health.in_discards.toLocaleString()} in / ${health.out_discards.toLocaleString()} out`
                    : 'clean')
                : undefined}
              tone={health?.issues?.includes('discards') ? 'text-warning' : undefined}
            />
            {/* On a SPAN/mirror port the agent reports ifOperStatus=down while
                the port forwards, so availability and flap counts are noise —
                say that rather than print a contradictory "Up · 0% available". */}
            <MiniStat
              label="Link stability"
              value={
                !health ? '—'
                : health.oper_status_reliable === false ? 'n/a'
                : `${health.availability_pct}%`
              }
              sub={
                !health ? undefined
                : health.oper_status_reliable === false
                  ? 'port reports down while forwarding'
                  : health.flaps > 0
                    ? `${health.flaps} state change${health.flaps === 1 ? '' : 's'}`
                    : 'no state changes'
              }
              tone={health?.issues?.includes('flapping') ? 'text-warning' : undefined}
            />
          </div>

          {/* Only draw the chart when something actually happened — an all-zero
              plot reads as a broken chart rather than a healthy link. */}
          {hasErrorEvents ? (
            <div className="mt-2 h-40 rounded-lg border border-border/60 bg-surface2/20 p-2">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={errorSeries} margin={{ top: 8, right: axisRightPad(hours), bottom: 0, left: 0 }}>
                  <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.4} strokeDasharray="3 3" vertical={false} />
                  <XAxis
                    dataKey="ts" type="number" scale="time" domain={[fromTs, toTs]}
                    ticks={ticks} interval={0} tickFormatter={tickFormatter}
                    tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false}
                  />
                  <YAxis
                    width={52} allowDecimals={false}
                    tickFormatter={(v) => formatCount(Number(v))}
                    tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false}
                  />
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={timeTooltipLabelFormatter}
                    formatter={(v: number, name: string) => [Number(v).toLocaleString(), name]}
                  />
                  <Legend verticalAlign="top" align="right" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  <Area type="monotone" dataKey="in_errors" name="In errors" stroke="rgb(var(--danger))" fill="rgb(var(--danger))" fillOpacity={0.2} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                  <Area type="monotone" dataKey="out_errors" name="Out errors" stroke="rgb(var(--warning))" fill="rgb(var(--warning))" fillOpacity={0.15} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                  <Area type="monotone" dataKey="in_discards" name="In discards" stroke="rgb(var(--info))" fill="none" strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
                  <Area type="monotone" dataKey="out_discards" name="Out discards" stroke="rgb(var(--accent))" fill="none" strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="mt-2 rounded-lg border border-border/60 bg-surface2/20 px-3 py-2.5 text-[11px] text-muted">
              {isLoading
                ? 'Loading…'
                : 'No errors or discards recorded on this interface in the selected window.'}
            </div>
          )}
        </div>

        {/* Bandwidth chart */}
        <div>
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3">
            <h3 className="text-sm font-medium">Bandwidth · {rangeLabel}</h3>
            <span className="text-[11px] text-muted">
              {isAveraged
                ? `SNMP polling · ${formatBucket(bucketSeconds)} average, peak shaded`
                : 'SNMP polling · raw samples'}
            </span>
          </div>
          <div className="h-56 rounded-lg border border-border/60 bg-surface2/20 p-2">
            {isLoading ? (
              <div className="flex h-full items-center justify-center text-sm text-muted">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : chartData.length === 0 ? (
              <div className="flex h-full items-center justify-center text-sm text-muted">No traffic in this window</div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 8, right: axisRightPad(hours), bottom: 0, left: 0 }}>
                  <defs>
                    <linearGradient id="linkIn" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.4} />
                      <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="linkOut" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.4} />
                      <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.4} strokeDasharray="3 3" vertical={false} />
                  {/* Numeric time scale over the selected window: a category
                      axis spaces points by index, hiding polling gaps and
                      stretching partial data across the full width. */}
                  <XAxis
                    dataKey="ts"
                    type="number"
                    scale="time"
                    domain={[fromTs, toTs]}
                    ticks={ticks}
                    interval={0}
                    tickFormatter={tickFormatter}
                    tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    width={52}
                    tickFormatter={(v) => formatBpsAxis(Number(v))}
                    tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                    axisLine={false}
                    tickLine={false}
                  />
                  {/* extendDomain pins the y-axis to link capacity so the
                      remaining headroom is visible; gated so quiet links
                      aren't flattened against a far-away ceiling. */}
                  {showCapacityLine && (
                    <ReferenceLine
                      y={speed}
                      ifOverflow="extendDomain"
                      stroke="rgb(var(--warning))"
                      strokeDasharray="4 4"
                      // Bottom-left: the legend owns the top-right corner.
                      label={{ value: `100% · ${formatBps(speed)}`, fontSize: 10, fill: 'rgb(var(--warning))', position: 'insideBottomLeft' }}
                    />
                  )}
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={timeTooltipLabelFormatter}
                    formatter={(v: number, name: string) => [formatBps(v), name]}
                  />
                  <Legend verticalAlign="top" align="right" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  {/* Peaks first so the averages draw on top of them. */}
                  {isAveraged && (
                    <Area type="monotone" dataKey="in_peak_bps" name="Inbound peak" stroke="rgb(var(--success))" strokeOpacity={0.45} fill="rgb(var(--success))" fillOpacity={0.1} strokeWidth={1} dot={false} isAnimationActive={false} />
                  )}
                  {isAveraged && (
                    <Area type="monotone" dataKey="out_peak_bps" name="Outbound peak" stroke="rgb(var(--info))" strokeOpacity={0.45} fill="rgb(var(--info))" fillOpacity={0.1} strokeWidth={1} dot={false} isAnimationActive={false} />
                  )}
                  <Area type="monotone" dataKey="in_bps" name="Inbound" stroke="rgb(var(--success))" fill="url(#linkIn)" strokeWidth={2} dot={false} isAnimationActive={false} />
                  <Area type="monotone" dataKey="out_bps" name="Outbound" stroke="rgb(var(--info))" fill="url(#linkOut)" strokeWidth={2} dot={false} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>

        {/* Utilization % chart */}
        {speed > 0 && chartData.length > 0 && (
          <div>
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-sm font-medium">
                Utilization %
                {isAveraged && <span className="ml-1.5 font-normal text-muted">· peak per {formatBucket(bucketSeconds)}</span>}
              </h3>
              {overCapacity && (
                <span
                  className="text-[11px] text-warning"
                  title="Throughput exceeds the interface speed reported over SNMP, so the percentage is not meaningful. Set a manual speed override for this interface to correct it."
                >
                  Exceeds reported {formatBps(speed)} capacity — link speed looks misreported
                </span>
              )}
            </div>
            <div className="h-36 rounded-lg border border-border/60 bg-surface2/20 p-2">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.4} strokeDasharray="3 3" vertical={false} />
                  {/* Same numeric domain as the bandwidth chart so the two
                      stay vertically aligned and gaps line up. */}
                  <XAxis dataKey="ts" type="number" scale="time" domain={[fromTs, toTs]} hide />
                  <YAxis width={44} domain={[0, utilAxisMax]} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} />
                  <ReferenceLine y={80} stroke="rgb(var(--danger))" strokeDasharray="3 3" strokeOpacity={0.6} />
                  <ReferenceLine y={50} stroke="rgb(var(--warning))" strokeDasharray="3 3" strokeOpacity={0.5} />
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={timeTooltipLabelFormatter}
                    formatter={(v: number, name: string) => [`${v.toFixed(1)}%`, name]}
                  />
                  <Area type="monotone" dataKey="util_in" name="In util" stroke="rgb(var(--success))" fill="rgb(var(--success))" fillOpacity={0.15} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                  <Area type="monotone" dataKey="util_out" name="Out util" stroke="rgb(var(--info))" fill="rgb(var(--info))" fillOpacity={0.15} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* NetFlow section */}
        {link.has_netflow && (
          <div className="rounded-lg border border-info/20 bg-info/5 p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="flex items-center gap-2 text-sm font-semibold">
                <Layers className="h-4 w-4 text-info" />
                NetFlow overlay
              </h3>
              <Link
                to={`/netflow?exporter=${encodeURIComponent(link.device_ip)}&iface=${link.if_index}`}
                className="text-xs text-info hover:underline"
              >
                Open in NetFlow explorer →
              </Link>
            </div>

            {!nf?.has_flows ? (
              <p className="text-xs text-muted">No flow records for this interface in {rangeLabel}.</p>
            ) : (
              <div className="space-y-4">
                <div className="h-32 rounded-md border border-border/40 bg-surface/60 p-2">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={nf.timeseries} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                      <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.3} vertical={false} />
                      <XAxis dataKey="ts" type="number" scale="time" domain={[fromTs, toTs]} hide />
                      <YAxis width={44} tickFormatter={(v) => formatBpsAxis(Number(v))} tick={{ fontSize: 9, fill: 'rgb(var(--muted))' }} />
                      <Tooltip
                        contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 11 }}
                        labelFormatter={timeTooltipLabelFormatter}
                        formatter={(v: number, n: string) => [formatBps(v), n === 'in_bps' ? 'Flow in' : 'Flow out']}
                      />
                      <Area type="monotone" dataKey="in_bps" stroke="rgb(var(--success))" fill="rgb(var(--success))" fillOpacity={0.2} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                      <Area type="monotone" dataKey="out_bps" stroke="rgb(var(--info))" fill="rgb(var(--info))" fillOpacity={0.2} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Top talkers</h4>
                    <div className="space-y-1">
                      {(nf.top_talkers || []).slice(0, 6).map((t) => (
                        <div key={t.ip} className="flex items-center justify-between text-xs">
                          <span className="font-mono">{t.ip}</span>
                          <span className="text-muted">{formatBytes(t.bytes)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Protocols</h4>
                    <div className="space-y-1">
                      {(nf.protocols || []).map((p) => (
                        <div key={p.protocol} className="flex items-center justify-between text-xs">
                          <span>{PROTO_NAMES[p.protocol] || `Proto ${p.protocol}`}</span>
                          <span className="text-muted">{formatBytes(p.bytes)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        <LinkAlertsSection link={link} onCreate={onAlert} onEdit={onEditAlert} />

        <div className="flex flex-wrap gap-2 border-t border-border/50 pt-3">
          <Button size="sm" variant="outline" asChild>
            <Link to={`/devices/${link.device_id}/interfaces`}>
              <Activity className="h-3.5 w-3.5" /> All interfaces
            </Link>
          </Button>
          {link.has_netflow && (
            <Button size="sm" variant="outline" asChild>
              <Link to={`/netflow/devices/${encodeURIComponent(link.device_ip)}`}>
                <Network className="h-3.5 w-3.5" /> NetFlow device
              </Link>
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function LinkAlertsSection({
  link, onCreate, onEdit,
}: {
  link: LinkRow
  onCreate: () => void
  onEdit: (rule: AlertRule) => void
}) {
  const qc = useQueryClient()
  // Must return a bare array: this cache key is shared with AlertRulesPage and
  // RoutingTab, which map over it directly. Caching the raw {data: [...]}
  // envelope here meant that visiting Link Utilization first left an object
  // under the key, and Alert Rules then died on "(rules || []).map is not a
  // function". Every reader of ['alert-rules'] normalises the same way.
  const { data, isLoading } = useQuery<AlertRule[]>({
    queryKey: ['alert-rules'],
    queryFn: async () => {
      const r = (await api.get('/alert-rules')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  const rules = useMemo(
    () => (data || []).filter((r) => ruleMatchesLink(r, link)),
    [data, link],
  )

  const toggle = useMutation({
    mutationFn: async (id: string) => (await api.post(`/alert-rules/${id}/toggle`)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alert-rules'] }),
    onError: (e) => toast.error(apiErrorMessage(e)),
  })
  const remove = useMutation({
    mutationFn: async (id: string) => api.delete(`/alert-rules/${id}`),
    onSuccess: () => {
      toast.success('Alert rule deleted')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  return (
    <div className="rounded-lg border border-border/60 bg-surface2/20 p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Bell className="h-4 w-4 text-warning" />
          Configured alerts
          {rules.length > 0 && (
            <Badge variant="outline" className="text-[10px]">{rules.length}</Badge>
          )}
        </h3>
        <Button size="sm" variant="outline" onClick={onCreate}>
          <Plus className="h-3.5 w-3.5" /> Add rule
        </Button>
      </div>

      {isLoading ? (
        <div className="py-2 text-xs text-muted">
          <Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" /> Loading rules…
        </div>
      ) : rules.length === 0 ? (
        <p className="text-xs text-muted">
          No alert rules cover this interface yet — add one to get notified on high
          utilization, errors, or link state changes.
        </p>
      ) : (
        <div className="space-y-1.5">
          {rules.map((r) => {
            // Rules with several AND/OR conditions can't be edited by this
            // simple dialog without dropping the other conditions.
            const compound = (r.conditions?.length || 0) > 1
            return (
              <div
                key={r.id}
                className={cn(
                  'flex flex-wrap items-center gap-2 rounded-md border border-border/50 bg-surface/60 px-3 py-2',
                  !r.enabled && 'opacity-60',
                )}
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium">{r.name}</div>
                  <div className="text-[11px] text-muted">
                    {compound
                      ? `${r.conditions!.length} conditions`
                      : `${IF_METRIC_LABELS[r.metric] || r.metric} ${r.operator} ${ruleThresholdLabel(r)}`}
                    {r.min_duration > 0 && ` · sustained ${Math.round(r.min_duration / 60)}m`}
                    {' · '}
                    {r.target ? 'this interface' : 'all interfaces'}
                    {!r.device_id && ' · all devices'}
                  </div>
                </div>
                <Badge
                  variant={r.severity === 'critical' ? 'danger' : r.severity === 'warning' ? 'warning' : 'info'}
                  className="text-[10px]"
                >
                  {r.severity}
                </Badge>
                <div className="flex items-center gap-0.5">
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-7 p-0"
                    title={r.enabled ? 'Disable rule' : 'Enable rule'}
                    disabled={toggle.isPending}
                    onClick={() => toggle.mutate(r.id)}
                  >
                    {r.enabled
                      ? <BellRing className="h-3.5 w-3.5 text-success" />
                      : <BellOff className="h-3.5 w-3.5 text-muted" />}
                  </Button>
                  {compound ? (
                    <Button size="sm" variant="ghost" className="h-7 w-7 p-0" title="Compound rule — edit in Alert Rules" asChild>
                      <Link to="/alert-rules"><Pencil className="h-3.5 w-3.5" /></Link>
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-7 w-7 p-0"
                      title="Edit rule"
                      onClick={() => onEdit(r)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-7 p-0"
                    title="Delete rule"
                    disabled={remove.isPending}
                    onClick={() => {
                      if (window.confirm(`Delete alert rule "${r.name}"?`)) remove.mutate(r.id)
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5 text-danger" />
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function MiniStat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="rounded-md border border-border/50 bg-surface2/30 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('text-sm font-semibold tabular-nums', tone)}>{value}</div>
      {sub && <div className="text-[10px] tabular-nums text-muted">{sub}</div>}
    </div>
  )
}

function LinkAlertDialog({
  open, onOpenChange, link, rule,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  link: LinkRow
  /** When set, the dialog edits this existing rule instead of creating one. */
  rule?: AlertRule | null
}) {
  const qc = useQueryClient()
  const isEdit = !!rule
  const label = link.if_alias || link.if_name || `if${link.if_index}`
  const [name, setName] = useState('')
  const [metric, setMetric] = useState('if_util_pct')
  const [threshold, setThreshold] = useState('80')
  const [severity, setSeverity] = useState('warning')
  const [minutes, setMinutes] = useState('5')

  // The dialog stays mounted across open/close, so seed the fields on every
  // open: from the rule when editing, back to defaults when creating.
  useEffect(() => {
    if (!open) return
    setName(rule?.name ?? '')
    setMetric(rule?.metric ?? 'if_util_pct')
    setThreshold(rule?.threshold != null ? String(rule.threshold) : '80')
    setSeverity(rule?.severity ?? 'warning')
    setMinutes(rule ? String(Math.round((rule.min_duration || 0) / 60)) : '5')
  }, [open, rule])

  const create = useMutation({
    mutationFn: async () => {
      const body = {
        name: name || `${label} high utilization`,
        metric,
        threshold: Number(threshold),
        severity,
        min_duration: Math.round(Number(minutes) * 60),
      }
      if (isEdit) {
        // Operator and scope are preserved. The engine reads the flat fields,
        // but a rule saved with a conditions array keeps it as the source of
        // truth in the Alert Rules editor — resend it so both stay in step.
        const withConditions = rule!.conditions?.length === 1
          ? {
              ...body,
              conditions: [{
                metric,
                operator: rule!.operator || '>',
                threshold: Number(threshold),
              }],
            }
          : body
        return (await api.put(`/alert-rules/${rule!.id}`, withConditions)).data
      }
      return (await api.post('/alert-rules', {
        ...body,
        operator: '>',
        device_id: link.device_id,
        target: link.if_name || String(link.if_index),
        enabled: true,
        notify_channels: [],
      })).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Alert rule updated' : 'Alert rule created')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-warning" />
            {isEdit ? `Edit alert · ${rule!.name}` : `Usage alert for ${label}`}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <p className="text-xs text-muted">
            Alert on <strong>{link.hostname}</strong> · interface <strong>{label}</strong>
          </p>
          <div>
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={`${label} high utilization`} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Metric</Label>
              <Select value={metric} onValueChange={setMetric}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="if_util_pct">Utilization %</SelectItem>
                  <SelectItem value="if_in_bps">Inbound bps</SelectItem>
                  <SelectItem value="if_out_bps">Outbound bps</SelectItem>
                  <SelectItem value="if_errors">Errors (window)</SelectItem>
                  <SelectItem value="if_discards">Discards (window)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Severity</Label>
              <Select value={severity} onValueChange={setSeverity}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="warning">Warning</SelectItem>
                  <SelectItem value="critical">Critical</SelectItem>
                  <SelectItem value="info">Info</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Threshold {metric === 'if_util_pct' ? '%' : metric.includes('bps') ? '(bps)' : ''}</Label>
              <Input type="number" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
            </div>
            <div>
              <Label>Sustained (min)</Label>
              <Input type="number" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>
            {create.isPending
              ? <Loader2 className="h-4 w-4 animate-spin" />
              : isEdit ? 'Save changes' : 'Create rule'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
