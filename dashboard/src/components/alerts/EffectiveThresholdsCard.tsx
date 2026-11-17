/* Effective alert thresholds of one device or server (migrate-193, docs/alert-rule-overrides.md): every rule
 * that covers the asset with the values in force for it and where they come from — the rule, an override
 * row, an exclusion or the rule being off. Overrides limited to some instances (interfaces, filesystems)
 * are listed under their rule. Used on the device page; written for servers too. */
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { QueryFailure } from '@/components/ui/QueryState'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { type Condition, holdWords, roleLabel, ruleWords, SOURCE_LABEL } from './thresholdModel'

type Values = { conditions: Condition[]; min_duration: number | null; clear_duration: number | null; priority: string | null }
type OverrideRef = { id: string; target_kind: 'device' | 'server' | 'group'; reason: string; instance_pattern: string | null; expires_at: string | null }

export type EffectiveItem = {
  id: string
  name: string
  enabled: boolean
  metric: string
  severity: string | null
  family?: string | null
  scope?: { kind: string; label: string } | null
  shared: boolean
  excluded: unknown
  active: boolean
  target?: string | null
  if_role: string | null
  pack_key: string | null
  profile_id: string | null
  rule: Values | null
  effective: Values | null
  source: 'rule' | 'override' | 'excluded' | 'disabled'
  override: OverrideRef | null
  instance_overrides: (OverrideRef & Values)[]
}

export type EffectiveResponse = { asset: { kind: string; id: string; name: string | null }; items: EffectiveItem[] }

const SOURCE_VARIANT: Record<string, 'default' | 'info' | 'warning' | 'outline'> = {
  rule: 'default', override: 'info', excluded: 'warning', disabled: 'outline',
}

function expiresWords(iso: string | null): string {
  return iso ? `, until ${new Date(iso).toLocaleString()}` : ''
}

/** A short "what changed" for an instance row against the rule. */
function instanceWords(x: OverrideRef & Values, rule: Values | null): string {
  const parts: string[] = []
  x.conditions.forEach((c, i) => {
    const base = rule?.conditions?.[i]
    if (!base || base.threshold !== c.threshold) parts.push(`threshold ${c.threshold}`)
    if (c.reset_threshold != null && base?.reset_threshold !== c.reset_threshold) parts.push(`clears at ${c.reset_threshold}`)
  })
  if (rule && x.min_duration !== rule.min_duration) parts.push(`hold ${holdWords(x.min_duration).replace(/^for /, '')}`)
  if (rule && x.clear_duration !== rule.clear_duration && x.clear_duration != null) parts.push(`recovery hold ${x.clear_duration} s`)
  if (rule && x.priority !== rule.priority && x.priority) parts.push(`priority ${x.priority}`)
  return parts.join(', ') || 'same values'
}

export function EffectiveThresholdsCard({ kind, id }: { kind: 'device' | 'server'; id: string }) {
  const q = useQuery<EffectiveResponse>({
    queryKey: ['alert-rules', 'effective', kind, id],
    queryFn: async () => (await api.get('/alert-rules/effective', { params: { asset_kind: kind, asset_id: id } })).data,
    staleTime: 30_000,
  })
  // Template metric names (tpl_<key>) from the device's template readings, shared with the device page's cache.
  const insights = useQuery<{ groups?: { metrics?: { key: string; name: string }[] }[] }>({
    queryKey: ['device', id, 'template-insights'],
    queryFn: async () => (await api.get(`/devices/${id}/template-insights`)).data,
    enabled: kind === 'device' && (q.data?.items || []).some((it) => String(it.metric || '').startsWith('tpl_')),
    staleTime: 30_000,
  })
  const names: Record<string, string> = {}
  for (const g of insights.data?.groups || []) for (const m of g.metrics || []) names[m.key] = m.name
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} compact what="alert thresholds" />
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading thresholds…</div>
  const items = q.data?.items || []
  if (!items.length) return <p className="text-sm text-muted">No alert rule covers this {kind}.</p>
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded-md border border-border">
        <Table className="text-sm">
          <THead className="bg-surface2/60">
            <Tr><Th>Rule</Th><Th>In force here</Th><Th>Source</Th></Tr>
          </THead>
          <TBody>
            {items.map((it) => {
              const v = it.effective
              return (
                <Tr key={it.id} className={cn(!it.active && 'text-muted')}>
                  <Td className="align-top">
                    <Link to="/alert-rules" className="font-medium text-primary hover:underline">{it.name}</Link>
                    {(it.if_role || it.target) && (
                      <div className="text-xs text-muted">
                        {[it.if_role ? `${roleLabel(it.if_role)} interfaces` : null, it.target ? `interfaces matching ${it.target}` : null].filter(Boolean).join(' · ')}
                      </div>
                    )}
                  </Td>
                  <Td className="align-top">
                    {v ? (
                      <>
                        <div>{ruleWords(v, names)}</div>
                        {v.priority && <div className="text-xs text-muted">priority {v.priority}</div>}
                      </>
                    ) : <span className="text-muted">—</span>}
                    {it.instance_overrides.length > 0 && (
                      <ul className="mt-1 space-y-0.5 text-xs text-text2" aria-label="Overrides for some instances">
                        {it.instance_overrides.map((x) => (
                          <li key={x.id} title={`${x.target_kind} override: ${x.reason}${expiresWords(x.expires_at)}`}>
                            <span className="font-mono">{x.instance_pattern}</span> → {instanceWords(x, it.rule)}
                          </li>
                        ))}
                      </ul>
                    )}
                  </Td>
                  <Td className="align-top">
                    <Badge variant={SOURCE_VARIANT[it.source] || 'default'}
                      title={it.override ? `${it.override.target_kind} override: ${it.override.reason}${expiresWords(it.override.expires_at)}` : undefined}>
                      {SOURCE_LABEL[it.source] || it.source}
                    </Badge>
                    {it.override && (
                      <div className="mt-0.5 max-w-[16rem] text-xs text-muted">
                        {it.override.target_kind} row: “{it.override.reason}”{expiresWords(it.override.expires_at)}
                      </div>
                    )}
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      </div>
      <p className="text-2xs text-muted">
        Overrides change a rule’s threshold, holds or priority for some assets; the first matching row applies.{' '}
        <Link to="/alert-rules" className="text-primary hover:underline">Manage rules</Link>
      </p>
    </div>
  )
}
