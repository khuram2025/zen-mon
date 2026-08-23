import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Bot, Download, Loader2, Power, Settings2, ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'

interface AgentProcess {
  id: string
  agent_id: string
  server_id: string
  hostname: string
  agent_status: string
  agent_version?: string
  agent_capabilities?: string[]
  runtime: string
  runtime_version?: string
  service_name_guess?: string
  windows_service?: string | null
  iis_site?: string | null
  iis_app_pool?: string | null
  instrumentation_state: 'none' | 'pending' | 'active' | 'failed' | 'unsupported'
  otel_detected: boolean
  otel_endpoint?: string | null
  last_seen_at: string
  last_command_status?: string | null
  last_command_error?: string | null
  configured_service_name?: string
  configured_environment?: string
  telemetry_status?: 'receiving' | 'stale' | 'waiting_for_first_trace' | 'not_configured'
  traces_15m?: number
  last_trace_at?: string | null
  last_deployment_at?: string | null
}

function isCommandPending(row: AgentProcess) {
  return ['queued', 'sent', 'running'].includes(row.last_command_status || '')
}

function stateBadge(row: AgentProcess) {
  if (row.instrumentation_state === 'active' || row.otel_detected) return <Badge variant="success">Profiler active</Badge>
  if (row.instrumentation_state === 'failed' || row.last_command_status === 'failed') return <Badge variant="danger">Needs attention</Badge>
  if (row.instrumentation_state === 'pending' || isCommandPending(row)) return <Badge variant="warning">Change pending</Badge>
  if (row.instrumentation_state === 'unsupported') return <Badge variant="outline">Unsupported</Badge>
  return <Badge variant="outline">Discovered</Badge>
}

function telemetryBadge(row: AgentProcess) {
  if (row.telemetry_status === 'receiving') return <Badge variant="success">Receiving · {row.traces_15m || 0}</Badge>
  if (row.telemetry_status === 'stale') return <Badge variant="warning">No recent traces</Badge>
  if (row.telemetry_status === 'waiting_for_first_trace') return <Badge variant="warning">Waiting for first trace</Badge>
  return <Badge variant="outline">No telemetry</Badge>
}

function InstrumentationDialog({ row, onClose }: { row: AgentProcess | null; onClose: () => void }) {
  const queryClient = useQueryClient()
  const currentlyEnabled = !!row && (row.instrumentation_state === 'active' || row.instrumentation_state === 'pending' || row.otel_detected)
  const [enabled, setEnabled] = useState(currentlyEnabled)
  const [restart, setRestart] = useState(true)
  const [serviceName, setServiceName] = useState(row?.configured_service_name || row?.service_name_guess || row?.iis_app_pool || row?.windows_service || '')
  const [environment, setEnvironment] = useState(row?.configured_environment || 'prod')

  useEffect(() => {
    setEnabled(currentlyEnabled)
    setRestart(true)
    setServiceName(row?.configured_service_name || row?.service_name_guess || row?.iis_app_pool || row?.windows_service || '')
    setEnvironment(row?.configured_environment || 'prod')
  }, [row, currentlyEnabled])

  const save = useMutation({
    mutationFn: async () => {
      if (!row) return
      return (await api.post(`/apm/agent-processes/${row.id}/instrumentation`, {
        enabled,
        restart,
        service_name: serviceName.trim(),
        environment: environment.trim(),
      })).data
    },
    onSuccess: () => {
      const targetType = row?.iis_app_pool ? 'IIS application pool' : 'Windows service'
      toast.success(enabled ? 'Instrumentation queued' : 'Removal queued', restart ? `The ${targetType} will restart once.` : 'The change activates on its next restart.')
      queryClient.invalidateQueries({ queryKey: ['apm', 'agent-processes'] })
      onClose()
    },
    onError: (error) => toast.error('Could not queue APM change', apiErrorMessage(error)),
  })

  return (
    <Dialog open={!!row} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Settings2 className="h-4 w-4 text-primary" /> Manage {row?.runtime === 'iis' ? 'IIS' : row?.runtime} instrumentation</DialogTitle>
          <DialogDescription>ZenPlus changes only <span className="font-medium text-text">{row?.iis_app_pool || row?.windows_service}</span>. Application files and source code remain untouched.</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="flex items-center justify-between rounded-lg border border-border bg-surface2/40 p-3">
            <div><div className="text-sm font-semibold text-text">Managed OpenTelemetry tracing</div><p className="mt-0.5 text-xs text-muted">Use the bundled, fully offline {row?.runtime} runtime pack.</p></div>
            <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Managed OpenTelemetry tracing" />
          </div>
          {enabled && (
            <div className="grid gap-3 sm:grid-cols-2">
              <FormField label="Service name" hint="How this application appears in APM."><Input value={serviceName} maxLength={255} onChange={(event) => setServiceName(event.target.value)} /></FormField>
              <FormField label="Environment" hint="For example: prod, staging, or test."><Input value={environment} maxLength={64} onChange={(event) => setEnvironment(event.target.value)} /></FormField>
            </div>
          )}
          <div className="flex items-center justify-between rounded-lg border border-border p-3">
            <div><div className="text-sm font-medium text-text">{row?.runtime === 'iis' ? 'Recycle application pool now' : 'Restart Windows service now'}</div><p className="mt-0.5 text-xs text-muted">Recommended. ZenPlus verifies that the target returns to a running state.</p></div>
            <Switch checked={restart} onCheckedChange={setRestart} aria-label="Restart target now" />
          </div>
          {!enabled && <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs leading-relaxed text-warning">ZenPlus will restore every environment value captured before instrumentation. No unrelated pool setting is removed.</div>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant={enabled ? 'default' : 'destructive'} disabled={save.isPending || (enabled && (!serviceName.trim() || !environment.trim()))} onClick={() => save.mutate()}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Power className="h-4 w-4" />}{enabled ? 'Enable tracing' : 'Disable and restore'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ApmAgentsTab() {
  const [installOpen, setInstallOpen] = useState(false)
  const [selected, setSelected] = useState<AgentProcess | null>(null)
  const processes = useQuery<AgentProcess[]>({ queryKey: ['apm', 'agent-processes'], queryFn: async () => (await api.get('/apm/agent-processes')).data, refetchInterval: 10_000 })
  const rows = processes.data ?? []
  const hosts = new Set(rows.map((row) => row.hostname)).size
  const active = rows.filter((row) => row.instrumentation_state === 'active' || row.otel_detected).length
  const pending = rows.filter(isCommandPending).length

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-4">
          <div><CardTitle className="flex items-center gap-2"><Bot className="h-4 w-4 text-primary" /> ZenPlus APM Agent</CardTitle><p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted">Discover applications, activate supported runtimes, and verify the profiler from one control plane. The package is fully offline; authorization and ingest credentials remain appliance-managed.</p></div>
          <Button onClick={() => setInstallOpen(true)}><Download className="h-4 w-4" /> Download APM installer</Button>
        </CardHeader>
        <CardContent><div className="grid gap-3 md:grid-cols-3">
          <div className="rounded-lg border border-border bg-surface2/30 p-3"><ShieldCheck className="mb-2 h-4 w-4 text-success" /><div className="text-xs font-semibold text-text">Controller-authorized</div><p className="mt-1 text-[11px] text-muted">No local token, site ID, or policy ID is required.</p></div>
          <div className="rounded-lg border border-border bg-surface2/30 p-3"><Activity className="mb-2 h-4 w-4 text-primary" /><div className="text-xs font-semibold text-text">Local OTLP gateway</div><p className="mt-1 text-[11px] text-muted">Loopback-only ports 4317 and 4318 buffer telemetry.</p></div>
          <div className="rounded-lg border border-border bg-surface2/30 p-3"><Settings2 className="mb-2 h-4 w-4 text-primary" /><div className="text-xs font-semibold text-text">Managed runtime lifecycle</div><p className="mt-1 text-[11px] text-muted">IIS, .NET services, Java, and Node.js with verified rollback.</p></div>
        </div></CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Discovered application runtimes</CardTitle><p className="text-xs text-muted">{hosts} reporting host{hosts === 1 ? '' : 's'} · {active} profiler active · {pending} change{pending === 1 ? '' : 's'} pending</p></CardHeader>
        <CardContent>
          {processes.isLoading ? <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading runtime inventory…</div>
            : processes.isError ? <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">Could not load runtime inventory — {apiErrorMessage(processes.error)}</div>
              : rows.length === 0 ? <div className="py-10 text-center text-sm text-muted">No application runtimes have reported yet. Install the APM profile, approve the agent, and allow up to one minute for discovery.</div>
                : <Table><THead><Tr><Th>Host</Th><Th>Application</Th><Th>Runtime</Th><Th>Instrumentation</Th><Th>Telemetry</Th><Th>Last seen</Th><Th className="text-right">Action</Th></Tr></THead><TBody>{rows.map((row) => {
                  const isIIS = row.runtime === 'iis' && !!row.iis_app_pool
                  const isManagedServiceRuntime = ['dotnet', 'dotnet_framework', 'java', 'node'].includes(row.runtime) && !!row.windows_service
                  const capable = isIIS
                    ? (row.agent_capabilities || []).includes('apm_iis_instrumentation_v1')
                    : (row.agent_capabilities || []).includes('apm_windows_service_instrumentation_v1')
                  const manageable = (isIIS || isManagedServiceRuntime) && capable
                  const actionLabel = manageable ? 'Manage'
                    : (isIIS || isManagedServiceRuntime) ? 'Agent upgrade required'
                      : row.runtime === 'python' ? 'Compatibility gated' : 'Manual setup'
                  return <Tr key={row.id}>
                    <Td><div className="font-medium text-text">{row.hostname}</div><div className="text-[11px] text-muted">Agent {row.agent_status}{row.agent_version ? ` · v${row.agent_version}` : ''}</div></Td>
                    <Td><div className="font-medium text-text">{row.configured_service_name || row.service_name_guess || 'Unnamed process'}</div>{row.iis_app_pool && <div className="text-[11px] text-muted">IIS pool · {row.iis_app_pool}</div>}{row.windows_service && <div className="text-[11px] text-muted">Windows service · {row.windows_service}</div>}{row.last_deployment_at && <div className="mt-1 text-[11px] font-medium text-primary">Deployment detected {relativeTime(row.last_deployment_at)}</div>}{row.last_command_error && <div className="mt-1 max-w-xs text-[11px] text-danger">{row.last_command_error}</div>}</Td>
                    <Td><Badge variant="outline">{row.runtime}{row.runtime_version ? ` ${row.runtime_version}` : ''}</Badge></Td><Td>{stateBadge(row)}</Td><Td><div>{telemetryBadge(row)}</div>{row.last_trace_at && <div className="mt-1 text-[11px] text-muted">Last {relativeTime(row.last_trace_at)}</div>}</Td><Td className="text-xs text-muted">{relativeTime(row.last_seen_at)}</Td>
                    <Td className="text-right"><Button variant="outline" size="sm" disabled={!manageable || isCommandPending(row)} onClick={() => setSelected(row)}>{isCommandPending(row) ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Settings2 className="h-3.5 w-3.5" />}{actionLabel}</Button></Td>
                  </Tr>
                })}</TBody></Table>}
        </CardContent>
      </Card>
      <InstrumentationDialog row={selected} onClose={() => setSelected(null)} />
      <InstallTokenDialog open={installOpen} onOpenChange={setInstallOpen} installProfile="apm" />
    </div>
  )
}
