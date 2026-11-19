/* Per-asset exclusions from shared alert rules (migrate-187, docs/alert-rule-exclusions.md).
 *
 * A shared rule (one that applies to many devices or servers) is switched off for one asset with an
 * exclusion, never by flipping the rule's own `enabled`: that used to silence it for everyone. These
 * helpers decide what an asset page's switch means and how an exclusion is described; no React here, so
 * `scripts/test-rule-exclusions.mjs` can pin the rules. */

export type AssetKind = 'device' | 'server'

export type RuleExclusion = {
  id: string
  rule_id: string
  asset_kind: AssetKind
  asset_id: string
  asset_name: string | null
  asset_ip: string | null
  asset_path: string
  reason: string | null
  created_by: string | null
  created_by_name: string | null
  created_at: string | null
}

/** One row of GET /alert-rules/for-asset (and the server page's host rules). */
export type AssetRule = {
  id: string
  name: string
  enabled: boolean
  /** true when the rule is not pinned to this asset (it covers others too). */
  shared: boolean
  excluded: RuleExclusion | null
  /** The rule's effect on this asset: on globally and not excluded here. */
  active: boolean
  metric?: string | null
  severity?: string | null
  scope?: { kind: string; label: string } | null
}

export type RuleExclusionsResponse = { rule_id: string; kinds: AssetKind[]; items: RuleExclusion[]; hidden: number }

export type SwitchAction = 'exclude' | 'include' | 'toggle' | 'none'

export type RuleSwitch = {
  checked: boolean
  disabled: boolean
  /** What flipping the switch does. */
  action: SwitchAction
  /** Accessible label for the switch. */
  label: string
  /** Short state line under the rule name, or null when nothing needs saying. */
  note: string | null
}

const noun = (kind: AssetKind) => (kind === 'device' ? 'device' : 'server')

/**
 * What an asset page's switch shows and does for one rule.
 * - A shared rule switches on/off for this asset only (exclusion), and only with alerts.manage.
 * - A shared rule that is off globally cannot be turned on from one asset: its switch is off and locked.
 * - A rule pinned to this asset is the asset's own rule: the switch toggles the rule itself.
 */
export function ruleSwitch(rule: Pick<AssetRule, 'name' | 'enabled' | 'shared' | 'excluded'>, kind: AssetKind,
  canManage: boolean): RuleSwitch {
  const n = noun(kind)
  if (!rule.shared) {
    return {
      checked: rule.enabled, disabled: !canManage, action: canManage ? 'toggle' : 'none',
      label: `${rule.enabled ? 'Disable' : 'Enable'} ${rule.name}`, note: null,
    }
  }
  if (!rule.enabled) {
    return {
      checked: false, disabled: true, action: 'none', label: `${rule.name} is off for every ${n}`,
      note: `Off for every ${n} — turn it on in Alert Rules`,
    }
  }
  if (rule.excluded) {
    return {
      checked: false, disabled: !canManage, action: canManage ? 'include' : 'none',
      label: `Enable ${rule.name} for this ${n}`, note: exclusionNote(rule.excluded, kind),
    }
  }
  return {
    checked: true, disabled: !canManage, action: canManage ? 'exclude' : 'none',
    label: `Disable ${rule.name} for this ${n} only`, note: null,
  }
}

/** "Disabled for this server by Sara · 12 Nov 2026, 14:05" (+ the reason when given). */
export function exclusionNote(ex: Pick<RuleExclusion, 'created_by_name' | 'created_at' | 'reason'>, kind: AssetKind): string {
  const who = ex.created_by_name ? ` by ${ex.created_by_name}` : ''
  const when = ex.created_at ? ` · ${formatWhen(ex.created_at)}` : ''
  const why = ex.reason ? ` — ${ex.reason}` : ''
  return `Disabled for this ${noun(kind)}${who}${when}${why}`
}

/** The asset page an exclusion links to. */
export function exclusionHref(ex: Pick<RuleExclusion, 'asset_kind' | 'asset_id'>): string {
  return ex.asset_kind === 'device' ? `/devices/${ex.asset_id}` : `/servers/${ex.asset_id}`
}

/** The asset's display name, falling back to its address or its kind. */
export function exclusionAssetLabel(ex: Pick<RuleExclusion, 'asset_kind' | 'asset_name' | 'asset_ip'>): string {
  return ex.asset_name || ex.asset_ip || (ex.asset_kind === 'device' ? 'Device' : 'Server')
}

/** The Alert Rules list badge: "Off for 2" / null when the rule runs everywhere it applies. */
export function exclusionBadge(count: number | null | undefined): string | null {
  const n = Number(count) || 0
  return n > 0 ? `Off for ${n}` : null
}

/** Confirmation text before excluding (the exclusion resolves the rule's open incidents here). */
export function excludeConsequence(kind: AssetKind): string {
  const n = noun(kind)
  return `The rule keeps running for every other ${n}. Open alerts from this rule on this ${n} are resolved `
    + `without a recovery notice, and nothing new is raised here until you turn it back on.`
}

export function formatWhen(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** API paths, in one place. */
export const exclusionApi = {
  list: (ruleId: string) => `/alert-rules/${ruleId}/exclusions`,
  create: (ruleId: string) => `/alert-rules/${ruleId}/exclusions`,
  remove: (ruleId: string, kind: AssetKind, assetId: string) => `/alert-rules/${ruleId}/exclusions/${kind}/${assetId}`,
  forAsset: '/alert-rules/for-asset',
}
