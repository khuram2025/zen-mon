import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowDownRight, ArrowUpRight, Clock, Map as MapIcon, Network } from 'lucide-react'
import { deviceStatusView, TONE_CLASS } from '@/components/devices/DeviceStatusBadge'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Panel } from './charts'
import { formatAbsolute, formatRelative } from './format'
import {
  childrenText, dependencyLabel, mapFocusHref, relatedIsEmpty, sameTimeText, statusWord,
  type DeviceRelated, type RelatedDevice,
} from './relatedModel'

/* Context for troubleshooting: what this device depends on, what depends on
 * it, where it sits on the network maps, and which devices changed status at
 * the same time (±2 min of this device's last confirmed transitions). */

export function useDeviceRelated(id: string) {
  return useQuery<DeviceRelated>({
    queryKey: ['device', id, 'related'],
    queryFn: async () => (await api.get(`/devices/${id}/related`)).data,
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
  })
}

function StatusPill({ status }: { status: string }) {
  const view = deviceStatusView(status)
  return <span className={cn('shrink-0 rounded-full border px-1.5 text-xs', TONE_CLASS[view.tone])}>{view.label}</span>
}

function DeviceLine({ device, extra }: { device: RelatedDevice; extra?: React.ReactNode }) {
  return (
    <li className="flex min-w-0 items-center justify-between gap-2 py-1 text-sm">
      <span className="min-w-0">
        <Link to={`/devices/${device.id}`} className="break-words font-medium text-primary hover:underline">{device.hostname}</Link>
        {(device.ip_address || extra) && <span className="block text-xs text-muted">{[device.ip_address, extra].filter(Boolean).map((v, i) => <span key={i}>{i ? ' · ' : ''}{v}</span>)}</span>}
      </span>
      <StatusPill status={device.status} />
    </li>
  )
}

function Section({ icon: Icon, title, children }: { icon: React.ComponentType<{ className?: string }>; title: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0">
      <h4 className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-muted"><Icon className="h-3.5 w-3.5" aria-hidden />{title}</h4>
      {children}
    </section>
  )
}

export function RelatedPanel({ deviceId }: { deviceId: string }) {
  const { data, isPending, isError, refetch } = useDeviceRelated(deviceId)
  const meta = 'Dependencies, maps and devices that changed at the same time'
  if (isPending) return <Panel title="Related" meta={meta}><p className="text-sm text-muted">Loading related devices…</p></Panel>
  if (isError && !data) {
    return <Panel title="Related" meta={meta}>
      <p className="text-sm text-warning">Related devices could not be loaded. <button type="button" className="underline" onClick={() => refetch()}>Retry</button></p>
    </Panel>
  }
  const r = data!
  const upstream = [...(r.parent ? [{ ...r.parent, dependency_type: 'managed', suppress_alerts: false, enabled: true }] : []), ...r.dependencies.upstream]
  const same = r.same_time
  return (
    <Panel title="Related" meta={meta}>
      {relatedIsEmpty(r) && !r.unavailable.length ? (
        <p className="text-sm text-muted">No managing controller, dependencies or maps are recorded for this device, and no other device changed status within two minutes of its last status changes.</p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {upstream.length > 0 && <Section icon={ArrowUpRight} title="Depends on">
            <ul className="divide-y divide-border/50">{upstream.map((d) => <DeviceLine key={`${d.id}-${d.dependency_type}`} device={d}
              extra={d.dependency_type === 'managed' ? 'Managing controller' : `${dependencyLabel(d.dependency_type)} dependency${d.suppress_alerts ? ' · suppresses alerts' : ''}`} />)}</ul>
          </Section>}
          {(r.children.total > 0 || r.dependencies.downstream.length > 0) && <Section icon={ArrowDownRight} title="Depends on this device">
            {r.children.total > 0 && <p className="text-sm">
              <Link to={`/devices?managed_by=${deviceId}`} className="font-medium text-primary hover:underline">{childrenText(r.children)}</Link>
            </p>}
            <ul className="divide-y divide-border/50">
              {r.dependencies.downstream.map((d) => <DeviceLine key={`${d.id}-${d.dependency_type}`} device={d} extra={`${dependencyLabel(d.dependency_type)} dependency`} />)}
              {r.children.items.filter((c) => c.status !== 'up').slice(0, 5).map((d) => <DeviceLine key={d.id} device={d} extra="Managed device" />)}
            </ul>
          </Section>}
          {r.maps && r.maps.maps.length > 0 && <Section icon={MapIcon} title="On network maps">
            <ul className="space-y-1 text-sm">
              {r.maps.maps.map((m) => <li key={m.id}><Link to={mapFocusHref(m.id, deviceId)} className="text-primary hover:underline">{m.name}</Link> <span className="text-xs text-muted">· opens centred on this device</span></li>)}
            </ul>
            {r.maps.neighbours.length > 0 && <>
              <h5 className="mt-2 text-xs text-muted">Linked on the maps</h5>
              <ul className="divide-y divide-border/50">{r.maps.neighbours.slice(0, 6).map((d) => <DeviceLine key={d.id} device={d} />)}</ul>
            </>}
          </Section>}
          <Section icon={Clock} title={`Changed at the same time (±${Math.round((same?.window_s ?? 120) / 60)} min)`}>
            {same == null ? <p className="text-sm text-muted">The status history is unavailable right now.</p>
              : !same.transitions.length ? <p className="text-sm text-muted">This device has no confirmed status changes in the last 30 days.</p>
                : !same.devices.length ? <p className="text-sm text-muted">No other device changed status within two minutes of this device's last {same.transitions.length === 1 ? 'change' : `${same.transitions.length} changes`} (latest {statusWord(same.transitions[0].new_status)}, <span title={formatAbsolute(same.transitions[0].at)}>{formatRelative(same.transitions[0].at)}</span>).</p>
                  : <ul className="divide-y divide-border/50">
                    {same.devices.slice(0, 8).map((d) => <DeviceLine key={d.id} device={d}
                      extra={<span title={formatAbsolute(d.matches[0].at, { seconds: true })}>{sameTimeText(d.matches[0])}{d.shared_transitions > 1 ? ` · ${d.shared_transitions} shared changes` : ''}</span>} />)}
                  </ul>}
          </Section>
          {!upstream.length && !r.children.total && !r.dependencies.downstream.length && <Section icon={Network} title="Dependencies">
            <p className="text-sm text-muted">No dependencies are configured. Dependencies let a parent outage suppress this device's alerts.</p>
          </Section>}
        </div>
      )}
    </Panel>
  )
}
