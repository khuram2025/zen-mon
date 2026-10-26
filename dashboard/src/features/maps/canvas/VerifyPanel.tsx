/* "Verify topology" (F3): checks every cable on the map against what the
 * devices report — interface bound and present, port up, fresh CDP/LLDP
 * evidence for the pair — plus devices that are down/unknown. Floating panel
 * in the ProblemsPanel style; each row focuses its link or node. */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertOctagon, AlertTriangle, ChevronDown, ChevronRight, Info, Link2Off, Loader2, RefreshCw, ServerCrash, ShieldCheck, Unplug, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { formatAgo } from '../core'

export type VerifySeverity = 'critical' | 'warning' | 'info'

export type VerifyLinkIssue = {
  link_id: string
  annotation: boolean
  severity: VerifySeverity
  code: string
  message: string
}

export type VerifyNodeIssue = {
  node_id: string
  severity: VerifySeverity
  code: string
  message: string
}

export type VerifyResult = {
  links: VerifyLinkIssue[]
  nodes: VerifyNodeIssue[]
  summary: { critical: number; warning: number; info: number }
  generated_at: string
}

export type VerifyPanelProps = {
  mapId: string
  open: boolean
  onClose: () => void
  onFocusLink: (id: string) => void
  onFocusNode: (id: string) => void
  labelForLink: (id: string) => string
  labelForNode: (id: string) => string
}

/** Friendly titles for the server's issue codes (contract item 6). */
export const VERIFY_CODE_TEXT: Record<string, string> = {
  unbound: 'No interface set on either end',
  half_bound: 'Interface set on one end only',
  port_not_found: 'Interface not found on the device',
  port_down: 'Port is down',
  port_never_up: 'Port has never been up',
  no_evidence: 'No CDP/LLDP evidence for this cable',
  contradicted: 'Discovery shows a different neighbour',
  stale_adjacency: 'Neighbour evidence is stale',
  device_down: 'Device is down',
  device_unknown: 'Device status unknown',
}

export function verifyCodeText(code: string): string {
  return VERIFY_CODE_TEXT[code] || code.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

export function verifyQueryKey(mapId: string) {
  return ['manual-map-verify', mapId] as const
}

const SEVERITIES: { key: VerifySeverity; label: string; text: string; chip: string; icon: React.ReactNode }[] = [
  { key: 'critical', label: 'Critical', text: 'text-danger', chip: 'bg-danger/15 text-danger', icon: <AlertOctagon className="h-3.5 w-3.5" /> },
  { key: 'warning', label: 'Warnings', text: 'text-warning', chip: 'bg-warning/15 text-warning', icon: <AlertTriangle className="h-3.5 w-3.5" /> },
  { key: 'info', label: 'Notes', text: 'text-muted', chip: 'bg-surface2 text-muted', icon: <Info className="h-3.5 w-3.5" /> },
]

type Row = { key: string; kind: 'link' | 'node'; id: string; severity: VerifySeverity; code: string; message: string }

function rowIcon(r: Row) {
  if (r.kind === 'node') return <ServerCrash className="h-3.5 w-3.5" />
  if (r.code === 'unbound' || r.code === 'half_bound' || r.code === 'port_not_found') return <Unplug className="h-3.5 w-3.5" />
  return <Link2Off className="h-3.5 w-3.5" />
}

export function VerifyPanel({ mapId, open, onClose, onFocusLink, onFocusNode, labelForLink, labelForNode }: VerifyPanelProps) {
  const [collapsed, setCollapsed] = useState<Record<VerifySeverity, boolean>>({ critical: false, warning: false, info: true })

  const q = useQuery<VerifyResult>({
    queryKey: verifyQueryKey(mapId),
    enabled: open && !!mapId,
    // Re-check on every open; never poll (the check walks the topology).
    staleTime: 0,
    refetchOnWindowFocus: false,
    queryFn: async () => (await api.get(`/maps/${mapId}/verify`)).data,
  })

  const groups = useMemo(() => {
    const out: Record<VerifySeverity, Row[]> = { critical: [], warning: [], info: [] }
    // The same code can be raised for both ends of one cable — index keeps keys unique.
    ;(q.data?.links || []).forEach((l, i) => {
      const sev = (out[l.severity] ? l.severity : 'info') as VerifySeverity
      out[sev].push({ key: `l-${l.link_id}-${l.code}-${i}`, kind: 'link', id: l.link_id, severity: sev, code: l.code, message: l.message })
    })
    ;(q.data?.nodes || []).forEach((n, i) => {
      const sev = (out[n.severity] ? n.severity : 'info') as VerifySeverity
      out[sev].push({ key: `n-${n.node_id}-${n.code}-${i}`, kind: 'node', id: n.node_id, severity: sev, code: n.code, message: n.message })
    })
    return out
  }, [q.data])

  if (!open) return null

  const total = groups.critical.length + groups.warning.length + groups.info.length
  const problems = groups.critical.length + groups.warning.length

  return (
    <div className="flex w-80 max-w-[85vw] flex-col rounded-lg border border-border bg-surface/95 shadow-xl backdrop-blur">
      {/* Header */}
      <div className="flex items-center justify-between gap-2 px-2.5 py-1.5">
        <span className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-muted">
          {q.data && problems === 0
            ? <ShieldCheck className="h-3.5 w-3.5 text-success" />
            : <AlertTriangle className={cn('h-3.5 w-3.5', groups.critical.length ? 'text-danger' : 'text-warning')} />}
          Verify topology
        </span>
        <span className="flex items-center gap-1">
          {SEVERITIES.map((s) => groups[s.key].length > 0 && (
            <span key={s.key} className={cn('rounded px-1 font-mono text-2xs', s.chip)}>{groups[s.key].length}</span>
          ))}
          <button
            type="button"
            onClick={() => q.refetch()}
            disabled={q.isFetching}
            className="rounded p-1 text-muted transition hover:bg-primary/10 hover:text-text disabled:opacity-50"
            title="Check again"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', q.isFetching && 'animate-spin')} />
          </button>
          <button type="button" onClick={onClose} className="rounded p-1 text-muted transition hover:bg-primary/10 hover:text-text" title="Close">
            <X className="h-3.5 w-3.5" />
          </button>
        </span>
      </div>

      {/* Body */}
      <div className="max-h-[55vh] overflow-y-auto border-t border-border/60">
        {q.isLoading ? (
          <div className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Checking cables against discovery…
          </div>
        ) : q.isError && !q.data ? (
          <div className="flex flex-col items-center gap-2 px-3 py-4 text-center text-xs text-danger">
            {apiErrorMessage(q.error, 'Verification failed')}
            <button type="button" onClick={() => q.refetch()} className="rounded border border-border px-2 py-0.5 text-text2 hover:bg-primary/10">Retry</button>
          </div>
        ) : total === 0 ? (
          <div className="flex flex-col items-center gap-1.5 px-3 py-6 text-center">
            <ShieldCheck className="h-6 w-6 text-success" />
            <div className="text-sm font-semibold text-text">All cables verified</div>
            <div className="text-2xs text-muted">Every cable matches a bound, up interface with fresh neighbour evidence.</div>
          </div>
        ) : (
          <>
            {q.isError && (
              <div className="border-b border-border/60 px-2.5 py-1 text-2xs text-danger">
                Re-check failed: {apiErrorMessage(q.error, 'request failed')} — showing the previous result.
              </div>
            )}
            {problems === 0 && (
              <div className="flex items-center gap-1.5 border-b border-border/60 px-2.5 py-1.5 text-xs text-success">
                <ShieldCheck className="h-3.5 w-3.5" /> No problems found — only notes below.
              </div>
            )}
            {SEVERITIES.map((s) => {
              const rows = groups[s.key]
              if (!rows.length) return null
              const isCollapsed = collapsed[s.key]
              return (
                <div key={s.key} className="border-b border-border/40 last:border-b-0">
                  <button
                    type="button"
                    onClick={() => setCollapsed((c) => ({ ...c, [s.key]: !c[s.key] }))}
                    className={cn('flex w-full items-center gap-1.5 px-2.5 py-1 text-2xs font-bold uppercase tracking-wider transition hover:bg-primary/5', s.text)}
                  >
                    {isCollapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                    {s.icon} {s.label}
                    <span className="ml-auto font-mono">{rows.length}</span>
                  </button>
                  {!isCollapsed && rows.map((r) => (
                    <button
                      key={r.key}
                      type="button"
                      onClick={() => (r.kind === 'link' ? onFocusLink(r.id) : onFocusNode(r.id))}
                      className="flex w-full items-start gap-2 px-2.5 py-1.5 text-left transition hover:bg-primary/10"
                      title="Locate on map"
                    >
                      <span className={cn('mt-0.5 shrink-0', s.text)}>{rowIcon(r)}</span>
                      <span className="min-w-0">
                        <span className="block truncate text-xs font-semibold text-text">
                          {r.kind === 'link' ? labelForLink(r.id) : labelForNode(r.id)}
                        </span>
                        <span className="block text-2xs text-text2">{verifyCodeText(r.code)}</span>
                        {r.message && <span className="block text-2xs leading-snug text-muted">{r.message}</span>}
                      </span>
                    </button>
                  ))}
                </div>
              )
            })}
          </>
        )}
      </div>

      {q.data?.generated_at && (
        <div className="border-t border-border/60 px-2.5 py-1 text-2xs text-muted">Checked {formatAgo(q.data.generated_at)}</div>
      )}
    </div>
  )
}
