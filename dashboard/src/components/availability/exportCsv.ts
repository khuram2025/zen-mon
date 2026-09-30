/**
 * CSV evidence export for the Availability page. Exports the same values the
 * page displays (already scoped by the API to the caller's visibility), with
 * the period, timezone, basis, configured target and coverage as a header so
 * the file can be read on its own.
 */
import type { BusinessData, ExecutiveData } from '@/hooks/useReports'
import type { ServerMonitoringOverview } from '@/types/servers'
import type { Assessment, FleetDomain, ServiceRow } from './model'

type Cell = string | number | boolean | null | undefined

function esc(v: Cell): string {
  if (v == null) return ''
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  // Neutralise spreadsheet formula injection from device/service names.
  const s = /^[=+\-@\t]/.test(v) ? `'${v}` : v
  return /[",\n\r']/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

function line(cells: Cell[]): string {
  return cells.map(esc).join(',')
}

const num = (v: number | null | undefined, digits = 4) => (v == null || Number.isNaN(v) ? '' : Number(v.toFixed(digits)))

export interface CsvInput {
  domain: FleetDomain
  periodLabel: string
  fromISO: string
  toISO: string
  timezone: string
  generatedAt: string | null
  assessment: Assessment
  exec?: ExecutiveData
  business?: BusinessData
  serviceRows: ServiceRow[]
  servers?: ServerMonitoringOverview
}

export function buildAvailabilityCsv(input: CsvInput): string {
  const { domain, assessment } = input
  const out: string[] = []
  out.push(line(['ZenPlus availability evidence']))
  out.push(line(['Domain', domain]))
  out.push(line(['Period', input.periodLabel]))
  out.push(line(['Period start (UTC)', input.fromISO]))
  out.push(line(['Period end (UTC)', input.toISO]))
  out.push(line(['Display timezone', input.timezone]))
  out.push(line(['Appliance generated at', input.generatedAt ?? 'not reported']))
  out.push(line(['Exported at (browser clock, UTC)', new Date().toISOString()]))
  out.push(line(['Basis', assessment.basis]))
  out.push(line(['Configured target', assessment.targetLabel]))
  out.push(line(['Evidence coverage', assessment.coverageText]))
  out.push(line(['Summary', assessment.brief.headline]))
  out.push(line(['Brief', assessment.brief.sentences.join(' ')]))
  out.push('')
  out.push(line(['Metric', 'Value', 'Status', 'Detail']))
  for (const m of assessment.metrics) out.push(line([m.label, m.value, m.verdict?.text, m.lines.join(' | ')]))
  out.push('')

  if (domain === 'devices' && input.exec) {
    const e = input.exec
    out.push(line(['Site', 'Devices', 'Down now', 'Availability % (period)']))
    for (const r of e.location_summary || []) out.push(line([r.location || 'Not assigned', r.devices, r.down, num(r.availability_pct)]))
    out.push('')
    if (e.group_summary) {
      out.push(line(['Group', 'Devices', 'Measured', 'Down now', 'Availability % (period)']))
      for (const r of e.group_summary) out.push(line([r.name || 'Not assigned', r.devices, r.measured, r.down_now, num(r.availability_pct)]))
      out.push('')
    }
    if (e.type_summary) {
      out.push(line(['Device type', 'Devices', 'Measured', 'Down now', 'Availability % (period)']))
      for (const r of e.type_summary) out.push(line([r.name || 'Not assigned', r.devices, r.measured, r.down_now, num(r.availability_pct)]))
      out.push('')
    }
    out.push(line(['Outage device', 'Started (UTC)', 'Recovered (UTC)', 'Ongoing', 'Duration minutes']))
    for (const o of e.outage_timeline || []) out.push(line([o.hostname, o.started_at, o.ended_at ?? '', o.ongoing ?? '', num(o.duration_minutes, 2)]))
    out.push('')
    out.push(line(['Trend interval start (UTC)', 'Availability % (blank = no evidence)']))
    for (const p of e.availability_trend || []) out.push(line([p.ts, num(p.availability_pct)]))
  }

  if (domain === 'services') {
    out.push(line([
      'Service', 'Type', 'Group', 'Criticality', 'Business owner', 'Target %', 'Target source',
      'Compliance', 'Availability % (basis above)', 'Probe success %', 'Coverage %',
      'Confirmed downtime minutes', 'Outages', 'Failed probes', 'Total probes', 'Status now',
    ]))
    for (const r of input.serviceRows) {
      out.push(line([
        r.name, r.type, r.group ?? 'Not assigned', r.criticality ?? '', r.owner ?? '', num(r.target, 3), r.targetSource,
        r.compliance, num(r.availability), num(r.probeAvailability), num(r.coverage, 2),
        num(r.downtimeMin, 2), r.outages ?? '', r.failedChecks, r.totalChecks, r.status,
      ]))
    }
    out.push('')
    out.push(line(['Outage service', 'Group', 'Started (UTC)', 'Recovered (UTC)', 'Ongoing', 'Duration minutes']))
    for (const o of input.business?.service_outages || []) {
      out.push(line([o.name, o.group_name ?? '', o.started_at, o.ended_at ?? '', o.ongoing ?? '', num(o.duration_minutes, 2)]))
    }
  }

  if (domain === 'servers' && input.servers) {
    out.push(line(['Server status (current snapshot)', 'Count']))
    for (const [k, v] of Object.entries(input.servers.status_counts || {})) out.push(line([k, v ?? 0]))
    out.push(line(['Total', input.servers.total]))
  }
  return `${out.join('\r\n')}\r\n`
}

export function downloadText(filename: string, text: string, type = 'text/csv;charset=utf-8') {
  const blob = new Blob(['﻿', text], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1_000)
}
