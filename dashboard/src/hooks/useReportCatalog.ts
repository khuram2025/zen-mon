import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

/* ------------------------------------------------------------------ */
/*  Types — GET /reports/catalog                                       */
/* ------------------------------------------------------------------ */

export interface CatalogType {
  key: string
  title: string
  description: string
  category: string
  engine: 'legacy' | 'sections'
  formats: string[]
  sections?: string[]
}

export interface CatalogSection {
  id: string
  title: string
  description: string
  category: string
}

export interface CustomReport {
  id: string
  name: string
  description?: string | null
  sections: string[]
  created_at?: string
}

export interface ReportCatalog {
  types: CatalogType[]
  sections: CatalogSection[]
  custom: CustomReport[]
}

export function useReportCatalog(opts?: { enabled?: boolean }) {
  return useQuery<ReportCatalog>({
    queryKey: ['reports', 'catalog'],
    queryFn: async () => (await api.get('/reports/catalog')).data,
    enabled: opts?.enabled ?? true,
  })
}

/* ------------------------------------------------------------------ */
/*  Report-type metadata shared by the library, schedules & the form   */
/* ------------------------------------------------------------------ */

export const LEGACY_REPORT_TYPES = [
  { id: 'executive_summary', label: 'Executive Summary' },
  { id: 'device_health', label: 'Device Health' },
  { id: 'service_health', label: 'Service Health' },
  { id: 'alert_analysis', label: 'Alert Analysis' },
  { id: 'full_report', label: 'Full Comprehensive Report' },
] as const

export const SECTION_REPORT_TYPES = [
  { id: 'availability', label: 'Availability & SLA' },
  { id: 'performance', label: 'Network Performance' },
  { id: 'traffic', label: 'Traffic & NetFlow' },
  { id: 'alerts', label: 'Alerts & Incidents' },
  { id: 'capacity', label: 'Capacity Planning' },
  { id: 'apm_performance', label: 'Application Performance' },
  { id: 'usage', label: 'Application Usage' },
  { id: 'inventory', label: 'Asset Inventory' },
] as const

export const REPORT_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  [...LEGACY_REPORT_TYPES, ...SECTION_REPORT_TYPES].map((t) => [t.id, t.label]),
)

/** report_type keys rendered by the sections engine (PDF/none delivery only). */
export const SECTION_ENGINE_KEYS = new Set<string>([
  ...SECTION_REPORT_TYPES.map((t) => t.id),
  'custom',
])
