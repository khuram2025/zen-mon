import { Link } from 'react-router-dom'
import {
  AlertTriangle, ArrowDownCircle, ArrowUpCircle, Cpu, MonitorCog, PackageMinus, PackagePlus, RefreshCw, Wrench, type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { assetsHref } from '@/pages/inventory/filters'
import { CATEGORY_LABELS } from './changeModel'
import {
  PARTNER_ELSEWHERE_NOTE, entryTime, truncatedNote, type ChangeEntry, type DayGroup, type Glyph,
} from './changesModel'
import { toMillis } from './model'

const GLYPHS: Record<Glyph, { icon: LucideIcon; tone: string; label: string }> = {
  installed: { icon: PackagePlus, tone: 'bg-primary/10 text-primary', label: 'Installed' },
  removed: { icon: PackageMinus, tone: 'bg-surface3 text-text2', label: 'Removed' },
  upgraded: { icon: ArrowUpCircle, tone: 'bg-primary/10 text-primary', label: 'Upgraded' },
  downgraded: { icon: ArrowDownCircle, tone: 'bg-surface3 text-text2', label: 'Downgraded' },
  changed: { icon: RefreshCw, tone: 'bg-surface3 text-text2', label: 'Changed' },
  os: { icon: MonitorCog, tone: 'bg-primary/10 text-primary', label: 'OS change' },
  firmware: { icon: Cpu, tone: 'bg-primary/10 text-primary', label: 'Firmware change' },
  hardware: { icon: Wrench, tone: 'bg-primary/10 text-primary', label: 'Hardware change' },
  marker: { icon: AlertTriangle, tone: 'bg-warning/10 text-warning-fg', label: 'Not recorded' },
}

function AssetName({ entry }: { entry: ChangeEntry }) {
  const name = entry.assetName ?? entry.address ?? 'Unknown asset'
  if (!entry.address) {
    // Deleted since: the history survives it, the page does not exist any more.
    return <span className="font-medium text-text" title="This asset has been deleted; its history is kept">{name} <span className="text-[11px] font-normal text-muted">(deleted)</span></span>
  }
  return (
    <Link to={assetsHref({}, { asset: entry.address })} className="rounded-sm font-medium text-text outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary" title="Open this asset's details">
      {name}
    </Link>
  )
}

/** One row of the list: time, a glyph (decorative: the sentence says it), the sentence and the category. */
export function ChangeRowView({ entry, tz }: { entry: ChangeEntry; tz: string }) {
  const g = GLYPHS[entry.glyph]
  const Icon = g.icon
  const at = toMillis(entry.at)
  const time = entryTime(entry, tz)
  if (entry.kind === 'marker') {
    return (
      <li className="flex items-start gap-3 rounded-md border border-dashed border-warning/50 bg-warning/5 px-2 py-2" data-change-kind="marker" data-change={entry.ids.join(',')}>
        <time dateTime={entry.at} title={at === null ? undefined : new Date(at).toLocaleString()} className="w-11 shrink-0 pt-0.5 text-xs tabular-nums text-muted">{time}</time>
        <span aria-hidden className={cn('flex h-6 w-6 shrink-0 items-center justify-center rounded-md', g.tone)}><Icon className="h-3.5 w-3.5" /></span>
        <div className="min-w-0 flex-1 text-[13px] leading-5">
          <span className="font-medium text-warning-fg">Change budget exceeded</span>
          <span className="text-text2"> for <AssetName entry={entry} />: further changes of that report were not recorded.</span>
          <p className="mt-0.5 text-[11px] text-muted">This marks a gap in the history. It is not a change of the asset.</p>
        </div>
        <span className="hidden shrink-0 rounded border border-warning/35 bg-warning/10 px-1.5 py-px text-[10.5px] font-medium text-warning-fg sm:inline">Gap</span>
      </li>
    )
  }
  return (
    <li className="flex items-start gap-3 py-2" data-change-kind={entry.kind} data-change={entry.ids.join(',')}>
      <time dateTime={entry.at} title={at === null ? undefined : new Date(at).toLocaleString()} className="w-11 shrink-0 pt-0.5 text-xs tabular-nums text-muted">{time}</time>
      <span aria-hidden className={cn('flex h-6 w-6 shrink-0 items-center justify-center rounded-md', g.tone)}><Icon className="h-3.5 w-3.5" /></span>
      <div className="min-w-0 flex-1 text-[13px] leading-5">
        <span className="text-text2">
          <span className="font-semibold text-text">{entry.subject}</span> {entry.verb}
          {entry.detail && <> <span className="break-words font-mono text-xs text-text">{entry.detail}</span></>}
          {entry.replaces && <span className="text-muted"> (replaces {entry.replaces})</span>}
          <span className="text-muted"> on </span>
        </span>
        <AssetName entry={entry} />
        {entry.kind === 'pair' && (
          <span className="ml-1.5 text-[11px] text-muted" title="The server records an upgrade of a product whose title carries its version as a removal and an installation; they are shown as one change">1 change, 2 events</span>
        )}
        {entry.partnerElsewhere && <p className="mt-0.5 text-[11px] text-muted">{PARTNER_ELSEWHERE_NOTE}</p>}
        {entry.truncated && (
          <p className="mt-0.5 flex items-start gap-1 text-[11px] text-warning-fg" data-truncated="">
            <AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />
            <span>{truncatedNote(entry)}</span>
          </p>
        )}
      </div>
      <span className="hidden shrink-0 rounded border border-border bg-surface2 px-1.5 py-px text-[10.5px] font-medium text-text2 sm:inline">{CATEGORY_LABELS[entry.category]}</span>
    </li>
  )
}

/** Entries grouped by calendar day (newest first), each day a heading and a list: a real structure for a screen reader. */
export function ChangesList({ groups, tz }: { groups: readonly DayGroup[]; tz: string }) {
  return (
    <div className="divide-y divide-border" data-changes-list="">
      {groups.map((g) => (
        <section key={g.day} aria-labelledby={`day-${g.day}`} className="px-4 py-3" data-day={g.day}>
          <h3 id={`day-${g.day}`} className="mb-1 flex items-baseline gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
            {g.relative && <span className="text-text2">{g.relative}</span>}
            <span>{g.text}</span>
          </h3>
          <ol className="divide-y divide-border/60">
            {g.entries.map((e) => <ChangeRowView key={e.key} entry={e} tz={tz} />)}
          </ol>
        </section>
      ))}
    </div>
  )
}
