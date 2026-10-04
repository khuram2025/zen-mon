/**
 * Alert Center — the operations backlog.
 *
 * Alerts development review A01, A11–A18, A25–A27; F01, F03, F06, F09, F10, F12.
 *  - The URL is the single source of truth for tab, filters, sort and page;
 *    invalid values are normalised and unrelated params are preserved.
 *  - The list is server-paged with a stated total; nothing (counts, MTTR,
 *    critical banner) is computed from the loaded page.
 *  - Summary figures come from /alerts/summary with the same filters and say
 *    whether they are organisation-wide or filtered; failures never read as 0.
 *  - Relative windows are rolling on the appliance clock (`hours=`), never
 *    frozen browser-computed bounds; custom from/to are preserved verbatim.
 *  - Triage controls appear only with alerts.acknowledge.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BellOff, BellRing, Check, ChevronDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Download,
  Filter, Loader2, MoreHorizontal, RefreshCw, Search, ShieldAlert, Siren, SlidersHorizontal, Timer, UserCheck,
  UserPlus, X, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import {
  AssignDialog, ENTITY_KIND_LABEL, EntityCell, NotifyCell, ResolveDialog, SeverityBadge, SnoozeDialog, StatusChip,
  cleanAlertMessage, entityOf, focusRing, isOpen, useAlertActions, useAssignees, type SnoozeChoice,
} from '@/components/alerts/AlertBits'
import { AlertInsights } from '@/components/alerts/AlertInsights'
import { DataConfidence, SavedViewsMenu, SummaryStrip } from '@/components/alerts/AlertCenterBits'
import { SilencesPanel } from '@/components/alerts/SilencesPanel'
import {
  ageOf, fmtAgo, fmtAge, fmtAlertDate, fmtDur, parseTs, spanSeconds, useApplianceNow,
} from '@/components/alerts/format'
import type {
  AlertListResponse, AlertRow, AlertSummary, BulkAction, BulkResponse, EntityKind,
} from '@/components/alerts/types'

/* ── URL model ────────────────────────────────────────────────────────── */

type PageView = 'active' | 'acknowledged' | 'suppressed' | 'pending' | 'resolved' | 'open' | 'silences'

const TABS: Array<{ key: PageView; label: string }> = [
  { key: 'active', label: 'Active' },
  { key: 'acknowledged', label: 'Acknowledged' },
  { key: 'suppressed', label: 'Suppressed' },
  { key: 'pending', label: 'Pending' },
  { key: 'resolved', label: 'Resolved' },
  { key: 'open', label: 'All open' },
  { key: 'silences', label: 'Silences' },
]
const VIEW_KEYS = TABS.map((t) => t.key) as string[]
const SEVERITIES = ['critical', 'warning', 'info'] as const
const KINDS: EntityKind[] = ['device', 'server', 'service_check', 'sensor', 'apm', 'system']
const SORTS = [
  { key: 'newest', label: 'Newest first' },
  { key: 'oldest', label: 'Oldest first' },
  { key: 'severity', label: 'Severity, then age' },
  { key: 'closed', label: 'Recently closed' },
] as const
const SIZES = [25, 50, 100, 200]
const DEFAULT_SIZE = 50
const RANGES = [
  { hours: 1, label: '1 h' },
  { hours: 24, label: '24 h' },
  { hours: 168, label: '7 d' },
  { hours: 720, label: '30 d' },
  { hours: 2160, label: '90 d' },
  { hours: 8760, label: '1 y' },
]
const BASES = [
  { key: 'resolved', label: 'Closed', verb: 'Closed' },
  { key: 'triggered', label: 'Opened', verb: 'Opened' },
  { key: 'acknowledged', label: 'Acknowledged', verb: 'Acknowledged' },
] as const
const AGES = [
  { key: 'gt1h', label: '> 1 h' },
  { key: 'gt24h', label: '> 24 h' },
  { key: 'gt7d', label: '> 7 days' },
] as const
const POPULATIONS = [
  { key: 'incidents', label: 'Incidents' },
  { key: 'all', label: 'Include notices' },
  { key: 'notices', label: 'Notices only' },
] as const
const RULE_STATES = [
  { key: 'disabled', label: 'Rule disabled' },
  { key: 'missing', label: 'Rule deleted' },
] as const
const DELIVERY_STATES = [
  { key: 'failed', label: 'Delivery failed' },
  { key: 'none', label: 'No delivery recorded' },
] as const
const ID_KEYS = ['device_id', 'server_id', 'service_check_id', 'sensor_id', 'rule_id'] as const
/** Keys "Clear all" removes (tab, sort and page size are kept). */
const FILTER_KEYS = [...ID_KEYS, 'severity', 'entity_kind', 'assignee', 'population', 'hours', 'from', 'to', 'basis', 'age', 'rule_state', 'delivery', 'q', 'page']
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const LEGACY_RANGE: Record<string, number> = { '24h': 24, '7d': 168, '30d': 720 }

const defaultSort = (view: string) => (view === 'resolved' ? 'closed' : 'newest')
const defaultBasis = (view: string) => (view === 'resolved' ? 'resolved' : 'triggered')
const validIso = (v: string | null) => v != null && parseTs(v) != null

/** Canonical form of the query string. Unknown keys pass through untouched. */
function normalize(src: URLSearchParams): URLSearchParams {
  const p = new URLSearchParams(src)
  const drop = (k: string) => p.delete(k)
  // Legacy links: ?status=… and ?range=…
  const legacyStatus = p.get('status')
  if (legacyStatus != null) {
    if (!p.get('view') && VIEW_KEYS.includes(legacyStatus)) p.set('view', legacyStatus)
    drop('status')
  }
  const legacyRange = p.get('range')
  if (legacyRange != null) {
    if (!p.get('hours') && LEGACY_RANGE[legacyRange]) p.set('hours', String(LEGACY_RANGE[legacyRange]))
    drop('range')
  }
  const view = p.get('view')
  if (view != null && (!VIEW_KEYS.includes(view) || view === 'active')) drop('view')
  const v = p.get('view') || 'active'

  const sev = p.get('severity')
  if (sev != null) {
    const parts = SEVERITIES.filter((s) => sev.split(',').map((x) => x.trim().toLowerCase()).includes(s))
    if (parts.length === 0 || parts.length === SEVERITIES.length) drop('severity')
    else p.set('severity', parts.join(','))
  }
  for (const k of ID_KEYS) {
    const val = p.get(k)
    if (val != null && !ID_RE.test(val)) drop(k)
  }
  const kind = p.get('entity_kind')
  if (kind != null && !KINDS.includes(kind as EntityKind)) drop('entity_kind')
  const assignee = p.get('assignee')
  if (assignee != null && assignee !== 'me' && assignee !== 'unassigned' && !ID_RE.test(assignee)) drop('assignee')
  const pop = p.get('population')
  if (pop != null && (pop === 'incidents' || !POPULATIONS.some((x) => x.key === pop))) drop('population')

  const hours = p.get('hours')
  if (hours != null) {
    const h = Number(hours)
    if (!Number.isInteger(h) || h < 1 || h > 8760) drop('hours')
  }
  if (p.get('from') != null && !validIso(p.get('from'))) drop('from')
  if (p.get('to') != null && !validIso(p.get('to'))) drop('to')
  if (p.get('from') || p.get('to')) drop('hours')
  const basis = p.get('basis')
  if (basis != null && (!BASES.some((b) => b.key === basis) || basis === defaultBasis(v))) drop('basis')
  const age = p.get('age')
  if (age != null && !AGES.some((a) => a.key === age)) drop('age')
  const rs = p.get('rule_state')
  if (rs != null && !RULE_STATES.some((r) => r.key === rs)) drop('rule_state')
  const dv = p.get('delivery')
  if (dv != null && !DELIVERY_STATES.some((d) => d.key === dv)) drop('delivery')
  const q = p.get('q')
  if (q != null) {
    const t = q.trim().slice(0, 200)
    if (!t) drop('q')
    else if (t !== q) p.set('q', t)
  }
  const sort = p.get('sort')
  if (sort != null && (!SORTS.some((s) => s.key === sort) || sort === defaultSort(v))) drop('sort')
  const page = p.get('page')
  if (page != null) {
    const n = Number(page)
    if (!Number.isInteger(n) || n <= 1) drop('page')
  }
  const size = p.get('size')
  if (size != null && (!SIZES.includes(Number(size)) || Number(size) === DEFAULT_SIZE)) drop('size')
  return p
}

type UrlState = ReturnType<typeof readState>

function readState(p: URLSearchParams) {
  const view = (p.get('view') || 'active') as PageView
  const severity = p.get('severity')?.split(',').filter(Boolean) ?? []
  const hoursParam = p.get('hours') ? Number(p.get('hours')) : null
  const from = p.get('from')
  const to = p.get('to')
  const custom = !!(from || to)
  // Resolved defaults to "closed in the last 24 h" (rolling, appliance clock).
  const hours = custom ? null : hoursParam ?? (view === 'resolved' ? 24 : null)
  return {
    view,
    severity,
    ids: Object.fromEntries(ID_KEYS.map((k) => [k, p.get(k)])) as Record<(typeof ID_KEYS)[number], string | null>,
    entityKind: p.get('entity_kind') as EntityKind | null,
    assignee: p.get('assignee'),
    population: p.get('population') || 'incidents',
    hours,
    hoursExplicit: hoursParam != null,
    from,
    to,
    custom,
    basis: p.get('basis') || defaultBasis(view),
    age: p.get('age'),
    ruleState: p.get('rule_state'),
    delivery: p.get('delivery'),
    q: p.get('q') || '',
    sort: p.get('sort') || defaultSort(view),
    page: Math.max(1, Number(p.get('page') || 1)),
    size: Number(p.get('size') || DEFAULT_SIZE),
  }
}

/** Filters shared by list, summary, trend and export. */
function filterParams(s: UrlState): URLSearchParams {
  const p = new URLSearchParams()
  if (s.severity.length) p.set('severity', s.severity.join(','))
  for (const k of ID_KEYS) if (s.ids[k]) p.set(k, s.ids[k]!)
  if (s.entityKind) p.set('entity_kind', s.entityKind)
  if (s.assignee) p.set('assignee', s.assignee)
  if (s.population !== 'incidents') p.set('population', s.population)
  if (s.age) p.set('age', s.age)
  if (s.ruleState) p.set('rule_state', s.ruleState)
  if (s.delivery) p.set('delivery', s.delivery)
  if (s.q) p.set('search', s.q)
  return p
}

/** Time window for the list: rolling `hours` or explicit custom bounds. */
function windowParams(s: UrlState, p: URLSearchParams) {
  if (s.custom) {
    if (s.from) p.set('from', s.from)
    if (s.to) p.set('to', s.to)
    p.set('basis', s.basis)
  } else if (s.hours != null) {
    p.set('hours', String(s.hours))
    p.set('basis', s.basis)
  }
}

function hoursLabel(h: number): string {
  if (h % 8760 === 0) return h === 8760 ? '12 months' : `${h / 8760} years`
  if (h % 24 === 0) return h === 24 ? '24 h' : `${h / 24} days`
  return h === 1 ? 'hour' : `${h} h`
}

function windowLabel(s: UrlState): string | null {
  const verb = BASES.find((b) => b.key === s.basis)?.verb ?? 'Opened'
  if (s.custom) return `${verb} ${s.from ? `from ${fmtAlertDate(s.from)}` : ''}${s.to ? ` to ${fmtAlertDate(s.to)}` : ''}`
  if (s.hours != null) return `${verb} in the last ${hoursLabel(s.hours)}`
  return null
}

function toLocalInput(iso: string | null): string {
  const ms = parseTs(iso)
  if (ms == null) return ''
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/* ── Page ─────────────────────────────────────────────────────────────── */

type RowDialog = { kind: 'resolve' | 'snooze' | 'assign'; alert: AlertRow } | null
type BulkDialog = 'resolve' | 'snooze' | 'assign' | null
type BulkOutcome = { action: BulkAction; res: BulkResponse; labels: Record<string, string> } | null

export function AlertsPage() {
  const qc = useQueryClient()
  const can = useCan()
  const me = useAuth((s) => s.user)
  const canTriage = can('alerts.acknowledge')
  const [params, setParams] = useSearchParams()

  const normalized = useMemo(() => normalize(params), [params])
  const normalizedStr = normalized.toString()
  useEffect(() => {
    if (normalizedStr !== params.toString()) setParams(new URLSearchParams(normalizedStr), { replace: true })
  }, [normalizedStr, params, setParams])
  const s = useMemo(() => readState(normalized), [normalized])

  /** Patch URL params (unrelated keys preserved). Filter changes reset paging. */
  const patch = (changes: Record<string, string | null>, opts: { replace?: boolean; keepPage?: boolean } = {}) => {
    const next = new URLSearchParams(normalized)
    for (const [k, v] of Object.entries(changes)) {
      if (v == null || v === '') next.delete(k)
      else next.set(k, v)
    }
    if (!opts.keepPage && !('page' in changes)) next.delete('page')
    setParams(normalize(next), { replace: !!opts.replace })
  }

  /* Search: local draft, resynced when the URL changes (Back/Forward, links). */
  const [draft, setDraft] = useState(s.q)
  const echo = useRef<string | null>(null)
  useEffect(() => {
    if (echo.current !== null && echo.current === s.q) { echo.current = null; return }
    echo.current = null
    setDraft(s.q)
  }, [s.q])
  useEffect(() => {
    const t = draft.trim()
    if (t === s.q) return
    const id = window.setTimeout(() => {
      echo.current = t
      patch({ q: t || null }, { replace: true })
    }, 300)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  /* Queries */
  const baseFilters = useMemo(() => filterParams(s), [s])
  // Paging snapshot (A01): page 1 is live; later pages are pinned to the
  // appliance time page 1 was read at, so new arrivals cannot shift rows
  // between pages. The API reports how many newer rows exist.
  const anchorKey = useMemo(() => {
    const p = new URLSearchParams(baseFilters)
    p.set('view', s.view)
    windowParams(s, p)
    p.set('sort', s.sort)
    return p.toString()
  }, [baseFilters, s])
  const [anchor, setAnchor] = useState<{ key: string; asOf: string } | null>(null)
  const pinnedAsOf = s.page > 1 && anchor?.key === anchorKey ? anchor.asOf : null
  const listQs = useMemo(() => {
    const p = new URLSearchParams(anchorKey)
    if (pinnedAsOf) p.set('as_of', pinnedAsOf)
    p.set('skip', String((s.page - 1) * s.size))
    p.set('limit', String(s.size))
    return p.toString()
  }, [anchorKey, pinnedAsOf, s.page, s.size])
  const summaryQs = useMemo(() => {
    const p = new URLSearchParams(baseFilters)
    if (s.custom) {
      if (s.from) p.set('from', s.from)
      if (s.to) p.set('to', s.to)
    } else {
      p.set('window_hours', String(s.view === 'resolved' && s.hours ? s.hours : 24))
    }
    return p.toString()
  }, [baseFilters, s])
  const exportQs = useMemo(() => {
    const p = new URLSearchParams(baseFilters)
    p.set('view', s.view)
    windowParams(s, p)
    p.set('sort', s.sort)
    p.set('format', 'csv')
    return p
  }, [baseFilters, s])

  const isList = s.view !== 'silences'
  const list = useQuery<AlertListResponse>({
    queryKey: ['alerts', 'list', listQs],
    queryFn: async () => {
      const r = (await api.get(`/alerts?${listQs}`)).data
      if (Array.isArray(r)) {
        return { data: r, meta: { total: r.length, skip: 0, limit: r.length, sort: 'newest', view: null, server_time: new Date().toISOString() } } as AlertListResponse
      }
      return r
    },
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
    enabled: isList,
  })
  const summary = useQuery<AlertSummary>({
    queryKey: ['alerts', 'summary', summaryQs],
    queryFn: async () => (await api.get(`/alerts/summary?${summaryQs}`)).data,
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  })
  const assigneeIsUser = !!s.assignee && s.assignee !== 'me' && s.assignee !== 'unassigned'
  const assignees = useAssignees(assigneeIsUser)

  useEffect(() => {
    if (s.page === 1 && list.data && !list.isPlaceholderData && list.data.meta.server_time) {
      setAnchor({ key: anchorKey, asOf: list.data.meta.server_time })
    }
  }, [s.page, list.data, list.isPlaceholderData, anchorKey])
  const newer = list.data?.meta.newer ?? null

  const rows = list.data?.data ?? []
  const total = list.data?.meta.total ?? 0
  const serverTime = list.data?.meta.server_time ?? summary.data?.server_time ?? null
  const now = useApplianceNow(serverTime)
  const lastPage = Math.max(1, Math.ceil(total / s.size))

  // A page past the end (e.g. after triage shrank the backlog) snaps back.
  useEffect(() => {
    if (list.data && !list.isPlaceholderData && s.page > lastPage && total > 0) patch({ page: String(lastPage) }, { replace: true, keepPage: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.data, list.isPlaceholderData, lastPage, s.page, total])

  /* Selection + bulk */
  const [selected, setSelected] = useState<Set<string>>(new Set())
  useEffect(() => { setSelected(new Set()) }, [listQs])
  const [bulkDialog, setBulkDialog] = useState<BulkDialog>(null)
  const [bulkOutcome, setBulkOutcome] = useState<BulkOutcome>(null)
  const bulk = useMutation({
    mutationFn: async (body: { action: BulkAction; reason?: string; minutes?: number | null; user_id?: string | null }) =>
      (await api.post<BulkResponse>('/alerts/bulk', { ids: [...selected], ...body })).data,
    onSuccess: (res, body) => {
      const labels: Record<string, string> = {}
      for (const r of rows) labels[r.id] = cleanAlertMessage(r.message)
      setBulkOutcome({ action: body.action, res, labels })
      setSelected(new Set())
      setBulkDialog(null)
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e) => toast.error('Bulk action failed', apiErrorMessage(e)),
  })

  /* Row triage */
  const actions = useAlertActions()
  const [rowDialog, setRowDialog] = useState<RowDialog>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  /* Filters panel + insights */
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [insightsOpen, setInsightsOpen] = useState(() => {
    try { return !window.matchMedia('(max-width: 767px)').matches } catch { return true }
  })
  const [exporting, setExporting] = useState(false)

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ['alerts'] })
  }

  const exportCsv = async () => {
    setExporting(true)
    try {
      const r = await api.get('/alerts/export', { params: exportQs, responseType: 'blob', timeout: 120_000 })
      const cd = String(r.headers?.['content-disposition'] ?? '')
      const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i)
      const name = m ? decodeURIComponent(m[1]) : `alerts-${s.view}-${new Date(now).toISOString().slice(0, 10)}.csv`
      const url = URL.createObjectURL(r.data as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast.success('Export ready', `${total.toLocaleString()} matching alert${total === 1 ? '' : 's'} (max 50,000 rows).`)
    } catch (e) {
      toast.error('Export failed', apiErrorMessage(e))
    } finally {
      setExporting(false)
    }
  }

  /* Labels */
  const nameFor = (key: (typeof ID_KEYS)[number], id: string): string => {
    const r = rows.find((x) => x[key] === id)
    switch (key) {
      case 'device_id': return `Device: ${r?.device_hostname || r?.device_ip || id.slice(0, 8)}`
      case 'server_id': return `Server: ${r?.server_hostname || id.slice(0, 8)}`
      case 'service_check_id': return `Service check: ${r?.service_check_name || id.slice(0, 8)}`
      case 'sensor_id': return `Sensor: ${r?.sensor_name || id.slice(0, 8)}`
      default: return `Rule: ${r?.rule_name || id.slice(0, 8)}`
    }
  }
  const chips: Array<{ key: string; label: string; remove: Record<string, null> }> = []
  if (s.severity.length) chips.push({ key: 'severity', label: `Severity: ${s.severity.join(', ')}`, remove: { severity: null } })
  for (const k of ID_KEYS) if (s.ids[k]) chips.push({ key: k, label: nameFor(k, s.ids[k]!), remove: { [k]: null } })
  if (s.entityKind) chips.push({ key: 'entity_kind', label: `Entity: ${ENTITY_KIND_LABEL[s.entityKind]}`, remove: { entity_kind: null } })
  if (s.assignee) {
    const who = s.assignee === 'me' ? 'me' : s.assignee === 'unassigned' ? 'nobody (unassigned)' : assignees.data?.find((u) => u.id === s.assignee)?.name ?? s.assignee.slice(0, 8)
    chips.push({ key: 'assignee', label: `Owner: ${who}`, remove: { assignee: null } })
  }
  if (s.population !== 'incidents') chips.push({ key: 'population', label: POPULATIONS.find((x) => x.key === s.population)!.label, remove: { population: null } })
  if (s.age) chips.push({ key: 'age', label: `Open ${AGES.find((a) => a.key === s.age)!.label}`, remove: { age: null } })
  if (s.ruleState) chips.push({ key: 'rule_state', label: RULE_STATES.find((r) => r.key === s.ruleState)!.label, remove: { rule_state: null } })
  if (s.delivery) chips.push({ key: 'delivery', label: DELIVERY_STATES.find((d) => d.key === s.delivery)!.label, remove: { delivery: null } })
  if (s.q) chips.push({ key: 'q', label: `Search: “${s.q}”`, remove: { q: null } })
  if (s.view !== 'resolved' && (s.hoursExplicit || s.custom)) {
    chips.push({ key: 'window', label: windowLabel(s) ?? 'Time window', remove: { hours: null, from: null, to: null, basis: null } })
  }
  const clearAll = () => {
    setDraft('')
    patch(Object.fromEntries(FILTER_KEYS.map((k) => [k, null])))
  }

  const scopeLabel = summary.data
    ? summary.data.filtered ? `Filtered: ${summary.data.filters.join(' · ') || 'filters applied'}` : 'Organisation — everything you can see'
    : chips.length ? `Filtered: ${chips.map((c) => c.label).join(' · ')}` : 'Organisation — everything you can see'
  const summaryWindowLabel = s.custom
    ? 'Custom range'
    : `Closed in the last ${hoursLabel(s.view === 'resolved' && s.hours ? s.hours : 24)}`

  const tabCount = (v: PageView): number | null => {
    const o = summary.data?.open
    if (!o) return null
    switch (v) {
      case 'active': return o.active
      case 'acknowledged': return o.acknowledged
      case 'suppressed': return o.suppressed
      case 'pending': return o.pending
      case 'open': return o.total
      case 'resolved': return summary.data?.window.closed ?? null
      default: return null
    }
  }

  const critical = summary.data?.open.by_severity.critical ?? 0
  const firstRow = total === 0 ? 0 : (s.page - 1) * s.size + 1
  const lastRow = Math.min(total, s.page * s.size)
  const pageIds = rows.map((r) => r.id)
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.has(id))
  const someOnPage = pageIds.some((id) => selected.has(id))
  const colCount = canTriage ? 8 : 6
  const wl = windowLabel(s)

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <ShieldAlert className="h-5 w-5 text-warning" aria-hidden />
            Alert Center
          </h1>
          <p className="text-sm text-muted">
            What needs attention, who owns it, how old it is, whether it is still firing, and whether anyone was notified.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button asChild variant="outline" size="sm" className={focusRing}>
            <Link to="/alert-rules"><BellRing className="h-3.5 w-3.5" /> Alert rules</Link>
          </Button>
        </div>
      </div>

      <DataConfidence
        updatedAt={isList ? list.dataUpdatedAt : summary.dataUpdatedAt}
        fetching={list.isFetching || summary.isFetching}
        stale={isList ? list.isError && !!list.data : summary.isError && !!summary.data}
        onRefresh={refreshAll}
        serverTime={serverTime}
      />

      {critical > 0 && (
        <div role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-danger/35 bg-danger/10 px-3 py-2 text-sm text-danger">
          <Siren className="h-4 w-4" aria-hidden />
          <span>
            <strong>{critical.toLocaleString()}</strong> critical incident{critical === 1 ? '' : 's'} open and not suppressed
            {summary.data ? <> — <strong>{summary.data.open.unassigned_critical.toLocaleString()}</strong> unassigned</> : null}
            <span className="text-danger/80"> · scope: {summary.data?.filtered ? 'current filters' : 'organisation'}</span>
          </span>
          <Button
            size="sm" variant="outline" className={cn('ml-auto h-7 border-danger/40 text-danger', focusRing)}
            onClick={() => patch({ view: 'open', severity: 'critical', sort: 'severity' })}
          >
            Triage critical, oldest first
          </Button>
        </div>
      )}

      <SummaryStrip
        summary={summary.data}
        loading={summary.isLoading}
        error={summary.isError}
        onRetry={() => summary.refetch()}
        scopeLabel={scopeLabel}
        windowLabel={summaryWindowLabel}
        now={now}
        onDrill={(p) => patch(p)}
      />

      <Card>
        <CardContent className="space-y-3 p-3 sm:p-4">
          {/* Tabs */}
          <ViewTabs
            view={s.view}
            count={tabCount}
            onSelect={(v) => {
              const keepWindow = v === 'resolved'
              patch({
                view: v === 'active' ? null : v,
                sort: null,
                basis: null,
                hours: keepWindow ? normalized.get('hours') : null,
                from: keepWindow ? normalized.get('from') : null,
                to: keepWindow ? normalized.get('to') : null,
              })
            }}
          />

          <div id="alerts-panel" role="tabpanel" aria-labelledby={`tab-${s.view}`} className="space-y-3">
            {s.view === 'silences' ? (
              <SilencesPanel />
            ) : (
              <>
                {/* Toolbar */}
                <div className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="relative w-full sm:w-72">
                      <label htmlFor="alert-search" className="sr-only">Search alerts</label>
                      <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
                      <input
                        id="alert-search"
                        type="search"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        placeholder="Message, host, IP, service, server, sensor, rule, alert ID…"
                        className={cn('h-9 w-full rounded-md border border-border bg-surface pl-8 pr-3 text-sm', focusRing)}
                      />
                    </div>
                    <Button
                      size="sm" variant={filtersOpen ? 'secondary' : 'outline'} className={focusRing}
                      aria-expanded={filtersOpen} aria-controls="alert-filters"
                      onClick={() => setFiltersOpen((v) => !v)}
                    >
                      <SlidersHorizontal className="h-3.5 w-3.5" /> Filters
                      {chips.length > 0 && <span className="rounded-full bg-primary px-1.5 text-xs text-white">{chips.length}</span>}
                    </Button>
                    <div className="flex items-center gap-1.5">
                      <label htmlFor="alert-sort" className="text-sm text-muted">Sort</label>
                      <Select value={s.sort} onValueChange={(v) => patch({ sort: v })}>
                        <SelectTrigger id="alert-sort" className={cn('h-8 w-44', focusRing)} aria-label="Sort alerts"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {SORTS.map((o) => <SelectItem key={o.key} value={o.key}>{o.label}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <SavedViewsMenu currentQuery={normalizedStr.replace(/(^|&)page=\d+/, '').replace(/^&/, '')} onApply={(q) => setParams(normalize(new URLSearchParams(q)))} />
                    <Button size="sm" variant="outline" className={focusRing} onClick={exportCsv} disabled={exporting || total === 0} aria-label="Export all matching alerts as CSV">
                      {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />} Export CSV
                    </Button>
                  </div>
                </div>

                {/* History window (Resolved) */}
                {s.view === 'resolved' && <RangeControls s={s} patch={patch} />}

                {filtersOpen && <FiltersPanel s={s} patch={patch} />}

                {chips.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5" aria-label="Active filters">
                    <Filter className="h-3.5 w-3.5 text-muted" aria-hidden />
                    {chips.map((c) => (
                      <span key={c.key} className="inline-flex items-center gap-1 rounded-full border border-border bg-surface2 py-0.5 pl-2.5 pr-1 text-sm">
                        {c.label}
                        <button
                          onClick={() => { if (c.key === 'q') setDraft(''); patch(c.remove) }}
                          className={cn('rounded-full p-0.5 text-muted hover:bg-surface3 hover:text-text', focusRing)}
                          aria-label={`Remove filter: ${c.label}`}
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </span>
                    ))}
                    <button onClick={clearAll} className={cn('ml-1 text-sm text-muted underline hover:text-text', focusRing)}>Clear all</button>
                  </div>
                )}

                {/* Bulk bar */}
                {canTriage && selected.size > 0 && (
                  <div className="flex flex-wrap items-center gap-2 rounded-md border border-primary/40 bg-primary/5 px-3 py-2" role="region" aria-label="Bulk actions">
                    <span className="text-sm font-medium">{selected.size} selected</span>
                    <Button size="sm" variant="outline" className={focusRing} disabled={bulk.isPending} onClick={() => bulk.mutate({ action: 'acknowledge' })}>
                      {bulk.isPending && bulk.variables?.action === 'acknowledge' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />} Acknowledge
                    </Button>
                    <Button size="sm" variant="outline" className={focusRing} disabled={bulk.isPending} onClick={() => setBulkDialog('resolve')}>
                      <XCircle className="h-3.5 w-3.5" /> Close…
                    </Button>
                    <Button size="sm" variant="outline" className={focusRing} disabled={bulk.isPending} onClick={() => setBulkDialog('snooze')}>
                      <BellOff className="h-3.5 w-3.5" /> Snooze…
                    </Button>
                    <Button size="sm" variant="outline" className={focusRing} disabled={bulk.isPending} onClick={() => setBulkDialog('assign')}>
                      <UserPlus className="h-3.5 w-3.5" /> Assign…
                    </Button>
                    <button className={cn('ml-auto text-sm text-muted underline hover:text-text', focusRing)} onClick={() => setSelected(new Set())}>Clear selection</button>
                  </div>
                )}
                {bulkOutcome && <BulkResult outcome={bulkOutcome} onDismiss={() => setBulkOutcome(null)} />}

                {/* Table */}
                {list.isError && !list.data ? (
                  <div className="flex flex-col items-start gap-2 rounded-md border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
                    <span>Failed to load alerts: {apiErrorMessage(list.error)}. The backlog is unknown — this is not an empty list.</span>
                    <Button size="sm" variant="outline" className={focusRing} onClick={() => list.refetch()}>
                      <RefreshCw className="h-3.5 w-3.5" /> Retry
                    </Button>
                  </div>
                ) : (
                  <>
                    <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted">
                      <span aria-live="polite">
                        {list.isLoading ? 'Loading…' : total === 0 ? 'No matching alerts' : <>Showing <b className="text-text">{firstRow.toLocaleString()}–{lastRow.toLocaleString()}</b> of <b className="text-text">{total.toLocaleString()}</b></>}
                        {wl && <> · {wl}</>}
                        {list.isPlaceholderData && <Loader2 className="ml-2 inline h-3.5 w-3.5 animate-spin" aria-label="Loading page" />}
                      </span>
                      <span className="text-xs">
                        {SORTS.find((o) => o.key === s.sort)?.label}
                        {pinnedAsOf ? (
                          <>
                            {' · '}pages pinned to {fmtAlertDate(pinnedAsOf)}
                            {newer ? (
                              <>
                                {' · '}
                                <button type="button" className="font-medium text-primary hover:underline" onClick={() => patch({ page: null })}>
                                  {newer.toLocaleString()} newer — back to page 1
                                </button>
                              </>
                            ) : null}
                          </>
                        ) : (
                          <span className="hidden md:inline"> · live — later pages are pinned to this moment</span>
                        )}
                      </span>
                    </div>
                    <div className={cn('overflow-hidden rounded-md border border-border', list.isPlaceholderData && 'opacity-70')}>
                      <Table className="min-w-[940px] table-fixed [&_td]:px-2.5 [&_th]:px-2.5">
                        <THead className="bg-surface2/50">
                          <Tr>
                            {canTriage && (
                              <Th className="w-9">
                                <HeaderCheckbox
                                  checked={allOnPage}
                                  indeterminate={!allOnPage && someOnPage}
                                  onChange={(v) => {
                                    const next = new Set(selected)
                                    for (const id of pageIds) { if (v) next.add(id); else next.delete(id) }
                                    setSelected(next)
                                  }}
                                />
                              </Th>
                            )}
                            <Th>Alert</Th>
                            <Th className="w-[150px] min-[1440px]:w-[176px]">Entity</Th>
                            <Th className="w-[122px] min-[1440px]:w-[136px]">Status</Th>
                            <Th className="w-[124px]">Owner</Th>
                            <Th className="w-[104px] min-[1440px]:w-[132px]">{s.view === 'resolved' ? 'Closed' : 'Age'}</Th>
                            <Th className="w-[116px]" title="Configured routing and recorded delivery outcome">Notify</Th>
                            {canTriage && <Th className="w-[108px] text-right"><span className="sr-only">Actions</span></Th>}
                          </Tr>
                        </THead>
                        <TBody>
                          {list.isLoading ? (
                            Array.from({ length: 6 }).map((_, i) => (
                              <Tr key={i}><Td colSpan={colCount}><Skeleton className="h-8 w-full" /></Td></Tr>
                            ))
                          ) : rows.length === 0 ? (
                            <Tr>
                              <Td colSpan={colCount} className="py-12 text-center text-sm text-muted">
                                {emptyText(s.view, chips.length > 0)}
                              </Td>
                            </Tr>
                          ) : rows.map((a) => (
                            <AlertTableRow
                              key={a.id}
                              alert={a}
                              now={now}
                              canTriage={canTriage}
                              selected={selected.has(a.id)}
                              onSelect={(v) => {
                                const next = new Set(selected)
                                if (v) next.add(a.id); else next.delete(a.id)
                                setSelected(next)
                              }}
                              expanded={expanded.has(a.id)}
                              onToggleExpand={() => {
                                const next = new Set(expanded)
                                if (next.has(a.id)) next.delete(a.id); else next.add(a.id)
                                setExpanded(next)
                              }}
                              resolvedView={s.view === 'resolved'}
                              onAck={() => actions.acknowledge.mutate({ id: a.id })}
                              ackPending={actions.acknowledge.isPending && actions.acknowledge.variables?.id === a.id}
                              onUnsnooze={() => actions.unsnooze.mutate({ id: a.id })}
                              onAssignMe={me ? () => actions.assign.mutate({ id: a.id, user_id: me.id }) : undefined}
                              onDialog={(kind) => setRowDialog({ kind, alert: a })}
                            />
                          ))}
                        </TBody>
                      </Table>
                    </div>
                    <Pager
                      page={s.page}
                      lastPage={lastPage}
                      size={s.size}
                      total={total}
                      onPage={(p) => patch({ page: p === 1 ? null : String(p) }, { keepPage: true })}
                      onSize={(n) => patch({ size: n === DEFAULT_SIZE ? null : String(n) })}
                    />
                  </>
                )}
              </>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Insights */}
      <section className="rounded-lg border border-border bg-surface">
        <button
          className={cn('flex w-full items-center justify-between gap-2 px-4 py-3 text-left', focusRing)}
          aria-expanded={insightsOpen}
          aria-controls="alert-insights"
          onClick={() => setInsightsOpen((v) => !v)}
        >
          <span>
            <span className="block text-base font-semibold">Insights</span>
            <span className="block text-sm text-muted">Opened vs closed trend, aging, top sources and backlog health</span>
          </span>
          <ChevronDown className={cn('h-4 w-4 shrink-0 text-muted transition-transform', insightsOpen && 'rotate-180')} aria-hidden />
        </button>
        {insightsOpen && (
          <div id="alert-insights" className="border-t border-border p-4">
            <AlertInsights
              filterQs={baseFilters.toString()}
              summary={summary.data}
              summaryError={summary.isError}
              onFilter={(p) => patch(p)}
              scopeLabel={scopeLabel}
            />
          </div>
        )}
      </section>

      {/* Row dialogs */}
      {rowDialog?.kind === 'resolve' && (
        <ResolveDialog
          open
          onOpenChange={(v) => { if (!v) setRowDialog(null) }}
          alertLabel={cleanAlertMessage(rowDialog.alert.message)}
          pending={actions.resolve.isPending}
          onConfirm={(reason) => actions.resolve.mutate({ id: rowDialog.alert.id, reason }, { onSettled: () => setRowDialog(null) })}
        />
      )}
      {rowDialog?.kind === 'snooze' && (
        <SnoozeDialog
          open
          onOpenChange={(v) => { if (!v) setRowDialog(null) }}
          alertId={rowDialog.alert.id}
          alertLabel={cleanAlertMessage(rowDialog.alert.message)}
        />
      )}
      {rowDialog?.kind === 'assign' && (
        <AssignDialog
          open
          onOpenChange={(v) => { if (!v) setRowDialog(null) }}
          current={rowDialog.alert.assigned_to}
          pending={actions.assign.isPending}
          onAssign={(uid) => actions.assign.mutate({ id: rowDialog.alert.id, user_id: uid }, { onSettled: () => setRowDialog(null) })}
        />
      )}

      {/* Bulk dialogs */}
      <ResolveDialog
        open={bulkDialog === 'resolve'}
        onOpenChange={(v) => { if (!v) setBulkDialog(null) }}
        count={selected.size}
        pending={bulk.isPending}
        onConfirm={(reason) => bulk.mutate({ action: 'resolve', ...(reason ? { reason } : {}) })}
      />
      <SnoozeDialog
        open={bulkDialog === 'snooze'}
        onOpenChange={(v) => { if (!v) setBulkDialog(null) }}
        count={selected.size}
        onSubmit={(c: SnoozeChoice) => bulk.mutateAsync({ action: 'snooze', minutes: c.minutes, ...(c.reason ? { reason: c.reason } : {}) })}
      />
      <AssignDialog
        open={bulkDialog === 'assign'}
        onOpenChange={(v) => { if (!v) setBulkDialog(null) }}
        count={selected.size}
        pending={bulk.isPending}
        onAssign={(uid) => bulk.mutate({ action: 'assign', user_id: uid })}
      />
    </div>
  )
}

function emptyText(view: PageView, filtered: boolean): string {
  const suffix = filtered ? ' match the current filters.' : '.'
  switch (view) {
    case 'active': return `No active alerts${suffix}`
    case 'acknowledged': return `No acknowledged alerts${suffix}`
    case 'suppressed': return `No suppressed conditions with open alerts${suffix}`
    case 'pending': return `No alerts are being held for their hold time${suffix}`
    case 'resolved': return `No incidents closed in this window${suffix}`
    default: return `No open alerts${suffix}`
  }
}

/* ── Tabs ─────────────────────────────────────────────────────────────── */

function ViewTabs({ view, count, onSelect }: { view: PageView; count: (v: PageView) => number | null; onSelect: (v: PageView) => void }) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const onKey = (e: React.KeyboardEvent, idx: number) => {
    let next = -1
    if (e.key === 'ArrowRight') next = (idx + 1) % TABS.length
    else if (e.key === 'ArrowLeft') next = (idx - 1 + TABS.length) % TABS.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = TABS.length - 1
    if (next < 0) return
    e.preventDefault()
    const key = TABS[next].key
    refs.current[key]?.focus()
    onSelect(key)
  }
  return (
    <div className="-mx-1 overflow-x-auto px-1">
      <div role="tablist" aria-label="Alert views" className="inline-flex min-w-max gap-0.5 rounded-md bg-surface2 p-0.5">
        {TABS.map((t, i) => {
          const n = count(t.key)
          const active = view === t.key
          return (
            <button
              key={t.key}
              id={`tab-${t.key}`}
              ref={(el) => { refs.current[t.key] = el }}
              role="tab"
              aria-selected={active}
              aria-controls="alerts-panel"
              tabIndex={active ? 0 : -1}
              onKeyDown={(e) => onKey(e, i)}
              onClick={() => onSelect(t.key)}
              className={cn(
                'inline-flex items-center gap-1.5 whitespace-nowrap rounded px-3 py-1.5 text-sm font-medium transition-colors',
                focusRing,
                active ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
              )}
              title={t.key === 'resolved' ? 'Incidents closed in the selected window (notices excluded)' : undefined}
            >
              {t.label}
              {n != null && (
                <span className={cn(
                  'rounded-full px-1.5 text-xs tabular-nums',
                  t.key === 'active' && n > 0 ? 'bg-danger/15 text-danger' : 'bg-surface3/70 text-text2',
                )}>
                  {n.toLocaleString()}
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

/* ── Range (Resolved history) ─────────────────────────────────────────── */

function RangeControls({ s, patch }: { s: UrlState; patch: (c: Record<string, string | null>, o?: { replace?: boolean }) => void }) {
  const [customOpen, setCustomOpen] = useState(s.custom)
  const [from, setFrom] = useState(toLocalInput(s.from))
  const [to, setTo] = useState(toLocalInput(s.to))
  useEffect(() => { setFrom(toLocalInput(s.from)); setTo(toLocalInput(s.to)); if (s.custom) setCustomOpen(true) }, [s.from, s.to, s.custom])
  const fromMs = from ? new Date(from).getTime() : null
  const toMs = to ? new Date(to).getTime() : null
  const valid = (fromMs != null || toMs != null) && !(fromMs != null && toMs != null && fromMs >= toMs)
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-surface2/30 p-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5" role="group" aria-label="Timestamp basis">
          {BASES.map((b) => (
            <button
              key={b.key}
              aria-pressed={s.basis === b.key}
              onClick={() => patch({ basis: b.key })}
              className={cn('rounded px-2.5 py-1 text-sm', focusRing, s.basis === b.key ? 'bg-surface font-medium text-text shadow-sm' : 'text-muted hover:text-text')}
            >
              {b.label}
            </button>
          ))}
        </div>
        <span className="text-sm text-muted">in the last</span>
        <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5" role="group" aria-label="Time window">
          {RANGES.map((r) => {
            const on = !s.custom && s.hours === r.hours
            return (
              <button
                key={r.hours}
                aria-pressed={on}
                onClick={() => { setCustomOpen(false); patch({ hours: String(r.hours), from: null, to: null }) }}
                className={cn('rounded px-2.5 py-1 text-sm', focusRing, on ? 'bg-surface font-medium text-text shadow-sm' : 'text-muted hover:text-text')}
              >
                {r.label}
              </button>
            )
          })}
          <button
            aria-pressed={s.custom}
            aria-expanded={customOpen}
            onClick={() => setCustomOpen((v) => !v)}
            className={cn('rounded px-2.5 py-1 text-sm', focusRing, s.custom ? 'bg-surface font-medium text-text shadow-sm' : 'text-muted hover:text-text')}
          >
            Custom…
          </button>
        </div>
        <span className="text-sm font-medium text-text2">{windowLabel(s)}</span>
      </div>
      <p className="text-xs text-muted">Rolling windows are evaluated on the appliance clock each refresh; custom bounds are fixed.</p>
      {customOpen && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (!valid) return
            patch({ from: fromMs != null ? new Date(fromMs).toISOString() : null, to: toMs != null ? new Date(toMs).toISOString() : null, hours: null })
          }}
        >
          <label className="text-sm">
            <span className="block text-muted">From (your local time)</span>
            <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} className={cn('h-9 rounded-md border border-border bg-surface px-2 text-sm', focusRing)} />
          </label>
          <label className="text-sm">
            <span className="block text-muted">To</span>
            <input type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} className={cn('h-9 rounded-md border border-border bg-surface px-2 text-sm', focusRing)} />
          </label>
          <Button type="submit" size="sm" disabled={!valid}>Apply</Button>
          {!valid && (from || to) && <span className="text-sm text-danger">“From” must be before “To”.</span>}
        </form>
      )}
    </div>
  )
}

/* ── Filters panel ────────────────────────────────────────────────────── */

function Seg<T extends string>({
  label, options, value, onChange, multi,
}: {
  label: string
  options: ReadonlyArray<{ key: T; label: string }>
  value: T | T[] | null
  onChange: (v: T | null) => void
  multi?: boolean
}) {
  const isOn = (k: T) => (Array.isArray(value) ? value.includes(k) : value === k)
  return (
    <div className="min-w-0">
      <div className="mb-1 text-sm font-medium text-text2" id={`seg-${label}`}>{label}</div>
      <div className="flex flex-wrap gap-1" role="group" aria-labelledby={`seg-${label}`}>
        {!multi && (
          <button
            aria-pressed={value == null}
            onClick={() => onChange(null)}
            className={cn('rounded-md border px-2.5 py-1 text-sm', focusRing, value == null ? 'border-primary bg-primary/10 font-medium text-primary' : 'border-border hover:bg-surface2')}
          >
            Any
          </button>
        )}
        {options.map((o) => (
          <button
            key={o.key}
            aria-pressed={isOn(o.key)}
            onClick={() => onChange(o.key)}
            className={cn('rounded-md border px-2.5 py-1 text-sm', focusRing, isOn(o.key) ? 'border-primary bg-primary/10 font-medium text-primary' : 'border-border hover:bg-surface2')}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function FiltersPanel({ s, patch }: { s: UrlState; patch: (c: Record<string, string | null>) => void }) {
  const toggleSeverity = (k: string | null) => {
    if (!k) return patch({ severity: null })
    const set = new Set(s.severity)
    if (set.has(k)) set.delete(k); else set.add(k)
    patch({ severity: SEVERITIES.filter((x) => set.has(x)).join(',') || null })
  }
  return (
    <div id="alert-filters" className="grid gap-4 rounded-md border border-border bg-surface2/30 p-3 sm:grid-cols-2 xl:grid-cols-3">
      <Seg
        label="Severity"
        multi
        options={[{ key: 'critical', label: 'Critical' }, { key: 'warning', label: 'Warning' }, { key: 'info', label: 'Info' }]}
        value={s.severity as Array<'critical' | 'warning' | 'info'>}
        onChange={toggleSeverity}
      />
      <Seg
        label="Entity type"
        options={KINDS.map((k) => ({ key: k, label: ENTITY_KIND_LABEL[k] }))}
        value={s.entityKind}
        onChange={(v) => patch({ entity_kind: v })}
      />
      <Seg
        label="Owner"
        options={[{ key: 'me', label: 'Me' }, { key: 'unassigned', label: 'Unassigned' }]}
        value={s.assignee === 'me' || s.assignee === 'unassigned' ? s.assignee : null}
        onChange={(v) => patch({ assignee: v })}
      />
      <Seg
        label="Open for"
        options={AGES}
        value={s.age as (typeof AGES)[number]['key'] | null}
        onChange={(v) => patch({ age: v })}
      />
      <Seg
        label="Rule state"
        options={RULE_STATES}
        value={s.ruleState as (typeof RULE_STATES)[number]['key'] | null}
        onChange={(v) => patch({ rule_state: v })}
      />
      <Seg
        label="Notifications"
        options={DELIVERY_STATES}
        value={s.delivery as (typeof DELIVERY_STATES)[number]['key'] | null}
        onChange={(v) => patch({ delivery: v })}
      />
      {s.view === 'resolved' && (
        <div>
          <Seg
            label="Population"
            multi
            options={POPULATIONS}
            value={s.population as (typeof POPULATIONS)[number]['key']}
            onChange={(v) => patch({ population: v === 'incidents' ? null : v })}
          />
          <p className="mt-1 text-xs text-muted">Notices are recovery, dependency and hold-cancelled rows; they are never counted as incidents.</p>
        </div>
      )}
    </div>
  )
}

/* ── Table row ────────────────────────────────────────────────────────── */

function HeaderCheckbox({ checked, indeterminate, onChange }: { checked: boolean; indeterminate: boolean; onChange: (v: boolean) => void }) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { if (ref.current) ref.current.indeterminate = indeterminate }, [indeterminate])
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
      aria-label="Select all alerts on this page"
      className={cn('h-4 w-4 cursor-pointer', focusRing)}
    />
  )
}

function AlertTableRow({
  alert: a, now, canTriage, selected, onSelect, expanded, onToggleExpand, resolvedView,
  onAck, ackPending, onUnsnooze, onAssignMe, onDialog,
}: {
  alert: AlertRow
  now: number
  canTriage: boolean
  selected: boolean
  onSelect: (v: boolean) => void
  expanded: boolean
  onToggleExpand: () => void
  resolvedView: boolean
  onAck: () => void
  ackPending: boolean
  onUnsnooze: () => void
  onAssignMe?: () => void
  onDialog: (k: 'resolve' | 'snooze' | 'assign') => void
}) {
  const msg = cleanAlertMessage(a.message)
  const entity = entityOf(a)
  const open = isOpen(a)
  const suppressed = !!a.suppression || a.snoozed
  const ownerless = open && !a.assigned_to
  const age = ageOf(a.triggered_at, now)
  const msgId = `msg-${a.id}`
  return (
    <Tr data-state={selected ? 'selected' : undefined}>
      {canTriage && (
        <Td>
          <input
            type="checkbox"
            checked={selected}
            onChange={(e) => onSelect(e.target.checked)}
            aria-label={`Select alert: ${msg}`}
            className={cn('h-4 w-4 cursor-pointer', focusRing)}
          />
        </Td>
      )}
      <Td>
        <div className="flex items-start gap-1.5">
          <button
            onClick={onToggleExpand}
            aria-expanded={expanded}
            aria-controls={msgId}
            aria-label={`${expanded ? 'Collapse' : 'Show'} full message: ${msg}`}
            className={cn('mt-0.5 shrink-0 rounded p-0.5 text-muted hover:bg-surface2 hover:text-text', focusRing)}
          >
            <ChevronRight className={cn('h-4 w-4 transition-transform', expanded && 'rotate-90')} />
          </button>
          <div className="min-w-0" id={msgId}>
            <Link
              to={`/alerts/${a.id}`}
              title={msg}
              className={cn('block text-sm font-medium hover:text-primary hover:underline', expanded ? 'whitespace-pre-wrap break-words' : 'truncate', focusRing)}
            >
              {msg}
            </Link>
            <div className="mt-0.5 flex min-w-0 items-center gap-1.5">
              <SeverityBadge severity={a.severity} />
              <span className="truncate text-xs text-muted">{a.rule_name ? `Rule: ${a.rule_name}` : 'No rule'}</span>
            </div>
            {expanded && (
              <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-xs text-text2">
                <dt className="text-muted">Triggered</dt><dd>{fmtAlertDate(a.triggered_at, { seconds: true, year: 'always' })}</dd>
                {a.acknowledged_at && <><dt className="text-muted">Acknowledged</dt><dd>{fmtAlertDate(a.acknowledged_at, { seconds: true, year: 'always' })}{a.acknowledged_by ? ` by ${a.acknowledged_by.name}` : ''}</dd></>}
                {a.resolved_at && <><dt className="text-muted">Closed</dt><dd>{fmtAlertDate(a.resolved_at, { seconds: true, year: 'always' })}{a.closure ? ` · ${a.closure.label}` : ''}{a.closure?.note ? ` — ${a.closure.note}` : ''}</dd></>}
                {suppressed && <><dt className="text-muted">Suppressed</dt><dd>{a.suppression?.forever ? 'Indefinitely' : a.suppression?.until || a.snoozed_until ? `until ${fmtAlertDate(a.suppression?.until ?? a.snoozed_until)}` : 'yes'}{a.suppression?.reason ? ` — ${a.suppression.reason}` : ''}</dd></>}
                <dt className="text-muted">Alert ID</dt><dd className="font-mono">{a.id}</dd>
              </dl>
            )}
          </div>
        </div>
      </Td>
      <Td><EntityCell entity={entity} /></Td>
      <Td><StatusChip alert={a} /></Td>
      <Td className="min-w-[120px]">
        {a.assigned_to ? (
          <span className="inline-flex items-center gap-1 text-sm"><UserCheck className="h-3.5 w-3.5 text-primary" aria-hidden />{a.assigned_to.name}</span>
        ) : (
          <span className={cn('text-sm', ownerless && a.severity === 'critical' ? 'font-medium text-danger' : 'text-muted')}>Unassigned</span>
        )}
        {!a.assigned_to && a.acknowledged_by && <div className="text-xs text-muted">Ack: {a.acknowledged_by.name}</div>}
        {a.ack_overdue && (
          <div className="mt-1">
            <span className="inline-flex items-center gap-1 rounded border border-danger/40 bg-danger/10 px-1.5 py-px text-xs font-medium text-danger" title={a.ack_due_at ? `Acknowledge target was ${fmtAlertDate(a.ack_due_at)}` : undefined}>
              <Timer className="h-3 w-3" aria-hidden /> Ack overdue
            </span>
          </div>
        )}
      </Td>
      <Td className="whitespace-nowrap">
        {resolvedView || a.status === 'resolved' ? (
          <div title={fmtAlertDate(a.resolved_at, { seconds: true, year: 'always' })}>
            <div className="text-sm">{fmtAgo(a.resolved_at, now)}</div>
            <div className="text-xs text-muted">open {fmtDur(spanSeconds(a.triggered_at, a.resolved_at))}</div>
          </div>
        ) : (
          <div title={fmtAlertDate(a.triggered_at, { seconds: true, year: 'always' })}>
            <div className={cn('text-sm tabular-nums', age.future && 'text-warning', (age.seconds ?? 0) > 7 * 86400 && 'font-medium text-warning')}>
              {fmtAge(a.triggered_at, now)}
            </div>
            <div className="hidden text-xs text-muted min-[1440px]:block">{fmtAlertDate(a.triggered_at)}</div>
          </div>
        )}
      </Td>
      <Td><NotifyCell channels={a.channels} delivery={a.delivery} /></Td>
      {canTriage && (
        <Td className="text-right">
          <div className="flex justify-end gap-1">
            {a.status === 'active' && (
              <Button size="sm" variant="outline" className={focusRing} onClick={onAck} disabled={ackPending} aria-label={`Acknowledge alert: ${msg}`}>
                {ackPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />} Ack
              </Button>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm" variant="ghost" className={cn('px-2', focusRing)} aria-label={`More actions for alert: ${msg}`}>
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuItem asChild><Link to={`/alerts/${a.id}`}>Open details</Link></DropdownMenuItem>
                {open && !a.is_notice && (
                  <>
                    <DropdownMenuSeparator />
                    {onAssignMe && <DropdownMenuItem onSelect={onAssignMe}><UserCheck /> Assign to me</DropdownMenuItem>}
                    <DropdownMenuItem onSelect={() => onDialog('assign')}><UserPlus /> Assign…</DropdownMenuItem>
                    {suppressed ? (
                      <DropdownMenuItem onSelect={onUnsnooze}><BellRing /> Unsnooze (notify again)</DropdownMenuItem>
                    ) : (
                      <DropdownMenuItem onSelect={() => onDialog('snooze')}><BellOff /> Snooze notifications…</DropdownMenuItem>
                    )}
                    <DropdownMenuItem onSelect={() => onDialog('resolve')}><XCircle /> Close incident…</DropdownMenuItem>
                  </>
                )}
                {!open && suppressed && (
                  <DropdownMenuItem onSelect={onUnsnooze}><BellRing /> Remove suppression</DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </Td>
      )}
    </Tr>
  )
}

/* ── Pager ────────────────────────────────────────────────────────────── */

function Pager({
  page, lastPage, size, total, onPage, onSize,
}: { page: number; lastPage: number; size: number; total: number; onPage: (p: number) => void; onSize: (n: number) => void }) {
  const btn = (label: string, icon: React.ReactNode, target: number, disabled: boolean) => (
    <Button size="sm" variant="outline" className={cn('h-8 w-8 p-0', focusRing)} onClick={() => onPage(target)} disabled={disabled} aria-label={label}>
      {icon}
    </Button>
  )
  return (
    <nav className="flex flex-wrap items-center justify-between gap-2" aria-label="Pagination">
      <div className="flex items-center gap-1.5 text-sm text-muted">
        <label htmlFor="page-size">Rows per page</label>
        <Select value={String(size)} onValueChange={(v) => onSize(Number(v))}>
          <SelectTrigger id="page-size" className={cn('h-8 w-20', focusRing)}><SelectValue /></SelectTrigger>
          <SelectContent>
            {SIZES.map((n) => <SelectItem key={n} value={String(n)}>{n}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center gap-1">
        {btn('First page', <ChevronsLeft className="h-4 w-4" />, 1, page <= 1)}
        {btn('Previous page', <ChevronLeft className="h-4 w-4" />, page - 1, page <= 1)}
        <span className="px-2 text-sm tabular-nums" aria-current="page">
          Page {page.toLocaleString()} of {lastPage.toLocaleString()}
        </span>
        {btn('Next page', <ChevronRight className="h-4 w-4" />, page + 1, page >= lastPage || total === 0)}
        {btn('Last page', <ChevronsRight className="h-4 w-4" />, lastPage, page >= lastPage || total === 0)}
      </div>
    </nav>
  )
}

/* ── Bulk result ──────────────────────────────────────────────────────── */

const BULK_VERB: Record<BulkAction, string> = {
  acknowledge: 'acknowledged', resolve: 'closed', snooze: 'suppressed', assign: 'assigned',
}

function BulkResult({ outcome, onDismiss }: { outcome: NonNullable<BulkOutcome>; onDismiss: () => void }) {
  const { res, action, labels } = outcome
  const failed = res.results.filter((r) => !r.ok)
  const unchanged = res.results.filter((r) => r.ok && !r.changed).length
  return (
    <div role="status" className={cn('rounded-md border px-3 py-2 text-sm', failed.length ? 'border-warning/40 bg-warning/10' : 'border-border bg-surface2/50')}>
      <div className="flex flex-wrap items-center gap-2">
        <span>
          <b>{res.summary.changed.toLocaleString()}</b> {BULK_VERB[action]}
          {unchanged > 0 && <> · <b>{unchanged.toLocaleString()}</b> already in that state</>}
          {failed.length > 0 && <> · <b className="text-warning">{failed.length.toLocaleString()}</b> skipped or failed</>}
        </span>
        <button onClick={onDismiss} className={cn('ml-auto rounded p-0.5 text-muted hover:text-text', focusRing)} aria-label="Dismiss bulk result"><X className="h-4 w-4" /></button>
      </div>
      {failed.length > 0 && (
        <ul className="mt-1.5 max-h-40 space-y-0.5 overflow-y-auto">
          {failed.map((f) => (
            <li key={f.id} className="flex gap-2 text-sm">
              <Link to={`/alerts/${f.id}`} className={cn('min-w-0 truncate text-primary hover:underline', focusRing)}>{labels[f.id] ?? f.id.slice(0, 8)}</Link>
              <span className="shrink-0 text-muted">— {f.error || 'failed'}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

