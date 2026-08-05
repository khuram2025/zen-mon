import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, ArrowLeft, ExternalLink } from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'
import { KpiTile } from '@/components/apm/shared'
import { ErrorStatusBadge, ERROR_STATUSES } from '@/components/apm/errorShared'

interface Occurrence { timestamp: string; trace_id: string; span_id: string; service: string; message: string }
interface ErrorDetail {
  group_id: string; exception_type: string; message: string; service: string; services: string[]
  occurrences: number; traces: number; first_seen: string; last_seen: string; versions: string[]
  status: string; assignee: string | null; resolved_in_version: string | null
  sample_stack: string; representative_trace_id: string
  per_service: { service: string; count: number }[]
  occurrences_recent: Occurrence[]; trend: { timestamp: string; count: number }[]
}

export function ErrorIssueDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const q = useQuery<ErrorDetail>({
    queryKey: ['apm', 'error', id],
    queryFn: async () => (await api.get(`/apm/errors/${id}?range_=7d`)).data,
  })

  const [status, setStatus] = useState('')
  const [assignee, setAssignee] = useState('')
  const [version, setVersion] = useState('')
  useEffect(() => {
    if (q.data) { setStatus(q.data.status); setAssignee(q.data.assignee || ''); setVersion(q.data.resolved_in_version || '') }
  }, [q.data])

  const triage = useMutation({
    mutationFn: async () => (await api.patch(`/apm/errors/${id}`, {
      status, assignee: assignee || null,
      resolved_in_version: status === 'resolved_in_version' ? version : null,
    })).data,
    onSuccess: () => {
      toast.success('Issue updated')
      qc.invalidateQueries({ queryKey: ['apm', 'error', id] })
      qc.invalidateQueries({ queryKey: ['apm', 'errors'] })
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  if (q.isLoading) return <div className="flex items-center justify-center gap-2 text-muted p-12"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
  if (q.isError || !q.data) return <div className="space-y-4"><Button variant="ghost" onClick={() => navigate('/apm/errors')}><ArrowLeft className="w-4 h-4 mr-1" /> Back</Button><div className="text-center text-muted py-12">Issue not found.</div></div>
  const d = q.data

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/apm/errors')}><ArrowLeft className="w-4 h-4 mr-1" /> Errors</Button>
        <h1 className="text-lg font-semibold text-text">{d.exception_type}</h1>
        <ErrorStatusBadge status={d.status} />
      </div>
      <div className="text-sm text-danger font-mono">{d.message}</div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <KpiTile label="Events" value={d.occurrences} />
        <KpiTile label="Traces" value={d.traces} />
        <KpiTile label="Service" value={d.service} />
        <KpiTile label="First seen" value={new Date(d.first_seen).toLocaleDateString()} />
        <KpiTile label="Last seen" value={new Date(d.last_seen).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 space-y-4">
          <Card>
            <CardHeader className="pb-1"><CardTitle className="text-sm">Occurrences over time</CardTitle></CardHeader>
            <CardContent>
              <ResponsiveContainer width="100%" height={150}>
                <AreaChart data={d.trend} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
                  <defs><linearGradient id="g-err" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#ef4444" stopOpacity={0.4} /><stop offset="100%" stopColor="#ef4444" stopOpacity={0} /></linearGradient></defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" vertical={false} />
                  <XAxis dataKey="timestamp" tick={{ fontSize: 10, fill: '#94a3b8' }} tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit' })} minTickGap={40} />
                  <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} allowDecimals={false} width={36} />
                  <Tooltip contentStyle={{ background: '#0d121b', border: '1px solid #1e293b', fontSize: 12, color: '#e5e7eb' }} labelFormatter={(t) => new Date(t).toLocaleString()} />
                  <Area type="monotone" dataKey="count" stroke="#ef4444" fill="url(#g-err)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          {d.sample_stack && (
            <Card>
              <CardHeader className="pb-1"><CardTitle className="text-sm">Stack trace</CardTitle></CardHeader>
              <CardContent><pre className="text-xs bg-surface2 p-3 rounded overflow-x-auto whitespace-pre-wrap text-text">{d.sample_stack}</pre></CardContent>
            </Card>
          )}

          <Card>
            <CardHeader className="pb-1"><CardTitle className="text-sm">Recent occurrences</CardTitle></CardHeader>
            <CardContent className="p-0">
              <div className="divide-y divide-border">
                {d.occurrences_recent.map((o, i) => (
                  <div key={i} className="flex items-center justify-between px-3 py-2 text-xs hover:bg-surface2">
                    <div className="flex items-center gap-3">
                      <span className="text-muted w-32">{new Date(o.timestamp).toLocaleString()}</span>
                      <span>{o.service}</span>
                      <span className="text-muted truncate max-w-xs">{o.message}</span>
                    </div>
                    <button className="flex items-center gap-1 text-primary" onClick={() => navigate(`/apm/traces/${o.trace_id}`)}>
                      trace <ExternalLink className="w-3 h-3" />
                    </button>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-1"><CardTitle className="text-sm">Triage</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              <div>
                <label className="text-xs text-muted">Status</label>
                <select value={status} onChange={(e) => setStatus(e.target.value)} className="w-full h-9 mt-1 rounded-md bg-surface2 border border-border text-sm px-2 text-text capitalize">
                  {ERROR_STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
                </select>
              </div>
              {status === 'resolved_in_version' && (
                <div><label className="text-xs text-muted">Resolved in version</label><Input className="mt-1" value={version} onChange={(e) => setVersion(e.target.value)} placeholder="2.4.0" /></div>
              )}
              <div><label className="text-xs text-muted">Assignee</label><Input className="mt-1" value={assignee} onChange={(e) => setAssignee(e.target.value)} placeholder="user@example.com" /></div>
              <Button className="w-full" disabled={triage.isPending} onClick={() => triage.mutate()}>
                {triage.isPending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />} Save
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-1"><CardTitle className="text-sm">Affected services</CardTitle></CardHeader>
            <CardContent className="space-y-1">
              {d.per_service.map((p) => (
                <div key={p.service} className="flex items-center justify-between text-sm"><span>{p.service}</span><span className="font-mono text-xs text-muted">{p.count}</span></div>
              ))}
              {d.versions.length > 0 && <div className="text-xs text-muted pt-2">Versions: {d.versions.join(', ')}</div>}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
