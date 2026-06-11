import {
  AlertTriangle,
  CheckCircle2,
  CircleDot,
  Clock,
  Pause,
  PlayCircle,
  ShieldAlert,
  XCircle,
} from 'lucide-react'
import { Badge } from '@/components/ui/Badge'
import {
  CredentialStatus,
  DiscoveryProtocol,
  ResultStatus,
  RunStatus,
} from './types'

const STATUS_LABELS: Record<RunStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  partial: 'Partial',
}

export function RunStatusBadge({ status }: { status: RunStatus | null | undefined }) {
  if (!status) return <Badge variant="outline">never run</Badge>
  const tone =
    status === 'completed' ? 'success' :
    status === 'failed' ? 'danger' :
    status === 'cancelled' ? 'outline' :
    status === 'partial' ? 'warning' :
    'info'
  const Icon =
    status === 'completed' ? CheckCircle2 :
    status === 'failed' ? XCircle :
    status === 'cancelled' ? Pause :
    status === 'partial' ? AlertTriangle :
    status === 'running' ? CircleDot :
    Clock
  return (
    <Badge variant={tone as any}>
      <Icon className="h-3 w-3" />
      {STATUS_LABELS[status]}
    </Badge>
  )
}

const RESULT_STATUS_TONE: Record<ResultStatus, 'success' | 'info' | 'warning' | 'danger' | 'outline' | 'default'> = {
  new: 'info',
  existing: 'outline',
  changed: 'warning',
  unknown: 'default',
  ignored: 'outline',
  failed: 'danger',
  imported: 'success',
}

const RESULT_STATUS_LABEL: Record<ResultStatus, string> = {
  new: 'New',
  existing: 'Existing',
  changed: 'Changed',
  unknown: 'Unknown',
  ignored: 'Ignored',
  failed: 'Failed',
  imported: 'Imported',
}

export function ResultStatusBadge({ status }: { status: ResultStatus }) {
  return (
    <Badge variant={RESULT_STATUS_TONE[status] as any}>
      {RESULT_STATUS_LABEL[status]}
    </Badge>
  )
}

const CRED_TONE: Record<CredentialStatus, 'success' | 'warning' | 'danger' | 'outline'> = {
  valid: 'success',
  partial: 'warning',
  permission_issue: 'warning',
  invalid: 'danger',
  not_tested: 'outline',
}
const CRED_LABEL: Record<CredentialStatus, string> = {
  valid: 'Valid',
  partial: 'Partial',
  permission_issue: 'Permission issue',
  invalid: 'Invalid',
  not_tested: 'Not tested',
}

export function CredentialStatusBadge({ status }: { status: CredentialStatus }) {
  return <Badge variant={CRED_TONE[status]}>{CRED_LABEL[status]}</Badge>
}

const PROTOCOL_LABELS: Record<DiscoveryProtocol, string> = {
  icmp: 'ICMP',
  snmp: 'SNMP',
  ssh: 'SSH',
  wmi: 'WMI',
  winrm: 'WinRM',
  http: 'HTTP',
  https: 'HTTPS',
  tcp: 'TCP',
}

export function ProtocolPill({ p }: { p: string }) {
  const k = (p as DiscoveryProtocol) || 'tcp'
  return (
    <span className="inline-flex items-center rounded-md border border-border bg-surface2 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-muted">
      {PROTOCOL_LABELS[k] || p}
    </span>
  )
}

export function PhaseLabel({ phase }: { phase: string }) {
  const map: Record<string, string> = {
    preparing: 'Preparing',
    validating: 'Validating credentials',
    scanning: 'Scanning IPs',
    identifying: 'Identifying devices',
    matching: 'Matching existing inventory',
    applying_rules: 'Applying rules',
    reporting: 'Generating report',
    done: 'Complete',
  }
  return <span>{map[phase] || phase}</span>
}

export function formatScope(profile: { scope_type: string; targets: string[] }) {
  const t = profile.targets || []
  if (!t.length) return '—'
  if (t.length === 1) return t[0]
  return `${t[0]} +${t.length - 1} more`
}

export function describeNextRun(iso: string | null | undefined): string {
  if (!iso) return 'Not scheduled'
  const ts = new Date(iso).getTime()
  if (isNaN(ts)) return iso
  const diff = ts - Date.now()
  if (diff <= 0) return 'Imminent'
  if (diff < 60_000) return 'In <1m'
  if (diff < 3_600_000) return `In ${Math.round(diff / 60_000)}m`
  if (diff < 86_400_000) return `In ${Math.round(diff / 3_600_000)}h`
  return new Date(iso).toLocaleString()
}
