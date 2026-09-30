/**
 * Alert Center building blocks: the server-side summary strip (A11, A14,
 * A15), the data-confidence line (A13, A26) and saved views (F10).
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  AlertCircle, Bookmark, BookmarkPlus, Clock, Copy, Hourglass, Loader2, RefreshCw, Timer, Trash2,
  TriangleAlert, UserX, BellOff, Siren, MailX,
} from 'lucide-react'
import { copyText, cn } from '@/lib/utils'
import { describeSkew, useClockOffset } from '@/lib/serverClock'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { focusRing } from './AlertBits'
import { ageOf, fmtAlertDate, fmtClock, fmtDur, fmtMinutes } from './format'
import type { AlertSummary, DurationStats } from './types'

/* ── Summary strip ────────────────────────────────────────────────────── */

type Tone = 'danger' | 'warning' | 'info' | 'muted' | 'neutral'
const TONE_TEXT: Record<Tone, string> = {
  danger: 'text-danger', warning: 'text-warning', info: 'text-primary', muted: 'text-text2', neutral: 'text-text',
}
const TONE_ICON: Record<Tone, string> = {
  danger: 'bg-danger/10 text-danger', warning: 'bg-warning/10 text-warning', info: 'bg-primary/10 text-primary',
  muted: 'bg-surface2 text-muted', neutral: 'bg-surface2 text-text2',
}

function Tile({
  label, value, sub, icon: Icon, tone, loading, unavailable, dim, onClick, ariaLabel,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  icon: typeof AlertCircle
  tone: Tone
  loading: boolean
  unavailable: boolean
  dim: boolean
  onClick?: () => void
  ariaLabel?: string
}) {
  const body = (
    <>
      <span className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-md', unavailable ? 'bg-surface2 text-muted' : TONE_ICON[tone])}>
        <Icon className="h-4 w-4" aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-xs font-medium text-muted">{label}</span>
        {loading ? (
          <Skeleton className="mt-1 h-6 w-16" />
        ) : unavailable ? (
          <span className="block text-base font-semibold text-muted">Unavailable</span>
        ) : (
          <span className={cn('block truncate text-xl font-semibold tabular-nums', TONE_TEXT[tone], dim && 'opacity-50')}>{value}</span>
        )}
        {sub && !loading && !unavailable && <span className={cn('line-clamp-2 block text-xs text-muted', dim && 'opacity-60')} title={typeof sub === 'string' ? sub : undefined}>{sub}</span>}
      </span>
    </>
  )
  const cls = 'flex min-w-0 items-start gap-2.5 rounded-lg border border-border bg-surface p-3 text-left'
  if (onClick && !loading && !unavailable) {
    return (
      <button onClick={onClick} aria-label={ariaLabel} className={cn(cls, 'transition-colors hover:border-primary/40 hover:bg-surface2/40', focusRing)}>
        {body}
      </button>
    )
  }
  return <div className={cls}>{body}</div>
}

function pct(d: DurationStats | undefined): { value: string; sub: string } {
  if (!d || d.count === 0 || d.p50_s == null) return { value: '—', sub: 'No closed samples in window' }
  return {
    value: `${fmtDur(d.p50_s)} p50`,
    sub: `p95 ${fmtDur(d.p95_s)} · n=${d.count.toLocaleString()}${d.mean_s != null ? ` · mean ${fmtDur(d.mean_s)}` : ''}`,
  }
}

export function SummaryStrip({
  summary, loading, error, onRetry, scopeLabel, windowLabel, now, onDrill,
}: {
  summary: AlertSummary | undefined
  loading: boolean
  error: boolean
  onRetry: () => void
  scopeLabel: string
  windowLabel: string
  now: number
  onDrill: (patch: Record<string, string | null>) => void
}) {
  const unavailable = error && !summary
  const dim = error && !!summary
  const o = summary?.open
  const w = summary?.window
  const t = summary?.response_targets
  const oldest = ageOf(o?.oldest_triggered_at, now)
  const targets = t
    ? (['critical', 'warning', 'info'] as const).filter((k) => t[k] != null).map((k) => `${k} ${fmtMinutes(t[k])}`).join(' · ')
    : ''
  const recover = pct(w?.time_to_recover)
  const ack = pct(w?.time_to_ack)
  const common = { loading: loading && !summary, unavailable, dim }
  const other = w ? w.by_reason.manual + w.by_reason.suppressed + w.by_reason.superseded + (w.by_reason.administrative ?? 0) : 0
  const dlv = w?.delivery
  const openFailed = summary?.diagnostics.open_delivery_failed ?? 0

  return (
    <section aria-label="Alert summary" className="space-y-2">
      {error && (
        <div role="status" className={cn('flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm', summary ? 'border-warning/40 bg-warning/10 text-warning' : 'border-danger/40 bg-danger/10 text-danger')}>
          <TriangleAlert className="h-4 w-4" aria-hidden />
          {summary
            ? `Summary refresh failed — values below are from ${fmtAlertDate(summary.server_time, { seconds: true })} and may be out of date.`
            : 'Summary unavailable — counts are unknown (not zero).'}
          <Button size="sm" variant="outline" className={cn('ml-auto h-7', focusRing)} onClick={onRetry}>
            <RefreshCw className="h-3.5 w-3.5" /> Retry
          </Button>
        </div>
      )}
      <div className="grid gap-3 xl:grid-cols-[minmax(0,5fr)_minmax(0,3fr)]">
        <div className="min-w-0">
          <h2 className="mb-1.5 text-sm font-semibold">
            Open now <span className="font-normal text-muted">· {scopeLabel}</span>
          </h2>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
            <Tile
              {...common}
              label="Needs attention"
              icon={Siren}
              tone={o && o.active > 0 ? 'danger' : 'neutral'}
              value={o?.active.toLocaleString()}
              sub={o ? `Active, not suppressed · ${o.pending.toLocaleString()} pending` : undefined}
              onClick={() => onDrill({ view: null })}
              ariaLabel={o ? `${o.active} active alerts need attention — show them` : undefined}
            />
            <Tile
              {...common}
              label="Unassigned critical"
              icon={UserX}
              tone={o && o.unassigned_critical > 0 ? 'danger' : 'neutral'}
              value={o?.unassigned_critical.toLocaleString()}
              sub={o ? `of ${o.by_severity.critical.toLocaleString()} open critical` : undefined}
              onClick={() => onDrill({ view: 'open', severity: 'critical', assignee: 'unassigned' })}
              ariaLabel={o ? `${o.unassigned_critical} unassigned critical — show them` : undefined}
            />
            <Tile
              {...common}
              label="Ack overdue"
              icon={Timer}
              tone={o && o.ack_overdue > 0 ? 'danger' : 'neutral'}
              value={o?.ack_overdue.toLocaleString()}
              sub={targets ? `Targets: ${targets}` : 'No response targets set'}
            />
            <Tile
              {...common}
              label="Suppressed"
              icon={BellOff}
              tone="muted"
              value={o?.suppressed.toLocaleString()}
              sub="Muted — condition still active"
              onClick={() => onDrill({ view: 'suppressed' })}
              ariaLabel={o ? `${o.suppressed} suppressed — show them` : undefined}
            />
            <Tile
              {...common}
              label="Oldest open"
              icon={Hourglass}
              tone={oldest.seconds != null && oldest.seconds > 7 * 86400 ? 'warning' : 'neutral'}
              value={o?.oldest_triggered_at ? (oldest.future ? 'Future time' : fmtDur(oldest.seconds)) : '—'}
              sub={o?.oldest_triggered_at ? `since ${fmtAlertDate(o.oldest_triggered_at)}` : 'No open incidents'}
              onClick={o?.oldest_triggered_at ? () => onDrill({ view: 'open', sort: 'oldest' }) : undefined}
              ariaLabel="Show open incidents, oldest first"
            />
          </div>
        </div>
        <div className="min-w-0">
          <h2 className="mb-1.5 text-sm font-semibold">
            Closed incidents <span className="font-normal text-muted">· {windowLabel}{w ? ` (${fmtAlertDate(w.from)} → ${fmtAlertDate(w.to)})` : ''}</span>
          </h2>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-2">
            <Tile
              {...common}
              label="Closed"
              icon={Clock}
              tone="neutral"
              value={w?.closed.toLocaleString()}
              sub={w ? `${w.by_reason.recovered.toLocaleString()} recovered · ${other.toLocaleString()} other · ${w.notices.toLocaleString()} notices excluded` : undefined}
            />
            <Tile {...common} label="Time to recover" icon={Timer} tone="neutral" value={recover.value} sub={recover.sub} />
            <Tile {...common} label="Time to acknowledge" icon={Timer} tone="neutral" value={ack.value} sub={ack.sub} />
            <Tile
              {...common}
              label="Notifications failed"
              icon={MailX}
              tone={dlv && dlv.failed > 0 ? 'warning' : 'neutral'}
              value={dlv ? dlv.failed.toLocaleString() : '—'}
              sub={dlv ? `of ${dlv.attempts.toLocaleString()} attempts · ${openFailed.toLocaleString()} open incident${openFailed === 1 ? '' : 's'} affected` : undefined}
              onClick={openFailed > 0 ? () => onDrill({ view: 'open', delivery: 'failed', page: null }) : undefined}
              ariaLabel="Show open incidents with a failed notification"
            />
          </div>
        </div>
      </div>
    </section>
  )
}

/* ── Data confidence ──────────────────────────────────────────────────── */

export function DataConfidence({
  updatedAt, fetching, stale, onRefresh, serverTime,
}: {
  /** Browser epoch ms of the last successful list fetch (0 = never). */
  updatedAt: number
  fetching: boolean
  /** A background refetch failed while cached data is shown. */
  stale: boolean
  onRefresh: () => void
  serverTime: string | null
}) {
  const { offsetMs, skewed } = useClockOffset()
  const applianceUpdated = updatedAt ? updatedAt + offsetMs : null
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
        <span>
          {applianceUpdated
            ? <>Last refreshed <span className="text-text2">{fmtClock(applianceUpdated)}</span> (appliance time) · auto-refresh every 15 s</>
            : 'Not loaded yet'}
        </span>
        {serverTime && <span className="hidden sm:inline">Appliance clock {fmtAlertDate(serverTime, { seconds: true })}</span>}
        <Button size="sm" variant="ghost" className={cn('h-7 px-2', focusRing)} onClick={onRefresh} disabled={fetching} aria-label="Refresh alerts now">
          {fetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          Refresh
        </Button>
      </div>
      {stale && (
        <div role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-1.5 text-sm text-warning">
          <TriangleAlert className="h-4 w-4" aria-hidden />
          Latest refresh failed — the table shows data from {applianceUpdated ? fmtClock(applianceUpdated) : 'an earlier load'} and may be out of date.
          <button onClick={onRefresh} className={cn('font-medium underline', focusRing)}>Retry</button>
        </div>
      )}
      {skewed && (
        <div role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-1.5 text-sm text-warning">
          <Clock className="h-4 w-4" aria-hidden />
          This browser's clock is {describeSkew(offsetMs)} the appliance. Ages and time windows use the appliance clock;
          if the appliance itself is not NTP-synchronised, recorded times may also be off.
        </div>
      )}
    </div>
  )
}

/* ── Saved views ──────────────────────────────────────────────────────── */

const SAVED_KEY = 'zenplus.alerts.savedViews'
type SavedView = { id: string; name: string; query: string; created: number }

function loadViews(): SavedView[] {
  try {
    const raw = window.localStorage.getItem(SAVED_KEY)
    const v = raw ? JSON.parse(raw) : []
    return Array.isArray(v) ? v.filter((x) => x && typeof x.name === 'string' && typeof x.query === 'string') : []
  } catch {
    return []
  }
}

function storeViews(v: SavedView[]): boolean {
  try {
    window.localStorage.setItem(SAVED_KEY, JSON.stringify(v))
    return true
  } catch {
    return false
  }
}

export function SavedViewsMenu({ currentQuery, onApply }: { currentQuery: string; onApply: (query: string) => void }) {
  const [views, setViews] = useState<SavedView[]>(() => loadViews())
  const [saveOpen, setSaveOpen] = useState(false)
  const [manageOpen, setManageOpen] = useState(false)
  const [name, setName] = useState('')
  useEffect(() => { if (saveOpen) setName('') }, [saveOpen])

  const save = () => {
    const n = name.trim()
    if (!n) return
    const next = [...views.filter((v) => v.name !== n), { id: `${Date.now()}`, name: n, query: currentQuery, created: Date.now() }]
    if (storeViews(next)) {
      setViews(next)
      toast.success(`Saved view “${n}”`)
    } else {
      toast.error('Could not save view', 'Browser storage is unavailable (private window or blocked site data).')
    }
    setSaveOpen(false)
  }
  const remove = (id: string) => {
    const next = views.filter((v) => v.id !== id)
    storeViews(next)
    setViews(next)
  }
  const copyLink = async () => {
    const ok = await copyText(window.location.href)
    if (ok) toast.success('Link copied', 'Anyone with access sees the same filters.')
    else toast.error('Could not copy link')
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" className={focusRing} aria-label="Saved views and sharing">
            <Bookmark className="h-3.5 w-3.5" /> Views
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent className="w-64">
          <DropdownMenuLabel>Saved views (this browser)</DropdownMenuLabel>
          {views.length === 0 && <div className="px-2.5 py-1.5 text-sm text-muted">No saved views yet</div>}
          {views.map((v) => (
            <DropdownMenuItem key={v.id} onSelect={() => onApply(v.query)}>
              <Bookmark /> <span className="truncate">{v.name}</span>
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => setSaveOpen(true)}><BookmarkPlus /> Save current view…</DropdownMenuItem>
          {views.length > 0 && <DropdownMenuItem onSelect={() => setManageOpen(true)}><Trash2 /> Manage saved views…</DropdownMenuItem>}
          <DropdownMenuItem onSelect={() => { void copyLink() }}><Copy /> Copy link to this view</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent className="w-[calc(100vw-2rem)] max-w-sm">
          <DialogHeader>
            <DialogTitle>Save view</DialogTitle>
            <DialogDescription>Stores the current tab, filters, sort and page size in this browser.</DialogDescription>
          </DialogHeader>
          <form onSubmit={(e) => { e.preventDefault(); save() }}>
            <label htmlFor="view-name" className="text-sm font-medium">Name</label>
            <input
              id="view-name"
              autoFocus
              value={name}
              maxLength={60}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Core critical, unassigned"
              className={cn('mt-1 h-9 w-full rounded-md border border-border bg-surface px-3 text-sm', focusRing)}
            />
            <DialogFooter className="mt-4">
              <Button type="button" variant="outline" onClick={() => setSaveOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={!name.trim()}>Save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={manageOpen} onOpenChange={setManageOpen}>
        <DialogContent className="w-[calc(100vw-2rem)] max-w-md">
          <DialogHeader>
            <DialogTitle>Saved views</DialogTitle>
          </DialogHeader>
          <ul className="max-h-80 space-y-1 overflow-y-auto">
            {views.map((v) => (
              <li key={v.id} className="flex items-center gap-2 rounded-md border border-border px-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{v.name}</div>
                  <div className="truncate font-mono text-xs text-muted">{v.query ? `?${v.query}` : 'default view'}</div>
                </div>
                <Button size="sm" variant="ghost" className={focusRing} onClick={() => { onApply(v.query); setManageOpen(false) }}>Open</Button>
                <Button size="sm" variant="ghost" className={focusRing} onClick={() => remove(v.id)} aria-label={`Delete saved view ${v.name}`}>
                  <Trash2 className="h-3.5 w-3.5 text-danger" />
                </Button>
              </li>
            ))}
            {views.length === 0 && <li className="text-sm text-muted">No saved views.</li>}
          </ul>
          <DialogFooter>
            <Button variant="outline" onClick={() => setManageOpen(false)}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
