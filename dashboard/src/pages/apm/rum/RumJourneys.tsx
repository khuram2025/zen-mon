import { useMemo, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { ArrowRight, Filter, Loader2, Plus, Route, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { fmtCount } from '@/components/apm/viz'
import { EXPLORER_HEAD, EXPLORER_ROWS } from '@/components/apm/explorer'
import { useTheme } from '@/stores/theme'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import type { RumFilters, RumFunnel, RumFunnelResults, RumFunnelStep, RumJourneys } from '@/types/apm'
import { QueryErrorPanel, RumEmptyState, RumSectionHeader, RumTableCard } from './RumUi'

const EXIT = '__exit__'

/**
 * Journey map: a Sankey whose nodes are "route @ step" so a route revisited
 * later in the path is a new node (Sankey layouts cannot contain cycles).
 * "Left the site" collects sessions whose path ended at that step.
 */
function JourneySankey({ data }: { data: RumJourneys }) {
  const theme = useTheme((state) => state.theme)
  const option = useMemo(() => {
    const steps = data.journey.steps
    const nodes = new Map<string, { name: string; label: string; step: number; exit?: boolean }>()
    const links: Array<{ source: string; target: string; value: number }> = []
    const key = (route: string, step: number) => `${step}:${route}`
    for (const item of steps) {
      const source = key(item.from, item.step)
      if (!nodes.has(source)) nodes.set(source, { name: source, label: item.from, step: item.step })
      const targetRoute = item.to == null ? EXIT : item.to
      const target = key(targetRoute, item.step + 1)
      if (!nodes.has(target)) nodes.set(target, { name: target, label: targetRoute === EXIT ? 'Left the site' : targetRoute === '…' ? 'Other routes' : targetRoute, step: item.step + 1, exit: targetRoute === EXIT })
      links.push({ source, target, value: item.sessions })
    }
    const dark = theme === 'dark'
    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item',
        formatter: (params: { dataType?: string; data?: { value?: number; source?: string; target?: string; label?: string } }) => {
          if (params.dataType === 'edge') {
            const src = nodes.get(params.data?.source ?? '')?.label
            const dst = nodes.get(params.data?.target ?? '')?.label
            return `${src} → ${dst}<br/><b>${fmtCount(params.data?.value)}</b> sessions`
          }
          return params.data?.label ?? ''
        },
      },
      series: [{
        type: 'sankey', left: 8, right: 130, top: 12, bottom: 12, nodeWidth: 14, nodeGap: 14, nodeAlign: 'left',
        draggable: false,
        emphasis: { focus: 'adjacency' },
        lineStyle: { color: 'gradient', curveness: 0.5, opacity: dark ? 0.35 : 0.3 },
        label: { color: dark ? '#e2e8f0' : '#1e293b', fontSize: 11, fontFamily: 'ui-monospace, monospace', formatter: (params: { data?: { label?: string } }) => params.data?.label ?? '' },
        data: [...nodes.values()].map((node) => ({ name: node.name, label: node.label, itemStyle: { color: node.exit ? '#64748b' : node.label === 'Other routes' ? '#94a3b8' : ['#6366f1', '#0ea5e9', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#14b8a6', '#f97316'][node.step % 8] } })),
        links,
      }],
    }
  }, [data, theme])
  if (!data.journey.steps.length) return <div className="flex h-[300px] items-center justify-center text-xs text-muted">No sessions passed through {data.journey.start || 'this route'} in this window.</div>
  return <ReactECharts option={option} style={{ height: Math.max(300, Math.min(560, 90 + data.journey.steps.length * 22)) }} notMerge lazyUpdate />
}

function RouteList({ title, hint, rows, onFilter, extra }: { title: string; hint: string; rows: Array<{ route: string; sessions: number; extra?: string }>; onFilter: (route: string) => void; extra?: string }) {
  const max = Math.max(1, ...rows.map((row) => row.sessions))
  return (
    <Card className="h-full">
      <CardHeader className="border-b border-border px-3 py-2"><CardTitle className="text-[13px]">{title}</CardTitle><p className="text-[10px] text-muted">{hint}</p></CardHeader>
      <CardContent className="space-y-1 p-2">
        {rows.length ? rows.map((row) => (
          <button key={row.route} type="button" onClick={() => onFilter(row.route)} title={`Filter by ${row.route}`} className="block w-full rounded-md px-1.5 py-1 text-left hover:bg-surface2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
            <div className="flex items-center justify-between gap-2 text-[11px]"><span className="truncate font-mono text-text2">{row.route || '/'}</span><span className="shrink-0 font-mono tabular-nums text-muted">{fmtCount(row.sessions)}{row.extra ? <span className="ml-1 text-[10px]">{row.extra}</span> : null}</span></div>
            <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-surface2"><div className="h-full rounded-full bg-primary/70" style={{ width: `${(row.sessions / max) * 100}%` }} /></div>
          </button>
        )) : <div className="py-6 text-center text-[11px] text-muted">{extra ?? 'No data'}</div>}
      </CardContent>
    </Card>
  )
}

function FunnelBars({ results }: { results: RumFunnelResults }) {
  const max = Math.max(1, results.entered)
  return (
    <ol className="space-y-2">
      {results.steps.map((step, index) => (
        <li key={step.index} className="grid grid-cols-[28px,minmax(0,1fr)] items-center gap-3">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-surface2 text-[10px] font-semibold text-text2">{step.index}</span>
          <div>
            <div className="flex items-baseline justify-between gap-2 text-[11px]">
              <span className="truncate"><span className="font-mono text-text">{step.label}</span><span className="ml-1.5 text-[10px] uppercase tracking-wider text-muted">{step.type}</span></span>
              <span className="shrink-0 font-mono tabular-nums text-text2">{fmtCount(step.sessions)}{index > 0 && step.step_conversion_pct != null && <span className={cn('ml-1.5 text-[10px]', step.step_conversion_pct < 50 ? 'text-danger' : step.step_conversion_pct < 80 ? 'text-warning' : 'text-success')}>{step.step_conversion_pct.toFixed(0)}% of previous</span>}{index > 0 && step.drop_off > 0 && <span className="ml-1.5 text-[10px] text-muted">−{fmtCount(step.drop_off)}</span>}</span>
            </div>
            <div className="mt-1 h-3 overflow-hidden rounded bg-surface2"><div className={cn('h-full rounded', index === results.steps.length - 1 ? 'bg-success/80' : 'bg-primary/70')} style={{ width: `${Math.max(step.sessions / max * 100, step.sessions ? 1 : 0)}%` }} /></div>
          </div>
        </li>
      ))}
    </ol>
  )
}

function FunnelEditor({ open, onOpenChange, applications, onSave, saving }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  applications: string[]
  onSave: (body: { application_id: string; name: string; description: string; steps: RumFunnelStep[]; window_seconds: number }) => void
  saving?: boolean
}) {
  const [name, setName] = useState('')
  const [applicationId, setApplicationId] = useState(applications[0] ?? '')
  const [windowMinutes, setWindowMinutes] = useState('60')
  const [steps, setSteps] = useState<RumFunnelStep[]>([{ type: 'view', match: '/' }, { type: 'view', match: '' }])
  const valid = name.trim() && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(applicationId.trim()) && steps.length >= 2 && steps.every((step) => step.match.trim()) && Number(windowMinutes) >= 1
  const update = (index: number, patch: Partial<RumFunnelStep>) => setSteps((current) => current.map((step, i) => (i === index ? { ...step, ...patch } : step)))
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>New funnel</DialogTitle>
          <DialogDescription>Ordered steps a session must reach, in order, within the window. Routes may use <code className="rounded bg-surface2 px-1">*</code> for one path segment; actions match the action name the SDK reports.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="Name" required><Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Checkout" /></FormField>
            <FormField label="Application" required>
              {applications.length ? (
                <Select value={applicationId} onValueChange={setApplicationId}><SelectTrigger><SelectValue placeholder="Application" /></SelectTrigger><SelectContent>{applications.map((app) => <SelectItem key={app} value={app}>{app}</SelectItem>)}</SelectContent></Select>
              ) : <Input value={applicationId} onChange={(event) => setApplicationId(event.target.value)} placeholder="customer-portal" />}
            </FormField>
          </div>
          <FormField label="Complete within" hint="Minutes from the first step; later steps outside the window do not count.">
            <Input type="number" min={1} max={10080} value={windowMinutes} onChange={(event) => setWindowMinutes(event.target.value)} className="w-32" />
          </FormField>
          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">Steps</div>
            <ol className="space-y-1.5">
              {steps.map((step, index) => (
                <li key={index} className="flex items-center gap-2">
                  <span className="w-5 text-right font-mono text-[11px] text-muted">{index + 1}</span>
                  <Select value={step.type} onValueChange={(value) => update(index, { type: value as 'view' | 'action' })}><SelectTrigger className="w-[110px]"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="view">Route</SelectItem><SelectItem value="action">Action</SelectItem></SelectContent></Select>
                  <Input value={step.match} onChange={(event) => update(index, { match: event.target.value })} placeholder={step.type === 'view' ? '/checkout' : 'Place order'} className="font-mono text-xs" />
                  <Input value={step.label ?? ''} onChange={(event) => update(index, { label: event.target.value })} placeholder="Label (optional)" className="w-36 text-xs" />
                  <Button variant="ghost" size="sm" className="h-8 w-8 p-0 text-muted" disabled={steps.length <= 2} aria-label="Remove step" onClick={() => setSteps((current) => current.filter((_, i) => i !== index))}><Trash2 className="h-3.5 w-3.5" /></Button>
                </li>
              ))}
            </ol>
            <Button variant="outline" size="sm" className="mt-2" disabled={steps.length >= 10} onClick={() => setSteps((current) => [...current, { type: 'view', match: '' }])}><Plus className="h-3.5 w-3.5" /> Add step</Button>
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!valid || saving} onClick={() => onSave({ application_id: applicationId.trim(), name: name.trim(), description: '', steps: steps.map((step) => ({ ...step, match: step.match.trim(), label: step.label?.trim() || undefined })), window_seconds: Math.round(Number(windowMinutes) * 60) })}>
            {saving && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}Create funnel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function RumJourneysPanel({ data, loading, error, onRetry, start, onStart, onFilter, funnels, funnelResults, selectedFunnel, onSelectFunnel, onCreateFunnel, onDeleteFunnel, creatingFunnel, applications }: {
  data?: RumJourneys
  loading?: boolean
  error?: unknown
  onRetry?: () => void
  start: string
  onStart: (route: string) => void
  onFilter: (key: keyof RumFilters, value: string) => void
  funnels?: RumFunnel[]
  funnelResults?: RumFunnelResults
  selectedFunnel: string
  onSelectFunnel: (id: string) => void
  onCreateFunnel: (body: { application_id: string; name: string; description: string; steps: RumFunnelStep[]; window_seconds: number }) => void
  onDeleteFunnel: (id: string) => void
  creatingFunnel?: boolean
  applications: string[]
}) {
  const [editorOpen, setEditorOpen] = useState(false)
  const startRoute = start || data?.journey.start || ''
  const startOptions = useMemo(() => {
    const routes = new Set<string>()
    data?.entries.forEach((row) => routes.add(row.route))
    data?.transitions.forEach((row) => { routes.add(row.from); routes.add(row.to) })
    if (startRoute) routes.add(startRoute)
    return [...routes]
  }, [data, startRoute])
  if (error) return <QueryErrorPanel label="journeys" error={error} onRetry={onRetry} />
  const funnelOptions = funnels ?? []
  return (
    <div className="space-y-4">
      <section aria-labelledby="rum-journey-map">
        <RumSectionHeader id="rum-journey-map" title="Journey map" description={data ? `${fmtCount(data.sessions)} sessions · ${data.avg_path_length.toFixed(1)} routes per session on average · ${fmtCount(data.bounced_sessions)} left after one route. Each column is the next step from the chosen start.` : 'Where sessions go, step by step, from a chosen start route.'}
          action={(
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] uppercase tracking-wider text-muted">Start at</span>
              <Select value={startRoute} onValueChange={onStart}>
                <SelectTrigger className="h-7 w-[220px] font-mono text-[11px]" aria-label="Start route"><SelectValue placeholder="Top entry route" /></SelectTrigger>
                <SelectContent>{startOptions.map((route) => <SelectItem key={route} value={route}>{route || '/'}</SelectItem>)}</SelectContent>
              </Select>
            </div>
          )} />
        <Card className="overflow-hidden">
          {!data ? <div className="flex h-[300px] items-center justify-center text-xs text-muted">{loading ? 'Tracing journeys…' : 'No journey data'}</div>
            : !data.sessions ? <RumEmptyState icon={Route} title="No sessions in this window" description="Journeys need at least one sampled session with a page view." />
            : <JourneySankey data={data} />}
        </Card>
      </section>

      <div className="grid gap-2.5 md:grid-cols-3">
        <RouteList title="Entry routes" hint="First route of each session; the bounce figure is sessions that saw nothing else" rows={(data?.entries ?? []).map((row) => ({ route: row.route, sessions: row.sessions, extra: row.bounced ? `· ${fmtCount(row.bounced)} bounced` : undefined }))} onFilter={(route) => onFilter('view_name', route)} />
        <RouteList title="Exit routes" hint="Last route before the session ended" rows={data?.exits ?? []} onFilter={(route) => onFilter('view_name', route)} />
        <Card className="h-full">
          <CardHeader className="border-b border-border px-3 py-2"><CardTitle className="text-[13px]">Top transitions</CardTitle><p className="text-[10px] text-muted">Most travelled route-to-route moves</p></CardHeader>
          <CardContent className="overflow-x-auto p-0">
            {data?.transitions.length ? (
              <Table>
                <THead className={EXPLORER_HEAD}><Tr><Th>From</Th><Th /><Th>To</Th><Th className="text-right">Sessions</Th></Tr></THead>
                <TBody className={EXPLORER_ROWS}>
                  {data.transitions.slice(0, 10).map((row) => (
                    <Tr key={`${row.from}>${row.to}`}>
                      <Td className="max-w-[120px]"><button type="button" className="block max-w-full truncate font-mono text-[11px] text-text2 hover:text-text hover:underline" onClick={() => onStart(row.from)} title="Start the journey map here">{row.from || '/'}</button></Td>
                      <Td className="w-6 text-muted"><ArrowRight className="h-3 w-3" /></Td>
                      <Td className="max-w-[120px]"><span className="block truncate font-mono text-[11px] text-text2" title={row.to}>{row.to || '/'}</span></Td>
                      <Td className="whitespace-nowrap text-right font-mono text-xs tabular-nums">{fmtCount(row.sessions)}</Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            ) : <div className="py-6 text-center text-[11px] text-muted">No multi-route sessions yet.</div>}
          </CardContent>
        </Card>
      </div>

      <section aria-labelledby="rum-funnels">
        <RumSectionHeader id="rum-funnels" title="Funnels" description="Sessions that reach each step in order within the funnel's window. Results use the current time range and segment filters."
          action={<Button size="sm" variant="outline" className="h-7 px-2 text-[11px]" onClick={() => setEditorOpen(true)}><Plus className="h-3.5 w-3.5" /> New funnel</Button>} />
        {!funnelOptions.length ? (
          <Card><RumEmptyState icon={Filter} title="No funnels yet" description="Define the routes and actions a user must pass through, for example Home → Product → Cart → Checkout, and see where sessions drop off." action={<Button size="sm" onClick={() => setEditorOpen(true)}><Plus className="h-3.5 w-3.5" /> Create the first funnel</Button>} /></Card>
        ) : (
          <div className="grid gap-2.5 xl:grid-cols-[260px,minmax(0,1fr)]">
            <Card className="self-start">
              <CardContent className="p-1.5">
                {funnelOptions.map((funnel) => (
                  <div key={funnel.id} className={cn('flex items-center justify-between gap-1 rounded-md px-2 py-1.5', funnel.id === selectedFunnel ? 'bg-primary/10' : 'hover:bg-surface2')}>
                    <button type="button" className="min-w-0 flex-1 text-left" onClick={() => onSelectFunnel(funnel.id)}>
                      <div className={cn('truncate text-xs font-medium', funnel.id === selectedFunnel ? 'text-primary' : 'text-text')}>{funnel.name}</div>
                      <div className="truncate text-[10px] text-muted">{funnel.application_id} · {funnel.steps.length} steps · {Math.round(funnel.window_seconds / 60)} min</div>
                    </button>
                    <Button variant="ghost" size="sm" className="h-6 w-6 shrink-0 p-0 text-muted hover:text-danger" aria-label={`Delete funnel ${funnel.name}`} onClick={() => onDeleteFunnel(funnel.id)}><Trash2 className="h-3 w-3" /></Button>
                  </div>
                ))}
              </CardContent>
            </Card>
            <RumTableCard title={funnelResults?.funnel.name ?? 'Funnel'} description={funnelResults ? `${fmtCount(funnelResults.entered)} sessions entered · ${fmtCount(funnelResults.converted)} completed · ${funnelResults.conversion_pct == null ? '—' : `${funnelResults.conversion_pct.toFixed(1)}%`} conversion · within ${Math.round(funnelResults.funnel.window_seconds / 60)} min` : 'Select a funnel'}>
              <div className="p-4">
                {funnelResults ? <FunnelBars results={funnelResults} /> : <div className="py-8 text-center text-xs text-muted">Loading results…</div>}
              </div>
            </RumTableCard>
          </div>
        )}
      </section>
      <FunnelEditor open={editorOpen} onOpenChange={setEditorOpen} applications={applications} saving={creatingFunnel} onSave={(body) => { onCreateFunnel(body); setEditorOpen(false) }} />
    </div>
  )
}
