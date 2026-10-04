import type { ReactNode } from 'react'
import { AlertTriangle, Clock, Database, EyeOff, Filter, Gauge, Layers, Scissors, Timer, type LucideIcon } from 'lucide-react'
import { describeSkew, useClockOffset } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { useNfCoverage } from '@/lib/netflow/api'
import { FLOW_FILTER_LABELS, unappliedFilterKeys, type FlowFilter } from '@/lib/netflow/filters'
import { formatCount, formatDateTime, formatDuration, formatInterval, formatPercent } from '@/lib/netflow/format'
import { tcpFlagSupport } from '@/lib/netflow/status'
import type { NfCoverage, NfDataSource, NfMeta } from '@/lib/netflow/types'
import { displayTimeZone, hasEffectiveShift, windowLabel, type NfWindow } from '@/lib/netflow/window'

const SOURCE_LABELS: Record<NfDataSource, string> = {
  raw: 'raw flow records',
  rollup: 'hourly rollup',
  hybrid: 'hourly rollup with raw edges',
  interface_rollup: '5-minute interface rollup',
  pg: 'configuration database',
}

export interface DataQualityBarProps {
  /** `meta` of the panel's response (null when the endpoint returned no envelope). */
  meta: NfMeta | null | undefined
  /** Page window: labels the period when `meta` has none, and scopes the coverage query. */
  window?: NfWindow | null
  /** Page filter: its exporter scopes coverage; its keys are checked against the applied filters. */
  filter?: FlowFilter | null
  /**
   * Coverage for the window. Omit to let the bar load `/netflow/coverage` itself (shared with any
   * other bar on the page through the query cache); pass `null` to hide coverage.
   */
  coverage?: NfCoverage | null
  className?: string
}

type Tone = 'plain' | 'warn'

interface Item {
  key: string
  icon: LucideIcon
  text: ReactNode
  tone: Tone
  title?: string
}

/**
 * What a result is based on (FE-03, FE-11): period and timezone, data source and granularity,
 * retention limits, sampling, collection coverage, TCP-flag availability, visibility scope,
 * truncation, filters the panel did not apply, and API warnings. Conditions that weaken a
 * conclusion are marked; the rest is plain context.
 */
export function DataQualityBar({ meta, window: w, filter, coverage, className }: DataQualityBarProps) {
  const { offsetMs, skewed } = useClockOffset()
  const selfFetch = coverage === undefined && !!w
  const cov = useNfCoverage(w, filter, selfFetch)
  const coverageData: NfCoverage | null = coverage !== undefined ? coverage : (cov.data?.data ?? null)
  const coverageFailed = selfFetch && cov.status === 'error'

  const items: Item[] = []
  const tz = meta?.window?.tz ?? w?.tz ?? displayTimeZone()

  if (meta?.window) {
    const mw = meta.window
    const source = mw.source ? SOURCE_LABELS[mw.source] ?? mw.source : null
    const gran = mw.granularity_s && mw.granularity_s > 1 ? ` · ${formatInterval(mw.granularity_s)} buckets` : ''
    items.push({ key: 'window', icon: Clock, tone: 'plain', text: <>{windowLabel(mw)}{source ? ` · ${source}` : ''}{gran}</> })
    if (hasEffectiveShift(mw)) {
      items.push({
        key: 'effective',
        icon: AlertTriangle,
        tone: 'warn',
        text: `Data covers ${windowLabel(mw, { effective: true })}`,
        title: 'The source stores whole buckets, so the covered period differs from the one requested',
      })
    }
  } else if (w) {
    items.push({ key: 'window', icon: Clock, tone: 'plain', text: `${windowLabel(w)} (requested; the response did not report its period)` })
  }

  const ret = meta?.retention
  if (ret) {
    const parts = [
      ret.raw_days != null ? `raw ${ret.raw_days} d` : null,
      ret.rollup_days != null ? `rollups ${ret.rollup_days} d` : null,
      ret.interface_days != null ? `interface ${ret.interface_days} d` : null,
    ].filter(Boolean)
    if (ret.limited) {
      items.push({
        key: 'retention',
        icon: Database,
        tone: 'warn',
        text: 'Part of this period is older than the data source keeps; results can be incomplete',
        title: parts.length ? `Retention: ${parts.join(', ')}` : undefined,
      })
    } else if (parts.length) {
      items.push({ key: 'retention', icon: Database, tone: 'plain', text: `Kept: ${parts.join(' · ')}` })
    }
  }

  const sampling = meta?.sampling ?? null
  const sampledShare = sampling?.sampled_share ?? coverageData?.totals.sampled_share ?? null
  if (sampledShare != null) {
    const maxInterval = sampling?.max_interval ?? coverageData?.totals.max_sampling ?? null
    items.push(
      sampledShare > 0
        ? {
            key: 'sampling',
            icon: Gauge,
            tone: 'warn',
            text: `${formatPercent(sampledShare * 100)} of records sampled${maxInterval && maxInterval > 1 ? ` (up to 1:${maxInterval})` : ''}; volumes are scaled estimates`,
          }
        : { key: 'sampling', icon: Gauge, tone: 'plain', text: 'Unsampled records' },
    )
  } else if (sampling) {
    items.push({ key: 'sampling', icon: Gauge, tone: 'plain', text: 'Sampling unknown' })
  }

  if (coverageData) {
    const t = coverageData.totals
    const exporters = coverageData.exporters.length
    if (typeof t.records === 'number' && t.records === 0) {
      items.push({ key: 'coverage', icon: Layers, tone: 'warn', text: 'No records received in this period: traffic is unknown, not zero' })
    } else if (typeof t.minutes_with_data === 'number' && typeof t.minutes_in_window === 'number' && t.minutes_in_window > 0) {
      const pct = (100 * t.minutes_with_data) / t.minutes_in_window
      items.push({
        key: 'coverage',
        icon: Layers,
        tone: pct < 90 ? 'warn' : 'plain',
        text: `Records in ${formatCount(t.minutes_with_data)} of ${formatCount(t.minutes_in_window)} minutes${exporters ? ` from ${exporters} exporter${exporters === 1 ? '' : 's'}` : ''}`,
        title: pct < 90 ? 'Minutes without records are collection gaps, not idle traffic' : undefined,
      })
    }
    const flags = tcpFlagSupport(coverageData)
    if (flags.support === 'unsupported') {
      items.push({ key: 'flags', icon: AlertTriangle, tone: 'warn', text: 'TCP flags are not exported: SYN/RST results are unavailable' })
    } else if (flags.support === 'partial' && flags.pct != null) {
      items.push({ key: 'flags', icon: AlertTriangle, tone: 'warn', text: `TCP flags exported for ${formatPercent(flags.pct)} of TCP records` })
    }
    if (typeof t.legacy_records === 'number' && t.legacy_records > 0 && typeof t.records === 'number' && t.records > 0) {
      items.push({
        key: 'legacy',
        icon: Database,
        tone: 'plain',
        text: `${formatPercent((100 * t.legacy_records) / t.records)} of records predate field tracking`,
        title: 'For these records the collector did not record which fields the exporter sent',
      })
    }
  } else if (coverageFailed) {
    items.push({ key: 'coverage', icon: Layers, tone: 'warn', text: 'Collection coverage could not be loaded' })
  }

  if (meta?.restricted) {
    items.push({ key: 'restricted', icon: EyeOff, tone: 'plain', text: 'Limited to the exporters you are allowed to see' })
  }

  if (meta?.truncated?.more) {
    items.push({
      key: 'truncated',
      icon: Scissors,
      tone: 'warn',
      text: `Showing the first ${formatCount(meta.truncated.returned)}; more results exist`,
    })
  }

  if (meta && filter) {
    const unapplied = unappliedFilterKeys(filter, meta.filters)
    if (unapplied.length) {
      items.push({
        key: 'unapplied',
        icon: Filter,
        tone: 'warn',
        text: `Not applied to this panel: ${unapplied.map((k) => FLOW_FILTER_LABELS[k]).join(', ')}`,
        title: 'This panel shows the wider context for these dimensions',
      })
    }
  }

  for (const warning of meta?.warnings ?? []) {
    items.push({ key: `w-${warning.code}`, icon: AlertTriangle, tone: 'warn', text: warning.message })
  }

  if (skewed) {
    items.push({
      key: 'skew',
      icon: Clock,
      tone: 'warn',
      text: `This browser's clock is ${describeSkew(offsetMs)} the appliance; times use the appliance clock`,
    })
  }

  if (meta?.server_time) {
    const took = meta.query_ms != null ? `Query ${formatDuration(meta.query_ms)} · ` : ''
    items.push({
      key: 'asof',
      icon: Timer,
      tone: 'plain',
      text: `${took}as of ${formatDateTime(meta.server_time, tz, { zone: 'none', timeOnly: true })}`,
    })
  }

  if (!items.length) return null
  return (
    <section aria-label="Data quality" className={cn('rounded-md border border-border bg-surface2/40 px-3 py-2', className)}>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] leading-snug">
        {items.map((it) => (
          <li key={it.key} className={cn('inline-flex min-w-0 items-start gap-1', it.tone === 'warn' ? 'text-text' : 'text-text2')} title={it.title}>
            <it.icon className={cn('mt-px h-3.5 w-3.5 shrink-0', it.tone === 'warn' ? 'text-warning-fg' : 'text-text2')} aria-hidden />
            <span className="min-w-0 break-words">
              {it.tone === 'warn' && <span className="sr-only">Caution: </span>}
              {it.text}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
