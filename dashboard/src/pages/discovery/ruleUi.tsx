// Shared pieces of the Discovery rules UI: catalog and reference lookups, mode/decision chips, the "why" popover and
// the table of planned changes (run preview and approval review).
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueries, useQuery } from '@tanstack/react-query'
import { HelpCircle, Search, X } from 'lucide-react'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { cn } from '@/lib/utils'
import { discoveryApi, rulesApi } from './api'
import type { InventoryChange, RuleCatalog, RuleMode, RulePlan } from './types'
import {
  CHANGE_OUTCOME_LABEL,
  CHANGE_OUTCOME_TONE,
  assetPath,
  proposedText,
  settingLabel,
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

/** A choice for a reference: `hint` is a second, muted part of the label (a sensor's site and status, a policy's platform). */
export type RefOption = { id: string; name: string; hint?: string }

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
 * Names for the ids a rule refers to (groups, templates, SNMP credentials, discovery profiles, maps, and since phase 4
 * sensors, agent policies and service check templates). Each list comes from the API the rest of the dashboard uses; a
 * role that cannot read one of them sees the ids instead of names.
 */
export function useRuleLookups(enabled = true): { lookups: Lookups; options: Record<string, RefOption[]> } {
  const opts = { enabled, retry: false, staleTime: 60_000 } as const
  const groups = useQuery<any>({ queryKey: ['device-groups'], queryFn: async () => (await api.get('/devices/groups')).data, ...opts })
  const templates = useQuery<any>({ queryKey: ['snmp-profiles'], queryFn: async () => (await api.get('/snmp/profiles')).data, ...opts })
  const creds = useQuery<any>({ queryKey: ['snmp-credentials'], queryFn: async () => (await api.get('/snmp-credentials')).data, ...opts })
  const profiles = useQuery<any>({ queryKey: ['discovery', 'profiles'], queryFn: discoveryApi.listProfiles, ...opts })
  // Network maps (the "Add to network map" action); GET /maps never returns background images.
  const maps = useQuery<any>({ queryKey: ['manual-maps'], queryFn: async () => (await api.get('/maps')).data, ...opts })
  // Phase 4: "Monitor from sensor", "Agent policy" (action and fact), "Add service check from template". Own keys: other
  // pages cache these lists in other shapes (GET /agent-policies answers {items}).
  const sensors = useQuery<any>({ queryKey: ['discovery', 'rules', 'ref', 'sensors'], queryFn: async () => (await api.get('/sensors')).data, ...opts })
  const policies = useQuery<any>({ queryKey: ['discovery', 'rules', 'ref', 'agent-policies'], queryFn: async () => (await api.get('/agent-policies')).data, ...opts })
  // "Site" (servers): GET /sites, the list the server and sensor pages use.
  const sites = useQuery<any>({ queryKey: ['sites'], queryFn: async () => (await api.get('/sites')).data, ...opts })
  const svcTemplates = useQuery<any>({
    queryKey: ['discovery', 'rules', 'ref', 'service-check-templates'],
    queryFn: async () => (await api.get('/service-check-templates')).data,
    ...opts,
  })
  return useMemo(() => {
    const options: Record<string, RefOption[]> = {
      // A rule sets a device's own group: static groups only (migrate-192).
      device_groups: asOptions((Array.isArray(groups.data) ? groups.data : []).filter((g: any) => (g.kind ?? 'static') === 'static')),
      device_profiles: asOptions(templates.data),
      snmp_credentials: asOptions(creds.data),
      discovery_profiles: asOptions(profiles.data),
      manual_maps: asOptions(maps.data),
      sensors: withHints(sensors.data, (r) => [r.site_name, r.status && r.status !== 'online' ? r.status : null]),
      agent_policies: withHints(policies.data, (r) => [r.platform]),
      service_check_templates: withHints(svcTemplates.data, (r) => [r.check_type ?? r.type]),
      sites: asOptions(sites.data),
    }
    const lookups: Lookups = {}
    for (const [source, list] of Object.entries(options)) {
      lookups[source] = Object.fromEntries(list.map((o) => [o.id, o.name]))
    }
    return { lookups, options }
  }, [groups.data, templates.data, creds.data, profiles.data, maps.data, sensors.data, policies.data, svcTemplates.data, sites.data])
}

function withHints(rows: any, hint: (row: any) => (string | null | undefined)[]): RefOption[] {
  const list: any[] = Array.isArray(rows) ? rows : Array.isArray(rows?.items) ? rows.items : Array.isArray(rows?.data) ? rows.data : []
  const byId = new Map(list.filter((r) => r && r.id != null).map((r) => [String(r.id), r]))
  return asOptions(list).map((o) => {
    const h = hint(byId.get(o.id) || {}).filter(Boolean).join(' · ')
    return h ? { ...o, hint: h } : o
  })
}

/**
 * `lookups` plus the names of the given devices (uplink parents of the "Depends on" action), each read with
 * GET /devices/{id}; a device the viewer cannot see keeps its short id.
 */
export function useWithDeviceNames(lookups: Lookups, ids: string[]): Lookups {
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: ['device', id, 'name'],
      queryFn: async () => (await api.get(`/devices/${id}`)).data as { id: string; hostname?: string; ip_address?: string },
      retry: false,
      staleTime: 5 * 60_000,
    })),
  })
  const names = results.map((r) => (r.data ? r.data.hostname || r.data.ip_address || '' : '')).join('|')
  return useMemo(() => {
    if (!ids.length) return lookups
    const devices: Record<string, string> = { ...(lookups.devices || {}) }
    ids.forEach((id, i) => {
      const d = results[i]?.data
      const name = d?.hostname || d?.ip_address
      if (name) devices[id] = name
    })
    return { ...lookups, devices }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lookups, ids.join(','), names])
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

/**
 * Planned changes on devices, servers and controller-managed children: asset, setting, current → new (or the revert),
 * rule and outcome. Used by Run on inventory's preview and by the review of a run waiting for approval.
 */
export function ChangesTable({
  changes,
  catalog,
  lookups,
}: {
  changes: InventoryChange[]
  catalog: RuleCatalog | null | undefined
  lookups?: Lookups
}) {
  return (
    <div className="rounded-md border border-border">
      <Table>
        <THead className="bg-surface2/50">
          <Tr>
            <Th>Asset</Th>
            <Th>Setting</Th>
            <Th>Current → new</Th>
            <Th>Rule</Th>
            <Th>Outcome</Th>
          </Tr>
        </THead>
        <TBody>
          {changes.map((c, i) => {
            const href = assetPath(c.asset_kind, c.asset_id)
            const kindWord = c.asset_kind === 'server' ? 'server' : c.asset_kind === 'child' ? 'managed switch / AP' : ''
            return (
              <Tr key={`${c.asset_kind}:${c.asset_id}:${c.action_key}:${c.item_key}:${i}`}>
                <Td className="text-xs">
                  {href ? (
                    <Link to={href} className="font-medium text-primary hover:underline">
                      {c.asset_name || c.ip_address || c.asset_id.slice(0, 8)}
                    </Link>
                  ) : (
                    c.asset_name || '—'
                  )}
                  <div className="text-2xs text-muted">
                    {c.ip_address && <span className="font-mono">{c.ip_address}</span>}
                    {kindWord ? `${c.ip_address ? ' · ' : ''}${kindWord}` : ''}
                  </div>
                </Td>
                <Td className="text-xs">{settingLabel(c.action_key, catalog)}</Td>
                <Td className={cn('text-xs', c.outcome === 'revert' && 'text-warning-fg')}>{proposedText(c, lookups)}</Td>
                <Td className="text-xs">
                  {c.rule_id ? (
                    <Link to={`/discovery/rules/${c.rule_id}`} className="hover:underline">
                      {c.rule_name || c.rule_id.slice(0, 8)}
                    </Link>
                  ) : (
                    c.rule_name || '—'
                  )}
                </Td>
                <Td>
                  <Badge variant={(CHANGE_OUTCOME_TONE as Record<string, string>)[c.outcome] as any ?? 'default'} className="whitespace-nowrap">
                    {(CHANGE_OUTCOME_LABEL as Record<string, string>)[c.outcome] ?? c.outcome}
                  </Badge>
                  {c.detail && <div className="mt-0.5 max-w-xs text-2xs text-muted">{c.detail}</div>}
                </Td>
              </Tr>
            )
          })}
        </TBody>
      </Table>
    </div>
  )
}

/**
 * Pick one device by searching the device list (GET /devices?search=), for the "Depends on (uplink parent)" action.
 * Only ids are stored; the chosen device's name comes from `lookups.devices`.
 */
export function DevicePicker({
  value,
  lookups,
  onChange,
  label = 'Device',
}: {
  value: string
  lookups: Lookups
  onChange: (id: string) => void
  label?: string
}) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(!value)
  const search = q.trim()
  const { data: found = [], isFetching, error } = useQuery<{ id: string; hostname?: string; ip_address?: string; device_type?: string }[]>({
    queryKey: ['discovery', 'rules', 'device-search', search],
    queryFn: async () => (await api.get('/devices', { params: { limit: 20, ...(search ? { search } : {}) } })).data?.data ?? [],
    enabled: open,
    retry: false,
    staleTime: 30_000,
  })
  const name = value ? lookups.devices?.[value.toLowerCase()] || lookups.devices?.[value] : ''
  return (
    <div className="space-y-1.5">
      {value && (
        <div className="flex items-center gap-2 text-sm">
          <Link to={`/devices/${value}`} className="font-medium text-primary hover:underline">
            {name || `${value.slice(0, 8)}…`}
          </Link>
          <button type="button" className="text-xs text-muted hover:text-text" onClick={() => setOpen(!open)}>
            {open ? 'Close search' : 'Change'}
          </button>
          <button type="button" className="text-muted hover:text-danger" title="Clear" aria-label="Clear the device" onClick={() => { onChange(''); setOpen(true) }}>
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      {open && (
        <>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              aria-label={`Search ${label.toLowerCase()}`}
              className="pl-8"
              placeholder="Search by name or IP address"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          <div className="max-h-48 overflow-y-auto rounded-md border border-border bg-surface">
            {error ? (
              <div className="px-3 py-2 text-xs text-muted">The device list could not be loaded.</div>
            ) : found.length === 0 ? (
              <div className="px-3 py-2 text-xs text-muted">{isFetching ? 'Searching…' : 'No device matches.'}</div>
            ) : (
              <ul className="divide-y divide-border/60">
                {found.map((d) => (
                  <li key={d.id}>
                    <button
                      type="button"
                      className={cn(
                        'flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-sm hover:bg-surface2',
                        d.id === value && 'bg-primary/10',
                      )}
                      onClick={() => {
                        onChange(d.id)
                        setOpen(false)
                        setQ('')
                      }}
                    >
                      <span className="font-medium">{d.hostname || d.ip_address || d.id.slice(0, 8)}</span>
                      {d.ip_address && <span className="font-mono text-2xs text-muted">{d.ip_address}</span>}
                      {d.device_type && <span className="ml-auto text-2xs text-muted">{d.device_type.replace(/_/g, ' ')}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  )
}

/** The browser's time zone names for the schedule's time zone field (none on a browser that cannot list them). */
export function TimeZoneList({ id }: { id: string }) {
  const zones = useMemo(() => {
    try {
      const list = (Intl as any).supportedValuesOf?.('timeZone') as string[] | undefined
      return Array.isArray(list) ? Array.from(new Set(['UTC', ...list])) : ['UTC']
    } catch {
      return ['UTC']
    }
  }, [])
  return (
    <datalist id={id}>
      {zones.map((z) => (
        <option key={z} value={z} />
      ))}
    </datalist>
  )
}
