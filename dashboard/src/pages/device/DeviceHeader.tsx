import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  BellRing, Box, ChevronDown, Copy, Database, ExternalLink, Hourglass, Info, Link2, Loader2, MoreHorizontal, Network,
  Pencil, Router as RouterIcon, Save, Server, Shield, Trash2, Wifi, Wrench, Zap,
} from 'lucide-react'
import { DeviceStatusExplanation, useDeviceStatusExplanation } from '@/components/devices/DeviceStatusExplanation'
import { DeviceStatusBadge, HealthRollupChip } from '@/components/devices/DeviceStatusBadge'
import { flapChangesInWindow, flapTooltip, isFlapping, pendingDownChip, statusForText } from '@/components/devices/deviceStatus'
import { FlappingTag } from '@/components/devices/FlappingTag'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { toast } from '@/components/ui/Toast'
import { serverNow } from '@/lib/serverClock'
import { cn, copyText } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { DiagnosticsDialog, useAcknowledgeAll, useConfigBackup, useDiagnostics, type DiagnosticKind } from './DeviceActions'
import { formatAbsolute, formatRelative, formatSpan, MISSING } from './format'
import type { DeviceRecord, DeviceSummary } from './types'

const TYPE_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  router: RouterIcon, switch: Network, firewall: Shield, server: Server, access_point: Wifi,
  storage: Database, hypervisor: Server, other: Box,
}

function titleCase(s: string) {
  return s.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** Sticky device header: identity, the confirmed status beside a separate
 *  health rollup, and one action bar. It compacts once the page scrolls. */
export function DeviceHeader({
  device, summary, onShowIssues, onEdit, onDelete, onMaintenance, uptimeSeconds,
}: {
  device: DeviceRecord
  summary?: DeviceSummary
  onShowIssues: () => void
  onEdit: () => void
  onDelete: () => void
  onMaintenance: () => void
  uptimeSeconds: number | null
}) {
  const can = useCan()
  const sentinel = useRef<HTMLDivElement>(null)
  const [compact, setCompact] = useState(false)
  useEffect(() => {
    const el = sentinel.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(([entry]) => setCompact(!entry.isIntersecting), { threshold: 0 })
    io.observe(el)
    return () => io.disconnect()
  }, [])

  const Icon = TYPE_ICON[device.device_type] || Box
  // The confirmed status, its age, a pending Down and the flap episode. The explanation query is the one the
  // status explanation uses (same key, one request).
  const { data: explanation } = useDeviceStatusExplanation(device.id, !!device.ping_enabled)
  const now = serverNow()
  const statusFor = statusForText(device.status_changed_at, now)
  const inMaintenance = device.status === 'maintenance' || explanation?.state?.in_maintenance === true
  const pendingChip = pendingDownChip(explanation?.state?.pending, inMaintenance ? 'maintenance' : explanation?.state?.hard_status ?? device.status)
  const flapping = isFlapping(device as any) || explanation?.flapping?.active === true
  const flap = explanation?.flapping
  const flapTitle = flapping ? flapTooltip({
    kind: flap?.kind ?? device.flap_kind,
    since: flap?.since ?? device.flapping_since,
    changes: flap ? flapChangesInWindow(flap) : null,
    windowMinutes: flap?.window_minutes ?? null,
    stableMinutes: flap?.stable_minutes ?? null,
    earliestStopAt: flap?.earliest_stop_at ?? null,
  }, now) : ''

  const diagnostics = useDiagnostics(device)
  const [diagOpen, setDiagOpen] = useState(false)
  const runChecks = (kind: DiagnosticKind) => { setDiagOpen(true); diagnostics.run.mutate(kind) }
  const ack = useAcknowledgeAll(device.id)
  const [ackOpen, setAckOpen] = useState(false)
  const backup = useConfigBackup(device.id)
  const activeAlerts = summary?.alerts?.active ?? 0

  const ip = device.ip_address || device.managed_ip || null
  const identity = summary?.identity
  const typeLabel = device.device_type === 'other' && device.profile_name ? device.profile_name : titleCase(device.device_type || 'device')
  const eyebrow = [typeLabel, summary?.template?.name || device.vendor].filter(Boolean).join(' · ')
  const cadence = [
    device.ping_enabled ? `ping every ${formatSpan(device.ping_interval)}` : 'ping off',
    device.snmp_enabled ? `SNMP v${device.snmp_version}${summary?.polling.snmp_interval_s ? ` every ${formatSpan(summary.polling.snmp_interval_s)}` : ''}` : 'SNMP off',
  ].join(' · ')
  const ids: Array<{ label: string; value: React.ReactNode; mono?: boolean }> = [
    { label: 'IP', value: ip ? <span className="inline-flex items-center gap-1">{ip}<button type="button" aria-label="Copy IP address" className="rounded p-0.5 text-muted hover:bg-surface2 hover:text-text" onClick={() => copyText(ip).then(() => toast.success('IP address copied'))}><Copy className="h-3 w-3" /></button></span> : 'Not reported', mono: true },
    { label: 'Model', value: identity?.model.value || device.model || 'Not reported' },
    { label: 'OS', value: identity?.os_version.value || 'Not reported' },
    { label: 'S/N', value: identity?.serial_number.value || 'Not reported', mono: true },
    { label: 'Uptime', value: uptimeSeconds != null ? formatSpan(uptimeSeconds) : MISSING },
    { label: 'Site', value: device.location || 'Not assigned' },
    { label: 'Group', value: device.group_name || 'No group' },
  ]

  return (
    <>
      <div ref={sentinel} aria-hidden="true" className="h-px" />
      <header className={cn('sticky top-0 z-20 -mx-1 rounded-xl border border-border bg-surface/95 px-4 shadow-sm backdrop-blur transition-[padding]', compact ? 'py-2' : 'py-4')}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 flex-1 items-start gap-3">
            <span className={cn('flex shrink-0 items-center justify-center rounded-lg border border-primary/20 bg-primary/10 text-primary', compact ? 'h-8 w-8' : 'h-11 w-11')}><Icon className={compact ? 'h-4 w-4' : 'h-5 w-5'} /></span>
            <div className="min-w-0">
              {!compact && <div className="text-xs font-medium text-muted">{eyebrow}</div>}
              <div className="flex flex-wrap items-center gap-2">
                <h1 className={cn('break-words font-semibold tracking-tight', compact ? 'text-lg' : 'text-2xl')}>{device.hostname}</h1>
                <DeviceStatusBadge status={device.status} since={statusFor ? statusFor.replace(/^for\s+/i, '') : null} />
                {summary && <HealthRollupChip state={summary.health.state} count={summary.health.issue_count} onClick={onShowIssues} />}
                {flapping && <FlappingTag device={{ ...device, flap_kind: flap?.kind ?? device.flap_kind } as any} title={flapTitle} withKind className="px-2 py-0.5 text-xs" />}
                {pendingChip && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-dashed border-danger/40 bg-danger/5 px-2 py-0.5 text-xs font-medium text-danger">
                    <Hourglass className="h-3 w-3" aria-hidden />{pendingChip} · not confirmed yet
                  </span>
                )}
                <Popover>
                  <PopoverTrigger className="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-xs text-muted hover:bg-surface2 hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
                    <Info className="h-3.5 w-3.5" aria-hidden />How status is decided
                  </PopoverTrigger>
                  <PopoverContent className="w-[min(92vw,560px)]">
                    <p className="mb-2 text-xs text-muted">
                      Confirmed status{statusFor ? ` ${statusFor}` : ''}{device.status_changed_at ? ` (since ${formatAbsolute(device.status_changed_at)})` : ''}.
                      It changes only after the confirmation checks set under Settings → Monitoring. Template readings and
                      alerts are counted separately as issues.
                    </p>
                    <DeviceStatusExplanation deviceId={device.id} />
                  </PopoverContent>
                </Popover>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                <span>Last response: <span className="font-medium text-text" title={device.last_seen ? formatAbsolute(device.last_seen, { seconds: true }) : undefined}>{device.last_seen ? formatRelative(device.last_seen) : 'Not reported'}</span></span>
                <span>{cadence}</span>
                {device.managed_by_device_id && <Link to={`/devices/${device.managed_by_device_id}`} className="text-primary hover:underline">Managed by {device.managed_by_hostname || 'controller'}</Link>}
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {can('devices.manage') && (
              <Button size="sm" className="h-9" disabled={diagnostics.run.isPending} onClick={() => runChecks('all')}>
                {diagnostics.run.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}Check now
              </Button>
            )}
            {can('devices.manage') && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button size="sm" variant="outline" className="h-9">Diagnostics<ChevronDown className="h-3.5 w-3.5" /></Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <DropdownMenuItem onSelect={() => runChecks('ping')}>Ping test</DropdownMenuItem>
                  <DropdownMenuItem disabled={!device.snmp_enabled} onSelect={() => runChecks('snmp')}>SNMP test{!device.snmp_enabled ? ' (SNMP off)' : ''}</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {can('devices.manage') && (
              <Button size="sm" variant="outline" className="h-9" onClick={onMaintenance}><Wrench className="h-4 w-4" />Maintenance</Button>
            )}
            {can('alerts.acknowledge') && activeAlerts > 0 && (
              <Button size="sm" variant="outline" className="h-9 border-warning/40 text-warning hover:bg-warning/10" disabled={ack.isPending} onClick={() => setAckOpen(true)}>
                {ack.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <BellRing className="h-4 w-4" />}Acknowledge ({activeAlerts})
              </Button>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm" variant="outline" className="h-9 px-2" aria-label="More actions"><MoreHorizontal className="h-4 w-4" /></Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                {can('ncm.manage') && <DropdownMenuItem disabled={backup.isPending} onSelect={() => backup.mutate()}><Save />Back up configuration</DropdownMenuItem>}
                {can('ncm.view') && <DropdownMenuItem asChild><Link to={`/ncm/${device.id}`}><Save />Configuration history</Link></DropdownMenuItem>}
                {ip && <DropdownMenuItem asChild><a href={`https://${ip}`} target="_blank" rel="noreferrer noopener"><ExternalLink />Open web UI</a></DropdownMenuItem>}
                <DropdownMenuItem onSelect={() => copyText(window.location.href).then(() => toast.success('Link copied'))}><Link2 />Copy link to this page</DropdownMenuItem>
                {can('devices.manage') && <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onSelect={onEdit}><Pencil />Edit device</DropdownMenuItem>
                  <DropdownMenuItem destructive onSelect={onDelete}><Trash2 />Delete device</DropdownMenuItem>
                </>}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        {!compact && (
          <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 border-t border-border/60 pt-3 text-sm">
            {ids.map((item) => (
              <div key={item.label} className="flex min-w-0 items-baseline gap-1.5">
                <dt className="text-xs text-muted">{item.label}</dt>
                <dd className={cn('min-w-0 break-words font-medium', item.mono && 'font-mono text-[13px]')}>{item.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </header>
      <DiagnosticsDialog open={diagOpen} onOpenChange={setDiagOpen} running={diagnostics.run.isPending} result={diagnostics.result}
        kind={diagnostics.kind} snmpEnabled={!!device.snmp_enabled} onRerun={() => diagnostics.run.mutate(diagnostics.kind)} />
      <ConfirmDialog open={ackOpen} onOpenChange={setAckOpen} title="Acknowledge alerts"
        description={<>Acknowledge all <b>{activeAlerts}</b> active alert{activeAlerts === 1 ? '' : 's'} for this device?</>}
        confirmText="Acknowledge" loading={ack.isPending} onConfirm={() => ack.mutate(undefined, { onSettled: () => setAckOpen(false) })} />
    </>
  )
}
