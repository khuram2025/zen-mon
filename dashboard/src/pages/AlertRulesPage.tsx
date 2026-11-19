import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, BellOff, Copy, Eye, Pencil, Plus, SlidersHorizontal, Trash2, Zap } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Switch } from '@/components/ui/Switch'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { AlertRuleWizardDialog } from '@/components/forms/AlertRuleWizardDialog'
import { AlertMessagePreviewDialog } from '@/components/alerts/AlertMessagePreviewDialog'
import { RuleExclusionsDialog } from '@/components/alerts/RuleExclusions'
import { RuleOverridesDialog } from '@/components/alerts/RuleOverrides'
import { roleLabel, ruleTakesOverrides } from '@/components/alerts/thresholdModel'
import { exclusionBadge } from '@/components/alerts/exclusionModel'
import { toast } from '@/components/ui/Toast'
import { NETFLOW_METRIC_BY_KEY, formFromRule, isNetflowMetric, summarizeNetflowForm, type NetflowMetric } from '@/components/forms/netflowAlertRule'
import { downRuleCoversServers, isServerMetric, serverRuleCopy, serverScopeLabel } from '@/components/forms/serverAlertRule'
import { groupsApi } from '@/pages/groups/api'
import { effectivePriority, policyForPriority, ruleEscalationSummary, ruleRouteSource } from '@/features/escalation/model'
import { usePolicies } from '@/features/escalation/api'
import type { EscalationPolicy } from '@/features/escalation/types'
import { PriorityBadge } from '@/features/escalation/ui'
import { apmEventScope, isApmEvent } from '@/components/forms/apmEventTriggers'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import {
  RULE_SOURCES, closesText, metricSpec, routingNote, ruleSource, templateIndex, timersText, triggerSummary,
  type RouteSourceLike, type RuleSource,
} from '@/components/forms/alertRuleCatalog'
import { eventExpiryFromApi, expiryWindowLabel } from '@/features/escalation/eventExpiry'
import { RecommendedRulesPanel } from '@/pages/alertRules/RecommendedRulesPanel'

function scopeLabel(r: any, serverNames: Map<string, string>, groupNames: Map<string, string>, channelNames?: Map<string, string>): string {
  if (isApmEvent(r.metric)) return r.target ? (r.metric === 'apm_synthetic_down' ? 'One scenario' : 'One SLO') : apmEventScope(r.metric).all
  const family = ruleSource(r)
  // The families added in the alerting audit: their scope is not the device one.
  if (family === 'appliance') {
    const kind = metricSpec(r.metric)?.target
    if (kind && r.target) return kind === 'channel' ? `Channel ${channelNames?.get(r.target) || r.target}` : `Sensor ${r.target.length > 12 ? 'one sensor' : r.target}`
    return 'ZenPlus appliance'
  }
  if (family === 'virtualization') {
    if (r.target) return 'One virtualisation source'
    if (r.device_id) return 'Linked device'
    if (r.scope_tag) return `#${r.scope_tag}`
    return 'All sources'
  }
  if (family === 'netpath') return r.target ? `Probe ${r.target}` : 'All probes'
  if (r.metric === 'agent_no_metrics') {
    const group = r.group_id ? groupNames.get(r.group_id) || 'a group' : ''
    return group ? `Servers in ${group}` : r.scope_tag ? `Servers with tag ${r.scope_tag}` : 'All agent servers'
  }
  if (r.metric === 'service_tls_days' || r.metric === 'service_response_ms') {
    if (r.service_check_id) return 'Service'
    if (r.service_check_group_id) return 'Svc group'
    if (r.device_id) return 'Linked device'
    if (r.scope_tag) return `#${r.scope_tag}`
    return 'All checks'
  }
  // One server, all servers, or the servers with a tag and/or in a device group.
  if (isServerMetric(r.metric)) {
    return serverScopeLabel(r, { server: r.server_id ? serverNames.get(r.server_id) : null, group: r.group_id ? groupNames.get(r.group_id) : null })
  }
  if (r.family === 'netflow' || isNetflowMetric(r.metric)) {
    const scope = r.flow_scope && typeof r.flow_scope === 'object' ? r.flow_scope : {}
    const exporter = scope.exporter || String(scope.iface || '').split('@')[1]
    const others = Object.keys(scope).filter((k) => k !== 'exporter' && !(k === 'iface' && !scope.exporter)).length
    if (exporter) return others ? `NetFlow ${exporter} +${others}` : `NetFlow ${exporter}`
    return others ? `NetFlow · ${others} filter${others === 1 ? '' : 's'}` : 'NetFlow (all)'
  }
  if (r.service_check_id) return 'Service'
  if (r.service_check_group_id) return 'Svc group'
  if (r.group_id) return 'Group'
  if (r.device_type) return r.device_type
  if (r.location) return r.location
  if (r.scope_tag) return `#${r.scope_tag}`
  if (r.device_id) return 'Device'
  if (r.metric === 'trap') return 'Traps'
  // An unscoped Device Down rule also pages for agent servers (server_availability.is_down_rule).
  if (downRuleCoversServers(r)) return 'All + servers'
  return 'All'
}

/** The escalation route the server resolves for a rule; null while the policies are unknown. */
function routeOf(rule: any, policies: EscalationPolicy[] | null): RouteSourceLike | null {
  if (!policies) return null
  const priority = effectivePriority(rule.priority, rule.severity)
  const tiers = Array.isArray(rule.escalation_levels) ? rule.escalation_levels.length : 0
  const policy = rule.escalation_policy_id ? policies.find((p) => p.id === rule.escalation_policy_id) ?? null : null
  return ruleRouteSource({ policyId: rule.escalation_policy_id, policyEnabled: policy?.enabled, tiers, defaultPolicy: policyForPriority(policies, priority) })
}

/**
 * Who the rule's alerts reach: its channels (named), or only an escalation route, or nobody. A rule with no
 * channel, no policy, no legacy tiers and no default policy for its priority raises alerts nobody is told about.
 */
function RuleNotifies({ rule, policies, channelNames }: { rule: any; policies: EscalationPolicy[] | null; channelNames: Map<string, string> }) {
  const ids: string[] = Array.isArray(rule.notify_channels) ? rule.notify_channels : []
  const note = routingNote(ids.length, routeOf(rule, policies))
  const names = ids.map((id) => channelNames.get(id)).filter(Boolean) as string[]
  return (
    <div className="text-xs">
      <div className={note.tone === 'danger' ? 'font-medium text-danger' : note.tone === 'ok' ? 'text-text' : 'text-muted'}
        title={names.length ? names.join(', ') : undefined}>
        {note.tone === 'ok' && names.length ? names.slice(0, 2).join(', ') + (names.length > 2 ? ` +${names.length - 2}` : '') : note.text}
      </div>
      <RuleEscalationLine rule={rule} policies={policies} />
    </div>
  )
}

/**
 * The rule's escalation route as the server resolves it: its own enabled policy > its own legacy tiers >
 * the priority default policy > nothing. Without the policy list the policy counts as enabled and the default is unknown.
 */
function RuleEscalationLine({ rule, policies }: { rule: any; policies: EscalationPolicy[] | null }) {
  const priority = effectivePriority(rule.priority, rule.severity)
  const tiers: number[] = Array.isArray(rule.escalation_levels)
    ? rule.escalation_levels.map((lv: any) => Number(lv?.after_minutes)).filter((m: number) => Number.isFinite(m))
    : []
  const policy = rule.escalation_policy_id && policies ? policies.find((p) => p.id === rule.escalation_policy_id) ?? null : null
  const defaultPolicy = policies ? policyForPriority(policies, priority) : null
  const source = ruleRouteSource({ policyId: rule.escalation_policy_id, policyEnabled: policy?.enabled, tiers: tiers.length, defaultPolicy })
  if (source === 'none' && !rule.escalation_policy_id) return null
  if (source === 'priority_default' && !rule.escalation_policy_id) {
    return <div className="mt-0.5 text-2xs text-muted" title="No policy or legacy tiers on this rule">{priority} default: {defaultPolicy!.name}</div>
  }
  const text = policies
    ? ruleEscalationSummary({ policy, policyId: rule.escalation_policy_id, tiers, priority, defaultPolicy })
    : source === 'rule' ? 'Escalation policy' : `${tiers.length} legacy tier${tiers.length === 1 ? '' : 's'}`
  const tone = rule.escalation_policy_id && source !== 'rule' ? 'text-warning' : source === 'rule' ? 'text-primary' : 'text-amber-600'
  return (
    <div className={`mt-0.5 max-w-[16rem] text-2xs font-medium ${tone}`}
      title={source === 'legacy_levels' ? `The rule's own tiers take precedence over the ${priority} default policy` : undefined}>
      {text}
    </div>
  )
}

export function AlertRulesPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [deleting, setDeleting] = useState<any>(null)
  const [previewing, setPreviewing] = useState<any>(null)
  // The assets a shared rule is turned off for (per-device/server exclusions).
  const [exclusionsOf, setExclusionsOf] = useState<any>(null)
  // Per-device/server/group threshold, hold and priority changes (alert_rule_overrides).
  const [overridesOf, setOverridesOf] = useState<any>(null)
  const [tab, setTab] = useState<'rules' | 'recommended'>('rules')
  const [family, setFamily] = useState<'all' | RuleSource>('all')

  const policiesQ = usePolicies()
  const { data: rules, isError, error } = useQuery<any[]>({
    queryKey: ['alert-rules'],
    queryFn: async () => {
      const r = (await api.get('/alert-rules')).data
      return Array.isArray(r) ? r : r?.data || []
    },
  })

  // Names for the Scope column of rules that watch one server.
  const hasServerScoped = (rules || []).some((r) => isServerMetric(r.metric) && r.server_id)
  const { data: serversResp } = useQuery<any>({
    queryKey: ['servers', 'list-min'],
    queryFn: async () => (await api.get('/servers?page_size=200&sort=display_name')).data,
    enabled: hasServerScoped,
  })
  const serverNames = useMemo(
    () => new Map<string, string>((serversResp?.items || []).map((x: any) => [x.id, x.display_name || x.hostname])),
    [serversResp],
  )
  // Names for server rules scoped to a device group.
  const hasGroupScoped = (rules || []).some((r) => isServerMetric(r.metric) && r.group_id)
  const { data: groupList } = useQuery({ queryKey: ['groups-tree'], queryFn: groupsApi.list, enabled: hasGroupScoped, retry: false, staleTime: 60_000 })
  const groupNames = useMemo(() => new Map<string, string>((groupList || []).map((g) => [g.id, g.name])), [groupList])
  // Names for the agent metric gap rules scoped to a group.
  const hasGapGroup = (rules || []).some((r) => r.metric === 'agent_no_metrics' && r.group_id)
  const { data: gapGroups } = useQuery({ queryKey: ['groups-tree'], queryFn: groupsApi.list, enabled: hasGapGroup && !hasGroupScoped, retry: false, staleTime: 60_000 })
  const allGroupNames = useMemo(() => new Map<string, string>([...(gapGroups || []).map((g) => [g.id, g.name] as [string, string]), ...groupNames]), [gapGroups, groupNames])
  // Channel names for the Notifies column.
  const { data: channelsResp } = useQuery<any>({
    queryKey: ['channels', 'list-min'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    enabled: (rules || []).length > 0,
    staleTime: 60_000,
  })
  const channelNames = useMemo(() => {
    const list: any[] = Array.isArray(channelsResp) ? channelsResp : channelsResp?.data || []
    return new Map<string, string>(list.map((c) => [String(c.id), c.name]))
  }, [channelsResp])
  // Template metric names and state labels for template rules ("IPsec Tunnels: Status is Down").
  const hasTemplateRules = (rules || []).some((r) => String(r.metric || '').startsWith('tpl_'))
  const { data: templatesResp } = useQuery<any>({
    queryKey: ['snmp-profiles'],
    queryFn: async () => (await api.get('/snmp/profiles')).data,
    enabled: hasTemplateRules,
    staleTime: 60_000,
  })
  const tplIndex = useMemo(() => templateIndex(Array.isArray(templatesResp) ? templatesResp : templatesResp?.data || []), [templatesResp])
  // Event alerts (trap, syslog, Device Tracker events) close after the event expiry window.
  const { data: expiryRaw } = useQuery<any>({
    queryKey: ['alerts', 'event-expiry'],
    queryFn: async () => (await api.get('/alerts/event-expiry')).data,
    enabled: (rules || []).some((r) => ['trap', 'syslog'].includes(r.metric) || String(r.metric || '').startsWith('udt_')),
    staleTime: 60_000,
    retry: false,
  })
  const expiry = useMemo(() => {
    const p = eventExpiryFromApi(expiryRaw)
    return { enabled: p.enabled, label: expiryWindowLabel(p.hours) }
  }, [expiryRaw])
  const counts = useMemo(() => {
    const out = new Map<RuleSource, number>()
    for (const r of rules || []) out.set(ruleSource(r), (out.get(ruleSource(r)) || 0) + 1)
    return out
  }, [rules])
  const shown = useMemo(() => (rules || []).filter((r) => family === 'all' || ruleSource(r) === family), [rules, family])
  const openRule = (id: string) => {
    const r = (rules || []).find((x) => x.id === id)
    if (r) { setEditing(r); setFormOpen(true) } else toast.error('Rule not found', 'Reload the page and try again.')
  }

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/alert-rules/${id}`),
    onSuccess: () => {
      toast.success('Rule deleted')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const toggle = useMutation({
    mutationFn: async (id: string) => api.post(`/alert-rules/${id}/toggle`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alert-rules'] }),
    onError: (e: any) => toast.error('Toggle failed', apiErrorMessage(e)),
  })

  const duplicate = useMutation({
    mutationFn: async (rule: any) => {
      // Server rules are written through their own API, which takes a server scope and no device columns.
      if (isServerMetric(rule.metric)) return api.post('/host-alert-rules', serverRuleCopy(rule))
      const { id, created_at, updated_at, created_by, ...rest } = rule
      return api.post('/alert-rules', {
        ...rest,
        name: `${rule.name} (copy)`,
        enabled: false,
      })
    },
    onSuccess: () => {
      toast.success('Rule duplicated')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
    },
    onError: (e: any) => toast.error('Duplicate failed', apiErrorMessage(e)),
  })

  const simulate = useMutation({
    mutationFn: async (id: string) => (await api.post(`/alert-rules/${id}/simulate`)).data,
    onSuccess: (data: any) => {
      const details = (data.results || []).map((r: any) => `${r.channel}: ${r.status}`).join(', ')
      toast.success(data.message || 'Test notification sent', details)
    },
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Bell className="h-5 w-5 text-primary" />
            Alert Manager
          </h1>
          <p className="mt-1 max-w-2xl text-xs text-muted">
            Define trigger conditions, reset behavior, notification actions, SLA escalation
            tiers, and schedules — professional alert definitions for devices, servers,
            services, applications, and SNMP traps.
          </p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
          <Plus className="h-4 w-4" />
          Create alert
        </Button>
      </div>

      {isError && (
        <Card>
          <CardContent className="py-6">
            <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
              <div className="font-medium">Backend error loading alert rules</div>
              <div className="text-xs opacity-80">{apiErrorMessage(error as any)}</div>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex rounded-lg border border-border bg-surface2 p-0.5 text-xs" role="tablist" aria-label="Alert rules views">
          {([['rules', `Rules${rules ? ` (${rules.length})` : ''}`], ['recommended', 'Recommended']] as const).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
              className={`rounded-md px-3 py-1.5 font-medium ${tab === id ? 'bg-primary text-white' : 'text-muted hover:text-text'}`}>
              {label}
            </button>
          ))}
        </div>
        {tab === 'rules' && (
          <div className="w-60">
            <Select value={family} onValueChange={(v) => setFamily(v as 'all' | RuleSource)}>
              <SelectTrigger aria-label="Rule family"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All families</SelectItem>
                {RULE_SOURCES.map((f) => (
                  <SelectItem key={f.value} value={f.value}>{f.label} ({counts.get(f.value) || 0})</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
      </div>

      {tab === 'recommended' ? (
        <Card><CardContent className="pt-4"><RecommendedRulesPanel onOpenRule={openRule} /></CardContent></Card>
      ) : (
      <Card>
        <CardContent className="pt-4">
          <div className="overflow-x-auto rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-12">On</Th>
                  <Th>Name</Th>
                  <Th>Trigger</Th>
                  <Th>Scope</Th>
                  <Th>Severity</Th>
                  <Th>Timers and reset</Th>
                  <Th>Notifies</Th>
                  <Th>Last fired</Th>
                  <Th className="w-40 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {shown.map((r) => {
                  const trig = isNetflowMetric(r.metric) ? null : triggerSummary(r, tplIndex)
                  return (
                  <Tr key={r.id}>
                    <Td>
                      <Switch checked={r.enabled} onCheckedChange={() => toggle.mutate(r.id)} aria-label={`Rule ${r.name} enabled`} />
                    </Td>
                    <Td>
                      <div className="font-medium" title={r.created_at ? `Created ${relativeTime(r.created_at)}` : undefined}>{r.name}</div>
                      {r.description && <div className="max-w-xs truncate text-xs text-muted">{r.description}</div>}
                      {r.pack_key && <div className="text-2xs text-muted">{String(r.pack_key).startsWith('default:') ? 'Recommended default rule' : 'From a template recommendation'}</div>}
                    </Td>
                    <Td className="text-xs">
                      {isNetflowMetric(r.metric) ? (
                        <>
                          <div className="font-medium text-text">{NETFLOW_METRIC_BY_KEY[r.metric as NetflowMetric].label}</div>
                          <div className="max-w-[16rem] text-2xs text-muted">{summarizeNetflowForm(formFromRule(r))}</div>
                        </>
                      ) : (
                        // Every other family in words with its unit (alertRuleCatalog.triggerSummary), never a raw key.
                        <>
                          <div className="font-medium text-text">{trig!.title}</div>
                          <div className="max-w-[18rem] text-2xs text-muted">{trig!.detail}</div>
                        </>
                      )}
                    </Td>
                    <Td>
                      <Badge variant="outline" className="text-2xs">{scopeLabel(r, serverNames, allGroupNames, channelNames)}</Badge>
                      {(r.profile_id || r.if_role) && (
                        <div className="mt-1 text-2xs text-muted" title={r.pack_key ? 'Accepted from a template’s recommended rules' : undefined}>
                          {[r.profile_id ? 'Template devices' : null, r.if_role ? `${roleLabel(r.if_role)} interfaces` : null].filter(Boolean).join(' · ')}
                        </div>
                      )}
                      {exclusionBadge(r.exclusion_count) && (
                        <button type="button" onClick={() => setExclusionsOf(r)}
                          className="mt-1 flex items-center gap-1 text-2xs font-medium text-warning hover:underline"
                          title="Devices or servers this rule is turned off for">
                          <BellOff className="h-3 w-3" aria-hidden /> {exclusionBadge(r.exclusion_count)}
                        </button>
                      )}
                    </Td>
                    <Td>
                      <div className="flex flex-wrap items-center gap-1">
                        <Badge variant={r.severity === 'critical' ? 'danger' : r.severity === 'warning' ? 'warning' : 'info'}>
                          {r.severity}
                        </Badge>
                        <span title={r.priority ? 'Priority chosen on the rule' : 'Priority derived from severity'}>
                          <PriorityBadge priority={effectivePriority(r.priority, r.severity)} compact className={r.priority ? undefined : 'opacity-70'} />
                        </span>
                      </div>
                    </Td>
                    <Td className="text-xs text-muted">
                      <div className="text-text">{timersText(r)}</div>
                      {(() => {
                        const closes = closesText(r, expiry)
                        return <div className={closes === 'Auto + notify' ? 'text-emerald-600' : undefined}>{closes}</div>
                      })()}
                    </Td>
                    <Td><RuleNotifies rule={r} policies={policiesQ.data ?? null} channelNames={channelNames} /></Td>
                    <Td className="whitespace-nowrap text-xs text-muted" title={r.last_triggered_at ? new Date(r.last_triggered_at).toLocaleString() : 'No alert from this rule is on record'}>
                      {r.last_triggered_at ? relativeTime(r.last_triggered_at) : 'Never'}
                    </Td>
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button variant="ghost" size="icon" className="h-7 w-7" title={isServerMetric(r.metric) ? 'Preview message' : 'Preview & edit message'}
                          onClick={() => setPreviewing(r)}>
                          <Eye className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Send test notification"
                          onClick={() => {
                            if (confirm(`Send test notification for "${r.name}"?`)) simulate.mutate(r.id)
                          }}>
                          <Zap className="h-3.5 w-3.5 text-amber-500" />
                        </Button>
                        {ruleTakesOverrides(r) && (
                          <Button variant="ghost" size="icon" className="h-7 w-7" title="Overrides: other thresholds, holds or priority for some devices, servers or groups"
                            aria-label={`Overrides of ${r.name}`} onClick={() => setOverridesOf(r)}>
                            <SlidersHorizontal className="h-3.5 w-3.5" />
                          </Button>
                        )}
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate"
                          onClick={() => duplicate.mutate(r)}>
                          <Copy className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit"
                          onClick={() => { setEditing(r); setFormOpen(true) }}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" title="Delete"
                          onClick={() => setDeleting(r)}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                  )
                })}
                {(!rules || rules.length === 0) && !isError && (
                  <Tr>
                    <Td colSpan={9} className="py-12 text-center text-muted">
                      No alert rules yet. Click &quot;Create alert&quot; to open the wizard, or accept a recommended rule.
                    </Td>
                  </Tr>
                )}
                {rules && rules.length > 0 && shown.length === 0 && (
                  <Tr>
                    <Td colSpan={9} className="py-8 text-center text-muted">No rules in this family.</Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>
      )}

      <AlertRuleWizardDialog open={formOpen} onOpenChange={setFormOpen} rule={editing} />
      <RuleExclusionsDialog
        open={!!exclusionsOf}
        onOpenChange={(o) => !o && setExclusionsOf(null)}
        rule={exclusionsOf}
      />
      <RuleOverridesDialog
        open={!!overridesOf}
        onOpenChange={(o) => !o && setOverridesOf(null)}
        rule={overridesOf}
      />
      <AlertMessagePreviewDialog
        open={!!previewing}
        onOpenChange={(o) => !o && setPreviewing(null)}
        rule={previewing}
      />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete alert rule"
        description={<>Delete <span className="font-semibold text-text">{deleting?.name}</span>?</>}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}
