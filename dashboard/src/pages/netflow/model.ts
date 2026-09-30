import { Activity, AlertTriangle, Shield, ShieldAlert, Signal, WifiOff, Zap } from 'lucide-react'
import { formatBps, formatBytes, relativeTime } from '@/lib/utils'
import type { Overview, NetflowInterfaceRef, Application, Exporter, DeviceStatus, Alert } from './types'

// The traffic heatmap is always a 7-day hour×day grid. Any "When" (hour/dow)
// filter picked from it therefore needs a page range at least this wide to
// match anything — see the onSelect handler and the range guard below.
const HEATMAP_HOURS = 168

const TALKER_COLORS = ['#22d3ee', '#a78bfa', '#f472b6', '#34d399', '#facc15', '#fb923c']
const APP_COLORS = ['#22d3ee', '#34d399', '#facc15', '#fb7185', '#a78bfa', '#f59e0b', '#10b981', '#ef4444']
const PROTO_COLORS = ['#22d3ee', '#a78bfa', '#facc15', '#fb7185', '#34d399', '#fb923c']
const CONV_COLORS = ['#22d3ee', '#34d399', '#facc15', '#a78bfa', '#fb7185', '#fb923c']

// Auto-refresh cadence scaled to the window: multi-day windows run multi-second
// ClickHouse scans — re-polling those every 15s just burns the database.
function pollMs(hours: number, floor = 15_000): number | false {
  const ms = hours <= 6 ? 15_000 : hours <= 48 ? 60_000 : 300_000
  return Math.max(ms, floor)
}

function normalizeInterfaceRefs(resolved?: NetflowInterfaceRef[], indexes?: number[]): NetflowInterfaceRef[] {
  if (resolved?.length) return dedupeInterfaceRefs(resolved)
  return dedupeInterfaceRefs((indexes || []).filter(Boolean).map((ifindex) => ({ exporter_ip: '', ifindex })))
}

function dedupeInterfaceRefs(values: NetflowInterfaceRef[]): NetflowInterfaceRef[] {
  const seen = new Set<string>()
  const out: NetflowInterfaceRef[] = []
  for (const value of values) {
    const key = `${value.exporter_ip || 'unknown'}-${value.ifindex}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(value)
  }
  return out
}

function interfaceLabel(value: Pick<NetflowInterfaceRef, 'display_name' | 'if_name' | 'if_descr' | 'if_alias' | 'ifindex'>): string {
  return value.display_name || value.if_name || value.if_descr || value.if_alias || `ifIndex ${value.ifindex}`
}

function describeTcpFlags(flags: number): { primary: string; detail: string } {
  if (!flags) return { primary: 'None', detail: 'No TCP control flags' }
  const names: [number, string][] = [
    [1, 'FIN'],
    [2, 'SYN'],
    [4, 'RST'],
    [8, 'PSH'],
    [16, 'ACK'],
    [32, 'URG'],
  ]
  const found = names.filter(([bit]) => (flags & bit) !== 0).map(([, name]) => name)
  return {
    primary: found.slice(0, 3).join(' + ') || 'TCP',
    detail: found.length > 3 ? `${found.length} flags: ${found.join(', ')}` : found.join(', ') || `mask ${flags}`,
  }
}

function inferApplication(port: number): string {
  if ([80, 443, 8080, 8443].includes(port)) return 'Web (HTTP/HTTPS)'
  if ([554, 1755, 1935, 5004, 5005, 8000, 8554, 5060, 5061].includes(port)) return 'Streaming Media'
  if (port === 53) return 'DNS'
  if ([25, 110, 143, 465, 587, 993, 995].includes(port)) return 'Email (SMTP/IMAP/POP)'
  if ([20, 21, 69, 115, 445, 2049].includes(port)) return 'File Transfer (FTP/SMB)'
  if ([22, 23, 3389, 5900, 5938].includes(port)) return 'Remote Access (SSH/RDP/Telnet)'
  if ([1433, 1521, 3306, 5432, 6379, 9042, 27017].includes(port)) return 'Database'
  if ([1719, 1720, 3478, 3479, 5060, 5061, 16384, 19302].includes(port)) return 'VoIP / Video Conf.'
  if ([123, 161, 162, 514, 6343].includes(port)) return 'Network Mgmt'
  if (port >= 1 && port <= 1023) return 'System Services'
  return 'Other'
}

function describePortClass(port: number): string {
  if (port === 0) return 'No TCP/UDP destination port was reported.'
  if (port >= 1 && port <= 1023) return 'Well-known system port.'
  if (port >= 1024 && port <= 49151) return 'Registered/user port. Application is inferred only if the port is in the known map.'
  return 'Dynamic/private port, commonly used by clients or custom services.'
}

function buildAlerts(overview?: Overview, exporters: Exporter[] = [], device?: DeviceStatus, applications: Application[] = []): Alert[] {
  const list: Alert[] = []
  const now = Date.now()
  for (const e of exporters) {
    const age = now - new Date(e.last_seen).getTime()
    if (age >= 15 * 60_000) {
      list.push({
        id: `down-${e.exporter_ip}`,
        title: 'Device Offline',
        body: `${e.exporter_ip} stopped exporting flows`,
        severity: 'critical',
        icon: WifiOff,
        ts: e.last_seen,
      })
    } else if (age >= 2 * 60_000) {
      list.push({
        id: `stale-${e.exporter_ip}`,
        title: 'Stale Exporter',
        body: `${e.exporter_ip} last seen ${relativeTime(e.last_seen)}`,
        severity: 'warning',
        icon: AlertTriangle,
        ts: e.last_seen,
      })
    }
  }
  if (overview && overview.current_bps > 0) {
    if (overview.current_bps > 100_000_000) {
      list.push({
        id: 'high-bw',
        title: 'High Bandwidth Usage',
        body: `${formatBps(overview.current_bps)} aggregate rate`,
        severity: 'warning',
        icon: Zap,
      })
    } else {
      list.push({
        id: 'throughput-ok',
        title: 'Throughput Healthy',
        body: `${formatBps(overview.current_bps)} aggregate rate`,
        severity: 'success',
        icon: Activity,
      })
    }
  }
  if (device && device.packet_loss_pct >= 5) {
    list.push({
      id: 'rst-ratio',
      title: 'High TCP RST Ratio',
      body: `${device.packet_loss_pct.toFixed(2)}% of TCP flows carried RST (aborts/probes)`,
      severity: 'warning',
      icon: ShieldAlert,
    })
  }
  const suspicious = applications.find((a) => a.name === 'System Services')
  if (suspicious && suspicious.bytes > 0) {
    list.push({
      id: 'unauth',
      title: 'Privileged Service Traffic',
      body: `${formatBytes(suspicious.bytes)} on system ports`,
      severity: 'info',
      icon: Shield,
    })
  }
  if (overview?.flows && overview.flows > 1_000_000) {
    list.push({
      id: 'flows-high',
      title: 'Large Flow Volume',
      body: `${overview.flows.toLocaleString()} flows in window`,
      severity: 'info',
      icon: Signal,
    })
  }
  return list.slice(0, 6)
}

function formatBitsPerSecond(bps: number): string {
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  let v = bps; let i = 0
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

const DSCP_COLORS = ['#22d3ee', '#34d399', '#facc15', '#fb7185', '#a78bfa', '#fb923c', '#06b6d4', '#10b981', '#ef4444']

const NETCLASS_COLORS: Record<string, string> = {
  'Private 10/8': '#22d3ee',
  'Private 172.16/12': '#34d399',
  'Private 192.168/16': '#facc15',
  'Public': '#fb7185',
  'Multicast': '#a78bfa',
  'Loopback': '#94a3b8',
  'Link-local': '#fb923c',
  'CGNAT': '#06b6d4',
}

function flagEmoji(cc: string | null): string {
  if (!cc || cc.length !== 2) return '🌐'
  const base = 0x1f1e6
  const a = 'A'.charCodeAt(0)
  return String.fromCodePoint(base + cc.charCodeAt(0) - a, base + cc.charCodeAt(1) - a)
}

export { HEATMAP_HOURS, TALKER_COLORS, APP_COLORS, PROTO_COLORS, CONV_COLORS, pollMs, normalizeInterfaceRefs, interfaceLabel, describeTcpFlags, inferApplication, describePortClass, buildAlerts, formatBitsPerSecond, DSCP_COLORS, NETCLASS_COLORS, flagEmoji }
