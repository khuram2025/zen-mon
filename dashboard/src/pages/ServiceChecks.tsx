import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import { useServiceChecks, useServiceCheckSummary } from '@/hooks/useServiceChecks'
import { formatRTT, timeAgo, cn } from '@/lib/utils'
import { api } from '@/lib/api'
import type { ServiceCheck, ServiceCheckSummary } from '@/types'
import {
  Activity,
  CheckCircle,
  XCircle,
  AlertTriangle,
  HelpCircle,
  Globe,
  Plug,
  ShieldCheck,
  Search,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Download,
  Trash2,
  X,
  Filter,
} from 'lucide-react'

// ── Constants ──────────────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = {
  up: '#22C55E',
  down: '#EF4444',
  warning: '#F97316',
  degraded: '#EAB308',
  unknown: '#6B7280',
}

const PAGE_SIZE = 20

// ── Inline FilterDropdown ──────────────────────────────────────────────────────

function FilterDropdown({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: { label: string; value: string }[]
  onChange: (v: string) => void
}) {
  const [open, setOpen] = useState(false)
  const selected = options.find((o) => o.value === value)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors"
        style={{
          background: 'var(--bg-tertiary)',
          color: 'var(--text-primary)',
          border: '1px solid rgba(255,255,255,0.06)',
        }}
      >
        <Filter size={14} style={{ color: 'var(--text-muted)' }} />
        <span style={{ color: 'var(--text-secondary)' }}>{label}:</span>
        <span>{selected?.label ?? value}</span>
        <ChevronDown size={14} style={{ color: 'var(--text-muted)' }} />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div
            className="absolute left-0 top-full z-50 mt-1 min-w-[160px] overflow-hidden rounded-lg py-1 shadow-xl"
            style={{
              background: 'var(--bg-elevated)',
              border: '1px solid rgba(255,255,255,0.08)',
            }}
          >
            {options.map((opt) => (
              <button
                key={opt.value}
                onClick={() => {
                  onChange(opt.value)
                  setOpen(false)
                }}
                className="flex w-full items-center px-3 py-2 text-left text-sm transition-colors hover:bg-white/5"
                style={{
                  color: opt.value === value ? 'var(--accent)' : 'var(--text-primary)',
                  fontWeight: opt.value === value ? 600 : 400,
                }}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// ── Delete Confirmation Dialog ─────────────────────────────────────────────────

function DeleteDialog({
  count,
  onConfirm,
  onCancel,
}: {
  count: number
  onConfirm: () => void
  onCancel: () => void
}) {
  const [typed, setTyped] = useState('')

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div
        className="w-full max-w-md rounded-xl p-6 shadow-2xl"
        style={{
          background: 'var(--bg-secondary)',
          border: '1px solid rgba(255,255,255,0.08)',
        }}
      >
        <div className="mb-1 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-red-500/10">
            <Trash2 size={20} className="text-red-400" />
          </div>
          <h3 className="text-lg font-semibold" style={{ color: 'var(--text-primary)' }}>
            Delete {count} Service Check{count > 1 ? 's' : ''}
          </h3>
        </div>
        <p className="mb-4 mt-3 text-sm" style={{ color: 'var(--text-secondary)' }}>
          This action is permanent and cannot be undone. Type{' '}
          <code
            className="rounded px-1.5 py-0.5 font-mono text-xs font-bold text-red-400"
            style={{ background: 'rgba(239,68,68,0.1)' }}
          >
            delete
          </code>{' '}
          to confirm.
        </p>
        <input
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder='Type "delete" to confirm'
          className="mb-4 w-full rounded-lg px-3 py-2.5 text-sm outline-none transition-colors focus:ring-2 focus:ring-red-500/40"
          style={{
            background: 'var(--bg-tertiary)',
            color: 'var(--text-primary)',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
        />
        <div className="flex justify-end gap-3">
          <button
            onClick={onCancel}
            className="rounded-lg px-4 py-2 text-sm font-medium transition-colors hover:bg-white/5"
            style={{ color: 'var(--text-secondary)' }}
          >
            Cancel
          </button>
          <button
            disabled={typed !== 'delete'}
            onClick={onConfirm}
            className="rounded-lg px-4 py-2 text-sm font-semibold text-white transition-all disabled:cursor-not-allowed disabled:opacity-40"
            style={{
              background: typed === 'delete' ? '#EF4444' : 'rgba(239,68,68,0.3)',
            }}
          >
            Delete Permanently
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Main Page ──────────────────────────────────────────────────────────────────

export function ServiceChecksPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // Filters
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(0)

  // Selection
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [showDeleteDialog, setShowDeleteDialog] = useState(false)

  // Data
  const { data: summaryData } = useServiceCheckSummary()
  const summary: ServiceCheckSummary = summaryData ?? { total: 0, up: 0, down: 0, warning: 0, degraded: 0, unknown: 0 }

  const { data: checksResponse, isLoading } = useServiceChecks({
    check_type: typeFilter || undefined,
    status: statusFilter || undefined,
    search: search || undefined,
    skip: page * PAGE_SIZE,
    limit: PAGE_SIZE,
  })

  const checks: ServiceCheck[] = checksResponse?.data ?? []
  const totalRecords = checksResponse?.meta?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(totalRecords / PAGE_SIZE))

  // All checks for heatmap (lightweight, first page of a large set)
  const { data: allChecksResponse } = useServiceChecks({ limit: 200, skip: 0 })
  const allChecks: ServiceCheck[] = allChecksResponse?.data ?? []

  // Bulk delete mutation
  const bulkDeleteMutation = useMutation({
    mutationFn: (ids: string[]) => api.post('/service-checks/bulk-delete', { check_ids: ids }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['serviceChecks'] })
      queryClient.invalidateQueries({ queryKey: ['serviceCheckSummary'] })
      setSelectedIds(new Set())
      setShowDeleteDialog(false)
    },
  })

  // Export handler
  const handleExport = async () => {
    try {
      const data = await api.get<unknown[]>('/service-checks/export/json')
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'service-checks-export.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      // silent
    }
  }

  // Selection helpers
  const allOnPageSelected = checks.length > 0 && checks.every((c) => selectedIds.has(c.id))

  function toggleAll() {
    if (allOnPageSelected) {
      const next = new Set(selectedIds)
      checks.forEach((c) => next.delete(c.id))
      setSelectedIds(next)
    } else {
      const next = new Set(selectedIds)
      checks.forEach((c) => next.add(c.id))
      setSelectedIds(next)
    }
  }

  function toggleOne(id: string) {
    const next = new Set(selectedIds)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelectedIds(next)
  }

  // ── Summary Cards Config ─────────────────────────────────────────────────

  const summaryCards = [
    { label: 'Total Checks', value: summary.total, color: '#6366F1', icon: Activity, pct: 100 },
    { label: 'Healthy', value: summary.up, color: '#22C55E', icon: CheckCircle, pct: summary.total ? Math.round((summary.up / summary.total) * 100) : 0 },
    { label: 'Down', value: summary.down, color: '#EF4444', icon: XCircle, pct: summary.total ? Math.round((summary.down / summary.total) * 100) : 0 },
    { label: 'Warning', value: (summary.warning ?? 0) + (summary.degraded ?? 0), color: '#F97316', icon: AlertTriangle, pct: summary.total ? Math.round((((summary.warning ?? 0) + (summary.degraded ?? 0)) / summary.total) * 100) : 0 },
    { label: 'Unknown', value: summary.unknown, color: '#6B7280', icon: HelpCircle, pct: summary.total ? Math.round((summary.unknown / summary.total) * 100) : 0 },
  ]

  // ── ECharts Donut ────────────────────────────────────────────────────────

  const chartOption = useMemo(() => {
    const segments = [
      { name: 'Healthy', value: summary.up, itemStyle: { color: '#22C55E' } },
      { name: 'Down', value: summary.down, itemStyle: { color: '#EF4444' } },
      { name: 'Warning', value: (summary.warning ?? 0) + (summary.degraded ?? 0), itemStyle: { color: '#F97316' } },
      { name: 'Unknown', value: summary.unknown, itemStyle: { color: '#6B7280' } },
    ].filter((s) => s.value > 0)

    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item',
        backgroundColor: '#1A1D27',
        borderColor: 'rgba(255,255,255,0.08)',
        textStyle: { color: '#E8EAED', fontSize: 13 },
        formatter: (p: { name: string; value: number; percent: number }) =>
          `<div style="font-weight:600;margin-bottom:4px">${p.name}</div>
           <div style="font-size:20px;font-weight:700;font-family:monospace">${p.value}</div>
           <div style="color:#9BA1B0;font-size:12px;margin-top:2px">${p.percent.toFixed(1)}% of total</div>`,
      },
      series: [
        {
          type: 'pie',
          radius: ['58%', '80%'],
          center: ['50%', '50%'],
          avoidLabelOverlap: false,
          padAngle: 3,
          itemStyle: { borderRadius: 6 },
          label: {
            show: true,
            position: 'center',
            formatter: () => `{total|${summary.total}}\n{label|Services}`,
            rich: {
              total: { fontSize: 32, fontWeight: 700, color: '#E8EAED', fontFamily: 'ui-monospace, monospace', lineHeight: 40 },
              label: { fontSize: 13, color: '#9BA1B0', lineHeight: 22 },
            },
          },
          emphasis: {
            label: { show: true },
            itemStyle: { shadowBlur: 20, shadowColor: 'rgba(0,0,0,0.4)' },
          },
          data: segments,
        },
      ],
    }
  }, [summary])

  // ── Type icon helper ─────────────────────────────────────────────────────

  function TypePill({ type }: { type: string }) {
    const config: Record<string, { icon: typeof Globe; bg: string; text: string }> = {
      http: { icon: Globe, bg: 'rgba(99,102,241,0.1)', text: '#818CF8' },
      tcp: { icon: Plug, bg: 'rgba(16,185,129,0.1)', text: '#34D399' },
      tls: { icon: ShieldCheck, bg: 'rgba(245,158,11,0.1)', text: '#FBBF24' },
    }
    const c = config[type] ?? config.http
    const Icon = c.icon
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-wider"
        style={{ background: c.bg, color: c.text }}
      >
        <Icon size={13} />
        {type}
      </span>
    )
  }

  // ── Target display ───────────────────────────────────────────────────────

  function targetDisplay(check: ServiceCheck) {
    if (check.check_type === 'http') return check.target_url ?? '—'
    return check.target_host ? `${check.target_host}:${check.target_port ?? ''}` : '—'
  }

  // ── Pagination range ─────────────────────────────────────────────────────

  function pageRange(): number[] {
    const range: number[] = []
    const maxVisible = 7
    let start = Math.max(0, page - Math.floor(maxVisible / 2))
    let end = Math.min(totalPages, start + maxVisible)
    if (end - start < maxVisible) start = Math.max(0, end - maxVisible)
    for (let i = start; i < end; i++) range.push(i)
    return range
  }

  // Active filter pills
  const activeFilters: { label: string; clear: () => void }[] = []
  if (typeFilter) activeFilters.push({ label: `Type: ${typeFilter.toUpperCase()}`, clear: () => { setTypeFilter(''); setPage(0) } })
  if (statusFilter) activeFilters.push({ label: `Status: ${statusFilter}`, clear: () => { setStatusFilter(''); setPage(0) } })
  if (search) activeFilters.push({ label: `Search: "${search}"`, clear: () => { setSearch(''); setPage(0) } })

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen w-full" style={{ background: 'var(--bg-primary)', color: 'var(--text-primary)' }}>
      <div className="mx-auto w-full max-w-[1600px] px-6 py-8">

        {/* Header */}
        <div className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight" style={{ color: 'var(--text-primary)' }}>
              Service Checks
            </h1>
            <p className="mt-1 text-sm" style={{ color: 'var(--text-secondary)' }}>
              Monitor HTTP, TCP, and TLS endpoints across your infrastructure
            </p>
          </div>
          <button
            onClick={handleExport}
            className="flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium transition-all hover:brightness-110"
            style={{
              background: 'var(--bg-tertiary)',
              color: 'var(--text-primary)',
              border: '1px solid rgba(255,255,255,0.06)',
            }}
          >
            <Download size={16} />
            Export JSON
          </button>
        </div>

        {/* ── 1. Top Visual Section ──────────────────────────────────────── */}
        <div
          className="mb-6 grid grid-cols-1 gap-6 rounded-xl p-6 lg:grid-cols-5"
          style={{
            background: 'var(--bg-secondary)',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
        >
          {/* Left: Summary Cards */}
          <div className="col-span-1 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:col-span-3 lg:grid-cols-3">
            {summaryCards.map((card) => {
              const Icon = card.icon
              return (
                <div
                  key={card.label}
                  className="relative overflow-hidden rounded-lg px-4 py-4 transition-all hover:brightness-110"
                  style={{
                    background: 'var(--bg-tertiary)',
                    borderLeft: `4px solid ${card.color}`,
                  }}
                >
                  {/* Subtle glow */}
                  <div
                    className="pointer-events-none absolute -right-4 -top-4 h-20 w-20 rounded-full opacity-[0.07]"
                    style={{ background: card.color, filter: 'blur(20px)' }}
                  />
                  <div className="flex items-center gap-2.5">
                    <Icon size={18} style={{ color: card.color }} />
                    <span className="text-xs font-medium uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                      {card.label}
                    </span>
                  </div>
                  <div className="mt-2 font-mono text-3xl font-bold" style={{ color: 'var(--text-primary)' }}>
                    {card.value}
                  </div>
                  <div className="mt-1 text-xs" style={{ color: 'var(--text-muted)' }}>
                    {card.pct}% of total
                  </div>
                </div>
              )
            })}
          </div>

          {/* Right: Ring Chart */}
          <div className="col-span-1 flex items-center justify-center lg:col-span-2">
            <ReactECharts
              option={chartOption}
              style={{ width: '100%', height: 280 }}
              opts={{ renderer: 'svg' }}
            />
          </div>
        </div>

        {/* ── 2. Heatmap Strip ───────────────────────────────────────────── */}
        {allChecks.length > 0 && (
          <div
            className="mb-6 overflow-hidden rounded-xl p-4"
            style={{
              background: 'var(--bg-secondary)',
              border: '1px solid rgba(255,255,255,0.06)',
            }}
          >
            <div className="mb-3 flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                Service Health Heatmap
              </span>
              <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                {allChecks.length} checks
              </span>
            </div>
            <div className="flex flex-wrap gap-1">
              {allChecks.map((check) => (
                <button
                  key={check.id}
                  onClick={() => navigate(`/service-checks/${check.id}`)}
                  className="group relative h-5 w-5 rounded-sm transition-all hover:scale-150 hover:z-10"
                  style={{ background: STATUS_COLORS[check.status] ?? STATUS_COLORS.unknown }}
                  title={`${check.name} (${check.check_type.toUpperCase()}) — ${check.status.toUpperCase()}`}
                >
                  {/* Tooltip on hover */}
                  <div
                    className="pointer-events-none absolute -top-10 left-1/2 z-50 hidden -translate-x-1/2 whitespace-nowrap rounded-md px-2 py-1 text-xs font-medium shadow-lg group-hover:block"
                    style={{
                      background: 'var(--bg-elevated)',
                      color: 'var(--text-primary)',
                      border: '1px solid rgba(255,255,255,0.1)',
                    }}
                  >
                    {check.name} &middot; {check.check_type.toUpperCase()}
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* ── 3. Filter Bar ──────────────────────────────────────────────── */}
        <div className="mb-4 flex flex-wrap items-center gap-3">
          {/* Search */}
          <div className="relative flex-1" style={{ minWidth: 220, maxWidth: 360 }}>
            <Search
              size={16}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2"
              style={{ color: 'var(--text-muted)' }}
            />
            <input
              type="text"
              value={search}
              onChange={(e) => { setSearch(e.target.value); setPage(0) }}
              placeholder="Search checks by name, host, or URL..."
              className="w-full rounded-lg py-2.5 pl-10 pr-3 text-sm outline-none transition-colors focus:ring-2 focus:ring-indigo-500/40"
              style={{
                background: 'var(--bg-tertiary)',
                color: 'var(--text-primary)',
                border: '1px solid rgba(255,255,255,0.06)',
              }}
            />
          </div>

          <FilterDropdown
            label="Type"
            value={typeFilter}
            options={[
              { label: 'All Types', value: '' },
              { label: 'HTTP', value: 'http' },
              { label: 'TCP', value: 'tcp' },
              { label: 'TLS', value: 'tls' },
            ]}
            onChange={(v) => { setTypeFilter(v); setPage(0) }}
          />

          <FilterDropdown
            label="Status"
            value={statusFilter}
            options={[
              { label: 'All Statuses', value: '' },
              { label: 'Up', value: 'up' },
              { label: 'Down', value: 'down' },
              { label: 'Warning', value: 'warning' },
              { label: 'Unknown', value: 'unknown' },
            ]}
            onChange={(v) => { setStatusFilter(v); setPage(0) }}
          />

          {/* Active filter pills */}
          {activeFilters.map((f) => (
            <span
              key={f.label}
              className="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium"
              style={{ background: 'rgba(99,102,241,0.12)', color: '#818CF8' }}
            >
              {f.label}
              <button onClick={f.clear} className="transition-colors hover:text-white">
                <X size={12} />
              </button>
            </span>
          ))}
        </div>

        {/* ── 4. Selection Toolbar ────────────────────────────────────────── */}
        {selectedIds.size > 0 && (
          <div
            className="mb-4 flex items-center justify-between rounded-xl px-5 py-3"
            style={{
              background: 'rgba(99,102,241,0.08)',
              border: '1px solid rgba(99,102,241,0.2)',
            }}
          >
            <span className="text-sm font-medium" style={{ color: '#818CF8' }}>
              {selectedIds.size} check{selectedIds.size > 1 ? 's' : ''} selected
            </span>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setSelectedIds(new Set())}
                className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors hover:bg-white/5"
                style={{ color: 'var(--text-secondary)' }}
              >
                Clear Selection
              </button>
              <button
                onClick={() => setShowDeleteDialog(true)}
                className="flex items-center gap-1.5 rounded-lg bg-red-500/10 px-3 py-1.5 text-xs font-semibold text-red-400 transition-colors hover:bg-red-500/20"
              >
                <Trash2 size={13} />
                Delete Selected
              </button>
            </div>
          </div>
        )}

        {/* ── 5. Table ───────────────────────────────────────────────────── */}
        <div
          className="overflow-hidden rounded-xl"
          style={{
            background: 'var(--bg-secondary)',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
        >
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr style={{ background: 'var(--bg-tertiary)' }}>
                  <th className="w-12 px-4 py-3 text-left">
                    <input
                      type="checkbox"
                      checked={allOnPageSelected}
                      onChange={toggleAll}
                      className="h-4 w-4 rounded border-gray-600 accent-indigo-500"
                    />
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                    Status
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                    Name
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                    Type
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                    Target
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                    Device
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                    Response
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
                    Last Check
                  </th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr>
                    <td colSpan={8} className="px-4 py-20 text-center">
                      <div className="flex flex-col items-center gap-3">
                        <div className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
                        <span className="text-sm" style={{ color: 'var(--text-muted)' }}>Loading service checks...</span>
                      </div>
                    </td>
                  </tr>
                ) : checks.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="px-4 py-20 text-center">
                      <div className="flex flex-col items-center gap-2">
                        <Activity size={32} style={{ color: 'var(--text-muted)' }} />
                        <span className="text-sm font-medium" style={{ color: 'var(--text-secondary)' }}>No service checks found</span>
                        <span className="text-xs" style={{ color: 'var(--text-muted)' }}>Try adjusting your filters</span>
                      </div>
                    </td>
                  </tr>
                ) : (
                  checks.map((check) => {
                    const statusColor = STATUS_COLORS[check.status] ?? STATUS_COLORS.unknown
                    const tlsNearExpiry =
                      check.check_type === 'tls' &&
                      check.tls_days_remaining != null &&
                      check.tls_days_remaining < 30

                    return (
                      <tr
                        key={check.id}
                        onClick={() => navigate(`/service-checks/${check.id}`)}
                        className="cursor-pointer transition-colors hover:bg-white/[0.03]"
                        style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}
                      >
                        {/* Checkbox */}
                        <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={selectedIds.has(check.id)}
                            onChange={() => toggleOne(check.id)}
                            className="h-4 w-4 rounded border-gray-600 accent-indigo-500"
                          />
                        </td>

                        {/* Status */}
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <span
                              className="inline-block h-2.5 w-2.5 rounded-full shadow-sm"
                              style={{
                                background: statusColor,
                                boxShadow: `0 0 8px ${statusColor}40`,
                              }}
                            />
                            <span
                              className="text-xs font-bold uppercase tracking-wider"
                              style={{ color: statusColor }}
                            >
                              {check.status}
                            </span>
                            {tlsNearExpiry && (
                              <span
                                className="rounded px-1.5 py-0.5 font-mono text-[10px] font-bold"
                                style={{
                                  background:
                                    check.tls_days_remaining! < 7
                                      ? 'rgba(239,68,68,0.15)'
                                      : check.tls_days_remaining! < 14
                                        ? 'rgba(249,115,22,0.15)'
                                        : 'rgba(234,179,8,0.15)',
                                  color:
                                    check.tls_days_remaining! < 7
                                      ? '#EF4444'
                                      : check.tls_days_remaining! < 14
                                        ? '#F97316'
                                        : '#EAB308',
                                }}
                              >
                                {check.tls_days_remaining}d
                              </span>
                            )}
                          </div>
                        </td>

                        {/* Name */}
                        <td className="px-4 py-3">
                          <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                            {check.name}
                          </span>
                        </td>

                        {/* Type */}
                        <td className="px-4 py-3">
                          <TypePill type={check.check_type} />
                        </td>

                        {/* Target */}
                        <td className="max-w-[240px] truncate px-4 py-3 font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>
                          {targetDisplay(check)}
                        </td>

                        {/* Device */}
                        <td className="px-4 py-3 text-sm" style={{ color: 'var(--text-secondary)' }}>
                          {check.device_hostname || '—'}
                        </td>

                        {/* Response */}
                        <td className="px-4 py-3 text-right font-mono text-sm" style={{ color: 'var(--text-primary)' }}>
                          {check.last_response_ms != null ? formatRTT(check.last_response_ms) : '—'}
                        </td>

                        {/* Last Check */}
                        <td className="px-4 py-3 text-right text-sm" style={{ color: 'var(--text-muted)' }}>
                          {check.last_check_at ? timeAgo(check.last_check_at) : '—'}
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── 6. Pagination ──────────────────────────────────────────────── */}
        {totalPages > 1 && (
          <div className="mt-4 flex items-center justify-between">
            <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
              Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, totalRecords)} of {totalRecords}
            </span>
            <div className="flex items-center gap-1">
              <button
                disabled={page === 0}
                onClick={() => setPage(page - 1)}
                className="rounded-lg p-2 transition-colors hover:bg-white/5 disabled:opacity-30"
                style={{ color: 'var(--text-secondary)' }}
              >
                <ChevronLeft size={16} />
              </button>
              {pageRange().map((p) => (
                <button
                  key={p}
                  onClick={() => setPage(p)}
                  className="min-w-[36px] rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
                  style={{
                    background: p === page ? 'var(--accent)' : 'transparent',
                    color: p === page ? '#fff' : 'var(--text-secondary)',
                  }}
                >
                  {p + 1}
                </button>
              ))}
              <button
                disabled={page >= totalPages - 1}
                onClick={() => setPage(page + 1)}
                className="rounded-lg p-2 transition-colors hover:bg-white/5 disabled:opacity-30"
                style={{ color: 'var(--text-secondary)' }}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ── 7. Delete Dialog ───────────────────────────────────────────── */}
      {showDeleteDialog && (
        <DeleteDialog
          count={selectedIds.size}
          onConfirm={() => bulkDeleteMutation.mutate(Array.from(selectedIds))}
          onCancel={() => setShowDeleteDialog(false)}
        />
      )}
    </div>
  )
}
