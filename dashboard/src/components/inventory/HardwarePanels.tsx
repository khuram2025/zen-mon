import { useMemo, useState, type CSSProperties } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Cpu, Grid3x3, HardDrive, Network, Server } from 'lucide-react'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'
import type { HardwareResponse } from '@/pages/inventory/types'
import { DrillTarget } from './DrillTarget'
import {
  MATRIX_STEPS, PENDING_SERVER_HARDWARE, SERIAL_TONE_TEXT, matrixModel, matrixSummary, modelCountText, moduleGroups, serverHardware,
  vendorRows, visibleModuleRows, withSerialText, type BucketRow, type MatrixModel, type SerialTone, type VendorRow,
} from './hardwareModel'
import { InventoryEmpty } from './InventoryEmpty'
import { fmtInt, fmtPct, plural } from './model'
import { Panel } from './PanelCard'
import { ScrollShadow } from './ScrollShadow'

const th = 'h-9 px-3 text-left align-middle text-xs font-semibold uppercase tracking-wider text-muted'
const thRight = cn(th, 'text-right')

/** A thin proportional bar (decorative: the number beside it is the information). Unknown is hatched. */
function Bar({ pct, unknown }: { pct: number; unknown?: boolean }) {
  return (
    <span aria-hidden className="block h-1.5 w-full overflow-hidden rounded-full bg-surface2">
      <span
        className="block h-full rounded-full"
        style={{
          width: `${Math.max(pct > 0 ? 2 : 0, Math.min(100, pct))}%`,
          background: unknown ? 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.7) 0 2px, rgb(var(--surface2)) 2px 5px)' : 'rgb(var(--primary) / 0.7)',
        }}
      />
    </span>
  )
}

/* ── Network hardware: vendor > model ────────────────────────────────────── */

const TONE_CLASS: Record<SerialTone, string> = {
  complete: 'border-success/30 bg-success/10 text-success-fg',
  partial: 'border-warning/35 bg-warning/10 text-warning-fg',
  poor: 'border-danger/30 bg-danger/10 text-danger-fg',
  none: 'border-border bg-surface2 text-text2',
}

/**
 * The serial coverage of a group: the percentage in a pill (the words are in the tooltip and for screen readers), the
 * missing devices a link. The percentage is over the devices that CAN report; those that cannot by design are their own
 * count and link. A group where none can report has no percentage (a dash and the reason), never 0 %.
 */
function SerialCell({ pct, tone, missing, drill, count, notCollected, notCollectedDrill }: {
  pct: number | null; tone: SerialTone; missing: number; drill: VendorRow['missingSerialDrill']; count: number; notCollected: number; notCollectedDrill: VendorRow['notCollectedDrill']
}) {
  const reporting = count - notCollected
  return (
    <span className="inline-flex flex-wrap items-center justify-end gap-x-2 gap-y-0.5">
      <span
        className={cn('inline-flex items-center rounded-full border px-2 py-px text-xs font-medium tabular-nums', TONE_CLASS[tone])}
        title={pct === null ? SERIAL_TONE_TEXT.none : `${SERIAL_TONE_TEXT[tone]}: ${fmtPct(pct)} of the ${plural(reporting, 'device')} that can report one have a serial number`}
      >
        {pct === null ? '—' : fmtPct(pct)}
        <span className="sr-only">{pct === null ? ` ${SERIAL_TONE_TEXT.none}.` : ` of devices report a serial number. ${SERIAL_TONE_TEXT[tone]}.`}</span>
      </span>
      {missing > 0 && drill && (
        <DrillTarget drill={drill} className="text-xs text-text2" title={`${fmtInt(missing)} without a serial number: open the list`}>{fmtInt(missing)} missing</DrillTarget>
      )}
      {notCollected > 0 && notCollectedDrill && (
        <DrillTarget drill={notCollectedDrill} className="text-xs text-muted" title="Ping-only and controller-managed devices cannot report a serial number; they are not in the percentage">{fmtInt(notCollected)} not reported by design</DrillTarget>
      )}
    </span>
  )
}

export function HardwareNetworkPanel({ hw, onMatrix, matrixVendor }: { hw: HardwareResponse; onMatrix: (vendor: string) => void; matrixVendor: string | null }) {
  const vendors = useMemo(() => vendorRows(hw), [hw])
  const modelRows = vendors.reduce((n, v) => n + v.rows.length, 0)
  // A small fleet shows every model; a large one opens the three biggest vendors and leaves the rest one click away.
  const initial = useMemo(() => new Set(vendors.filter((v, i) => v.rows.length > 0 && (modelRows <= 40 || i < 3)).map((v) => v.key)), [vendors, modelRows])
  const [opened, setOpened] = useState<Set<string> | null>(null)
  const expanded = opened ?? initial
  const toggle = (key: string) => {
    const next = new Set(expanded)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setOpened(next)
  }
  const expandable = vendors.filter((v) => v.rows.length > 0)
  return (
    <Panel
      title="Network hardware"
      icon={<Network className="h-4 w-4" />}
      hint="vendor, then model"
      bodyClassName="p-0"
      right={expandable.length > 0 ? (
        <div className="flex items-center gap-3 text-xs">
          <button type="button" onClick={() => setOpened(new Set(expandable.map((v) => v.key)))} className="rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">Expand all</button>
          <button type="button" onClick={() => setOpened(new Set())} className="rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">Collapse all</button>
        </div>
      ) : undefined}
    >
      {hw.network.devices === 0 ? (
        <div className="p-4"><InventoryEmpty kind="no-data" title="No network devices yet">Devices appear here once the poller has read their vendor and model. This page fills in by itself.</InventoryEmpty></div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-separate border-spacing-0 text-sm" data-hardware-table="">
            <caption className="sr-only">{fmtInt(hw.network.devices)} network devices by vendor and model, with firmware versions, serial number coverage and modules</caption>
            <thead>
              <tr className="[&>th]:border-b [&>th]:border-border">
                <th scope="col" className={th}>Vendor / model</th>
                <th scope="col" className={thRight}>Devices</th>
                <th scope="col" className={cn(th, 'hidden md:table-cell text-right')}>Firmware versions</th>
                <th scope="col" className={thRight}>Serial coverage</th>
                <th scope="col" className={cn(th, 'hidden lg:table-cell text-right')}>Modules</th>
                <th scope="col" className={cn(th, 'hidden lg:table-cell')}><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {vendors.flatMap((v) => {
                const open = expanded.has(v.key)
                const rows = [(
                  <tr key={v.key} data-vendor={v.vendor ?? ''} className="bg-surface2/50 transition-colors hover:bg-surface2 [&>td]:border-b [&>td]:border-border/60 [&>th]:border-b [&>th]:border-border/60">
                    <th scope="row" className="px-3 py-1.5 text-left align-middle font-normal">
                      {v.rows.length > 0 ? (
                        <button
                          type="button"
                          aria-expanded={open}
                          aria-label={`${open ? 'Collapse' : 'Expand'} ${v.label}, ${modelCountText(v.models, v.unknownModelDevices)}`}
                          onClick={() => toggle(v.key)}
                          className="-ml-1 flex items-center gap-1.5 rounded-sm px-1 py-0.5 text-left outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-primary"
                        >
                          {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />}
                          <span className="font-semibold">{v.label}</span>{' '}
                          <span className="text-xs text-muted">{modelCountText(v.models, v.unknownModelDevices)}</span>
                        </button>
                      ) : (
                        <span className="flex items-center gap-1.5 pl-5">
                          <span className={cn('font-semibold', v.unknown && 'italic text-muted')}>{v.label}</span>
                          {v.unknown && <span className="text-xs text-muted">no vendor or model identified</span>}
                        </span>
                      )}
                    </th>
                    <td className="px-3 text-right align-middle"><DrillTarget drill={v.drill} className="font-semibold" title={`${plural(v.count, 'device')}: open the list`} ariaLabel={`${v.label}: ${plural(v.count, 'device')}`}>{fmtInt(v.count)}</DrillTarget></td>
                    <td className="hidden px-3 text-right align-middle tabular-nums text-text2 md:table-cell">{v.unknown || v.firmwareVersions === 0 ? <span className="text-muted" title="No firmware version identified">{v.unknown ? '—' : 'none'}</span> : fmtInt(v.firmwareVersions)}</td>
                    <td className="px-3 text-right align-middle"><SerialCell pct={v.serialPct} tone={v.serialTone} missing={v.serialMissing} drill={v.missingSerialDrill} count={v.count} notCollected={v.notCollected} notCollectedDrill={v.notCollectedDrill} /></td>
                    <td className="hidden px-3 text-right align-middle tabular-nums text-text2 lg:table-cell">{v.modules > 0 ? fmtInt(v.modules) : <span className="text-muted">—</span>}</td>
                    <td className="hidden px-3 text-right align-middle lg:table-cell">
                      {v.vendor !== null && (
                        <button
                          type="button"
                          onClick={() => onMatrix(v.vendor as string)}
                          aria-pressed={matrixVendor === v.vendor}
                          className="whitespace-nowrap rounded text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary"
                          title={`Show ${v.vendor} models by firmware release`}
                        >
                          Firmware matrix
                        </button>
                      )}
                    </td>
                  </tr>
                )]
                if (open) {
                  for (const m of v.rows) {
                    rows.push(
                      <tr key={m.key} data-model={m.model ?? ''} className="transition-colors hover:bg-surface2/60 [&>td]:border-b [&>td]:border-border/60 [&>th]:border-b [&>th]:border-border/60">
                        <th scope="row" className="px-3 py-1.5 pl-9 text-left align-middle font-normal">
                          <span className={cn('block truncate', m.unknown && 'italic text-muted')} title={m.label}>{m.label}</span>
                        </th>
                        <td className="px-3 text-right align-middle"><DrillTarget drill={m.drill} className="font-medium" title={`${plural(m.count, 'device')}: open the list`} ariaLabel={`${m.vendor} ${m.label}: ${plural(m.count, 'device')}`}>{fmtInt(m.count)}</DrillTarget></td>
                        <td className="hidden px-3 text-right align-middle tabular-nums text-text2 md:table-cell">{m.firmwareVersions > 0 ? fmtInt(m.firmwareVersions) : <span className="text-muted" title="No firmware version identified">none</span>}</td>
                        <td className="px-3 text-right align-middle"><SerialCell pct={m.serialPct} tone={m.serialTone} missing={m.serialMissing} drill={m.missingSerialDrill} count={m.count} notCollected={m.notCollected} notCollectedDrill={m.notCollectedDrill} /></td>
                        <td className="hidden px-3 text-right align-middle tabular-nums text-text2 lg:table-cell">{m.modules > 0 ? fmtInt(m.modules) : <span className="text-muted">—</span>}</td>
                        <td className="hidden lg:table-cell" />
                      </tr>,
                    )
                  }
                }
                return rows
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

/* ── The models x firmware matrix ────────────────────────────────────────── */

function cellStyle(step: number): CSSProperties {
  if (step <= 0) return {}
  const s = MATRIX_STEPS[step - 1]
  // The inset ring keeps the lightest steps visible on the dark card; the number is in the cell either way.
  return { background: `rgb(var(--primary) / ${s.alpha})`, color: `rgb(var(--${s.text}))`, boxShadow: 'inset 0 0 0 1px rgb(var(--primary) / 0.35)' }
}

export function MatrixLegend({ m }: { m: MatrixModel }) {
  if (m.legend.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted" data-matrix-legend="">
      <span>Devices per cell (every cell shows its number):</span>
      <ul role="list" className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {m.legend.map((l) => (
          <li key={l.step} className="inline-flex items-center gap-1.5">
            <span aria-hidden className="h-3.5 w-5 rounded-sm border border-border" style={{ background: `rgb(var(--primary) / ${MATRIX_STEPS[l.step - 1].alpha})` }} />
            <span className="tabular-nums">{l.range}</span>
          </li>
        ))}
        <li className="inline-flex items-center gap-1.5"><span aria-hidden className="h-3.5 w-5 rounded-sm border border-dashed border-border-strong" /><span>none</span></li>
      </ul>
    </div>
  )
}

export function MatrixTable({ m }: { m: MatrixModel }) {
  return (
    // Total sits right after Model so it is never the column that scrolls out of view; the releases scroll, and say so.
    <ScrollShadow label={`${m.vendor} models by firmware release`} hint="Scroll sideways to see more firmware releases">
      <table className="w-full border-separate border-spacing-0 text-xs" data-matrix="">
        <caption className="sr-only">{matrixSummary(m)}. Each cell is the number of devices of that model on that firmware release.</caption>
        <thead>
          <tr>
            <th scope="col" className="sticky left-0 z-10 h-9 bg-surface px-2 text-left align-bottom text-xs font-semibold uppercase tracking-wider text-muted">Model</th>
            <th scope="col" className="h-9 px-2 text-right align-bottom text-xs font-semibold uppercase tracking-wider text-muted">Total</th>
            {m.columns.map((c) => (
              <th key={c.release} scope="col" className={cn('h-9 min-w-[3.25rem] px-1 text-center align-bottom text-xs font-semibold', c.unknown ? 'italic text-muted' : 'font-mono text-text2')}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {m.rows.map((r) => (
            <tr key={r.key} data-matrix-row={r.model ?? ''}>
              <th scope="row" className={cn('sticky left-0 z-10 max-w-[11rem] truncate bg-surface px-2 py-0.5 text-left text-sm font-normal', r.unknown && 'italic text-muted')} title={r.label}>
                {r.label}
                {r.releases > 1 && (
                  <span className="ml-1.5 rounded-full border border-border px-1.5 text-2xs text-text2" title={`Runs on ${plural(r.releases, 'release')}`}>
                    <span aria-hidden>{r.releases}</span>
                    <span className="sr-only">, runs on {plural(r.releases, 'release')}</span>
                  </span>
                )}
              </th>
              <td className="px-2 py-0.5 text-right">
                <DrillTarget drill={r.totalDrill} className="font-semibold" title={`${plural(r.total, 'device')}: open the list`} ariaLabel={`${m.vendor} ${r.label}, all releases: ${plural(r.total, 'device')}`}>{fmtInt(r.total)}</DrillTarget>
              </td>
              {r.cells.map((c) => (
                <td key={c.release} className="p-0.5 text-center">
                  {c.drill ? (
                    <DrillTarget
                      drill={c.drill}
                      className="flex h-8 min-w-[2.75rem] items-center justify-center rounded text-xs font-semibold tabular-nums hover:no-underline hover:ring-2 hover:ring-primary/60"
                      ariaLabel={`${r.label} on ${c.release === 'unknown' ? 'an unknown release' : `release ${c.release}`}: ${plural(c.count, 'device')}`}
                    >
                      <span className="flex h-full w-full items-center justify-center rounded" style={cellStyle(c.step)}>{fmtInt(c.count)}</span>
                    </DrillTarget>
                  ) : (
                    <span role="img" className="flex h-8 min-w-[2.75rem] items-center justify-center rounded border border-dashed border-border text-muted/60" aria-label="No devices">
                      <span aria-hidden>·</span>
                    </span>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollShadow>
  )
}

/* ── ENTITY-MIB modules and components ───────────────────────────────────── */

export function HardwareModulesPanel({ hw }: { hw: HardwareResponse }) {
  const groups = useMemo(() => moduleGroups(hw), [hw])
  const [all, setAll] = useState<Set<string>>(new Set())
  return (
    <Panel title="Modules and components" icon={<HardDrive className="h-4 w-4" />} hint="ENTITY-MIB" bodyClassName="p-0">
      {groups.length === 0 ? (
        <div className="p-4">
          <InventoryEmpty kind="no-data" compact title="No components reported yet">
            Devices that answer ENTITY-MIB report their chassis, modules, power supplies and fans. No device has reported any yet, so there is nothing to list. Module firmware and component serial numbers are listed on each device's detail.
          </InventoryEmpty>
        </div>
      ) : (
        <div className="space-y-3 p-4">
          <p className="text-xs text-muted">A group is a component class and model. The link counts the devices that have it; a device can hold several, so components can outnumber devices.</p>
          {groups.map((g) => {
            const open = all.has(g.class)
            const { shown: rows, hidden } = visibleModuleRows(g.rows, open)
            return (
              <section key={g.class} aria-label={g.label} data-module-class={g.class}>
                <h3 className="mb-1 flex items-baseline justify-between text-xs font-semibold uppercase tracking-wider text-muted">
                  <span>{g.label}</span>
                  <span className="font-normal normal-case tracking-normal">{fmtInt(g.components)} {g.components === 1 ? 'component' : 'components'}</span>
                </h3>
                <table className="w-full text-xs">
                  <caption className="sr-only">{g.label} components by model: devices that have them, components and how many report a serial</caption>
                  <thead>
                    <tr>
                      <th scope="col" className="sr-only">Model</th>
                      <th scope="col" className="sr-only">Devices</th>
                      <th scope="col" className="sr-only">Components</th>
                      <th scope="col" className="sr-only">Serial numbers</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.key} className="border-t border-border/60">
                        <th scope="row" className={cn('max-w-0 truncate px-1.5 py-1 text-left font-normal', r.unknown && 'italic text-muted')} title={r.label}>{r.label}</th>
                        <td className="w-24 whitespace-nowrap px-1.5 text-right">
                          {r.drill ? <DrillTarget drill={r.drill} className="font-medium" title={`${plural(r.devices, 'device')} ${r.devices === 1 ? 'has' : 'have'} this: open the list`}>{plural(r.devices, 'device')}</DrillTarget> : <span className="tabular-nums">{plural(r.devices, 'device')}</span>}
                        </td>
                        <td className="hidden w-24 whitespace-nowrap px-1.5 text-right tabular-nums text-text2 sm:table-cell">{fmtInt(r.components)} {r.components === 1 ? 'part' : 'parts'}</td>
                        <td className="hidden w-36 whitespace-nowrap px-1.5 text-right tabular-nums text-muted md:table-cell" title="Components that report a serial number">{withSerialText(r.components, r.withSerial)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {(hidden > 0 || open) && (
                  <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setAll((s) => { const n = new Set(s); if (n.has(g.class)) n.delete(g.class); else n.add(g.class); return n })}
                    className="mt-1 rounded text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary"
                  >
                    {open ? 'Show fewer' : `Show ${fmtInt(hidden)} more ${hidden === 1 ? 'model' : 'models'}`}
                  </button>
                )}
              </section>
            )
          })}
        </div>
      )}
    </Panel>
  )
}

/* ── Server hardware ─────────────────────────────────────────────────────── */

function Distribution({ title, rows, noun = 'servers', note, stacked }: { title: string; rows: BucketRow[]; noun?: string; note?: string; stacked?: boolean }) {
  return (
    <section aria-label={title} className="min-w-0">
      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted">{title}</h3>
      <ul role="list" className="space-y-0.5">
        {rows.map((r) => (
          <li key={r.value}>
            <DrillTarget
              drill={r.drill}
              className={cn('rounded-md px-1.5 py-1 text-sm hover:bg-surface2 hover:no-underline', stacked ? 'block' : 'flex items-center gap-2')}
              ariaLabel={`${r.label}: ${fmtInt(r.count)} ${r.count === 1 ? noun.replace(/s$/, '') : noun}, ${fmtPct(r.pct)}`}
            >
              {stacked ? (
                // Long names (CPU models) get the whole line; the bar sits under them.
                <>
                  <span className="flex items-baseline gap-2">
                    <span className={cn('min-w-0 flex-1 truncate', r.unknown && 'italic text-muted')} title={r.label}>{r.label}</span>
                    <span className="w-8 shrink-0 text-right font-medium tabular-nums">{fmtInt(r.count)}</span>
                    <span className="w-12 shrink-0 text-right text-xs tabular-nums text-muted">{fmtPct(r.pct)}</span>
                  </span>
                  <span className="mt-1 block"><Bar pct={r.barPct} unknown={r.unknown} /></span>
                </>
              ) : (
                <>
                  <span className={cn('w-[42%] min-w-0 shrink-0 truncate', r.unknown && 'italic text-muted')} title={r.label}>{r.label}</span>
                  <span className="min-w-0 flex-1"><Bar pct={r.barPct} unknown={r.unknown} /></span>
                  <span className="w-8 text-right font-medium tabular-nums">{fmtInt(r.count)}</span>
                  <span className="hidden w-12 text-right text-xs tabular-nums text-muted sm:block">{fmtPct(r.pct)}</span>
                </>
              )}
            </DrillTarget>
          </li>
        ))}
      </ul>
      {note && <p className="mt-1.5 px-1.5 text-xs text-muted">{note}</p>}
    </section>
  )
}

export function HardwareServersPanel({ hw }: { hw: HardwareResponse }) {
  const s = useMemo(() => serverHardware(hw), [hw])
  return (
    <Panel title="Server hardware" icon={<Server className="h-4 w-4" />} hint="CPU, memory, disks">
      {s.total === 0 ? (
        <InventoryEmpty kind="no-data" compact title="No servers yet">Servers appear here once their agent reports. CPU models, memory and disks follow in the same report.</InventoryEmpty>
      ) : (
        <div className="space-y-4">
          {!s.anyKnown && (
            <p className="flex items-start gap-2 rounded-md border border-border bg-surface2/50 px-3 py-2 text-xs text-text2" data-server-hardware="none">
              <Cpu className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
              <span>No server has reported its CPU, memory or disks yet, so every server below is Unknown. The agent sends them with its next inventory report.</span>
            </p>
          )}
          <div className="grid gap-x-8 gap-y-5 md:grid-cols-2 xl:grid-cols-4">
            <Distribution
              title="CPU models"
              stacked
              rows={s.cpu}
              note={s.cpuMore ? `${plural(s.cpuMore.models, 'more CPU model')} on ${plural(s.cpuMore.servers, 'server')} ${s.cpuMore.models === 1 ? 'is' : 'are'} not listed.` : undefined}
            />
            <Distribution title="Installed memory" rows={s.memory} />
            <Distribution title="Physical disks" rows={s.disks} />
            {s.virtualization ? (
              <Distribution title="Virtualization" rows={s.virtualization} />
            ) : (
              <section aria-label="Virtualization" className="min-w-0" data-server-hardware="pending">
                <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted">Virtualization</h3>
                <p className="rounded-md border border-dashed border-border-strong px-3 py-2 text-xs leading-5 text-muted">{PENDING_SERVER_HARDWARE}</p>
              </section>
            )}
          </div>
        </div>
      )}
    </Panel>
  )
}

/* ── The matrix panel: a vendor selector, its own request and its own states ───────────── */

export function HardwareMatrixPanel({
  query, vendors, vendor, onVendor,
}: {
  query: Pick<UseQueryResult<HardwareResponse, Error>, 'data' | 'isError' | 'error' | 'isFetching' | 'isPlaceholderData' | 'refetch'>
  vendors: { vendor: string; count: number }[]
  vendor: string | null
  onVendor: (vendor: string) => void
}) {
  const m = useMemo(() => (query.data ? matrixModel(query.data) : null), [query.data])
  const select = vendors.length > 0 ? (
    <label className="flex items-center gap-2 text-xs text-muted">
      <span>Vendor</span>
      <select
        value={vendor ?? ''}
        onChange={(e) => onVendor(e.target.value)}
        className="h-8 max-w-[14rem] rounded-md border border-border bg-surface px-2 text-xs text-text outline-none focus-visible:ring-2 focus-visible:ring-primary"
        data-matrix-vendor=""
      >
        {vendors.map((v) => <option key={v.vendor} value={v.vendor}>{v.vendor} ({fmtInt(v.count)})</option>)}
      </select>
    </label>
  ) : undefined
  return (
    <Panel title="Models by firmware" icon={<Grid3x3 className="h-4 w-4" />} hint="mixed releases at a glance" right={select}>
      {vendors.length === 0 ? (
        <InventoryEmpty kind="no-data" compact title="No vendor identified yet">The matrix needs devices with a known vendor. It fills in once the poller has identified some.</InventoryEmpty>
      ) : query.data === undefined && !query.isError ? (
        <div className="space-y-2" aria-busy="true" aria-label="Loading the firmware matrix">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
      ) : query.data === undefined ? (
        <QueryFailure compact error={query.error} what="the firmware matrix" onRetry={() => void query.refetch()} retrying={query.isFetching} />
      ) : !m || m.rows.length === 0 ? (
        <InventoryEmpty kind="no-data" compact title={`No ${vendor ?? ''} devices`}>There are no devices of this vendor in your view.</InventoryEmpty>
      ) : (
        <div className={cn('space-y-3', query.isPlaceholderData && 'opacity-60 transition-opacity')} aria-busy={query.isFetching}>
          {query.isError && <QueryFailure compact error={query.error} what="the firmware matrix" onRetry={() => void query.refetch()} retrying={query.isFetching} />}
          <p className="text-xs text-text2" role="status">{matrixSummary(m)}.</p>
          <MatrixTable m={m} />
          <MatrixLegend m={m} />
        </div>
      )}
    </Panel>
  )
}
