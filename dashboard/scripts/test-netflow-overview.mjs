// NetFlow overview (WP B1) pure logic: telemetry insights (NF-23), exporter-health rows and the
// client-clock independence of exporter state (NF-01), comparison pairing (NF-02/X-23), chart
// rows and gaps (X-35), heatmap scale and labels (NF-28), host filter (NF-26), KPI unit split
// (NF-30), rate wording, and rendered components.
// Run from dashboard/: node scripts/test-netflow-overview.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')
const entry = `
export * as M from '@/pages/netflow/overviewModel'
export { splitUnit, KpiCard } from '@/pages/netflow/components/KpiCard'
export { heatCellLabel, TrafficHeatmap } from '@/pages/netflow/components/TrafficHeatmap'
export { TelemetryInsights } from '@/pages/netflow/components/TelemetryInsights'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter } from 'react-router-dom'
export { isExpiredResult, panelStatus, retentionWarnings } from '@/lib/netflow/status'
export { nfWindowFromRange, windowParams, isPendingParams, useNfWindow } from '@/lib/netflow/window'
export { nfParams, paramsKey, toSearchParams } from '@/lib/netflow/api'
export { QueryPanel } from '@/components/netflow/QueryPanel'
export { ThroughputDisplay } from '@/pages/netflow/components/ThroughputDisplay'
`
const aliasPlugin = {
  name: 'app-alias',
  setup(b) {
    b.onResolve({ filter: /^@\// }, (args) => b.resolve(`./${args.path.slice(2)}`, { resolveDir: src, kind: args.kind }))
  },
}
const bundle = await build({
  stdin: { contents: entry, resolveDir: root, loader: 'ts' },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  target: 'es2022',
  jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
  plugins: [aliasPlugin],
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { M, splitUnit, heatCellLabel, createElement: h, renderToStaticMarkup, MemoryRouter } = m
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }

/* ─── Exporter state never uses the browser clock (NF-01, X-24) ────── */
const NOW = Date.parse('2026-09-30T12:08:51Z')
eq(M.receiptState('2026-09-30T12:08:48Z', NOW).state, 'receiving', 'seconds-old receipt is receiving')
eq(M.receiptState('2026-09-30T12:04:00Z', NOW).state, 'late')
eq(M.receiptState('2026-09-30T11:40:00Z', NOW).state, 'silent')
eq(M.receiptState(null, NOW).state, 'never')
// The "now" is the calibrated appliance clock handed in; a browser 12 h ahead changes nothing.
eq(M.receiptState('2026-09-30T12:08:48Z', NOW + 0).ageS, 3)

const statusRow = (exporter, state, extra = {}) => ({
  exporter, display_name: null, device_id: null, device_hostname: null, device_status: null, last_received_at: null,
  receipt_age_s: 30, state, records_15m: 10, expected_interval_s: 60, clock_offset_ms: null, clock_suspect: false,
  export_protocols: ['netflow9'], loss_pct_1h: null, flags_exported: true, ...extra,
})
const traffic = [
  { exporter_ip: '10.0.0.1', bytes: 900, packets: 9, flows: 3, last_seen: null, last_received_at: '2026-09-30T12:08:50Z' },
  { exporter_ip: '10.0.0.2', bytes: 100, packets: 1, flows: 1, last_seen: null, last_received_at: '2026-09-30T11:30:00Z' },
]
let rows = M.exporterHealthRows({
  windowEndsNow: true,
  status: { available: true, rows: [statusRow('10.0.0.1', 'receiving'), statusRow('10.0.0.3', 'silent', { device_hostname: 'core-sw1', device_status: 'up', receipt_age_s: 1380 })] },
  traffic, coverage: null, nowMs: NOW,
})
eq(rows.map((r) => [r.exporter, r.state, r.mode]), [['10.0.0.3', 'silent', 'live'], ['10.0.0.2', 'silent', 'estimated'], ['10.0.0.1', 'receiving', 'live']])
// Historical window: no current state claimed, delivery coverage instead.
rows = M.exporterHealthRows({
  windowEndsNow: false,
  status: { available: true, rows: [statusRow('10.0.0.1', 'silent')] },
  traffic,
  coverage: { exporters: [{ exporter: '10.0.0.1', minutes_with_data: 30, minutes_in_window: 60 }], totals: {} },
  nowMs: NOW,
})
ok(rows.every((r) => r.mode === 'historical' && r.state === null), 'historical rows carry no live state')
eq(rows.find((r) => r.exporter === '10.0.0.1').coveragePct, 50)
// ...but the status service still names the exporter and its device (never its live state).
rows = M.exporterHealthRows({
  windowEndsNow: false,
  status: { available: true, rows: [statusRow('10.0.0.1', 'silent', { display_name: 'edge-1', device_hostname: 'core-sw1', device_status: 'down' }), statusRow('10.0.0.2', 'receiving')] },
  traffic, coverage: null, nowMs: NOW,
})
eq(rows.map((r) => [r.exporter, r.name, r.deviceHostname, r.deviceStatus, r.mapped, r.state]), [['10.0.0.1', 'edge-1', 'core-sw1', null, true, null], ['10.0.0.2', '10.0.0.2', null, null, false, null]])
// Without the status service, mapping is unknown (not "not mapped").
rows = M.exporterHealthRows({ windowEndsNow: false, status: null, traffic, coverage: null, nowMs: NOW })
ok(rows.every((r) => r.mapped === null), 'unknown mapping without the status service')
// Deep-dive: only the page's exporter.
rows = M.exporterHealthRows({ windowEndsNow: true, status: null, traffic, coverage: null, nowMs: NOW, exporter: '10.0.0.2' })
eq(rows.map((r) => r.exporter), ['10.0.0.2'])

/* ─── Telemetry insights (NF-23) ───────────────────────────────────── */
const links = { anomalies: '/netflow/anomalies?range=24h', health: '/netflow/health', exporter: (ip) => `/netflow/devices/${ip}` }
const totals = { bytes: 658.2e9, packets: 1, flows: 1514234, exporters: 1, src_hosts: 1, dst_hosts: 1, last_seen: null, current_bps: 39e6, rate_window_s: 300, top_protocol: { protocol: 50, name: 'ESP', bytes: 4e9 } }
const device = (rst) => ({ rst: { pct: null, status: 'ok', rst_flows: 0, tcp_flows: 0, tcp_flows_with_flags: 0, flag_coverage_pct: null, min_sample: 200, ignored_filters: [], ...rst } })
// Healthy throughput, large flow volume and system-port traffic are context, never counted.
let ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: true, device: device({ pct: 0.2 }), findings: { available: true, data: { counts: { open: 0 } } }, coverage: null, totals, links })
eq(ins.actionable, [], 'nothing actionable')
eq(ins.maxSeverity, null)
ok(ins.context.some((c) => c.id === 'rate') && ins.context.some((c) => c.id === 'volume'), 'rate and volume are context')
ok(!JSON.stringify(ins).includes('100 Mbps') && !JSON.stringify(ins).includes('Healthy'), 'no hard-coded bandwidth verdict')
// Eight silent exporters -> eight actionable items (never capped at six); silence is not device failure.
const silent = Array.from({ length: 8 }, (_, i) => ({ exporter: `10.0.1.${i}`, name: `10.0.1.${i}`, deviceHostname: i === 0 ? 'core-sw1' : null, deviceStatus: i === 0 ? 'up' : null, mode: 'live', state: 'silent', ageS: 1380, clockOffsetMs: null, clockSuspect: false, coveragePct: null, bytes: null, flows: null }))
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: silent, statusAvailable: true, device: null, findings: null, coverage: null, totals: null, links })
eq(ins.actionable.length, 8)
eq(ins.maxSeverity, 'critical')
ok(ins.actionable[0].title.startsWith('Exporter silent: no flows from 10.0.1.0 for 23 min'), ins.actionable[0].title)
ok(ins.actionable[0].body.includes('core-sw1 is up'), 'device reachability shown separately')
ok(!JSON.stringify(ins).includes('Device Offline'), 'never "Device Offline"')
// Three or more of a kind fold into one box; the count stays the number of items.
const blocks = M.groupInsights([...ins.actionable, { id: 'findings', severity: 'warning', title: '2 open anomaly findings', body: '' }])
eq(blocks.map((b) => b.kind), ['group', 'item'])
eq([blocks[0].title, blocks[0].items.length, blocks[0].severity], ['8 exporters silent', 8, 'critical'])
ok(blocks[0].items[0].short.startsWith('10.0.1.0 · no flows for 23 min'), blocks[0].items[0].short)
eq(M.groupInsights(ins.actionable.slice(0, 2)).map((b) => b.kind), ['item', 'item'], 'two stay separate')
// Historical window: no silence insight at all.
ins = M.buildTelemetryInsights({ windowEndsNow: false, health: silent, statusAvailable: null, device: null, findings: null, coverage: null, totals: null, links })
eq(ins.actionable, [])
ok(ins.context.some((c) => c.id === 'historical'))
// RST: only a confident ratio raises; missing flags is "checks unavailable", not healthy.
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: true, device: device({ pct: 7.5, rst_flows: 75, tcp_flows_with_flags: 1000 }), findings: null, coverage: null, totals: null, links })
eq(ins.actionable.map((i) => i.id), ['rst-high'])
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: true, device: device({ pct: null, status: 'flags_unavailable' }), findings: null, coverage: null, totals: null, links })
eq(ins.actionable.map((i) => [i.id, i.title]), [['flags-missing', 'TCP flags are not exported: SYN/RST checks unavailable']])
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: true, device: device({ pct: null, status: 'low_sample' }), findings: null, coverage: null, totals: null, links })
eq(ins.actionable, [])
// Findings: counts come from the findings service and link to Anomalies; unavailable is said.
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: true, device: null, findings: { available: true, data: { counts: { open: 3, by_severity: { high: 1 }, by_severity_open: { high: 1 } } } }, coverage: null, totals: null, links })
eq(ins.actionable.map((i) => [i.id, i.severity, i.link.to]), [['findings', 'critical', links.anomalies]])
// "N open, X of them high or critical" counts open findings only: an acknowledged critical
// (in the active by_severity) does not make the open-findings insight critical.
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: true, device: null, findings: { available: true, data: { counts: { open: 2, by_severity: { critical: 1, low: 2 }, by_severity_open: { low: 2 } } } }, coverage: null, totals: null, links })
eq(ins.actionable.map((i) => [i.id, i.severity]), [['findings', 'warning']])
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: false, device: null, findings: { available: false, data: null }, coverage: null, totals: null, links })
ok(ins.context.some((c) => c.id === 'findings-unavailable') && ins.context.some((c) => c.id === 'status-unavailable'))
// Coverage below 90% is actionable.
ins = M.buildTelemetryInsights({ windowEndsNow: true, health: [], statusAvailable: true, device: null, findings: null, coverage: { exporters: [], totals: { minutes_with_data: 30, minutes_in_window: 60 } }, totals: null, links })
eq(ins.actionable.map((i) => [i.id, i.severity]), [['coverage', 'info']])

/* ─── Comparison pairing and chart rows (NF-02, X-23, X-35) ────────── */
const pt = (i, ts, state, bps, extra = {}) => ({ i, ts, covered_s: 60, partial: false, open: false, bps, bytes: bps == null ? null : bps * 7.5, packets: 1, flows: 1, state, ...extra })
const cur = [pt(0, 1000_000, 'data', 800), pt(1, 1060_000, 'gap', null), pt(2, 1120_000, 'idle', 0), pt(3, 1180_000, 'data', 400, { open: true, partial: true, covered_s: 20 })]
// Prior buckets have their own timestamps (a 7h03m span is not a bucket multiple): pairing is by index.
const prior = [pt(0, 600_000 + 33_000, 'data', 100), pt(1, 660_000 + 33_000, 'data', 200), pt(3, 780_000 + 33_000, 'unknown', null)]
const chart = M.chartRows(cur, 60, 1200_000, prior)
eq(chart.map((r) => r.prior_bps), [100, 200, null, null])
eq(chart.map((r) => r.prior_ts), [633_000, 693_000, null, 813_000])
eq(chart.map((r) => r.bps), [800, null, 0, 400], 'gap is null, idle is zero')
eq(chart[3].xEnd, 1200_000, 'last bucket clamped to the window end')
eq(M.stateSpans(chart, ['gap']), [{ from: 1060_000, to: 1120_000, state: 'gap' }])
const sum = M.summarizeSeries(cur)
eq([sum.peakBps, sum.gaps, sum.idle], [800, 1, 1])
eq(Math.round(sum.avgBps), Math.round((800 * 60 + 0 * 60 + 400 * 20) / 140))
eq(M.formatDelta(12.5), '+12.5%')
eq(M.formatDelta(-3.14), '−3.1%')
eq(M.formatDelta(null), '—')
ok(M.deltaReasonText('prior_low_coverage').includes('less than half'))
// Explainer wording (CR-15): signed byte changes; an item against the total's direction offsets it.
eq(M.signedBytes(10.5e9), '+10.5 GB')
eq(M.signedBytes(-123e6), '−123 MB')
eq(M.shareOfChangeText(102.5), '102.5% of the change')
eq(M.shareOfChangeText(-1.2), 'offsets 1.2% of the change')
eq(M.shareOfChangeText(null), null)

/* ─── Comparison: prior-only buckets stay visible (NF-02) ─────────── */
// 10:02→17:05 at 5-min buckets: the current grid has 85 buckets, the prior grid (02:59→10:02) 86.
const B5 = 300
const T = (iso) => Date.parse(iso)
const curPts = Array.from({ length: 85 }, (_, i) => pt(i, T('2026-09-29T10:00:00Z') + i * B5 * 1000, 'data', 1000, { covered_s: i === 0 ? 180 : 300, partial: i === 0 }))
const priorPts = Array.from({ length: 86 }, (_, i) => pt(i, T('2026-09-29T02:55:00Z') + i * B5 * 1000, i === 85 ? 'data' : 'data', 500, { covered_s: i === 0 ? 60 : i === 85 ? 120 : 300, partial: i === 0 || i === 85 }))
const endMs = T('2026-09-29T17:05:00Z')
const tail = M.chartRows(curPts, B5, endMs, priorPts)
eq(tail.length, 86, 'the extra prior bucket gets its own row')
eq(tail.filter((r) => r.prior_ts != null).length, 86, 'every prior bucket is plotted and listed')
const last = tail[tail.length - 1]
eq([last.state, last.bps, last.prior_bps, last.prior_ts, last.x], ['prior_only', null, 500, T('2026-09-29T10:00:00Z'), endMs], 'prior-only row sits at its index position, clamped to the window end')
ok(tail.every((r, i) => i === 0 || r.x >= tail[i - 1].x), 'rows stay in time order')
eq(M.stateSpans(tail, ['gap', 'unknown', 'expired']), [], 'a prior-only row is not shaded as a gap')
// 03:00→10:00: the prior period's only data bucket has null neighbours; it is drawn as a dot.
const curGap = Array.from({ length: 84 }, (_, i) => pt(i, T('2026-09-29T03:00:00Z') + i * B5 * 1000, 'data', 900))
const priorGap = Array.from({ length: 84 }, (_, i) => pt(i, T('2026-09-28T20:00:00Z') + i * B5 * 1000, i === 83 ? 'data' : 'gap', i === 83 ? 700 : null))
const gapRows = M.chartRows(curGap, B5, T('2026-09-29T10:00:00Z'), priorGap)
const isoPrior = M.isolatedRows(gapRows, 'prior_bps')
eq(isoPrior.filter(Boolean).length, 1, 'the isolated prior bucket is marked for a dot')
ok(isoPrior[83] && gapRows[83].prior_bps === 700)
eq(M.isolatedRows(gapRows, 'bps').filter(Boolean).length, 0, 'a continuous current series needs no dots')

/* ─── Peak and scale skip short and filling buckets ────────────────── */
const skew = [
  ...Array.from({ length: 58 }, (_, i) => pt(i + 1, 1000_000 + (i + 1) * 60_000, 'data', 600e6 + (i % 5) * 10e6)),
  pt(0, 1000_000, 'data', 900e6, { partial: true, covered_s: 12 }),
  pt(59, 1000_000 + 59 * 60_000, 'data', 1566.9e6, { partial: true, open: true, covered_s: 7 }),
]
const sk = M.summarizeSeries(skew, 60)
eq([sk.peakBps, sk.peakExcluded, sk.peakFromShort], [640e6, 2, false], 'open bucket (7 s) and short edge (12 s) leave the peak')
eq(M.maxForScale([skew], 60), 640e6, 'axis scale follows complete buckets')
ok(sk.avgBps > 600e6 && sk.avgBps < 650e6, 'average still weights every bucket by its seconds')
const halfEdge = M.summarizeSeries([pt(0, 0, 'data', 50, { partial: true, covered_s: 40 }), pt(1, 60_000, 'data', 10)], 60)
eq([halfEdge.peakBps, halfEdge.peakExcluded], [50, 0], 'an edge bucket covering at least half a bucket counts')
const onlyShort = M.summarizeSeries([pt(0, 0, 'data', 70, { open: true, partial: true, covered_s: 5 })], 60)
eq([onlyShort.peakBps, onlyShort.peakFromShort], [70, true], 'with nothing else, the short bucket is still shown, flagged')
eq(M.maxForScale([[pt(0, 0, 'gap', null)]], 60), null)

/* ─── Unknown rate (no deliveries) is not zero ─────────────────────── */
eq(M.rateUnknownText('no_receipts', true, 300), 'No records received in the last 5 min')
eq(M.rateUnknownText('no_receipts', false, 300), 'No records received in the last 5 min of the window')
eq(M.rateUnknownText('beyond_retention', true, 300), 'Raw records for this moment have expired')
eq(M.rateUnknownText('ok', true, 300), null)
const unknownRate = renderToStaticMarkup(h(m.ThroughputDisplay, { bps: null, label: 'Rate, last 5 min', unknownReason: 'No records received in the last 5 min' }))
ok(unknownRate.includes('Unknown') && unknownRate.includes('no records received') && !unknownRate.includes('0 bps'), 'headline says Unknown, never 0 bps')

/* ─── Expired by retention is not "no flows matched" (X-25) ────────── */
const expiredEnv = { data: { bytes: null }, meta: { window: { effective_from: '2026-08-24T13:43:30Z', effective_to: '2026-08-24T13:43:30Z' }, retention: { limited: true }, warnings: [{ code: 'raw_retention', message: 'The DSCP filter needs raw flow records, which are kept for 30 days; the part of the range before that is unknown for this filter, not zero' }] } }
ok(m.isExpiredResult(expiredEnv) && !m.isExpiredResult({ ...expiredEnv, meta: { ...expiredEnv.meta, retention: { limited: false } } }))
ok(!m.isExpiredResult({ data: [], meta: { window: { effective_from: '2026-08-24T10:00:00Z', effective_to: '2026-08-24T13:43:30Z' }, retention: { limited: true }, warnings: [] } }), 'a partly retained window is not expired')
const q = (data) => ({ data, error: null, status: 'success' })
eq(m.panelStatus(q(expiredEnv), { isEmpty: () => true }), 'expired', 'expired wins over the empty check')
const expiredHtml = renderToStaticMarkup(h(m.QueryPanel, { query: q(expiredEnv), isEmpty: () => true, children: () => 'DATA' }))
ok(expiredHtml.includes('older than the data kept') && expiredHtml.includes('DSCP filter needs raw flow records') && !expiredHtml.includes('No flows matched'), 'panel names the reason')

/* ─── Hour/day filters: window membership and zone switch (NF-28) ──── */
const sun22 = { hour: 22, dow: 7 }
ok(M.windowHasHourDow(T('2026-09-27T00:00:00Z'), T('2026-09-29T00:00:00Z'), 'UTC', sun22.hour, sun22.dow), 'Sat 00:00 → Mon 00:00 UTC holds Sunday 22:00 UTC')
ok(!M.windowHasHourDow(T('2026-09-28T00:00:00Z'), T('2026-09-29T00:00:00Z'), 'UTC', sun22.hour, sun22.dow), 'a Monday does not')
ok(M.windowHasHourDow(T('2026-09-27T22:55:00Z'), T('2026-09-27T23:30:00Z'), 'UTC', 22, 7), 'a window starting inside the hour holds it')
ok(M.windowHasHourDow(T('2026-09-27T21:50:00Z'), T('2026-09-27T22:05:00Z'), 'UTC', 22, null), 'a window ending just after the hour starts holds it')
ok(!M.windowHasHourDow(T('2026-09-27T19:00:00Z'), T('2026-09-27T22:00:00Z'), 'UTC', 22, null), 'half-open: ending at 22:00 does not')
ok(M.windowHasHourDow(T('2026-09-27T19:00:00Z'), T('2026-09-27T20:00:00Z'), 'Asia/Riyadh', 22, 7), 'evaluated in the filter zone (19:00 UTC = 22:00 Riyadh)')
eq(M.rezoneHourDow(sun22, 'UTC', 'Asia/Riyadh', T('2026-09-29T00:00:00Z')), { hour: 1, dow: 1 }, 'Sunday 22:00 UTC is Monday 01:00 Riyadh')
eq(M.rezoneHourDow({ hour: 1, dow: 1 }, 'Asia/Riyadh', 'UTC', T('2026-09-29T00:00:00Z')), { hour: 22, dow: 7 })
eq(M.rezoneHourDow({ hour: 5 }, 'UTC', 'Asia/Riyadh', T('2026-09-29T00:00:00Z')), { hour: 8 })
eq(M.rezoneHourDow(sun22, 'UTC', 'Asia/Kolkata', T('2026-09-29T00:00:00Z')), null, 'a half-hour zone has no exact equivalent')
eq(M.rezoneHourDow({ dow: 7 }, 'UTC', 'Asia/Riyadh', T('2026-09-29T00:00:00Z')), null, 'a whole day spans two days in another zone')
eq(M.rezoneHourDow({ dow: 7 }, 'UTC', 'UTC', T('2026-09-29T00:00:00Z')), { hour: undefined, dow: 7 })
eq(M.rezoneHourDow({}, 'UTC', 'Asia/Kolkata', 0), {})

/* ─── Calendar ranges wait for the appliance clock ─────────────────── */
const pendingW = m.nfWindowFromRange({ key: 'last_month', fromISO: '2026-07-31T21:00:00.000Z', toISO: '2026-08-31T21:00:00.000Z', hours: 744, label: 'Last month', isCustom: false, pending: true }, 'Asia/Riyadh')
ok(pendingW.pending === true && m.isPendingParams(m.windowParams(pendingW)) && m.isPendingParams(m.nfParams(pendingW, { protocol: 6 })), 'pending window marks its request parameters')
eq(m.toSearchParams(m.nfParams(pendingW)).toString(), 'from=2026-07-31T21%3A00%3A00.000Z&to=2026-08-31T21%3A00%3A00.000Z&tz=Asia%2FRiyadh', 'the mark is never sent')
const finalW = m.nfWindowFromRange({ key: 'last_month', fromISO: '2026-08-31T21:00:00.000Z', toISO: '2026-09-30T21:00:00.000Z', hours: 720, label: 'Last month', isCustom: false }, 'Asia/Riyadh')
ok(!finalW.pending && !m.isPendingParams(m.nfParams(finalW)), 'a calibrated calendar window queries')
// The page hook carries the pending state from the URL range (server render: clock not calibrated).
const probe = (url) => {
  const Probe = () => {
    const { window: w } = m.useNfWindow()
    return h('i', null, JSON.stringify({ key: w.key, pending: !!w.pending, gated: m.isPendingParams(m.nfParams(w)) }))
  }
  const html = renderToStaticMarkup(h(MemoryRouter, { initialEntries: [url] }, h(Probe)))
  return JSON.parse(html.replace(/^<i>|<\/i>$/g, '').replace(/&quot;/g, '"'))
}
eq(probe('/netflow?range=last_month'), { key: 'last_month', pending: true, gated: true }, 'useNfWindow keeps a calendar range pending until the clock is calibrated')
eq(probe('/netflow?range=24h'), { key: '24h', pending: false, gated: false })
eq(probe('/netflow?range=custom&from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z'), { key: 'custom', pending: false, gated: false })
ok(!m.isPendingParams(m.nfParams(m.nfWindowFromRange({ key: '24h', fromISO: '2026-09-29T00:00:00.000Z', toISO: '2026-09-30T00:00:00.000Z', hours: 24, label: 'Last 24h', isCustom: false, pending: true }))), 'relative windows are resolved by the server and never wait')

/* ─── Time axis in the viewer's zone ───────────────────────────────── */
const from = Date.parse('2026-09-30T00:00:00Z')
const riyadh = M.timeTicks(from, from + 24 * 3600e3, 'Asia/Riyadh')
ok(riyadh.length >= 4 && riyadh.length <= 7, 'a handful of ticks')
ok(riyadh.every((t) => (t + 3 * 3600e3) % (3 * 3600e3) === 0 || (t + 3 * 3600e3) % (6 * 3600e3) === 0), 'ticks sit on local boundaries')
eq(M.formatTick(Date.parse('2026-09-30T21:00:00Z'), 24 * 3600e3, 'Asia/Riyadh'), '00:00')
const kolkata = M.timeTicks(from, from + 6 * 3600e3, 'Asia/Kolkata')
ok(kolkata.every((t) => ((t + 5.5 * 3600e3) / 60000) % 60 === 0), 'half-hour zone ticks on local hours')
eq(M.tzOffsetMs(from, 'Asia/Riyadh'), 3 * 3600e3)

/* ─── Heatmap scale and labels (NF-28) ─────────────────────────────── */
eq(M.heatBin(0, 100), -1)
eq(M.heatBin(null, 100), -1)
eq(M.heatBin(1, 100), 0)
eq(M.heatBin(100, 100), M.HEAT_STEPS - 1)
eq(M.heatBin(50, 100), 3)
eq(M.heatLegend(600).map((b) => b.lo), [0, 100, 200, 300, 400, 500])
eq([...M.HOUR_TICKS], [0, 6, 12, 18, 23])
eq(M.HEAT_COLORS.light.length, M.HEAT_STEPS)
ok(M.HEAT_COLORS.light.every((c) => /^#[0-9a-f]{6}$/.test(c)), 'sequential single-hue ramp')
const cell = { dow: 1, hour: 1, bytes: 6000, flows: 60, value: 6000, occurrences: 1, covered_occurrences: 1, dates_with_data: 1, state: 'data' }
eq(heatCellLabel(cell, 'avg', 'Asia/Riyadh'), 'Monday 01:00–01:59 (Asia/Riyadh): 6.00 kB per hour on average, traffic in every occurrence (1 of 1 hour with records)')
ok(heatCellLabel({ ...cell, value: null, state: 'no_coverage' }, 'avg', 'UTC').includes('no records received (collection gap)'))

/* ─── Host filter (NF-26) and rate wording ─────────────────────────── */
eq(M.parseHostFilter(' 10.0.0.5 '), { ok: true, value: '10.0.0.5' })
eq(M.parseHostFilter('10.0.0.0/24'), { ok: true, value: '10.0.0.0/24' })
eq(M.parseHostFilter('2001:DB8::1'), { ok: true, value: '2001:db8::1' })
eq(M.parseHostFilter(''), { ok: true, value: null })
ok(!M.parseHostFilter('invalid-ip').ok && !M.parseHostFilter('10.0.0.1/33').ok)
eq(M.rateLabel(true, 300).label, 'Rate, last 5 min')
eq(M.rateLabel(false, 300).label, 'Rate at end of window')

/* ─── KPI values (NF-30) ───────────────────────────────────────────── */
eq(splitUnit('613.0 GB'), ['613.0', 'GB'])
eq(splitUnit('1.5M'), ['1.5M', ''])
eq(splitUnit('17 of 19'), ['17', 'of 19'])
eq(splitUnit('<1 bps'), ['<1', 'bps'])
eq(splitUnit('—'), ['—', ''])
const kpi = renderToStaticMarkup(h(m.KpiCard, { icon: () => null, label: 'Flow records', value: '1.5M', title: '1,514,234 flow records' }))
ok(!kpi.includes('truncate') && kpi.includes('1,514,234 flow records'), 'KPI keeps the exact value, never truncates')

/* ─── Rendered insights: count = actionable, neutral when none ─────── */
const html = renderToStaticMarkup(h(MemoryRouter, null, h(m.TelemetryInsights, {
  insights: { actionable: [], context: [{ id: 'rate', severity: 'info', title: 'Rate 39.0 Mbps', body: 'x' }], maxSeverity: null },
  links: { anomalies: '/a', alerts: '/alerts', health: '/h' },
})))
ok(html.includes('Telemetry insights') && html.includes('0 actionable') && !html.includes('Alerts / Incidents'))
ok(html.includes('not a security clearance'), 'an empty list never reads as all-clear')

const silentAll = M.buildTelemetryInsights({ windowEndsNow: true, health: silent, statusAvailable: true, device: null, findings: null, coverage: null, totals: null, links })
const grouped = renderToStaticMarkup(h(MemoryRouter, null, h(m.TelemetryInsights, { insights: silentAll, links: { anomalies: '/a', alerts: '/alerts', health: '/h' } })))
ok(grouped.includes('8 actionable') && grouped.includes('8 exporters silent'), 'grouped box keeps the full count')
eq((grouped.match(/href="\/netflow\/devices\//g) ?? []).length, 8, 'one exporter link per grouped item')

console.log(`test-netflow-overview: ${checks} checks passed`)
