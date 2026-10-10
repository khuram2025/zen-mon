import { Info } from 'lucide-react'
import { historyText, type HistoryInfo } from './changesModel'

/**
 * The data-integrity footnote auditors ask for: when change capture began and how long changes are kept. Both
 * come from the Changes response itself (`history_start`, `retention_days`), never from a constant or from the
 * events on the page.
 */
export function ChangesFootnote({
  info, now, tz, browserTz, tzFallback = false, restricted,
}: {
  info: HistoryInfo | null | undefined
  now: number
  /** The zone the server used for days and the footnote date (`window.tz`). */
  tz: string
  /** The zone the browser reported, named when the server could not use it. */
  browserTz?: string
  tzFallback?: boolean
  restricted: boolean
}) {
  const t = historyText(info, now, tz)
  return (
    <div className="space-y-1 px-1 text-[11px] leading-4 text-muted" data-changes-footnote="">
      <p className="flex items-start gap-1.5">
        <Info className="mt-px h-3 w-3 shrink-0" aria-hidden />
        <span title={t.title ?? undefined}>{t.text}</span>
      </p>
      {tzFallback && (
        <p className="pl-[18px]">Days are grouped in {tz}: the server does not know this browser&apos;s time zone{browserTz ? ` (${browserTz})` : ''}.</p>
      )}
      {restricted && (
        <p className="pl-[18px]">You see the changes of the assets in your scope. Changes of an asset that has been deleted since are not shown to a scoped user.</p>
      )}
    </div>
  )
}
