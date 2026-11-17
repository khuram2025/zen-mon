// "Automation" on a device or server page: what discovery rules set on this asset, hand edits they kept, the rules
// that match it now and what a run would still change. GET /discovery-v2/rules/assets/{kind}/{id}; a role that cannot
// read it (403) or an asset out of scope (404) gets no card at all, so the page asks with useAssetAutomation first.
// A controller-managed FortiSwitch / FortiAP (device page, poll_mode via_controller) is asked for as kind `child`.
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Wand2 } from 'lucide-react'
import { useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { relativeTime } from '@/lib/utils'
import { rulesApi } from './api'
import type { AssetAutomation, AssetKind } from './types'
import { dependencyIds, formatSettingValue, plural, settingLabel, settingValueLink } from './ruleModel'
import { useRuleCatalog, useRuleLookups, useWithDeviceNames } from './ruleUi'

const REF_KEYS = new Set(['group', 'template', 'snmp_credential', 'map'])

export function useAssetAutomation(kind: AssetKind, assetId: string | undefined) {
  return useQuery<AssetAutomation>({
    queryKey: ['discovery', 'rules', 'asset', kind, assetId],
    queryFn: () => rulesApi.asset(kind, assetId!),
    enabled: !!assetId,
    retry: false,
    staleTime: 30_000,
  })
}

/** The card body; the page wraps it in its own panel. */
export function AssetAutomationBody({ data }: { data: AssetAutomation }) {
  const can = useCan()
  const canManage = can('discovery.manage_rules')
  const { data: catalog } = useRuleCatalog(can('discovery.view'))
  // Names for group / template / credential ids, only when a setting needs one.
  const { lookups: baseLookups } = useRuleLookups(data.settings.some((s) => REF_KEYS.has(s.action_key)))
  const lookups = useWithDeviceNames(baseLookups, useMemo(() => dependencyIds(data.settings), [data.settings]))
  const kind = data.asset_kind
  const noun = kind === 'server' ? 'server' : 'device'
  const pending = data.pending.filter((c) => c.outcome === 'apply').length
  const reverts = data.pending.filter((c) => c.outcome === 'revert').length
  const runLink = `/discovery/rules/run?${kind}=${data.asset_id}`
  const empty = data.settings.length === 0 && data.matching_rules.length === 0

  return (
    <div className="space-y-3 text-sm">
      {!data.engine_enabled && (
        <div className="text-xs text-muted">Discovery rules are off: new {noun}s and scans are not evaluated.</div>
      )}
      {empty ? (
        <div className="text-sm text-muted">No discovery rule has set anything on this {noun}, and no active rule matches it.</div>
      ) : (
        <>
          {data.settings.length > 0 && (
            <div>
              <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Set by rules</div>
              <ul className="divide-y divide-border/60">
                {data.settings.map((s) => {
                  const shown = s.action_key === 'tags' || s.action_key === 'map' || s.action_key === 'dependency_parent' ? s.value ?? s.item_key : s.value
                  const href = settingValueLink(s.action_key, shown)
                  return (
                  <li key={`${s.action_key}:${s.item_key ?? ''}`} className="flex flex-wrap items-baseline gap-x-2 py-1.5">
                    <span className="text-xs text-muted">{settingLabel(s.action_key, catalog)}</span>
                    {href ? (
                      <Link to={href} className="font-medium text-primary hover:underline">
                        {formatSettingValue(s.action_key, shown, lookups)}
                      </Link>
                    ) : (
                      <span className="font-medium">{formatSettingValue(s.action_key, shown, lookups)}</span>
                    )}
                    {s.state === 'manual_edit' && (
                      <Badge variant="info" title={`Now: ${formatSettingValue(s.action_key, s.current, lookups)}`}>
                        edited by hand — kept
                      </Badge>
                    )}
                    <span className="ml-auto text-2xs text-muted">
                      {s.rule_id ? (
                        <Link to={`/discovery/rules/${s.rule_id}`} className="text-primary hover:underline">
                          {s.rule_name || 'rule'}
                        </Link>
                      ) : (
                        s.rule_name || 'a deleted rule'
                      )}
                      {s.applied_at && ` · ${relativeTime(s.applied_at)}`}
                    </span>
                  </li>
                  )
                })}
              </ul>
            </div>
          )}
          {data.matching_rules.length > 0 && (
            <div>
              <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Active rules that match now</div>
              <div className="flex flex-wrap gap-1">
                {data.matching_rules.map((r) => (
                  <Link key={r.rule_id} to={`/discovery/rules/${r.rule_id}`}>
                    <Badge variant="outline" className="hover:border-primary/40 hover:text-text">
                      {r.rule_name || r.rule_id.slice(0, 8)}
                    </Badge>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </>
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className={pending || reverts ? 'text-warning-fg' : 'text-muted'}>
          {pending || reverts
            ? `${[pending ? plural(pending, 'change') : '', reverts ? `${plural(reverts, 'revert')}` : ''].filter(Boolean).join(' and ')} pending: a run would still change this ${noun}`
            : 'Nothing pending'}
        </span>
        {canManage && (
          <Link to={runLink} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
            <Wand2 className="h-3.5 w-3.5" /> Run rules on this {noun}
          </Link>
        )}
      </div>
    </div>
  )
}
