import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Activity, ExternalLink, Globe2, MonitorSmartphone, TriangleAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { fmtMs } from '@/components/apm/shared'
import { ApmRangePicker, type ApmRangeKey } from '@/components/apm/ApmRange'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmKpi, VitalGauge, LatencyCell, RankBar, fmtCount } from '@/components/apm/viz'

interface RumSummary {
  events: number; sessions: number; views: number; lcp_p75: number; inp_p75: number; cls_p75: number; errors: number
  routes: { application_id: string; view_name: string; views: number; sessions: number; lcp_p75: number; inp_p75: number; cls_p75: number; errors: number; last_seen: string }[]
  recent_sessions: { session_id: string; application_id: string; started_at: string; last_seen: string; events: number; errors: number; browser: string; device_type: string; backend_trace_id: string }[]
}

const score = (name: 'lcp' | 'inp' | 'cls', value: number) => {
  const limits = name === 'lcp' ? [2500, 4000] : name === 'inp' ? [200, 500] : [0.1, 0.25]
  return value <= limits[0] ? 'text-success' : value <= limits[1] ? 'text-warning' : 'text-danger'
}

export function RumPage() {
  const navigate = useNavigate()
  const [range, setRange] = useState<ApmRangeKey>('24h')
  const query = useQuery<RumSummary>({ queryKey: ['apm', 'rum', range], queryFn: async () => (await api.get(`/apm/rum/summary?range=${range}`)).data, refetchInterval: 30_000 })
  const d = query.data
  return <div className="space-y-4">
    <ApmPageHeader title="Real User Monitoring" description="Core Web Vitals, browser errors, sessions, and frontend-to-backend trace correlation." article="rum"
      actions={<ApmRangePicker value={range} onChange={setRange} />} />
    {!query.isLoading && !query.isError && d?.events === 0 && <Card><CardContent className="flex flex-col items-center py-12 text-center">
      <Globe2 className="mb-3 h-9 w-9 text-primary" /><h2 className="text-base font-semibold text-text">Connect your first web application</h2>
      <p className="mt-1 max-w-xl text-sm text-muted">Create a Browser RUM key with an exact origin allowlist, then add the controller-hosted SDK tag. The SDK has no internet dependency and masks form input by design.</p>
      <Button className="mt-4" onClick={() => navigate('/apm/settings?tab=keys')}>Create Browser RUM key</Button>
    </CardContent></Card>}
    {query.isError && <div className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">RUM analytics are temporarily unavailable.</div>}
    {d && d.events > 0 && <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <ApmKpi label="Sessions" tone="info" value={fmtCount(d.sessions)} sub={`${fmtCount(d.views)} page views`} />
        <ApmKpi label="JS errors" tone={d.errors ? 'danger' : 'success'} value={fmtCount(d.errors)} sub={`${fmtCount(d.events)} events`} />
        <VitalGauge label="LCP p75" value={d.lcp_p75} good={2500} poor={4000} format={fmtMs} />
        <VitalGauge label="INP p75" value={d.inp_p75} good={200} poor={500} format={fmtMs} />
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <VitalGauge label="CLS p75" value={d.cls_p75} good={0.1} poor={0.25} format={(v) => v.toFixed(3)} />
        <ApmKpi label="Routes" tone="primary" value={fmtCount(d.routes.length)} sub="distinct views" />
        <ApmKpi label="Recent sessions" tone="accent" value={fmtCount(d.recent_sessions.length)} sub="latest clients" />
      </div>
      <Card><CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Activity className="h-4 w-4 text-primary" /> Route experience</CardTitle></CardHeader><CardContent className="p-0"><Table>
        <THead><Tr><Th>Application / route</Th><Th className="text-right">Views</Th><Th className="text-right">LCP p75</Th><Th className="text-right">INP p75</Th><Th className="text-right">CLS p75</Th><Th className="text-right">Errors</Th></Tr></THead>
        <TBody>{d.routes.map(r => {
          const maxViews = Math.max(...d.routes.map((x) => x.views), 1)
          return <Tr key={`${r.application_id}:${r.view_name}`}><Td><div className="font-medium text-text">{r.view_name}</div><div className="text-xs text-muted">{r.application_id}</div></Td><Td className="text-right"><div className="font-mono text-xs">{r.views}</div><RankBar value={r.views} max={maxViews} /></Td><Td className="text-right"><LatencyCell ms={r.lcp_p75} /></Td><Td className="text-right"><LatencyCell ms={r.inp_p75} /></Td><Td className={`text-right font-mono text-xs ${score('cls', r.cls_p75)}`}>{r.cls_p75.toFixed(3)}</Td><Td className="text-right">{r.errors}</Td></Tr>
        })}</TBody>
      </Table></CardContent></Card>
      <Card><CardHeader><CardTitle className="flex items-center gap-2 text-sm"><MonitorSmartphone className="h-4 w-4 text-primary" /> Recent sessions</CardTitle></CardHeader><CardContent className="p-0"><Table>
        <THead><Tr><Th>Session</Th><Th>Client</Th><Th className="text-right">Events</Th><Th className="text-right">Errors</Th><Th>Last seen</Th><Th>Backend trace</Th></Tr></THead>
        <TBody>{d.recent_sessions.map(s => <Tr key={s.session_id}><Td className="font-mono text-xs">{s.session_id.slice(0, 12)}…</Td><Td>{s.browser} · {s.device_type}</Td><Td className="text-right">{s.events}</Td><Td className="text-right">{s.errors ? <span className="inline-flex items-center gap-1 text-danger"><TriangleAlert className="h-3 w-3" />{s.errors}</span> : '0'}</Td><Td className="text-xs text-muted">{new Date(s.last_seen).toLocaleString()}</Td><Td>{s.backend_trace_id ? <button className="inline-flex items-center gap-1 font-mono text-xs text-primary hover:underline" onClick={() => navigate(`/apm/traces/${s.backend_trace_id}`)}>{s.backend_trace_id.slice(0, 10)}…<ExternalLink className="h-3 w-3" /></button> : <span className="text-xs text-muted">—</span>}</Td></Tr>)}</TBody>
      </Table></CardContent></Card>
    </>}
  </div>
}
