import {
  Boxes, Camera, Cpu, HelpCircle, Laptop, MonitorSmartphone, Network, Phone,
  Printer, Router, Server, Wifi,
} from 'lucide-react'
import { Badge } from '@/components/ui/Badge'
import type { EndpointType, TypeSource } from './types'

type TypeMeta = { label: string; icon: React.ComponentType<{ className?: string }> }

export const ENDPOINT_TYPE_META: Record<string, TypeMeta> = {
  workstation: { label: 'Workstation', icon: Laptop },
  server: { label: 'Server', icon: Server },
  phone: { label: 'IP Phone', icon: Phone },
  printer: { label: 'Printer', icon: Printer },
  access_point: { label: 'Access Point', icon: Wifi },
  camera: { label: 'Camera', icon: Camera },
  virtual: { label: 'Virtual', icon: Cpu },
  network: { label: 'Network', icon: Router },
  iot: { label: 'IoT', icon: MonitorSmartphone },
  unknown: { label: 'Unknown', icon: HelpCircle },
}

// Custom groups get a prettified slug label and a generic icon.
export function endpointTypeMeta(type: string | null | undefined): TypeMeta {
  if (type && ENDPOINT_TYPE_META[type]) return ENDPOINT_TYPE_META[type]
  if (!type) return ENDPOINT_TYPE_META.unknown
  const label = type.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  return { label, icon: Boxes }
}

export function EndpointTypeIcon({ type, className }: { type: EndpointType; className?: string }) {
  const Icon = endpointTypeMeta(type).icon
  return <Icon className={className} />
}

export function TypeSourceBadge({ source }: { source?: TypeSource }) {
  if (source === 'manual') return <Badge variant="outline" title="Type pinned by an operator">manual</Badge>
  if (source === 'rule') return <Badge variant="outline" title="Type set by a classification rule">rule</Badge>
  return null
}

export function AuthBadge({ authorized, watched, randomized }: { authorized: boolean | null; watched?: boolean; randomized?: boolean }) {
  if (authorized === false) return <Badge variant="danger">Rogue</Badge>
  if (watched) return <Badge variant="warning">Watched</Badge>
  if (authorized === true) return <Badge variant="success">Allowed</Badge>
  if (randomized) return <Badge variant="info">Random MAC</Badge>
  return <Badge variant="outline">Unclassified</Badge>
}

export function OnlineDot({ online }: { online?: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${online ? 'bg-success' : 'bg-muted/40'}`}
      title={online ? 'Active (seen in last 15 min)' : 'Inactive'}
    />
  )
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function durationSince(iso: string | null | undefined): string {
  if (!iso) return '—'
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

export function durationBetween(a: string | null | undefined, b: string | null | undefined): string {
  if (!a) return '—'
  const end = b ? new Date(b).getTime() : Date.now()
  const s = Math.max(0, Math.floor((end - new Date(a).getTime()) / 1000))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

const EVENT_META: Record<string, { label: string; variant: 'default' | 'success' | 'warning' | 'danger' | 'info' | 'outline' }> = {
  new_endpoint: { label: 'New endpoint', variant: 'info' },
  endpoint_moved: { label: 'Moved', variant: 'warning' },
  rogue_detected: { label: 'Rogue', variant: 'danger' },
  watch_seen: { label: 'Watch seen', variant: 'warning' },
  ip_changed: { label: 'IP changed', variant: 'default' },
  user_login: { label: 'User login', variant: 'success' },
  port_admin: { label: 'Port action', variant: 'default' },
}

export function EventBadge({ type }: { type: string }) {
  const m = EVENT_META[type] || { label: type, variant: 'outline' as const }
  return <Badge variant={m.variant}>{m.label}</Badge>
}

export function macCol(mac: string) {
  return <span className="font-mono text-xs tabular-nums">{mac}</span>
}

export function portLabel(name: string | null | undefined, ifIndex: number | null | undefined): string {
  if (name) return name
  if (ifIndex != null) return `if ${ifIndex}`
  return '—'
}

export function speedLabel(bps: number | null | undefined): string {
  if (!bps) return '—'
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(bps % 1e9 === 0 ? 0 : 1)}G`
  if (bps >= 1e6) return `${Math.round(bps / 1e6)}M`
  return `${Math.round(bps / 1e3)}k`
}
