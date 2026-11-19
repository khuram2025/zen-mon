import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Activity, Loader2, RefreshCw, Shield, Zap } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { MaintenanceDialog } from '@/components/maintenance/MaintenanceDialog'
import type { MaintenanceWindow } from '@/components/maintenance/model'
import { formatSpan } from './format'
import type { DeviceRecord } from './types'

/* ── Check now / diagnostics ────────────────────────────────────────────── */

export type DiagnosticsResult = { ping: any | null; snmp: any | null }
export type DiagnosticKind = 'all' | 'ping' | 'snmp'

/** Live reachability checks run from the server on demand. "Check now" runs
 *  both; the Diagnostics menu runs one. Afterwards the page's data is reloaded
 *  so the result is reflected without waiting for the next poll. */
export function useDiagnostics(device: DeviceRecord) {
  const qc = useQueryClient()
  const [result, setResult] = useState<DiagnosticsResult | null>(null)
  const [kind, setKind] = useState<DiagnosticKind>('all')
  const run = useMutation({
    mutationFn: async (which: DiagnosticKind): Promise<DiagnosticsResult> => {
      const failed = (e: unknown) => ({ ok: false, reason: apiErrorMessage(e) })
      const ping = which !== 'snmp'
        ? api.post(`/devices/${device.id}/ping-test`).then((r) => r.data, failed)
        : Promise.resolve(null)
      const snmp = which !== 'ping' && device.snmp_enabled
        ? api.post(`/devices/${device.id}/snmp-test`).then((r) => r.data, failed)
        : Promise.resolve(null)
      const [pingRes, snmpRes] = await Promise.all([ping, snmp])
      return { ping: pingRes, snmp: snmpRes }
    },
    onMutate: (which) => { setKind(which); setResult(null) },
    onSuccess: (data) => {
      setResult(data)
      qc.invalidateQueries({ queryKey: ['device', device.id] })
      qc.invalidateQueries({ queryKey: ['device-status-explanation', device.id] })
    },
    onError: (e: unknown) => toast.error('Checks failed', apiErrorMessage(e)),
  })
  return { run, result, kind }
}

export function DiagnosticsDialog({
  open, onOpenChange, running, result, kind, snmpEnabled, onRerun,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  running: boolean
  result: DiagnosticsResult | null
  kind: DiagnosticKind
  snmpEnabled: boolean
  onRerun: () => void
}) {
  const ping = result?.ping
  const snmp = result?.snmp
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Zap className="h-5 w-5 text-primary" />{kind === 'all' ? 'Check now' : kind === 'ping' ? 'Ping test' : 'SNMP test'}</DialogTitle>
          <DialogDescription>Live checks run from the ZenPlus server. ICMP and SNMP are tested independently: one can pass while the other fails. The confirmed status still follows the confirmation policy.</DialogDescription>
        </DialogHeader>
        {running || !result ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />Running checks…</div>
        ) : (
          <div className="space-y-2">
            {kind !== 'snmp' && (
              <DiagnosticRow icon={Activity} label="ICMP ping" ok={!!ping?.ok}
                detail={ping?.ok ? `${ping.received}/${ping.transmitted} replies · ${ping.rtt_avg_ms != null ? `${Number(ping.rtt_avg_ms).toFixed(1)} ms average` : 'no round-trip time'}` : ping?.reason || 'No reply'} />
            )}
            {kind !== 'ping' && (snmpEnabled ? (
              <DiagnosticRow icon={Shield} label="SNMP GET" ok={!!snmp?.ok}
                detail={snmp?.ok ? (snmp.sys_descr || 'Responded') : snmp?.reason || 'No response'}
                extra={!snmp?.ok && snmp?.snmp_error && snmp.snmp_error !== snmp.reason ? snmp.snmp_error : snmp?.ok && snmp?.sys_uptime_seconds != null ? `sysUpTime ${formatSpan(snmp.sys_uptime_seconds)}` : undefined} />
            ) : (
              <DiagnosticRow icon={Shield} label="SNMP GET" ok={null} detail="SNMP is disabled for this device" />
            ))}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
          <Button size="sm" disabled={running} onClick={onRerun}>
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}Run again
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DiagnosticRow({ icon: Icon, label, ok, detail, extra }: {
  icon: React.ComponentType<{ className?: string }>; label: string; ok: boolean | null; detail: string; extra?: string
}) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-border bg-surface2/40 p-3">
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${ok == null ? 'text-muted' : ok ? 'text-success' : 'text-danger'}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{label}</span>
          <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${ok == null ? 'bg-surface2 text-muted' : ok ? 'bg-success/15 text-success' : 'bg-danger/15 text-danger'}`}>
            {ok == null ? 'Skipped' : ok ? 'Pass' : 'Fail'}
          </span>
        </div>
        <div className="mt-0.5 break-words text-sm text-muted">{detail}</div>
        {extra && <div className="mt-1 break-words rounded bg-surface2 px-1.5 py-1 font-mono text-xs text-muted">{extra}</div>}
      </div>
    </div>
  )
}

/* ── Acknowledge open alerts ────────────────────────────────────────────── */

export function useAcknowledgeAll(deviceId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const { data } = await api.get(`/alerts?device_id=${deviceId}&status=active&limit=200`)
      const ids: string[] = (data?.data || []).map((a: { id: string }) => a.id)
      // Small batches: a device with dozens of open alerts would otherwise fire that many requests at once.
      let done = 0
      for (let i = 0; i < ids.length; i += 8) {
        await Promise.all(ids.slice(i, i + 8).map((id) => api.post(`/alerts/${id}/acknowledge`)))
        done += Math.min(8, ids.length - i)
      }
      return done
    },
    onSuccess: (n) => {
      toast.success('Alerts acknowledged', `${n} alert${n === 1 ? '' : 's'} acknowledged`)
      qc.invalidateQueries({ queryKey: ['device', deviceId] })
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: unknown) => toast.error('Acknowledge failed', apiErrorMessage(e)),
  })
}

/* ── Configuration backup (NCM) ─────────────────────────────────────────── */

export function useConfigBackup(deviceId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/config-fetch`)).data,
    onSuccess: (data: any) => {
      toast.success('Backup complete', data?.is_change === false ? 'No change since the last backup' : 'New configuration version saved')
      qc.invalidateQueries({ queryKey: ['device', deviceId, 'configs'] })
    },
    onError: (e: unknown) => toast.error('Backup failed', apiErrorMessage(e)),
  })
}

/* ── Maintenance windows ────────────────────────────────────────────────── */

/** The device page's maintenance dialog (one-off and recurring windows):
 *  components/maintenance/MaintenanceDialog for this device. */
export function DeviceMaintenanceDialog({
  open, onOpenChange, device, windows,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  device: DeviceRecord
  windows?: { active: MaintenanceWindow[]; upcoming: MaintenanceWindow[] }
}) {
  return (
    <MaintenanceDialog
      open={open}
      onOpenChange={onOpenChange}
      asset={{ kind: 'device', id: device.id, name: device.hostname }}
      windows={windows}
      queryKeys={[['device-maintenance', device.id], ['device', device.id], ['devices']]}
    />
  )
}
