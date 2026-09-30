import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Cable } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import type { ExporterDevice } from '../types'

/* ─────────────────────────────────────────────────────────────────
   Exporter → device mapping

   Flow records identify interfaces only by (exporter IP, ifIndex). Names come
   from SNMP, keyed on the device's management IP, so an exporter that sources
   flows from a loopback or WAN address resolves to nothing and the UI falls
   back to "ifIndex 16". This links the two so real names appear.
   ───────────────────────────────────────────────────────────────── */

function ExporterDeviceDialog({
  open, onOpenChange, exporters,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  exporters: ExporterDevice[]
}) {
  const qc = useQueryClient()
  const [picked, setPicked] = useState<Record<string, string>>({})

  const devices = useQuery<{ data: { id: string; hostname: string; ip_address: string }[] }>({
    queryKey: ['devices', 'for-exporter-map'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    enabled: open,
  })

  const save = useMutation({
    mutationFn: async ({ ip, deviceId }: { ip: string; deviceId: string }) =>
      api.put(`/netflow/exporter-devices/${ip}`, { device_id: deviceId }),
    onSuccess: (_d, v) => {
      toast.success('Exporter linked', `Interface names for ${v.ip} will resolve from now on.`)
      qc.invalidateQueries({ queryKey: ['netflow'] })
    },
    onError: (e: any) => toast.error('Could not link exporter', apiErrorMessage(e)),
  })

  const unlink = useMutation({
    mutationFn: async (ip: string) => api.delete(`/netflow/exporter-devices/${ip}`),
    onSuccess: () => {
      toast.success('Mapping removed')
      qc.invalidateQueries({ queryKey: ['netflow'] })
    },
    onError: (e: any) => toast.error('Could not remove mapping', apiErrorMessage(e)),
  })

  const deviceList = devices.data?.data || []
  const selectCls =
    'h-8 min-w-[200px] rounded-md border border-border bg-surface px-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40'

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Cable className="h-4 w-4 text-primary" />
            Flow exporters
          </DialogTitle>
          <DialogDescription>
            Interface names come from SNMP and are matched to flows by the exporter's IP. A device
            that exports from a loopback or WAN address will not match its own management IP — link
            it here so its ifIndex values resolve to real names.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5">
          {exporters.length === 0 && (
            <p className="text-xs text-muted">No exporters have sent flows in the last 7 days.</p>
          )}
          {exporters.map((e) => (
            <div
              key={e.exporter_ip}
              className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-xs"
            >
              <div className="min-w-0 flex-1">
                <div className="font-mono font-medium">{e.exporter_ip}</div>
                <div className="text-[11px] text-muted">
                  {e.flows.toLocaleString()} flows
                  {e.source === 'ip-match' && ` · matches ${e.device_hostname} by management IP`}
                  {e.source === 'mapped' && ` · linked to ${e.device_hostname}`}
                  {!e.source && ' · not matched to any device — shows raw ifIndex'}
                </div>
              </div>

              {e.source === 'ip-match' ? (
                <span className="rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[11px] text-success">
                  Resolved
                </span>
              ) : e.source === 'mapped' ? (
                <Button
                  variant="outline" size="sm" className="h-8"
                  disabled={unlink.isPending}
                  onClick={() => unlink.mutate(e.exporter_ip)}
                >
                  Unlink
                </Button>
              ) : (
                <>
                  <select
                    className={selectCls}
                    value={picked[e.exporter_ip] || ''}
                    onChange={(ev) => setPicked((p) => ({ ...p, [e.exporter_ip]: ev.target.value }))}
                  >
                    <option value="">Select device…</option>
                    {deviceList.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.hostname} ({d.ip_address})
                      </option>
                    ))}
                  </select>
                  <Button
                    size="sm" className="h-8"
                    disabled={!picked[e.exporter_ip] || save.isPending}
                    onClick={() => save.mutate({ ip: e.exporter_ip, deviceId: picked[e.exporter_ip] })}
                  >
                    Link
                  </Button>
                </>
              )}
            </div>
          ))}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export { ExporterDeviceDialog }
