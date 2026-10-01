/**
 * NetFlow scope for reports whose sections read flow data (traffic_*, NetFlow executive summary).
 *
 * The scope is a FlowFilter in canonical keys, kept in the report URL as `nf=<query string>` so a
 * scoped report is shareable, sent to the API as `netflow=<query string>` and stored with a
 * schedule as `filters.netflow` (an object of canonical keys) — the same period and scope every
 * time the schedule runs. The server still restricts every figure to what the viewer may see.
 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, Filter, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FilterChips } from '@/components/netflow/FilterChips'
import {
  FLOW_FILTER_KEYS,
  FLOW_FILTER_LABELS,
  flowFilterFromDraft,
  flowFilterToApiParams,
  type FlowFilter,
  type FlowFilterDraft,
  type FlowFilterKey,
} from '@/lib/netflow/filters'

/** The keys the report scope editor offers (any canonical key in `nf=` is still honoured). */
export const REPORT_SCOPE_KEYS: FlowFilterKey[] = ['exporter', 'group', 'app', 'talker', 'protocol', 'dst_port', 'count']

/** `nf=` URL value -> typed filter (invalid values are dropped and reported by the caller). */
export function parseReportScope(param: string | null | undefined): { filter: FlowFilter; invalid: string[] } {
  if (!param) return { filter: {}, invalid: [] }
  const sp = new URLSearchParams(param)
  const draft: FlowFilterDraft = {}
  for (const k of FLOW_FILTER_KEYS) {
    const v = sp.get(k)
    if (v !== null && v.trim() !== '') draft[k] = v
  }
  const parsed = flowFilterFromDraft(draft)
  return { filter: parsed.filter, invalid: Object.keys(parsed.errors) }
}

/** Typed filter -> canonical query string ('' when empty). */
export function reportScopeParam(f: FlowFilter): string {
  return new URLSearchParams(flowFilterToApiParams(f)).toString()
}

/** Canonical-key object stored with a schedule (`filters.netflow`). */
export function reportScopeObject(f: FlowFilter): Record<string, string> {
  return flowFilterToApiParams(f)
}

interface ExporterRow {
  exporter: string
  display_name?: string | null
  device_hostname?: string | null
}

export function NetflowScopePicker({ value, onChange }: { value: FlowFilter; onChange: (f: FlowFilter) => void }) {
  const [open, setOpen] = useState(false)
  const [alignRight, setAlignRight] = useState(true)
  const [draft, setDraft] = useState<FlowFilterDraft>({})
  const ref = useRef<HTMLDivElement>(null)
  const id = useId()
  const exporters = useQuery<ExporterRow[]>({
    queryKey: ['netflow', 'exporters', 'status', 'report-scope'],
    queryFn: async () => {
      const body = (await api.get('/netflow/exporters/status')).data
      const rows = body?.data?.exporters ?? body?.exporters ?? []
      return Array.isArray(rows) ? rows : []
    },
    enabled: open,
    staleTime: 60_000,
    retry: false,
  })
  const catalog = useQuery<{ applications?: string[]; groups?: string[] }>({
    queryKey: ['alert-rules', 'netflow', 'catalog'],
    queryFn: async () => (await api.get('/alert-rules/netflow/catalog')).data,
    enabled: open,
    staleTime: 5 * 60_000,
  })

  useEffect(() => {
    if (!open) return
    const current = flowFilterToApiParams(value)
    setDraft(Object.fromEntries(REPORT_SCOPE_KEYS.map((k) => [k, current[k] ?? ''])) as FlowFilterDraft)
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, value])

  const parsed = useMemo(() => flowFilterFromDraft(draft), [draft])
  const errorKeys = Object.keys(parsed.errors) as FlowFilterKey[]
  const count = Object.keys(flowFilterToApiParams(value)).length

  const apply = () => {
    if (errorKeys.length) return
    // Keys the editor does not show are kept (a shared link may carry more).
    const keep = Object.fromEntries(
      Object.entries(value).filter(([k]) => !REPORT_SCOPE_KEYS.includes(k as FlowFilterKey)),
    ) as FlowFilter
    onChange({ ...keep, ...parsed.filter })
    setOpen(false)
  }

  const field = (k: FlowFilterKey, control: ReactNode) => (
    <div key={k} className="space-y-1">
      <label htmlFor={`${id}-${k}`} className="block text-[11px] font-medium text-text2">{FLOW_FILTER_LABELS[k]}</label>
      {control}
      {parsed.errors[k] && <p className="text-[11px] font-medium text-danger-fg">{parsed.errors[k]}</p>}
    </div>
  )
  const text = (k: FlowFilterKey, placeholder: string, list?: string) =>
    field(k, (
      <Input id={`${id}-${k}`} value={draft[k] ?? ''} placeholder={placeholder} list={list}
        className="h-8 text-xs" onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))} />
    ))

  return (
    <div ref={ref} className="relative">
      <Button variant="outline" size="sm" className="h-8 gap-1.5" aria-expanded={open} aria-haspopup="dialog"
        onClick={() => {
          // Open towards the side with room: on a narrow screen the button sits at the left edge.
          const rect = ref.current?.getBoundingClientRect()
          if (rect) setAlignRight(rect.right - 352 >= 8)
          setOpen((v) => !v)
        }}>
        <Filter className="h-3.5 w-3.5" aria-hidden />
        <span className="text-xs font-semibold">{count ? `NetFlow scope (${count})` : 'NetFlow scope'}</span>
        <ChevronDown className="h-3 w-3 opacity-60" aria-hidden />
      </Button>
      {open && (
        <div role="dialog" aria-label="NetFlow scope"
          className={cn('absolute z-30 mt-1 w-[min(22rem,calc(100vw-2rem))] space-y-3 rounded-lg border border-border bg-surface p-3 shadow-elevated',
            alignRight ? 'right-0' : 'left-0')}>
          <p className="text-[11px] text-text2">
            Narrow the report's flow sections. Figures are always limited to the exporters you may see.
          </p>
          {field('exporter', (
            <select id={`${id}-exporter`} value={draft.exporter ?? ''}
              onChange={(e) => setDraft((d) => ({ ...d, exporter: e.target.value }))}
              className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs text-text">
              <option value="">All visible exporters</option>
              {draft.exporter && !(exporters.data ?? []).some((r) => r.exporter === draft.exporter) && (
                <option value={draft.exporter}>{draft.exporter}</option>
              )}
              {(exporters.data ?? []).map((r) => (
                <option key={r.exporter} value={r.exporter}>
                  {(r.display_name || r.device_hostname) ? `${r.display_name || r.device_hostname} (${r.exporter})` : r.exporter}
                </option>
              ))}
            </select>
          ))}
          <div className="grid grid-cols-2 gap-2">
            {text('group', 'Any group', `${id}-groups`)}
            {text('app', 'Any application', `${id}-apps`)}
            {text('talker', 'IP or CIDR')}
            {text('dst_port', '0–65535')}
          </div>
          <datalist id={`${id}-groups`}>{(catalog.data?.groups ?? []).map((g) => <option key={g} value={g} />)}</datalist>
          <datalist id={`${id}-apps`}>{(catalog.data?.applications ?? []).map((a) => <option key={a} value={a} />)}</datalist>
          {field('count', (
            <select id={`${id}-count`} value={draft.count ?? ''}
              onChange={(e) => setDraft((d) => ({ ...d, count: e.target.value }))}
              className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs text-text">
              <option value="">Every observation</option>
              <option value="edge">Edge exporters only (each flow once)</option>
            </select>
          ))}
          <div className="flex items-center justify-between gap-2 border-t border-border pt-2">
            <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={() => { onChange({}); setOpen(false) }}>
              <X className="h-3.5 w-3.5" aria-hidden /> Clear scope
            </Button>
            <Button size="sm" className="h-8 text-xs" disabled={errorKeys.length > 0} onClick={apply}>Apply</Button>
          </div>
        </div>
      )}
    </div>
  )
}

/** Applied scope as removable chips under the report header. */
export function NetflowScopeChips({ value, onChange, className }: { value: FlowFilter; onChange: (f: FlowFilter) => void; className?: string }) {
  if (!Object.keys(flowFilterToApiParams(value)).length) return null
  return (
    <div className={cn('flex flex-wrap items-center gap-2', className)}>
      <span className="text-[11px] font-semibold uppercase tracking-wide text-text2">NetFlow scope</span>
      <FilterChips
        filter={value}
        onRemove={(keys) => {
          const next = { ...value } as Record<string, unknown>
          for (const k of keys) delete next[k]
          onChange(next as FlowFilter)
        }}
        onClearAll={() => onChange({})}
      />
    </div>
  )
}
