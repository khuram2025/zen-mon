import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  FileText,
  BarChart3,
  Wrench,
  Briefcase,
  Boxes,
} from 'lucide-react'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { ExportMenu } from '@/components/reports/ExportMenu'
import { cn } from '@/lib/utils'

const TABS = [
  {
    to: 'executive',
    label: 'Executive',
    description: 'For leadership',
    icon: BarChart3,
    accent: 'text-indigo-400',
    reportType: 'executive_summary' as const,
  },
  {
    to: 'technical',
    label: 'Technical',
    description: 'For engineers',
    icon: Wrench,
    accent: 'text-emerald-400',
    reportType: 'device_health' as const,
  },
  {
    to: 'business',
    label: 'Business',
    description: 'For service owners',
    icon: Briefcase,
    accent: 'text-amber-400',
    reportType: 'service_health' as const,
  },
  {
    to: 'inventory',
    label: 'Inventory',
    description: 'Asset overview',
    icon: Boxes,
    accent: 'text-sky-400',
    reportType: 'full_report' as const,
  },
]

export function ReportsPage() {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const { pathname } = useLocation()
  const activeSlug = pathname.split('/').filter(Boolean)[1] || 'executive'
  const activeTab = TABS.find((t) => t.to === activeSlug) ?? TABS[0]
  const showTimeRange = activeSlug !== 'inventory'

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileText className="h-6 w-6 text-primary" />
            Reports
          </h1>
          <p className="mt-1 text-sm text-muted">
            Live dashboards for engineers, service owners, IT, and leadership — with PDF / Excel / CSV export.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {showTimeRange && (
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
            />
          )}
          <ExportMenu
            reportType={activeTab.reportType}
            fromISO={range.fromISO}
            toISO={range.toISO}
            label={`Export ${activeTab.label}`}
          />
        </div>
      </div>

      {/* Persona tab strip */}
      <nav className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            className={({ isActive }) =>
              cn(
                'group relative overflow-hidden rounded-lg border p-3 transition-all',
                'hover:border-primary/40 hover:bg-surface',
                isActive
                  ? 'border-primary/60 bg-surface shadow-card dark:shadow-card-dark'
                  : 'border-border bg-surface/60',
              )
            }
          >
            {({ isActive }) => (
              <div className="flex items-start gap-3">
                <div
                  className={cn(
                    'flex h-9 w-9 shrink-0 items-center justify-center rounded-md',
                    isActive ? 'bg-primary/15' : 'bg-surface2',
                    tab.accent,
                  )}
                >
                  <tab.icon className="h-[18px] w-[18px]" />
                </div>
                <div className="min-w-0">
                  <p className={cn('text-sm font-semibold tracking-tight', isActive ? 'text-text' : 'text-text2')}>
                    {tab.label}
                  </p>
                  <p className="mt-0.5 text-[11px] text-muted">{tab.description}</p>
                </div>
                {isActive && (
                  <span className="absolute bottom-0 left-0 h-[2px] w-full bg-primary" />
                )}
              </div>
            )}
          </NavLink>
        ))}
      </nav>

      <Outlet />
    </div>
  )
}
