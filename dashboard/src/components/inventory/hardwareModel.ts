// Pure logic behind the Hardware page (Documentation/Inventory/03-UI-UX-SPEC.md section 3.6): the
// vendor > model table with serial coverage, the models x firmware matrix and its colour scale, the
// ENTITY-MIB module groups and the server hardware distributions. No React, no router, no API:
// scripts/test-inventory-os-hardware-changes.mjs exercises it directly.
//
// Principle 1: every count is a link to the assets behind it and the drilled list's total EQUALS the
// number clicked (05-P1-API-CONTRACT.md section 4). The numbers that are NOT list totals carry no link:
// firmware_versions, component counts (`count`, `with_serial`) and `modules`.
// Principle 2: unknown is a value. A vendor or model nobody could identify is a row, counted and
// drillable through the sentinel (`vendor=unknown`, `model=unknown`), never hidden. The model count of every
// heading, row and summary counts KNOWN models only and says how many devices have none ({@link modelCountText}).
// A device that cannot report by design (ping-only, controller-managed) is counted in its row and listed in
// `not_collected`; it is out of the serial coverage and drills to `freshness=not_applicable`.

import {
  MODULE_CLASSES, type AssetFilters, type DiskBucket, type MemoryBucket, type ModuleClass,
} from '@/pages/inventory/filters'
import type { HardwareBucket, HardwareModelRow, HardwareResponse, HardwareVendorRow } from '@/pages/inventory/types'
import { fmtInt, share } from './model'
import { assetsDrill, type Drill } from './overviewModel'

/* ── Network hardware: vendor > model ────────────────────────────────────── */

/** How complete a group's serial numbers are: a pill with the percentage AND the words, never colour alone. `none`: no device of the group can report one. */
export type SerialTone = 'complete' | 'partial' | 'poor' | 'none'

/** 100 % is complete, 80 % and up is nearly there, below that the group needs attention; no percentage (nothing can report) is `none`. */
export function serialTone(pct: number | null | undefined): SerialTone {
  if (pct === null || pct === undefined) return 'none'
  if (pct >= 100) return 'complete'
  if (pct >= 80) return 'partial'
  return 'poor'
}

export const SERIAL_TONE_TEXT: Record<SerialTone, string> = {
  complete: 'All serials known',
  partial: 'Some serials missing',
  poor: 'Many serials missing',
  none: 'No device of this group can report a serial number',
}

/**
 * "12 models plus 3 devices with no model": the ONE sentence for a model count, used by the page header, the vendor
 * rows and the matrix (review finding UI-04). `known` counts distinct known (vendor, model) pairs only; devices that
 * report no model are not a model, they are their own count and their own row.
 */
export function modelCountText(known: number, unknownDevices: number): string {
  const models = `${fmtInt(known)} ${known === 1 ? 'model' : 'models'}`
  const none = `${fmtInt(unknownDevices)} ${unknownDevices === 1 ? 'device' : 'devices'} with no model`
  if (known > 0 && unknownDevices > 0) return `${models} plus ${none}`
  if (known > 0) return models
  return unknownDevices > 0 ? `no model identified on ${fmtInt(unknownDevices)} ${unknownDevices === 1 ? 'device' : 'devices'}` : 'no models'
}

export interface ModelRow {
  key: string
  vendor: string
  /** The model, or null when the vendor is known and the model is not. */
  model: string | null
  label: string
  count: number
  /** Share of the vendor's devices, 0 to 100. */
  pct: number
  firmwareVersions: number
  serialMissing: number
  /** Over the devices that can report; null when none can. */
  serialPct: number | null
  serialTone: SerialTone
  modules: number
  unknown: boolean
  /** Devices of the row that cannot report by design. */
  notCollected: number
  /** The devices of the model (`model: null`: the vendor's devices with no model, `model=unknown`). */
  drill: Drill
  /** The devices without a serial (`health=missing_serial`, the row's own filters). */
  missingSerialDrill: Drill | null
  /** The devices that cannot report by design (`freshness=not_applicable`, the row's own filters). */
  notCollectedDrill: Drill | null
}

export interface VendorRow {
  key: string
  /** The vendor, or null for the devices with no vendor. */
  vendor: string | null
  label: string
  count: number
  /** Share of all network devices, 0 to 100. */
  pct: number
  /** Distinct KNOWN models. */
  models: number
  /** Devices of this vendor with no model: a count of its own, not a model. */
  unknownModelDevices: number
  firmwareVersions: number
  serialMissing: number
  serialPct: number | null
  serialTone: SerialTone
  modules: number
  unknown: boolean
  notCollected: number
  drill: Drill
  /** The devices without a serial (`health=missing_serial`, the row's own filters). */
  missingSerialDrill: Drill | null
  notCollectedDrill: Drill | null
  /** Models of this vendor, biggest first, the unknown model last. */
  rows: ModelRow[]
}

const compareText = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })

const rowDrills = (base: AssetFilters, c: { count: number; serialMissing: number; notCollected: number }) => ({
  drill: assetsDrill(base, c.count),
  missingSerialDrill: c.serialMissing > 0 ? assetsDrill({ ...base, health: 'missing_serial' }, c.serialMissing) : null,
  notCollectedDrill: c.notCollected > 0 ? assetsDrill({ ...base, freshness: 'not_applicable' }, c.notCollected) : null,
})

function modelRow(m: HardwareModelRow): ModelRow {
  const known = m.model !== null && !m.unknown
  const base: AssetFilters = { kind: 'device', vendor: m.vendor, model: known ? (m.model as string) : 'unknown' }
  return {
    key: `m:${m.vendor}/${m.model ?? ''}`,
    vendor: m.vendor,
    model: m.model,
    label: m.model ?? 'Unknown model',
    count: m.count,
    pct: 0,
    firmwareVersions: m.firmware_versions,
    serialMissing: m.serial_missing,
    serialPct: m.serial_coverage_pct,
    serialTone: serialTone(m.serial_coverage_pct),
    modules: m.modules,
    unknown: !known,
    notCollected: m.not_collected,
    ...rowDrills(base, { count: m.count, serialMissing: m.serial_missing, notCollected: m.not_collected }),
  }
}

function vendorRow(v: HardwareVendorRow, models: readonly HardwareModelRow[], devices: number): VendorRow {
  const known = v.vendor !== null
  const rows = known
    ? models
      .filter((m) => m.vendor === v.vendor)
      .map(modelRow)
      .sort((a, b) => (a.unknown !== b.unknown ? (a.unknown ? 1 : -1) : b.count - a.count || compareText(a.label, b.label)))
    : []
  for (const r of rows) r.pct = share(r.count, v.count)
  // No vendor is not a string: `vendor=unknown` lists exactly those devices, whatever their freshness.
  const base: AssetFilters = { kind: 'device', vendor: known ? (v.vendor as string) : 'unknown' }
  return {
    key: `v:${v.vendor ?? ''}`,
    vendor: v.vendor,
    label: v.vendor ?? 'Unknown vendor',
    count: v.count,
    pct: share(v.count, devices),
    models: v.models_known,
    unknownModelDevices: v.unknown_model_devices,
    firmwareVersions: v.firmware_versions,
    serialMissing: v.serial_missing,
    serialPct: v.serial_coverage_pct,
    serialTone: serialTone(v.serial_coverage_pct),
    modules: v.modules,
    unknown: !known,
    notCollected: v.not_collected,
    ...rowDrills(base, { count: v.count, serialMissing: v.serial_missing, notCollected: v.not_collected }),
    rows,
  }
}

/** Vendors biggest first with the vendor-less devices last, each with its models. */
export function vendorRows(hw: HardwareResponse): VendorRow[] {
  const { vendors, models, devices } = hw.network
  return [...vendors]
    .sort((a, b) => ((a.vendor === null) !== (b.vendor === null) ? (a.vendor === null ? 1 : -1) : b.count - a.count || compareText(a.vendor ?? '', b.vendor ?? '')))
    .map((v) => vendorRow(v, models, devices))
}

/** The vendors a matrix can be built for (a named vendor): the selector's options, biggest first. */
export function matrixVendors(hw: HardwareResponse): { vendor: string; count: number }[] {
  return vendorRows(hw).filter((v) => v.vendor !== null).map((v) => ({ vendor: v.vendor as string, count: v.count }))
}

/** The vendor the matrix shows: the one in the address when the fleet has it, otherwise the biggest vendor. */
export function chooseMatrixVendor(requested: string | null | undefined, hw: HardwareResponse | undefined): string | null {
  if (!hw) return requested?.trim() || null
  const options = matrixVendors(hw)
  const wanted = (requested ?? '').trim().toLowerCase()
  const hit = wanted ? options.find((o) => o.vendor.toLowerCase() === wanted) : undefined
  return hit?.vendor ?? options[0]?.vendor ?? null
}

/* ── The models x firmware matrix ────────────────────────────────────────── */

/**
 * The colour scale of a cell: five steps from "a few" to "the most" of the matrix, as the share of the
 * largest cell. `alpha` is the strength of the primary colour over the card; `text` is the token the
 * number is drawn in. They are chosen so the number reads at 4.5:1 or better in both themes
 * (scripts/test-inventory-os-hardware-changes.mjs measures it from styles/globals.css); the number is
 * in every cell, so the colour is a second signal, never the only one.
 */
export const MATRIX_STEPS: readonly { alpha: number; text: 'text' | 'on-primary' }[] = [
  { alpha: 0.1, text: 'text' },
  { alpha: 0.24, text: 'text' },
  { alpha: 0.4, text: 'text' },
  { alpha: 0.55, text: 'text' },
  { alpha: 1, text: 'on-primary' },
]

/** 0 for an empty cell, otherwise 1 to 5: the share of the largest cell, rounded up. */
export function matrixStep(count: number, max: number): number {
  if (count <= 0 || max <= 0) return 0
  return Math.min(MATRIX_STEPS.length, Math.max(1, Math.ceil((count / max) * MATRIX_STEPS.length)))
}

export interface MatrixCell {
  release: string
  count: number
  /** 0 for an empty cell. */
  step: number
  /** The devices of this model on this release; null for an empty cell. */
  drill: Drill | null
}

export interface MatrixRow {
  key: string
  model: string | null
  label: string
  unknown: boolean
  total: number
  totalDrill: Drill
  /** One cell per release column, in the columns' order. */
  cells: MatrixCell[]
  /** Releases the model runs on: more than one is a mixed-firmware fleet. */
  releases: number
}

export interface MatrixLegendStep {
  step: number
  /** The counts the step covers, "1 to 3". */
  range: string
}

export interface MatrixModel {
  vendor: string
  /** Columns, newest first, `unknown` last. */
  columns: { release: string; label: string; unknown: boolean }[]
  rows: MatrixRow[]
  max: number
  legend: MatrixLegendStep[]
  /** Models that run more than one release. */
  mixed: number
  /** Distinct KNOWN models (the unknown-model row is not one). */
  knownModels: number
  /** Devices of the vendor with no model: the unknown-model row's total. */
  unknownModelDevices: number
}

/**
 * The counts each colour step stands for, found by running every count of the matrix through the same
 * function the cells use, so the legend can never disagree with the cells. A step no count reaches
 * (a small matrix) is left out.
 */
export function matrixLegend(max: number): MatrixLegendStep[] {
  const span = new Map<number, [number, number]>()
  for (let n = 1; n <= max; n += 1) {
    const step = matrixStep(n, max)
    const at = span.get(step)
    span.set(step, at ? [at[0], n] : [n, n])
  }
  return [...span.entries()].sort(([a], [b]) => a - b).map(([step, [lo, hi]]) => ({ step, range: lo === hi ? `${lo}` : `${lo} to ${hi}` }))
}

const releaseFilter = (release: string): Pick<AssetFilters, 'os_release'> => ({ os_release: release })

export function matrixModel(hw: HardwareResponse): MatrixModel | null {
  const m = hw.matrix
  if (!m) return null
  const max = Math.max(0, ...m.models.flatMap((row) => row.cells.map((c) => c.count)))
  const rows: MatrixRow[] = m.models.map((row) => {
    const known = row.model !== null && !row.unknown
    const base: AssetFilters = { kind: 'device', vendor: m.vendor, model: known ? (row.model as string) : 'unknown' }
    const byRelease = new Map(row.cells.map((c) => [c.release, c.count]))
    const cells: MatrixCell[] = m.releases.map((release) => {
      const count = byRelease.get(release) ?? 0
      return {
        release,
        count,
        step: matrixStep(count, max),
        drill: count > 0 ? assetsDrill({ ...base, ...releaseFilter(release) }, count) : null,
      }
    })
    return {
      key: `${m.vendor}/${row.model ?? ''}`,
      model: row.model,
      label: row.model ?? 'Unknown model',
      unknown: !known,
      total: row.count,
      totalDrill: assetsDrill(base, row.count),
      cells,
      releases: cells.filter((c) => c.count > 0).length,
    }
  })
  rows.sort((a, b) => (a.unknown !== b.unknown ? (a.unknown ? 1 : -1) : b.total - a.total || compareText(a.label, b.label)))
  const legend = matrixLegend(max)
  return {
    vendor: m.vendor,
    columns: m.releases.map((release) => ({ release, label: release === 'unknown' ? 'Unknown' : release, unknown: release === 'unknown' })),
    rows,
    max,
    legend,
    mixed: rows.filter((r) => !r.unknown && r.releases > 1).length,
    knownModels: rows.filter((r) => !r.unknown).length,
    unknownModelDevices: rows.filter((r) => r.unknown).reduce((n, r) => n + r.total, 0),
  }
}

/** "Cisco: 5 models plus 2 devices with no model, 3 of them on more than one release" for the matrix header and a screen reader. */
export function matrixSummary(m: MatrixModel): string {
  const models = modelCountText(m.knownModels, m.unknownModelDevices)
  if (m.knownModels === 0) return `${m.vendor}: ${models}`
  return m.mixed > 0 ? `${m.vendor}: ${models}; ${m.mixed} of the models on more than one release` : `${m.vendor}: ${models}; each model on a single release`
}

/* ── ENTITY-MIB modules and components ───────────────────────────────────── */

export interface ModuleModelRow {
  key: string
  model: string | null
  label: string
  unknown: boolean
  /** Components of the group: not a list total (a device can hold several), so plain text. */
  components: number
  /** Components that report a serial: plain text. */
  withSerial: number
  /** Distinct devices that have the component: the drilled list's total. */
  devices: number
  drill: Drill | null
}

export interface ModuleClassGroup {
  class: string
  label: string
  components: number
  rows: ModuleModelRow[]
}

const isModuleClass = (c: string): c is ModuleClass => (MODULE_CLASSES as readonly string[]).includes(c)

/** Components grouped by class (the API's class order), models with a name first, then by count. */
export function moduleGroups(hw: HardwareResponse): ModuleClassGroup[] {
  const groups = new Map<string, ModuleClassGroup>()
  for (const m of hw.modules) {
    const group = groups.get(m.class) ?? { class: m.class, label: m.class_label || m.class, components: 0, rows: [] }
    groups.set(m.class, group)
    group.components += m.count
    group.rows.push({
      key: `${m.class}/${m.model ?? ''}`,
      model: m.model,
      label: m.model ?? 'Unknown model',
      unknown: m.model === null,
      components: m.count,
      withSerial: m.with_serial,
      devices: m.devices,
      // A class the filter vocabulary does not know cannot be addressed: its number stays text.
      drill: isModuleClass(m.class) ? assetsDrill({ module_class: m.class, module_model: m.model ?? 'unknown' }, m.devices) : null,
    })
  }
  const order = (c: string) => { const i = (MODULE_CLASSES as readonly string[]).indexOf(c); return i < 0 ? MODULE_CLASSES.length : i }
  return [...groups.values()]
    .sort((a, b) => order(a.class) - order(b.class) || compareText(a.label, b.label))
    .map((g) => ({
      ...g,
      rows: [...g.rows].sort((a, b) => (a.unknown !== b.unknown ? (a.unknown ? 1 : -1) : b.devices - a.devices || b.components - a.components || compareText(a.label, b.label))),
    }))
}

/** Models listed per class before "Show N more". */
export const MODULE_ROWS_SHOWN = 6

/**
 * The rows of one class to draw: the first {@link MODULE_ROWS_SHOWN} named models, and the unnamed model ALWAYS
 * (unknown is a value: it is never folded behind "Show more"). `hidden` is how many rows the button reveals.
 */
export function visibleModuleRows(rows: readonly ModuleModelRow[], expanded: boolean, limit = MODULE_ROWS_SHOWN): { shown: ModuleModelRow[]; hidden: number } {
  if (expanded || rows.length <= limit) return { shown: [...rows], hidden: 0 }
  const named = rows.filter((r) => !r.unknown).slice(0, limit)
  const unknown = rows.filter((r) => r.unknown)
  const shown = [...named, ...unknown]
  return { shown, hidden: rows.length - shown.length }
}

/** Serial coverage of a component group in words: "5 of 6 report a serial". */
export function withSerialText(components: number, withSerial: number): string {
  return `${withSerial} of ${components} report a serial`
}

/* ── Server hardware ─────────────────────────────────────────────────────── */

export interface BucketRow {
  value: string
  label: string
  count: number
  /** Share of all servers, 0 to 100. */
  pct: number
  /** Share of the largest bucket, 0 to 100 (the bar's width). */
  barPct: number
  unknown: boolean
  drill: Drill
}

export interface ServerHardwareModel {
  total: number
  cpu: BucketRow[]
  /** What the 25 listed CPU models leave out: text, not a link (it is several models). */
  cpuMore: { models: number; servers: number } | null
  memory: BucketRow[]
  disks: BucketRow[]
  /** Null until some server reports a virtualisation value: the page says so, in words. */
  virtualization: BucketRow[] | null
  /** Whether any server reported a CPU model, a memory size or a disk count (false: every figure is Unknown). */
  anyKnown: boolean
}

function bucketRows(buckets: readonly HardwareBucket[], total: number, toFilters: (value: string) => AssetFilters): BucketRow[] {
  const max = Math.max(1, ...buckets.map((b) => b.count))
  return buckets.map((b) => ({
    value: b.value,
    label: b.label,
    count: b.count,
    pct: share(b.count, total),
    barPct: share(b.count, max),
    unknown: b.value === 'unknown',
    drill: assetsDrill(toFilters(b.value), b.count),
  }))
}

export function serverHardware(hw: HardwareResponse): ServerHardwareModel {
  const s = hw.servers
  const cpu = bucketRows(s.cpu_models, s.total, (v) => ({ cpu_model: v }))
  const memory = bucketRows(s.memory, s.total, (v) => ({ memory: v as MemoryBucket }))
  const disks = bucketRows(s.disks, s.total, (v) => ({ disks: v as DiskBucket }))
  const virtualization = s.virtualization === null ? null : bucketRows(s.virtualization, s.total, (v) => ({ virtualization: v }))
  const more = s.cpu_models_more
  const known = (rows: BucketRow[]) => rows.some((r) => !r.unknown && r.count > 0)
  return {
    total: s.total,
    cpu,
    cpuMore: more.models > 0 ? { models: more.models, servers: more.servers } : null,
    memory,
    disks,
    virtualization,
    anyKnown: known(cpu) || known(memory) || known(disks),
  }
}

/**
 * What the agent's hardware identity report (not collected in this release) will add, said in plain words on the page
 * until a server reports it. The wording is for the empty state only; nothing here is a value.
 */
export const PENDING_SERVER_HARDWARE =
  'Manufacturer, model, serial number, BIOS version and whether a server is physical or virtual are not reported yet. This release does not collect them from servers.'

/** "12 servers" / "1 server" for the page and for labels. */
export const serversText = (n: number) => `${n} ${n === 1 ? 'server' : 'servers'}`
