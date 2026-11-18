import { api } from '@/lib/api'

export type MetricThresholds = { warn?: number | null; crit?: number | null; op?: string }
export type TemplateMetric = {
  key: string
  name: string
  oid: string
  type?: string
  unit?: string | null
  scale?: number | null
  value_map?: Record<string, number> | null
  labels?: Record<string, { text: string; sev: string }> | null
  thresholds?: MetricThresholds | null
  format?: string | null
}
export type TemplateGroup = {
  key: string
  name: string
  kind?: 'scalar' | 'table'
  interval_seconds?: number
  description?: string | null
  table?: { label_oid?: string | null; max_rows?: number | null } | null
  metrics: TemplateMetric[]
  children?: Record<string, unknown> | null
  display?: string | null
  belongs_to?: Record<string, unknown> | null
  tab?: string | null
  applies_when?: Record<string, unknown> | null
}
export type MatchRules = {
  sys_object_id_prefixes?: string[]
  sys_descr_regex?: string | null
  require_sys_descr?: boolean
  default_vendor?: string | null
  default_model?: string | null
  extract_vendor?: string | null
  extract_model?: string | null
  extract_os_version?: string | null
}
/** One ordered interface role rule (server/app/services/interface_roles.py): the first match gives the role. */
export type InterfaceRoleRule = { role: string; field: string; pattern: string }

export type TemplateContent = {
  name: string
  vendor: string | null
  description: string | null
  device_type: string | null
  icon: string | null
  match_priority: number
  match_rules: MatchRules
  oid_groups: TemplateGroup[]
  /** null = none (on a customised template: the parent's). */
  interface_roles?: InterfaceRoleRule[] | null
  /** Recommended alert rules; kept as received (no editor yet). null = none / the parent's. */
  rule_pack?: Record<string, unknown>[] | null
}
export type Template = TemplateContent & {
  id: string
  version: number
  builtin: boolean
  builtin_key: string | null
  parent_id: string | null
  parent_name: string | null
  revision: number
  has_draft: boolean
  draft_updated_at: string | null
  pilot_device_count: number
  pilot_started_at: string | null
  device_count: number
  suggestion_count: number
  customized_count: number
  created_at: string
  updated_at: string
}
export type TemplateDetail = Template & {
  overlay: Record<string, unknown> | null
  draft: TemplateContent | null
  pilot_device_ids: string[]
  editable: boolean
  overlay_warnings?: string[]
  parent_groups?: TemplateGroup[]
  parent_revision?: number
  customized_by: { id: string; name: string }[]
}
export type Impact = {
  groups_added: string[]
  groups_removed: string[]
  groups_changed: string[]
  metrics_added: string[]
  metrics_removed: string[]
  metrics_changed: string[]
  oids_before: number
  oids_after: number
  devices: number
  customized_templates: { name: string; devices: number }[]
  alert_rules_on_removed_metrics: { id: string; name: string; metric: string; enabled: boolean }[]
  match_rules_changed: boolean
  device_type_changed: boolean
  warnings: string[]
  needs_confirmation: boolean
}
export type TemplateDevice = {
  id: string
  hostname: string
  ip_address: string | null
  status: string
  device_type: string | null
  vendor: string | null
  model: string | null
  profile_source: string | null
  profile_assigned_at: string | null
  suggested_id: string | null
  suggested_name: string | null
  in_pilot: boolean
  answered: number
  defined: number
  coverage_pct: number | null
  missing: string[]
}
export type Version = {
  revision: number
  action: string
  reason: string | null
  changed_by_name: string | null
  changed_at: string
  groups: number
}
export type TestResult = {
  device: { id: string; hostname: string; ip: string }
  duration_ms: number
  answered: number
  defined: number
  coverage_pct: number | null
  source: string
  groups: {
    key: string
    name: string
    kind: string
    error: string | null
    rows: number | null
    truncated: boolean
    duration_ms: number
    metrics: {
      key: string
      name: string
      oid: string
      ok: boolean
      value?: unknown
      raw?: string | null
      label?: string | null
      unit?: string | null
      rows?: number
      sample?: { index: string; label: string | null; value: unknown }[]
      error: string | null
    }[]
  }[]
}

export const P = '/snmp/profiles'

/** The editable document. `fallback` supplies interface roles and rule pack for a draft saved before they existed. */
export function contentOf(t: TemplateContent, fallback?: TemplateContent): TemplateContent {
  const pick = <K extends 'interface_roles' | 'rule_pack'>(k: K): TemplateContent[K] =>
    (k in t ? t[k] : fallback?.[k]) ?? null
  return {
    name: t.name,
    vendor: t.vendor ?? null,
    description: t.description ?? null,
    device_type: t.device_type ?? null,
    icon: t.icon ?? null,
    match_priority: t.match_priority ?? 100,
    match_rules: { ...(t.match_rules || {}) },
    oid_groups: JSON.parse(JSON.stringify(t.oid_groups || [])),
    interface_roles: JSON.parse(JSON.stringify(pick('interface_roles'))),
    rule_pack: JSON.parse(JSON.stringify(pick('rule_pack'))),
  }
}

export type RecommendationChange = { field: string; from: unknown; to: unknown }
export type Recommendation = {
  key: string
  name: string
  description: string | null
  metric: string
  operator: string
  threshold: number
  reset_threshold: number | null
  min_duration: number
  clear_duration: number | null
  severity: 'info' | 'warning' | 'critical'
  priority: string | null
  if_role: string | null
  source: 'template' | 'threshold'
  status: 'available' | 'accepted' | 'changed'
  changes: RecommendationChange[]
  accepted: null | {
    rule_id: string
    name: string
    enabled: boolean
    on_this_template: boolean
    template_name: string | null
    rule_values: Record<string, unknown>
  }
}

/** Error text for template endpoints, which return {message, errors[]} on validation failures. */
export function templateError(e: any): string {
  const d = e?.response?.data?.detail
  if (d && typeof d === 'object' && !Array.isArray(d) && Array.isArray(d.errors)) {
    return `${d.message}: ${d.errors.join('; ')}`
  }
  if (typeof d === 'string') return d
  if (Array.isArray(d)) return d.map((x: any) => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('; ')
  return e?.message || 'Request failed'
}

export const templatesApi = {
  list: async (): Promise<Template[]> => (await api.get(P)).data,
  get: async (id: string): Promise<TemplateDetail> => (await api.get(`${P}/${id}`)).data,
  devices: async (id: string) => (await api.get(`${P}/${id}/devices`)).data as {
    data: TemplateDevice[]; defined: number; average_pct: number | null
  },
  versions: async (id: string): Promise<Version[]> => (await api.get(`${P}/${id}/versions`)).data.data,
  version: async (id: string, rev: number) => (await api.get(`${P}/${id}/versions/${rev}`)).data,
  unmatched: async () => (await api.get(`${P}/unmatched`)).data.data as any[],
  suggestions: async () => (await api.get(`${P}/suggestions`)).data.data as any[],
  recommended: async (id: string): Promise<Recommendation[]> => (await api.get(`${P}/${id}/recommended`)).data.items,
}

/** Path of one recommendation action; keys may contain ':' ("fgt_cpu:crit"). */
export function recommendationPath(id: string, key: string, action: 'accept' | 'apply' | 'keep'): string {
  return `${P}/${id}/recommended/${encodeURIComponent(key)}/${action}`
}
