/**
 * Reports contract — mirrors server/app/api/v1/reports.py (catalog, render,
 * preview, organisation templates) and report_schedules.py.
 */
import type { Branding } from '@/lib/branding'

/* ------------------------------------------------------------------ */
/*  Catalog: GET /reports/catalog                                       */
/* ------------------------------------------------------------------ */

export type PeriodId =
  | 'last_24h' | 'last_7d' | 'last_30d' | 'last_90d'
  | 'today' | 'yesterday' | 'week_to_date' | 'previous_week' | 'previous_work_week'
  | 'month_to_date' | 'previous_month' | 'quarter_to_date' | 'previous_quarter'

export interface ReportCategory {
  id: string
  title: string
  icon: string
  description: string
}

export type OptionType = 'int' | 'number' | 'select' | 'bool'

export interface SectionOption {
  key: string
  label: string
  type: OptionType
  default: unknown
  min?: number
  max?: number
  choices?: [string, string][]
  help?: string
}

export type SectionScope = 'device' | 'netflow' | 'fleet'

/** A section in the library (GET /reports/catalog → sections). */
export interface SectionDef {
  id: string
  title: string
  description: string
  category: string
  scope: SectionScope
  requires: string[]
  options: SectionOption[]
  semantics: 'window' | 'snapshot'
  available: boolean
}

/** One section placed in a template, with its options. */
export interface TemplateEntry {
  id: string
  title?: string | null
  options?: Record<string, unknown>
  /** Catalog only: whether the caller may see this section. */
  available?: boolean
}

export type ScopeFilter = 'devices' | 'applications' | 'netflow'

/** A built-in template (or a legacy report type) in the catalog. */
export interface CatalogTemplate {
  key: string
  title: string
  description: string
  category: string
  category_title?: string
  engine: 'legacy' | 'sections'
  formats: string[]
  sections?: string[]
  entries?: TemplateEntry[]
  audience?: string[]
  tags?: string[]
  default_period?: PeriodId
  frequency?: 'daily' | 'weekly' | 'monthly'
  icon?: string
  featured?: boolean
  filterable?: ScopeFilter[]
  requires?: string[]
  available?: boolean
}

/** An organisation template (custom_reports). */
export interface CustomTemplate {
  id: string
  name: string
  description: string
  sections: string[]
  entries: TemplateEntry[]
  category: string | null
  tags: string[]
  visibility: 'private' | 'shared'
  source_key: string | null
  default_period: PeriodId | null
  owner_id: string | null
  owner_name: string | null
  is_owner: boolean
  can_edit: boolean
  can_delete: boolean
  can_change_visibility: boolean
  schedules: number
  created_at: string | null
  updated_at: string | null
}

export interface PeriodDef {
  id: PeriodId
  label: string
  kind: 'rolling' | 'calendar'
}

export interface ReportCatalog {
  categories: ReportCategory[]
  types: CatalogTemplate[]
  sections: SectionDef[]
  custom: CustomTemplate[]
  periods: PeriodDef[]
  can: { manage: boolean; export: boolean; share: boolean }
}

/** Body for POST /reports/custom and PUT /reports/custom/{id}. */
export interface TemplateBody {
  name: string
  description?: string | null
  sections: TemplateEntry[]
  category?: string | null
  tags?: string[]
  visibility: 'private' | 'shared'
  source_key?: string | null
  default_period?: PeriodId | null
}

/* ------------------------------------------------------------------ */
/*  Render: GET /reports/render/{key}?format=json, POST /reports/preview */
/* ------------------------------------------------------------------ */

export type KpiAccent = 'primary' | 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export interface RenderKpi {
  label: string
  value: string
  accent?: KpiAccent
  subtitle?: string
}

export interface SeriesPoint { t?: string; v?: number | null; label?: string; value?: number }

export interface ChartSeries {
  kind: 'area' | 'bars' | 'donut' | 'rank' | 'line' | 'stacked'
  unit?: string
  color?: string
  y_domain?: [number, number]
  points?: SeriesPoint[]
  /** Multi-series charts: [{name, points}] */
  series?: { name: string; color?: string; points: SeriesPoint[] }[]
}

export interface RenderChart {
  title: string
  data_uri?: string
  series?: ChartSeries
}

export type ColStyle = 'text' | 'num' | 'mono' | 'pct' | 'pct-bar' | 'status' | 'severity'

export interface RenderTable {
  title?: string
  headers: string[]
  styles?: ColStyle[]
  rows: (string | number | null)[][]
}

export interface RenderSection {
  id: string
  section_id?: string
  title: string
  description?: string
  category?: string
  half?: boolean
  kpis: RenderKpi[]
  charts: RenderChart[]
  tables: RenderTable[]
  notes: string[]
  options?: Record<string, unknown>
  /** The viewer's role may not see this section's data. */
  restricted?: boolean
  /** The section failed to generate (the rest of the report is intact). */
  error?: boolean
}

export interface RenderReport {
  key: string
  title: string
  description: string
  category?: string
  from: string
  to: string
  period_label: string
  generated_label: string
  company_name: string
  timezone?: string
  tz_label?: string
  scope_label?: string
  scoped?: boolean
  omitted_sections?: number
  restricted_sections?: number
  filters?: { netflow?: Record<string, unknown>; application_id?: string }
  branding?: Branding
  sections: RenderSection[]
}

/* ------------------------------------------------------------------ */
/*  Schedules: /report-schedules                                       */
/* ------------------------------------------------------------------ */

export type ScheduleFormat = 'pdf' | 'excel' | 'csv' | 'html' | 'none'

export interface ReportRun {
  id: string
  title: string
  report_type: string
  period: string
  token: string | null
  token_withheld?: boolean
  status: string
  delivered_to: string[]
  generated_at: string | null
  schedule_name?: string | null
}
