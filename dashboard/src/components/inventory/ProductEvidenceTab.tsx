import { Card } from '@/components/ui/Card'
import { cn } from '@/lib/utils'
import type { ProductResponse } from '@/pages/inventory/softwareSettings'
import { InventoryEmpty } from './InventoryEmpty'
import { fmtInt } from './model'
import { evidenceNote, evidenceStatusText, installSourceLabel } from './productModel'
import { ScrollShadow } from './ScrollShadow'
import { normStatusMeta } from './softwareModel'

/**
 * The Evidence tab: the raw package names and publishers the machines report, grouped into this product, with
 * how the normaliser reads each one today. A read-only table: raw evidence is shown as it arrived and never
 * edited. The host and installation counts have no list of their own (a raw title is not an address in the
 * asset filters), so they are text.
 */
export function ProductEvidenceTab({ data }: { data: Pick<ProductResponse, 'header' | 'evidence'> }) {
  const { evidence, header } = data
  const note = evidenceNote(evidence)
  return (
    <Card data-tab="evidence">
      <div className="flex flex-wrap items-baseline gap-x-2 border-b border-border px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight">Raw names grouped into {header.name}</h2>
        <span className="text-[10px] uppercase tracking-wider text-muted">as the servers report them</span>
      </div>
      {evidence.items.length === 0 ? (
        <InventoryEmpty kind="no-data" title="No raw names to show">Nothing on the servers you can see maps to this product.</InventoryEmpty>
      ) : (
        // A table wider than the phone scrolls sideways: the region is focusable (arrow keys) and says it scrolls (NF-4).
        <ScrollShadow label={`Raw names grouped into ${header.name}`} hint="Scroll sideways to see the other columns">
          <table className="w-full border-separate border-spacing-0 text-[13px]" data-evidence-table="">
            <caption className="sr-only">Raw package names and publishers that map to {header.name}, with how many hosts and installations report each</caption>
            <thead>
              <tr className="[&>th]:border-b [&>th]:border-border">
                {['Raw package name', 'Raw publisher', 'Source'].map((h) => (
                  <th key={h} scope="col" className="h-9 px-3 text-left text-[11px] font-semibold uppercase tracking-wider text-muted">{h}</th>
                ))}
                <th scope="col" className="h-9 px-3 text-right text-[11px] font-semibold uppercase tracking-wider text-muted">Hosts</th>
                <th scope="col" className="h-9 px-3 text-right text-[11px] font-semibold uppercase tracking-wider text-muted">Installations</th>
                <th scope="col" className="h-9 px-3 text-left text-[11px] font-semibold uppercase tracking-wider text-muted">Read by the catalog as</th>
              </tr>
            </thead>
            <tbody>
              {evidence.items.map((e, i) => {
                const status = normStatusMeta(e.status)
                return (
                  <tr key={`${e.package_name}:${e.vendor}:${e.source}:${i}`} className="[&>td]:border-b [&>td]:border-border/60 [&>td]:py-1.5 [&>th]:border-b [&>th]:border-border/60 [&>th]:py-1.5">
                    <th scope="row" className="max-w-[22rem] px-3 text-left align-middle font-mono text-xs font-normal"><span className="block truncate" title={e.package_name}>{e.package_name}</span></th>
                    <td className="max-w-[16rem] truncate px-3 align-middle text-text2" title={e.vendor ?? undefined}>{e.vendor ?? <span className="italic text-muted">No publisher reported</span>}</td>
                    <td className="px-3 align-middle"><span className="rounded-full border border-border bg-surface2 px-1.5 py-px text-[10.5px] text-text2">{installSourceLabel(e.source)}</span></td>
                    <td className="px-3 text-right align-middle tabular-nums">{fmtInt(e.hosts)}</td>
                    <td className="px-3 text-right align-middle tabular-nums text-text2">{fmtInt(e.installs)}</td>
                    <td className="px-3 align-middle">
                      <span className="flex flex-wrap items-center gap-1.5">
                        {e.status && <span title={status.meaning} className={cn('rounded-full border px-1.5 py-px text-[10px] font-medium', status.tone)}>{status.label}</span>}
                        <span className="text-xs text-text2">{evidenceStatusText(e)}</span>
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </ScrollShadow>
      )}
      <div className="space-y-1 border-t border-border/60 px-4 py-3 text-xs text-muted">
        {note && <p>{note}</p>}
        <p>
          This table is read-only. If a title is grouped under the wrong product, the catalog rule behind it needs changing. Rules cannot be edited in this release; these raw names are the evidence to bring to whoever maintains the catalog.
        </p>
      </div>
    </Card>
  )
}
