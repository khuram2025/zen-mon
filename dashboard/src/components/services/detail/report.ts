/**
 * Shareable report for the selected window (feature: shareable reports).
 * Uses GET /service-checks/{id}/report when the appliance provides it; an
 * older appliance gets a browser-generated summary that says so.
 */
import { api } from '@/lib/api'
import { serverNow } from '@/lib/serverClock'
import { knownUnsupported, markUnsupported } from './capabilities'
import { browserTimezone, isUnsupported } from './format'
import type { MaintenanceWindow, Outage, ServiceCheckExt, SlaStats } from './types'

function save(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function filenameFrom(disposition: string | undefined, fallback: string): string {
  const m = disposition && /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition)
  return m ? decodeURIComponent(m[1]) : fallback
}

function slug(name: string) {
  return name.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '').toLowerCase() || 'service'
}

const csvCell = (v: unknown) => {
  const s = v == null ? '' : String(v)
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** Returns 'server' or 'browser' so the caller can tell the user which one they got. */
export async function downloadServiceReport(args: {
  check: ServiceCheckExt
  format: 'csv' | 'json'
  fromISO: string
  toISO: string
  sla?: SlaStats
  outages: Outage[]
  maintenance: MaintenanceWindow[]
}): Promise<'server' | 'browser'> {
  const { check, format, fromISO, toISO } = args
  const timezone = browserTimezone()
  const base = `${slug(check.name)}-${fromISO.slice(0, 10)}-to-${toISO.slice(0, 10)}`
  if (!knownUnsupported('report')) {
    try {
      const r = await api.get(`/service-checks/${check.id}/report`, {
        params: { from: fromISO, to: toISO, format, timezone },
        responseType: 'blob',
      })
      save(r.data as Blob, filenameFrom(r.headers?.['content-disposition'], `${base}.${format}`))
      return 'server'
    } catch (e) {
      if (!isUnsupported(e)) throw e
      markUnsupported('report')
    }
  }

  const sla = args.sla
  const summary = {
    generated_by: 'browser (this appliance has no report endpoint)',
    generated_at: new Date(serverNow()).toISOString(),
    timezone,
    basis: 'confirmed',
    check: { id: check.id, name: check.name, type: check.check_type, target: check.target_url || check.target_host, owner_team: check.owner_team ?? null },
    window: { from: fromISO, to: toISO },
    availability_pct: sla?.uptime_pct ?? null,
    probe_success_pct: sla?.probe_uptime_pct ?? null,
    coverage_pct: sla?.coverage_pct ?? null,
    downtime_sec: sla?.total_downtime_sec ?? null,
    unknown_sec: sla?.unknown_sec ?? null,
    maintenance_sec: sla?.maintenance_sec ?? null,
    incidents: args.outages.map((o) => ({
      kind: o.kind, start: new Date(o.start).toISOString(), end: o.end == null ? null : new Date(o.end).toISOString(),
      starts_before_window: o.clippedStart, continues_after_window: !!o.clippedEnd, reason: o.reason ?? null,
    })),
    maintenance_windows: args.maintenance.map((w) => ({ scope: w.scope_type, label: w.scope_label ?? null, starts_at: w.starts_at, ends_at: w.effective_ends_at || w.ends_at, reason: w.reason ?? null })),
  }
  if (format === 'json') {
    save(new Blob([JSON.stringify(summary, null, 2)], { type: 'application/json' }), `${base}.json`)
  } else {
    const lines: string[] = []
    lines.push('# Summary', 'field,value')
    for (const [k, v] of Object.entries(summary)) {
      if (Array.isArray(v) || (v && typeof v === 'object')) continue
      lines.push(`${csvCell(k)},${csvCell(v)}`)
    }
    lines.push(`check,${csvCell(check.name)}`, `window_from,${fromISO}`, `window_to,${toISO}`, '')
    lines.push('# Incidents', 'kind,start,end,starts_before_window,continues_after_window,reason')
    for (const i of summary.incidents) lines.push([i.kind, i.start, i.end, i.starts_before_window, i.continues_after_window, i.reason].map(csvCell).join(','))
    lines.push('', '# Maintenance windows', 'scope,label,starts_at,ends_at,reason')
    for (const w of summary.maintenance_windows) lines.push([w.scope, w.label, w.starts_at, w.ends_at, w.reason].map(csvCell).join(','))
    save(new Blob([lines.join('\n')], { type: 'text/csv' }), `${base}.csv`)
  }
  return 'browser'
}
