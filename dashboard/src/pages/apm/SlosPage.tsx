import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, Loader2, Check, Target } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { KpiTile } from '@/components/apm/shared'

interface Slo {
  id: string
  name: string
  service_name: string
  env: string | null
  operation: string | null
  sli_type: 'availability' | 'latency' | 'error_rate'
  latency_threshold_ms: number | null
  target: number
  window_days: number
  burn_alert_enabled: boolean
  notify_channels: string[]
}

interface BurnTier {
  tier: string
  long_window_s: number
  short_window_s: number
  factor: number
  severity: string
  long_burn: number | null
  short_burn: number | null
  breaching: boolean
}

interface Budget {
  budget_consumed: number | null
  budget_remaining: number | null
  window_requests: number
  tiers: BurnTier[]
}

const SLI_LABEL: Record<string, string> = {
  availability: 'Availability',
  latency: 'Latency',
  error_rate: 'Error rate',
}

function fmtWindow(s: number): string {
  if (s >= 86_400) return `${s / 86_400}d`
  if (s >= 3_600) return `${s / 3_600}h`
  return `${s / 60}m`
}

function BudgetCell({ slo, onDetail }: { slo: Slo; onDetail: (b: Budget) => void }) {
  const q = useQuery<Budget>({
    queryKey: ['apm', 'slo-budget', slo.id],
    queryFn: async () => (await api.get(`/apm/slos/${slo.id}/budget`)).data,
    refetchInterval: 60_000,
  })
  if (q.isLoading) return <Loader2 className="w-3.5 h-3.5 animate-spin text-[var(--text-muted)]" />
  const b = q.data
  if (!b || b.budget_remaining == null) {
    return <span className="text-xs text-[var(--text-muted)]">no data</span>
  }
  const remaining = b.budget_remaining
  const breaching = b.tiers.some((t) => t.breaching)
  const color = breaching || remaining <= 0 ? '#ef4444' : remaining < 0.25 ? '#f59e0b' : '#22c55e'
  return (
    <button onClick={() => onDetail(b)} className="w-40 text-left group" title="Burn-rate details">
      <div className="flex items-center justify-between text-xs mb-1">
        <span style={{ color }} className="font-medium">{(remaining * 100).toFixed(1)}% left</span>
        {breaching && <span className="text-[10px] font-semibold text-[#ef4444] uppercase">burning</span>}
      </div>
      <div className="h-1.5 rounded-full bg-[var(--bg-elevated)] overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${Math.max(2, remaining * 100)}%`, backgroundColor: color }} />
      </div>
    </button>
  )
}

export function SlosPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Slo | null>(null)
  const [deleting, setDeleting] = useState<Slo | null>(null)
  const [detail, setDetail] = useState<{ slo: Slo; budget: Budget } | null>(null)

  const slosQuery = useQuery<{ items: Slo[] }>({
    queryKey: ['apm', 'slos'],
    queryFn: async () => (await api.get('/apm/slos')).data,
  })
  const slos = slosQuery.data?.items ?? []

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/apm/slos/${id}`),
    onSuccess: () => {
      toast.success('SLO deleted')
      qc.invalidateQueries({ queryKey: ['apm', 'slos'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-[var(--text-primary)]">SLOs</h1>
          <p className="text-sm text-[var(--text-muted)] mt-1">
            Reliability targets with error budgets. Burn alerts page when the budget is being consumed
            too fast (multi-window burn rate), not on isolated spikes.
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="w-4 h-4" /> New SLO
        </Button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 max-w-xl">
        <KpiTile label="SLOs" value={slos.length} />
        <KpiTile label="Burn alerts on" value={slos.filter((s) => s.burn_alert_enabled).length} />
      </div>

      <Card>
        <CardContent className="p-0">
          {slosQuery.isLoading ? (
            <div className="flex items-center justify-center gap-2 text-[var(--text-muted)] py-10">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : slos.length === 0 ? (
            <div className="text-center py-14 px-6">
              <Target className="w-8 h-8 mx-auto text-[var(--text-muted)]" />
              <div className="mt-3 font-medium text-[var(--text-primary)]">No SLOs yet</div>
              <p className="text-sm text-[var(--text-muted)] mt-1 max-w-md mx-auto">
                Define a target like “99.9% of checkout requests succeed over 30 days” and ZenPlus
                will track the error budget and alert on fast burn.
              </p>
              <Button className="mt-4" onClick={() => { setEditing(null); setFormOpen(true) }}>
                <Plus className="w-4 h-4" /> Create your first SLO
              </Button>
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Name</Th><Th>Service</Th><Th>SLI</Th><Th>Target</Th>
                  <Th>Window</Th><Th>Error budget</Th><Th>Alerts</Th><Th className="w-20" />
                </Tr>
              </THead>
              <TBody>
                {slos.map((s) => (
                  <Tr key={s.id}>
                    <Td className="font-medium text-[var(--text-primary)]">{s.name}</Td>
                    <Td>
                      {s.service_name}
                      {s.env && <span className="ml-1.5 text-xs text-[var(--text-muted)]">{s.env}</span>}
                      {s.operation && <div className="text-xs text-[var(--text-muted)]">{s.operation}</div>}
                    </Td>
                    <Td>
                      {SLI_LABEL[s.sli_type] || s.sli_type}
                      {s.sli_type === 'latency' && s.latency_threshold_ms != null && (
                        <span className="ml-1 text-xs text-[var(--text-muted)]">≤{s.latency_threshold_ms}ms</span>
                      )}
                    </Td>
                    <Td>{s.target}%</Td>
                    <Td>{s.window_days}d</Td>
                    <Td><BudgetCell slo={s} onDetail={(b) => setDetail({ slo: s, budget: b })} /></Td>
                    <Td>
                      {s.burn_alert_enabled
                        ? <span className="text-xs text-[#22c55e]">on</span>
                        : <span className="text-xs text-[var(--text-muted)]">off</span>}
                    </Td>
                    <Td>
                      <div className="flex gap-1">
                        <Button variant="ghost" size="icon" className="h-7 w-7"
                          onClick={() => { setEditing(s); setFormOpen(true) }} title="Edit">
                          <Pencil className="w-3.5 h-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7 text-[var(--text-muted)] hover:text-[#ef4444]"
                          onClick={() => setDeleting(s)} title="Delete">
                          <Trash2 className="w-3.5 h-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <SloFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        slo={editing}
        onSaved={() => qc.invalidateQueries({ queryKey: ['apm', 'slos'] })}
      />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(v) => !v && setDeleting(null)}
        title="Delete SLO?"
        description={`"${deleting?.name}" and its burn-alert state will be removed. Historical data is unaffected.`}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => deleting && del.mutate(deleting.id)}
      />

      <Dialog open={!!detail} onOpenChange={(v) => !v && setDetail(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Burn rate — {detail?.slo.name}</DialogTitle>
          </DialogHeader>
          {detail && (
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-3">
                <KpiTile label="Budget left"
                  value={detail.budget.budget_remaining != null ? `${(detail.budget.budget_remaining * 100).toFixed(1)}%` : '—'} />
                <KpiTile label="Consumed"
                  value={detail.budget.budget_consumed != null
                    ? detail.budget.budget_consumed >= 1 ? '100% (exhausted)' : `${(detail.budget.budget_consumed * 100).toFixed(1)}%`
                    : '—'} />
                <KpiTile label={`Requests (${detail.slo.window_days}d)`}
                  value={detail.budget.window_requests.toLocaleString()} />
              </div>
              <Table>
                <THead>
                  <Tr><Th>Tier</Th><Th>Windows</Th><Th>Threshold</Th><Th>Long burn</Th><Th>Short burn</Th><Th>State</Th></Tr>
                </THead>
                <TBody>
                  {detail.budget.tiers.map((t) => (
                    <Tr key={t.tier}>
                      <Td className="capitalize">{t.tier}</Td>
                      <Td>{fmtWindow(t.long_window_s)} / {fmtWindow(t.short_window_s)}</Td>
                      <Td>{t.factor}×</Td>
                      <Td>{t.long_burn != null ? `${t.long_burn}×` : '—'}</Td>
                      <Td>{t.short_burn != null ? `${t.short_burn}×` : '—'}</Td>
                      <Td>
                        {t.breaching
                          ? <span className="text-xs font-semibold text-[#ef4444] uppercase">breaching</span>
                          : <span className="text-xs text-[#22c55e]">ok</span>}
                      </Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
              <p className="text-[11px] text-[var(--text-muted)]">
                A tier alerts only when both its long and short windows burn faster than the threshold —
                the short window makes the alert clear within minutes of recovery.
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}

function SloFormDialog({ open, onOpenChange, slo, onSaved }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  slo: Slo | null
  onSaved: () => void
}) {
  const isEdit = !!slo
  const [name, setName] = useState('')
  const [serviceName, setServiceName] = useState('')
  const [env, setEnv] = useState('prod')
  const [operation, setOperation] = useState('')
  const [sliType, setSliType] = useState<string>('availability')
  const [target, setTarget] = useState('99.9')
  const [windowDays, setWindowDays] = useState('30')
  const [latencyMs, setLatencyMs] = useState('500')
  const [burnEnabled, setBurnEnabled] = useState(true)
  const [channels, setChannels] = useState<string[]>([])

  const { data: servicesResp } = useQuery<any>({
    queryKey: ['apm', 'services', 'list-min'],
    queryFn: async () => (await api.get('/apm/services?range=24h')).data,
    enabled: open,
  })
  const services: { name: string; envs: string[] }[] = servicesResp?.services ?? []

  const { data: channelsResp } = useQuery<any>({
    queryKey: ['channels', 'list-min'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    enabled: open,
  })
  const allChannels: any[] = Array.isArray(channelsResp) ? channelsResp : channelsResp?.data || []

  // Populate on open (mirrors AlertRuleFormDialog's effect-free reset-on-open idiom
  // via key remount below — the parent passes a new `slo` each time).
  const [seededFor, setSeededFor] = useState<string | null>(null)
  const seedKey = open ? (slo?.id ?? '__new__') : null
  if (seedKey !== seededFor) {
    setSeededFor(seedKey)
    if (open) {
      setName(slo?.name ?? '')
      setServiceName(slo?.service_name ?? '')
      setEnv(slo?.env ?? 'prod')
      setOperation(slo?.operation ?? '')
      setSliType(slo?.sli_type ?? 'availability')
      setTarget(String(slo?.target ?? '99.9'))
      setWindowDays(String(slo?.window_days ?? '30'))
      setLatencyMs(String(slo?.latency_threshold_ms ?? '500'))
      setBurnEnabled(slo?.burn_alert_enabled ?? true)
      setChannels(slo?.notify_channels ?? [])
    }
  }

  const save = useMutation({
    mutationFn: async () => {
      const payload: any = {
        name,
        operation: operation.trim() || null,
        sli_type: sliType,
        latency_threshold_ms: sliType === 'latency' ? Number(latencyMs) : null,
        target: Number(target),
        window_days: Number(windowDays),
        burn_alert_enabled: burnEnabled,
        notify_channels: channels,
      }
      if (isEdit) return (await api.put(`/apm/slos/${slo!.id}`, payload)).data
      return (await api.post('/apm/slos', { ...payload, service_name: serviceName, env })).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'SLO updated' : 'SLO created')
      onSaved()
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const selectedService = services.find((x) => x.name === serviceName)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit SLO' : 'New SLO'}</DialogTitle>
        </DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-4">
          <FormField label="Name" required>
            <Input required value={name} onChange={(e) => setName(e.target.value)}
              placeholder="Checkout availability" />
          </FormField>

          {!isEdit && (
            <div className="grid grid-cols-2 gap-3">
              <FormField label="Service" required>
                <Select value={serviceName || undefined} onValueChange={(v) => {
                  setServiceName(v)
                  const svc = services.find((x) => x.name === v)
                  if (svc?.envs?.length) setEnv(svc.envs[0])
                }}>
                  <SelectTrigger><SelectValue placeholder="Pick a service" /></SelectTrigger>
                  <SelectContent>
                    {services.map((x) => (
                      <SelectItem key={x.name} value={x.name}>{x.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Environment">
                <Select value={env} onValueChange={setEnv}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {(selectedService?.envs?.length ? selectedService.envs : ['prod', 'staging', 'dev']).map((e) => (
                      <SelectItem key={e} value={e}>{e}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            </div>
          )}

          <FormField label="Operation (optional)" hint="Empty = whole service. Otherwise one operation/route, e.g. GET /api/checkout.">
            <Input value={operation} onChange={(e) => setOperation(e.target.value)} placeholder="Whole service" />
          </FormField>

          <div className="grid grid-cols-3 gap-3">
            <FormField label="SLI">
              <Select value={sliType} onValueChange={setSliType}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="availability">Availability</SelectItem>
                  <SelectItem value="latency">Latency</SelectItem>
                  <SelectItem value="error_rate">Error rate</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Target (%)" required>
              <Input required type="number" step="0.01" min="1" max="99.999"
                value={target} onChange={(e) => setTarget(e.target.value)} />
            </FormField>
            <FormField label="Window">
              <Select value={windowDays} onValueChange={setWindowDays}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="7">7 days</SelectItem>
                  <SelectItem value="30">30 days</SelectItem>
                  <SelectItem value="90">90 days</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
          </div>

          {sliType === 'latency' && (
            <FormField label="Latency threshold (ms)" required
              hint="A request slower than this consumes error budget.">
              <Input required type="number" min="1" value={latencyMs}
                onChange={(e) => setLatencyMs(e.target.value)} />
            </FormField>
          )}

          <div className="flex items-center justify-between rounded-md border border-[var(--bg-elevated)] px-3 py-2">
            <div>
              <div className="text-sm font-medium text-[var(--text-primary)]">Burn-rate alerts</div>
              <div className="text-[11px] text-[var(--text-muted)]">
                Page at 14.4× (1h) and 6× (6h); ticket at 1× (3d).
              </div>
            </div>
            <Switch checked={burnEnabled} onCheckedChange={setBurnEnabled} />
          </div>

          {burnEnabled && (
            <div className="space-y-2">
              <div className="text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)]">Notify channels</div>
              {allChannels.length === 0 ? (
                <p className="text-[11px] text-[var(--text-muted)]">
                  No channels configured — burn alerts will be recorded but not delivered.
                </p>
              ) : (
                <div className="grid grid-cols-2 gap-2">
                  {allChannels.map((c) => {
                    const checked = channels.includes(c.id)
                    return (
                      <button key={c.id} type="button"
                        onClick={() => setChannels(checked ? channels.filter((x) => x !== c.id) : [...channels, c.id])}
                        className={`flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                          checked
                            ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--text-primary)]'
                            : 'border-[var(--bg-elevated)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'
                        }`}>
                        <span className="min-w-0 truncate font-medium">{c.name}</span>
                        {checked && <Check className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending || (!isEdit && !serviceName)}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              {isEdit ? 'Save changes' : 'Create SLO'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
