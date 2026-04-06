import { useState } from 'react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import {
  FileText,
  BarChart3,
  Monitor,
  ShieldCheck,
  Bell,
  Check,
  Calendar,
  Download,
  Loader2,
  AlertCircle,
  X,
  Filter,
  ChevronDown,
  ChevronUp,
  FileSpreadsheet,
  Table2,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useDeviceGroups } from '@/hooks/useDevices'

/* ------------------------------------------------------------------ */
/*  Types & Constants                                                  */
/* ------------------------------------------------------------------ */

interface ReportType {
  key: string
  label: string
  shortDesc: string
  icon: React.ElementType
  accent: string
  accentBg: string
}

const REPORT_TYPES: ReportType[] = [
  {
    key: 'executive_summary',
    label: 'Executive Summary',
    shortDesc: 'KPIs, health scores & charts for stakeholders',
    icon: BarChart3,
    accent: 'text-indigo-400',
    accentBg: 'bg-indigo-500/15',
  },
  {
    key: 'device_health',
    label: 'Device Health',
    shortDesc: 'Availability, RTT analysis & status history',
    icon: Monitor,
    accent: 'text-emerald-400',
    accentBg: 'bg-emerald-500/15',
  },
  {
    key: 'service_health',
    label: 'Service Health',
    shortDesc: 'SLA compliance, latency & error rates',
    icon: ShieldCheck,
    accent: 'text-amber-400',
    accentBg: 'bg-amber-500/15',
  },
  {
    key: 'alert_analysis',
    label: 'Alert Analysis',
    shortDesc: 'Alert volume, MTTR & incident patterns',
    icon: Bell,
    accent: 'text-rose-400',
    accentBg: 'bg-rose-500/15',
  },
  {
    key: 'full_report',
    label: 'Full Report',
    shortDesc: 'Comprehensive all-in-one document',
    icon: FileText,
    accent: 'text-violet-400',
    accentBg: 'bg-violet-500/15',
  },
]

type PeriodKey = '24h' | '7d' | '30d' | 'custom'

const PERIODS: { key: PeriodKey; label: string }[] = [
  { key: '24h', label: '24h' },
  { key: '7d', label: '7 Days' },
  { key: '30d', label: '30 Days' },
  { key: 'custom', label: 'Custom' },
]

type ExportFormat = 'pdf' | 'excel' | 'csv'

interface FormatOption {
  key: ExportFormat
  label: string
  icon: React.ElementType
  desc: string
}

const FORMATS: FormatOption[] = [
  { key: 'pdf', label: 'PDF', icon: FileText, desc: 'Professional document with charts' },
  { key: 'excel', label: 'Excel', icon: FileSpreadsheet, desc: 'Multi-sheet workbook (.xlsx)' },
  { key: 'csv', label: 'CSV', icon: Table2, desc: 'Flat data for analysis' },
]

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export function ReportsPage() {
  const [selectedType, setSelectedType] = useState<string | null>(null)
  const [period, setPeriod] = useState<PeriodKey>('7d')
  const [fromTime, setFromTime] = useState('')
  const [toTime, setToTime] = useState('')
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [selectedGroups, setSelectedGroups] = useState<string[]>([])
  const [format, setFormat] = useState<ExportFormat>('pdf')
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  const { data: deviceGroups = [] } = useDeviceGroups()

  useQuery({
    queryKey: ['report-types'],
    queryFn: () => api.get('/api/v1/reports/types'),
  })

  const activeReport = REPORT_TYPES.find((r) => r.key === selectedType)

  function toggleGroup(id: string) {
    setSelectedGroups((prev) =>
      prev.includes(id) ? prev.filter((g) => g !== id) : [...prev, id],
    )
  }

  async function handleGenerate() {
    if (!selectedType) return
    setGenerating(true)
    setError(null)
    setSuccess(false)

    try {
      const payload: Record<string, unknown> = {
        report_type: selectedType,
        period,
        format,
      }
      if (period === 'custom') {
        if (!fromTime || !toTime) {
          setError('Please select both start and end dates.')
          setGenerating(false)
          return
        }
        payload.from_time = fromTime
        payload.to_time = toTime
      }
      if (selectedGroups.length > 0) {
        payload.group_ids = selectedGroups
      }

      const token = localStorage.getItem('token')
      const response = await fetch('/api/v1/reports/generate', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      })

      if (!response.ok) {
        const text = await response.text().catch(() => '')
        throw new Error(text || `Server responded with ${response.status}`)
      }

      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url

      const ext = format === 'excel' ? 'xlsx' : format
      anchor.download = `${selectedType}_report_${new Date().toISOString().slice(0, 10)}.${ext}`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)

      setSuccess(true)
      setTimeout(() => setSuccess(false), 4000)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Report generation failed.'
      setError(message)
    } finally {
      setGenerating(false)
    }
  }

  const activeFormat = FORMATS.find((f) => f.key === format)!

  /* ------------------------------------------------------------------ */
  /*  Render                                                             */
  /* ------------------------------------------------------------------ */

  return (
    <div className="min-h-screen" style={{ background: 'var(--bg-primary, #0F1117)' }}>
      <div className="mx-auto max-w-5xl px-5 py-8">

        {/* ---- Header ------------------------------------------------- */}
        <div className="mb-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div
              className="flex h-9 w-9 items-center justify-center rounded-lg"
              style={{ background: 'var(--bg-tertiary, #242832)' }}
            >
              <FileText className="h-4.5 w-4.5" style={{ color: 'var(--accent, #6366F1)' }} />
            </div>
            <div>
              <h1
                className="text-xl font-semibold tracking-tight"
                style={{ color: 'var(--text-primary, #E8EAED)' }}
              >
                Reports
              </h1>
              <p
                className="text-xs"
                style={{ color: 'var(--text-muted, #5F6578)' }}
              >
                Generate & export infrastructure reports
              </p>
            </div>
          </div>
        </div>

        {/* ---- Main Grid: 2-column layout ----------------------------- */}
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_340px]">

          {/* ---- Left: Report Type Selection + Config ------------------- */}
          <div className="space-y-5">

            {/* Report Type Cards - compact horizontal list */}
            <section>
              <h2
                className="mb-3 text-[11px] font-semibold uppercase tracking-widest"
                style={{ color: 'var(--text-muted, #5F6578)' }}
              >
                Report Type
              </h2>
              <div className="space-y-2">
                {REPORT_TYPES.map((rt) => {
                  const Icon = rt.icon
                  const isSelected = selectedType === rt.key
                  return (
                    <button
                      key={rt.key}
                      onClick={() => {
                        setSelectedType(rt.key)
                        setError(null)
                        setSuccess(false)
                      }}
                      className={cn(
                        'group flex w-full items-center gap-3.5 rounded-xl border px-4 py-3 text-left transition-all duration-150',
                        isSelected
                          ? 'border-[var(--accent,#6366F1)]/40 shadow-[0_0_0_1px_rgba(99,102,241,0.3)]'
                          : 'border-[var(--bg-tertiary,#242832)] hover:border-[var(--bg-elevated,#2D3140)]',
                      )}
                      style={{
                        background: isSelected
                          ? 'var(--bg-tertiary, #242832)'
                          : 'var(--bg-secondary, #1A1D27)',
                      }}
                    >
                      <div
                        className={cn(
                          'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg transition-colors',
                          rt.accentBg,
                        )}
                      >
                        <Icon className={cn('h-4 w-4', rt.accent)} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div
                          className="text-[13px] font-semibold"
                          style={{ color: 'var(--text-primary, #E8EAED)' }}
                        >
                          {rt.label}
                        </div>
                        <div
                          className="truncate text-[11px]"
                          style={{ color: 'var(--text-secondary, #9BA1B0)' }}
                        >
                          {rt.shortDesc}
                        </div>
                      </div>
                      {isSelected && (
                        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[var(--accent,#6366F1)]">
                          <Check className="h-3 w-3 text-white" strokeWidth={3} />
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </section>

            {/* Filters - collapsible */}
            <section
              className="rounded-xl border"
              style={{
                background: 'var(--bg-secondary, #1A1D27)',
                borderColor: 'var(--bg-tertiary, #242832)',
              }}
            >
              <button
                onClick={() => setFiltersOpen((o) => !o)}
                className="flex w-full items-center gap-2.5 px-4 py-3 text-sm font-medium transition-colors hover:text-[var(--text-primary,#E8EAED)]"
                style={{ color: 'var(--text-secondary, #9BA1B0)' }}
              >
                <Filter className="h-3.5 w-3.5" />
                <span>Device Group Filters</span>
                {selectedGroups.length > 0 && (
                  <span
                    className="inline-flex h-4.5 min-w-[18px] items-center justify-center rounded-full px-1.5 text-[10px] font-bold text-white"
                    style={{ background: 'var(--accent, #6366F1)' }}
                  >
                    {selectedGroups.length}
                  </span>
                )}
                <span className="ml-auto">
                  {filtersOpen ? (
                    <ChevronUp className="h-3.5 w-3.5" />
                  ) : (
                    <ChevronDown className="h-3.5 w-3.5" />
                  )}
                </span>
              </button>

              {filtersOpen && (
                <div
                  className="border-t px-4 pb-4 pt-3"
                  style={{ borderColor: 'var(--bg-tertiary, #242832)' }}
                >
                  {deviceGroups.length === 0 ? (
                    <p
                      className="text-xs italic"
                      style={{ color: 'var(--text-muted, #5F6578)' }}
                    >
                      No device groups available. All devices will be included.
                    </p>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      {deviceGroups.map((group: { id: string; name: string }) => {
                        const checked = selectedGroups.includes(group.id)
                        return (
                          <label
                            key={group.id}
                            className={cn(
                              'flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium transition-all',
                              checked
                                ? 'border-[var(--accent,#6366F1)]/40 bg-[var(--accent,#6366F1)]/10'
                                : 'border-[var(--bg-elevated,#2D3140)] hover:border-[var(--bg-elevated,#2D3140)] hover:bg-[var(--bg-tertiary,#242832)]',
                            )}
                            style={{ color: checked ? 'var(--accent, #6366F1)' : 'var(--text-secondary, #9BA1B0)' }}
                          >
                            <span
                              className={cn(
                                'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border transition-colors',
                                checked
                                  ? 'border-transparent bg-[var(--accent,#6366F1)]'
                                  : 'border-[var(--bg-elevated,#2D3140)]',
                              )}
                            >
                              {checked && <Check className="h-2.5 w-2.5 text-white" strokeWidth={3} />}
                            </span>
                            <input
                              type="checkbox"
                              className="sr-only"
                              checked={checked}
                              onChange={() => toggleGroup(group.id)}
                            />
                            {group.name}
                          </label>
                        )
                      })}
                    </div>
                  )}
                  {selectedGroups.length > 0 && (
                    <button
                      onClick={() => setSelectedGroups([])}
                      className="mt-2 text-[11px] font-medium text-[var(--text-muted,#5F6578)] transition-colors hover:text-[var(--text-primary,#E8EAED)]"
                    >
                      Clear all
                    </button>
                  )}
                </div>
              )}
            </section>
          </div>

          {/* ---- Right: Configuration Panel ------------------------------ */}
          <div className="space-y-4">

            {/* Period Selection */}
            <section
              className="rounded-xl border p-4"
              style={{
                background: 'var(--bg-secondary, #1A1D27)',
                borderColor: 'var(--bg-tertiary, #242832)',
              }}
            >
              <label
                className="mb-2.5 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-widest"
                style={{ color: 'var(--text-muted, #5F6578)' }}
              >
                <Calendar className="h-3.5 w-3.5" />
                Time Period
              </label>
              <div className="flex gap-1.5">
                {PERIODS.map((p) => {
                  const active = period === p.key
                  return (
                    <button
                      key={p.key}
                      onClick={() => setPeriod(p.key)}
                      className={cn(
                        'flex-1 rounded-lg px-2 py-1.5 text-[12px] font-medium transition-all duration-150',
                        active
                          ? 'text-white shadow-sm'
                          : 'hover:text-[var(--text-primary,#E8EAED)]',
                      )}
                      style={
                        active
                          ? { background: 'var(--accent, #6366F1)' }
                          : {
                              background: 'var(--bg-tertiary, #242832)',
                              color: 'var(--text-secondary, #9BA1B0)',
                            }
                      }
                    >
                      {p.label}
                    </button>
                  )
                })}
              </div>

              {period === 'custom' && (
                <div className="mt-3 grid grid-cols-2 gap-2">
                  <div>
                    <label
                      className="mb-1 block text-[10px] font-medium uppercase"
                      style={{ color: 'var(--text-muted, #5F6578)' }}
                    >
                      From
                    </label>
                    <input
                      type="date"
                      value={fromTime}
                      onChange={(e) => setFromTime(e.target.value)}
                      className="w-full rounded-lg border px-3 py-1.5 text-xs outline-none focus:ring-1 focus:ring-[var(--accent,#6366F1)]"
                      style={{
                        background: 'var(--bg-tertiary, #242832)',
                        borderColor: 'var(--bg-elevated, #2D3140)',
                        color: 'var(--text-primary, #E8EAED)',
                      }}
                    />
                  </div>
                  <div>
                    <label
                      className="mb-1 block text-[10px] font-medium uppercase"
                      style={{ color: 'var(--text-muted, #5F6578)' }}
                    >
                      To
                    </label>
                    <input
                      type="date"
                      value={toTime}
                      onChange={(e) => setToTime(e.target.value)}
                      className="w-full rounded-lg border px-3 py-1.5 text-xs outline-none focus:ring-1 focus:ring-[var(--accent,#6366F1)]"
                      style={{
                        background: 'var(--bg-tertiary, #242832)',
                        borderColor: 'var(--bg-elevated, #2D3140)',
                        color: 'var(--text-primary, #E8EAED)',
                      }}
                    />
                  </div>
                </div>
              )}
            </section>

            {/* Export Format Selection */}
            <section
              className="rounded-xl border p-4"
              style={{
                background: 'var(--bg-secondary, #1A1D27)',
                borderColor: 'var(--bg-tertiary, #242832)',
              }}
            >
              <label
                className="mb-2.5 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-widest"
                style={{ color: 'var(--text-muted, #5F6578)' }}
              >
                <Download className="h-3.5 w-3.5" />
                Export Format
              </label>
              <div className="space-y-1.5">
                {FORMATS.map((f) => {
                  const Icon = f.icon
                  const active = format === f.key
                  return (
                    <button
                      key={f.key}
                      onClick={() => setFormat(f.key)}
                      className={cn(
                        'flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-all duration-150',
                        active
                          ? 'border-[var(--accent,#6366F1)]/40 shadow-[0_0_0_1px_rgba(99,102,241,0.25)]'
                          : 'border-transparent hover:bg-[var(--bg-tertiary,#242832)]',
                      )}
                      style={{
                        background: active
                          ? 'rgba(99,102,241,0.08)'
                          : 'transparent',
                      }}
                    >
                      <div
                        className={cn(
                          'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
                          active ? 'bg-[var(--accent,#6366F1)]/20' : 'bg-[var(--bg-tertiary,#242832)]',
                        )}
                      >
                        <Icon
                          className="h-4 w-4"
                          style={{ color: active ? 'var(--accent, #6366F1)' : 'var(--text-muted, #5F6578)' }}
                        />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div
                          className="text-[12px] font-semibold"
                          style={{ color: active ? 'var(--accent, #6366F1)' : 'var(--text-primary, #E8EAED)' }}
                        >
                          {f.label}
                        </div>
                        <div
                          className="text-[10px]"
                          style={{ color: 'var(--text-muted, #5F6578)' }}
                        >
                          {f.desc}
                        </div>
                      </div>
                      {active && (
                        <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-[var(--accent,#6366F1)]">
                          <Check className="h-2.5 w-2.5 text-white" strokeWidth={3} />
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </section>

            {/* Generate Button + Status */}
            <div>
              {/* Error */}
              {error && (
                <div className="mb-3 flex items-start gap-2.5 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3.5 py-3">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-400" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[12px] font-medium text-rose-300">Generation failed</p>
                    <p className="mt-0.5 truncate text-[11px] text-rose-400/80">{error}</p>
                  </div>
                  <button
                    onClick={() => setError(null)}
                    className="text-rose-400 transition-colors hover:text-rose-300"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}

              {/* Success */}
              {success && (
                <div className="mb-3 flex items-center gap-2.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3.5 py-3">
                  <Check className="h-4 w-4 shrink-0 text-emerald-400" />
                  <p className="text-[12px] font-medium text-emerald-300">
                    Report downloaded as {activeFormat.label}
                  </p>
                </div>
              )}

              <button
                disabled={generating || !selectedType}
                onClick={handleGenerate}
                className={cn(
                  'flex w-full items-center justify-center gap-2.5 rounded-xl py-3 text-[13px] font-semibold text-white transition-all duration-200',
                  generating
                    ? 'cursor-wait opacity-70'
                    : !selectedType
                      ? 'cursor-not-allowed opacity-40'
                      : 'hover:opacity-90 active:scale-[0.995]',
                )}
                style={{
                  background:
                    generating || !selectedType
                      ? 'var(--bg-elevated, #2D3140)'
                      : 'linear-gradient(135deg, var(--accent, #6366F1), var(--accent-hover, #818CF8))',
                  boxShadow:
                    generating || !selectedType
                      ? 'none'
                      : '0 4px 20px -4px rgba(99,102,241,0.4)',
                }}
              >
                {generating ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>Generating...</span>
                  </>
                ) : (
                  <>
                    <Download className="h-4 w-4" />
                    <span>
                      {activeReport
                        ? `Generate ${activeFormat.label}`
                        : 'Select a report type'}
                    </span>
                  </>
                )}
              </button>

              {activeReport && (
                <p
                  className="mt-2 text-center text-[10px]"
                  style={{ color: 'var(--text-muted, #5F6578)' }}
                >
                  {activeReport.label} &middot; {PERIODS.find((p) => p.key === period)?.label} &middot; {activeFormat.label}
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
