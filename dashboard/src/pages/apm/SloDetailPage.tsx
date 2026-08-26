import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Loader2, Target } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { APM_SERIES, ApmKpi, ApmTimeChart, ChartPanel, fmtCount } from '@/components/apm/viz'
import { fmtPct } from '@/components/apm/shared'
import type { Slo, SloBudget } from '@/types/apm'

function fmtWindow(s: number): string {
  if (s >= 86_400) return `${s / 86_400}d`
  if (s >= 3_600) return `${s / 3_600}h`
  return `${s / 60}m`
}

interface SeriesPoint { timestamp: string; requests: number; bad: number; sli: number; budget_remaining: number }

export function SloDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const budgetQ = useQuery<SloBudget>({ queryKey: ['apm', 'slo-budget', id], queryFn: async () => (await api.get(`/apm/slos/${id}/budget`)).data, enabled: !!id, refetchInterval: 60_000 })
  const seriesQ = useQuery<{ points: SeriesPoint[] }>({ queryKey: ['apm', 'slo-series', id], queryFn: async () => (await api.get(`/apm/slos/${id}/series`)).data, enabled: !!id })

  const slo = budgetQ.data?.slo
  const budget = budgetQ.data
  const points = seriesQ.data?.points ?? []
  const remaining = budget?.budget_remaining
  const tone = remaining == null ? 'primary' : remaining <= 0 ? 'danger' : remaining < 0.25 ? 'warning' : 'success'

  if (budgetQ.isLoading) {
    return <div className="flex items-center justify-center gap-2 py-16 text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading SLO…</div>
  }
  if (budgetQ.isError || !slo) {
    return (
      <div className="space-y-4">
        <Button variant="ghost" size="sm" onClick={() => navigate('/apm/slos')}><ArrowLeft className="mr-1 h-4 w-4" /> SLOs</Button>
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          {budgetQ.error ? apiErrorMessage(budgetQ.error) : 'SLO not found.'}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/apm/slos')}><ArrowLeft className="mr-1 h-4 w-4" /> SLOs</Button>
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">SLO detail</div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-text"><Target className="h-5 w-5 text-primary" />{slo.name}</h1>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <ApmKpi label="Target" tone="primary" value={`${slo.target}%`} sub={`${slo.sli_type} · ${slo.window_days}d`} />
        <ApmKpi
          label="Budget left"
          tone={tone}
          value={remaining != null ? `${(remaining * 100).toFixed(1)}%` : '—'}
          sub={budget?.budget_consumed != null ? `${(Math.min(budget.budget_consumed, 1) * 100).toFixed(1)}% consumed` : 'no data'}
        />
        <ApmKpi label="Requests" tone="info" value={fmtCount(budget?.window_requests)} sub={`over ${slo.window_days}d`} />
        <ApmKpi
          to={`/apm/services/${encodeURIComponent(slo.service_name)}`}
          label="Service"
          tone="accent"
          value={slo.service_name}
          sub={slo.operation || slo.env || 'all operations'}
        />
      </div>

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        <ChartPanel title="Error budget remaining" hint="cumulative over the SLO window">
          <ApmTimeChart
            data={points}
            series={[{ key: 'budget_remaining', name: 'remaining', color: APM_SERIES.apdex, fmt: fmtPct }]}
            height={240}
            loading={seriesQ.isLoading}
            empty="No rollup history for this SLO window yet."
          />
        </ChartPanel>
        <ChartPanel title="Daily SLI" hint="observed reliability each UTC day">
          <ApmTimeChart
            data={points}
            series={[{ key: 'sli', name: 'SLI %', color: APM_SERIES.latency, fmt: (v) => `${v.toFixed(2)}%` }]}
            height={240}
            loading={seriesQ.isLoading}
            empty="No rollup history for this SLO window yet."
          />
        </ChartPanel>
      </div>

      <Card className="overflow-hidden">
        <CardContent className="p-0">
          <Table>
            <THead>
              <Tr><Th>Tier</Th><Th>Windows</Th><Th>Threshold</Th><Th>Long burn</Th><Th>Short burn</Th><Th>State</Th></Tr>
            </THead>
            <TBody>
              {(budget?.tiers ?? []).map((t) => (
                <Tr key={t.tier}>
                  <Td className="capitalize">{t.tier}</Td>
                  <Td>{fmtWindow(t.long_window_s)} / {fmtWindow(t.short_window_s)}</Td>
                  <Td>{t.factor}×</Td>
                  <Td>{t.long_burn != null ? `${t.long_burn}×` : '—'}</Td>
                  <Td>{t.short_burn != null ? `${t.short_burn}×` : '—'}</Td>
                  <Td>
                    {t.breaching
                      ? <span className="text-xs font-semibold uppercase text-[#ef4444]">breaching</span>
                      : <span className="text-xs text-[#22c55e]">ok</span>}
                  </Td>
                </Tr>
              ))}
              {(budget?.tiers ?? []).length === 0 && (
                <Tr><Td colSpan={6} className="py-8 text-center text-muted">Burn tiers will appear once this service has rollup data.</Td></Tr>
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <p className="text-[11px] text-muted">
        A tier alerts only when both its long and short windows burn faster than the threshold.
        Deep-dive the instrumented service:{' '}
        <Link className="text-primary hover:underline" to={`/apm/services/${encodeURIComponent(slo.service_name)}`}>{slo.service_name}</Link>
        {' · '}
        <Link className="text-primary hover:underline" to={`/apm/errors?service=${encodeURIComponent(slo.service_name)}`}>Errors</Link>
      </p>
    </div>
  )
}
