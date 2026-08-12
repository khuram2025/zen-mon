import { Badge } from '@/components/ui/Badge'
import type { ProbeStatus } from './types'

// Theme-safe status palette used by the SVG graph and timeline (SVG can't read
// tailwind classes, so these are explicit hex that read well in both themes).
export const STATUS_HEX: Record<string, string> = {
  ok: '#22c55e',
  degraded: '#f59e0b',
  down: '#ef4444',
  unreached: '#ef4444',
  pending: '#94a3b8',
}
export const LOSS_HEX = { none: '#22c55e', warn: '#f59e0b', crit: '#ef4444' }
export const INTERNAL_HEX = '#3b82f6' // your network / monitored device
export const ANON_HEX = '#94a3b8'
export const SEV_HEX: Record<string, string> = {
  info: '#3b82f6', warning: '#f59e0b', critical: '#ef4444',
}

const BADGE_VARIANT: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'outline'> = {
  ok: 'success', degraded: 'warning', down: 'danger', unreached: 'danger', pending: 'outline',
}

export function StatusBadge({ status }: { status: ProbeStatus | string | null }) {
  const s = (status || 'pending') as string
  const label = s === 'unreached' ? 'Unreachable' : s.charAt(0).toUpperCase() + s.slice(1)
  return <Badge variant={BADGE_VARIANT[s] || 'outline'}>{label}</Badge>
}

export function lossHex(loss: number | null | undefined, warn: number, crit: number): string {
  const v = loss ?? 0
  if (v >= crit) return LOSS_HEX.crit
  if (v > warn) return LOSS_HEX.warn
  if (v > 0) return LOSS_HEX.warn
  return LOSS_HEX.none
}

export function rttHex(rtt: number | null | undefined, warn: number, crit: number): string {
  const v = rtt ?? 0
  if (v >= crit) return STATUS_HEX.down
  if (v >= warn) return STATUS_HEX.degraded
  return STATUS_HEX.ok
}

export function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v < 1) return `${(v * 1000).toFixed(0)}µs`
  return `${v.toFixed(v < 10 ? 1 : 0)} ms`
}

export function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${v % 1 === 0 ? v.toFixed(0) : v.toFixed(1)}%`
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const d = new Date(iso).getTime()
  const s = Math.floor((Date.now() - d) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function fmtClock(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// Country flag emoji from an ISO-3166 alpha-2 code.
export function flag(cc: string | null | undefined): string {
  if (!cc || cc.length !== 2) return ''
  const base = 0x1f1e6
  return String.fromCodePoint(base + (cc.toUpperCase().charCodeAt(0) - 65)) +
    String.fromCodePoint(base + (cc.toUpperCase().charCodeAt(1) - 65))
}

export function nodeLabel(ip: string, hostname: string | null): string {
  if (hostname && hostname !== ip) {
    return hostname.length > 22 ? hostname.slice(0, 20) + '…' : hostname
  }
  return ip
}

export const PROTO_LABEL: Record<string, string> = { icmp: 'ICMP', tcp: 'TCP SYN', udp: 'UDP' }
