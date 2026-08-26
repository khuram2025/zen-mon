import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Bell } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/Dialog'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'

const METRICS: Array<{ key: string; label: string; hint: string; defaultThreshold: string; unit: string }> = [
  { key: 'apm_latency_p95', label: 'p95 latency', hint: 'milliseconds', defaultThreshold: '1000', unit: 'ms' },
  { key: 'apm_latency_p50', label: 'p50 latency', hint: 'milliseconds', defaultThreshold: '400', unit: 'ms' },
  { key: 'apm_error_rate', label: 'Error rate', hint: 'fraction 0–1 (0.05 = 5%)', defaultThreshold: '0.05', unit: '' },
  { key: 'apm_throughput', label: 'Throughput', hint: 'requests / second', defaultThreshold: '1', unit: 'req/s' },
  { key: 'apm_apdex', label: 'Apdex', hint: 'fraction 0–1', defaultThreshold: '0.85', unit: '' },
]

export function CreateMonitorDialog({
  open, onOpenChange, service, suggestedMetric = 'apm_latency_p95', suggestedThreshold,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  service: string
  suggestedMetric?: string
  suggestedThreshold?: number
}) {
  const suggested = METRICS.find((m) => m.key === suggestedMetric) || METRICS[0]
  const [metric, setMetric] = useState(suggested.key)
  const [operator, setOperator] = useState(suggested.key === 'apm_apdex' || suggested.key === 'apm_throughput' ? '<' : '>')
  const [threshold, setThreshold] = useState(
    suggestedThreshold != null ? String(suggestedThreshold) : suggested.defaultThreshold,
  )
  const [enabled, setEnabled] = useState(false)
  const current = METRICS.find((m) => m.key === metric) || suggested

  useEffect(() => {
    if (!open) return
    const m = METRICS.find((x) => x.key === suggestedMetric) || METRICS[0]
    setMetric(m.key)
    setOperator(m.key === 'apm_apdex' || m.key === 'apm_throughput' ? '<' : '>')
    setThreshold(suggestedThreshold != null ? String(Math.round(suggestedThreshold)) : m.defaultThreshold)
    setEnabled(false)
  }, [open, suggestedMetric, suggestedThreshold])

  const create = useMutation({
    mutationFn: async () => {
      const name = `${service} ${current.label} ${operator} ${threshold}${current.unit}`
      return (await api.post('/alert-rules', {
        name,
        metric,
        operator,
        threshold: Number(threshold),
        target: service,
        severity: 'warning',
        enabled,
        notify_channels: [],
      })).data
    },
    onSuccess: (rule: { id?: string; name?: string }) => {
      toast.success(enabled ? 'Monitor enabled' : 'Monitor saved (disabled)', rule.name || 'Alert rule created')
      onOpenChange(false)
    },
    onError: (e: unknown) => toast.error('Could not create monitor', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Bell className="h-4 w-4" /> Create APM monitor</DialogTitle>
          <DialogDescription>
            Alert when <span className="font-medium text-text">{service}</span> crosses a golden-signal threshold.
            Latency is in milliseconds; error rate and Apdex are fractions (0.02 = 2%).
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <label className="block text-xs font-medium text-muted">
            Metric
            <select
              className="mt-1 h-9 w-full rounded-md border border-border bg-surface2 px-2 text-sm text-text"
              value={metric}
              onChange={(e) => {
                const next = e.target.value
                setMetric(next)
                const m = METRICS.find((x) => x.key === next)
                if (m) setThreshold(m.defaultThreshold)
                setOperator(next === 'apm_apdex' || next === 'apm_throughput' ? '<' : '>')
              }}
            >
              {METRICS.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
            </select>
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-xs font-medium text-muted">
              When
              <select
                className="mt-1 h-9 w-full rounded-md border border-border bg-surface2 px-2 text-sm text-text"
                value={operator}
                onChange={(e) => setOperator(e.target.value)}
              >
                <option value=">">greater than</option>
                <option value=">=">at least</option>
                <option value="<">less than</option>
                <option value="<=">at most</option>
              </select>
            </label>
            <label className="block text-xs font-medium text-muted">
              Threshold {current.unit && `(${current.unit})`}
              <Input className="mt-1" type="number" step="any" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
            </label>
          </div>
          <p className="text-[11px] text-muted">{current.hint}</p>
          <label className="flex items-center gap-2 text-xs text-muted">
            <Switch checked={enabled} onCheckedChange={setEnabled} />
            Enable immediately (leave off to save without paging)
          </label>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => create.mutate()} disabled={create.isPending || !threshold}>
            {create.isPending ? 'Saving…' : 'Create monitor'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
