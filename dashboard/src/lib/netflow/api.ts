/**
 * NetFlow data access (contract §9.3, §10).
 *
 * Every NetFlow read goes through `nf.get`, which asks for the response envelope (`envelope=1`)
 * and returns `{data, meta}`: the legacy body plus the window, source, retention, sampling,
 * truncation and warnings the page must disclose. An endpoint that does not return the envelope
 * yet yields `meta: null` rather than a guessed one.
 *
 * `useNfQuery` wraps TanStack Query with the NetFlow policies: keep the previous result visible
 * (dimmed, flagged as loading) while a new window or filter loads instead of flashing zeros;
 * poll relative windows at a cadence that shrinks as they grow; never poll fixed windows; never
 * retry client errors (a 400/403 will not heal) or budget timeouts (a retry doubles the load).
 */
import { keepPreviousData, useQuery, type QueryKey, type UseQueryResult } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { flowFilterToApiParams, type FlowFilter } from './filters'
import { isRetryableError } from './status'
import type { NfCoverage, NfMeta, NfResult, NfWarning } from './types'
import { isPendingParams, pollIntervalFor, windowParams, type NfMetaWindow, type NfWindow } from './window'

export type NfParamValue = string | number | boolean | null | undefined | readonly (string | number)[]
export type NfParams = Record<string, NfParamValue>

/**
 * Query parameters without empty values. Arrays are comma-joined (the API's list form, e.g.
 * `dst_ports=22,3389`), `true` is "1" and `false` is omitted. Numeric zero is kept.
 */
export function toSearchParams(params?: NfParams | null): URLSearchParams {
  const sp = new URLSearchParams()
  if (!params) return sp
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '' || v === false) continue
    if (typeof v === 'number' && !Number.isFinite(v)) continue
    if (Array.isArray(v)) {
      if (v.length) sp.set(k, v.join(','))
      continue
    }
    sp.set(k, v === true ? '1' : String(v))
  }
  return sp
}

/** Order-independent text of a parameter set, for query keys. */
export function paramsKey(params?: NfParams | null): string {
  const sp = toSearchParams(params)
  sp.sort()
  return sp.toString()
}

/** Window, filter and endpoint-specific parameters in one set; a filter's `tz` overrides the window's. */
export function nfParams(w: NfWindow | null | undefined, filter?: FlowFilter | null, extra?: NfParams | null): NfParams {
  return { ...(w ? windowParams(w) : {}), ...(filter ? flowFilterToApiParams(filter) : {}), ...(extra ?? {}) }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

/** Coerce a `meta` block defensively: a missing field is unknown (null), not a default value. */
export function normalizeMeta(raw: unknown): NfMeta {
  const m = isRecord(raw) ? raw : {}
  const w = isRecord(m.window) ? m.window : null
  const window: NfMetaWindow | null =
    w && str(w.from) && str(w.to)
      ? {
          from: w.from as string,
          to: w.to as string,
          mode: w.mode === 'relative' ? 'relative' : 'fixed',
          tz: str(w.tz) ?? 'UTC',
          effective_from: str(w.effective_from),
          effective_to: str(w.effective_to),
          source: (str(w.source) as NfMetaWindow['source']) ?? null,
          granularity_s: num(w.granularity_s),
        }
      : null
  const r = isRecord(m.retention) ? m.retention : null
  const t = isRecord(m.truncated) ? m.truncated : null
  const s = isRecord(m.sampling) ? m.sampling : null
  const warnings: NfWarning[] = Array.isArray(m.warnings)
    ? m.warnings
        .filter(isRecord)
        .map((x) => ({ code: str(x.code) ?? 'warning', message: str(x.message) ?? '' }))
        .filter((x) => x.message)
    : []
  return {
    window,
    server_time: str(m.server_time),
    query_ms: num(m.query_ms),
    restricted: m.restricted === true,
    filters: isRecord(m.filters) ? m.filters : {},
    retention: r
      ? { raw_days: num(r.raw_days), rollup_days: num(r.rollup_days), interface_days: num(r.interface_days), limited: r.limited === true }
      : null,
    truncated: t && num(t.limit) !== null && num(t.returned) !== null
      ? { limit: num(t.limit) as number, returned: num(t.returned) as number, more: t.more === true }
      : null,
    sampling: s ? { sampled_share: num(s.sampled_share), max_interval: num(s.max_interval) } : null,
    warnings,
  }
}

/** `{data, meta}` from an enveloped body; a legacy body becomes `{data: body, meta: null}`. */
export function unwrapEnvelope<T>(body: unknown): NfResult<T> {
  if (isRecord(body) && 'data' in body && isRecord(body.meta)) {
    return { data: body.data as T, meta: normalizeMeta(body.meta) }
  }
  return { data: body as T, meta: null }
}

export interface NfRequestOptions {
  signal?: AbortSignal
  /** Client timeout; defaults a little above the API's 30 s forensics budget. */
  timeoutMs?: number
}

const DEFAULT_TIMEOUT_MS = 35_000

export const nf = {
  /** GET a NetFlow read endpoint (path from the API root, e.g. `/netflow/overview`) with `envelope=1`. */
  async get<T>(path: string, params?: NfParams | null, opts: NfRequestOptions = {}): Promise<NfResult<T>> {
    const sp = toSearchParams(params)
    sp.set('envelope', '1')
    const res = await api.get(path, {
      params: sp,
      signal: opts.signal,
      timeout: opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    })
    return unwrapEnvelope<T>(res.data)
  },
}

/** Query-key factory. Everything NetFlow lives under `['netflow', …]` for bulk invalidation. */
export const nfKeys = {
  all: ['netflow'] as const,
  query: (path: string, params?: NfParams | null) => ['netflow', path, paramsKey(params)] as const,
  coverage: (params?: NfParams | null) => nfKeys.query('/netflow/coverage', params),
  retention: () => nfKeys.query('/netflow/retention'),
  exporterStatus: () => nfKeys.query('/netflow/exporters/status'),
  findings: (params?: NfParams | null) => nfKeys.query('/netflow/findings', params),
  /** Saved-view list (the key the existing Saved Views page already uses). */
  savedViews: () => ['netflow', 'saved-views'] as const,
}

export interface UseNfQueryOptions {
  /** The page window: decides the poll cadence (fixed windows never poll). */
  window?: NfWindow | null
  enabled?: boolean
  /** Overrides the window-derived cadence (e.g. `false` for an expensive panel, 30 s for status). */
  refetchInterval?: number | false
  /** Keep the previous result as a placeholder while a new key loads (default true). */
  keepPrevious?: boolean
  staleTime?: number
  timeoutMs?: number
  queryKey?: QueryKey
}

/** Retry once for network/5xx failures; never for 4xx or query-budget timeouts. */
export function nfRetry(failureCount: number, error: unknown): boolean {
  return failureCount < 1 && isRetryableError(error)
}

export function useNfQuery<T>(
  path: string,
  params?: NfParams | null,
  opts: UseNfQueryOptions = {},
): UseQueryResult<NfResult<T>> {
  return useQuery<NfResult<T>>({
    queryKey: opts.queryKey ?? nfKeys.query(path, params),
    queryFn: ({ signal }) => nf.get<T>(path, params, { signal, timeoutMs: opts.timeoutMs }),
    // A calendar window still waiting for the appliance clock would query the wrong period.
    enabled: (opts.enabled ?? true) && !opts.window?.pending && !isPendingParams(params),
    placeholderData: opts.keepPrevious === false ? undefined : keepPreviousData,
    refetchInterval: opts.refetchInterval !== undefined ? opts.refetchInterval : pollIntervalFor(opts.window),
    staleTime: opts.staleTime,
    retry: nfRetry,
  })
}

/**
 * Collection coverage for a window (`/netflow/coverage`, contract §11), scoped to the filter's
 * exporter when it has one. Coverage is about what exporters delivered, so traffic filters do
 * not apply to it.
 */
export function useNfCoverage(w: NfWindow | null | undefined, filter?: FlowFilter | null, enabled = true) {
  const params = nfParams(w, filter?.exporter ? { exporter: filter.exporter } : null)
  return useNfQuery<NfCoverage>('/netflow/coverage', params, {
    window: w,
    enabled: enabled && !!w,
    queryKey: nfKeys.coverage(params),
  })
}
