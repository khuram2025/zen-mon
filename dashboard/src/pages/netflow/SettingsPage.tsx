/**
 * NetFlow settings hub (FE-09, CR-12, CR-13): managed IP groups, application classification
 * rules, enrichment (reverse DNS, GeoIP) and what flow export cannot see, plus links to the
 * collector-health workspace and the detection settings.
 *
 * Reads go through `nf.get` / `useNfQuery` (envelope, panel states); writes need
 * `netflow.manage` and show field-level validation, conflicts and progress. Every traffic
 * figure on the page states its window and data quality.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Boxes,
  CheckCircle2,
  ChevronDown,
  Database,
  Download,
  EyeOff,
  FileUp,
  FlaskConical,
  Globe2,
  History,
  Info,
  Layers,
  Loader2,
  Lock,
  Network,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  SlidersHorizontal,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { TimeRangePicker } from '@/components/TimeRangePicker'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { PanelMessage, QueryPanel, StaleChip } from '@/components/netflow/QueryPanel'
import { StatusTile, type StatusTone } from '@/components/netflow/StatusTile'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { FilterChips } from '@/components/netflow/FilterChips'
import { nfKeys, nfParams, unwrapEnvelope, useNfQuery } from '@/lib/netflow/api'
import type { FlowFilter } from '@/lib/netflow/filters'
import {
  formatBytes,
  formatBytesExact,
  formatCount,
  formatCountExact,
  formatDateTime,
  formatPercent,
  protocolLabel,
} from '@/lib/netflow/format'
import { parseIpOrCidr } from '@/lib/netflow/ip'
import { buildNetflowLink } from '@/lib/netflow/links'
import { apiErrorMessage, apiErrorRef, httpStatus, panelStatus, type PanelQueryLike } from '@/lib/netflow/status'
import type { NfMeta, NfResult, NfRetention } from '@/lib/netflow/types'
import { useFlowFilter } from '@/lib/netflow/useFlowFilter'
import { displayTimeZone, useNfWindow, windowParams, type NfWindow } from '@/lib/netflow/window'

/* ─── Pure helpers (exported for scripts/test-netflow-settings.mjs) ──────────────────────── */

export const SETTINGS_TABS = ['groups', 'applications', 'enrichment'] as const
export type SettingsTab = (typeof SETTINGS_TABS)[number]

export function normalizeSettingsTab(v: string | null | undefined): SettingsTab {
  return (SETTINGS_TABS as readonly string[]).includes(v ?? '') ? (v as SettingsTab) : 'groups'
}

export interface CidrLine {
  raw: string
  ok: boolean
  error?: string
}

/** One CIDR or address per line (commas and spaces also separate); blank lines are ignored. */
export function parseCidrLines(text: string): { lines: CidrLine[]; valid: string[]; invalid: number } {
  const parts = text
    .split(/[\s,;]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  const lines: CidrLine[] = []
  const valid: string[] = []
  for (const raw of parts) {
    if (raw.includes('%')) {
      lines.push({ raw, ok: false, error: 'Zone ids are not allowed' })
      continue
    }
    const p = parseIpOrCidr(raw)
    if (!p) {
      lines.push({ raw, ok: false, error: 'Not an IP address or CIDR' })
      continue
    }
    lines.push({ raw, ok: true })
    valid.push(raw)
  }
  return { lines, valid, invalid: lines.filter((l) => !l.ok).length }
}

/** Percent 0..100 of `part` in `total`, or null when the denominator is unknown or zero. */
export function shareOf(part: number | null | undefined, total: number | null | undefined): number | null {
  if (typeof part !== 'number' || typeof total !== 'number' || !Number.isFinite(part) || !(total > 0)) return null
  return (100 * part) / total
}

export const GROUP_COLORS: { name: string; hex: string }[] = [
  { name: 'Blue', hex: '#2a78d6' },
  { name: 'Orange', hex: '#eb6834' },
  { name: 'Aqua', hex: '#1baf7a' },
  { name: 'Yellow', hex: '#eda100' },
  { name: 'Magenta', hex: '#e87ba4' },
  { name: 'Green', hex: '#008300' },
  { name: 'Violet', hex: '#4a3aa7' },
  { name: 'Red', hex: '#e34948' },
]

export function isHexColor(v: string): boolean {
  return /^#[0-9a-fA-F]{6}$/.test(v)
}

/** Field errors (`{field: message}`) from a 400 response of the settings API. */
export function responseFieldErrors(err: unknown): Record<string, string> {
  const data = (err as { response?: { data?: unknown } } | null)?.response?.data as Record<string, unknown> | undefined
  const fields = data?.fields
  if (!fields || typeof fields !== 'object') return {}
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(fields as Record<string, unknown>)) {
    if (typeof v === 'string') out[k] = v
  }
  return out
}

export const GROUP_FIELDS = ['name', 'description', 'cidrs', 'color', 'tags', 'parent_id'] as const
export const RULE_FIELDS = ['application', 'service', 'category', 'protocol', 'port_lo', 'port_hi', 'cidrs', 'priority'] as const

/** True when every field error can be shown next to an input of the form (no banner needed). */
export function onlyKnownFields(fields: Record<string, string>, known: readonly string[]): boolean {
  const keys = Object.keys(fields)
  return keys.length > 0 && keys.every((k) => known.includes(k))
}

export interface AppRule {
  id: string
  application: string
  service: string | null
  category: string | null
  protocol: number | null
  protocol_name: string | null
  port_lo: number | null
  port_hi: number | null
  cidrs: string[]
  priority: number
  enabled: boolean
  kind: 'port' | 'protocol' | 'address'
  created_at: string | null
  updated_at: string | null
  created_by: string | null
  updated_by: string | null
}

const PORT_PROTOCOLS = new Set([6, 17, 33, 132, 136])

/** "TCP 8080–8090 · 10.0.0.0/8", "TCP/UDP 443", "ESP (50)", "Addresses 10.1.0.0/16". */
export function describeRuleMatch(r: Pick<AppRule, 'protocol' | 'port_lo' | 'port_hi' | 'cidrs'>): string {
  const parts: string[] = []
  const proto = r.protocol == null ? null : protocolLabel(r.protocol).replace(/ \(\d+\)$/, '')
  if (r.port_lo != null) {
    const ports = r.port_hi != null && r.port_hi !== r.port_lo ? `${r.port_lo}–${r.port_hi}` : String(r.port_lo)
    parts.push(`${proto ?? 'Any transport'} ${ports}`)
  } else if (r.protocol != null) {
    parts.push(protocolLabel(r.protocol))
  }
  if (r.cidrs?.length) {
    const shown = r.cidrs.slice(0, 2).join(', ') + (r.cidrs.length > 2 ? ` +${r.cidrs.length - 2}` : '')
    parts.push(r.port_lo != null || r.protocol != null ? shown : `Addresses ${shown}`)
  }
  return parts.join(' · ') || '—'
}

export interface RuleForm {
  application: string
  service: string
  category: string
  protocol: string // '' = any, else a number
  port_lo: string
  port_hi: string
  cidrs: string
  priority: string
  enabled: boolean
}

export const EMPTY_RULE_FORM: RuleForm = {
  application: '',
  service: '',
  category: '',
  protocol: '6',
  port_lo: '',
  port_hi: '',
  cidrs: '',
  priority: '100',
  enabled: true,
}

export interface RulePayload {
  application: string
  service: string | null
  category: string | null
  protocol: number | null
  port_lo: number | null
  port_hi: number | null
  cidrs: string[]
  priority: number
  enabled: boolean
}

function intOrNull(v: string): number | null | 'bad' {
  const t = v.trim()
  if (!t) return null
  if (!/^\d+$/.test(t)) return 'bad'
  return Number(t)
}

/** The API payload for a rule form, and the errors a user can fix before sending it. */
export function ruleFormToPayload(f: RuleForm): { payload: RulePayload; errors: Record<string, string> } {
  const errors: Record<string, string> = {}
  const application = f.application.trim()
  if (!application) errors.application = 'Application name is required'
  else if (application.length > 80) errors.application = 'At most 80 characters'
  const protocol = intOrNull(f.protocol)
  if (protocol === 'bad' || (typeof protocol === 'number' && protocol > 255)) errors.protocol = 'IP protocol is 0–255'
  const lo = intOrNull(f.port_lo)
  const hi = intOrNull(f.port_hi)
  if (lo === 'bad' || (typeof lo === 'number' && (lo < 1 || lo > 65535))) errors.port_lo = 'Ports are 1–65535'
  if (hi === 'bad' || (typeof hi === 'number' && (hi < 1 || hi > 65535))) errors.port_hi = 'Ports are 1–65535'
  if (typeof hi === 'number' && lo === null) errors.port_lo = 'A range needs its first port'
  if (typeof hi === 'number' && typeof lo === 'number' && hi < lo) errors.port_hi = 'The last port must not be below the first'
  if (typeof lo === 'number' && typeof protocol === 'number' && !PORT_PROTOCOLS.has(protocol) && !errors.protocol) {
    errors.protocol = `${protocolLabel(protocol)} has no ports; clear the ports to classify the whole protocol`
  }
  const cidrs = parseCidrLines(f.cidrs)
  if (cidrs.invalid) errors.cidrs = cidrs.lines.filter((l) => !l.ok).map((l) => `${l.raw}: ${l.error}`).slice(0, 3).join('; ')
  const priority = intOrNull(f.priority)
  if (priority === 'bad' || priority === null || priority > 100000) errors.priority = 'Priority is 0–100000'
  if (lo === null && hi === null && protocol === null && cidrs.valid.length === 0 && !errors.cidrs) {
    errors.port_lo = 'A rule needs a port, a protocol or an address range'
  }
  const num = (v: number | null | 'bad') => (typeof v === 'number' ? v : null)
  return {
    payload: {
      application,
      service: f.service.trim() || null,
      category: f.category.trim() || null,
      protocol: num(protocol),
      port_lo: num(lo),
      port_hi: num(hi),
      cidrs: cidrs.valid,
      priority: typeof priority === 'number' ? priority : 100,
      enabled: f.enabled,
    },
    errors,
  }
}

export function ruleToForm(r: AppRule): RuleForm {
  return {
    application: r.application,
    service: r.service ?? '',
    category: r.category ?? '',
    protocol: r.protocol == null ? '' : String(r.protocol),
    port_lo: r.port_lo == null ? '' : String(r.port_lo),
    port_hi: r.port_hi == null ? '' : String(r.port_hi),
    cidrs: r.cidrs.join('\n'),
    priority: String(r.priority),
    enabled: r.enabled,
  }
}

/** Order-independent text key of a JSON value, for debounced preview keys. */
export function stableKey(v: unknown): string {
  return JSON.stringify(v, (_k, x) => (x && typeof x === 'object' && !Array.isArray(x) ? Object.fromEntries(Object.entries(x).sort()) : x))
}

/** A value that follows `value` after it has been stable for `ms`. */
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const h = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(h)
  }, [value, ms])
  return v
}

/** POST with `envelope=1`, unwrapped like `nf.get`. */
async function nfPost<T>(path: string, body: unknown, params?: Record<string, string>): Promise<NfResult<T>> {
  const res = await api.post(path, body, { params: { ...(params ?? {}), envelope: '1' } })
  return unwrapEnvelope<T>(res.data)
}

function useInvalidateNetflow() {
  const qc = useQueryClient()
  // IP groups and rules change filters, breakdowns and labels everywhere in NetFlow.
  return () => qc.invalidateQueries({ queryKey: nfKeys.all })
}

/* ─── Shared bits ────────────────────────────────────────────────────────────────────────── */

function SectionTitle({ icon: Icon, children, hint }: { icon: typeof Info; children: ReactNode; hint?: ReactNode }) {
  return (
    <div>
      <CardTitle className="flex items-center gap-2 text-sm">
        <Icon className="h-4 w-4 text-text2" aria-hidden />
        {children}
      </CardTitle>
      {hint && <p className="mt-1 text-xs text-text2">{hint}</p>}
    </div>
  )
}

function FieldError({ id, children }: { id?: string; children?: ReactNode }) {
  if (!children) return null
  return (
    <p id={id} className="text-[11px] font-medium text-danger-fg" role="alert">
      {children}
    </p>
  )
}

function Field({
  label,
  htmlFor,
  error,
  hint,
  required,
  children,
  className,
}: {
  label: string
  htmlFor: string
  error?: string
  hint?: ReactNode
  required?: boolean
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('space-y-1', className)}>
      <label htmlFor={htmlFor} className="block text-xs font-medium text-text">
        {label}
        {required && <span className="ml-0.5 text-danger-fg" aria-hidden>*</span>}
      </label>
      {children}
      {error ? <FieldError id={`${htmlFor}-error`}>{error}</FieldError> : hint ? <p className="text-[11px] text-text2">{hint}</p> : null}
    </div>
  )
}

function Note({ tone = 'info', children, className }: { tone?: 'info' | 'warning' | 'danger'; children: ReactNode; className?: string }) {
  const Icon = tone === 'info' ? Info : AlertTriangle
  const box = {
    info: 'border-info/35 bg-info/10',
    warning: 'border-warning/35 bg-warning/10',
    danger: 'border-danger/35 bg-danger/10',
  }[tone]
  const fg = { info: 'text-info-fg', warning: 'text-warning-fg', danger: 'text-danger-fg' }[tone]
  return (
    <div role={tone === 'info' ? 'note' : 'alert'} className={cn('flex gap-2 rounded-md border px-3 py-2 text-xs text-text', box, className)}>
      <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', fg)} aria-hidden />
      <div className="min-w-0 space-y-1">{children}</div>
    </div>
  )
}

function ReadOnlyNotice() {
  return (
    <Note>
      <span className="inline-flex items-center gap-1">
        <Lock className="h-3 w-3" aria-hidden /> You can view these settings. Changing them needs the <strong>Manage NetFlow settings</strong> permission.
      </span>
    </Note>
  )
}

function MutationError({ error }: { error: unknown }) {
  if (!error) return null
  const ref = apiErrorRef(error)
  return (
    <Note tone="danger">
      <div>{apiErrorMessage(error, 'The change could not be saved')}</div>
      {ref && <div className="text-[11px] text-text2">Reference <span className="select-all font-mono">{ref}</span></div>}
    </Note>
  )
}

function Warnings({ items, title = 'Check before saving' }: { items: string[]; title?: string }) {
  if (!items.length) return null
  return (
    <Note tone="warning">
      <div className="font-medium">{title}</div>
      <ul className="list-disc space-y-0.5 pl-4">
        {items.map((w, i) => (
          <li key={i}>{w}</li>
        ))}
      </ul>
    </Note>
  )
}

function Who({ at, by }: { at: string | null; by: string | null }) {
  if (!at) return null
  const tz = displayTimeZone()
  return (
    <span title={formatDateTime(at, tz)}>
      {relativeTime(at)}
      {by ? ` by ${by}` : ''}
    </span>
  )
}

const selectClass =
  'h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40'

/* ─── Page ───────────────────────────────────────────────────────────────────────────────── */

/** NetFlow settings: IP groups, applications, enrichment, and links to related workspaces. */
export function NetflowSettingsPage() {
  const [sp, setSp] = useSearchParams()
  const tab = normalizeSettingsTab(sp.get('tab'))
  const can = useCan()
  const canManage = can('netflow.manage')
  const { window: w, rangeIdx, isCustom, setPreset, setCustom, range } = useNfWindow()
  const { filter, invalid, errors, removeKeys, clearAll } = useFlowFilter()

  const setTab = (next: string) => {
    const n = new URLSearchParams(sp)
    n.set('tab', normalizeSettingsTab(next))
    setSp(n, { replace: true })
  }

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title="NetFlow settings"
        icon={SlidersHorizontal}
        crumb="Settings"
        description="How flows are grouped and classified, how hosts are enriched, and what flow data cannot see."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-text2">Traffic figures cover</span>
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
            />
          </div>
        }
      />
      <HubLinks window={w} filter={filter} />
      <FilterChips filter={filter} invalid={invalid} errors={errors} onRemove={removeKeys} onClearAll={clearAll} tz={w.tz} />
      {!canManage && <ReadOnlyNotice />}
      <Tabs value={tab} onValueChange={setTab}>
        <div className="-mx-1 overflow-x-auto px-1">
          <TabsList aria-label="Settings sections">
            <TabsTrigger value="groups" className="gap-1.5">
              <Layers className="h-3.5 w-3.5" aria-hidden />
              IP groups
            </TabsTrigger>
            <TabsTrigger value="applications" className="gap-1.5">
              <Boxes className="h-3.5 w-3.5" aria-hidden />
              Applications
            </TabsTrigger>
            <TabsTrigger value="enrichment" className="gap-1.5">
              <Globe2 className="h-3.5 w-3.5" aria-hidden />
              Enrichment
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="groups">
          <IpGroupsTab window={w} filter={filter} canManage={canManage} />
        </TabsContent>
        <TabsContent value="applications">
          <ApplicationsTab window={w} filter={filter} canManage={canManage} />
        </TabsContent>
        <TabsContent value="enrichment">
          <EnrichmentTab window={w} filter={filter} canManage={canManage} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function HubLinks({ window: w, filter }: { window: NfWindow; filter: FlowFilter }) {
  const cards = [
    {
      to: buildNetflowLink('/netflow/health', { window: w, filter }),
      icon: Activity,
      title: 'Collector health',
      text: 'Exporters, templates, drops, sampling, clock offsets and onboarding.',
    },
    {
      to: buildNetflowLink('/netflow/anomalies', { window: w, filter, extra: { tab: 'settings' } }),
      icon: ShieldAlert,
      title: 'Detection settings',
      text: 'Detector thresholds, exceptions and threat-intelligence feeds.',
    },
  ]
  return (
    <nav aria-label="Related NetFlow settings" className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {cards.map((c) => (
        <Link
          key={c.title}
          to={c.to}
          className="group flex items-start gap-3 rounded-lg border border-border bg-surface p-3 shadow-card transition-shadow hover:shadow-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        >
          <c.icon className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-1 text-sm font-medium text-text">
              {c.title}
              <ArrowRight className="h-3.5 w-3.5 text-text2 transition-transform group-hover:translate-x-0.5" aria-hidden />
            </span>
            <span className="mt-0.5 block text-xs text-text2">{c.text}</span>
          </span>
        </Link>
      ))}
    </nav>
  )
}

/* ─── IP groups ──────────────────────────────────────────────────────────────────────────── */

interface GroupOverlap {
  group_id: string
  group: string
  cidr: string
  other_cidr: string
  relation: 'equal' | 'inside' | 'contains' | 'overlaps'
  nested: boolean
}

interface AddressCount {
  ipv4: number
  ipv6_prefixes: number
  label: string
}

export interface IpGroup {
  id: string
  name: string
  description: string | null
  cidrs: string[]
  color: string | null
  tags: string[]
  parent_id: string | null
  parent_name: string | null
  depth: number
  children: string[]
  addresses: AddressCount
  effective_cidrs: string[]
  effective_addresses: AddressCount
  overlaps: GroupOverlap[]
  references: { saved_views: number }
  created_at: string | null
  updated_at: string | null
  created_by: string | null
  updated_by: string | null
}

interface GroupList {
  items: IpGroup[]
  import: { source?: string; imported_at?: string; groups?: number; problems?: string[] } | null
  counts: { groups: number; cidrs: number }
  can_manage: boolean
}

interface GroupUsage {
  items: { id: string; name: string; hosts: number; bytes: number; flows: number }[]
  total: { hosts: number; bytes: number; flows: number }
  ungrouped: { bytes: number; flows: number }
  counting: string
}

/** Parents before children, children indented under their parent (then by name). */
export function orderGroupsAsTree<T extends { id: string; name: string; parent_id: string | null }>(groups: T[]): T[] {
  const byParent = new Map<string | null, T[]>()
  const ids = new Set(groups.map((g) => g.id))
  for (const g of groups) {
    const key = g.parent_id && ids.has(g.parent_id) ? g.parent_id : null
    byParent.set(key, [...(byParent.get(key) ?? []), g])
  }
  const out: T[] = []
  const seen = new Set<string>()
  const walk = (key: string | null) => {
    for (const g of (byParent.get(key) ?? []).sort((a, b) => a.name.localeCompare(b.name))) {
      if (seen.has(g.id)) continue
      seen.add(g.id)
      out.push(g)
      walk(g.id)
    }
  }
  walk(null)
  for (const g of groups) if (!seen.has(g.id)) out.push(g) // cycle guard
  return out
}

export function groupMatchesSearch(g: Pick<IpGroup, 'name' | 'description' | 'cidrs' | 'tags'>, q: string): boolean {
  const s = q.trim().toLowerCase()
  if (!s) return true
  return (
    g.name.toLowerCase().includes(s) ||
    (g.description ?? '').toLowerCase().includes(s) ||
    g.tags.some((t) => t.toLowerCase().includes(s)) ||
    g.cidrs.some((c) => c.toLowerCase().includes(s))
  )
}

/**
 * What the IP-group usage columns and summary show (NF-03): `stale` keeps the last good figures
 * (with a "refresh failed, showing data from …" chip and Retry), `failed` only when there is
 * nothing to show, `no_records` when the window has no flow records at all (unknown, not zero).
 */
export function groupUsageView(
  q: PanelQueryLike<{ data: { total: { flows: number } } }>,
): 'loading' | 'failed' | 'forbidden' | 'stale' | 'no_records' | 'ok' {
  const st = panelStatus(q, { isEmpty: () => false })
  if (st === 'loading') return 'loading'
  if (st === 'forbidden') return 'forbidden'
  if (st === 'error') return 'failed'
  if (q.data && q.data.data.total.flows === 0) return 'no_records'
  return st === 'stale' ? 'stale' : 'ok'
}

function IpGroupsTab({ window: w, filter, canManage }: { window: NfWindow; filter: FlowFilter; canManage: boolean }) {
  const list = useNfQuery<GroupList>('/netflow/settings/ip-groups', null, { refetchInterval: false })
  const usageParams = nfParams(w, filter)
  const usage = useNfQuery<GroupUsage>('/netflow/settings/ip-groups/usage', usageParams, { window: w })
  const [q, setQ] = useState('')
  const [editing, setEditing] = useState<IpGroup | 'new' | null>(null)
  const [deleting, setDeleting] = useState<IpGroup | null>(null)
  const [importing, setImporting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const invalidate = useInvalidateNetflow()
  const [deleteBusy, setDeleteBusy] = useState(false)

  const doExport = async () => {
    setExporting(true)
    try {
      const res = await api.get('/netflow/settings/ip-groups/export', { responseType: 'blob' })
      const disp = String(res.headers['content-disposition'] ?? '')
      const name = /filename="([^"]+)"/.exec(disp)?.[1] ?? 'netflow-ip-groups.json'
      const url = URL.createObjectURL(res.data as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error('Export failed', apiErrorMessage(e))
    } finally {
      setExporting(false)
    }
  }

  const doDelete = async () => {
    if (!deleting) return
    setDeleteBusy(true)
    try {
      const res = await api.delete(`/netflow/settings/ip-groups/${deleting.id}`)
      const detached: string[] = res.data?.detached_children ?? []
      toast.success(`Deleted ${deleting.name}`, detached.length ? `${detached.join(', ')} now stand on their own` : undefined)
      setDeleting(null)
      await invalidate()
    } catch (e) {
      toast.error('Could not delete the group', apiErrorMessage(e))
    } finally {
      setDeleteBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="gap-3 pb-3">
          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
            <SectionTitle
              icon={Layers}
              hint="Named address ranges (sites, departments, server farms). They label hosts, filter flows (group=) and break traffic down. A sub-group's addresses also count for its parent."
            >
              IP groups
            </SectionTitle>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={doExport} disabled={exporting} aria-label="Export IP groups as JSON">
                {exporting ? <Loader2 className="animate-spin" aria-hidden /> : <Download aria-hidden />}
                Export
              </Button>
              {canManage && (
                <>
                  <Button variant="outline" size="sm" onClick={() => setImporting(true)}>
                    <FileUp aria-hidden />
                    Import
                  </Button>
                  <Button size="sm" onClick={() => setEditing('new')}>
                    <Plus aria-hidden />
                    New group
                  </Button>
                </>
              )}
            </div>
          </div>
          <div className="relative max-w-sm">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-text2" aria-hidden />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search name, CIDR or tag"
              aria-label="Search IP groups"
              className="pl-8"
            />
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <UsageStatus usage={usage} window={w} />
          <QueryPanel
            query={list}
            label="IP groups"
            skeleton="rows"
            isEmpty={(r) => r.data.items.length === 0}
            empty={
              <span>
                No IP groups yet.{' '}
                {canManage ? 'Create one for each site, department or server range you want to see traffic for.' : ''}
              </span>
            }
          >
            {(r) => {
              const shown = orderGroupsAsTree(r.data.items).filter((g) => groupMatchesSearch(g, q))
              const u = usage.data?.data
              const usageById = new Map((u?.items ?? []).map((x) => [x.id, x]))
              const view = groupUsageView(usage)
              const usageLoading = view === 'loading'
              // A failed refresh keeps the last good figures (stale, flagged above the table); only
              // a failure with nothing to show, or a window without any flow records, is unknown.
              const usageFailed = view === 'failed' || view === 'forbidden' || view === 'no_records'
              if (!shown.length) {
                return <PanelMessage className="py-8" icon={Search} title={`No group matches "${q}"`} />
              }
              return (
                <GroupTable
                  groups={shown}
                  usageById={usageById}
                  total={u?.total ?? null}
                  usageLoading={usageLoading}
                  usageFailed={usageFailed}
                  window={w}
                  filter={filter}
                  canManage={canManage}
                  onEdit={setEditing}
                  onDelete={setDeleting}
                />
              )
            }}
          </QueryPanel>
          {list.data?.data.import && (
            <p className="text-[11px] text-text2">
              Imported once from {list.data.data.import.source ?? 'the legacy groups file'}
              {list.data.data.import.imported_at ? ` on ${formatDateTime(list.data.data.import.imported_at, displayTimeZone(), { seconds: false })}` : ''}
              {list.data.data.import.problems?.length ? ` (${list.data.data.import.problems.length} entries skipped)` : ''}. The file is no longer read.
            </p>
          )}
        </CardContent>
      </Card>
      {editing && (
        <GroupEditorDialog
          group={editing === 'new' ? null : editing}
          groups={list.data?.data.items ?? []}
          window={w}
          filter={filter}
          canManage={canManage}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await invalidate()
          }}
        />
      )}
      {importing && (
        <ImportDialog
          onClose={() => setImporting(false)}
          onApplied={async () => {
            setImporting(false)
            await invalidate()
          }}
        />
      )}
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={deleting ? `Delete ${deleting.name}?` : 'Delete group?'}
        destructive
        loading={deleteBusy}
        confirmText={deleteBusy ? 'Deleting…' : 'Delete group'}
        onConfirm={doDelete}
        description={
          deleting ? (
            <span className="space-y-1">
              <span className="block">Flows keep their addresses; only this label and filter go away.</span>
              {deleting.children.length > 0 && (
                <span className="block">Sub-groups {deleting.children.join(', ')} will stand on their own.</span>
              )}
              {deleting.references.saved_views > 0 && (
                <span className="block font-medium text-warning-fg">
                  {deleting.references.saved_views} saved view(s) filter on this group and will stop working until edited.
                </span>
              )}
            </span>
          ) : undefined
        }
      />
    </div>
  )
}

function UsageStatus({ usage, window: w }: { usage: ReturnType<typeof useNfQuery<GroupUsage>>; window: NfWindow }) {
  const view = groupUsageView(usage)
  if (view === 'forbidden') {
    return <Note tone="warning">You do not have access to traffic per group; the columns show no figures.</Note>
  }
  if (view === 'failed') {
    return (
      <Note tone="warning">
        <div>
          Traffic per group could not be loaded: {apiErrorMessage(usage.error)}
          {apiErrorRef(usage.error) && <span className="ml-1 font-mono text-[11px]">(ref {apiErrorRef(usage.error)})</span>}
        </div>
        <button type="button" onClick={() => usage.refetch()} className="inline-flex items-center gap-1 font-medium underline-offset-2 hover:underline">
          <RefreshCw className="h-3 w-3" aria-hidden /> Retry
        </button>
      </Note>
    )
  }
  const d = usage.data?.data
  if (d && d.total.flows === 0) {
    return (
      <div className="space-y-2">
        <Note tone="warning">No flow records were received in this window, so traffic per group is unknown (not zero).</Note>
        <DataQualityBar meta={usage.data?.meta} window={w} />
      </div>
    )
  }
  return (
    <div className="space-y-2">
      {view === 'stale' && <StaleChip query={usage} />}
      {d && (
        <p className="text-[11px] text-text2">
          {formatBytes(d.ungrouped.bytes)} ({formatPercent(shareOf(d.ungrouped.bytes, d.total.bytes))}) of the traffic in this window touched no group.{' '}
          {d.counting}
        </p>
      )}
      <DataQualityBar meta={usage.data?.meta} window={w} />
    </div>
  )
}

function ShareBar({ pct }: { pct: number | null }) {
  if (pct == null) return null
  return (
    <span className="mt-1 block h-1.5 w-full overflow-hidden rounded-full bg-surface2" aria-hidden>
      <span className="block h-full rounded-full bg-primary/70" style={{ width: `${Math.max(1, Math.min(100, pct))}%` }} />
    </span>
  )
}

function GroupTable({
  groups,
  usageById,
  total,
  usageLoading,
  usageFailed,
  window: w,
  filter,
  canManage,
  onEdit,
  onDelete,
}: {
  groups: IpGroup[]
  usageById: Map<string, { hosts: number; bytes: number; flows: number }>
  total: { hosts: number; bytes: number; flows: number } | null
  usageLoading: boolean
  usageFailed: boolean
  window: NfWindow
  filter: FlowFilter
  canManage: boolean
  onEdit: (g: IpGroup) => void
  onDelete: (g: IpGroup) => void
}) {
  const traffic = (g: IpGroup) => {
    if (usageLoading) return <Skeleton className="h-4 w-20" />
    const u = usageById.get(g.id)
    if (usageFailed || !u) return <span className="text-text2" title="Traffic is unknown">—</span>
    const pct = shareOf(u.bytes, total?.bytes)
    return (
      <span className="block min-w-[7rem]" title={`${formatBytesExact(u.bytes)}; ${formatCountExact(u.flows)} flow records`}>
        <span className="font-medium tabular-nums text-text">{formatBytes(u.bytes)}</span>
        <span className="ml-1 text-[11px] text-text2">{pct == null ? '' : formatPercent(pct)}</span>
        <ShareBar pct={pct} />
      </span>
    )
  }
  const hosts = (g: IpGroup) => {
    if (usageLoading) return <Skeleton className="h-4 w-10" />
    const u = usageById.get(g.id)
    if (usageFailed || !u) return <span className="text-text2">—</span>
    return <span className="tabular-nums" title={`${formatCountExact(u.hosts)} distinct addresses seen`}>{formatCount(u.hosts)}</span>
  }
  const overlapBadge = (g: IpGroup) => {
    const foreign = g.overlaps.filter((o) => !o.nested)
    if (!foreign.length) return null
    const names = Array.from(new Set(foreign.map((o) => o.group)))
    return (
      <span
        className="inline-flex items-center gap-1 rounded border border-warning/35 bg-warning/10 px-1.5 py-0.5 text-[11px] font-medium text-warning-fg"
        title={foreign.map((o) => `${o.cidr} ${o.relation} ${o.group} ${o.other_cidr}`).join('\n')}
      >
        <AlertTriangle className="h-3 w-3" aria-hidden />
        Overlaps {names.slice(0, 2).join(', ')}
        {names.length > 2 ? ` +${names.length - 2}` : ''}
      </span>
    )
  }
  const groupLink = (g: IpGroup) => buildNetflowLink('/netflow', { window: w, filter: { ...filter, group: g.name } })
  const actions = (g: IpGroup) => (
    <div className="flex items-center justify-end gap-1">
      <Button asChild variant="ghost" size="sm" className="h-8 px-2">
        <Link to={groupLink(g)} aria-label={`Traffic of ${g.name} in the overview`}>
          <ArrowRight aria-hidden />
          <span className="sr-only md:not-sr-only">Traffic</span>
        </Link>
      </Button>
      <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => onEdit(g)} aria-label={canManage ? `Edit ${g.name}` : `View ${g.name}`}>
        <Pencil aria-hidden />
      </Button>
      {canManage && (
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => onDelete(g)} aria-label={`Delete ${g.name}`}>
          <Trash2 aria-hidden />
        </Button>
      )}
    </div>
  )
  const nameCell = (g: IpGroup) => (
    <div className="flex min-w-0 items-start gap-2" style={{ paddingLeft: `${Math.min(g.depth, 4) * 14}px` }}>
      <span
        className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full border border-border-strong"
        style={{ background: g.color && isHexColor(g.color) ? g.color : 'transparent' }}
        aria-hidden
      />
      <div className="min-w-0">
        <Link to={groupLink(g)} className="break-words font-medium text-text hover:underline">
          {g.name}
        </Link>
        {g.parent_name && <div className="text-[11px] text-text2">Sub-group of {g.parent_name}</div>}
        {g.description && <div className="line-clamp-2 text-[11px] text-text2">{g.description}</div>}
        {g.tags.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-1">
            {g.tags.map((t) => (
              <span key={t} className="rounded bg-surface2 px-1.5 py-px text-[10px] text-text2">
                {t}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
  const cidrCell = (g: IpGroup) => (
    <div className="min-w-0">
      <div className="break-all font-mono text-[11px] text-text">
        {g.cidrs.slice(0, 3).join(', ')}
        {g.cidrs.length > 3 && <span className="text-text2"> +{g.cidrs.length - 3} more</span>}
      </div>
      <div className="text-[11px] text-text2">
        {g.addresses.label}
        {g.children.length > 0 && ` · with sub-groups: ${g.effective_addresses.label}`}
      </div>
      <div className="mt-0.5 flex flex-wrap gap-1">
        {overlapBadge(g)}
        {g.references.saved_views > 0 && (
          <span className="rounded border border-border px-1.5 py-0.5 text-[11px] text-text2">
            {g.references.saved_views} saved view{g.references.saved_views === 1 ? '' : 's'}
          </span>
        )}
      </div>
    </div>
  )
  return (
    <>
      {/* Narrow screens: one card per group, every figure readable without scrolling sideways. */}
      <ul className="space-y-2 md:hidden" aria-label="IP groups">
        {groups.map((g) => (
          <li key={g.id} className="rounded-lg border border-border p-3">
            <div className="flex items-start justify-between gap-2">
              {nameCell(g)}
              {actions(g)}
            </div>
            <div className="mt-2">{cidrCell(g)}</div>
            <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">
              <div>
                <dt className="text-text2">Hosts seen</dt>
                <dd className="font-medium text-text">{hosts(g)}</dd>
              </div>
              <div>
                <dt className="text-text2">Traffic</dt>
                <dd>{traffic(g)}</dd>
              </div>
            </dl>
          </li>
        ))}
      </ul>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full text-sm">
          <caption className="sr-only">IP groups with their address ranges and traffic in the selected window</caption>
          <thead>
            <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-text2">
              <th scope="col" className="px-2 py-2 font-medium">Group</th>
              <th scope="col" className="px-2 py-2 font-medium">Addresses</th>
              <th scope="col" className="px-2 py-2 text-right font-medium">Hosts seen</th>
              <th scope="col" className="px-2 py-2 font-medium">Traffic</th>
              <th scope="col" className="px-2 py-2 font-medium">Changed</th>
              <th scope="col" className="px-2 py-2 text-right font-medium"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.id} className="border-b border-border align-top last:border-0 hover:bg-surface2/50">
                <td className="max-w-[16rem] px-2 py-2">{nameCell(g)}</td>
                <td className="max-w-[18rem] px-2 py-2">{cidrCell(g)}</td>
                <td className="px-2 py-2 text-right">{hosts(g)}</td>
                <td className="px-2 py-2">{traffic(g)}</td>
                <td className="whitespace-nowrap px-2 py-2 text-[11px] text-text2">
                  <Who at={g.updated_at} by={g.updated_by} />
                </td>
                <td className="px-2 py-2">{actions(g)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

interface GroupPreview {
  cidrs: string[]
  errors: Record<string, string>
  warnings: string[]
  addresses: AddressCount
  overlaps: GroupOverlap[]
  traffic: { bytes: number; flows: number; total_bytes: number; hosts: number; top_hosts: { ip: string; bytes: number; flows: number }[] } | null
}

function GroupEditorDialog({
  group,
  groups,
  window: w,
  filter,
  canManage,
  onClose,
  onSaved,
}: {
  group: IpGroup | null
  groups: IpGroup[]
  window: NfWindow
  filter: FlowFilter
  canManage: boolean
  onClose: () => void
  onSaved: () => void | Promise<void>
}) {
  const [name, setName] = useState(group?.name ?? '')
  const [description, setDescription] = useState(group?.description ?? '')
  const [cidrText, setCidrText] = useState((group?.cidrs ?? []).join('\n'))
  const [parentId, setParentId] = useState(group?.parent_id ?? '')
  const [color, setColor] = useState(group?.color ?? '')
  const [tags, setTags] = useState((group?.tags ?? []).join(', '))
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [saveError, setSaveError] = useState<unknown>(null)
  const [conflict, setConflict] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [warnings, setWarnings] = useState<string[]>([])
  const [preview, setPreview] = useState<{ data: GroupPreview; meta: NfMeta | null } | null>(null)
  const [previewError, setPreviewError] = useState<unknown>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const readOnly = !canManage

  const parsed = useMemo(() => parseCidrLines(cidrText), [cidrText])
  const previewKey = useDebounced(stableKey({ c: parsed.valid, p: parentId, w: windowParams(w), f: filter }), 500)
  const seq = useRef(0)
  useEffect(() => {
    if (!parsed.valid.length) {
      setPreview(null)
      return
    }
    const mine = ++seq.current
    setPreviewLoading(true)
    setPreviewError(null)
    nfPost<GroupPreview>(
      '/netflow/settings/ip-groups/preview',
      { id: group?.id ?? null, name, cidrs: parsed.valid, parent_id: parentId || null },
      { ...windowParams(w), ...Object.fromEntries(Object.entries(nfParams(null, filter)).map(([k, v]) => [k, String(v)])) },
    )
      .then((r) => mine === seq.current && setPreview(r))
      .catch((e) => mine === seq.current && setPreviewError(e))
      .finally(() => mine === seq.current && setPreviewLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey])

  // Parents a group may nest under: not itself or one of its own sub-groups.
  const blocked = useMemo(() => {
    const out = new Set<string>()
    if (!group) return out
    out.add(group.id)
    let grew = true
    while (grew) {
      grew = false
      for (const g of groups) {
        if (g.parent_id && out.has(g.parent_id) && !out.has(g.id)) {
          out.add(g.id)
          grew = true
        }
      }
    }
    return out
  }, [group, groups])

  const save = async () => {
    const errs: Record<string, string> = {}
    if (!name.trim()) errs.name = 'Name is required'
    if (parsed.invalid) errs.cidrs = `${parsed.invalid} entr${parsed.invalid === 1 ? 'y is' : 'ies are'} not an IP address or CIDR`
    else if (!parsed.valid.length) errs.cidrs = 'Add at least one address or CIDR'
    if (color && !isHexColor(color)) errs.color = 'Use a hex colour such as #2a78d6'
    setFieldErrors(errs)
    if (Object.keys(errs).length) return
    setSaving(true)
    setSaveError(null)
    setConflict(null)
    const body = {
      name: name.trim(),
      description: description.trim() || null,
      cidrs: parsed.valid,
      parent_id: parentId || null,
      color: color || null,
      tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
      expected_updated_at: group?.updated_at ?? null,
    }
    try {
      const res = group
        ? await api.put(`/netflow/settings/ip-groups/${group.id}`, body)
        : await api.post('/netflow/settings/ip-groups', body)
      const warns: string[] = res.data?.warnings ?? []
      toast.success(group ? `Saved ${body.name}` : `Created ${body.name}`, warns.length ? warns[0] : undefined)
      setWarnings(warns)
      await onSaved()
    } catch (e) {
      const s = httpStatus(e)
      const fields = s === 400 ? responseFieldErrors(e) : {}
      setFieldErrors(fields)
      if (s === 409) setConflict(apiErrorMessage(e))
      // Field errors are shown next to their inputs; the banner is for anything else.
      else if (!onlyKnownFields(fields, GROUP_FIELDS)) setSaveError(e)
    } finally {
      setSaving(false)
    }
  }

  const traffic = preview?.data.traffic
  const foreign = (preview?.data.overlaps ?? group?.overlaps ?? []).filter((o) => !o.nested)
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-1.5rem)] max-w-3xl overflow-y-auto p-4 sm:p-6">
        <DialogHeader>
          <DialogTitle>{group ? (readOnly ? group.name : `Edit ${group.name}`) : 'New IP group'}</DialogTitle>
          <DialogDescription>
            Addresses are IPv4 or IPv6, single hosts or CIDR ranges, one per line. Flows match when either side is in the group.
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_minmax(0,18rem)]"
          onSubmit={(e) => {
            e.preventDefault()
            if (!readOnly) void save()
          }}
        >
          <fieldset disabled={readOnly || saving} className="min-w-0 space-y-3">
            <Field label="Name" htmlFor="grp-name" error={fieldErrors.name} required>
              <Input id="grp-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} aria-invalid={!!fieldErrors.name} aria-describedby={fieldErrors.name ? 'grp-name-error' : undefined} autoFocus />
            </Field>
            <Field
              label="Addresses"
              htmlFor="grp-cidrs"
              error={fieldErrors.cidrs}
              required
              hint={`${parsed.valid.length} valid entr${parsed.valid.length === 1 ? 'y' : 'ies'}${parsed.invalid ? `, ${parsed.invalid} invalid` : ''}`}
            >
              <Textarea
                id="grp-cidrs"
                value={cidrText}
                onChange={(e) => setCidrText(e.target.value)}
                rows={6}
                spellCheck={false}
                className="font-mono text-xs"
                placeholder={'10.20.0.0/16\n192.168.10.0/24\n2001:db8:10::/48'}
                aria-invalid={!!fieldErrors.cidrs || parsed.invalid > 0}
                aria-describedby={fieldErrors.cidrs ? 'grp-cidrs-error' : undefined}
              />
            </Field>
            {parsed.invalid > 0 && (
              <ul className="space-y-0.5 text-[11px] text-danger-fg" aria-label="Invalid entries">
                {parsed.lines.filter((l) => !l.ok).slice(0, 5).map((l, i) => (
                  <li key={i}>
                    <span className="font-mono">{l.raw}</span>: {l.error}
                  </li>
                ))}
              </ul>
            )}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Parent group" htmlFor="grp-parent" error={fieldErrors.parent_id} hint="Its addresses also count for the parent.">
                <select id="grp-parent" className={selectClass} value={parentId} onChange={(e) => setParentId(e.target.value)}>
                  <option value="">None (top level)</option>
                  {orderGroupsAsTree(groups)
                    .filter((g) => !blocked.has(g.id))
                    .map((g) => (
                      <option key={g.id} value={g.id}>
                        {'  '.repeat(g.depth)}
                        {g.name}
                      </option>
                    ))}
                </select>
              </Field>
              <Field label="Tags" htmlFor="grp-tags" error={fieldErrors.tags} hint="Comma separated, e.g. site, riyadh">
                <Input id="grp-tags" value={tags} onChange={(e) => setTags(e.target.value)} />
              </Field>
            </div>
            <Field label="Description" htmlFor="grp-desc" error={fieldErrors.description}>
              <Textarea id="grp-desc" value={description} onChange={(e) => setDescription(e.target.value)} rows={2} maxLength={2000} />
            </Field>
            <div className="space-y-1">
              <span id="grp-color-label" className="text-xs font-medium text-text">Colour</span>
              <div className="flex flex-wrap items-center gap-1.5" role="radiogroup" aria-labelledby="grp-color-label">
                <button
                  type="button"
                  role="radio"
                  aria-checked={!color}
                  onClick={() => setColor('')}
                  className={cn('h-7 rounded-md border px-2 text-[11px] text-text2', !color ? 'border-primary ring-2 ring-primary/40' : 'border-border')}
                >
                  None
                </button>
                {GROUP_COLORS.map((c) => (
                  <button
                    key={c.hex}
                    type="button"
                    role="radio"
                    aria-checked={color.toLowerCase() === c.hex}
                    aria-label={`${c.name} ${c.hex}`}
                    title={c.name}
                    onClick={() => setColor(c.hex)}
                    className={cn('h-7 w-7 rounded-md border border-border', color.toLowerCase() === c.hex && 'ring-2 ring-primary ring-offset-1 ring-offset-surface')}
                    style={{ background: c.hex }}
                  />
                ))}
                <Input
                  aria-label="Custom colour (hex)"
                  value={color}
                  onChange={(e) => setColor(e.target.value.trim())}
                  placeholder="#2a78d6"
                  className="h-7 w-24 font-mono text-xs"
                />
              </div>
              <FieldError>{fieldErrors.color}</FieldError>
            </div>
          </fieldset>
          <aside className="min-w-0 space-y-3 rounded-md border border-border bg-surface2/40 p-3 text-xs" aria-live="polite" aria-label="What this group covers">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-text">What it covers</span>
              {previewLoading && <Loader2 className="h-3.5 w-3.5 animate-spin text-text2" aria-label="Updating preview" />}
            </div>
            {!parsed.valid.length ? (
              <p className="text-text2">Add addresses to see how many hosts and how much traffic this group would cover.</p>
            ) : previewError ? (
              <p className="text-danger-fg">Preview unavailable: {apiErrorMessage(previewError)}</p>
            ) : preview ? (
              <>
                <p className="text-text">{preview.data.addresses.label}</p>
                {traffic ? (
                  <dl className="grid grid-cols-2 gap-2">
                    <div>
                      <dt className="text-text2">Hosts seen</dt>
                      <dd className="text-base font-semibold tabular-nums text-text">{formatCount(traffic.hosts)}</dd>
                    </div>
                    <div>
                      <dt className="text-text2">Traffic</dt>
                      <dd className="text-base font-semibold tabular-nums text-text" title={formatBytesExact(traffic.bytes)}>
                        {formatBytes(traffic.bytes)}
                      </dd>
                      <dd className="text-[11px] text-text2">{formatPercent(shareOf(traffic.bytes, traffic.total_bytes))} of all</dd>
                    </div>
                  </dl>
                ) : (
                  <p className="text-text2">No traffic figures.</p>
                )}
                <p className="text-[11px] text-text2">In {w.label}{preview.meta?.restricted ? ', exporters you can access' : ''}.</p>
                {traffic && traffic.top_hosts.length > 0 && (
                  <div>
                    <div className="mb-1 text-text2">Busiest hosts</div>
                    <ol className="space-y-0.5">
                      {traffic.top_hosts.slice(0, 5).map((h) => (
                        <li key={h.ip} className="flex justify-between gap-2">
                          <span className="truncate font-mono text-[11px] text-text" title={h.ip}>{h.ip}</span>
                          <span className="shrink-0 tabular-nums text-text2">{formatBytes(h.bytes)}</span>
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
                {foreign.length > 0 && (
                  <div className="space-y-1">
                    <div className="font-medium text-warning-fg">Overlaps other groups</div>
                    <ul className="space-y-0.5 text-[11px] text-text">
                      {foreign.slice(0, 5).map((o, i) => (
                        <li key={i}>
                          <span className="font-mono">{o.cidr}</span> {o.relation === 'inside' ? 'is inside' : o.relation === 'contains' ? 'contains' : o.relation === 'equal' ? 'equals' : 'overlaps'}{' '}
                          {o.group} <span className="font-mono">{o.other_cidr}</span>
                        </li>
                      ))}
                    </ul>
                    <p className="text-[11px] text-text2">Hosts in both count for both groups; host labels name the more specific range.</p>
                  </div>
                )}
                {preview.data.warnings.length > 0 && (
                  <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-text">
                    {preview.data.warnings.map((x, i) => (
                      <li key={i}>{x}</li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <Skeleton className="h-24 w-full" />
            )}
          </aside>
          <div className="space-y-2 lg:col-span-2">
            {conflict && (
              <Note tone="warning">
                <div>{conflict}</div>
              </Note>
            )}
            <MutationError error={saveError} />
            <Warnings items={warnings} title="Saved with notes" />
            {group && (
              <p className="text-[11px] text-text2">
                Created <Who at={group.created_at} by={group.created_by} />
                {group.updated_at !== group.created_at && (
                  <>
                    {' · '}changed <Who at={group.updated_at} by={group.updated_by} />
                  </>
                )}
              </p>
            )}
            {group && canManage && <HistoryList resource="ip_group" id={group.id} />}
            <DialogFooter className="flex-col-reverse gap-2 sm:flex-row">
              <Button type="button" variant="outline" onClick={onClose}>
                {readOnly ? 'Close' : 'Cancel'}
              </Button>
              {!readOnly && (
                <Button type="submit" disabled={saving}>
                  {saving && <Loader2 className="animate-spin" aria-hidden />}
                  {saving ? 'Saving…' : group ? 'Save changes' : 'Create group'}
                </Button>
              )}
            </DialogFooter>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

interface HistoryItem {
  at: string | null
  actor: string | null
  action: string
  resource_id: string | null
  metadata: Record<string, unknown>
}

function HistoryList({ resource, id }: { resource: 'ip_group' | 'app_rule' | 'enrichment'; id?: string }) {
  const [open, setOpen] = useState(false)
  const q = useNfQuery<{ items: HistoryItem[] }>('/netflow/settings/history', { resource, id, limit: 20 }, { enabled: open, refetchInterval: false })
  return (
    <details className="rounded-md border border-border text-xs" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="flex cursor-pointer items-center gap-1.5 px-3 py-2 font-medium text-text">
        <History className="h-3.5 w-3.5 text-text2" aria-hidden />
        Change history
      </summary>
      <div className="border-t border-border px-3 py-2">
        <QueryPanel query={q} label="change history" skeleton="rows" compact isEmpty={(r) => r.data.items.length === 0} empty="No recorded changes">
          {(r) => (
            <ol className="space-y-1">
              {r.data.items.map((h, i) => (
                <li key={i} className="flex flex-wrap gap-x-2 text-[11px]">
                  <span className="text-text2">{formatDateTime(h.at, displayTimeZone(), { seconds: false })}</span>
                  <span className="font-medium text-text">{h.actor ?? 'system'}</span>
                  <span className="text-text2">{h.action.replace(/^netflow\./, '').replace(/[._]/g, ' ')}</span>
                </li>
              ))}
            </ol>
          )}
        </QueryPanel>
      </div>
    </details>
  )
}

interface ImportResult {
  dry_run: boolean
  mode: 'merge' | 'replace'
  created: string[]
  updated: string[]
  unchanged: string[]
  deleted: string[]
  problems: string[]
  warnings: string[]
}

function ImportDialog({ onClose, onApplied }: { onClose: () => void; onApplied: () => void | Promise<void> }) {
  const [text, setText] = useState('')
  const [fileName, setFileName] = useState<string | null>(null)
  const [mode, setMode] = useState<'merge' | 'replace'>('merge')
  const [plan, setPlan] = useState<ImportResult | null>(null)
  const [parseError, setParseError] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<'check' | 'apply' | null>(null)
  const [confirmReplace, setConfirmReplace] = useState(false)

  const doc = useMemo(() => {
    if (!text.trim()) return null
    try {
      return JSON.parse(text) as unknown
    } catch {
      return undefined
    }
  }, [text])

  useEffect(() => {
    setPlan(null)
    setParseError(doc === undefined ? 'This is not valid JSON' : null)
  }, [doc, mode])

  const run = async (dryRun: boolean) => {
    if (!doc) return
    setBusy(dryRun ? 'check' : 'apply')
    setError(null)
    try {
      const res = await api.post('/netflow/settings/ip-groups/import', { document: doc, mode, dry_run: dryRun })
      const result = res.data as ImportResult
      if (dryRun) setPlan(result)
      else {
        toast.success('IP groups imported', `${result.created.length} created, ${result.updated.length} updated, ${result.deleted.length} deleted`)
        await onApplied()
      }
    } catch (e) {
      const r = (e as { response?: { data?: { result?: ImportResult } } }).response?.data?.result
      if (r) setPlan(r)
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  const list = (label: string, names: string[], tone?: 'danger') =>
    names.length > 0 && (
      <div>
        <div className={cn('font-medium', tone === 'danger' ? 'text-danger-fg' : 'text-text')}>
          {label} ({names.length})
        </div>
        <div className="break-words text-text2">{names.slice(0, 20).join(', ')}{names.length > 20 ? ` +${names.length - 20} more` : ''}</div>
      </div>
    )

  const canApply = !!plan && plan.problems.length === 0 && !busy
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-1.5rem)] max-w-2xl overflow-y-auto p-4 sm:p-6">
        <DialogHeader>
          <DialogTitle>Import IP groups</DialogTitle>
          <DialogDescription>
            A file exported from this page, or the legacy list of {'{name, cidrs}'} objects. Check it first: nothing changes until you apply, and a file with problems is not applied at all.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 font-medium text-text hover:bg-surface2 focus-within:ring-2 focus-within:ring-primary/40">
              <Upload className="h-3.5 w-3.5" aria-hidden />
              Choose file
              <input
                type="file"
                accept="application/json,.json"
                className="sr-only"
                onChange={async (e) => {
                  const f = e.target.files?.[0]
                  if (!f) return
                  setFileName(f.name)
                  setText(await f.text())
                }}
              />
            </label>
            <span className="text-text2">{fileName ?? 'or paste the JSON below'}</span>
          </div>
          <Textarea aria-label="Groups JSON" value={text} onChange={(e) => setText(e.target.value)} rows={6} spellCheck={false} className="font-mono text-[11px]" placeholder='{"groups": [{"name": "HQ", "cidrs": ["10.1.0.0/16"]}]}' />
          {parseError && <FieldError>{parseError}</FieldError>}
          <fieldset className="space-y-1">
            <legend className="font-medium text-text">Mode</legend>
            <label className="flex items-start gap-2">
              <input type="radio" name="import-mode" checked={mode === 'merge'} onChange={() => setMode('merge')} className="mt-0.5" />
              <span>
                <span className="font-medium text-text">Merge</span>
                <span className="block text-text2">Create new names, update existing ones, keep groups that are not in the file.</span>
              </span>
            </label>
            <label className="flex items-start gap-2">
              <input type="radio" name="import-mode" checked={mode === 'replace'} onChange={() => setMode('replace')} className="mt-0.5" />
              <span>
                <span className="font-medium text-text">Replace</span>
                <span className="block text-text2">Make the appliance match the file: groups not in it are deleted.</span>
              </span>
            </label>
          </fieldset>
          {plan && (
            <div className="space-y-2 rounded-md border border-border p-3" aria-live="polite">
              <div className="flex items-center gap-1.5 font-medium text-text">
                {plan.problems.length ? <AlertTriangle className="h-3.5 w-3.5 text-danger-fg" aria-hidden /> : <CheckCircle2 className="h-3.5 w-3.5 text-success-fg" aria-hidden />}
                {plan.problems.length ? `${plan.problems.length} problem(s) to fix in the file` : 'Ready to apply'}
              </div>
              {plan.problems.length > 0 && (
                <ul className="list-disc space-y-0.5 pl-4 text-danger-fg">
                  {plan.problems.slice(0, 12).map((p, i) => (
                    <li key={i}>{p}</li>
                  ))}
                </ul>
              )}
              {list('Create', plan.created)}
              {list('Update', plan.updated)}
              {list('Delete', plan.deleted, 'danger')}
              {list('Unchanged', plan.unchanged)}
              {plan.warnings.length > 0 && <Warnings items={plan.warnings.slice(0, 10)} title="Notes" />}
            </div>
          )}
          <MutationError error={error} />
        </div>
        <DialogFooter className="flex-col-reverse gap-2 sm:flex-row">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="outline" onClick={() => run(true)} disabled={!doc || !!busy}>
            {busy === 'check' && <Loader2 className="animate-spin" aria-hidden />}
            Check file
          </Button>
          <Button
            onClick={() => (mode === 'replace' && plan && plan.deleted.length ? setConfirmReplace(true) : run(false))}
            disabled={!canApply}
            variant={mode === 'replace' && plan?.deleted.length ? 'destructive' : 'default'}
          >
            {busy === 'apply' && <Loader2 className="animate-spin" aria-hidden />}
            Apply import
          </Button>
        </DialogFooter>
        <ConfirmDialog
          open={confirmReplace}
          onOpenChange={setConfirmReplace}
          title={`Delete ${plan?.deleted.length ?? 0} group(s)?`}
          destructive
          confirmText="Replace groups"
          description="Replace mode deletes every group that is not in the file. Saved views that filter on them stop working until edited."
          onConfirm={() => {
            setConfirmReplace(false)
            void run(false)
          }}
        />
      </DialogContent>
    </Dialog>
  )
}

/* ─── Applications ───────────────────────────────────────────────────────────────────────── */

interface RuleList {
  items: AppRule[]
  can_manage: boolean
  builtin_priority: number
  counts: { rules: number; enabled: number }
}

interface Catalogue {
  builtin: {
    application: string
    category: string
    ports: { port: number; service: string; transports: string[] }[]
    protocols: { protocol: number; name: string }[]
  }[]
  builtin_priority: number
  ephemeral_min: number
  operator_rules: number
  exporter_names: number
  applications: string[]
  categories: string[]
  precedence: string[]
  service_port: string
}

interface ExporterNames {
  items: {
    app_id: string
    engine: number
    selector: number
    name: string | null
    description: string | null
    category: string | null
    /** The application flows with this id are counted as (an NBAR "icmp" counts as ICMP). */
    application?: string | null
    exporter: string | null
    exporters: number
    updated_at: string | null
    bytes: number
    flows: number
  }[]
  unnamed: { app_id: string; engine: number; selector: number; bytes: number; flows: number }[]
  coverage: { records: number; with_app_id_field: number; with_app_id: number; legacy_records: number; share: number | null }
}

function ApplicationsTab({ window: w, filter, canManage }: { window: NfWindow; filter: FlowFilter; canManage: boolean }) {
  const rules = useNfQuery<RuleList>('/netflow/settings/app-rules', null, { refetchInterval: false })
  const catalogue = useNfQuery<Catalogue>('/netflow/settings/applications', null, { refetchInterval: false, staleTime: 60_000 })
  const [editing, setEditing] = useState<AppRule | 'new' | null>(null)
  const [deleting, setDeleting] = useState<AppRule | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const invalidate = useInvalidateNetflow()

  const toggle = async (r: AppRule, enabled: boolean) => {
    setBusyId(r.id)
    try {
      await api.patch(`/netflow/settings/app-rules/${r.id}`, { enabled })
      toast.success(`${enabled ? 'Enabled' : 'Disabled'} ${r.application}`)
      await invalidate()
    } catch (e) {
      toast.error('Could not change the rule', apiErrorMessage(e))
    } finally {
      setBusyId(null)
    }
  }
  const doDelete = async () => {
    if (!deleting) return
    setBusyId(deleting.id)
    try {
      await api.delete(`/netflow/settings/app-rules/${deleting.id}`)
      toast.success(`Deleted the ${deleting.application} rule`)
      setDeleting(null)
      await invalidate()
    } catch (e) {
      toast.error('Could not delete the rule', apiErrorMessage(e))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
            <SectionTitle
              icon={Boxes}
              hint="Your rules name applications by protocol, port range and address range. They apply before the built-in rules unless their priority number is higher."
            >
              Application rules
            </SectionTitle>
            {canManage && (
              <Button size="sm" onClick={() => setEditing('new')}>
                <Plus aria-hidden />
                New rule
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent>
          <QueryPanel
            query={rules}
            label="application rules"
            skeleton="rows"
            isEmpty={(r) => r.data.items.length === 0}
            empty={<span>No rules of your own yet: flows are classified by the exporter's application ids and the built-in rules below.</span>}
          >
            {(r) => (
              <RulesTable
                rules={r.data.items}
                canManage={canManage}
                busyId={busyId}
                window={w}
                onToggle={toggle}
                onEdit={setEditing}
                onDelete={setDeleting}
              />
            )}
          </QueryPanel>
        </CardContent>
      </Card>

      <ClassifyTester catalogue={catalogue.data?.data ?? null} />

      <ExporterNamesCard window={w} filter={filter} />

      <BuiltinCard catalogue={catalogue} />

      {editing && (
        <RuleEditorDialog
          rule={editing === 'new' ? null : editing}
          catalogue={catalogue.data?.data ?? null}
          canManage={canManage}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await invalidate()
          }}
        />
      )}
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={deleting ? `Delete the ${deleting.application} rule?` : 'Delete rule?'}
        destructive
        loading={!!deleting && busyId === deleting.id}
        confirmText="Delete rule"
        onConfirm={doDelete}
        description="Flows it classified fall back to the next matching rule (the exporter's application id, another rule of yours or a built-in rule). Saved filters on this application name stop matching if no other rule produces it."
      />
    </div>
  )
}

function RulesTable({
  rules,
  canManage,
  busyId,
  window: w,
  onToggle,
  onEdit,
  onDelete,
}: {
  rules: AppRule[]
  canManage: boolean
  busyId: string | null
  window: NfWindow
  onToggle: (r: AppRule, enabled: boolean) => void
  onEdit: (r: AppRule) => void
  onDelete: (r: AppRule) => void
}) {
  const row = (r: AppRule) => ({
    app: (
      <div className="min-w-0">
        <Link to={buildNetflowLink('/netflow', { window: w, filter: { app: r.application } })} className="break-words font-medium text-text hover:underline">
          {r.application}
        </Link>
        <div className="text-[11px] text-text2">
          {r.category ?? 'No category'}
          {r.service ? ` · service "${r.service}"` : ''}
        </div>
      </div>
    ),
    match: <span className="break-all font-mono text-[11px] text-text">{describeRuleMatch(r)}</span>,
    toggle: (
      <span className="inline-flex items-center gap-2">
        <Switch
          checked={r.enabled}
          disabled={!canManage || busyId === r.id}
          onCheckedChange={(v) => onToggle(r, v)}
          aria-label={`${r.enabled ? 'Disable' : 'Enable'} the ${r.application} rule`}
        />
        <span className="text-[11px] text-text2">{r.enabled ? 'On' : 'Off'}</span>
      </span>
    ),
    actions: (
      <div className="flex justify-end gap-1">
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => onEdit(r)} aria-label={canManage ? `Edit the ${r.application} rule` : `View the ${r.application} rule`}>
          <Pencil aria-hidden />
        </Button>
        {canManage && (
          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => onDelete(r)} aria-label={`Delete the ${r.application} rule`}>
            <Trash2 aria-hidden />
          </Button>
        )}
      </div>
    ),
  })
  return (
    <>
      <ul className="space-y-2 md:hidden" aria-label="Application rules">
        {rules.map((r) => {
          const c = row(r)
          return (
            <li key={r.id} className={cn('rounded-lg border border-border p-3', !r.enabled && 'opacity-80')}>
              <div className="flex items-start justify-between gap-2">
                {c.app}
                {c.actions}
              </div>
              <div className="mt-1">{c.match}</div>
              <div className="mt-2 flex items-center justify-between text-[11px] text-text2">
                <span>Priority {r.priority}</span>
                {c.toggle}
              </div>
            </li>
          )
        })}
      </ul>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full text-sm">
          <caption className="sr-only">Application rules in the order they apply</caption>
          <thead>
            <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-text2">
              <th scope="col" className="px-2 py-2 text-right font-medium">Priority</th>
              <th scope="col" className="px-2 py-2 font-medium">Application</th>
              <th scope="col" className="px-2 py-2 font-medium">Matches</th>
              <th scope="col" className="px-2 py-2 font-medium">Enabled</th>
              <th scope="col" className="px-2 py-2 font-medium">Changed</th>
              <th scope="col" className="px-2 py-2"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r) => {
              const c = row(r)
              return (
                <tr key={r.id} className="border-b border-border align-top last:border-0 hover:bg-surface2/50">
                  <td className="px-2 py-2 text-right tabular-nums">{r.priority}</td>
                  <td className="max-w-[16rem] px-2 py-2">{c.app}</td>
                  <td className="max-w-[20rem] px-2 py-2">{c.match}</td>
                  <td className="px-2 py-2">{c.toggle}</td>
                  <td className="whitespace-nowrap px-2 py-2 text-[11px] text-text2"><Who at={r.updated_at} by={r.updated_by} /></td>
                  <td className="px-2 py-2">{c.actions}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

interface RulePreview {
  window: { from: string; to: string }
  total: { bytes: number; flows: number }
  changed: { bytes: number; flows: number; share: number | null }
  already: { application: string | null; bytes: number; flows: number }
  changes: {
    from: string
    to: string
    bytes: number
    flows: number
    examples: { protocol: number; port: number | null; service: string | null; bytes: number; flows: number }[]
  }[]
  truncated: boolean
  basis: string
}

interface Classification {
  service_port: number | null
  direction: string
  service: string | null
  application: string
  category: string
  ip_protocol: string
  basis: string
  confidence: string
  port_applicable: boolean
}

const PROTOCOL_CHOICES: { value: string; label: string }[] = [
  { value: '6', label: 'TCP' },
  { value: '17', label: 'UDP' },
  { value: '', label: 'Any transport (TCP, UDP, SCTP…)' },
  { value: '132', label: 'SCTP' },
  { value: '1', label: 'ICMP' },
  { value: '58', label: 'ICMPv6' },
  { value: '50', label: 'ESP' },
  { value: '51', label: 'AH' },
  { value: '47', label: 'GRE' },
]

function RuleEditorDialog({
  rule,
  catalogue,
  canManage,
  onClose,
  onSaved,
}: {
  rule: AppRule | null
  catalogue: Catalogue | null
  canManage: boolean
  onClose: () => void
  onSaved: () => void | Promise<void>
}) {
  const [form, setForm] = useState<RuleForm>(rule ? ruleToForm(rule) : EMPTY_RULE_FORM)
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({})
  const [touched, setTouched] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<unknown>(null)
  const [conflict, setConflict] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const readOnly = !canManage
  const { payload, errors } = useMemo(() => ruleFormToPayload(form), [form])
  const shownErrors = { ...(touched ? errors : {}), ...serverErrors }
  const customProto = form.protocol !== '' && !PROTOCOL_CHOICES.some((c) => c.value === form.protocol)
  const set = (patch: Partial<RuleForm>) => {
    setForm((f) => ({ ...f, ...patch }))
    setServerErrors({})
  }

  const previewKey = useDebounced(Object.keys(errors).length ? null : stableKey(payload), 600)
  const [preview, setPreview] = useState<RulePreview | null>(null)
  const [previewError, setPreviewError] = useState<unknown>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const seq = useRef(0)
  useEffect(() => {
    if (!previewKey) {
      setPreview(null)
      return
    }
    const mine = ++seq.current
    setPreviewLoading(true)
    setPreviewError(null)
    nfPost<RulePreview>('/netflow/settings/app-rules/preview', { rule: payload, replace_id: rule?.id ?? null }, { hours: '1' })
      .then((r) => mine === seq.current && setPreview(r.data))
      .catch((e) => mine === seq.current && setPreviewError(e))
      .finally(() => mine === seq.current && setPreviewLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey])

  const save = async () => {
    setTouched(true)
    if (Object.keys(errors).length) return
    setSaving(true)
    setSaveError(null)
    setConflict(null)
    try {
      const body = { ...payload, expected_updated_at: rule?.updated_at ?? null }
      const res = rule ? await api.put(`/netflow/settings/app-rules/${rule.id}`, body) : await api.post('/netflow/settings/app-rules', body)
      const warns: string[] = res.data?.warnings ?? []
      setWarnings(warns)
      toast.success(rule ? `Saved the ${payload.application} rule` : `Created the ${payload.application} rule`, warns[0])
      await onSaved()
    } catch (e) {
      const s = httpStatus(e)
      const fields = s === 400 ? responseFieldErrors(e) : {}
      setServerErrors(fields)
      if (s === 409) setConflict(apiErrorMessage(e))
      else if (!onlyKnownFields(fields, RULE_FIELDS)) setSaveError(e)
    } finally {
      setSaving(false)
    }
  }

  const err = (k: string) => shownErrors[k]
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-1.5rem)] max-w-3xl overflow-y-auto p-4 sm:p-6">
        <DialogHeader>
          <DialogTitle>{rule ? (readOnly ? `${rule.application} rule` : `Edit the ${rule.application} rule`) : 'New application rule'}</DialogTitle>
          <DialogDescription>
            A port rule matches the service port on either side of a flow, so replies count as the same application as requests.
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_minmax(0,18rem)]"
          onSubmit={(e) => {
            e.preventDefault()
            if (!readOnly) void save()
          }}
        >
          <fieldset disabled={readOnly || saving} className="min-w-0 space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Application" htmlFor="rule-app" error={err('application')} required>
                <Input id="rule-app" list="rule-app-names" value={form.application} onChange={(e) => set({ application: e.target.value })} maxLength={80} autoFocus />
                <datalist id="rule-app-names">
                  {(catalogue?.applications ?? []).map((a) => (
                    <option key={a} value={a} />
                  ))}
                </datalist>
              </Field>
              <Field label="Category" htmlFor="rule-cat" error={err('category')} hint="Groups applications in reports">
                <Input id="rule-cat" list="rule-cat-names" value={form.category} onChange={(e) => set({ category: e.target.value })} maxLength={64} />
                <datalist id="rule-cat-names">
                  {(catalogue?.categories ?? []).map((a) => (
                    <option key={a} value={a} />
                  ))}
                </datalist>
              </Field>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Field label="Protocol" htmlFor="rule-proto" error={err('protocol')} className="sm:col-span-1">
                <select id="rule-proto" className={selectClass} value={customProto ? 'custom' : form.protocol} onChange={(e) => set({ protocol: e.target.value === 'custom' ? '0' : e.target.value })}>
                  {PROTOCOL_CHOICES.map((c) => (
                    <option key={c.value || 'any'} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                  <option value="custom">Other protocol number…</option>
                </select>
                {customProto && (
                  <Input aria-label="IP protocol number" className="mt-1" inputMode="numeric" value={form.protocol} onChange={(e) => set({ protocol: e.target.value })} />
                )}
              </Field>
              <Field label="First port" htmlFor="rule-lo" error={err('port_lo')}>
                <Input id="rule-lo" inputMode="numeric" value={form.port_lo} onChange={(e) => set({ port_lo: e.target.value })} placeholder="e.g. 8443" />
              </Field>
              <Field label="Last port" htmlFor="rule-hi" error={err('port_hi')} hint="Blank for a single port">
                <Input id="rule-hi" inputMode="numeric" value={form.port_hi} onChange={(e) => set({ port_hi: e.target.value })} />
              </Field>
            </div>
            <Field
              label="Server addresses"
              htmlFor="rule-cidrs"
              error={err('cidrs')}
              hint="Optional. With ports: the side with the service port must be in these ranges. Without ports: either side."
            >
              <Textarea id="rule-cidrs" rows={3} spellCheck={false} className="font-mono text-xs" value={form.cidrs} onChange={(e) => set({ cidrs: e.target.value })} placeholder="10.0.10.0/24" />
            </Field>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Field label="Service label" htmlFor="rule-svc" error={err('service')} hint={'e.g. "Payroll API"'} className="sm:col-span-2">
                <Input id="rule-svc" value={form.service} onChange={(e) => set({ service: e.target.value })} maxLength={80} />
              </Field>
              <Field label="Priority" htmlFor="rule-prio" error={err('priority')} hint={`Lower applies first; built-ins ${catalogue?.builtin_priority ?? 1000}`}>
                <Input id="rule-prio" inputMode="numeric" value={form.priority} onChange={(e) => set({ priority: e.target.value })} />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-xs text-text">
              <Switch checked={form.enabled} onCheckedChange={(v) => set({ enabled: v })} aria-label="Rule enabled" />
              Enabled
            </label>
          </fieldset>
          <aside className="min-w-0 space-y-2 rounded-md border border-border bg-surface2/40 p-3 text-xs" aria-live="polite" aria-label="What this rule would change">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-text">Effect in the last hour</span>
              {previewLoading && <Loader2 className="h-3.5 w-3.5 animate-spin text-text2" aria-label="Updating preview" />}
            </div>
            {!previewKey ? (
              <p className="text-text2">Complete the rule to see which flows it would reclassify.</p>
            ) : previewError ? (
              <p className="text-danger-fg">
                Preview unavailable: {apiErrorMessage(previewError)}
                {apiErrorRef(previewError) && <span className="ml-1 font-mono text-[11px]">(ref {apiErrorRef(previewError)})</span>}
              </p>
            ) : preview ? (
              <RulePreviewView preview={preview} />
            ) : (
              <Skeleton className="h-24 w-full" />
            )}
          </aside>
          <div className="space-y-2 lg:col-span-2">
            <RuleTestBox draft={Object.keys(errors).length ? null : payload} replaceId={rule?.id ?? null} />
            {conflict && <Note tone="warning">{conflict}</Note>}
            <MutationError error={saveError} />
            <Warnings items={warnings} title="Saved with notes" />
            {rule && canManage && <HistoryList resource="app_rule" id={rule.id} />}
            <DialogFooter className="flex-col-reverse gap-2 sm:flex-row">
              <Button type="button" variant="outline" onClick={onClose}>
                {readOnly ? 'Close' : 'Cancel'}
              </Button>
              {!readOnly && (
                <Button type="submit" disabled={saving}>
                  {saving && <Loader2 className="animate-spin" aria-hidden />}
                  {saving ? 'Saving…' : rule ? 'Save rule' : 'Create rule'}
                </Button>
              )}
            </DialogFooter>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function RulePreviewView({ preview }: { preview: RulePreview }) {
  const p = preview
  if (p.total.flows === 0) {
    return <p className="text-text2">No flow records in the last hour from the exporters you can access, so the effect is unknown.</p>
  }
  return (
    <div className="space-y-2">
      <p className="text-text">
        {p.changed.flows === 0 ? (
          'Would change no flows.'
        ) : (
          <>
            Would reclassify <strong className="tabular-nums">{formatCount(p.changed.flows)}</strong> of {formatCount(p.total.flows)} flow records (
            <span title={formatBytesExact(p.changed.bytes)}>{formatBytes(p.changed.bytes)}</span>, {formatPercent(p.changed.share == null ? null : p.changed.share * 100)} of bytes).
          </>
        )}
      </p>
      {p.already.application && p.already.flows > 0 && (
        <p className="text-[11px] text-text2">
          {formatCount(p.already.flows)} flow records are already {p.already.application}.
        </p>
      )}
      {p.changes.length > 0 && (
        <ul className="space-y-1.5">
          {p.changes.slice(0, 6).map((c, i) => (
            <li key={i} className="rounded border border-border bg-surface p-2">
              <div className="flex flex-wrap items-center gap-1 text-[11px] text-text">
                <span className="text-text2">{c.from}</span>
                <ArrowRight className="h-3 w-3 text-text2" aria-label="becomes" />
                <span className="font-medium">{c.to}</span>
              </div>
              <div className="text-[11px] text-text2">
                {formatCount(c.flows)} flows · {formatBytes(c.bytes)}
                {c.examples.length > 0 && ` · e.g. ${c.examples.map((e) => e.service ?? `${protocolLabel(e.protocol)}`).join(', ')}`}
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="text-[11px] text-text2">{p.basis}</p>
    </div>
  )
}

function ClassificationView({ c, title }: { c: Classification; title: string }) {
  const conf = { high: 'High', medium: 'Medium', low: 'Low', 'n/a': 'Not applicable' }[c.confidence] ?? c.confidence
  const basis: Record<string, string> = {
    exporter: "exporter's application id",
    dst_rule: 'rule on the destination port',
    src_rule: 'rule on the source port (a reply)',
    rule: 'your protocol/address rule',
    protocol: 'protocol',
    lower_port: 'well-known port',
    ephemeral: 'registered port talking to an ephemeral one',
    none: 'nothing matched',
  }
  return (
    <div className="min-w-0 rounded-md border border-border p-2">
      <div className="text-[11px] text-text2">{title}</div>
      <div className="break-words text-sm font-semibold text-text">{c.application}</div>
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-[11px]">
        <dt className="text-text2">Service</dt>
        <dd className="text-text">{c.port_applicable ? c.service ?? 'Unknown' : c.service ?? 'Not applicable (no ports)'}</dd>
        <dt className="text-text2">Direction</dt>
        <dd className="text-text">{c.direction}</dd>
        <dt className="text-text2">Based on</dt>
        <dd className="text-text">{basis[c.basis] ?? c.basis}</dd>
        <dt className="text-text2">Confidence</dt>
        <dd className="text-text">{conf}</dd>
      </dl>
    </div>
  )
}

interface TestForm {
  protocol: string
  src_ip: string
  src_port: string
  dst_ip: string
  dst_port: string
}

function useClassifyTest(draft: RulePayload | null, replaceId: string | null) {
  const [form, setForm] = useState<TestForm>({ protocol: '6', src_ip: '', src_port: '51514', dst_ip: '', dst_port: '443' })
  const [result, setResult] = useState<{ current: Classification; candidate: Classification | null } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const body = {
        protocol: Number(form.protocol || 0),
        src_port: Number(form.src_port || 0),
        dst_port: Number(form.dst_port || 0),
        src_ip: form.src_ip.trim() || null,
        dst_ip: form.dst_ip.trim() || null,
        rule: draft,
        replace_id: replaceId,
      }
      const res = await api.post('/netflow/settings/applications/test', body)
      setResult(res.data)
    } catch (e) {
      setError(e)
      setResult(null)
    } finally {
      setBusy(false)
    }
  }
  return { form, setForm, result, error, busy, run }
}

function TestInputs({ t, idPrefix }: { t: ReturnType<typeof useClassifyTest>; idPrefix: string }) {
  const f = t.form
  const set = (p: Partial<TestForm>) => t.setForm({ ...f, ...p })
  const fields = responseFieldErrors(t.error)
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
      <Field label="Protocol" htmlFor={`${idPrefix}-proto`}>
        <select id={`${idPrefix}-proto`} className={selectClass} value={f.protocol} onChange={(e) => set({ protocol: e.target.value })}>
          {PROTOCOL_CHOICES.filter((c) => c.value).map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Source IP" htmlFor={`${idPrefix}-sip`} error={fields.src_ip}>
        <Input id={`${idPrefix}-sip`} value={f.src_ip} onChange={(e) => set({ src_ip: e.target.value })} placeholder="optional" />
      </Field>
      <Field label="Source port" htmlFor={`${idPrefix}-sport`}>
        <Input id={`${idPrefix}-sport`} inputMode="numeric" value={f.src_port} onChange={(e) => set({ src_port: e.target.value })} />
      </Field>
      <Field label="Destination IP" htmlFor={`${idPrefix}-dip`} error={fields.dst_ip}>
        <Input id={`${idPrefix}-dip`} value={f.dst_ip} onChange={(e) => set({ dst_ip: e.target.value })} placeholder="optional" />
      </Field>
      <Field label="Destination port" htmlFor={`${idPrefix}-dport`}>
        <Input id={`${idPrefix}-dport`} inputMode="numeric" value={f.dst_port} onChange={(e) => set({ dst_port: e.target.value })} />
      </Field>
    </div>
  )
}

function RuleTestBox({ draft, replaceId }: { draft: RulePayload | null; replaceId: string | null }) {
  const t = useClassifyTest(draft, replaceId)
  return (
    <details className="rounded-md border border-border text-xs">
      <summary className="flex cursor-pointer items-center gap-1.5 px-3 py-2 font-medium text-text">
        <FlaskConical className="h-3.5 w-3.5 text-text2" aria-hidden />
        Test a flow against this rule
      </summary>
      <div className="space-y-2 border-t border-border p-3">
        <TestInputs t={t} idPrefix="rt" />
        <Button type="button" size="sm" variant="outline" onClick={t.run} disabled={t.busy || !draft}>
          {t.busy && <Loader2 className="animate-spin" aria-hidden />}
          Classify
        </Button>
        {t.error && Object.keys(responseFieldErrors(t.error)).length === 0 ? <MutationError error={t.error} /> : null}
        {t.result && (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <ClassificationView c={t.result.current} title="Today" />
            {t.result.candidate && <ClassificationView c={t.result.candidate} title="With this rule" />}
          </div>
        )}
      </div>
    </details>
  )
}

function ClassifyTester({ catalogue }: { catalogue: Catalogue | null }) {
  const t = useClassifyTest(null, null)
  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionTitle icon={FlaskConical} hint="Check how a flow would be labelled: which side is the service, which application, and why.">
          Test the classifier
        </SectionTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void t.run()
          }}
          className="space-y-2"
        >
          <TestInputs t={t} idPrefix="ct" />
          <Button type="submit" size="sm" variant="outline" disabled={t.busy}>
            {t.busy && <Loader2 className="animate-spin" aria-hidden />}
            Classify
          </Button>
        </form>
        {t.error && Object.keys(responseFieldErrors(t.error)).length === 0 ? <MutationError error={t.error} /> : null}
        {t.result && <ClassificationView c={t.result.current} title="Result" />}
        {catalogue && (
          <details className="text-xs">
            <summary className="cursor-pointer font-medium text-text">How the classifier decides</summary>
            <ol className="mt-2 list-decimal space-y-1 pl-5 text-text2">
              {catalogue.precedence.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ol>
            <p className="mt-2 text-text2">{catalogue.service_port}</p>
          </details>
        )}
      </CardContent>
    </Card>
  )
}

function ExporterNamesCard({ window: w, filter }: { window: NfWindow; filter: FlowFilter }) {
  const q = useNfQuery<ExporterNames>('/netflow/settings/applications/exporter-names', nfParams(w, filter), { window: w })
  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionTitle
          icon={Network}
          hint="Application names that exporters announce with NBAR2 / App-ID (IPFIX IE 95 with option records 96/94/372). They take precedence over port rules for flows that carry an application id."
        >
          Exporter application names
        </SectionTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <QueryPanel query={q} label="exporter application names" skeleton="rows" isEmpty={() => false}>
          {(r) => {
            const d = r.data
            const cov = d.coverage
            const tone: StatusTone = cov.records === 0 ? 'unknown' : cov.with_app_id > 0 ? 'success' : 'neutral'
            return (
              <div className="space-y-3">
                <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-3">
                  <StatusTile
                    tone={tone}
                    word={cov.records === 0 ? 'No data' : cov.with_app_id > 0 ? 'In use' : 'Not sent'}
                    label="Records with an application id"
                    value={cov.records === 0 ? null : formatPercent(cov.share == null ? null : cov.share * 100)}
                    caption={cov.records === 0 ? 'No records in this window' : `${formatCount(cov.with_app_id)} of ${formatCount(cov.records)}`}
                  />
                  <StatusTile tone="neutral" label="Names announced" value={formatCount(d.items.length)} caption="By exporters you can access" />
                  <StatusTile
                    tone={d.unnamed.length ? 'warning' : 'neutral'}
                    word={d.unnamed.length ? 'Unnamed' : null}
                    label="Ids without a name"
                    value={formatCount(d.unnamed.length)}
                    caption="Classified by port until the exporter sends its option data"
                  />
                </div>
                {cov.records > 0 && cov.with_app_id === 0 && (
                  <Note>
                    No exporter sent application ids in this window, so applications come from the port and address rules. On Cisco, add
                    <span className="mx-1 font-mono">match application name</span>
                    to the flow record and export the application-table option.
                  </Note>
                )}
                {d.items.length > 0 && (
                  <ul className="divide-y divide-border md:hidden" aria-label="Application names announced by exporters">
                    {d.items.map((a) => (
                      <li key={a.app_id} className="py-2 text-sm">
                        <div className="flex items-baseline justify-between gap-3">
                          <span className="min-w-0 break-words font-medium text-text">{a.name ?? 'Unnamed'}</span>
                          <span className="shrink-0 tabular-nums" title={formatBytesExact(a.bytes)}>{formatBytes(a.bytes)}</span>
                        </div>
                        {a.description && <div className="text-[11px] text-text2">{a.description}</div>}
                        {a.application && a.application !== a.name && <div className="text-[11px] text-text2">Counted as {a.application}</div>}
                        <div className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-text2">
                          <span className="font-mono">{a.engine}:{a.selector}</span>
                          {a.category && <span>{a.category}</span>}
                          <span>
                            {a.exporter}
                            {a.exporters > 1 ? ` +${a.exporters - 1}` : ''} · {relativeTime(a.updated_at)}
                          </span>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
                {d.items.length > 0 && (
                  <div className="hidden overflow-x-auto md:block">
                    <table className="w-full text-sm">
                      <caption className="sr-only">Application names announced by exporters and their traffic</caption>
                      <thead>
                        <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-text2">
                          <th scope="col" className="px-2 py-2 font-medium">Application</th>
                          <th scope="col" className="px-2 py-2 font-medium">Id</th>
                          <th scope="col" className="px-2 py-2 font-medium">Category</th>
                          <th scope="col" className="px-2 py-2 text-right font-medium">Traffic</th>
                          <th scope="col" className="px-2 py-2 font-medium">Announced</th>
                        </tr>
                      </thead>
                      <tbody>
                        {d.items.map((a) => (
                          <tr key={a.app_id} className="border-b border-border last:border-0">
                            <td className="px-2 py-1.5">
                              <div className="font-medium text-text">{a.name ?? 'Unnamed'}</div>
                              {a.description && <div className="text-[11px] text-text2">{a.description}</div>}
                              {a.application && a.application !== a.name && <div className="text-[11px] text-text2">Counted as {a.application}</div>}
                            </td>
                            <td className="px-2 py-1.5 font-mono text-[11px] text-text2">{a.engine}:{a.selector}</td>
                            <td className="px-2 py-1.5 text-text2">{a.category ?? '—'}</td>
                            <td className="px-2 py-1.5 text-right tabular-nums" title={formatBytesExact(a.bytes)}>{formatBytes(a.bytes)}</td>
                            <td className="px-2 py-1.5 text-[11px] text-text2">
                              {a.exporter}
                              {a.exporters > 1 ? ` +${a.exporters - 1}` : ''} · {relativeTime(a.updated_at)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <DataQualityBar meta={r.meta} window={w} filter={filter} />
              </div>
            )
          }}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}

function BuiltinCard({ catalogue }: { catalogue: ReturnType<typeof useNfQuery<Catalogue>> }) {
  const [q, setQ] = useState('')
  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionTitle icon={BookOpen} hint="Shipped with the appliance and read-only. Add a rule of your own to override one: yours apply first at the default priority.">
          Built-in rules
        </SectionTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="relative max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-text2" aria-hidden />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search application, service or port" aria-label="Search built-in rules" className="pl-8" />
        </div>
        <QueryPanel query={catalogue} label="built-in rules" skeleton="rows" isEmpty={(r) => r.data.builtin.length === 0}>
          {(r) => {
            const s = q.trim().toLowerCase()
            const apps = r.data.builtin.filter(
              (a) =>
                !s ||
                a.application.toLowerCase().includes(s) ||
                a.ports.some((p) => String(p.port) === s || p.service.toLowerCase().includes(s)) ||
                a.protocols.some((p) => p.name.toLowerCase().includes(s)),
            )
            if (!apps.length) return <PanelMessage className="py-6" icon={Search} title={`Nothing matches "${q}"`} />
            return (
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                {apps.map((a) => (
                  <details key={a.application} className="rounded-md border border-border text-xs" open={!!s}>
                    <summary className="flex cursor-pointer items-center justify-between gap-2 px-3 py-2">
                      <span className="min-w-0">
                        <span className="font-medium text-text">{a.application}</span>
                        {a.category.toLowerCase() !== a.application.toLowerCase() && <span className="ml-1.5 text-[11px] text-text2">· {a.category}</span>}
                      </span>
                      <span className="shrink-0 text-[11px] text-text2">
                        {a.ports.length ? `${a.ports.length} port${a.ports.length === 1 ? '' : 's'}` : ''}
                        {a.protocols.length ? `${a.ports.length ? ' · ' : ''}${a.protocols.map((p) => p.name).join(', ')}` : ''}
                      </span>
                    </summary>
                    {a.ports.length > 0 && (
                      <ul className="grid grid-cols-1 gap-x-3 gap-y-0.5 border-t border-border px-3 py-2 sm:grid-cols-2">
                        {a.ports.map((p) => (
                          <li key={p.port} className="flex justify-between gap-2">
                            <span className="text-text">{p.service}</span>
                            <span className="font-mono text-[11px] text-text2">
                              {p.port}/{p.transports.join('+').toLowerCase()}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </details>
                ))}
              </div>
            )
          }}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}

/* ─── Enrichment ─────────────────────────────────────────────────────────────────────────── */

interface EnrichmentStatus {
  rdns: {
    enabled: boolean
    deadline_s: number
    updated_at: string | null
    cache: { names: number; no_record: number; expired: number; pending: number; ttl_s: number; negative_ttl_s: number }
    resolvers: string[]
    limits: { deadline_min_s: number; deadline_max_s: number }
    notes: string[]
  }
  geoip: {
    directory: string
    writable: boolean
    available: boolean
    databases: {
      kind: 'country' | 'asn'
      file: string
      present: boolean
      valid: boolean | null
      size: number | null
      modified_at: string | null
      build_date: string | null
      age_days: number | null
      database_type: string | null
      description: string | null
      stamp: string | null
      stale: boolean | null
      error: string | null
      sample: { ip: string; value: string | number | null } | null
    }[]
    attribution: string
    refresh: string
    stale_after_days: number
  }
  can_manage: boolean
}

interface VisibilityNotes {
  totals: {
    records: number
    bytes: number
    tunnel_bytes: number
    nat_field_records: number
    translated_records: number
    app_id_records: number
    legacy_records: number
    fw_event_records: number
    tunnel_share: number | null
    nat_share: number | null
    app_id_share: number | null
    legacy_share: number | null
  }
  exporters: {
    exporter: string
    records: number
    bytes: number
    tunnel_bytes: number
    nat_field_records: number
    translated_records: number
    app_id_records: number
    legacy_records: number
    fw_event_records: number
  }[]
  tunnels: { protocol: number; protocol_name: string; service: string | null; bytes: number; flows: number }[]
  notes: { tunnels: string; nat: string; app_id: string }
}

/** GeoIP database tone: missing/unreadable is a warning, older than the stale limit too. */
export function geoipTone(d: { present: boolean; valid: boolean | null; stale: boolean | null }): StatusTone {
  if (!d.present) return 'warning'
  if (d.valid === false) return 'danger'
  if (d.stale) return 'warning'
  return d.valid ? 'success' : 'unknown'
}

function EnrichmentTab({ window: w, filter, canManage }: { window: NfWindow; filter: FlowFilter; canManage: boolean }) {
  const status = useNfQuery<EnrichmentStatus>('/netflow/settings/enrichment', null, { refetchInterval: 30_000 })
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <RdnsCard status={status} canManage={canManage} />
        <GeoipCard status={status} canManage={canManage} />
      </div>
      <VisibilityCard window={w} filter={filter} />
      <RetentionCard />
    </div>
  )
}

/**
 * Every place reverse-DNS names are looked up (FE-09): the switch below governs all of them, so the
 * hint lists them all. Keep in step with the API (hosts.py, breakdowns.py resolve=1, exporters.py
 * suggestions).
 */
export const RDNS_USES = [
  'host profiles',
  'the "Show reverse-DNS names" option on top talkers, endpoints, conversations and next hops',
  'exporter-to-device suggestions',
] as const
export const RDNS_USES_HINT = `Host names from PTR records, used on ${RDNS_USES[0]}, by ${RDNS_USES[1]}, and in ${RDNS_USES[2]}. When off, none of them sends a lookup.`

function RdnsCard({ status, canManage }: { status: ReturnType<typeof useNfQuery<EnrichmentStatus>>; canManage: boolean }) {
  const qc = useQueryClient()
  const current = status.data?.data.rdns
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [deadline, setDeadline] = useState<string>('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [testIp, setTestIp] = useState('')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ status: string; hostname: string | null; elapsed_ms: number } | null>(null)
  const [testError, setTestError] = useState<unknown>(null)
  const [flushing, setFlushing] = useState(false)
  useEffect(() => {
    if (current && enabled === null) {
      setEnabled(current.enabled)
      setDeadline(String(current.deadline_s))
    }
  }, [current, enabled])
  const refresh = () => qc.invalidateQueries({ queryKey: nfKeys.query('/netflow/settings/enrichment') })
  const dirty = !!current && (enabled !== current.enabled || Number(deadline) !== current.deadline_s)
  const deadlineNum = Number(deadline)
  const deadlineError =
    current && (!Number.isFinite(deadlineNum) || deadlineNum < current.limits.deadline_min_s || deadlineNum > current.limits.deadline_max_s)
      ? `Between ${current.limits.deadline_min_s} and ${current.limits.deadline_max_s} seconds`
      : undefined

  const save = async () => {
    if (deadlineError) return
    setSaving(true)
    setError(null)
    try {
      await api.put('/netflow/settings/enrichment/rdns', { enabled, deadline_s: deadlineNum })
      toast.success('Reverse DNS settings saved')
      await refresh()
    } catch (e) {
      setError(e)
    } finally {
      setSaving(false)
    }
  }
  const test = async () => {
    setTesting(true)
    setTestError(null)
    setTestResult(null)
    try {
      const res = await api.post('/netflow/settings/enrichment/rdns/test', { ip: testIp })
      setTestResult(res.data)
      await refresh()
    } catch (e) {
      setTestError(e)
    } finally {
      setTesting(false)
    }
  }
  const flush = async () => {
    setFlushing(true)
    try {
      const res = await api.post('/netflow/settings/enrichment/rdns/flush')
      toast.success('Cached names cleared', `${res.data?.dropped ?? 0} entries dropped`)
      await refresh()
    } catch (e) {
      toast.error('Could not clear the cache', apiErrorMessage(e))
    } finally {
      setFlushing(false)
    }
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionTitle icon={Network} hint={RDNS_USES_HINT}>
          Reverse DNS
        </SectionTitle>
      </CardHeader>
      <CardContent>
        <QueryPanel query={status} label="reverse-DNS settings" skeleton="tiles" isEmpty={() => false}>
          {(r) => {
            const d = r.data.rdns
            return (
              <div className="space-y-3">
                <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-3">
                  <StatusTile tone={d.enabled ? 'success' : 'neutral'} word={d.enabled ? 'On' : 'Off'} label="Lookups" value={d.enabled ? 'Enabled' : 'Disabled'} caption={`Deadline ${d.deadline_s} s`} />
                  <StatusTile tone="neutral" label="Cached names" value={formatCount(d.cache.names)} caption={`Kept ${Math.round(d.cache.ttl_s / 60)} min`} />
                  <StatusTile
                    tone={d.cache.pending ? 'info' : 'neutral'}
                    word={d.cache.pending ? 'Resolving' : null}
                    label="No record / pending"
                    value={`${formatCount(d.cache.no_record)} / ${formatCount(d.cache.pending)}`}
                    caption={`"No record" kept ${Math.round(d.cache.negative_ttl_s / 60)} min`}
                  />
                </div>
                <p className="text-[11px] text-text2">
                  Resolver{d.resolvers.length === 1 ? '' : 's'}: {d.resolvers.length ? <span className="font-mono">{d.resolvers.join(', ')}</span> : 'unknown'}
                  {d.updated_at && <> · changed <Who at={d.updated_at} by={null} /></>}
                </p>
                {canManage && (
                  <form
                    className="space-y-2 rounded-md border border-border p-3"
                    onSubmit={(e) => {
                      e.preventDefault()
                      void save()
                    }}
                  >
                    <label className="flex items-center gap-2 text-xs text-text">
                      <Switch checked={!!enabled} onCheckedChange={setEnabled} aria-label="Reverse DNS lookups enabled" />
                      Look up host names
                    </label>
                    <Field label="Deadline per request (seconds)" htmlFor="rdns-deadline" error={deadlineError} hint="Lookups still running then finish in the background.">
                      <Input id="rdns-deadline" inputMode="decimal" value={deadline} onChange={(e) => setDeadline(e.target.value)} className="max-w-[8rem]" />
                    </Field>
                    <MutationError error={error} />
                    <div className="flex flex-wrap gap-2">
                      <Button type="submit" size="sm" disabled={!dirty || saving || !!deadlineError}>
                        {saving && <Loader2 className="animate-spin" aria-hidden />}
                        Save
                      </Button>
                      <Button type="button" size="sm" variant="outline" onClick={flush} disabled={flushing}>
                        {flushing ? <Loader2 className="animate-spin" aria-hidden /> : <X aria-hidden />}
                        Clear cached names
                      </Button>
                    </div>
                  </form>
                )}
                {canManage && (
                  <form
                    className="flex flex-wrap items-end gap-2"
                    onSubmit={(e) => {
                      e.preventDefault()
                      void test()
                    }}
                  >
                    <Field label="Test a lookup" htmlFor="rdns-test" error={responseFieldErrors(testError).ip} className="min-w-[10rem] flex-1">
                      <Input id="rdns-test" value={testIp} onChange={(e) => setTestIp(e.target.value)} placeholder="10.0.0.5" />
                    </Field>
                    <Button type="submit" size="sm" variant="outline" disabled={testing || !testIp.trim()}>
                      {testing && <Loader2 className="animate-spin" aria-hidden />}
                      Resolve
                    </Button>
                    {testResult && (
                      <p className="w-full text-xs text-text" role="status">
                        {testResult.status === 'ok'
                          ? `${testResult.hostname} (${testResult.elapsed_ms} ms)`
                          : testResult.status === 'pending'
                            ? `Still resolving after ${testResult.elapsed_ms} ms; the answer will be cached when it arrives.`
                            : `No PTR record (${testResult.elapsed_ms} ms).`}
                      </p>
                    )}
                    {testError && !responseFieldErrors(testError).ip ? <MutationError error={testError} /> : null}
                  </form>
                )}
                <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-text2">
                  {d.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              </div>
            )
          }}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}

function fileSize(n: number | null): string {
  if (n == null) return '—'
  return formatBytes(n)
}

function GeoipCard({ status, canManage }: { status: ReturnType<typeof useNfQuery<EnrichmentStatus>>; canManage: boolean }) {
  const qc = useQueryClient()
  const [kind, setKind] = useState<'country' | 'asn'>('country')
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const upload = async () => {
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      const fd = new FormData()
      fd.append('kind', kind)
      fd.append('file', file)
      await api.post('/netflow/settings/enrichment/geoip', fd, { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 300_000 })
      toast.success(`${kind === 'country' ? 'Country' : 'ASN'} database installed`)
      setFile(null)
      if (fileRef.current) fileRef.current.value = ''
      await qc.invalidateQueries({ queryKey: nfKeys.query('/netflow/settings/enrichment') })
    } catch (e) {
      setError(e)
    } finally {
      setUploading(false)
    }
  }
  const tz = displayTimeZone()
  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionTitle icon={Globe2} hint="Country and autonomous system of public addresses, read from local databases (no lookups leave the appliance).">
          GeoIP
        </SectionTitle>
      </CardHeader>
      <CardContent>
        <QueryPanel query={status} label="GeoIP databases" skeleton="tiles" isEmpty={() => false}>
          {(r) => {
            const g = r.data.geoip
            return (
              <div className="space-y-3">
                <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2">
                  {g.databases.map((d) => (
                    <StatusTile
                      key={d.kind}
                      tone={geoipTone(d)}
                      word={!d.present ? 'Missing' : d.valid === false ? 'Unreadable' : d.stale ? 'Outdated' : 'Current'}
                      label={d.kind === 'country' ? 'Country database' : 'ASN database'}
                      value={d.present ? (d.build_date ? formatDateTime(d.build_date, tz, { seconds: false, zone: 'none' }).slice(0, 10) : 'Unknown date') : 'Not installed'}
                      caption={
                        d.present ? (
                          <>
                            {d.database_type ?? 'Unknown type'} · {fileSize(d.size)}
                            {d.age_days != null && ` · built ${d.age_days} days ago`}
                            {d.sample && d.sample.value != null && (
                              <span className="block">
                                Check: {d.sample.ip} → {String(d.sample.value)}
                              </span>
                            )}
                            {d.error && <span className="block text-danger-fg">{d.error}</span>}
                          </>
                        ) : (
                          'Countries and ASNs show as unknown'
                        )
                      }
                    />
                  ))}
                </div>
                <p className="text-[11px] text-text2">{g.refresh}</p>
                <p className="text-[11px] text-text2">{g.attribution}</p>
                {canManage && (
                  <form
                    className="space-y-2 rounded-md border border-border p-3"
                    onSubmit={(e) => {
                      e.preventDefault()
                      void upload()
                    }}
                  >
                    <div className="text-xs font-medium text-text">Install a database file</div>
                    <p className="text-[11px] text-text2">For appliances without internet access: a MaxMind DB (.mmdb), gzip'ed, or a .tar.gz containing one (DB-IP Lite or MaxMind GeoLite2/GeoIP2).</p>
                    {!g.writable && <Note tone="warning">The GeoIP directory is not writable by the API service; uploads will fail.</Note>}
                    <div className="flex flex-wrap items-end gap-2">
                      <Field label="Database" htmlFor="geo-kind">
                        <select id="geo-kind" className={cn(selectClass, 'w-32')} value={kind} onChange={(e) => setKind(e.target.value as 'country' | 'asn')}>
                          <option value="country">Country</option>
                          <option value="asn">ASN</option>
                        </select>
                      </Field>
                      <Field label="File" htmlFor="geo-file" error={responseFieldErrors(error).file} className="min-w-[12rem] flex-1">
                        <input
                          id="geo-file"
                          ref={fileRef}
                          type="file"
                          accept=".mmdb,.gz,.tgz,application/gzip,application/octet-stream"
                          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                          className="block w-full text-xs text-text file:mr-2 file:rounded-md file:border file:border-border file:bg-surface file:px-2 file:py-1 file:text-xs file:text-text"
                        />
                      </Field>
                      <Button type="submit" size="sm" disabled={!file || uploading}>
                        {uploading ? <Loader2 className="animate-spin" aria-hidden /> : <Upload aria-hidden />}
                        {uploading ? 'Checking…' : 'Install'}
                      </Button>
                    </div>
                    {error && !responseFieldErrors(error).file ? <MutationError error={error} /> : null}
                  </form>
                )}
              </div>
            )
          }}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}

function VisibilityCard({ window: w, filter }: { window: NfWindow; filter: FlowFilter }) {
  const q = useNfQuery<VisibilityNotes>('/netflow/settings/visibility', nfParams(w, filter), { window: w })
  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionTitle icon={EyeOff} hint="Measured in the selected window (at most 7 days), for the exporters you can access.">
          What flow data cannot see
        </SectionTitle>
      </CardHeader>
      <CardContent>
        <QueryPanel
          query={q}
          label="visibility limits"
          skeleton="tiles"
          isEmpty={() => false}
          coverage={q.data?.data.totals.records === 0 ? 'none' : null}
        >
          {(r) => {
            const d = r.data
            const t = d.totals
            const natExporters = d.exporters.filter((e) => e.nat_field_records > 0).length
            const appExporters = d.exporters.filter((e) => e.app_id_records > 0).length
            return (
              <div className="space-y-3">
                <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-4">
                  <StatusTile
                    tone={t.tunnel_share != null && t.tunnel_share > 0.2 ? 'warning' : 'info'}
                    word={t.tunnel_share != null && t.tunnel_share > 0.2 ? 'Large share' : 'Info'}
                    label="Tunnelled traffic"
                    value={formatPercent(t.tunnel_share == null ? null : t.tunnel_share * 100)}
                    caption={`${formatBytes(t.tunnel_bytes)} of ${formatBytes(t.bytes)}; contents not visible`}
                  />
                  <StatusTile
                    tone="neutral"
                    label="Exporters sending NAT fields"
                    value={`${natExporters} of ${d.exporters.length}`}
                    caption={`${formatPercent(t.nat_share == null ? null : t.nat_share * 100)} of records carry post-NAT addresses`}
                  />
                  <StatusTile
                    tone="neutral"
                    label="Exporters sending application ids"
                    value={`${appExporters} of ${d.exporters.length}`}
                    caption={`${formatPercent(t.app_id_share == null ? null : t.app_id_share * 100)} of records`}
                  />
                  <StatusTile
                    tone={t.legacy_share != null && t.legacy_share > 0 ? 'info' : 'neutral'}
                    word={t.legacy_share != null && t.legacy_share > 0 ? 'Legacy' : null}
                    label="Records without field tracking"
                    value={formatPercent(t.legacy_share == null ? null : t.legacy_share * 100)}
                    caption="Stored before the collector upgrade: which fields were exported is unknown"
                  />
                </div>
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
                  <Note>
                    <div className="font-medium">Tunnels</div>
                    <div>{d.notes.tunnels}</div>
                    {d.tunnels.length > 0 && (
                      <ul className="mt-1 space-y-0.5">
                        {d.tunnels.slice(0, 5).map((x, i) => (
                          <li key={i} className="flex justify-between gap-2">
                            <span>{x.service ?? x.protocol_name}</span>
                            <span className="tabular-nums text-text2">{formatBytes(x.bytes)}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </Note>
                  <Note>
                    <div className="font-medium">NAT</div>
                    <div>{d.notes.nat}</div>
                  </Note>
                  <Note>
                    <div className="font-medium">Application ids</div>
                    <div>{d.notes.app_id}</div>
                  </Note>
                </div>
                {d.exporters.length > 0 && (
                  <details className="text-xs">
                    <summary className="cursor-pointer font-medium text-text">Per exporter ({d.exporters.length})</summary>
                    <ul className="mt-2 divide-y divide-border md:hidden" aria-label="Visibility limits per exporter">
                      {d.exporters.map((e) => (
                        <li key={e.exporter} className="py-2">
                          <div className="flex items-baseline justify-between gap-2">
                            <span className="break-all font-mono text-text">{e.exporter}</span>
                            <span className="shrink-0 tabular-nums text-text2">{formatCount(e.records)} records</span>
                          </div>
                          <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
                            {(
                              [
                                ['Tunnelled', shareOf(e.tunnel_bytes, e.bytes)],
                                ['NAT fields', shareOf(e.nat_field_records, e.records)],
                                ['App ids', shareOf(e.app_id_records, e.records)],
                                ['Legacy', shareOf(e.legacy_records, e.records)],
                              ] as const
                            ).map(([label, v]) => (
                              <div key={label} className="flex justify-between gap-2">
                                <dt className="text-text2">{label}</dt>
                                <dd className="tabular-nums text-text">{formatPercent(v)}</dd>
                              </div>
                            ))}
                          </dl>
                        </li>
                      ))}
                    </ul>
                    <div className="mt-2 hidden overflow-x-auto md:block">
                      <table className="w-full text-xs">
                        <caption className="sr-only">Visibility limits per exporter</caption>
                        <thead>
                          <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-text2">
                            <th scope="col" className="px-2 py-1.5 font-medium">Exporter</th>
                            <th scope="col" className="px-2 py-1.5 text-right font-medium">Records</th>
                            <th scope="col" className="px-2 py-1.5 text-right font-medium">Tunnelled</th>
                            <th scope="col" className="px-2 py-1.5 text-right font-medium">NAT fields</th>
                            <th scope="col" className="px-2 py-1.5 text-right font-medium">App ids</th>
                            <th scope="col" className="px-2 py-1.5 text-right font-medium">Legacy</th>
                          </tr>
                        </thead>
                        <tbody>
                          {d.exporters.map((e) => (
                            <tr key={e.exporter} className="border-b border-border last:border-0">
                              <td className="px-2 py-1.5 font-mono">{e.exporter}</td>
                              <td className="px-2 py-1.5 text-right tabular-nums">{formatCount(e.records)}</td>
                              <td className="px-2 py-1.5 text-right tabular-nums">{formatPercent(shareOf(e.tunnel_bytes, e.bytes))}</td>
                              <td className="px-2 py-1.5 text-right tabular-nums">{formatPercent(shareOf(e.nat_field_records, e.records))}</td>
                              <td className="px-2 py-1.5 text-right tabular-nums">{formatPercent(shareOf(e.app_id_records, e.records))}</td>
                              <td className="px-2 py-1.5 text-right tabular-nums">{formatPercent(shareOf(e.legacy_records, e.records))}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </details>
                )}
                <DataQualityBar meta={r.meta} window={w} filter={filter} />
              </div>
            )
          }}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}

function RetentionCard() {
  const retention = useNfQuery<NfRetention>('/netflow/retention', null, {
    queryKey: nfKeys.retention(),
    refetchInterval: false,
    staleTime: 5 * 60_000,
  })
  const tz = displayTimeZone()
  const [open, setOpen] = useState(false)
  return (
    <Card>
      <CardHeader className="pb-3">
        <button type="button" className="flex w-full items-center justify-between gap-2 text-left" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <SectionTitle icon={Database} hint="How long each level of detail is kept. Changed in Settings → Storage.">
            Data retention
          </SectionTitle>
          <ChevronDown className={cn('h-4 w-4 shrink-0 text-text2 transition-transform', open && 'rotate-180')} aria-hidden />
        </button>
      </CardHeader>
      {open && (
        <CardContent>
          <QueryPanel query={retention} label="data retention" skeleton="rows" isEmpty={() => false}>
            {(r) => (
              <dl className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-3">
                <RetentionItem label="Raw flow records" days={r.data.raw_days} earliest={r.data.earliest_raw} tz={tz} note="Every field of every record: forensics, exports, host interfaces." />
                <RetentionItem label="Hourly rollups" days={r.data.rollup_days} earliest={r.data.earliest_rollup} tz={tz} note="Conversation and QoS totals per hour for long windows and host first/last seen." />
                <RetentionItem label="Interface rollups" days={r.data.interface_days} earliest={r.data.earliest_interface} tz={tz} note="Per-interface, per-direction 5-minute volumes for capacity." />
              </dl>
            )}
          </QueryPanel>
        </CardContent>
      )}
    </Card>
  )
}

function RetentionItem({ label, days, earliest, tz, note }: { label: string; days: number | null; earliest: string | null; tz: string; note: string }) {
  return (
    <div className="rounded-md border border-border p-3">
      <dt className="text-text2">{label}</dt>
      <dd className="mt-1 text-lg font-semibold tabular-nums text-text">{days != null ? `${days} days` : 'Unknown'}</dd>
      <dd className="text-[11px] text-text2">{earliest ? `Oldest data ${formatDateTime(earliest, tz, { seconds: false })}` : 'No data stored yet'}</dd>
      <dd className="mt-1 text-[11px] text-text2">{note}</dd>
    </div>
  )
}
