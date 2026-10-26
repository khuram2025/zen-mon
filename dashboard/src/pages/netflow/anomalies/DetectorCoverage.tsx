import { useState } from 'react'
import { ChevronDown, ChevronRight, Radar } from 'lucide-react'
import { formatDateTime } from '@/lib/netflow/format'
import { relativeTime } from '@/lib/utils'
import { FIELD_LABEL, coverageLine, detectorNotes, reducedSensitivityText, summarizeDetectors, type DetectorStatusRow, type EngineStatus } from './model'
import { DetectorStateChip, ToneChip } from './parts'

/**
 * Which checks actually ran (NF-03, NF-07, X-33): every detector with its state and reason, the
 * engine's last evaluation and how much of the window it covered. Detectors that ran on part of
 * the traffic (flags missing on some exporters, sampling) say so.
 */
export function DetectorCoverage({
  detectors,
  engine,
  tz,
  mode,
}: {
  detectors: DetectorStatusRow[]
  engine?: EngineStatus | null
  tz: string
  mode: 'stored' | 'run'
}) {
  const s = summarizeDetectors(detectors)
  const problems = s.unavailable.length + s.failed.length + s.noData.length
  const [open, setOpen] = useState(problems > 0)
  return (
    <section aria-labelledby="nf-detector-coverage" className="rounded-lg border border-border bg-surface">
      <button
        type="button"
        className="flex w-full flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="flex min-w-0 items-center gap-2">
          {open ? <ChevronDown className="h-4 w-4 shrink-0 text-text2" aria-hidden /> : <ChevronRight className="h-4 w-4 shrink-0 text-text2" aria-hidden />}
          <Radar className="h-4 w-4 shrink-0 text-primary" aria-hidden />
          <span id="nf-detector-coverage" className="text-sm font-semibold text-text">Detector coverage</span>
        </span>
        <span className="flex flex-wrap items-center gap-1.5 text-xs text-text2">
          <span>{coverageLine(detectors)}</span>
          {problems > 0 && <ToneChip tone="warning">{problems} not evaluated</ToneChip>}
          {s.partial.length > 0 && <ToneChip tone="info">{s.partial.length} partial</ToneChip>}
        </span>
      </button>
      <div className="border-t border-border px-3 py-2 text-xs text-text2">
        {mode === 'stored' && engine ? (
          engine.status === 'never' ? (
            <span>The detection engine has not evaluated any traffic yet.</span>
          ) : (
            <span>
              Engine: {engine.status === 'running' ? 'running' : 'not running'}; last evaluated the 5 minutes to{' '}
              <span className="text-text">{formatDateTime(engine.last_bucket_end, tz, { seconds: false })}</span> ({relativeTime(engine.last_run_at)})
              {typeof engine.evaluated_pct === 'number' && <> · evaluated <span className="text-text">{engine.evaluated_pct}%</span> of this window</>}
              . Status below is from that evaluation.
            </span>
          )
        ) : (
          <span>Evaluated now over raw flow records in this window, 5 minutes at a time. Not stored.</span>
        )}
      </div>
      {open && (
        <ul className="divide-y divide-border border-t border-border">
          {detectors.map((d) => (
            <DetectorCoverageRow key={d.kind} d={d} />
          ))}
        </ul>
      )}
    </section>
  )
}

/** One detector: state, reason, notes, every exporter with reduced sensitivity (X-09) and needs. */
export function DetectorCoverageRow({ d }: { d: DetectorStatusRow }) {
  const sampled = reducedSensitivityText(d.reduced_sensitivity)
  return (
    <li className="flex flex-col gap-1 px-3 py-2 min-[560px]:flex-row min-[560px]:items-start min-[560px]:gap-3">
      <div className="flex min-w-0 items-center gap-2 min-[560px]:w-64 min-[560px]:shrink-0">
        <DetectorStateChip state={d.status} />
        <span className="text-sm font-medium text-text">{d.label}</span>
      </div>
      <div className="min-w-0 flex-1 text-xs text-text2">
        {d.reason && <p className="text-text">{d.reason}{d.error_id ? ` (ref ${d.error_id})` : ''}</p>}
        {detectorNotes(d).map((n, i) => (
          <p key={i}>{n}</p>
        ))}
        {sampled && <p>{sampled}</p>}
        {!d.reason && !(d.notes ?? []).length && !d.reduced_sensitivity?.length && d.status === 'ok' && (
          <p>
            {typeof d.match_count === 'number' ? `${d.match_count.toLocaleString('en-US')} match${d.match_count === 1 ? '' : 'es'}` : 'Evaluated'}
            {d.truncated ? ` (showing ${d.returned})` : ''}
            {typeof d.coverage_pct === 'number' ? ` · fields present on ${d.coverage_pct}% of relevant records` : ''}
          </p>
        )}
        {(d.required_fields ?? []).length > 0 && (
          <p className="text-xs">Needs: {(d.required_fields ?? []).map((f) => FIELD_LABEL[f] ?? f).join(', ')}</p>
        )}
      </div>
    </li>
  )
}
