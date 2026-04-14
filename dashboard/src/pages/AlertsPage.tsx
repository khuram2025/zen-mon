import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, X } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'

export function AlertsPage() {
  const qc = useQueryClient()
  const [status, setStatus] = useState<'active' | 'acknowledged' | 'resolved'>('active')

  const { data: stats } = useQuery<any>({
    queryKey: ['alerts', 'stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  })

  const { data: alerts } = useQuery<any[]>({
    queryKey: ['alerts', status],
    queryFn: async () => {
      const r = (await api.get(`/alerts?status=${status}&limit=100`)).data
      return Array.isArray(r) ? r : r?.data || []
    },
    refetchInterval: 15_000,
  })

  const ack = useMutation({
    mutationFn: async (id: string) => api.post(`/alerts/${id}/acknowledge`),
    onSuccess: () => {
      toast.success('Alert acknowledged')
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => toast.error('Acknowledge failed', apiErrorMessage(e)),
  })

  const resolve = useMutation({
    mutationFn: async (id: string) => api.post(`/alerts/${id}/resolve`),
    onSuccess: () => {
      toast.success('Alert resolved')
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => toast.error('Resolve failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <AlertTriangle className="h-5 w-5 text-warning" />
          Alerts
        </h1>
        <p className="text-xs text-muted">
          {stats?.active ?? 0} active • {stats?.acknowledged ?? 0} acknowledged • {stats?.resolved_today ?? 0} resolved today
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Active" value={stats?.active ?? 0} tone={(stats?.active ?? 0) > 0 ? 'danger' : undefined} />
        <Stat label="Acknowledged" value={stats?.acknowledged ?? 0} />
        <Stat label="Critical (24h)" value={stats?.critical ?? 0} tone={(stats?.critical ?? 0) > 0 ? 'danger' : undefined} />
        <Stat label="Warning (24h)" value={stats?.warning ?? 0} tone={(stats?.warning ?? 0) > 0 ? 'warning' : undefined} />
      </div>

      <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5 w-fit">
        {(['active', 'acknowledged', 'resolved'] as const).map((s) => (
          <button
            key={s}
            onClick={() => setStatus(s)}
            className={`rounded px-3 py-1.5 text-xs font-medium capitalize transition-colors ${
              status === s ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text'
            }`}
          >
            {s}
          </button>
        ))}
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Severity</Th>
                  <Th>Device</Th>
                  <Th>Message</Th>
                  <Th>Since</Th>
                  <Th className="w-32 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {(alerts || []).map((a) => (
                  <Tr key={a.id}>
                    <Td>
                      <Badge
                        variant={
                          a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info'
                        }
                      >
                        {a.severity}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="font-medium">{a.device_hostname || '—'}</div>
                      {a.device_ip && <div className="font-mono text-xs text-muted">{a.device_ip}</div>}
                    </Td>
                    <Td className="max-w-[400px] truncate text-sm">{a.message || '—'}</Td>
                    <Td className="text-xs text-muted">{relativeTime(a.triggered_at)}</Td>
                    <Td>
                      <div className="flex justify-end gap-1">
                        {a.status === 'active' && (
                          <Button size="sm" variant="outline" onClick={() => ack.mutate(a.id)}>
                            <Check className="h-3.5 w-3.5" />
                            Ack
                          </Button>
                        )}
                        {(a.status === 'active' || a.status === 'acknowledged') && (
                          <Button size="sm" variant="outline" onClick={() => resolve.mutate(a.id)}>
                            <X className="h-3.5 w-3.5" />
                            Resolve
                          </Button>
                        )}
                      </div>
                    </Td>
                  </Tr>
                ))}
                {(!alerts || alerts.length === 0) && (
                  <Tr>
                    <Td colSpan={5} className="py-12 text-center text-muted">
                      No {status} alerts 🎉
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'success' | 'warning' | 'danger'
}) {
  const color =
    tone === 'success' ? 'text-success' :
    tone === 'warning' ? 'text-warning' :
    tone === 'danger' ? 'text-danger' : 'text-text'
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  )
}
