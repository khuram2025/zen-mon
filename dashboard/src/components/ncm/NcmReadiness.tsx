import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'

export function NcmReadiness({ deviceId }: { deviceId: string }) {
  const { data, isPending, isError, refetch } = useQuery({ queryKey: ['ncm', 'readiness', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-readiness`)).data, refetchInterval: 30000 })
  return <Card><CardContent className="space-y-3 pt-4">
    <div className="flex items-center justify-between"><h2 className="font-semibold">Backup readiness</h2><Button size="sm" variant="outline" onClick={() => refetch()}>Recheck</Button></div>
    {isError ? <p role="alert" className="text-sm text-danger">Readiness could not be checked. Recheck before starting a backup.</p> : isPending ? <p className="text-sm text-muted">Checking saved prerequisites…</p> : <>
      <p className="text-xs text-muted">{data.assessment} Format validation does not certify a complete recovery backup.</p>
      <div className="grid gap-3 md:grid-cols-2">{data.checks.map((c: any) => <div key={c.id} className="rounded border border-border p-2 text-xs">
        <div className="flex justify-between gap-2 font-medium"><span>{c.label}</span><span className={c.status === 'pass' ? 'text-success' : c.status === 'blocked' ? 'text-danger' : 'text-warning'}>{c.status === 'pass' ? 'Ready' : c.status === 'blocked' ? 'Action needed' : 'Not verified'}</span></div>
        <p className="mt-1 text-muted">{c.detail}</p>
      </div>)}</div>
      <p className="text-xs font-medium">{data.can_capture ? 'Saved prerequisites allow a capture attempt. Check the resulting job and artifact before enabling recurring coverage.' : 'Resolve the required setup items above, then save settings and recheck.'}</p>
    </>}
  </CardContent></Card>
}

export function NcmHealth() {
  const { data, isError, isPending } = useQuery({ queryKey: ['ncm', 'health'], queryFn: async () => (await api.get('/ncm/health')).data, refetchInterval: 30000 })
  return <div className="rounded-md border border-border px-3 py-2 text-xs" aria-label="NCCM runtime health">
    {isError ? <span role="alert" className="text-warning">Runtime health unavailable — refresh to retry.</span> : isPending ? 'Checking runtime health…' : <div className="flex flex-wrap gap-x-5 gap-y-1">{[['worker', 'Backup worker'], ['scheduler', 'Schedule timer'], ['delivery', 'Delivery timer'], ['encryption', 'Encryption']].map(([key, label]) => <span key={key}>{label}: <strong className={['active', 'configured'].includes(data[key]) ? 'text-success' : 'text-warning'}>{data[key] || 'unknown'}</strong></span>)}</div>}
  </div>
}
