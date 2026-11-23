/**
 * "Test against the last 7 days" for a combined rule draft: replays it over stored history
 * (POST /alert-rules/combined/replay, server/app/services/combined_replay.py) and lists when it would have fired.
 */
import { useMutation } from '@tanstack/react-query'
import { History, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'

type Episode = { subject: string; name: string; start: number; end: number | null }
type ReplayResult = {
  subjects: number
  subjects_capped: boolean
  episode_count: number
  subjects_affected: number
  episodes: Episode[]
  not_replayed_labels: string[]
  replayable: boolean
}

function when(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function lasted(start: number, end: number | null): string {
  if (end == null) return 'still open'
  const min = Math.round((end - start) / 60)
  return min >= 120 ? `${Math.round(min / 60)} h` : `${min} min`
}

export function CombinedReplay({ body, disabled }: { body: () => Record<string, unknown> | null; disabled?: boolean }) {
  const run = useMutation({
    mutationFn: async () => (await api.post('/alert-rules/combined/replay', body())).data as ReplayResult,
  })
  const r = run.data
  return (
    <div className="space-y-2 rounded-lg border border-border/60 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" disabled={disabled || run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <History className="h-3.5 w-3.5" />}
          Test against the last 7 days
        </Button>
        <span className="text-xs text-muted">Replays this draft over stored history; nothing is saved or sent.</span>
      </div>
      {run.isError && <p className="text-xs text-danger">{apiErrorMessage(run.error)}</p>}
      {r && (
        <div className="space-y-2 text-xs">
          {!r.replayable ? (
            <p className="text-muted">This kind of rule has no stored history to replay.</p>
          ) : (
            <p className="text-text2">
              {r.episode_count === 0
                ? `Would not have fired on any of ${r.subjects} ${r.subjects === 1 ? 'asset' : 'assets'}.`
                : `Would have fired ${r.episode_count} ${r.episode_count === 1 ? 'time' : 'times'} on ${r.subjects_affected} of ${r.subjects} ${r.subjects === 1 ? 'asset' : 'assets'}.`}
              {r.subjects_capped && ' Only the first 200 assets were replayed.'}
            </p>
          )}
          {r.not_replayed_labels.length > 0 && (
            <p className="text-warning">
              No stored history for: {r.not_replayed_labels.join('; ')}. These read as unknown in the replay, so an
              All (AND) rule that needs them cannot fire here.
            </p>
          )}
          {r.episodes.length > 0 && (
            <table className="w-full text-left">
              <thead className="text-2xs uppercase tracking-wider text-muted">
                <tr><th className="py-1 pr-3 font-medium">Asset</th><th className="py-1 pr-3 font-medium">From</th><th className="py-1 font-medium">Lasted</th></tr>
              </thead>
              <tbody>
                {r.episodes.slice(0, 20).map((e, i) => (
                  <tr key={i} className="border-t border-border/40">
                    <td className="py-1 pr-3">{e.name}</td>
                    <td className="py-1 pr-3 tabular-nums">{when(e.start)}</td>
                    <td className="py-1 tabular-nums">{lasted(e.start, e.end)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
