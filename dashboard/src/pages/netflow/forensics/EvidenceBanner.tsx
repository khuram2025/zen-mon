import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Crosshair, Expand, Search, ShieldAlert } from 'lucide-react'
import { nf } from '@/lib/netflow/api'
import { buildNetflowLink } from '@/lib/netflow/links'
import { windowLabel, type NfWindow } from '@/lib/netflow/window'
import type { FlowFilter } from '@/lib/netflow/filters'
import { FilterChips } from '@/components/netflow/FilterChips'
import { investigateLink, type Finding } from '../anomalies/model'
import { detectorLabel, evidenceHost, evidenceState, type EvidenceState } from './model'

interface FindingLike {
  id?: string
  kind?: string | null
  title?: string | null
  description?: string | null
  severity?: string | null
  confidence?: string | null
  status?: string | null
  /** Detector display name (findings API). */
  label?: string | null
  host?: string | null
  src?: string | null
  src_ip?: string | null
  persisted?: boolean
  evidence?: unknown
}

export interface EvidenceBannerProps {
  finding: string | null
  findingId: string | null
  filter: FlowFilter
  window: NfWindow
  /** Whether the page runs a search for this URL (its filters or `run=1`); a finding id alone never does. */
  runnable: boolean
  /** Link that keeps the window but searches every flow of the host. */
  broadenLink: (host: string) => string
}

const SEVERITY_TONE: Record<string, string> = {
  critical: 'text-danger-fg',
  high: 'text-danger-fg',
  medium: 'text-warning-fg',
  low: 'text-info-fg',
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function httpStatus(err: unknown): number | null {
  const s = (err as { response?: { status?: unknown } } | null)?.response?.status
  return typeof s === 'number' ? s : null
}

const ACTION = 'inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2 py-1 font-medium hover:bg-surface2'

/**
 * Shown when Forensics was opened from a finding (NF-09): which detector, which host, the frozen
 * evidence window with its timezone and the detector's filters, so the rows below are read as the
 * evidence of that finding — plus a separate action to broaden the search to all of the host's
 * traffic in the same window.
 *
 * The banner only calls the rows "evidence" once the finding is confirmed. A link whose finding
 * cannot be loaded (unknown, deleted or outside the caller's scope) says so instead, and a finding
 * id alone never runs a search (`isRunnable`): the rows, if any, then match the link's filters only.
 */
export function EvidenceBanner({ finding, findingId, filter, window: w, runnable, broadenLink }: EvidenceBannerProps) {
  const validId = !!findingId && UUID_RE.test(findingId)
  const details = useQuery({
    queryKey: ['netflow', 'finding', findingId],
    queryFn: async ({ signal }) => (await nf.get<FindingLike>(`/netflow/findings/${encodeURIComponent(findingId as string)}`, null, { signal })).data,
    enabled: validId,
    retry: false,
    staleTime: 60_000,
  })
  const f = details.data && typeof details.data === 'object' ? details.data : null
  const state: EvidenceState = evidenceState({
    findingId,
    loading: validId && details.isPending,
    found: f !== null,
    status: details.isError ? httpStatus(details.error) : null,
  })
  const kind = f?.kind ?? finding
  const label = f?.label || detectorLabel(kind)
  const back = (
    <Link to={buildNetflowLink('/netflow/anomalies', { window: w })} className={ACTION}>
      <Crosshair className="h-3.5 w-3.5" aria-hidden />
      Back to anomalies
    </Link>
  )

  if (state === 'missing') {
    return (
      <section
        aria-label="Finding evidence"
        role="status"
        className="flex flex-col gap-2 rounded-md border border-l-4 border-warning/35 border-l-warning bg-warning/10 px-3 py-2.5 text-xs text-text"
      >
        <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-fg" aria-hidden />
          <div className="min-w-0 flex-1">
            <div className="font-semibold">Finding not found or not visible</div>
            <div className="text-text2">
              This link refers to {kind ? <>a finding of the {detectorLabel(kind)} detector</> : 'a finding'}
              {findingId ? <> (reference <span className="select-all font-mono">{findingId}</span>)</> : null} that does not
              exist, was removed, or is outside your access.{' '}
              {runnable
                ? 'The records below match the link’s filters only; they are not confirmed evidence of a finding.'
                : 'Nothing was searched. Enter conditions and press Search, or return to Anomalies.'}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2 pl-7">{back}</div>
      </section>
    )
  }

  const host = evidenceHost(filter, f?.host ?? f?.src ?? f?.src_ip)
  const severity = f?.severity?.toLowerCase() ?? null
  // Already every flow of the host: nothing to broaden.
  const narrowed = Object.entries(filter).some(([k, v]) => v !== undefined && v !== '' && !['talker', 'exporter', 'tz'].includes(k))
  const canBroaden = runnable && !!host && (narrowed || filter.talker !== host)
  // A confirmed finding opened without its filters (e.g. a bare `?finding=<id>` link): offer the
  // finding's own evidence search rather than running anything unfiltered.
  const ownEvidence = state === 'found' && !runnable && f ? investigateLink(f as unknown as Finding) : null
  return (
    <section
      aria-label="Finding evidence"
      className="flex flex-col gap-2 rounded-md border border-l-4 border-info/35 border-l-info bg-info/10 px-3 py-2.5 text-xs text-text"
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-info-fg" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="font-semibold">
            <span className="text-info-fg">Evidence</span> for {label}
            {host ? <> on <span className="font-mono">{host}</span></> : null}
            {severity && <span className={`ml-2 font-semibold ${SEVERITY_TONE[severity] ?? 'text-text2'}`}>{f?.severity}</span>}
            {f?.status && f.status !== 'open' && <span className="ml-2 font-normal text-text2">({f.status})</span>}
          </div>
          {f?.title && <div className="text-text">{f.title}</div>}
          <div className="text-text2">
            {runnable ? (
              <>Records in the finding&apos;s window, {windowLabel(w)}, matching the detector&apos;s conditions below.</>
            ) : (
              <>This link carries no search conditions, so nothing was searched.</>
            )}
            {findingId ? <> Finding reference <span className="select-all font-mono">{findingId}</span>.</> : null}
            {state === 'loading' && <> Checking the finding…</>}
            {state === 'unverified' && findingId && <> The finding could not be loaded right now, so it is not confirmed.</>}
          </div>
        </div>
      </div>
      {runnable && <FilterChips filter={filter} tz={w.tz} className="pl-7" />}
      <div className="flex flex-wrap gap-2 pl-7">
        {ownEvidence && (
          <Link to={ownEvidence} className={ACTION}>
            <Search className="h-3.5 w-3.5" aria-hidden />
            Search this finding&apos;s evidence
          </Link>
        )}
        {host && canBroaden && (
          <Link to={broadenLink(host)} className={ACTION}>
            <Expand className="h-3.5 w-3.5" aria-hidden />
            Broaden to all traffic of {host}
          </Link>
        )}
        {back}
      </div>
    </section>
  )
}
