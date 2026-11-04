import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AlertTriangle, Lock, Router } from 'lucide-react'
import type { DeviceWidget } from '@/components/devices/DeviceWidgetGrid'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Panel, SERIES_COLORS, Sparkline, TimeSeriesPanel } from './charts'
import {
  availabilityText, formatWatts, historyPoints, isApPanel, isSwitchPanel, portList, signalTone, sourceText,
  type ApPanel, type ControllerHistory, type ControllerReadings, type NotSupported, type SwitchPanel,
} from './controllerModel'
import { formatAbsolute, formatBitRate, formatBitRateAxis, formatRelative, formatSpan, formatUsagePct, MISSING, PERCENT_DOMAIN } from './format'
import { KpiTile } from './KpiTile'
import type { DeviceRange, DeviceRecord } from './types'

/* A FortiSwitch or FortiAP promoted from its FortiGate is never polled itself:
 * everything here is read through the controller's own rows and history
 * (GET /devices/{id}/controller-readings, /controller-history). Every reading
 * names the controller and its measurement time; a stale reading says so, an
 * offline child shows no gauges rather than zeros, and what the controller's
 * MIB does not carry is listed as unavailable with the source it would need. */

export function isManagedChild(device: Pick<DeviceRecord, 'managed_by_device_id'> & { poll_mode?: unknown }): boolean {
  return device.poll_mode === 'via_controller' && !!device.managed_by_device_id
}

export function useControllerReadings(id: string, enabled: boolean) {
  return useQuery<ControllerReadings>({
    queryKey: ['device', id, 'controller-readings'],
    queryFn: async () => (await api.get(`/devices/${id}/controller-readings`)).data,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
    enabled,
  })
}

export function useControllerHistory(id: string, hours: number, enabled: boolean) {
  return useQuery<ControllerHistory>({
    queryKey: ['device', id, 'controller-history', hours],
    queryFn: async () => (await api.get(`/devices/${id}/controller-history?hours=${hours}`)).data,
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
    enabled,
  })
}

const SEV_TEXT: Record<string, string> = { ok: 'text-success', warn: 'text-warning', crit: 'text-danger', info: 'text-muted' }

function StaleNote({ data }: { data: ControllerReadings }) {
  return (
    <span className={cn('inline-flex items-center gap-1', data.stale && 'text-warning')}>
      {data.stale && <AlertTriangle className="h-3 w-3" aria-hidden />}
      <span title={data.measured_at ? formatAbsolute(data.measured_at, { seconds: true }) : undefined}>{sourceText(data)}</span>
    </span>
  )
}

/** Summary-tab KPI tiles for a managed child. */
export function controllerTiles(data: ControllerReadings | undefined, history: ControllerHistory | undefined, loading: boolean): DeviceWidget[] {
  const tiles: DeviceWidget[] = []
  const tile = (id: string, title: string, content: React.ReactNode) =>
    tiles.push({ id: `controller-${id}`, title, width: 2, height: 4, minWidth: 2, minHeight: 4, compact: true, content })
  if (!data?.managed && !loading) return tiles
  const r = data?.readings
  const sub = data ? <StaleNote data={data} /> : undefined
  const offline = r?.offline ? r.offline_text : null
  const spark = (key: string, label: string, color: string) => {
    const values = historyPoints(history, key).map((p) => p.value)
    return values.length ? <Sparkline label={label} domain={PERCENT_DOMAIN} stroke={color} format={(v) => `${v.toFixed(0)}%`} values={values.slice(-60)} /> : undefined
  }
  tile('cpu', 'CPU', <KpiTile label="CPU" loading={loading} value={formatUsagePct(r?.cpu)} sub={offline || sub} chart={spark('cpu', 'CPU', SERIES_COLORS.primary)} />)
  tile('memory', 'Memory', <KpiTile label="Memory" loading={loading} value={formatUsagePct(r?.memory)} sub={offline || sub} chart={spark('memory', 'Memory', SERIES_COLORS.accent)} />)
  const avail = availabilityText(data?.availability)
  tile('availability', 'Availability (24h)', <KpiTile label="Availability (24h)" loading={loading} value={avail.value}
    tone={data?.availability?.sufficient && (data.availability.pct ?? 100) < 95 ? 'warning' : 'neutral'} sub={avail.sub} />)
  const bootUnsupported = data?.not_supported?.find((u) => u.metric === 'boot_uptime')
  tile('uptime', 'Uptime (since boot)', <KpiTile label="Uptime (since boot)" loading={loading}
    value={r?.uptime_s != null ? formatSpan(r.uptime_s) : MISSING}
    sub={r?.uptime_s != null ? 'Reported by the controller' : offline || (bootUnsupported ? 'Not reported by this source' : 'Not reported')} />)
  const panel = data?.panel
  if (isSwitchPanel(panel)) {
    tile('ports', 'Ports', <KpiTile label="Ports" value={`${panel.summary.up} up`} sub={`of ${panel.summary.ports} · PoE ${formatWatts(panel.summary.poe_draw_w)} drawn`} />)
  } else if (isApPanel(panel) || data?.kind === 'access_point') {
    tile('clients', 'Wireless clients', <KpiTile label="Wireless clients" value={r?.clients != null ? Math.round(r.clients).toLocaleString() : MISSING}
      sub={offline || (isApPanel(panel) ? `${panel.summary.radios} radios · ${panel.summary.ssids} SSIDs` : undefined)} />)
  }
  return tiles
}

/** The controller relationship and identity of a managed child. */
export function ControllerSourcePanel({ data }: { data: ControllerReadings | undefined }) {
  if (!data) return <Panel title="Controller"><p className="text-sm text-muted">Loading controller readings…</p></Panel>
  if (!data.managed) return null
  const r = data.readings!
  const rows: Array<[string, React.ReactNode]> = [
    ['Managed through', data.controller ? <Link to={`/devices/${data.controller.id}`} className="font-medium text-primary hover:underline">{data.controller.hostname}</Link> : MISSING],
    ['Relationship', data.kind === 'switch' ? 'FortiLink-managed switch' : data.kind === 'access_point' ? 'CAPWAP-managed access point' : 'Controller-managed device'],
    ['Measured', data.measured_at ? <span title={formatAbsolute(data.measured_at, { seconds: true })}>{formatRelative(data.measured_at)}</span> : 'Not reported yet'],
    ['Freshness', data.stale ? <span className="text-warning">Stale: older than {Math.round((data.stale_after_s ?? 600) / 60)} min</span> : 'Current'],
  ]
  if (r.state) rows.push([data.kind === 'access_point' ? 'CAPWAP state' : 'Controller state', <span className={SEV_TEXT[r.state.sev || ''] || ''}>{r.state.text || MISSING}</span>])
  if (r.authorization) rows.push(['Authorization', <span className={SEV_TEXT[r.authorization.sev || ''] || ''}>{r.authorization.text || MISSING}</span>])
  rows.push(['Model', r.model || MISSING], ['Firmware', r.firmware || MISSING], ['Serial', r.serial || MISSING])
  if (r.ip) rows.push(['Address', r.ip])
  if (r.mac) rows.push(['Base MAC', <span className="font-mono text-xs">{r.mac}</span>])
  rows.push(['Uptime (since boot)', r.uptime_s != null ? formatSpan(r.uptime_s) : MISSING])
  if (r.session_uptime_s != null) rows.push(['CAPWAP session up', formatSpan(r.session_uptime_s)])
  if (r.joined_at) rows.push(['Joined FortiLink', formatAbsolute(r.joined_at)])
  if (r.flags) rows.push(['Transaction flags', r.flags])
  const avail = availabilityText(data.availability)
  rows.push(['Availability (24h)', <span title={avail.sub}>{avail.value}</span>])
  return (
    <Panel title="Controller" meta={<>Readings come from the controller's poll; this device is not pinged or polled directly and uses no license seat.</>}>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {rows.map(([k, v]) => <div key={k} className="contents"><dt className="text-muted">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>)}
      </dl>
      {r.offline && <p className="mt-2 text-xs text-muted">{r.offline_text}: CPU, memory and clients are left blank while the controller reports this device offline.</p>}
    </Panel>
  )
}

function NotSupportedList({ items }: { items: NotSupported[] | undefined }) {
  if (!items?.length) return null
  return (
    <Panel title="Not available from this controller" meta="Reported as unavailable instead of guessed. Each needs the read-only source named.">
      <ul className="space-y-2 text-sm">
        {items.map((u) => (
          <li key={u.metric}>
            <span className="font-medium">{u.label}</span>: <span className="text-muted">{u.reason}</span>
            <span className="block text-xs text-muted">Needs {u.requires}</span>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function HistoryCharts({ data, history, range }: { data: ControllerReadings; history: ControllerHistory | undefined; range: DeviceRange }) {
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const common = { fromTs, toTs, rangeHours: range.hours, syncId: 'controller-child', height: 150 }
  const isAp = data.kind === 'access_point'
  const radios = (history?.series || []).filter((s) => s.key.startsWith('radio_'))
  const colors = [SERIES_COLORS.primary, SERIES_COLORS.accent, SERIES_COLORS.info, SERIES_COLORS.warning]
  return (
    <Panel title="History" meta={<>From {data.controller?.hostname || 'the controller'}'s own series · {range.label}</>}>
      <div className="space-y-4">
        <TimeSeriesPanel {...common} domain={PERCENT_DOMAIN} format={(v) => `${v.toFixed(1)}%`} axisFormat={(v) => `${Math.round(v)}%`}
          series={[{ key: 'cpu', name: 'CPU', color: SERIES_COLORS.primary, points: historyPoints(history, 'cpu') },
            { key: 'memory', name: 'Memory', color: SERIES_COLORS.accent, points: historyPoints(history, 'memory') }]} />
        {isAp ? <>
          <TimeSeriesPanel {...common} domain="auto" format={formatBitRate} axisFormat={formatBitRateAxis}
            series={[{ key: 'rx', name: 'Traffic in', color: SERIES_COLORS.info, points: historyPoints(history, 'rx_bps') },
              { key: 'tx', name: 'Traffic out', color: SERIES_COLORS.accent, points: historyPoints(history, 'tx_bps') }]} emptyText="No traffic samples" />
          <TimeSeriesPanel {...common} domain="auto" format={(v) => `${Math.round(v)} clients`} axisFormat={(v) => `${Math.round(v)}`}
            series={radios.length ? radios.map((s, i) => ({ key: s.key, name: `Radio ${s.radio_id} clients`, color: colors[i % colors.length],
              points: s.points.map((p) => ({ ts: Date.parse(p.ts), value: p.value })) }))
              : [{ key: 'clients', name: 'Clients', color: SERIES_COLORS.primary, points: historyPoints(history, 'clients') }]}
            emptyText="No client samples" />
        </> : <>
          <TimeSeriesPanel {...common} domain="auto" format={(v) => formatWatts(v)} axisFormat={(v) => `${Math.round(v)} W`}
            series={[{ key: 'poe', name: 'PoE drawn', color: SERIES_COLORS.warning, points: historyPoints(history, 'poe_draw_w') }]} emptyText="No PoE samples" />
          <TimeSeriesPanel {...common} domain="auto" format={(v) => `${Math.round(v)} ports up`} axisFormat={(v) => `${Math.round(v)}`}
            series={[{ key: 'ports', name: 'Ports up', color: SERIES_COLORS.success, points: historyPoints(history, 'ports_up') }]} emptyText="No port samples" />
        </>}
      </div>
    </Panel>
  )
}

function SwitchPorts({ panel }: { panel: SwitchPanel }) {
  return (
    <Panel title="Ports" meta={<>{panel.summary.up} of {panel.summary.ports} up · PoE {formatWatts(panel.summary.poe_draw_w)} drawn
      · budget not reported by this MIB · {panel.measured_at ? `measured ${formatRelative(panel.measured_at)}` : 'not measured yet'}{panel.stale ? ' · stale' : ''}</>}>
      {panel.note ? <p className="text-sm text-muted">{panel.note}</p> : (
        <Table>
          <THead><Tr><Th>Port</Th><Th>Link</Th><Th>Speed</Th><Th>Native VLAN</Th><Th>Allowed VLANs</Th><Th>PoE</Th><Th className="text-right">Draw</Th><Th>Connected</Th></Tr></THead>
          <TBody>
            {panel.ports.map((p) => (
              <Tr key={p.instance}>
                <Td className="font-medium">{p.name}</Td>
                <Td><span className={p.up ? 'text-success' : 'text-muted'}>{p.link?.text || MISSING}</span></Td>
                <Td className="text-xs">{p.speed || MISSING}</Td>
                <Td className="tabular-nums">{p.native_vlan || MISSING}</Td>
                <Td className="max-w-[16rem] break-words text-xs">{p.allowed_vlans || MISSING}{p.untagged_vlans ? <span className="block text-muted">untagged {p.untagged_vlans}</span> : null}</Td>
                <Td className="text-xs">{p.poe.capable === false ? 'Not capable' : p.poe.enabled === false ? 'Disabled' : p.poe.state || MISSING}</Td>
                <Td className="text-right tabular-nums">{formatWatts(p.poe.draw_w)}</Td>
                <Td className="text-xs">{p.connected_devices.length ? p.connected_devices.map((d) => (
                  <Link key={d.id} to={`/devices/${d.id}`} className="block text-primary hover:underline">{d.hostname}</Link>)) : MISSING}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
      {panel.vlans.length > 0 && (
        <div className="mt-3">
          <h4 className="mb-1 text-xs font-semibold text-muted">VLAN membership (native)</h4>
          <ul className="flex flex-wrap gap-2 text-xs">
            {panel.vlans.map((v) => <li key={v.vlan} className="rounded-full border border-border px-2 py-0.5">VLAN {v.vlan} · {portList(v.native_ports)}</li>)}
          </ul>
        </div>
      )}
    </Panel>
  )
}

function ApRadios({ panel }: { panel: ApPanel }) {
  const up = panel.uplink
  return (
    <Panel title="Radios and SSIDs" meta={<>{panel.summary.radios} radios · {panel.summary.ssids} SSIDs · {panel.measured_at ? `measured ${formatRelative(panel.measured_at)}` : 'not measured yet'}{panel.stale ? ' · stale' : ''}</>}>
      <Table>
        <THead><Tr><Th>Radio</Th><Th>Band</Th><Th>Mode</Th><Th className="text-right">Channel</Th><Th>Width</Th><Th className="text-right">Tx power</Th><Th className="text-right">Clients</Th><Th>Signal (clients)</Th><Th>Utilization</Th></Tr></THead>
        <TBody>
          {panel.radios.map((r) => (
            <Tr key={r.radio_id}>
              <Td className="font-medium">Radio {r.radio_id}{r.phy ? <span className="block text-xs text-muted">{r.phy}</span> : null}</Td>
              <Td>{r.band || MISSING}</Td>
              <Td className="text-xs">{r.mode?.text || MISSING}</Td>
              <Td className="text-right tabular-nums">{r.channel ?? MISSING}</Td>
              <Td className="text-xs">{r.width || MISSING}</Td>
              <Td className="text-right tabular-nums">{r.tx_power_dbm != null ? `${r.tx_power_dbm} dBm` : MISSING}</Td>
              <Td className="text-right tabular-nums">{r.clients ?? MISSING}</Td>
              <Td className="text-xs">{r.signal ? `avg ${r.signal.avg_dbm} dBm · min ${r.signal.min_dbm} dBm${r.signal.poor_clients ? ` · ${r.signal.poor_clients} poor` : ''}` : MISSING}</Td>
              <Td className="text-xs text-muted" title="Not in FORTINET-FORTIGATE-MIB">Unavailable</Td>
            </Tr>
          ))}
        </TBody>
      </Table>
      {!panel.radios.length && <p className="mt-2 text-sm text-muted">No radios reported (the AP may be offline).</p>}
      {panel.ssids.length > 0 && (
        <Table className="mt-3">
          <THead><Tr><Th>SSID</Th><Th>Radio</Th><Th className="text-right">Clients</Th><Th className="text-right">In</Th><Th className="text-right">Out</Th></Tr></THead>
          <TBody>
            {panel.ssids.map((s) => (
              <Tr key={`${s.radio_id}-${s.if_index}`}>
                <Td className="font-medium">{s.ssid}</Td><Td>Radio {s.radio_id}</Td>
                <Td className="text-right tabular-nums">{s.clients ?? MISSING}</Td>
                <Td className="text-right tabular-nums">{formatBitRate(s.rx_bps)}</Td>
                <Td className="text-right tabular-nums">{formatBitRate(s.tx_bps)}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
      <div className="mt-3 text-sm">
        <h4 className="mb-1 text-xs font-semibold text-muted">Uplink</h4>
        <p>CAPWAP {up.capwap?.text || MISSING}{up.session_uptime_s != null ? ` · session up ${formatSpan(up.session_uptime_s)}` : ''}{up.ip ? ` · ${up.ip}` : ''}</p>
        {up.switch_ports.length ? up.switch_ports.map((p, i) => (
          <p key={i} className="text-xs">
            Seen on {p.switch ? <Link to={`/devices/${p.switch.id}`} className="text-primary hover:underline">{p.switch.hostname}</Link> : p.switch_serial}
            {' '}{p.if_name || (p.port_number ? `port${p.port_number}` : '')}{p.vlan_id ? ` · VLAN ${p.vlan_id}` : ''}{p.is_direct ? '' : ' (not direct)'}
            {p.last_seen ? <span className="text-muted"> · {formatRelative(p.last_seen)}</span> : null}
          </p>
        )) : <p className="text-xs text-muted">The switch port is not known: Device Tracker has not seen this AP's MAC on a FortiLink switch port.</p>}
      </div>
    </Panel>
  )
}

function ApClients({ panel }: { panel: ApPanel }) {
  return (
    <Panel title="Wireless clients" meta={<>{panel.summary.client_rows} associated{panel.summary.clients_truncated ? ' · the controller table hit its row limit, some clients are not listed' : ''}</>}
      actions={panel.clients_restricted ? <span className="inline-flex items-center gap-1 text-muted"><Lock className="h-3 w-3" aria-hidden />Identity needs Device Tracker access</span> : undefined}>
      {panel.clients_by_ssid.length > 0 && (
        <ul className="mb-2 flex flex-wrap gap-2 text-xs">
          {panel.clients_by_ssid.map((s) => <li key={s.ssid} className="rounded-full border border-border px-2 py-0.5">{s.ssid} · {s.clients}</li>)}
        </ul>
      )}
      {!panel.clients.length ? <p className="text-sm text-muted">No clients associated.</p> : (
        <Table>
          <THead><Tr>
            {!panel.clients_restricted && <><Th>Client</Th><Th>Address</Th></>}
            {!panel.users_restricted && <Th>User</Th>}
            <Th>SSID</Th><Th>Radio</Th><Th className="text-right">Signal</Th><Th className="text-right">SNR</Th><Th className="text-right">Rate (tx/rx)</Th>
          </Tr></THead>
          <TBody>
            {panel.clients.slice(0, 200).map((c, i) => (
              <Tr key={c.mac || i}>
                {!panel.clients_restricted && <>
                  <Td className="text-xs">{c.host || MISSING}<span className="block font-mono text-muted">{c.mac || MISSING}</span></Td>
                  <Td className="text-xs">{c.ip || MISSING}{c.vlan ? <span className="block text-muted">VLAN {c.vlan}</span> : null}</Td>
                </>}
                {!panel.users_restricted && <Td className="text-xs">{c.user || MISSING}</Td>}
                <Td className="text-xs">{c.ssid || MISSING}</Td>
                <Td className="text-xs">Radio {c.radio_id}{c.channel ? ` · ch ${c.channel}` : ''}</Td>
                <Td className={cn('text-right tabular-nums', SEV_TEXT[signalTone(c.signal_quality)])}>{c.signal_dbm != null ? `${c.signal_dbm} dBm` : MISSING}</Td>
                <Td className="text-right tabular-nums">{c.signal_dbm != null && c.noise_dbm != null ? `${c.signal_dbm - c.noise_dbm} dB` : MISSING}</Td>
                <Td className="text-right text-xs tabular-nums">{c.tx_kbps != null ? `${Math.round(c.tx_kbps / 1000)}/${Math.round((c.rx_kbps ?? 0) / 1000)} Mbps` : MISSING}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
    </Panel>
  )
}

/** The managed child's own tab: controller relationship, history, ports or radios/clients and unsupported readings. */
export function ManagedChildTab({ device, range }: { device: DeviceRecord; range: DeviceRange }) {
  const readings = useControllerReadings(device.id, true)
  const history = useControllerHistory(device.id, Math.max(1, Math.round(range.hours)), true)
  const data = readings.data
  if (readings.isPending) return <p className="text-sm text-muted">Loading controller readings…</p>
  if (!data?.managed) {
    return <p className="text-sm text-muted">This device is not managed through a controller.</p>
  }
  const panel = data.panel
  return (
    <section aria-label="Controller-managed device" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Router className="h-3.5 w-3.5" aria-hidden /><StaleNote data={data} />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <ControllerSourcePanel data={data} />
        <HistoryCharts data={data} history={history.data} range={range} />
      </div>
      {isSwitchPanel(panel) && <SwitchPorts panel={panel} />}
      {isApPanel(panel) && <><ApRadios panel={panel} /><ApClients panel={panel} /></>}
      {panel && !panel.supported && <p className="text-sm text-muted">{panel.reason}</p>}
      <NotSupportedList items={data.not_supported} />
    </section>
  )
}
