// Report renderer and viewer helpers: unit formatting, chart series, table
// sorting / search / CSV, status tones and the viewer's URL period contract
// (features/reports/render/format.ts, render/table.ts, viewer/range.ts).
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const toUrl = (code) => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
async function load(path, replacements = {}) {
  let src = fs.readFileSync(path, 'utf8')
  for (const [from, to] of Object.entries(replacements)) src = src.replaceAll(`from '${from}'`, `from '${to}'`)
  const out = await transformWithEsbuild(src, path.replace(/\.tsx?$/, '.ts'), { loader: 'ts', format: 'esm' })
  return { url: toUrl(out.code), mod: await import(toUrl(out.code)) }
}

const { mod: f } = await load('src/features/reports/render/format.ts')
const { mod: t } = await load('src/features/reports/render/table.ts')
const brand = await load('src/lib/branding.ts')
const periods = await load('src/features/reports/periods.ts', { '@/lib/branding': brand.url })
const { mod: r } = await load('src/features/reports/viewer/range.ts', { '../periods': periods.url })

/* ── Units ──────────────────────────────────────────────────────────── */
assert.equal(f.unitKind('%'), 'percent')
assert.equal(f.unitKind('bps'), 'bps')
assert.equal(f.unitKind('Bps'), 'Bps')
assert.equal(f.unitKind('B'), 'bytes')
assert.equal(f.unitKind('bytes'), 'bytes')
assert.equal(f.unitKind('sessions'), 'count')
assert.equal(f.formatValue(99.954, '%'), '99.95%', 'availability keeps two decimals')
assert.equal(f.formatValue(100, '%'), '100.00%')
assert.equal(f.formatValue(85, '%', { compact: true }), '85%')
assert.equal(f.formatValue(99.5, '%', { compact: true }), '99.5%')
assert.equal(f.formatValue(12.34, 'ms'), '12.3 ms')
assert.equal(f.formatValue(250, 'ms'), '250 ms')
assert.equal(f.formatValue(1534, 'ms'), '1.53 s')
assert.equal(f.formatValue(0.42, 'ms'), '0.42 ms')
assert.equal(f.formatValue(1_500_000, 'bps'), '1.50 Mbps')
assert.equal(f.formatValue(1_500_000, 'bps', { compact: true }), '1.5 Mbps')
assert.equal(f.formatValue(315_460, 'bps'), '315 Kbps')
assert.equal(f.formatValue(900, 'bps'), '900 bps')
assert.equal(f.formatValue(1536, 'bytes'), '1.50 KB')
assert.equal(f.formatValue(5 * 1024 ** 3, 'B'), '5.00 GB')
assert.equal(f.formatValue(2048, 'B/s'), '2.00 KB/s')
assert.equal(f.formatValue(3725, 's'), '1h 2m')
assert.equal(f.formatValue(42, 'seconds'), '42 s')
assert.equal(f.formatValue(90061, 's'), '1d 1h')
assert.equal(f.formatValue(1234, 'sessions'), '1,234 sessions')
assert.equal(f.formatValue(1234, 'sessions', { compact: true }), '1.2k')
assert.equal(f.formatValue(2_500_000, '', { compact: true }), '2.5M')
assert.equal(f.formatValue(3.14159), '3.14')
assert.equal(f.formatValue(null, '%'), '—', 'missing is never zero')
assert.equal(f.formatValue(Number.NaN, 'ms'), '—')
assert.equal(f.formatShare(0.0001, 100), '<0.1%')
assert.equal(f.formatShare(25, 100), '25.0%')
assert.equal(f.formatShare(1, 0), '—')

/* ── Organisation-timezone labels ───────────────────────────────────── */
const riyadh = { timezone: 'Asia/Riyadh', date_format: 'DD-MMM-YYYY', time_format: '24h' }
const at = Date.parse('2026-10-07T21:30:00Z') // 08 Oct 00:30 in Riyadh
assert.equal(f.shortDate(at, riyadh), '08-Oct')
assert.equal(f.shortTime(at, riyadh), '00:30')
assert.equal(f.shortTime(at, { ...riyadh, time_format: '12h' }), '12:30 AM')
assert.equal(f.shortDate(at, { ...riyadh, date_format: 'MM/DD/YYYY' }), '10/08')
assert.equal(f.shortDate(at, { ...riyadh, date_format: 'MMM D, YYYY' }), 'Oct 8')
const midnight = Date.parse('2026-10-07T21:00:00Z')
assert.equal(f.axisTimeLabel(midnight, 2 * 86_400_000, riyadh), '08-Oct', 'midnight ticks show the date')
assert.equal(f.axisTimeLabel(at, 86_400_000, riyadh), '00:30')
assert.equal(f.axisTimeLabel(at, 10 * 86_400_000, riyadh), '08-Oct')

/* ── Chart series ───────────────────────────────────────────────────── */
assert.equal(f.parseTime('2026-10-07T10:00:00'), Date.parse('2026-10-07T10:00:00Z'), 'naive timestamps are UTC')
assert.equal(f.parseTime('2026-10-07 10:00'), Date.parse('2026-10-07T10:00:00Z'))
assert.equal(f.parseTime('nope'), null)
const single = f.chartModel({
  kind: 'area', unit: '%',
  points: [{ t: '2026-10-07T02:00:00Z', v: 99 }, { t: '2026-10-07T00:00:00Z', v: 98 }, { t: '2026-10-07T01:00:00Z', v: null }],
}, 'Availability')
assert.equal(single.kind, 'time')
assert.equal(single.series[0].name, 'Availability')
assert.deepEqual(single.series[0].data.map((d) => d[1]), [98, null, 99], 'sorted by time, gaps kept as null')
assert.equal(single.start, Date.parse('2026-10-07T00:00:00Z'))
assert.equal(single.end, Date.parse('2026-10-07T02:00:00Z'))
const multi = f.chartModel({
  kind: 'line', unit: 'bps',
  series: [
    { name: 'In', color: 'primary', points: [{ t: '2026-10-07T00:00:00Z', v: 10 }] },
    { name: 'Out', points: [{ t: '2026-10-07T00:00:00Z', v: 5 }, { t: '2026-10-07T00:05:00Z', v: 7 }] },
  ],
})
assert.deepEqual(multi.series.map((s) => s.name), ['In', 'Out'])
assert.equal(multi.series[0].color, 'primary')
assert.equal(f.chartModel({ kind: 'area', points: [{ t: '2026-10-07T00:00:00Z', v: null }] }), null, 'no measurement → no chart')
assert.equal(f.chartModel({ kind: 'bars', points: [] }), null)
const cat = f.chartModel({
  kind: 'bars',
  series: [
    { name: 'Errors', points: [{ label: 'a', value: 3 }, { label: 'b', value: 1 }] },
    { name: 'Warnings', points: [{ label: 'b', value: 2 }, { label: 'c', value: 4 }] },
  ],
})
assert.equal(cat.kind, 'category')
assert.deepEqual(cat.labels, ['a', 'b', 'c'])
assert.deepEqual(cat.series[0].values, [3, 1, null])
assert.deepEqual(cat.series[1].values, [null, 2, 4])
const sl = f.slices([{ label: 'x', value: 1 }, { label: 'y', value: 3 }, { label: 'z', value: 0 }, { label: '', value: 5 }], true)
assert.deepEqual(sl.map((s) => s.label), ['y', 'x'], 'positive, labelled, largest first')
assert.deepEqual(sl.map((s) => s.share), ['75.0%', '25.0%'])
const desc = f.describeChart('Throughput', multi, 'bps', (ms) => new Date(ms).toISOString())
assert.match(desc, /Throughput: 2 series/)
assert.match(desc, /Out: min 5 bps, max 7 bps, latest 7 bps\./)

/* ── Sections ───────────────────────────────────────────────────────── */
assert.equal(f.sectionStatus({ kpis: [{ label: 'a', value: '1', accent: 'warning' }, { label: 'b', value: '2', accent: 'danger' }] }), 'danger')
assert.equal(f.sectionStatus({ kpis: [{ label: 'a', value: '1', accent: 'warning' }] }), 'warning')
assert.equal(f.sectionStatus({ kpis: [], error: true }), 'error')
assert.equal(f.sectionStatus({ kpis: [{ label: 'a', value: '1', accent: 'danger' }], restricted: true }), 'restricted')
assert.equal(f.sectionStatus({ kpis: [] }), 'ok')
assert.equal(f.sectionAnchorId('rpt', 0), 'rpt-s1')
const rows = f.layoutRows([{ half: true }, { half: true }, { half: true }, {}, { half: true }, { half: true }])
assert.deepEqual(rows.map((row) => row.map((x) => x.index)), [[0, 1], [2], [3], [4, 5]])
assert.equal(f.slug('Availability & SLA — Q3'), 'Availability-SLA-Q3')
assert.equal(f.slug('***'), 'report')

/* ── Tables ─────────────────────────────────────────────────────────── */
assert.deepEqual(
  t.inferStyles({ headers: ['Device', 'Latency', 'Note', 'Status'], rows: [['a', '12 ms', 'ok', 'Up'], ['b', '—', 'x', 'Down']], styles: [undefined, undefined, undefined, 'status'] }),
  ['text', 'num', 'text', 'status'],
)
assert.deepEqual(t.sortKey('1.5 s'), { n: 1500 })
assert.deepEqual(t.sortKey('850 ms'), { n: 850 })
assert.deepEqual(t.sortKey('2h 5m'), { n: 7_500_000 })
assert.deepEqual(t.sortKey('1,234'), { n: 1234 })
assert.deepEqual(t.sortKey('1.2 GB'), { n: 1.2 * 1024 ** 3 })
assert.deepEqual(t.sortKey('350 Mbps'), { n: 350e6 })
assert.deepEqual(t.sortKey('12 alerts'), { n: 12 })
assert.equal(t.sortKey('—'), null)
assert.equal(t.sortKey(null), null)
assert.ok(t.sortKey('<0.1%').n < 0.1)
assert.deepEqual(t.sortKey('Core-SW-01'), { s: 'core-sw-01' })
const lat = [['a', '1.2 s'], ['b', '—'], ['c', '850 ms'], ['d', '3 ms'], ['e', null]]
assert.deepEqual(t.sortRows(lat, 1, 'desc').map((x) => x[0]), ['a', 'c', 'd', 'b', 'e'], 'units normalised, empties last')
assert.deepEqual(t.sortRows(lat, 1, 'asc').map((x) => x[0]), ['d', 'c', 'a', 'b', 'e'], 'empties last in both directions')
const ips = [['10.0.0.10'], ['10.0.0.9'], ['9.255.0.1']]
assert.deepEqual(t.sortRows(ips, 0, 'asc').map((x) => x[0]), ['9.255.0.1', '10.0.0.9', '10.0.0.10'], 'IPv4 sorts numerically')
const names = [['sw-10'], ['sw-9'], ['Router']]
assert.deepEqual(t.sortRows(names, 0, 'asc').map((x) => x[0]), ['Router', 'sw-9', 'sw-10'], 'natural text order')
const stable = [['x', '1'], ['y', '1'], ['z', '1']]
assert.deepEqual(t.sortRows(stable, 1, 'desc').map((x) => x[0]), ['x', 'y', 'z'], 'stable for ties')
assert.equal(t.firstSortDir('num'), 'desc')
assert.equal(t.firstSortDir('text'), 'asc')
const people = [['Core switch', 'Riyadh', 'Up'], ['Edge router', 'Jeddah', 'Down'], ['Core router', 'Jeddah', 'Up']]
assert.deepEqual(t.filterRows(people, 'core jeddah').map((x) => x[0]), ['Core router'], 'every term must match')
assert.equal(t.filterRows(people, '   ').length, 3)
const csv = t.toCsv(['Name', 'Note', 'Value'], [['a,b', 'say "hi"', -5], ['=HYPERLINK("x")', 'line\nbreak', null], ['-1.2 ms', '+cmd', '@sum']])
assert.ok(csv.startsWith('﻿Name,Note,Value\r\n'), 'BOM and CRLF')
assert.ok(csv.includes('"a,b","say ""hi""",-5\r\n'), 'quotes commas and quotes; negative numbers kept')
assert.ok(csv.includes(`"'=HYPERLINK(""x"")","line\nbreak",\r\n`), 'formula injection neutralised; null is empty')
assert.ok(csv.includes(`-1.2 ms,'+cmd,'@sum\r\n`))
assert.equal(t.statusTone('Up'), 'success')
assert.equal(t.statusTone(' DOWN '), 'danger')
assert.equal(t.statusTone('Degraded'), 'warning')
assert.equal(t.statusTone('Acknowledged'), 'info')
assert.equal(t.statusTone('whatever'), 'neutral')
assert.equal(t.severityTone('Critical'), 'danger')
assert.equal(t.severityTone('minor'), 'warning')
assert.equal(t.severityTone('Low'), 'info')
assert.equal(t.pctValue('99.95%'), 99.95)
assert.equal(t.pctValue('<0.1%'), 0.1)
assert.equal(t.pctValue('—'), null)
assert.equal(t.pctTone(95, 'CPU used'), 'danger')
assert.equal(t.pctTone(80, 'Memory'), 'warning')
assert.equal(t.pctTone(40, 'Disk usage'), 'primary')
assert.equal(t.pctTone(99.95, 'Availability'), 'success')
assert.equal(t.pctTone(95, 'Uptime'), 'warning')
assert.equal(t.pctTone(80, 'SLA attainment'), 'danger')
assert.equal(t.pctTone(50, 'Share'), 'primary')

/* ── Viewer URL period contract ─────────────────────────────────────── */
assert.deepEqual(r.parseSelection('last_7d', null, null), { kind: 'period', id: 'last_7d' })
assert.deepEqual(r.parseSelection('previous_month', null, null), { kind: 'period', id: 'previous_month' })
assert.deepEqual(r.parseSelection('24h', null, null), { kind: 'period', id: 'last_24h' })
assert.deepEqual(r.parseSelection('7d', null, null), { kind: 'period', id: 'last_7d' })
assert.deepEqual(r.parseSelection('1M', null, null), { kind: 'period', id: 'last_30d' })
assert.deepEqual(r.parseSelection('month', null, null), { kind: 'period', id: 'month_to_date' })
assert.deepEqual(r.parseSelection('last_month', null, null), { kind: 'period', id: 'previous_month' })
assert.deepEqual(r.parseSelection('quarter', null, null), { kind: 'period', id: 'quarter_to_date' })
assert.deepEqual(r.parseSelection('1h', null, null), { kind: 'hour' })
assert.deepEqual(r.parseSelection(null, null, null, 'last_30d'), { kind: 'period', id: 'last_30d' }, 'template default')
assert.deepEqual(r.parseSelection('bogus', null, null), { kind: 'period', id: 'last_24h' })
const custom = r.parseSelection('custom', '2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z')
assert.equal(custom.kind, 'custom')
assert.equal(custom.from.toISOString(), '2026-09-01T00:00:00.000Z')
assert.equal(r.parseSelection(null, '2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z').kind, 'custom', 'from/to without range')
assert.deepEqual(r.parseSelection('custom', '2026-10-01T00:00:00Z', '2026-09-01T00:00:00Z', 'last_7d'), { kind: 'period', id: 'last_7d' }, 'inverted custom falls back')
assert.equal(r.needsDefaultPeriod(null, null, null), true)
assert.equal(r.needsDefaultPeriod('last_7d', null, null), false)
assert.equal(r.needsDefaultPeriod(null, '2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z'), false)
assert.deepEqual(r.selectionParams({ kind: 'period', id: 'last_7d' }), { range: 'last_7d' })
assert.deepEqual(r.selectionParams(custom), { range: 'custom', from: '2026-09-01T00:00:00.000Z', to: '2026-10-01T00:00:00.000Z' })
const now = new Date('2026-10-07T11:05:00Z')
const hour = r.resolveWindow({ kind: 'hour' }, 'UTC', 'monday', now)
assert.equal(hour.to.getTime() - hour.from.getTime(), 3_600_000)
const pm = r.resolveWindow({ kind: 'period', id: 'previous_month' }, 'Asia/Riyadh', 'monday', now)
assert.deepEqual([pm.from.toISOString(), pm.to.toISOString()], ['2026-08-31T21:00:00.000Z', '2026-09-30T21:00:00.000Z'], 'calendar periods in the organisation timezone')
assert.equal(r.selectionLabel({ kind: 'period', id: 'previous_quarter' }), 'Previous quarter')
assert.equal(r.toZonedInput(new Date('2026-10-07T21:30:00Z'), 'Asia/Riyadh'), '2026-10-08T00:30')
assert.equal(r.fromZonedInput('2026-10-08T00:30', 'Asia/Riyadh').toISOString(), '2026-10-07T21:30:00.000Z', 'custom inputs are organisation wall time')
const gap = r.fromZonedInput('2026-03-08T02:30', 'America/New_York') // a wall time skipped by DST
assert.ok(Math.abs(gap.getTime() - Date.parse('2026-03-08T07:00:00Z')) <= 3_600_000, 'DST gap resolves to an adjacent instant')
assert.equal(r.fromZonedInput('garbage', 'UTC'), null)
assert.equal(r.customRangeError(null, now), 'Enter both a start and an end.')
assert.equal(r.customRangeError(now, now), 'The start must be before the end.')
assert.match(r.customRangeError(new Date('2025-01-01T00:00:00Z'), now, now), /at most 400 days/)
assert.equal(r.customRangeError(new Date('2026-10-01T00:00:00Z'), now, now), null)
const sections = [{ id: 'node_availability', scope: 'device' }, { id: 'traffic_top', scope: 'netflow' }, { id: 'rum_pages', scope: 'fleet' }]
assert.deepEqual(r.filterableFor({ filterable: ['devices'] }, sections), ['devices'], 'built-in declaration wins')
assert.deepEqual(r.filterableFor({ entries: [{ id: 'traffic_top' }, { id: 'rum_pages' }, { id: 'node_availability' }] }, sections), ['devices', 'applications', 'netflow'])
assert.deepEqual(r.filterableFor({ sections: ['rum_pages'] }, sections), ['applications'])
assert.deepEqual(r.filterableFor(null, sections), [])
assert.deepEqual(r.parseNodes('a, b,,a'), ['a', 'b'])
assert.deepEqual(r.parseNodes(null), [])

console.log('report render ok')
