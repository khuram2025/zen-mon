import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { formatAgo, type SuggestedLink } from '../core'
import { useSuggestedLinks } from '../useMapData'

const pairKey = (s: SuggestedLink) => `${s.source_node_id}|${s.target_node_id}`

const REASON_TEXT: Record<string, string> = {
  old_evidence: 'not seen recently',
  local_port_never_up: 'local port never up',
  remote_port_never_up: 'remote port never up',
  local_port_down: 'local port down',
  remote_port_down: 'remote port down',
  local_port_missing: 'local port not found',
  remote_port_missing: 'remote port not found',
}

const short = (h: string) => h.replace(/\.(local|com|net|org|lan)$/i, '').replace(/\.[a-z0-9-]+\.(local|com|net|org)$/i, '')

/* Review step for Quick connect. Discovered CDP/LLDP adjacencies are only
 * trustworthy while they are current: a neighbour entry that stopped being
 * refreshed, or that names a port which has never been up, is the remains of
 * an old cabling and would draw a false link. Fresh pairs are pre-ticked;
 * stale pairs are listed with their reason and left unticked. */
export function QuickConnectDialog({ mapId, saving, onCancel, onConnect }: {
  mapId: string
  saving: boolean
  onCancel: () => void
  onConnect: (pairs: [string, string][], includeStale: boolean) => void
}) {
  const q = useSuggestedLinks(mapId, true, true)
  const rows = useMemo(() => {
    const list = q.data?.data || []
    return [...list].sort((a, b) => Number(!!a.stale) - Number(!!b.stale) || (a.source_hostname || '').localeCompare(b.source_hostname || ''))
  }, [q.data])
  const [picked, setPicked] = useState<Set<string>>(new Set())
  useEffect(() => {
    setPicked(new Set(rows.filter((r) => !r.stale).map(pairKey)))
  }, [rows])

  const freshCount = rows.filter((r) => !r.stale).length
  const staleCount = rows.length - freshCount
  const chosen = rows.filter((r) => picked.has(pairKey(r)))
  const toggle = (k: string) => setPicked((cur) => { const n = new Set(cur); if (n.has(k)) n.delete(k); else n.add(k); return n })

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>Quick connect — discovered links</DialogTitle></DialogHeader>
        <DialogDescription>
          CDP/LLDP adjacencies between devices on this map that have no cable yet. Stale evidence (not seen in the last
          24 hours, or a port that has never been up) is unticked — it usually means the cabling changed.
        </DialogDescription>

        {q.isLoading ? (
          <div className="flex items-center justify-center py-10 text-xs text-muted"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Checking discovered neighbours…</div>
        ) : q.isError ? (
          <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-xs text-danger">Could not load discovered links.</div>
        ) : rows.length === 0 ? (
          <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted">Every discovered adjacency among placed devices already has a cable.</div>
        ) : (
          <div className="max-h-[50vh] overflow-y-auto rounded-md border border-border">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-surface2 text-[10px] uppercase tracking-wide text-muted">
                <tr>
                  <th className="w-8 px-2 py-1.5 text-left">
                    <input
                      type="checkbox"
                      className="accent-primary"
                      checked={chosen.length === rows.length && rows.length > 0}
                      onChange={(e) => setPicked(e.target.checked ? new Set(rows.map(pairKey)) : new Set())}
                      aria-label="Select all"
                    />
                  </th>
                  <th className="px-2 py-1.5 text-left">Link</th>
                  <th className="px-2 py-1.5 text-left">Ports</th>
                  <th className="px-2 py-1.5 text-left">Evidence</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const k = pairKey(r)
                  return (
                    <tr key={k} className={cn('border-t border-border/60', r.stale && 'bg-warning/5')}>
                      <td className="px-2 py-1.5 align-top"><input type="checkbox" className="accent-primary" checked={picked.has(k)} onChange={() => toggle(k)} aria-label={`Connect ${r.source_hostname} to ${r.target_hostname}`} /></td>
                      <td className="px-2 py-1.5 align-top">
                        <div className="font-medium text-text">{short(r.source_hostname)} ⇄ {short(r.target_hostname)}</div>
                        {r.physical_links > 1 && <div className="text-[10px] text-muted">bundle · {r.physical_links} links</div>}
                      </td>
                      <td className="px-2 py-1.5 align-top font-mono text-[10.5px] text-text2">
                        {r.src_interface || '?'} ⇄ {r.dst_interface || '?'}
                        {(r.src_oper || r.dst_oper) && <div className="font-sans text-[10px] text-muted">{r.src_oper || '?'} / {r.dst_oper || '?'}</div>}
                      </td>
                      <td className="px-2 py-1.5 align-top">
                        <div className="text-[10.5px] uppercase text-muted">{r.protocol || 'cdp'} · seen {formatAgo(r.last_seen_at)}</div>
                        {r.stale && (
                          <div className="mt-0.5 flex items-start gap-1 text-[10.5px] text-warning">
                            <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
                            <span>{(r.reasons || []).map((x) => REASON_TEXT[x] || x.replace(/_/g, ' ')).join(' · ') || 'stale'}</span>
                          </div>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        <DialogFooter>
          <span className="mr-auto self-center text-[11px] text-muted">{freshCount} current · {staleCount} stale</span>
          <Button variant="outline" onClick={onCancel} disabled={saving}>Cancel</Button>
          <Button
            onClick={() => onConnect(chosen.map((r) => [r.source_node_id, r.target_node_id] as [string, string]), chosen.some((r) => r.stale))}
            disabled={saving || chosen.length === 0}
          >
            {saving ? 'Connecting…' : `Connect ${chosen.length}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
