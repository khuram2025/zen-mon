// Shared pieces of the Discovery rules UI: catalog and reference lookups, mode/decision chips and the "why" popover.
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { HelpCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/Badge'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { cn } from '@/lib/utils'
import { discoveryApi, rulesApi } from './api'
import type { RuleCatalog, RuleMode, RulePlan } from './types'
import {
  DECISION_LABEL,
  IMPORT_AS_LABEL,
  Lookups,
  MODE_LABEL,
  MODE_TONE,
  SKIP_TEXT,
  dispositionLabel,
  dispositionTone,
  reasonText,
  summarizeAction,
  traceLeafText,
} from './ruleModel'

export type RefOption = { id: string; name: string }

export function useRuleCatalog(enabled = true) {
  return useQuery<RuleCatalog>({
    queryKey: ['discovery', 'rules', 'catalog'],
    queryFn: rulesApi.catalog,
    staleTime: 5 * 60_000,
    enabled,
  })
}

const asOptions = (rows: any): RefOption[] =>
  (Array.isArray(rows) ? rows : Array.isArray(rows?.data) ? rows.data : Array.isArray(rows?.items) ? rows.items : [])
    .filter((r: any) => r && r.id != null)
    .map((r: any) => ({ id: String(r.id), name: String(r.name ?? r.label ?? r.id) }))

/**
 * Names for the ids a rule refers to (groups, templates, SNMP credentials, discovery profiles). Each list comes from
 * the API the rest of the dashboard uses; a role that cannot read one of them sees the ids instead of names.
 */
export function useRuleLookups(enabled = true): { lookups: Lookups; options: Record<string, RefOption[]> } {
  const opts = { enabled, retry: false, staleTime: 60_000 } as const
  const groups = useQuery<any>({ queryKey: ['device-groups'], queryFn: async () => (await api.get('/devices/groups')).data, ...opts })
  const templates = useQuery<any>({ queryKey: ['snmp-profiles'], queryFn: async () => (await api.get('/snmp/profiles')).data, ...opts })
  const creds = useQuery<any>({ queryKey: ['snmp-credentials'], queryFn: async () => (await api.get('/snmp-credentials')).data, ...opts })
  const profiles = useQuery<any>({ queryKey: ['discovery', 'profiles'], queryFn: discoveryApi.listProfiles, ...opts })
  return useMemo(() => {
    const options: Record<string, RefOption[]> = {
      device_groups: asOptions(groups.data),
      device_profiles: asOptions(templates.data),
      snmp_credentials: asOptions(creds.data),
      discovery_profiles: asOptions(profiles.data),
    }
    const lookups: Lookups = {}
    for (const [source, list] of Object.entries(options)) {
      lookups[source] = Object.fromEntries(list.map((o) => [o.id, o.name]))
    }
    return { lookups, options }
  }, [groups.data, templates.data, creds.data, profiles.data])
}

export function ModeBadge({ mode }: { mode: RuleMode }) {
  return <Badge variant={MODE_TONE[mode] as any}>{MODE_LABEL[mode] ?? mode}</Badge>
}

export function DispositionBadge({ disposition, reason }: { disposition: string | null | undefined; reason?: string | null }) {
  const r = reasonText(reason)
  return (
    <div className="space-y-0.5">
      <Badge variant={dispositionTone(disposition) as any} className="whitespace-nowrap">{dispositionLabel(disposition)}</Badge>
      {r && <div className="text-2xs text-muted">{r}</div>}
    </div>
  )
}

/** Matched rules, the settings they set, shadowed values and each rule's evaluated tests with ✓ / ✗. */
export function PlanDetails({ plan, catalog, lookups }: { plan: RulePlan; catalog: RuleCatalog | null | undefined; lookups?: Lookups }) {
  const settings = Object.entries(plan.settings || {})
  return (
    <div className="space-y-3 text-xs">
      <div>
        <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Matched rules</div>
        {plan.matched?.length ? (
          <ol className="list-decimal space-y-0.5 pl-4">
            {plan.matched.map((m) => (
              <li key={m.rule_id}>
                {m.rule_name || m.rule_id}
                {plan.stopped_by === m.rule_id && <span className="ml-1 text-muted">(stopped further rules)</span>}
              </li>
            ))}
          </ol>
        ) : (
          <div className="text-muted">No rule matched.</div>
        )}
      </div>
      {(settings.length > 0 || (plan.tags?.length ?? 0) > 0) && (
        <div>
          <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Result</div>
          <ul className="space-y-0.5">
            {settings.map(([key, s]) => (
              <li key={key}>
                {key === 'decision'
                  ? `${DECISION_LABEL[s.value] ?? s.value}${s.value === 'auto_import' ? ` as ${IMPORT_AS_LABEL[s.import_as || 'device'] ?? s.import_as}` : ''}`
                  : summarizeAction({ key, value: s.value, import_as: s.import_as }, lookups, catalog)}
                <span className="text-muted"> — {s.rule_name}</span>
              </li>
            ))}
            {plan.tags?.length > 0 && (
              <li>
                Tags: {plan.tags.map((t) => t.value).join(', ')}
              </li>
            )}
          </ul>
        </div>
      )}
      {plan.shadowed?.length > 0 && (
        <div>
          <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Overridden by an earlier rule</div>
          <ul className="space-y-0.5 text-muted">
            {plan.shadowed.map((s, i) => (
              <li key={i}>
                {s.rule_name}: {summarizeAction({ key: s.key, value: s.value }, lookups, catalog)} (kept {s.winner_rule_name})
              </li>
            ))}
          </ul>
        </div>
      )}
      {plan.trace?.length > 0 && (
        <div>
          <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Why</div>
          <ul className="space-y-1.5">
            {plan.trace.map((t) => (
              <li key={t.rule_id}>
                <div className={cn('font-medium', t.matched ? 'text-success-fg' : 'text-muted')}>
                  {t.matched ? '✓' : '✗'} {t.rule_name || t.rule_id}
                  {t.skipped && <span className="ml-1 font-normal text-muted">— skipped, {SKIP_TEXT[t.skipped] ?? t.skipped}</span>}
                </div>
                {(t.leaves?.length ?? 0) > 0 && (
                  <ul className="ml-4 space-y-0.5 text-muted">
                    {t.leaves!.map((l, i) => (
                      <li key={i} className={l[2] ? 'text-success-fg' : ''}>{traceLeafText(l, catalog)}</li>
                    ))}
                  </ul>
                )}
                {!t.skipped && (t.leaves?.length ?? 0) === 0 && t.matched && (
                  <div className="ml-4 text-muted">No conditions: matches every asset</div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {plan.reason && <div className="text-muted">Note: {reasonText(plan.reason)}</div>}
    </div>
  )
}

export function PlanWhy({ plan, catalog, lookups, label = 'Why' }: { plan: RulePlan | null | undefined; catalog: RuleCatalog | null | undefined; lookups?: Lookups; label?: string }) {
  if (!plan) return null
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-0.5 text-2xs text-primary hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          <HelpCircle className="h-3 w-3" /> {label}
        </button>
      </PopoverTrigger>
      <PopoverContent>
        <PlanDetails plan={plan} catalog={catalog} lookups={lookups} />
      </PopoverContent>
    </Popover>
  )
}
