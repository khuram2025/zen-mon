/**
 * One shared assessment model for the Availability page (A08).
 *
 * Every headline card, the executive brief, the chips and the top-risk list
 * are derived here from the same inputs, so the narrative can never disagree
 * with the numbers beside it. Current state ("now") and period performance
 * ("during the selected period") are always stated separately, and missing
 * evidence is reported as such — never as zero or as success.
 */
import type { BusinessData, ComplianceState, ExecutiveData } from '@/hooks/useReports'
import type { ServerMonitoringOverview } from '@/types/servers'
import {
  fmtAge, fmtAvail, fmtDuration, fmtPp, fmtShare, fmtTarget, parseTs, plural,
} from './format'

export type Tone = 'met' | 'risk' | 'breach' | 'unknown' | 'neutral' | 'maint'
export type EvidenceState = 'loading' | 'error' | 'not_measured' | 'stale' | 'measured'
export type FleetDomain = 'devices' | 'services' | 'servers'

/** Fallback when neither the report nor the policy endpoint states a target. */
export const FALLBACK_TARGET_PCT = 99.9
/** A last observation older than this is stale for a live estate. */
export const STALE_OBSERVATION_MS = 20 * 60_000

export interface ReportState {
  loading: boolean
  error: boolean
  /** Showing the previous period's data while the new one loads. */
  placeholder: boolean
  /** Cached data kept after a failed refresh. */
  refreshFailed: boolean
}

export interface DeviceSummary {
  total: number
  up: number
  down: number
  degraded: number
  unknown: number
  maintenance: number
}

export interface ServiceSummary {
  total: number
  up: number
  down: number
  warning: number
  degraded: number
  unknown: number
}

export interface DeviceLite {
  id: string
  hostname: string
  ip_address?: string
  location?: string | null
  device_type?: string
  status: string
  last_seen?: string | null
  status_changed_at?: string | null
}

export interface Verdict { tone: Tone; text: string }

export interface DecisionMetric {
  key: string
  label: string
  value: string
  /** Colour of the value itself; neutral by default. */
  valueTone?: Tone
  verdict?: Verdict | null
  lines: string[]
  comparison?: { text: string; tone: Tone } | null
  to?: string
  /** Definition shown on hover / to screen readers. */
  definition?: string
}

export interface Brief {
  tone: Tone
  headline: string
  sentences: string[]
  evidence: string
  action?: { label: string; to: string }
}

export interface RiskItem {
  key: string
  tone: Tone
  status: string
  title: string
  to: string
  reason: string
  context: string
}

export interface Assessment {
  domain: FleetDomain
  state: EvidenceState
  metrics: DecisionMetric[]
  brief: Brief
  risks: RiskItem[]
  /** Short statement of how the headline is calculated (shown + exported). */
  basis: string
  target: number | null
  targetLabel: string
  coverageText: string
}

/* ── Services: normalised rows ─────────────────────────────────────────── */

export type Criticality = 'critical' | 'high' | 'medium' | 'low'

export const CRITICALITY_WEIGHT: Record<string, number> = {
  critical: 4, high: 3, medium: 2, low: 1,
}

export interface ServiceRow {
  id: string
  name: string
  type: string
  group: string | null
  status: string
  availability: number | null
  probeAvailability: number | null
  coverage: number | null
  downtimeMin: number | null
  outages: number | null
  failedChecks: number
  totalChecks: number
  target: number
  targetSource: 'service' | 'policy' | 'default'
  compliance: ComplianceState
  errorBudgetUsed: number | null
  criticality: Criticality | null
  owner: string | null
  mapped: boolean
}

const UNGROUPED = new Set(['', 'ungrouped', 'unassigned', 'none'])

export function isConfirmedBasis(business?: BusinessData): boolean {
  if (!business) return false
  if (business.availability_basis) return business.availability_basis === 'confirmed'
  if (business.compliance_summary != null) return true
  return business.service_availability.some((row) => row.probe_availability_pct !== undefined)
}

export function normalizeServices(business: BusinessData | undefined, policyTarget: number | null): ServiceRow[] {
  if (!business) return []
  return business.service_availability.map((row) => {
    const target = typeof row.target_pct === 'number'
      ? row.target_pct
      : policyTarget ?? FALLBACK_TARGET_PCT
    const availability = row.availability_pct ?? null
    const derived: ComplianceState = availability == null
      ? 'not_assessed'
      : availability < target ? 'breached' : 'met'
    const group = row.group_name && !UNGROUPED.has(row.group_name.trim().toLowerCase())
      ? row.group_name
      : null
    const criticality = row.criticality && row.criticality in CRITICALITY_WEIGHT
      ? row.criticality
      : null
    return {
      id: row.service_check_id,
      name: row.name,
      type: row.type,
      group,
      status: row.status,
      availability,
      probeAvailability: row.probe_availability_pct ?? null,
      coverage: row.coverage_pct ?? null,
      downtimeMin: row.downtime_seconds != null ? row.downtime_seconds / 60 : null,
      outages: row.outages ?? null,
      failedChecks: row.checks_failed ?? 0,
      totalChecks: row.checks_total ?? 0,
      target,
      targetSource: row.target_source ?? (policyTarget != null ? 'policy' : 'default'),
      compliance: row.compliance ?? derived,
      errorBudgetUsed: row.error_budget_used_pct ?? null,
      criticality,
      owner: row.business_owner?.trim() || null,
      mapped: row.grouped != null ? Boolean(row.grouped && criticality) : Boolean(group && criticality),
    }
  })
}

export interface ComplianceCounts {
  met: number
  at_risk: number
  breached: number
  not_assessed: number
  eligible: number
}

export function complianceCounts(rows: ServiceRow[], business?: BusinessData): ComplianceCounts {
  if (business?.compliance_summary) return business.compliance_summary
  const counts = { met: 0, at_risk: 0, breached: 0, not_assessed: 0, eligible: rows.length }
  for (const row of rows) counts[row.compliance] += 1
  return counts
}

/** Exceptions first (breached, at risk), most important and worst first. */
export function rankServices(rows: ServiceRow[]): ServiceRow[] {
  const order: Record<ComplianceState, number> = { breached: 0, at_risk: 1, not_assessed: 2, met: 3 }
  return [...rows].sort((a, b) =>
    order[a.compliance] - order[b.compliance]
    || (CRITICALITY_WEIGHT[b.criticality ?? ''] ?? 0) - (CRITICALITY_WEIGHT[a.criticality ?? ''] ?? 0)
    || ((a.availability ?? 101) - a.target) - ((b.availability ?? 101) - b.target)
    || a.name.localeCompare(b.name))
}

/* ── Shared helpers ────────────────────────────────────────────────────── */

export function evidenceState(report: ReportState, hasData: boolean, measured: boolean): EvidenceState {
  if (!hasData) return report.error ? 'error' : 'loading'
  if (report.placeholder || report.refreshFailed) return 'stale'
  return measured ? 'measured' : 'not_measured'
}

function comparisonOf(pp: number | null | undefined): DecisionMetric['comparison'] {
  const text = fmtPp(pp)
  if (text == null) return { text: 'Comparison unavailable', tone: 'unknown' }
  const tone: Tone = (pp ?? 0) > 0.005 ? 'met' : (pp ?? 0) < -0.005 ? 'breach' : 'neutral'
  return { text: `${text} vs previous period`, tone }
}

function statusWord(v: boolean | null): Verdict {
  if (v == null) return { tone: 'unknown', text: 'Not measured — no target verdict' }
  return v ? { tone: 'met', text: 'Target met' } : { tone: 'breach', text: 'Below target' }
}

/* ── Devices ───────────────────────────────────────────────────────────── */

export interface DeviceInput {
  exec?: ExecutiveData
  report: ReportState
  live?: DeviceSummary
  downNow: DeviceLite[]
  policyTarget: number | null
  /** Mid-sentence period phrase, e.g. "the last 7 days". */
  period: string
  /** Appliance-aligned "now" for freshness checks. */
  nowMs: number
}

export function assessDevices(input: DeviceInput): Assessment {
  const { exec, report, live, downNow, policyTarget, period, nowMs } = input
  const k = exec?.kpis
  const legacy = !!k && k.outage_episodes === undefined
  const target = k?.configured_target_pct ?? k?.sla_target_pct ?? policyTarget ?? FALLBACK_TARGET_PCT
  const targetSource = k?.target_source ?? (k?.configured_target_pct != null ? 'policy' : 'default')
  const avail = k?.availability_pct ?? null
  const measuredDevices = k?.devices_measured ?? null
  const expectedDevices = k?.devices_expected ?? exec?.coverage?.expected_entities ?? k?.devices_monitored ?? live?.total ?? null
  const measured = avail != null && measuredDevices !== 0
  const verdict: boolean | null = measured ? (k?.sla_met ?? (avail as number) >= target) : null
  const state = evidenceState(report, !!exec, measured)

  const cov = exec?.coverage
  const coveredPct = cov?.covered_pct ?? legacyCoveredPct(exec)
  const lastObs = parseTs(cov?.last_observation ?? cov?.to ?? null)
  const obsStale = lastObs != null && nowMs - lastObs > STALE_OBSERVATION_MS
  const measuredEntities = cov?.measured_entities ?? measuredDevices
  const partialEntities = measuredEntities != null && expectedDevices != null && measuredEntities < expectedDevices

  const episodes = legacy ? null : (k?.outage_episodes ?? k?.incidents_count ?? null)
  const ongoing = legacy ? null : (k?.ongoing_outages ?? 0)
  const downtime = legacy ? null : (k?.outage_downtime_minutes ?? null)
  const criticalNow = k?.active_critical_now ?? (legacy ? null : k?.active_critical_count ?? null)
  const liveDown = live?.down ?? downNow.filter((d) => d.status === 'down').length
  const liveDegraded = live?.degraded ?? downNow.filter((d) => d.status === 'degraded').length

  const coverageText = exec
    ? [
        measuredEntities != null && expectedDevices != null
          ? `${measuredEntities} of ${plural(expectedDevices, 'device')} measured`
          : null,
        coveredPct != null ? `${fmtShare(coveredPct)} of the period between first and last sample` : 'period coverage not reported',
        lastObs != null ? `last observation ${fmtAge(lastObs, nowMs)}` : 'last observation not reported',
      ].filter(Boolean).join(' · ')
    : 'Evidence not loaded'

  const metrics: DecisionMetric[] = [
    {
      key: 'availability',
      label: 'Device availability',
      value: fmtAvail(avail),
      valueTone: verdict == null ? 'unknown' : 'neutral',
      verdict: statusWord(verdict),
      lines: [
        `Configured target ${fmtTarget(target)}${targetSource === 'default' ? ' (default)' : ''}`,
        'Ping reachability · maintenance excluded',
      ],
      // No comparison can be claimed when the current period itself is unmeasured.
      comparison: comparisonOf(k && measured ? (k.availability_delta_pp !== undefined ? k.availability_delta_pp : k.availability_delta_pct) : null),
      to: '/devices',
      definition: 'Share of ping samples that succeeded during the selected period, across devices currently in inventory. Planned maintenance is excluded.',
    },
    legacy
      ? {
          key: 'outages',
          label: 'Alerts raised',
          value: fmtCountOrDash(k?.incidents_count),
          valueTone: 'neutral',
          verdict: { tone: 'unknown', text: 'Outage episodes not reported' },
          lines: [
            'This appliance version counts alert records, not confirmed outages.',
            `Mean alert lifetime ${fmtDuration(k?.mttr_minutes, 'not available')}`,
          ],
          to: '/alerts',
        }
      : {
          key: 'outages',
          label: 'Confirmed outages',
          value: fmtCountOrDash(episodes),
          valueTone: 'neutral',
          verdict: !measured && !episodes
            ? { tone: 'unknown', text: 'No evidence for the period' }
            : (ongoing ?? 0) > 0
              ? { tone: 'breach', text: `${ongoing} ongoing` }
              : (episodes ?? 0) > 0
                ? { tone: 'risk', text: 'All recovered' }
                : { tone: 'met', text: 'None confirmed' },
          lines: [
            `Downtime ${fmtDuration(downtime, 'not reported')} in period (device-minutes)`,
            k?.mttr_minutes != null
              ? `Mean time to recover ${fmtDuration(k.mttr_minutes)}`
              : 'Mean time to recover: no completed outages',
          ],
          to: '/devices?status=down',
          definition: 'Distinct down-to-recovery episodes that intersect the period. Repeated alerts for one outage count once.',
        },
    {
      key: 'critical',
      label: 'Active critical alerts now',
      value: fmtCountOrDash(criticalNow),
      valueTone: 'neutral',
      verdict: criticalNow == null
        ? { tone: 'unknown', text: 'Not reported by this appliance version' }
        : criticalNow > 0
          ? { tone: 'breach', text: 'Needs action' }
          : { tone: 'met', text: 'None active' },
      lines: [
        k?.critical_raised_in_period != null
          ? `${plural(k.critical_raised_in_period, 'critical alert')} raised in ${period}`
          : legacy
            ? `Legacy count: ${fmtCountOrDash(k?.active_critical_count)} active among alerts raised in ${period}`
            : 'Current state, independent of the selected period',
      ],
      to: '/alerts?severity=critical',
      definition: 'Active (not yet acknowledged or resolved) critical alerts at this moment, regardless of when they were raised.',
    },
    {
      key: 'coverage',
      label: 'Evidence coverage',
      value: measuredEntities != null && expectedDevices != null
        ? `${measuredEntities} / ${expectedDevices}`
        : coveredPct != null ? fmtShare(coveredPct) : 'Not reported',
      valueTone: 'neutral',
      verdict: !exec
        ? null
        : !measured
          ? { tone: 'unknown', text: 'No evidence' }
          : obsStale
            ? { tone: 'risk', text: 'Stale collection' }
            : partialEntities || (coveredPct != null && coveredPct < 90)
              ? { tone: 'risk', text: 'Partial' }
              : { tone: 'neutral', text: 'Complete' },
      lines: [
        measuredEntities != null ? 'Devices with at least one sample in the period' : 'Measured-device count not reported',
        coveredPct != null ? `Period span covered ${fmtShare(coveredPct)}` : 'Period coverage not reported',
        lastObs != null ? `Last observation ${fmtAge(lastObs, nowMs)}` : 'Last observation not reported',
      ],
      definition: 'Covered time is the share of the period between the first and last sample; it does not prove continuous collection.',
    },
  ]

  /* Brief — current state, then period, then evidence. */
  const sentences: string[] = []
  if (live && live.total > 0) {
    const active = live.total - live.maintenance
    if (liveDown === 0 && liveDegraded === 0) {
      sentences.push(`All ${plural(active, 'monitored device')} ${active === 1 ? 'is' : 'are'} currently up${live.unknown ? ` or unknown (${live.unknown} unknown)` : ''}${live.maintenance ? `; ${live.maintenance} in planned maintenance` : ''}.`)
    } else {
      sentences.push(`${liveDown} of ${plural(live.total, 'device')} ${liveDown === 1 ? 'is' : 'are'} down now${liveDegraded ? ` and ${liveDegraded} degraded` : ''}.`)
    }
  }
  if (exec) {
    if (!measured) {
      sentences.push(`Availability for ${period} is not measured: no ping evidence was recorded, so no target verdict is given.`)
    } else {
      sentences.push(`During ${period}, device availability was ${fmtAvail(avail)} against a configured target of ${fmtTarget(target)} (${verdict ? 'met' : 'below target'}).`)
      if (!legacy && episodes != null) {
        sentences.push(episodes === 0
          ? 'No confirmed outages were recorded in the period.'
          : `${plural(episodes, 'confirmed outage')}${ongoing ? ` (${ongoing} ongoing)` : ''} totalling ${fmtDuration(downtime, 'an unknown duration')} of device downtime.`)
      }
    }
  }

  let tone: Tone = 'neutral'
  let headline = 'Device availability'
  if (!exec) {
    tone = 'unknown'
    headline = report.error ? 'Device availability could not be loaded' : 'Loading device availability'
  } else if (liveDown > 0 || (ongoing ?? 0) > 0) {
    tone = 'breach'
    headline = liveDown > 0 ? `${plural(liveDown, 'device')} ${liveDown === 1 ? 'is' : 'are'} down now` : 'Device outages are ongoing'
  } else if (!measured) {
    tone = 'unknown'
    headline = 'Device availability is not measured for this period'
  } else if (verdict === false) {
    tone = 'breach'
    headline = 'Device availability is below the configured target'
  } else if ((episodes ?? 0) > 0 || obsStale || partialEntities) {
    tone = 'risk'
    headline = obsStale ? 'Target met, but collection looks stale' : 'Target met with recovered outages'
  } else {
    tone = 'met'
    headline = 'Device availability met the configured target'
  }

  const risks: RiskItem[] = []
  for (const d of downNow.slice(0, 3)) {
    risks.push({
      key: `down-${d.id}`,
      tone: d.status === 'down' ? 'breach' : 'risk',
      status: d.status === 'down' ? 'Down now' : 'Degraded now',
      title: d.hostname,
      to: `/devices/${d.id}`,
      reason: d.status_changed_at ? `Since ${fmtAge(parseTs(d.status_changed_at), nowMs).replace(' ago', '')}` : 'Current state',
      context: [d.location || 'No location', d.ip_address].filter(Boolean).join(' · '),
    })
  }
  const exposures = (exec?.top_issues || [])
    .filter((issue) => (issue.category ?? (issue.issue === 'noisy' ? 'alert_noise' : 'outage')) !== 'alert_noise')
    .sort((a, b) => (b.downtime_minutes ?? b.duration_minutes ?? 0) - (a.downtime_minutes ?? a.duration_minutes ?? 0))
  for (const issue of exposures) {
    if (risks.length >= 3) break
    if (risks.some((r) => r.key === `down-${issue.device_id}`)) continue
    risks.push({
      key: `issue-${issue.device_id}-${issue.issue}`,
      tone: issue.category === 'degraded' ? 'risk' : 'breach',
      status: issue.category === 'degraded' ? 'Degraded' : 'Outage in period',
      title: issue.hostname,
      to: `/devices/${issue.device_id}`,
      reason: `${issue.outages != null ? `${plural(issue.outages, 'outage')} · ` : ''}${fmtDuration(issue.downtime_minutes ?? issue.duration_minutes)} downtime`,
      context: issue.issue,
    })
  }

  return {
    domain: 'devices',
    state,
    metrics,
    brief: {
      tone,
      headline,
      sentences,
      evidence: `Evidence: ${coverageText}.`,
      action: risks[0]
        ? { label: `Review ${risks[0].title}`, to: risks[0].to }
        : { label: 'Open device estate', to: '/devices' },
    },
    risks,
    basis: 'Device ping availability over the selected period; planned maintenance excluded; current inventory only.',
    target,
    targetLabel: `${fmtTarget(target)} (${targetSource})`,
    coverageText,
  }
}

function legacyCoveredPct(exec?: ExecutiveData): number | null {
  const first = parseTs(exec?.coverage?.from ?? null)
  const last = parseTs(exec?.coverage?.to ?? null)
  const from = parseTs(exec?.from ?? null)
  const to = parseTs(exec?.to ?? null)
  if (first == null || last == null || from == null || to == null || to <= from) return null
  return Math.max(0, Math.min(100, ((last - first) / (to - from)) * 100))
}

function fmtCountOrDash(v: number | null | undefined): string {
  return v == null ? '—' : v.toLocaleString()
}

/* ── Services ──────────────────────────────────────────────────────────── */

export interface ServiceInput {
  business?: BusinessData
  report: ReportState
  live?: ServiceSummary
  rows: ServiceRow[]
  counts: ComplianceCounts
  policyTarget: number | null
  period: string
  nowMs: number
}

export function assessServices(input: ServiceInput): Assessment {
  const { business, report, live, rows, counts, policyTarget, period } = input
  const confirmedBasis = isConfirmedBasis(business)
  const measuredRows = rows.filter((r) => r.availability != null)
  const measured = measuredRows.length > 0
  const state = evidenceState(report, !!business, measured)
  const total = live?.total ?? rows.length
  const downNowRows = rows.filter((r) => r.status === 'down')
  const downNow = live?.down ?? downNowRows.length
  const criticalDown = downNowRows.filter((r) => (CRITICALITY_WEIGHT[r.criticality ?? ''] ?? 0) >= 3).length
  const outages = business?.service_outages || []
  const ongoing = outages.filter((o) => o.ongoing).length
  const measuredEligible = counts.eligible - counts.not_assessed
  const mapped = rows.filter((r) => r.mapped).length
  const impactAssessed = business?.impact_assessed_services ?? mapped
  const impactUnassessed = business?.impact_unassessed_services ?? Math.max(0, rows.length - mapped)
  const covVals = measuredRows.map((r) => r.coverage).filter((v): v is number => v != null)
  const avgCoverage = covVals.length ? covVals.reduce((s, v) => s + v, 0) / covVals.length : null
  const tls = business?.tls_evidence
  const targets = new Set(rows.map((r) => r.target))
  const defaultTarget = policyTarget ?? FALLBACK_TARGET_PCT
  const targetLabel = targets.size > 1
    ? `per service (default ${fmtTarget(defaultTarget)})`
    : `${fmtTarget(rows[0]?.target ?? defaultTarget)}${rows[0]?.targetSource === 'service' ? '' : ' (default)'}`

  const coverageText = business
    ? [
        `${measuredRows.length} of ${plural(rows.length, 'service')} measured`,
        avgCoverage != null ? `average probe coverage ${fmtShare(avgCoverage)}` : null,
        `${impactAssessed} of ${rows.length} mapped to a business group and criticality`,
        tls ? `${tls.with_expiry_data} of ${plural(tls.eligible, 'TLS check')} with certificate expiry data` : null,
      ].filter(Boolean).join(' · ')
    : 'Evidence not loaded'

  const complianceVerdict: Verdict | null = !business
    ? null
    : measuredEligible === 0
      ? { tone: 'unknown', text: 'Not assessed' }
      : counts.breached > 0
        ? { tone: 'breach', text: `${counts.breached} breached` }
        : counts.at_risk > 0
          ? { tone: 'risk', text: `${counts.at_risk} at risk` }
          : { tone: 'met', text: 'All measured met' }

  const confirmedDowntime = business?.confirmed_downtime_minutes
  const metrics: DecisionMetric[] = [
    {
      key: 'compliance',
      label: 'Services meeting target',
      value: business ? `${counts.met} / ${measuredEligible}` : '—',
      valueTone: 'neutral',
      verdict: complianceVerdict,
      lines: [
        `${counts.at_risk} at risk · ${counts.breached} breached · ${counts.not_assessed} not assessed`,
        `Configured target ${targetLabel}`,
      ],
      to: '/services',
      definition: confirmedBasis
        ? 'Measured services whose confirmed-incident availability met their configured target. At risk: met, but more than 75% of the error budget is used.'
        : 'Legacy appliance: judged on raw probe success, not confirmed incidents.',
    },
    {
      key: 'downtime',
      label: 'Confirmed service downtime',
      value: confirmedDowntime !== undefined
        ? fmtDuration(confirmedDowntime, measured ? 'Unknown' : 'Not measured')
        : 'Not reported',
      valueTone: 'neutral',
      verdict: !business
        ? null
        : ongoing > 0
          ? { tone: 'breach', text: `${ongoing} ongoing` }
          : outages.length > 0
            ? { tone: 'risk', text: 'All recovered' }
            : measured
              ? { tone: 'met', text: 'None confirmed' }
              : { tone: 'unknown', text: 'No evidence' },
      lines: [
        `${plural(outages.length, 'outage episode')} in the period`,
        business?.customer_impact_minutes == null
          ? `Customer impact not assessed${impactUnassessed ? ` (${impactUnassessed} unmapped)` : ''}`
          : `Customer impact ${fmtDuration(business.customer_impact_minutes)} (${impactAssessed} mapped)`,
      ],
      to: '/services',
      definition: 'Elapsed time with at least one service confirmed down; overlapping outages are merged, not summed.',
    },
    {
      key: 'down-now',
      label: 'Services down now',
      value: live || business ? String(downNow) : '—',
      valueTone: 'neutral',
      verdict: !live && !business
        ? null
        : downNow > 0
          ? { tone: 'breach', text: criticalDown ? `${criticalDown} critical or high` : 'Needs action' }
          : { tone: 'met', text: 'None down' },
      lines: [
        live ? `${live.up} up · ${(live.warning ?? 0) + (live.degraded ?? 0)} warning · ${live.unknown} unknown` : 'Live state not loaded',
        'Current state, independent of the selected period',
      ],
      to: '/services',
    },
    {
      key: 'coverage',
      label: 'Evidence coverage',
      value: business ? `${measuredRows.length} / ${rows.length}` : '—',
      valueTone: 'neutral',
      verdict: !business
        ? null
        : !measured
          ? { tone: 'unknown', text: 'No evidence' }
          : measuredRows.length < rows.length || (avgCoverage != null && avgCoverage < 90) || impactUnassessed > 0
            ? { tone: 'risk', text: 'Partial' }
            : { tone: 'neutral', text: 'Complete' },
      lines: [
        avgCoverage != null ? `Average probe coverage ${fmtShare(avgCoverage)}` : 'Probe coverage not reported',
        `Business mapping ${impactAssessed} of ${rows.length}`,
      ],
    },
  ]

  const sentences: string[] = []
  if (total > 0) {
    if (downNow === 0) {
      const unknown = live?.unknown ?? 0
      sentences.push(unknown
        ? `${total - unknown} of ${plural(total, 'service')} are currently up; ${unknown} ${unknown === 1 ? 'has' : 'have'} unknown state.`
        : `All ${plural(total, 'service')} are currently up.`)
    } else {
      sentences.push(`${downNow} of ${plural(total, 'service')} ${downNow === 1 ? 'is' : 'are'} down now${criticalDown ? ` (${criticalDown} critical or high criticality)` : ''}.`)
    }
  } else if (business) {
    sentences.push('No service checks are configured.')
  }
  if (business && rows.length) {
    if (!measured) {
      sentences.push(`No service availability was measured during ${period}; no target verdict is given.`)
    } else {
      const below = counts.breached
      sentences.push(below
        ? `${below} ${below === 1 ? 'was' : 'were'} below target during ${period}${counts.at_risk ? `, and ${counts.at_risk} ${counts.at_risk === 1 ? 'is' : 'are'} at risk` : ''}.`
        : counts.at_risk
          ? `All measured services met their target during ${period}, but ${counts.at_risk} ${counts.at_risk === 1 ? 'is' : 'are'} at risk of breaching it.`
          : `All ${plural(measuredEligible, 'measured service')} met their target during ${period}.`)
      if (counts.not_assessed) sentences.push(`${plural(counts.not_assessed, 'service')} could not be assessed (no evidence).`)
    }
    sentences.push(business.customer_impact_minutes == null
      ? impactAssessed > 0 && impactUnassessed === 0
        ? 'Customer impact is not assessed: mapped services have no measurements in this period.'
        : 'Customer impact is not assessed for ungrouped or unclassified services.'
      : `Customer impact across ${plural(impactAssessed, 'mapped service')} was ${fmtDuration(business.customer_impact_minutes)} (confirmed downtime, overlaps merged)${impactUnassessed ? `; ${impactUnassessed} not assessed` : ''}.`)
    if (!confirmedBasis) sentences.push('This appliance version reports raw probe success rather than confirmed incidents.')
  }

  let tone: Tone
  let headline: string
  if (!business) {
    tone = 'unknown'
    headline = report.error ? 'Service availability could not be loaded' : 'Loading service availability'
  } else if (downNow > 0) {
    tone = 'breach'
    headline = `${plural(downNow, 'service')} ${downNow === 1 ? 'is' : 'are'} down now`
  } else if (!measured) {
    tone = 'unknown'
    headline = 'Service availability is not measured for this period'
  } else if (counts.breached > 0) {
    tone = 'breach'
    headline = `${plural(counts.breached, 'service')} missed the configured target`
  } else if (counts.at_risk > 0) {
    tone = 'risk'
    headline = `${plural(counts.at_risk, 'service')} at risk of missing target`
  } else {
    tone = 'met'
    headline = 'All measured services met their target'
  }

  const ranked = [...rows]
    .filter((r) => r.status === 'down' || r.compliance === 'breached' || r.compliance === 'at_risk')
    .sort((a, b) =>
      Number(b.status === 'down') - Number(a.status === 'down')
      || (CRITICALITY_WEIGHT[b.criticality ?? ''] ?? 0) - (CRITICALITY_WEIGHT[a.criticality ?? ''] ?? 0)
      || (b.downtimeMin ?? 0) - (a.downtimeMin ?? 0)
      || (a.availability ?? 0) - (b.availability ?? 0))
  const risks: RiskItem[] = ranked.slice(0, 3).map((r) => ({
    key: r.id,
    tone: r.status === 'down' || r.compliance === 'breached' ? 'breach' : 'risk',
    status: r.status === 'down' ? 'Down now' : r.compliance === 'breached' ? 'Target breached' : 'At risk',
    title: r.name,
    to: `/services/${r.id}`,
    reason: [
      r.availability != null ? `${fmtAvail(r.availability)} vs ${fmtTarget(r.target)}` : 'Not measured',
      r.downtimeMin != null ? `${fmtDuration(r.downtimeMin)} confirmed downtime` : null,
      r.compliance === 'at_risk' && r.errorBudgetUsed != null ? `${fmtShare(r.errorBudgetUsed)} of error budget used` : null,
    ].filter(Boolean).join(' · '),
    context: [
      r.criticality ? `${r.criticality[0].toUpperCase()}${r.criticality.slice(1)} criticality` : 'Criticality not set',
      r.owner ? `Owner ${r.owner}` : 'No owner assigned',
      r.group ?? 'Not assigned to a group',
    ].join(' · '),
  }))

  return {
    domain: 'services',
    state,
    metrics,
    brief: {
      tone,
      headline,
      sentences,
      evidence: `Evidence: ${coverageText}.`,
      action: risks[0]
        ? { label: `Review ${risks[0].title}`, to: risks[0].to }
        : { label: 'Open service operations', to: '/services' },
    },
    risks,
    basis: confirmedBasis
      ? 'Service availability from confirmed incidents (isolated failed probes are diagnostic only); maintenance excluded.'
      : 'Service availability from raw probe success (legacy appliance; not the confirmed-incident basis).',
    target: rows[0]?.target ?? defaultTarget,
    targetLabel,
    coverageText,
  }
}

/* ── Servers (current snapshot only) ───────────────────────────────────── */

export interface ServerInput {
  overview?: ServerMonitoringOverview
  loading: boolean
  error: boolean
}

export function assessServers({ overview, loading, error }: ServerInput): Assessment {
  const total = overview?.total ?? 0
  const sc = overview?.status_counts || {}
  const healthy = sc.healthy ?? 0
  const critical = sc.critical ?? 0
  const warning = sc.warning ?? 0
  const stale = sc.stale ?? 0
  const unknown = sc.unknown ?? 0
  const attention = critical + warning + stale + unknown
  const online = overview?.agent_counts?.online ?? 0
  const cpu = overview?.top_cpu?.[0]
  const mem = overview?.top_memory?.[0]
  const disk = overview?.top_disk?.[0]
  const nameOf = (i?: { display_name?: string; hostname?: string | null; server_id: string }) =>
    i ? i.display_name || i.hostname || i.server_id.slice(0, 8) : ''
  const state: EvidenceState = !overview ? (error ? 'error' : 'loading') : total === 0 ? 'not_measured' : 'measured'

  const metrics: DecisionMetric[] = [
    {
      key: 'healthy',
      label: 'Healthy servers now',
      value: overview ? `${healthy} / ${total}` : '—',
      valueTone: 'neutral',
      verdict: !overview ? null : total === 0
        ? { tone: 'unknown', text: 'No servers enrolled' }
        : healthy === total
          ? { tone: 'neutral', text: 'All healthy' }
          : { tone: 'risk', text: `${total - healthy} not healthy` },
      lines: ['Current snapshot — no historical server availability is measured'],
      to: '/servers',
    },
    {
      key: 'attention',
      label: 'Servers needing attention now',
      value: overview ? String(attention) : '—',
      valueTone: 'neutral',
      verdict: !overview ? null : attention > 0 ? { tone: 'risk', text: 'Review' } : { tone: 'neutral', text: 'None' },
      lines: [`${critical} critical · ${warning} warning · ${stale} stale · ${unknown} unknown`],
      to: '/servers',
    },
    {
      key: 'agents',
      label: 'Agents reporting now',
      value: overview ? `${online} / ${total}` : '—',
      valueTone: 'neutral',
      verdict: !overview ? null : total > 0 && online < total ? { tone: 'risk', text: `${total - online} not reporting` } : { tone: 'neutral', text: 'All reporting' },
      lines: ['Telemetry coverage for the server estate'],
      to: '/server-agents',
    },
    {
      key: 'cpu',
      label: 'Peak CPU utilisation now',
      value: cpu ? `${cpu.value.toFixed(1)}%` : 'Not reported',
      valueTone: cpu ? (cpu.value >= 90 ? 'breach' : cpu.value >= 75 ? 'risk' : 'neutral') : 'unknown',
      verdict: cpu ? { tone: cpu.value >= 90 ? 'breach' : cpu.value >= 75 ? 'risk' : 'neutral', text: nameOf(cpu) } : null,
      lines: [
        `Memory peak ${mem ? `${mem.value.toFixed(1)}% (${nameOf(mem)})` : 'not reported'}`,
        `Disk peak ${disk ? `${disk.value.toFixed(1)}% (${nameOf(disk)})` : 'not reported'}`,
      ],
      to: cpu ? `/servers/${cpu.server_id}` : '/servers',
      definition: 'Highest current CPU utilisation; thresholds 75% (amber) and 90% (red). Utilisation alone is not business impact.',
    },
  ]

  const sentences: string[] = []
  if (overview) {
    if (total === 0) sentences.push('No server agents or agentless workloads are enrolled.')
    else {
      sentences.push(healthy === total
        ? `All ${plural(total, 'server')} are healthy now.`
        : `${healthy} of ${plural(total, 'server')} are healthy now: ${[
          critical && `${critical} critical`, warning && `${warning} warning`,
          stale && `${stale} stale (no recent telemetry)`, unknown && `${unknown} unknown`,
        ].filter(Boolean).join(', ')}.`)
    }
    sentences.push('Server figures are a current snapshot; the selected period does not apply because no historical server availability is measured.')
  }

  const tone: Tone = !overview ? 'unknown' : total === 0 ? 'unknown' : attention > 0 ? 'risk' : 'neutral'
  const headline = !overview
    ? (error ? 'Server health could not be loaded' : 'Loading server health')
    : total === 0
      ? 'No servers enrolled'
      : critical > 0
        ? `${plural(critical, 'server')} critical now`
        : attention > 0
          ? `${plural(attention, 'server')} need attention now`
          : 'All servers healthy now'

  const risks: RiskItem[] = []
  if (critical) risks.push({ key: 'critical', tone: 'breach', status: 'Critical now', title: plural(critical, 'critical server'), to: '/servers', reason: 'Health checks report critical state', context: 'Current snapshot' })
  if (stale) risks.push({ key: 'stale', tone: 'unknown', status: 'No telemetry', title: plural(stale, 'stale server'), to: '/servers', reason: 'Agents have not reported recently — state unknown', context: 'Loss of observability, not confirmed downtime' })
  if (warning) risks.push({ key: 'warning', tone: 'risk', status: 'Warning now', title: plural(warning, 'server'), to: '/servers', reason: 'Resource or service warnings', context: 'Current snapshot' })
  if (cpu && cpu.value >= 90 && risks.length < 3) risks.push({ key: 'cpu', tone: 'risk', status: 'CPU pressure', title: nameOf(cpu), to: `/servers/${cpu.server_id}`, reason: `CPU ${cpu.value.toFixed(1)}% now`, context: 'Instantaneous peak; check sustained load' })

  return {
    domain: 'servers',
    state,
    metrics,
    brief: {
      tone,
      headline,
      sentences,
      evidence: overview ? `Evidence: ${online} of ${plural(total, 'agent')} reporting now.` : 'Evidence not loaded.',
      action: { label: 'Open server estate', to: '/servers' },
    },
    risks: risks.slice(0, 3),
    basis: 'Current server health snapshot (not a historical availability measure).',
    target: null,
    targetLabel: 'Not applicable',
    coverageText: overview ? `${online} of ${total} agents reporting` : 'Evidence not loaded',
  }
}
