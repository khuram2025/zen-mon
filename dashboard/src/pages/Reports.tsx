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
  ChevronDown,
  ChevronUp,
  Calendar,
  Download,
  Loader2,
  AlertCircle,
  X,
  Filter,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useDeviceGroups } from '@/hooks/useDevices'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface ReportType {
  key: string
  label: string
  description: string
  icon: React.ElementType
  accent: string
  accentBg: string
  accentRing: string
}

const REPORT_TYPES: ReportType[] = [
  {
    key: 'executive_summary',
    label: 'Executive Summary',
    description:
      'High-level overview of infrastructure health, uptime metrics, and key performance indicators for stakeholders.',
    icon: BarChart3,
    accent: 'text-indigo-400',
    accentBg: 'bg-indigo-500/15',
    accentRing: 'shadow-[0_0_0_2px_rgba(99,102,241,0.55),0_0_24px_-4px_rgba(99,102,241,0.35)]',
  },
  {
    key: 'device_health',
    label: 'Device Health',
    description:
      'Comprehensive device status breakdown including availability, response times, and resource utilisation trends.',
    icon: Monitor,
    accent: 'text-emerald-400',
    accentBg: 'bg-emerald-500/15',
    accentRing: 'shadow-[0_0_0_2px_rgba(16,185,129,0.55),0_0_24px_-4px_rgba(16,185,129,0.35)]',
  },
  {
    key: 'service_health',
    label: 'Service Health',
    description:
      'Service-level availability and SLA compliance report with latency percentiles and error-rate analysis.',
    icon: ShieldCheck,
    accent: 'text-amber-400',
    accentBg: 'bg-amber-500/15',
    accentRing: 'shadow-[0_0_0_2px_rgba(245,158,11,0.55),0_0_24px_-4px_rgba(245,158,11,0.35)]',
  },
  {
    key: 'alert_analysis',
    label: 'Alert Analysis',
    description:
      'Alert volume, frequency patterns, mean-time-to-resolve, and recurring incident correlation insights.',
    icon: Bell,
    accent: 'text-rose-400',
    accentBg: 'bg-rose-500/15',
    accentRing: 'shadow-[0_0_0_2px_rgba(244,63,94,0.55),0_0_24px_-4px_rgba(244,63,94,0.35)]',
  },
  {
    key: 'full_report',
    label: 'Full Report',
    description:
      'All-in-one comprehensive report combining every section into a single professional document.',
    icon: FileText,
    accent: 'text-violet-400',
    accentBg: 'bg-violet-500/15',
    accentRing: 'shadow-[0_0_0_2px_rgba(139,92,246,0.55),0_0_24px_-4px_rgba(139,92,246,0.35)]',
  },
]

type PeriodKey = '24h' | '7d' | '30d' | 'custom'

interface Period {
  key: PeriodKey
  label: string
}

const PERIODS: Period[] = [
  { key: '24h', label: 'Last 24 Hours' },
  { key: '7d', label: 'Last 7 Days' },
  { key: '30d', label: 'Last 30 Days' },
  { key: 'custom', label: 'Custom' },
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
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  /* data --------------------------------------------------------------- */

  const { data: deviceGroups = [] } = useDeviceGroups()

  useQuery({
    queryKey: ['report-types'],
    queryFn: () => api.get('/api/v1/reports/types'),
  })

  /* helpers ------------------------------------------------------------ */

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
      }
      if (period === 'custom') {
        if (!fromTime || !toTime) {
          setError('Please select both a start and end date for a custom period.')
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
      anchor.download = `${selectedType}_report_${new Date().toISOString().slice(0, 10)}.pdf`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)

      setSuccess(true)
      setTimeout(() => setSuccess(false), 5000)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Report generation failed.'
      setError(message)
    } finally {
      setGenerating(false)
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Render                                                             */
  /* ------------------------------------------------------------------ */

  return (
    <div className="min-h-screen" style={{ background: 'var(--bg-primary, #0F1117)' }}>
      <div className="mx-auto max-w-6xl px-6 py-10">
        {/* ---- Header ------------------------------------------------- */}
        <div className="mb-10">
          <div className="flex items-center gap-3 mb-2">
            <div
              className="flex h-10 w-10 items-center justify-center rounded-xl"
              style={{ background: 'var(--bg-tertiary, #242832)' }}
            >
              <FileText className="h-5 w-5" style={{ color: 'var(--accent, #6366F1)' }} />
            </div>
            <h1
              className="text-2xl font-semibold tracking-tight"
              style={{ color: 'var(--text-primary, #E8EAED)' }}
            >
              Reports
            </h1>
          </div>
          <p
            className="ml-[52px] text-sm"
            style={{ color: 'var(--text-secondary, #9BA1B0)' }}
          >
            Generate professional PDF reports for your infrastructure
          </p>
        </div>

        {/* ---- Report Type Cards -------------------------------------- */}
        <section className="mb-10">
          <h2
            className="mb-4 text-xs font-semibold uppercase tracking-widest"
            style={{ color: 'var(--text-muted, #5F6578)' }}
          >
            Select Report Type
          </h2>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
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
                    'group relative flex min-h-[188px] flex-col rounded-2xl border p-6 text-left transition-all duration-200',
                    isSelected
                      ? rt.accentRing + ' border-transparent'
                      : 'border-[var(--bg-tertiary,#242832)] hover:-translate-y-0.5 hover:border-[var(--bg-elevated,#2D3140)]',
                  )}
                  style={{
                    background: isSelected
                      ? 'var(--bg-tertiary, #242832)'
                      : 'var(--bg-secondary, #1A1D27)',
                  }}
                >
                  {/* checkmark overlay */}
                  {isSelected && (
                    <span className="absolute right-4 top-4 flex h-6 w-6 items-center justify-center rounded-full bg-[var(--accent,#6366F1)]">
                      <Check className="h-3.5 w-3.5 text-white" strokeWidth={3} />
                    </span>
                  )}

                  {/* icon */}
                  <div
                    className={cn(
                      'mb-4 flex h-11 w-11 items-center justify-center rounded-xl transition-colors',
                      rt.accentBg,
                    )}
                  >
                    <Icon className={cn('h-5 w-5', rt.accent)} />
                  </div>

                  {/* text */}
                  <h3
                    className="mb-1.5 text-[15px] font-semibold"
                    style={{ color: 'var(--text-primary, #E8EAED)' }}
                  >
                    {rt.label}
                  </h3>
                  <p
                    className="text-[13px] leading-relaxed"
                    style={{ color: 'var(--text-secondary, #9BA1B0)' }}
                  >
                    {rt.description}
                  </p>
                </button>
              )
            })}
          </div>
        </section>

        {/* ---- Configuration Panel ------------------------------------ */}
        {activeReport && (
          <section
            className="mb-10 rounded-2xl border p-8"
            style={{
              background: 'var(--bg-secondary, #1A1D27)',
              borderColor: 'var(--bg-tertiary, #242832)',
            }}
          >
            <h2
              className="mb-6 text-lg font-semibold"
              style={{ color: 'var(--text-primary, #E8EAED)' }}
            >
              Configure Report
            </h2>

            {/* -- Time Period ------------------------------------------ */}
            <div className="mb-8">
              <label
                className="mb-3 flex items-center gap-2 text-sm font-medium"
                style={{ color: 'var(--text-secondary, #9BA1B0)' }}
              >
                <Calendar className="h-4 w-4" />
                Time Period
              </label>

              <div className="flex flex-wrap gap-2">
                {PERIODS.map((p) => {
                  const active = period === p.key
                  return (
                    <button
                      key={p.key}
                      onClick={() => setPeriod(p.key)}
                      className={cn(
                        'rounded-full px-5 py-2 text-sm font-medium transition-all duration-150',
                        active
                          ? 'text-white shadow-md'
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

              {/* custom dates */}
              {period === 'custom' && (
                <div className="mt-4 flex flex-wrap items-center gap-4">
                  <div className="flex flex-col gap-1.5">
                    <label
                      className="text-xs font-medium"
                      style={{ color: 'var(--text-muted, #5F6578)' }}
                    >
                      From
                    </label>
                    <input
                      type="date"
                      value={fromTime}
                      onChange={(e) => setFromTime(e.target.value)}
                      className="rounded-lg border px-4 py-2 text-sm outline-none focus:ring-2 focus:ring-[var(--accent,#6366F1)]"
                      style={{
                        background: 'var(--bg-tertiary, #242832)',
                        borderColor: 'var(--bg-elevated, #2D3140)',
                        color: 'var(--text-primary, #E8EAED)',
                      }}
                    />
                  </div>
                  <span
                    className="mt-5 text-sm"
                    style={{ color: 'var(--text-muted, #5F6578)' }}
                  >
                    to
                  </span>
                  <div className="flex flex-col gap-1.5">
                    <label
                      className="text-xs font-medium"
                      style={{ color: 'var(--text-muted, #5F6578)' }}
                    >
                      To
                    </label>
                    <input
                      type="date"
                      value={toTime}
                      onChange={(e) => setToTime(e.target.value)}
                      className="rounded-lg border px-4 py-2 text-sm outline-none focus:ring-2 focus:ring-[var(--accent,#6366F1)]"
                      style={{
                        background: 'var(--bg-tertiary, #242832)',
                        borderColor: 'var(--bg-elevated, #2D3140)',
                        color: 'var(--text-primary, #E8EAED)',
                      }}
                    />
                  </div>
                </div>
              )}
            </div>

            {/* -- Filters (collapsible) -------------------------------- */}
            <div className="mb-8">
              <button
                onClick={() => setFiltersOpen((o) => !o)}
                className="flex items-center gap-2 text-sm font-medium transition-colors hover:text-[var(--text-primary,#E8EAED)]"
                style={{ color: 'var(--text-secondary, #9BA1B0)' }}
              >
                <Filter className="h-4 w-4" />
                Filters
                {selectedGroups.length > 0 && (
                  <span
                    className="ml-1 inline-flex h-5 min-w-[20px] items-center justify-center rounded-full px-1.5 text-[11px] font-semibold text-white"
                    style={{ background: 'var(--accent, #6366F1)' }}
                  >
                    {selectedGroups.length}
                  </span>
                )}
                {filtersOpen ? (
                  <ChevronUp className="ml-auto h-4 w-4" />
                ) : (
                  <ChevronDown className="ml-auto h-4 w-4" />
                )}
              </button>

              {filtersOpen && (
                <div
                  className="mt-4 rounded-xl border p-5"
                  style={{
                    background: 'var(--bg-tertiary, #242832)',
                    borderColor: 'var(--bg-elevated, #2D3140)',
                  }}
                >
                  <p
                    className="mb-3 text-xs font-medium uppercase tracking-wider"
                    style={{ color: 'var(--text-muted, #5F6578)' }}
                  >
                    Filter by Device Group
                  </p>

                  {deviceGroups.length === 0 ? (
                    <p
                      className="text-sm italic"
                      style={{ color: 'var(--text-muted, #5F6578)' }}
                    >
                      No device groups available. All devices will be included.
                    </p>
                  ) : (
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                      {deviceGroups.map((group: { id: string; name: string }) => {
                        const checked = selectedGroups.includes(group.id)
                        return (
                          <label
                            key={group.id}
                            className={cn(
                              'flex cursor-pointer items-center gap-3 rounded-lg border px-4 py-2.5 text-sm transition-colors',
                              checked
                                ? 'border-[var(--accent,#6366F1)]/40'
                                : 'border-transparent hover:bg-[var(--bg-elevated,#2D3140)]',
                            )}
                            style={{ color: 'var(--text-primary, #E8EAED)' }}
                          >
                            <span
                              className={cn(
                                'flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded border transition-colors',
                                checked
                                  ? 'border-transparent bg-[var(--accent,#6366F1)]'
                                  : 'border-[var(--bg-elevated,#2D3140)] bg-[var(--bg-primary,#0F1117)]',
                              )}
                            >
                              {checked && (
                                <Check className="h-3 w-3 text-white" strokeWidth={3} />
                              )}
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
                      className="mt-3 text-xs font-medium transition-colors hover:text-[var(--text-primary,#E8EAED)]"
                      style={{ color: 'var(--text-secondary, #9BA1B0)' }}
                    >
                      Clear selection
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* -- Error Banner ----------------------------------------- */}
            {error && (
              <div className="mb-6 flex items-start gap-3 rounded-xl border border-rose-500/30 bg-rose-500/10 px-5 py-4">
                <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-rose-400" />
                <div className="flex-1">
                  <p className="text-sm font-medium text-rose-300">
                    Report generation failed
                  </p>
                  <p className="mt-0.5 text-xs text-rose-400/80">{error}</p>
                </div>
                <button
                  onClick={() => setError(null)}
                  className="text-rose-400 transition-colors hover:text-rose-300"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            )}

            {/* -- Success Banner --------------------------------------- */}
            {success && (
              <div className="mb-6 flex items-center gap-3 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-5 py-4">
                <Check className="h-5 w-5 shrink-0 text-emerald-400" />
                <p className="text-sm font-medium text-emerald-300">
                  Your report has been generated and is downloading now.
                </p>
              </div>
            )}

            {/* -- Generate Button -------------------------------------- */}
            <button
              disabled={generating}
              onClick={handleGenerate}
              className={cn(
                'group relative flex w-full items-center justify-center gap-3 rounded-xl py-4 text-[15px] font-semibold text-white transition-all duration-200',
                generating
                  ? 'cursor-wait opacity-80'
                  : 'hover:opacity-90 active:scale-[0.995]',
              )}
              style={{
                background: generating
                  ? 'var(--bg-elevated, #2D3140)'
                  : 'linear-gradient(135deg, var(--accent, #6366F1), var(--accent-hover, #818CF8))',
                boxShadow: generating
                  ? 'none'
                  : '0 4px 24px -4px rgba(99,102,241,0.4)',
              }}
            >
              {generating ? (
                <>
                  <Loader2 className="h-5 w-5 animate-spin" />
                  <span>Generating Report...</span>
                </>
              ) : (
                <>
                  <Download className="h-5 w-5" />
                  <span>Generate {activeReport.label}</span>
                </>
              )}
            </button>

            {/* -- Subtle helper text ----------------------------------- */}
            <p
              className="mt-3 text-center text-xs"
              style={{ color: 'var(--text-muted, #5F6578)' }}
            >
              Your report will download automatically as a PDF
            </p>
          </section>
        )}

        {/* ---- Empty / Prompt State ----------------------------------- */}
        {!activeReport && (
          <div
            className="flex flex-col items-center justify-center rounded-2xl border border-dashed py-20"
            style={{
              borderColor: 'var(--bg-tertiary, #242832)',
              color: 'var(--text-muted, #5F6578)',
            }}
          >
            <FileText className="mb-4 h-10 w-10 opacity-40" />
            <p className="text-sm font-medium">Select a report type above to get started</p>
          </div>
        )}
      </div>
    </div>
  )
}
