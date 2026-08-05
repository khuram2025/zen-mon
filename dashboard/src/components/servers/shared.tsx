/** Small shared pieces for the Servers module: status/OS badges, usage bars, tag pills. */

import { AppWindow, Apple, Bot, CircleHelp, MonitorCog, Server, Terminal } from 'lucide-react'
import { Badge } from '@/components/ui/Badge'
import { cn } from '@/lib/utils'
import type { AgentStatus, OsType, ServerStatus } from '@/types/servers'

export const SERVER_STATUS_META: Record<ServerStatus, { label: string; variant: 'success' | 'warning' | 'danger' | 'info' | 'default' | 'outline'; dot: string }> = {
  healthy: { label: 'Healthy', variant: 'success', dot: 'bg-success' },
  warning: { label: 'Warning', variant: 'warning', dot: 'bg-warning' },
  critical: { label: 'Critical', variant: 'danger', dot: 'bg-danger' },
  stale: { label: 'Stale', variant: 'default', dot: 'bg-muted' },
  unknown: { label: 'Unknown', variant: 'outline', dot: 'bg-muted/60' },
  disabled: { label: 'Disabled', variant: 'outline', dot: 'bg-muted/40' },
}

export function ServerStatusBadge({ status, reasons }: { status: ServerStatus; reasons?: string[] }) {
  const meta = SERVER_STATUS_META[status] || SERVER_STATUS_META.unknown
  return (
    <Badge
      variant={meta.variant}
      title={reasons && reasons.length ? reasons.join('\n') : undefined}
      className="cursor-default"
    >
      <span className={cn('h-1.5 w-1.5 rounded-full', meta.dot)} />
      {meta.label}
    </Badge>
  )
}

export const AGENT_STATUS_META: Record<AgentStatus, { label: string; variant: 'success' | 'warning' | 'danger' | 'info' | 'default' | 'outline' }> = {
  online: { label: 'Online', variant: 'success' },
  stale: { label: 'Stale', variant: 'warning' },
  offline: { label: 'Offline', variant: 'danger' },
  enrolling: { label: 'Enrolling', variant: 'info' },
  updating: { label: 'Updating', variant: 'info' },
  error: { label: 'Error', variant: 'danger' },
  disabled: { label: 'Disabled', variant: 'outline' },
}

export function AgentStatusBadge({ status }: { status: AgentStatus | null }) {
  if (!status) {
    return <Badge variant="outline"><Bot className="h-3 w-3" /> No agent</Badge>
  }
  const meta = AGENT_STATUS_META[status] || AGENT_STATUS_META.error
  return <Badge variant={meta.variant}><Bot className="h-3 w-3" /> {meta.label}</Badge>
}

const AUTHORIZATION_META: Record<'pending' | 'authorized' | 'revoked', { label: string; variant: 'success' | 'warning' | 'danger' }> = {
  pending: { label: 'Pending', variant: 'warning' },
  authorized: { label: 'Authorized', variant: 'success' },
  revoked: { label: 'Revoked', variant: 'danger' },
}

const AUTH_SOURCE_LABEL: Record<string, string> = {
  legacy: 'legacy key',
  enrollment_token: 'enrollment token',
  admin: 'admin',
  pending: 'self-registered',
}

export function AuthorizationBadge({ state, source }: { state: 'pending' | 'authorized' | 'revoked'; source?: string | null }) {
  const meta = AUTHORIZATION_META[state] || AUTHORIZATION_META.pending
  const sourceLabel = source ? (AUTH_SOURCE_LABEL[source] || source) : undefined
  return (
    <Badge variant={meta.variant} title={sourceLabel ? `via ${sourceLabel}` : undefined}>
      {meta.label}
    </Badge>
  )
}

export function OsIcon({ os, className }: { os: OsType | string | null; className?: string }) {
  const cls = cn('h-4 w-4 shrink-0', className)
  switch (os) {
    case 'windows': return <AppWindow className={cn(cls, 'text-info')} />
    case 'linux': return <Terminal className={cn(cls, 'text-warning')} />
    case 'macos': return <Apple className={cls} />
    case 'bsd': return <MonitorCog className={cls} />
    case 'other': return <Server className={cls} />
    default: return <CircleHelp className={cn(cls, 'text-muted')} />
  }
}

/** Compact horizontal usage bar with threshold coloring (85 warn / 95 crit). */
export function UsageBar({ pct, warn = 85, crit = 95, className }: { pct: number | null | undefined; warn?: number; crit?: number; className?: string }) {
  if (pct == null || isNaN(pct)) return <span className="text-xs text-muted">—</span>
  const clamped = Math.max(0, Math.min(100, pct))
  const tone = clamped >= crit ? 'bg-danger' : clamped >= warn ? 'bg-warning' : 'bg-primary'
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-surface2">
        <div className={cn('h-full rounded-full transition-all', tone)} style={{ width: `${clamped}%` }} />
      </div>
      <span className={cn('text-xs tabular-nums', clamped >= crit ? 'text-danger' : clamped >= warn ? 'text-warning' : 'text-text2')}>
        {clamped.toFixed(clamped >= 10 ? 0 : 1)}%
      </span>
    </div>
  )
}

export function TagPill({ tag, onClick, active }: { tag: string; onClick?: (tag: string) => void; active?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick ? (e) => { e.stopPropagation(); onClick(tag) } : undefined}
      className={cn(
        'rounded-full border px-1.5 py-0.5 text-[10px] font-medium transition-colors',
        active
          ? 'border-primary/50 bg-primary/15 text-primary'
          : 'border-border bg-surface2 text-text2',
        onClick && 'hover:border-primary/40 hover:text-primary cursor-pointer',
      )}
    >
      {tag}
    </button>
  )
}

export function TagList({ tags, max = 3, onTagClick, activeTag }: { tags: string[]; max?: number; onTagClick?: (tag: string) => void; activeTag?: string | null }) {
  if (!tags || tags.length === 0) return <span className="text-xs text-muted">—</span>
  const shown = tags.slice(0, max)
  const extra = tags.length - shown.length
  return (
    <div className="flex flex-wrap items-center gap-1">
      {shown.map((t) => (
        <TagPill key={t} tag={t} onClick={onTagClick} active={activeTag === t} />
      ))}
      {extra > 0 && <span className="text-[10px] text-muted">+{extra}</span>}
    </div>
  )
}

/** KPI tile used across the module's headers. */
export function KpiTile({ icon: Icon, label, value, sub, tone }: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  tone?: 'success' | 'warning' | 'danger' | 'info' | 'default'
}) {
  const tones: Record<string, string> = {
    success: 'bg-success/10 text-success',
    warning: 'bg-warning/10 text-warning',
    danger: 'bg-danger/10 text-danger',
    info: 'bg-info/10 text-info',
    default: 'bg-primary/10 text-primary',
  }
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-surface p-3">
      <span className={cn('flex h-9 w-9 shrink-0 items-center justify-center rounded-md', tones[tone || 'default'])}>
        <Icon className="h-[18px] w-[18px]" />
      </span>
      <div className="min-w-0">
        <div className="truncate text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div>
        <div className="truncate text-lg font-semibold leading-tight">{value}</div>
        {sub && <div className="truncate text-[11px] text-muted">{sub}</div>}
      </div>
    </div>
  )
}
