/** System tab — what the host is: hardware spec sheet, storage volumes with
 *  growth forecasts, network interfaces with live throughput and traffic
 *  capture, and installed software.
 *
 *  Sub-sections are deep-linked through ?sub= (hardware · storage · network
 *  · software). */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowDownToLine, ArrowUpFromLine, CalendarClock, Clock3, Cpu, Database, Fingerprint,
  Gauge, Globe, HardDrive, MemoryStick, Monitor, Network as NetworkIcon, Package, Radio, Search,
  Server as ServerIcon, Timer, Users,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn, formatBps, formatBytes, relativeTime } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { productPath } from '@/components/inventory/softwareModel'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { OsIcon } from '@/components/servers/shared'
import {
  ExportCsvButton, QueryError, TablePager, TableStateRow, cmp, sortIndicator, sortableTh, usePagedRows,
} from '@/components/servers/tables'
import { NetworkCapturePanel } from '@/components/servers/NetworkCapture'
import type {
  ServerFilesystem, ServerItem, ServerNetworkInterface, ServerPhysicalDisk, ServerSoftware, ServerSoftwareResponse,
} from '@/types/servers'
import {
  C, EmptyState, KeyValues, Meter, Panel, Pill, SectionHeading, Segmented, Sparkline, StatTile,
  fmtPct, pctTone, type Tone,
} from '../ui'
import { isPseudoInterface, useFilesystems, useRecentDetail } from '../data'

export type SystemSection = 'hardware' | 'storage' | 'network' | 'software'
const SECTION_KEYS: readonly SystemSection[] = ['hardware', 'storage', 'network', 'software']

/* ── Local helpers ───────────────────────────────────────────────── */

const th = 'h-9 px-3 text-[11px]'
const td = 'px-3 py-2 text-[13px]'
const rowCls = 'border-border/60 hover:bg-surface2/40'

/** Compact uptime from a boot timestamp, e.g. "12d 4h" / "3h 20m". */
function formatUptime(bootTime: string): string {
  const secs = Math.max(0, (Date.now() - Date.parse(bootTime)) / 1000)
  const d = Math.floor(secs / 86400)
  const h = Math.floor((secs % 86400) / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

/** "15.9 GB" → ["15.9", "GB"] so tiles can style the unit. */
function splitBytes(bytes: number | null | undefined): [string, string | undefined] {
  if (!bytes) return ['—', undefined]
  const [v, u] = formatBytes(bytes).split(' ')
  return [v, u]
}

function splitBps(bps: number | null | undefined): [string, string | undefined] {
  if (bps == null) return ['—', undefined]
  const [v, u] = formatBps(bps).split(' ')
  return [String(Number(v)), u]
}

function capitalize(s: string | null | undefined): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '—'
}

const COLLECTION_LABEL: Record<string, string> = {
  agent: 'Agent',
  agentless_wmi: 'Agentless (WMI)',
  agentless_winrm: 'Agentless (WinRM)',
  snmp: 'SNMP',
  ssh: 'SSH',
  none: 'Not collected',
}

/** Mount keys that survive "C:" vs "C:\" vs "c:/" differences. */
function mountKey(m: string): string {
  const k = m.trim().toLowerCase().replace(/[\\/]+$/, '')
  return k || '/'
}

/** 1000 Mbps reads better as 1 Gbps, and link speed is reported in Mbps. */
function formatLinkSpeed(mbps: number | null): string {
  if (mbps == null || mbps <= 0) return '—'
  if (mbps >= 1000) {
    const gbps = mbps / 1000
    return `${Number.isInteger(gbps) ? gbps : gbps.toFixed(1)} Gbps`
  }
  return `${mbps} Mbps`
}

/** Loopback and pseudo adapters report nonsense (a 1073 Mbps "link" with an
 *  MTU of -1), so they are hidden by default. */
function isVirtualNic(nic: ServerNetworkInterface): boolean {
  return isPseudoInterface(nic.if_name || '') || (nic.mtu != null && nic.mtu <= 0)
}

/** Windows reports install dates as yyyymmdd; everything else is ISO. */
function parseInstallDate(s: string | null): Date | null {
  if (!s) return null
  const compact = /^(\d{4})(\d{2})(\d{2})$/.exec(s)
  const d = compact ? new Date(Number(compact[1]), Number(compact[2]) - 1, Number(compact[3])) : new Date(s)
  return Number.isNaN(d.getTime()) ? null : d
}

function diskTone(status: string | null | undefined): Tone {
  const s = (status || '').toLowerCase()
  if (!s) return 'muted'
  if (s === 'ok' || s === 'healthy') return 'success'
  if (/fail|error|degraded|lost|nonrecover|stress/.test(s)) return 'danger'
  return 'warning'
}

function fullInTone(days: number | null | undefined): Tone {
  if (days == null) return 'muted'
  return days < 7 ? 'danger' : days < 30 ? 'warning' : 'default'
}

function formatDays(days: number): string {
  if (days < 1) return '< 1 day'
  if (days > 365) return '> 1 year'
  const n = Math.round(days)
  return `~${n} day${n === 1 ? '' : 's'}`
}

const TONE_TEXT: Record<Tone, string> = {
  default: 'text-text', success: 'text-success', warning: 'text-warning',
  danger: 'text-danger', info: 'text-info', muted: 'text-muted',
}

/* ── Tab ─────────────────────────────────────────────────────────── */

export function SystemTab({ server, sub, setSub }: { server: ServerItem; sub: string; setSub: (s: string) => void }) {
  const active: SystemSection = (SECTION_KEYS as readonly string[]).includes(sub) ? (sub as SystemSection) : 'hardware'

  // Shared keys: these are the same fetches the header and other tabs use.
  const fsQ = useFilesystems(server.id)
  const netQ = useNetwork(server.id)
  // Software is the heaviest payload; fetch it when asked for, but keep
  // showing the cached count on the control afterwards.
  const swQ = useSoftware(server.id, active === 'software')

  const fullVolumes = (fsQ.data?.items || []).filter((fs) => (fs.used_pct || 0) >= 90).length
  const realNics = (netQ.data?.items || []).filter((n) => !isVirtualNic(n)).length
  const badDisks = (server.physical_disks || []).filter((d) => diskTone(d.status) === 'danger').length

  const items = [
    { key: 'hardware' as const, label: 'Hardware', icon: Cpu, count: badDisks, tone: 'danger' as const },
    { key: 'storage' as const, label: 'Storage', icon: HardDrive, count: fullVolumes, tone: 'warning' as const },
    { key: 'network' as const, label: 'Network', icon: NetworkIcon, count: realNics, tone: 'muted' as const },
    { key: 'software' as const, label: 'Software', icon: Package, count: swQ.data?.items?.length ?? 0, tone: 'muted' as const },
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented items={items} value={active} onChange={(k) => setSub(k)} />
        <span className="text-xs text-muted">
          Inventory from {COLLECTION_LABEL[server.collection_mode] ?? server.collection_mode}
          {server.last_seen ? ` · last seen ${relativeTime(server.last_seen)}` : ''}
        </span>
      </div>

      {active === 'hardware' && <HardwareSection server={server} />}
      {active === 'storage' && <StorageSection server={server} fsQ={fsQ} />}
      {active === 'network' && <NetworkSection server={server} netQ={netQ} />}
      {active === 'software' && <SoftwareSection server={server} swQ={swQ} />}
    </div>
  )
}

/* ── Queries ─────────────────────────────────────────────────────── */

function useNetwork(id: string) {
  return useQuery<{ items: ServerNetworkInterface[] }>({
    queryKey: ['servers', id, 'network'],
    queryFn: async () => (await api.get(`/servers/${id}/network`)).data,
    refetchInterval: 60_000,
  })
}

function useSoftware(id: string, enabled: boolean) {
  return useQuery<ServerSoftwareResponse>({
    queryKey: ['servers', id, 'software'],
    queryFn: async () => (await api.get(`/servers/${id}/software`)).data,
    refetchInterval: 60_000,
    enabled,
  })
}

type NetQuery = ReturnType<typeof useNetwork>
type SwQuery = ReturnType<typeof useSoftware>
type FsQuery = ReturnType<typeof useFilesystems>

/* ── Hardware ────────────────────────────────────────────────────── */

function HardwareSection({ server }: { server: ServerItem }) {
  const disks = server.physical_disks || []
  const diskTotal = disks.reduce((s, d) => s + (d.size_bytes || 0), 0)
  const [memV, memU] = splitBytes(server.memory_total_bytes)
  const [diskV, diskU] = splitBytes(diskTotal)
  const hostname = server.hostname || server.display_name
  const domain = server.fqdn && server.fqdn.includes('.')
    ? server.fqdn.slice(server.fqdn.indexOf('.') + 1)
    : null
  const threadsPerCore = server.cpu_cores && server.cpu_physical_cores
    ? server.cpu_cores / server.cpu_physical_cores
    : null
  const bootDate = server.boot_time ? new Date(server.boot_time) : null
  const hasHardware = Boolean(
    server.cpu_model || server.cpu_cores || server.cpu_physical_cores || server.memory_total_bytes || disks.length,
  )

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          icon={Cpu} label="Processor"
          value={server.cpu_cores ?? '—'} unit={server.cpu_cores ? 'logical cores' : undefined}
          sub={<span title={server.cpu_model || undefined}>{server.cpu_model || 'Model not reported'}</span>}
        />
        <StatTile
          icon={MemoryStick} label="Memory" value={memV} unit={memU}
          sub={server.memory_total_bytes ? 'Physical memory installed' : 'Not reported'}
        />
        <StatTile
          icon={HardDrive} label="Physical disks" value={diskV} unit={diskU}
          sub={disks.length ? `${disks.length} disk${disks.length === 1 ? '' : 's'} attached` : 'No disks reported'}
        />
        <StatTile
          icon={Timer} label="Uptime"
          value={server.boot_time ? formatUptime(server.boot_time) : '—'}
          sub={bootDate ? `Booted ${bootDate.toLocaleString()}` : 'Boot time not reported'}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Operating system" icon={Monitor}
          subtitle={server.os_name || capitalize(server.os_type)}
          actions={<OsIcon os={server.os_type} />}
        >
          <KeyValues
            items={[
              ['Name', server.os_name],
              ['Version', server.os_version],
              [server.os_type === 'windows' ? 'Build' : 'Kernel', server.kernel_or_build],
              ['Architecture', server.architecture],
              ['Platform', capitalize(server.os_type)],
              ['Boot time', bootDate ? bootDate.toLocaleString() : null],
            ]}
          />
        </Panel>

        <Panel title="Host identity" icon={Fingerprint} subtitle={server.fqdn || hostname}>
          <KeyValues
            items={[
              ['Hostname', <span key="h" className="font-mono text-[12px]" title={hostname}>{hostname}</span>],
              ['FQDN', server.fqdn ? <span key="f" className="font-mono text-[12px]" title={server.fqdn}>{server.fqdn}</span> : null],
              ['Primary IP', server.primary_ip ? <span key="ip" className="font-mono text-[12px]">{server.primary_ip}</span> : null],
              domain ? ['Domain', <span key="d" title={domain}>{domain}</span>] : null,
              ['Site', server.site_name],
              ['Collection', server.collection_mode === 'agent' && server.agent_version
                ? `Agent v${server.agent_version}`
                : COLLECTION_LABEL[server.collection_mode] ?? server.collection_mode],
            ]}
          />
        </Panel>

        <Panel title="Processor" icon={Cpu} subtitle={server.cpu_model || undefined}>
          <KeyValues
            items={[
              ['Model', server.cpu_model ? <span key="m" title={server.cpu_model}>{server.cpu_model}</span> : null],
              ['Physical cores', server.cpu_physical_cores ?? null],
              ['Logical processors', server.cpu_cores ?? null],
              ['Threads per core', threadsPerCore ? String(Math.round(threadsPerCore * 10) / 10) : null],
            ]}
          />
        </Panel>

        <Panel title="Memory & uptime" icon={Clock3}>
          <KeyValues
            items={[
              ['Physical memory', server.memory_total_bytes ? formatBytes(server.memory_total_bytes) : null],
              ['Uptime', server.boot_time ? formatUptime(server.boot_time) : null],
              ['Last seen', server.last_seen ? relativeTime(server.last_seen) : null],
              ['Agent heartbeat', server.agent_last_heartbeat_at ? relativeTime(server.agent_last_heartbeat_at) : null],
            ]}
          />
        </Panel>
      </div>

      {hasHardware ? (
        <PhysicalDisksPanel disks={disks} />
      ) : (
        <Panel title="Hardware inventory" icon={ServerIcon}>
          <EmptyState
            icon={Cpu}
            title="No hardware inventory reported"
            hint="A current agent reports CPU, physical memory and physical disk details with its inventory snapshot."
          />
        </Panel>
      )}
    </div>
  )
}

function PhysicalDisksPanel({ disks }: { disks: ServerPhysicalDisk[] }) {
  return (
    <Panel
      title="Physical disks" icon={HardDrive} flush
      subtitle={disks.length
        ? `${disks.length} disk${disks.length === 1 ? '' : 's'} · ${formatBytes(disks.reduce((s, d) => s + (d.size_bytes || 0), 0))} raw capacity`
        : undefined}
    >
      {disks.length === 0 ? (
        <EmptyState compact icon={HardDrive} title="No physical disks reported" hint="CPU and memory inventory is available; the agent did not report physical disks." />
      ) : (
        <Table>
          <THead className="bg-surface2/40">
            <Tr className="border-border/60 hover:bg-transparent">
              <Th className={cn(th, 'pl-4')}>Disk</Th>
              <Th className={th}>Interface</Th>
              <Th className={th}>Media</Th>
              <Th className={cn(th, 'text-right')}>Capacity</Th>
              <Th className={cn(th, 'pr-4')}>Status</Th>
            </Tr>
          </THead>
          <TBody>
            {disks.map((disk) => {
              const name = disk.model || disk.device_id || `Disk ${disk.index}`
              return (
                <Tr key={`${disk.index}-${disk.device_id || disk.model}`} className={rowCls}>
                  <Td className={cn(td, 'max-w-[360px] pl-4')}>
                    <div className="truncate font-medium text-text" title={name}>{name}</div>
                    <div className="truncate text-xs text-muted" title={disk.device_id || undefined}>
                      {disk.manufacturer || '—'} · {disk.device_id || `index ${disk.index}`}
                    </div>
                  </Td>
                  <Td className={cn(td, 'text-text2')}>{disk.interface_type || '—'}</Td>
                  <Td className={cn(td, 'text-text2')}>{disk.media_type || '—'}</Td>
                  <Td className={cn(td, 'text-right tabular-nums')}>{disk.size_bytes ? formatBytes(disk.size_bytes) : '—'}</Td>
                  <Td className={cn(td, 'pr-4')}>
                    <Pill tone={diskTone(disk.status)}>{disk.status ? capitalize(disk.status) : 'Unknown'}</Pill>
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      )}
    </Panel>
  )
}

/* ── Storage ─────────────────────────────────────────────────────── */

type VolumeRow = ServerFilesystem & { growth: number | null; daysFull: number | null }
type VolumeSort = 'mount' | 'usage' | 'full'

function formatGrowth(bpd: number | null): { text: string; tone: Tone } {
  if (bpd == null) return { text: '—', tone: 'muted' }
  // Under 1 MB/day is noise from log rotation and temp files.
  if (Math.abs(bpd) < 1024 * 1024) return { text: 'Stable', tone: 'muted' }
  return bpd > 0
    ? { text: `+${formatBytes(bpd)}/day`, tone: 'default' }
    : { text: `−${formatBytes(-bpd)}/day`, tone: 'success' }
}

function StorageSection({ server, fsQ }: { server: ServerItem; fsQ: FsQuery }) {
  const recent = useRecentDetail(server.id)
  const [sort, setSort] = useState<VolumeSort>('mount')
  const baseItems = fsQ.data?.items || []

  const rows: VolumeRow[] = useMemo(() => {
    const growth = new Map<string, { g: number | null; d: number | null }>()
    for (const f of recent.data?.filesystems || []) {
      growth.set(mountKey(f.mount), { g: f.growth_bytes_per_day ?? null, d: f.days_until_full ?? null })
    }
    const out = baseItems.map((fs) => {
      const g = growth.get(mountKey(fs.mount))
      return { ...fs, growth: g?.g ?? null, daysFull: g?.d ?? null }
    })
    return out.sort((a, b) => {
      if (sort === 'usage') return (b.used_pct || 0) - (a.used_pct || 0)
      if (sort === 'full') return (a.daysFull ?? Infinity) - (b.daysFull ?? Infinity) || cmp(a.mount, b.mount)
      return cmp(a.mount, b.mount)
    })
  }, [baseItems, recent.data, sort])

  const stale = baseItems.some((fs) => fs.is_stale)
  const totalBytes = baseItems.reduce((s, fs) => s + (fs.total_bytes || 0), 0)
  const usedBytes = baseItems.reduce((s, fs) => s + (fs.used_bytes || 0), 0)
  const freeBytes = baseItems.reduce((s, fs) => s + (fs.free_bytes || 0), 0)
  const highest = rows.reduce<VolumeRow | null>((m, r) => (!m || (r.used_pct || 0) > (m.used_pct || 0) ? r : m), null)
  const soonest = rows.reduce<VolumeRow | null>(
    (m, r) => (r.daysFull != null && (!m || r.daysFull < (m.daysFull ?? Infinity)) ? r : m), null,
  )
  const over90 = baseItems.filter((fs) => (fs.used_pct || 0) >= 90).length
  const [capV, capU] = splitBytes(totalBytes)

  const csvColumns = [
    { header: 'Mount', value: (f: VolumeRow) => f.mount },
    { header: 'Type', value: (f: VolumeRow) => f.fs_type },
    { header: 'Device', value: (f: VolumeRow) => f.device },
    { header: 'Total bytes', value: (f: VolumeRow) => f.total_bytes },
    { header: 'Used bytes', value: (f: VolumeRow) => f.used_bytes },
    { header: 'Free bytes', value: (f: VolumeRow) => f.free_bytes },
    { header: 'Used %', value: (f: VolumeRow) => f.used_pct },
    { header: 'Growth bytes/day', value: (f: VolumeRow) => (f.growth == null ? '' : Math.round(f.growth)) },
    { header: 'Days until full', value: (f: VolumeRow) => (f.daysFull == null ? '' : Math.round(f.daysFull)) },
  ]

  return (
    <div className="space-y-4">
      {baseItems.length > 0 && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            icon={HardDrive} label="Volumes" value={baseItems.length}
            sub={over90 ? <span className="text-warning">{over90} at or above 90%</span> : 'All below 90% used'}
          />
          <StatTile
            icon={Gauge} label="Highest used"
            value={fmtPct(highest?.used_pct ?? null)} tone={pctTone(highest?.used_pct)}
            pct={highest?.used_pct ?? null}
            sub={highest ? <span className="font-mono" title={highest.mount}>{highest.mount}</span> : undefined}
          />
          <StatTile
            icon={Database} label="Capacity" value={capV} unit={capU}
            pct={totalBytes ? (usedBytes / totalBytes) * 100 : null}
            sub={<><span className="text-text2">{formatBytes(freeBytes)} free</span> · {formatBytes(usedBytes)} used</>}
          />
          <StatTile
            icon={CalendarClock} label="Soonest full"
            value={soonest?.daysFull != null ? formatDays(soonest.daysFull) : '—'}
            tone={fullInTone(soonest?.daysFull)}
            sub={soonest
              ? <span title={soonest.mount}><span className="font-mono">{soonest.mount}</span> at {formatGrowth(soonest.growth).text}</span>
              : recent.isLoading ? 'Computing growth…' : 'No volume is filling up'}
          />
        </div>
      )}

      <Panel
        title="Volumes" icon={HardDrive} flush
        subtitle={baseItems.length
          ? `${baseItems.length} volume${baseItems.length === 1 ? '' : 's'} · growth measured over the last week`
          : undefined}
        actions={baseItems.length > 0 ? (
          <>
            <Segmented
              size="sm" value={sort} onChange={setSort}
              items={[
                { key: 'mount', label: 'Name' },
                { key: 'usage', label: 'Most used' },
                { key: 'full', label: 'Full soonest' },
              ]}
            />
            <ExportCsvButton rows={rows} columns={csvColumns} filename={`${server.display_name}-filesystems.csv`} />
          </>
        ) : undefined}
      >
        {stale && (
          <div className="flex items-start gap-2 border-b border-warning/30 bg-warning/5 px-4 py-2 text-xs text-warning">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>Last-known capacity — the agent has not re-reported these volumes recently.</span>
          </div>
        )}
        {fsQ.isError ? (
          <QueryError error={fsQ.error} onRetry={() => fsQ.refetch()} />
        ) : fsQ.isLoading ? (
          <div className="p-4"><Skeleton className="h-24 w-full" /></div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={HardDrive}
            title="No volumes reported"
            hint="Pseudo and removable filesystems (ISO, tmpfs, overlay) are excluded from capacity monitoring."
          />
        ) : (
          <Table>
            <THead className="bg-surface2/40">
              <Tr className="border-border/60 hover:bg-transparent">
                <Th className={cn(th, 'pl-4')}>Volume</Th>
                <Th className={cn(th, 'w-[26%] min-w-[180px]')}>Usage</Th>
                <Th className={cn(th, 'text-right')}>Used</Th>
                <Th className={cn(th, 'text-right')}>Free</Th>
                <Th className={cn(th, 'text-right')}>Total</Th>
                <Th className={cn(th, 'text-right')}>Growth</Th>
                <Th className={cn(th, 'pr-4 text-right')}>Full in</Th>
              </Tr>
            </THead>
            <TBody>
              {rows.map((fs) => {
                const tone = pctTone(fs.used_pct)
                const growth = formatGrowth(fs.growth)
                const fullTone = fullInTone(fs.daysFull)
                return (
                  <Tr key={fs.mount} className={rowCls}>
                    <Td className={cn(td, 'max-w-[260px] pl-4')}>
                      <div className="truncate font-mono text-[13px] font-medium text-text" title={fs.mount}>{fs.mount}</div>
                      <div className="truncate text-xs text-muted" title={`${fs.fs_type || '—'} · ${fs.device || '—'}`}>
                        {fs.fs_type || '—'} · {fs.device || '—'}
                      </div>
                    </Td>
                    <Td className={td}>
                      <div className="flex items-center gap-2.5">
                        <Meter pct={fs.used_pct} className="flex-1" />
                        <span className={cn('w-12 text-right text-xs font-semibold tabular-nums', TONE_TEXT[tone === 'default' ? 'default' : tone])}>
                          {fmtPct(fs.used_pct)}
                        </span>
                      </div>
                    </Td>
                    <Td className={cn(td, 'text-right tabular-nums')}>{formatBytes(fs.used_bytes || 0)}</Td>
                    <Td className={cn(td, 'text-right tabular-nums text-text2')}>{formatBytes(fs.free_bytes || 0)}</Td>
                    <Td className={cn(td, 'text-right tabular-nums text-text2')}>{formatBytes(fs.total_bytes || 0)}</Td>
                    <Td className={cn(td, 'whitespace-nowrap text-right tabular-nums', TONE_TEXT[growth.tone])}>
                      {recent.isLoading && fs.growth == null ? <Skeleton className="ml-auto h-3 w-14" /> : growth.text}
                    </Td>
                    <Td className={cn(td, 'whitespace-nowrap pr-4 text-right')}>
                      {fs.daysFull == null ? (
                        <span className="text-muted">—</span>
                      ) : fullTone === 'default' ? (
                        <span className="tabular-nums text-text2">{formatDays(fs.daysFull)}</span>
                      ) : (
                        <Pill tone={fullTone} title={`At the current growth rate ${fs.mount} fills in ${formatDays(fs.daysFull)}`}>
                          <span className="tabular-nums">{formatDays(fs.daysFull)}</span>
                        </Pill>
                      )}
                    </Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        )}
      </Panel>

      {(server.physical_disks || []).length > 0 && <PhysicalDisksPanel disks={server.physical_disks || []} />}
    </div>
  )
}

/* ── Network ─────────────────────────────────────────────────────── */

/** IPv4 first — it is what an operator looks up — then global and link-local
 *  IPv6, one per line. */
function InterfaceAddresses({ addresses }: { addresses: string[] }) {
  if (!addresses.length) return <span className="text-muted">—</span>
  const v4 = addresses.filter((a) => !a.includes(':'))
  const v6 = addresses.filter((a) => a.includes(':')).sort((a, b) => Number(a.startsWith('fe80')) - Number(b.startsWith('fe80')))
  const ordered = [...v4, ...v6]
  const shown = ordered.slice(0, 3)
  const extra = ordered.length - shown.length
  return (
    <div className="flex min-w-0 flex-col gap-0.5" title={ordered.join('\n')}>
      {shown.map((a) => (
        <span key={a} className={cn('truncate', a.includes(':') ? 'text-muted' : 'text-text')}>{a}</span>
      ))}
      {extra > 0 && <span className="text-[11px] text-muted">+{extra} more</span>}
    </div>
  )
}

interface NicTraffic {
  rx: number | null
  tx: number | null
  spark: (number | null)[]
  rxSpark: (number | null)[]
  txSpark: (number | null)[]
}

function NetworkSection({ server, netQ }: { server: ServerItem; netQ: NetQuery }) {
  const [showPseudo, setShowPseudo] = useState(false)
  const recent = useRecentDetail(server.id)
  const { data, isLoading, isError, error, refetch } = netQ

  const allItems = data?.items || []
  const pseudoCount = allItems.filter(isVirtualNic).length
  const items = useMemo(() => {
    const base = showPseudo ? allItems : allItems.filter((n) => !isVirtualNic(n))
    // Connected adapters first; the table is usually read for "which NIC is live".
    return [...base].sort((a, b) => Number(Boolean(b.is_up)) - Number(Boolean(a.is_up)) || cmp(a.if_name, b.if_name))
  }, [allItems, showPseudo])
  const upCount = items.filter((n) => n.is_up).length
  const downCount = items.length - upCount
  // Agents from 1.3 report link speed; older ones do not.
  const hasSpeed = items.some((n) => n.speed_mbps != null && n.speed_mbps > 0)

  // Current throughput per interface from the last hour of samples, in bits/s.
  const traffic = useMemo(() => {
    const byName = new Map<string, NicTraffic>()
    for (const nic of recent.data?.interfaces || []) {
      const pts = nic.points
      let last: (typeof pts)[number] | undefined
      for (let i = pts.length - 1; i >= 0; i--) {
        if (pts[i].rx_bytes_ps != null || pts[i].tx_bytes_ps != null) { last = pts[i]; break }
      }
      byName.set(nic.name.toLowerCase(), {
        rx: last?.rx_bytes_ps != null ? last.rx_bytes_ps * 8 : null,
        tx: last?.tx_bytes_ps != null ? last.tx_bytes_ps * 8 : null,
        spark: pts.map((p) => (p.rx_bytes_ps == null && p.tx_bytes_ps == null
          ? null
          : ((p.rx_bytes_ps || 0) + (p.tx_bytes_ps || 0)) * 8)),
        rxSpark: pts.map((p) => (p.rx_bytes_ps == null ? null : p.rx_bytes_ps * 8)),
        txSpark: pts.map((p) => (p.tx_bytes_ps == null ? null : p.tx_bytes_ps * 8)),
      })
    }
    return byName
  }, [recent.data])
  const trafficFor = (nic: ServerNetworkInterface) => traffic.get((nic.if_name || '').toLowerCase())
  const hasTraffic = items.some((n) => trafficFor(n))

  // Totals across the interfaces currently shown, with per-direction trends.
  const totals = useMemo(() => {
    const shown = items
      .map((n) => traffic.get((n.if_name || '').toLowerCase()))
      .filter((t): t is NicTraffic => Boolean(t))
    const sum = (pick: (t: NicTraffic) => (number | null)[]) => {
      const len = Math.max(0, ...shown.map((t) => pick(t).length))
      return Array.from({ length: len }, (_, i) => {
        const vals = shown.map((t) => { const s = pick(t); return s[s.length - len + i] }).filter((v): v is number => v != null)
        return vals.length ? vals.reduce((a, b) => a + b, 0) : null
      })
    }
    return {
      rx: shown.reduce((s, t) => s + (t.rx || 0), 0),
      tx: shown.reduce((s, t) => s + (t.tx || 0), 0),
      rxSpark: sum((t) => t.rxSpark),
      txSpark: sum((t) => t.txSpark),
    }
  }, [items, traffic])
  const [rxV, rxU] = splitBps(hasTraffic ? totals.rx : null)
  const [txV, txU] = splitBps(hasTraffic ? totals.tx : null)

  const csvColumns = [
    { header: 'Interface', value: (n: ServerNetworkInterface) => n.if_name },
    { header: 'State', value: (n: ServerNetworkInterface) => (n.is_up ? 'up' : 'down') },
    { header: 'IP addresses', value: (n: ServerNetworkInterface) => (n.ip_addresses || []).join(' ') },
    { header: 'MAC', value: (n: ServerNetworkInterface) => n.mac_address },
    { header: 'Speed Mbps', value: (n: ServerNetworkInterface) => n.speed_mbps },
    { header: 'MTU', value: (n: ServerNetworkInterface) => n.mtu },
    { header: 'In bps', value: (n: ServerNetworkInterface) => { const v = trafficFor(n)?.rx; return v == null ? '' : Math.round(v) } },
    { header: 'Out bps', value: (n: ServerNetworkInterface) => { const v = trafficFor(n)?.tx; return v == null ? '' : Math.round(v) } },
    { header: 'Updated', value: (n: ServerNetworkInterface) => n.updated_at },
  ]

  const cols = 5 + (hasSpeed ? 1 : 0) + (hasTraffic ? 2 : 0)

  return (
    <div className="space-y-4">
      {allItems.length > 0 && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            icon={NetworkIcon} label="Interfaces" value={items.length}
            sub={pseudoCount > 0 && !showPseudo ? `${pseudoCount} virtual hidden` : `${upCount} connected`}
          />
          <StatTile
            icon={Globe} label="Link state" value={`${upCount}/${items.length}`} unit="up"
            tone={downCount > 0 ? 'warning' : 'success'}
            sub={downCount > 0 ? `${downCount} disconnected or disabled` : 'All interfaces up'}
          />
          <StatTile
            icon={ArrowDownToLine} label="Inbound" value={rxV} unit={rxU}
            sub={hasTraffic ? 'Now, across shown interfaces' : recent.isLoading ? 'Loading…' : 'No recent samples'}
            spark={totals.rxSpark} sparkColor={C.green}
          />
          <StatTile
            icon={ArrowUpFromLine} label="Outbound" value={txV} unit={txU}
            sub={hasTraffic ? `Combined ${formatBps(totals.rx + totals.tx)}` : recent.isLoading ? 'Loading…' : 'No recent samples'}
            spark={totals.txSpark} sparkColor={C.sky}
          />
        </div>
      )}

      <Panel
        title="Network interfaces" icon={NetworkIcon} flush
        subtitle={items.length ? `${upCount} of ${items.length} up${hasTraffic ? ' · throughput is the latest 1-minute sample' : ''}` : undefined}
        actions={allItems.length > 0 ? (
          <>
            {pseudoCount > 0 && (
              <Button
                size="sm"
                variant={showPseudo ? 'default' : 'outline'}
                className="h-8"
                onClick={() => setShowPseudo((v) => !v)}
                title="Loopback, ISATAP, Teredo and similar pseudo adapters"
              >
                {showPseudo ? 'Hide' : 'Show'} virtual ({pseudoCount})
              </Button>
            )}
            <ExportCsvButton rows={items} columns={csvColumns} filename={`${server.display_name}-interfaces.csv`} />
          </>
        ) : undefined}
      >
        <Table>
          <THead className="bg-surface2/40">
            <Tr className="border-border/60 hover:bg-transparent">
              <Th className={cn(th, 'pl-4')}>Interface</Th>
              <Th className={th}>State</Th>
              <Th className={th}>Addresses</Th>
              {hasSpeed && <Th className={cn(th, 'text-right')}>Speed</Th>}
              <Th className={cn(th, 'text-right')}>MTU</Th>
              {hasTraffic && <Th className={cn(th, 'text-right')}>Throughput</Th>}
              {hasTraffic && <Th className={cn(th, 'w-[120px]')}>Last hour</Th>}
              <Th className={cn(th, 'pr-4 text-right')}>Updated</Th>
            </Tr>
          </THead>
          <TBody>
            {isError ? (
              <TableStateRow colSpan={cols}><QueryError error={error} onRetry={() => refetch()} /></TableStateRow>
            ) : isLoading ? (
              <TableStateRow colSpan={cols}><div className="py-6"><Skeleton className="h-20 w-full" /></div></TableStateRow>
            ) : items.length === 0 ? (
              <TableStateRow colSpan={cols}>
                <EmptyState
                  icon={NetworkIcon}
                  title={pseudoCount > 0 ? 'Only virtual adapters on this host' : 'No interfaces reported'}
                  hint={pseudoCount > 0
                    ? `${pseudoCount} loopback or pseudo adapter${pseudoCount === 1 ? '' : 's'} hidden — use “Show virtual” to list them.`
                    : 'The agent reports network interfaces with its inventory snapshot.'}
                />
              </TableStateRow>
            ) : items.map((nic) => {
              const t = trafficFor(nic)
              const peak = t && nic.speed_mbps && nic.speed_mbps > 0
                ? (Math.max(t.rx || 0, t.tx || 0) / (nic.speed_mbps * 1e6)) * 100
                : null
              const virtual = isVirtualNic(nic)
              return (
                <Tr key={nic.if_name} className={rowCls}>
                  <Td className={cn(td, 'max-w-[280px] pl-4')}>
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span className="truncate font-medium text-text" title={nic.if_name}>{nic.if_name}</span>
                      {virtual && <span className="shrink-0 rounded bg-surface2 px-1 text-[10px] font-medium uppercase text-muted">virtual</span>}
                    </div>
                    <div className="truncate font-mono text-xs text-muted">{nic.mac_address || '—'}</div>
                  </Td>
                  <Td className={td}>
                    {nic.is_up ? <Pill tone="success">Up</Pill> : <Pill tone="danger">Down</Pill>}
                  </Td>
                  <Td className={cn(td, 'max-w-[280px] font-mono text-xs')}>
                    <InterfaceAddresses addresses={nic.ip_addresses || []} />
                  </Td>
                  {hasSpeed && (
                    <Td className={cn(td, 'whitespace-nowrap text-right tabular-nums text-text2')}>{formatLinkSpeed(nic.speed_mbps)}</Td>
                  )}
                  {/* Pseudo adapters report -1; that is "not applicable". */}
                  <Td className={cn(td, 'text-right tabular-nums text-text2')}>
                    {nic.mtu != null && nic.mtu > 0 ? nic.mtu : '—'}
                  </Td>
                  {hasTraffic && (
                    <Td
                      className={cn(td, 'whitespace-nowrap text-right text-xs tabular-nums')}
                      title={peak != null ? `Busiest direction at ${peak.toFixed(1)}% of link speed` : undefined}
                    >
                      {t ? (
                        <div className={cn('flex flex-col', peak != null && peak >= 80 ? 'text-warning' : 'text-text')}>
                          <span><span className="text-muted">↓</span> {t.rx != null ? formatBps(t.rx) : '—'}</span>
                          <span><span className="text-muted">↑</span> {t.tx != null ? formatBps(t.tx) : '—'}</span>
                        </div>
                      ) : <span className="text-muted">—</span>}
                    </Td>
                  )}
                  {hasTraffic && (
                    <Td className={cn(td, 'py-1')}>
                      {t && t.spark.some((v) => v != null) ? (
                        <div className="h-7 w-[110px]"><Sparkline data={t.spark} color={C.blue} height={28} /></div>
                      ) : <span className="text-xs text-muted">—</span>}
                    </Td>
                  )}
                  <Td className={cn(td, 'whitespace-nowrap pr-4 text-right text-xs text-muted')} title={new Date(nic.updated_at).toLocaleString()}>
                    {relativeTime(nic.updated_at)}
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      </Panel>

      <SectionHeading
        title={<span className="inline-flex items-center gap-2"><Radio className="h-4 w-4 text-muted" />Traffic capture</span>}
        hint="Run a time-boxed flow capture on the agent to see which processes and peers generate traffic."
      />
      <NetworkCapturePanel
        serverId={server.id}
        serverName={server.display_name}
        interfaces={items}
        agentStatus={server.agent_status}
        agentVersion={server.agent_version}
        platform={server.os_type}
        agentCapabilities={server.agent_capabilities ?? []}
      />
    </div>
  )
}

/* ── Software ────────────────────────────────────────────────────── */

const SOFTWARE_CSV = [
  { header: 'Package', value: (s: ServerSoftware) => s.package_name },
  { header: 'Version', value: (s: ServerSoftware) => s.version },
  { header: 'Vendor', value: (s: ServerSoftware) => s.vendor },
  { header: 'Installed', value: (s: ServerSoftware) => s.install_date },
  { header: 'Last reported', value: (s: ServerSoftware) => s.updated_at },
]

type SwSort = 'package_name' | 'vendor' | 'install_date'

function SoftwareSection({ server, swQ }: { server: ServerItem; swQ: SwQuery }) {
  const [q, setQ] = useState('')
  const [sortBy, setSortBy] = useState<SwSort>('package_name')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const { data, isLoading, isError, error, refetch } = swQ
  const items = useMemo(() => data?.items || [], [data])
  // Each package with a catalog key links to its Inventory product page (inventory.view); the others stay text.
  const canInventory = useCan()('inventory.view')
  // Two different cuts: the agent stopped listing at its cap, or this response stopped at its request limit.
  const agentTruncated = data?.agent_truncated === true
  const responseTruncated = data?.truncated === true

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const base = needle
      ? items.filter((s) =>
          s.package_name.toLowerCase().includes(needle) ||
          (s.vendor || '').toLowerCase().includes(needle) ||
          (s.version || '').toLowerCase().includes(needle))
      : items
    const dir = sortDir === 'asc' ? 1 : -1
    if (sortBy === 'install_date') {
      // Real dates, so yyyymmdd and ISO values order together; unknown last.
      return [...base].sort((a, b) => {
        const da = parseInstallDate(a.install_date)?.getTime()
        const db = parseInstallDate(b.install_date)?.getTime()
        if (da == null && db == null) return cmp(a.package_name, b.package_name)
        if (da == null) return 1
        if (db == null) return -1
        return dir * (da - db)
      })
    }
    return [...base].sort((a, b) => dir * cmp(a[sortBy], b[sortBy]))
  }, [items, q, sortBy, sortDir])

  const pager = usePagedRows(filtered, 50)
  const toggleSort = (col: SwSort) => {
    if (sortBy === col) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortBy(col); setSortDir(col === 'install_date' ? 'desc' : 'asc') }
    pager.reset()
  }

  const vendors = useMemo(() => new Set(items.map((s) => (s.vendor || '').trim().toLowerCase()).filter(Boolean)).size, [items])
  const recentCount = useMemo(() => {
    const cutoff = Date.now() - 30 * 86_400_000
    return items.filter((s) => (parseInstallDate(s.install_date)?.getTime() ?? 0) >= cutoff).length
  }, [items])
  const lastReported = useMemo(
    () => items.reduce<string | null>((m, s) => (!m || s.updated_at > m ? s.updated_at : m), null),
    [items],
  )
  const recentActive = sortBy === 'install_date' && sortDir === 'desc'

  return (
    <div className="space-y-4">
      {items.length > 0 && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile icon={Package} label="Packages" value={items.length.toLocaleString()} sub="Installed on this host" />
          <StatTile icon={Users} label="Vendors" value={vendors.toLocaleString()} sub="Distinct publishers" />
          <StatTile
            icon={CalendarClock} label="Installed in 30 days" value={recentCount}
            tone={recentCount > 0 ? 'info' : 'default'}
            sub={recentActive ? 'Sorted newest first' : 'Click to sort newest first'}
            active={recentActive}
            onClick={() => { setSortBy('install_date'); setSortDir('desc'); pager.reset() }}
          />
          <StatTile
            icon={Clock3} label="Last reported" value={lastReported ? relativeTime(lastReported) : '—'}
            sub="Inventory snapshot, every 6 hours by default"
          />
        </div>
      )}

      {(agentTruncated || responseTruncated) && (
        <div role="status" className="space-y-1 rounded-lg border border-warning/35 bg-warning/10 px-3 py-2 text-xs text-warning-fg">
          {agentTruncated && (
            <p className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>The agent reported its first 1,000 software titles only; titles beyond that are not listed.</span>
            </p>
          )}
          {responseTruncated && (
            <p className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>
                Showing the first {items.length.toLocaleString()} of {(data?.total ?? items.length).toLocaleString()} stored
                packages; filtering and export cover only these.
              </span>
            </p>
          )}
        </div>
      )}

      <Panel
        title="Installed software" icon={Package} flush
        subtitle={items.length ? `${filtered.length.toLocaleString()} of ${items.length.toLocaleString()} packages` : undefined}
        actions={(
          <>
            <div className="relative w-[260px] max-w-full">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input
                className="h-8 pl-8 text-[13px]"
                placeholder="Filter packages, vendors or versions…"
                value={q}
                onChange={(e) => { setQ(e.target.value); pager.reset() }}
              />
            </div>
            <ExportCsvButton rows={filtered} columns={SOFTWARE_CSV} filename={`${server.display_name}-software.csv`} />
          </>
        )}
      >
        <Table>
          <THead className="bg-surface2/40">
            <Tr className="border-border/60 hover:bg-transparent">
              <Th className={cn(th, 'pl-4', sortableTh)} onClick={() => toggleSort('package_name')}>
                Package{sortIndicator(sortBy === 'package_name', sortDir)}
              </Th>
              <Th className={th}>Version</Th>
              <Th className={cn(th, sortableTh)} onClick={() => toggleSort('vendor')}>
                Vendor{sortIndicator(sortBy === 'vendor', sortDir)}
              </Th>
              <Th className={cn(th, sortableTh, 'text-right')} onClick={() => toggleSort('install_date')}>
                Installed{sortIndicator(sortBy === 'install_date', sortDir)}
              </Th>
              <Th className={cn(th, 'pr-4 text-right')}>Last reported</Th>
            </Tr>
          </THead>
          <TBody>
            {isError ? (
              <TableStateRow colSpan={5}><QueryError error={error} onRetry={() => refetch()} /></TableStateRow>
            ) : isLoading ? (
              <TableStateRow colSpan={5}><div className="py-6"><Skeleton className="h-24 w-full" /></div></TableStateRow>
            ) : filtered.length === 0 ? (
              <TableStateRow colSpan={5}>
                <EmptyState
                  icon={Package}
                  title={items.length ? 'No packages match this filter' : 'No software inventory yet'}
                  hint={items.length
                    ? 'Clear the filter to see the full inventory.'
                    : 'The Windows agent uploads the software list with its periodic inventory snapshot (every 6 hours by default). Linux servers and servers without the agent do not report software yet.'}
                  action={items.length ? (
                    <Button size="sm" variant="outline" onClick={() => { setQ(''); pager.reset() }}>Clear filter</Button>
                  ) : undefined}
                />
              </TableStateRow>
            ) : pager.pageRows.map((s) => {
              const installed = parseInstallDate(s.install_date)
              return (
                <Tr key={`${s.package_name}::${s.version || ''}`} className={rowCls}>
                  <Td className={cn(td, 'max-w-[380px] pl-4')}>
                    {canInventory && s.product_key ? (
                      <Link
                        to={productPath(s.product_key)}
                        className="block truncate rounded-sm font-medium text-primary underline decoration-primary/40 underline-offset-2 outline-none hover:decoration-primary focus-visible:ring-2 focus-visible:ring-primary"
                        title={`${s.package_name} — open in the Inventory software catalog`}
                      >
                        {s.package_name}
                      </Link>
                    ) : (
                      <div className="truncate font-medium text-text" title={s.package_name}>{s.package_name}</div>
                    )}
                  </Td>
                  <Td className={cn(td, 'max-w-[160px]')}>
                    <div className="truncate font-mono text-xs tabular-nums text-text2" title={s.version || undefined}>{s.version || '—'}</div>
                  </Td>
                  <Td className={cn(td, 'max-w-[240px]')}>
                    <div className="truncate text-text2" title={s.vendor || undefined}>{s.vendor || '—'}</div>
                  </Td>
                  <Td className={cn(td, 'whitespace-nowrap text-right tabular-nums text-text2')}>
                    {installed ? installed.toLocaleDateString() : s.install_date || '—'}
                  </Td>
                  <Td className={cn(td, 'whitespace-nowrap pr-4 text-right text-xs text-muted')} title={new Date(s.updated_at).toLocaleString()}>
                    {relativeTime(s.updated_at)}
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
        <TablePager {...pager} noun="packages" />
      </Panel>
    </div>
  )
}
