import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  RUM_RANGES,
  RUM_TABS,
  type RumFilters,
  type RumRange,
  type RumTab,
} from '@/types/apm'
import { buildRumQuery, RUM_FILTER_KEYS, type RumCustomBounds } from './model'

export type RumSortOrder = 'asc' | 'desc'
export type RumDetailKind = 'view' | 'session' | 'error' | 'resource' | 'action'

export const RUM_SORTS: Record<RumTab, readonly string[]> = {
  overview: [],
  'web-vitals': [],
  views: ['views', 'sessions', 'errors', 'error_session_rate', 'lcp_p75', 'inp_p75', 'cls_p75', 'fcp_p75', 'ttfb_p75', 'load_p75', 'last_seen'],
  sessions: ['last_seen', 'started_at', 'duration_ms', 'views', 'actions', 'resources', 'long_tasks', 'errors'],
  errors: ['count', 'sessions', 'first_seen', 'last_seen'],
  resources: ['count', 'failed_count', 'duration_p75', 'size_avg', 'last_seen'],
  actions: ['count', 'error_count', 'duration_p75', 'last_seen'],
}

export const RUM_DEFAULT_SORT: Record<RumTab, { sort: string; order: RumSortOrder }> = {
  overview: { sort: '', order: 'desc' },
  'web-vitals': { sort: '', order: 'desc' },
  views: { sort: 'views', order: 'desc' },
  sessions: { sort: 'last_seen', order: 'desc' },
  errors: { sort: 'count', order: 'desc' },
  resources: { sort: 'count', order: 'desc' },
  actions: { sort: 'count', order: 'desc' },
}

function oneOf<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return allowed.includes(value as T) ? value as T : fallback
}

function positiveInt(value: string | null, fallback: number, max: number): number {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? Math.min(parsed, max) : fallback
}

export function useRumUrlState() {
  const [params, setParams] = useSearchParams()
  const tab = oneOf(params.get('tab'), RUM_TABS, 'overview')
  const filters = useMemo<RumFilters>(() => ({
    application_id: params.get('application_id') || '',
    env: params.get('env') || '',
    view_name: params.get('view_name') || '',
    browser: params.get('browser') || '',
    browser_version: params.get('browser_version') || '',
    os: params.get('os') || '',
    device_type: params.get('device_type') || '',
    country: params.get('country') || '',
    service_version: params.get('service_version') || '',
    client_ip: params.get('client_ip') || '',
    user_id: params.get('user_id') || '',
    q: params.get('q') || '',
  }), [params])
  // A custom window is only real when both bounds parse; otherwise fall back
  // to the default preset so a mangled link never yields an empty dashboard.
  const bounds = useMemo<RumCustomBounds | undefined>(() => {
    const from = params.get('from') || ''
    const to = params.get('to') || ''
    if (!from || !to) return undefined
    const start = new Date(from).getTime()
    const end = new Date(to).getTime()
    return Number.isFinite(start) && Number.isFinite(end) && end > start ? { from, to } : undefined
  }, [params])
  const rangeParam = oneOf(params.get('range'), RUM_RANGES, '24h')
  const range: RumRange = rangeParam === 'custom' ? (bounds ? 'custom' : '24h') : rangeParam
  const page = positiveInt(params.get('page'), 1, 1_000_000)
  const pageSize = oneOf(params.get('page_size'), ['25', '50', '100'] as const, '25')
  const defaults = RUM_DEFAULT_SORT[tab]
  const sort = oneOf(params.get('sort'), RUM_SORTS[tab], defaults.sort)
  const order = oneOf(params.get('order'), ['asc', 'desc'] as const, defaults.order)
  const detailKind = oneOf(params.get('detail_kind'), ['view', 'session', 'error', 'resource', 'action'] as const, 'view')
  const detailId = params.get('detail') || ''

  const update = useCallback((mutate: (next: URLSearchParams) => void, replace = true) => {
    const next = new URLSearchParams(params)
    mutate(next)
    setParams(next, { replace })
  }, [params, setParams])

  const setTab = useCallback((value: RumTab) => update((next) => {
    if (value === 'overview') next.delete('tab')
    else next.set('tab', value)
    next.delete('page')
    next.delete('sort')
    next.delete('order')
    next.delete('detail')
    next.delete('detail_kind')
  }), [update])

  const setRange = useCallback((value: RumRange) => update((next) => {
    if (value === 'custom') return
    next.set('range', value)
    next.delete('from')
    next.delete('to')
    next.delete('page')
  }), [update])

  const setCustomRange = useCallback((from: string, to: string) => update((next) => {
    next.set('range', 'custom')
    next.set('from', from)
    next.set('to', to)
    next.delete('page')
  }), [update])

  const setFilter = useCallback((key: keyof RumFilters, value: string) => update((next) => {
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('page')
    next.delete('detail')
    next.delete('detail_kind')
  }), [update])

  const clearFilters = useCallback(() => update((next) => {
    RUM_FILTER_KEYS.forEach((key) => next.delete(key))
    next.delete('page')
    next.delete('detail')
    next.delete('detail_kind')
  }), [update])

  const setPage = useCallback((value: number) => update((next) => {
    if (value <= 1) next.delete('page')
    else next.set('page', String(value))
  }), [update])

  const setPageSize = useCallback((value: number) => update((next) => {
    if (value === 25) next.delete('page_size')
    else next.set('page_size', String(value))
    next.delete('page')
  }), [update])

  const setSort = useCallback((value: string) => update((next) => {
    const same = sort === value
    const nextOrder: RumSortOrder = same && order === 'desc' ? 'asc' : 'desc'
    next.set('sort', value)
    next.set('order', nextOrder)
    next.delete('page')
  }), [order, sort, update])

  const openDetail = useCallback((kind: RumDetailKind, id: string) => update((next) => {
    next.set('detail_kind', kind)
    next.set('detail', id)
  }, false), [update])

  const closeDetail = useCallback(() => update((next) => {
    next.delete('detail')
    next.delete('detail_kind')
  }), [update])

  const drillTo = useCallback((nextTab: RumTab, nextFilters: Partial<RumFilters>) => update((next) => {
    if (nextTab === 'overview') next.delete('tab')
    else next.set('tab', nextTab)
    Object.entries(nextFilters).forEach(([key, value]) => {
      if (value) next.set(key, value)
      else next.delete(key)
    })
    next.delete('page')
    next.delete('sort')
    next.delete('order')
    next.delete('detail')
    next.delete('detail_kind')
  }, false), [update])

  return {
    tab,
    range,
    bounds,
    filters,
    page,
    pageSize: Number(pageSize),
    sort,
    order,
    detailKind,
    detailId,
    activeFilterCount: RUM_FILTER_KEYS.filter((key) => filters[key]).length,
    query: (extra?: Record<string, string | number | undefined>) => buildRumQuery(range, filters, extra, bounds),
    setTab,
    setRange,
    setCustomRange,
    setFilter,
    clearFilters,
    setPage,
    setPageSize,
    setSort,
    openDetail,
    closeDetail,
    drillTo,
  }
}
