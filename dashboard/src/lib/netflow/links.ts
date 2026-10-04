/**
 * Links between NetFlow pages that keep the investigation context (NF-08, NF-09, FE-02): the
 * whole typed filter in canonical keys, the time window (a preset key, or explicit bounds for a
 * frozen/custom window) and any page-specific extras. Dropping any of these made drill-downs
 * return different flows from the item that was clicked.
 */
import { writeFlowFilter, type FlowFilter, type SavedQueryV2, type SavedQueryWindow } from './filters'
import { displayTimeZone, windowSearchParams, type NfWindow } from './window'

export type LinkExtra = Record<string, string | number | boolean | null | undefined>

export interface NetflowLinkOptions {
  filter?: FlowFilter | null
  /** Page window, or a saved-query window. Freeze it first (`freezeWindow`) for evidence links. */
  window?: NfWindow | SavedQueryWindow | null
  /** Extra parameters (`sort`, `finding`, `run`, …); empty values and `false` are omitted. */
  extra?: LinkExtra | null
}

/**
 * `path?query` for a NetFlow page. Parameters already on `path` are kept unless the filter,
 * window or extras set them.
 */
export function buildNetflowLink(path: string, opts: NetflowLinkOptions = {}): string {
  const q = path.indexOf('?')
  const base = q >= 0 ? path.slice(0, q) : path
  let sp = new URLSearchParams(q >= 0 ? path.slice(q + 1) : '')
  if (opts.window) {
    sp.delete('from')
    sp.delete('to')
    for (const [k, v] of Object.entries(windowSearchParams(opts.window))) sp.set(k, v)
    // Hour/day filters are evaluated in the window's zone; carry it when it is not the viewer default.
    if ('tz' in opts.window && opts.window.tz !== displayTimeZone() && !opts.filter?.tz) sp.set('tz', opts.window.tz)
  }
  if (opts.filter) sp = writeFlowFilter(sp, opts.filter)
  for (const [k, v] of Object.entries(opts.extra ?? {})) {
    if (v === undefined || v === null || v === '' || v === false) continue
    sp.set(k, v === true ? '1' : String(v))
  }
  const qs = sp.toString()
  return qs ? `${base}?${qs}` : base
}

const SAVED_KIND_PATH: Record<SavedQueryV2['kind'], string> = {
  forensics: '/netflow/forensics',
  overview: '/netflow',
  section: '/netflow',
}

/**
 * The link that re-runs a saved query exactly: filter, window, sort, order, columns and the page
 * settings (`by`, `side`, …). Forensics views carry `run=1` so a query with only a port or a
 * DSCP 0 condition still runs on open.
 */
export function savedQueryLink(query: SavedQueryV2, path?: string): string {
  return buildNetflowLink(path ?? SAVED_KIND_PATH[query.kind], {
    filter: query.filter,
    window: query.window,
    extra: {
      ...(query.params ?? {}),
      sort: query.sort,
      order: query.order,
      columns: query.columns?.length ? query.columns.join(',') : undefined,
      run: query.kind === 'forensics',
    },
  })
}

/** Exporter deep-dive page. */
export function exporterPath(exporter: string): string {
  return `/netflow/devices/${encodeURIComponent(exporter)}`
}

/** Host profile page. */
export function hostPath(ip: string): string {
  return `/netflow/hosts/${encodeURIComponent(ip)}`
}
