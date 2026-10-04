// NetFlow capacity page (WP B4) pure logic: headroom as remaining capacity with over-capacity kept
// at its real value and unknown speed never drawn (NF-15), the coverage sentence and the next
// action for every resolution state with permission gating (NF-16), where a speed change is
// written, speed parsing, sample/peak labelling (NF-13), change and forecast wording (CR-09),
// billing month defaults (CR-08), and drill-down links that carry the whole filter (FE-02).
// Run from dashboard/: node scripts/test-netflow-capacity.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')
const entry = `
export * as M from '@/pages/netflow/capacity/model'
export * as L from '@/lib/netflow/interfaceLinks'
export { parseFlowFilter } from '@/lib/netflow/filters'
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
  loader: { '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [aliasPlugin],
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { M, L, parseFlowFilter } = m
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }

/* ─── Headroom (NF-15) ─────────────────────────────────────────────── */
{
  const h = (o) => ({ utilization_pct: null, headroom_bps: null, headroom_pct: null, value_bps: null, state: 'unknown', ...o })
  const healthy = M.headroomView(h({ value_bps: 2e8, utilization_pct: 20, headroom_bps: 8e8, headroom_pct: 80, state: 'healthy' }))
  eq(healthy.kind, 'ok')
  eq(healthy.barPct, 80, 'the bar is the REMAINING share: 20 % used draws an 80 % bar')
  ok(healthy.text.includes('800 Mbps free') && healthy.text.includes('80%'), healthy.text)
  ok(healthy.title.includes('20% used'), 'the title names the used share too')

  const over = M.headroomView(h({ value_bps: 1.394e7, utilization_pct: 139.4, headroom_bps: -3.94e6, headroom_pct: -39.4, state: 'over' }))
  eq(over.kind, 'over')
  eq(over.barPct, null, 'over capacity draws no (clamped) bar')
  eq(over.text, 'Over by 3.94 Mbps', 'over shows its real excess')
  eq(over.sub, '139.4% of the speed', 'and its real utilization, never clamped to 100 %')
  ok(/duplicate/.test(over.title) && /sampling/.test(over.title), 'over explains what to check')

  const unknown = M.headroomView(h({ value_bps: 5e6, state: 'unknown' }))
  eq([unknown.kind, unknown.barPct, unknown.text], ['unknown', null, 'Unknown'], 'unknown speed: no bar, no percentage')

  eq(M.headroomView(h({ state: 'insufficient' })).text, 'No samples')
  const thin = M.headroomView(h({ value_bps: 1e6, utilization_pct: 1, headroom_bps: 9.9e7, headroom_pct: 99, state: 'insufficient' }))
  eq([thin.kind, thin.barPct, thin.text], ['insufficient', null, 'Too little data'], 'low coverage never shows a reassuring 99 % bar')

  const full = M.headroomView(h({ value_bps: 1e9, utilization_pct: 100, headroom_bps: 0, headroom_pct: 0, state: 'critical' }))
  eq([full.kind, full.barPct], ['ok', 0], 'exactly full is not over')
}

/* ─── States and labels ────────────────────────────────────────────── */
eq(M.STATE_LABELS.insufficient, 'Insufficient data')
eq(M.stateTone('over'), 'danger')
eq(M.stateTone('critical'), 'danger')
eq(M.stateTone('warning'), 'warning')
eq(M.stateTone('healthy'), 'success')
eq(M.stateTone('unknown'), 'unknown', 'unknown speed is never a healthy tone')
eq(M.stateTone('insufficient'), 'unknown')
for (const s of M.STATES) ok(/-fg$|text-text2/.test(M.stateTextClass(s)), `state text uses *-fg tokens (${s})`)
eq(M.statisticLabel('max'), 'Peak')
eq(M.statisticLabel('p95'), 'p95')

/* ─── Samples and peak labelling (NF-13) ───────────────────────────── */
{
  const row = { samples: 2015, expected_slots: 2016, coverage_pct: 99.95, headline: { statistic: 'p95', reason: null } }
  eq(M.samplesText(row), 'p95 of 2,015 samples · 100% coverage')
  eq(M.samplesShort(row), { count: '2,015 of 2,016', coverage: '100% coverage' })
  const peak = { samples: 78, expected_slots: 2016, coverage_pct: 3.869, headline: { statistic: 'max', reason: 'low_coverage' } }
  eq(M.samplesText(peak), 'Peak of 78 samples · 3.9% coverage (collection coverage too low for a percentile)')
  eq(M.peakNote(peak.headline), 'Peak: coverage too low for a percentile')
  eq(M.peakNote({ statistic: 'max', reason: 'too_few_samples' }), 'Peak: too few samples for a percentile')
  eq(M.peakNote({ statistic: 'p95', reason: null }), null, 'a stated percentile needs no note')
  eq(M.samplesShort({ samples: 0, expected_slots: 12, coverage_pct: null }).coverage, 'coverage unknown')
  eq(M.pReasonText('something_new'), 'something new')
  eq(M.pReasonText(null), null)
}

/* ─── Coverage sentence and next actions (NF-16) ───────────────────── */
{
  const br = (o) => ({ resolved: 0, exporter_unmapped: 0, no_interface_inventory: 0, interface_not_found: 0, speed_unknown: 0, ...o })
  eq(
    M.coverageSentence({ interfaces_total: 59, speed_known: 11, by_resolution: br({ resolved: 11, exporter_unmapped: 41, no_interface_inventory: 5, interface_not_found: 1, speed_unknown: 1 }) }),
    '48 of 59 interfaces lack a known capacity: 41 exporter not linked, 5 devices without interface inventory, 1 interface not in inventory, 1 speed unknown',
  )
  eq(M.coverageSentence({ interfaces_total: 3, speed_known: 3, by_resolution: br({ resolved: 3 }) }), 'All 3 interfaces have a known capacity')
  eq(M.coverageSentence({ interfaces_total: 1, speed_known: 0, by_resolution: br({ speed_unknown: 1 }) }), '1 of 1 interface lacks a known capacity: 1 speed unknown')
  eq(M.coverageSentence({ interfaces_total: 0, speed_known: 0, by_resolution: br({}) }), 'No interfaces carried flow traffic in this period')

  const none = { manage: false, device_speed: false }
  const all = { manage: true, device_speed: true }
  const opOnly = { manage: false, device_speed: true }
  const row = (resolution, extra = {}) => ({ resolution, device_id: 'd1', interface_in_inventory: true, ...extra })
  eq(M.resolutionAction(row('exporter_unmapped', { device_id: null }), all).kind, 'link_exporter')
  eq(M.resolutionAction(row('exporter_unmapped', { device_id: null }), none).disabled, true, 'linking needs netflow.manage')
  eq(M.resolutionAction(row('no_interface_inventory'), none).kind, 'open_device')
  eq(M.resolutionAction(row('no_interface_inventory'), none).disabled, false, 'opening a device needs no write permission')
  eq(M.resolutionAction(row('interface_not_found'), none).kind, 'open_interfaces')
  eq(M.resolutionAction(row('speed_unknown'), opOnly), { kind: 'set_speed', label: 'Set speed', hint: 'SNMP reports no speed for this interface. Set the circuit speed.', disabled: false })
  eq(M.resolutionAction(row('speed_unknown'), none).disabled, true)
  eq(M.resolutionAction(row('resolved'), all).kind, 'none')
  for (const r of ['exporter_unmapped', 'no_interface_inventory', 'interface_not_found']) {
    ok(/override/.test(M.resolutionAction(row(r), all).hint), `${r}: the hint offers the NetFlow override fallback`)
  }

  eq(M.speedTargets(row('speed_unknown'), all), ['device', 'netflow'], 'inventoried + both permissions: both targets')
  eq(M.speedTargets(row('interface_not_found', { interface_in_inventory: false }), all), ['netflow'], 'not inventoried: override only')
  eq(M.speedTargets(row('exporter_unmapped', { device_id: null, interface_in_inventory: false }), opOnly), [], 'nothing writable')
  eq(M.speedTargets(row('speed_unknown'), opOnly), ['device'])
}

/* ─── Speed input ──────────────────────────────────────────────────── */
eq(M.parseSpeed('100', 'Mbps'), 100_000_000)
eq(M.parseSpeed(' 1.5 ', 'Gbps'), 1_500_000_000)
eq(M.parseSpeed('', 'Mbps'), null, 'empty clears')
eq(M.parseSpeed('0', 'Mbps'), 'The speed must be above zero')
eq(M.parseSpeed('-5', 'Mbps'), 'Enter a number, for example 100')
eq(M.parseSpeed('1e3', 'Mbps'), 'Enter a number, for example 100')
eq(M.parseSpeed('2000000', 'Gbps'), 'The speed is too large')
eq(M.speedInput(1e10), { value: '10', unit: 'Gbps' })
eq(M.speedInput(1e8), { value: '100', unit: 'Mbps' })
eq(M.speedInput(1_544_000), { value: '1.544', unit: 'Mbps' })
eq(M.speedInput(64_000), { value: '64', unit: 'kbps' })
eq(M.speedInput(null), { value: '', unit: 'Mbps' })
for (const bps of [1e10, 2.5e9, 1e8, 1_544_000, 64_000, 512_000]) {
  const s = M.speedInput(bps)
  eq(M.parseSpeed(s.value, s.unit), bps, `speedInput/parseSpeed round-trip ${bps}`)
}

/* ─── Change and forecast wording (CR-09) ──────────────────────────── */
eq(M.changeText(8.4), '+8.4%')
eq(M.changeText(-12.25), '−12.3%')
eq(M.changeText(0.01), '±0%')
eq(M.changeText(null), '—', 'no comparable previous value is unknown, not 0 %')
eq(M.daysText(0), 'Now')
eq(M.daysText(0.4), '< 1 day')
eq(M.daysText(16.2), '16 days')
eq(M.daysText(520), '~17 months')
eq(M.daysText(null), '—')
{
  const fc = (o) => ({ status: 'ok', reason: null, days_used: 40, min_days: 14, slope_bps_per_day: 1e5, r2: 0.9, confidence: 'high', fitted_now_bps: 5e7, days_to_warn: null, days_to_crit: null, days_to_full: null, ...o })
  eq(M.forecastHeadline(fc({ days_to_warn: 0, days_to_crit: 16, days_to_full: 28 })), 'Already above warning')
  eq(M.forecastHeadline(fc({ days_to_warn: 0, days_to_crit: 0, days_to_full: 3 })), 'Already above critical')
  eq(M.forecastHeadline(fc({ days_to_warn: 0, days_to_crit: 0, days_to_full: 0 })), 'Already at capacity')
  eq(M.forecastHeadline(fc({ days_to_warn: 520, days_to_crit: 720 })), 'Warning in ~17 months')
  eq(M.forecastHeadline(fc({ status: 'insufficient_history', reason: '9 qualifying days; a forecast needs at least 14' })), 'Insufficient history')
  eq(M.forecastHeadline(fc({ status: 'not_growing' })), 'Not growing')
  eq(M.forecastHeadline(fc({ status: 'speed_unknown' })), 'Speed unknown')
  eq(M.growthText({ slope_bps_per_day: 1e5 }), '+3.00 Mbps per month')
  eq(M.growthText({ slope_bps_per_day: -2e4 }), '−600 kbps per month')
  eq(M.growthText({ slope_bps_per_day: null }), '—')
}

/* ─── Billing month and business hours (CR-08) ─────────────────────── */
{
  const t = Date.UTC(2026, 9, 1, 3, 0) // 2026-10-01 03:00 UTC
  eq(M.lastMonth(t, 'UTC'), '2026-09')
  eq(M.lastMonth(t, 'America/New_York'), '2026-08', 'still September in New York: the previous month is August')
  eq(M.lastMonth(Date.UTC(2027, 0, 15), 'UTC'), '2026-12', 'January wraps to December of the previous year')
  eq(M.businessHoursText({ enabled: true, start_hour: 8, end_hour: 18, days: [1, 2, 3, 4, 5], tz: 'Asia/Riyadh' }), 'Mon, Tue, Wed, Thu, Fri 08:00–18:00 (Asia/Riyadh)')
  eq(M.businessHoursText({ enabled: true, start_hour: 22, end_hour: 24, days: [7], tz: 'UTC' }), 'Sun 22:00–00:00 (UTC)')
}

/* ─── Drill-down links carry the whole filter (FE-02) ──────────────── */
{
  const w = { key: '7d', mode: 'relative', tz: 'UTC', hours: 168 }
  const filter = parseFlowFilter(new URLSearchParams('talker=10.1.0.0/16&protocol=6&dscp=0&exporter=127.0.0.81'))
  const links = M.interfaceLinks({ exporter: '127.0.0.81', ifindex: 5 }, filter, w)
  const ov = new URLSearchParams(links.overview.split('?')[1])
  ok(links.overview.startsWith('/netflow?'), links.overview)
  eq(ov.get('iface'), '5@127.0.0.81', 'qualified interface')
  eq([ov.get('talker'), ov.get('protocol'), ov.get('dscp'), ov.get('exporter')], ['10.1.0.0/16', '6', '0', '127.0.0.81'], 'every page filter key is kept (DSCP 0 included)')
  eq(ov.get('range'), '7d', 'the window travels with the link')
  const fx = new URLSearchParams(links.forensics.split('?')[1])
  ok(links.forensics.startsWith('/netflow/forensics?'), links.forensics)
  eq([fx.get('iface'), fx.get('run'), fx.get('talker')], ['5@127.0.0.81', '1', '10.1.0.0/16'])
  const other = M.interfaceLinks({ exporter: '127.0.0.82', ifindex: 3 }, filter, w)
  eq(new URLSearchParams(other.overview.split('?')[1]).get('exporter'), null, 'a different exporter filter would select nothing: dropped')
  eq(filter.exporter, '127.0.0.81', 'the page filter object is not mutated')
}

/* ─── C-fix capacity-breakdowns regressions ───────────────────────── */

/* NF-16: a speed known in one direction only is never "healthy" headroom: 30 Mbps in, out unknown,
   15 Mbps each way (Gi1/8). The API now calls it 'unknown'; the headroom names the missing side. */
{
  const partial = M.headroomView({ value_bps: 15e6, utilization_pct: 50, headroom_bps: 15e6, headroom_pct: 50, state: 'unknown',
    direction: 'in', partial_speed: true, unknown_directions: ['out'] })
  eq(partial.kind, 'unknown', 'partial speed is not an ok headroom')
  eq(partial.barPct, null, 'no remaining-capacity bar')
  eq(partial.text, 'Out speed unknown')
  eq(partial.sub, 'In: 15.0 Mbps free (50%)', 'the known side is a detail, not the verdict')
  ok(!/healthy/i.test(partial.text + partial.title))
  // A measured warning on the known side stays a warning with its headroom.
  const warn = M.headroomView({ value_bps: 24e6, utilization_pct: 80, headroom_bps: 6e6, headroom_pct: 20, state: 'warning',
    direction: 'in', partial_speed: true, unknown_directions: ['out'] })
  eq(warn.kind, 'ok')
  eq(M.unknownDirectionsText(['in']), 'In')
  eq(M.unknownDirectionsText(['in', 'out']), 'In and out')
}

/* SNMP cross-check: the API delta is (flow − SNMP) / SNMP, so the words say "flow is X% vs SNMP".
   127.0.0.81: flow in 73.2 vs SNMP 74.9 (−2.2%), flow out 17.0 vs SNMP 13.2 (+28.5%). */
{
  const snmp = { status: 'ok', statistic: 'p95', samples: 2016, in_p_bps: 74.9e6, out_p_bps: 13.2e6, in_delta_pct: -2.2, out_delta_pct: 28.5 }
  const sentence = M.snmpSentence(snmp)
  eq(sentence, 'SNMP counters (p95): in 74.9 Mbps, out 13.2 Mbps; flow is −2.2% (in) and +28.5% (out) vs SNMP.')
  ok(!/vs flow/.test(sentence), 'never phrased as SNMP relative to flow')
  const cc = M.crossCheckText(snmp)
  eq(cc.text, 'Flow +28.5% vs SNMP', 'the table cell names what is compared to what')
  ok(cc.title.includes('flow is −2.2% (in) and +28.5% (out) vs SNMP'), cc.title)
  eq(M.snmpSentence({ status: 'no_data' }), null)
}

/* Override shadowed by a configured circuit speed: Gi1/6 configured 50 Mbps, override 200/200. */
{
  const speed = { configured_bps: 50e6, override_in_bps: 200e6, override_out_bps: 200e6, override_shadowed: ['in', 'out'] }
  eq(M.overrideShadowNote(speed), 'NetFlow override (in 200 Mbps, out 200 Mbps) not in use: the device circuit speed 50.0 Mbps takes precedence')
  eq(M.overrideShadowNote({ ...speed, override_shadowed: [] }), null, 'nothing shadowed: no note')
  ok(/Not in use while the device circuit speed \(50\.0 Mbps\) is set/.test(M.overrideFormNote(50e6)), 'the override form warns before saving')
  eq(M.overrideFormNote(null), null)
}

/* FE-05 speed history: bulk changes and override entries print their values. */
{
  eq(M.speedChangeText({ at: null, action: 'device_interface.speed.bulk_update', source: 'configured', in: { old: 20e6, new: 30e6 }, out: { old: 20e6, new: 30e6 }, old: 20e6, new: 30e6, actor: 'admin', changes: null }),
    'circuit speed: 20.0 Mbps → 30.0 Mbps (bulk change)')
  eq(M.speedChangeText({ at: null, action: 'device_interface.speed.update', source: 'configured', in: { old: null, new: 20e6 }, out: { old: null, new: 20e6 }, old: null, new: 20e6, actor: null, changes: null }),
    'circuit speed: none → 20.0 Mbps')
  eq(M.speedChangeText({ at: null, action: 'netflow.interface_override.create', source: 'override', in: { old: null, new: 50e6 }, out: { old: null, new: 20e6 }, old: null, new: null, actor: null, changes: {} }),
    'NetFlow override: in none → 50.0 Mbps, out none → 20.0 Mbps')
  eq(M.speedChangeText({ at: null, action: 'netflow.interface_override.update', source: 'override', in: { old: 30e6, new: 50e6 }, out: { old: 30e6, new: 50e6 }, old: 30e6, new: 50e6, actor: null, changes: {} }),
    'NetFlow override: 30.0 Mbps → 50.0 Mbps (both directions)')
  eq(M.speedChangeText({ at: null, action: 'netflow.interface_override.update', source: 'override', in: null, out: { old: 20e6, new: null }, old: null, new: null, actor: null, changes: {} }),
    'NetFlow override: out 20.0 Mbps → none')
}

/* FE-05 trend chart: per-direction capacity lines and per-day speeds. */
{
  const day = (d, inP, outP, sIn, sOut, extra = {}) => ({ day: d, complete: true, samples: 288, expected_slots: 288, coverage_pct: 100, p_reason: null,
    speed: { in_bps: sIn, out_bps: sOut, source_in: 'override', source_out: 'override', changed: false, ...extra },
    in: { p_bps: inP, max_bps: inP, avg_bps: inP, utilization_p_pct: sIn ? (100 * inP) / sIn : null },
    out: { p_bps: outP, max_bps: outP, avg_bps: outP, utilization_p_pct: sOut ? (100 * outP) / sOut : null } })
  // 127.0.0.83 Branch-3 DSL: 50 Mbps down (in), 20 Mbps up (out).
  const dsl = { speed: { in_bps: 50e6, out_bps: 20e6 }, days: [day('2026-09-28', 30e6, 15e6, 50e6, 20e6), day('2026-09-29', 32e6, 16e6, 50e6, 20e6)] }
  const bps = M.trendModel(dsl, { warn: 70, crit: 90 }, 'bps')
  eq(bps.symmetric, false, 'asymmetric circuit')
  eq([bps.points[0].speedIn, bps.points[0].speedOut], [50e6, 20e6], 'a capacity line per direction')
  eq(bps.points[0].speed, null, 'never one line at max(in, out)')
  eq([bps.speedLabels.speedIn, bps.speedLabels.speedOut], ['In speed 50.0 Mbps', 'Out speed 20.0 Mbps'])
  const pct = M.trendModel(dsl, { warn: 70, crit: 90 }, 'pct')
  eq([pct.points[1].in, pct.points[1].out], [64, 80], 'each direction against its own speed: out is 80% of 20 Mbps')
  eq(pct.yMax, 100)
  // A symmetric circuit upgraded 20 -> 30 Mbps mid-range: one stepped speed line with levels per day.
  const up = { speed: { in_bps: 30e6, out_bps: 30e6 }, days: [day('2026-09-28', 15e6, 5e6, 20e6, 20e6), day('2026-09-29', 15e6, 5e6, 30e6, 30e6, { changed: true })] }
  const u = M.trendModel(up, { warn: 70, crit: 90 }, 'bps')
  eq(u.symmetric, true)
  eq(u.points.map((p) => p.speed), [20e6, 30e6], 'the speed in effect each day')
  eq(u.points.map((p) => p.warn), [14e6, 21e6], 'levels follow the day speed')
  eq(u.speedChanged, true)
  eq(u.speedLabels.speed, 'Speed 20.0 Mbps–30.0 Mbps')
  const up2 = M.trendModel(up, { warn: 70, crit: 90 }, 'pct')
  eq(up2.points.map((p) => p.in), [75, 50], 'an upgrade does not rewrite the earlier days')
  eq(M.daySpeedText({ in_bps: 50e6, out_bps: 20e6 }), '50.0 Mbps / 20.0 Mbps')
  eq(M.daySpeedText({ in_bps: 30e6, out_bps: 30e6 }), '30.0 Mbps')
}

/* Provisional (settling) slots are disclosed, never shown as idle. */
{
  eq(M.provisionalText([]), null)
  const t = M.provisionalText([{ exporter: '198.18.220.11', settled_until: '2026-10-01T12:00:00Z', provisional_slots: 7, active_timeout_s: 1800, basis: 'observed' }])
  ok(/newest 35 minutes of one exporter are still settling/.test(t) && /up to 30 min\)/.test(t) && /not known to be idle/.test(t), t)
  ok(/assumed where no flow durations/.test(M.provisionalText([{ exporter: 'x', settled_until: 'x', provisional_slots: 7, active_timeout_s: 1800, basis: 'default' }])))
}

/* X-19: Link Utilization -> NetFlow names the exporter that carried the ifIndex, with the LU window. */
{
  const custom = { key: 'custom', hours: 2, fromISO: '2026-09-29T00:00:00.000Z', toISO: '2026-09-29T02:00:00.000Z', isCustom: true, label: 'custom' }
  const links = L.netflowInterfaceLinks(1, [{ exporter: '10.220.250.1', bytes: 10 }, { exporter: '198.18.220.1', bytes: 9e9 }], custom, ['10.220.250.1', '198.18.220.1'])
  eq(links.map((x) => x.exporter), ['198.18.220.1', '10.220.250.1'], 'busiest exporter first')
  const q = new URLSearchParams(links[0].to.split('?')[1])
  ok(links[0].to.startsWith('/netflow?'), links[0].to)
  eq(q.get('iface'), '1@198.18.220.1', 'qualified interface, not the management address')
  eq(q.get('exporter'), null, 'no exporter filter on the management address')
  eq([q.get('range'), q.get('from'), q.get('to')], ['custom', '2026-09-29T00:00:00.000Z', '2026-09-29T02:00:00.000Z'], 'the LU custom range travels')
  const preset = L.netflowInterfaceLinks(3, [], { key: '24h', hours: 24, fromISO: 'a', toISO: 'b', isCustom: false, label: '24h' }, ['198.18.220.1'])
  eq(new URLSearchParams(preset[0].to.split('?')[1]).get('range'), '24h', 'a preset travels as its key')
  eq(new URLSearchParams(preset[0].to.split('?')[1]).get('iface'), '3@198.18.220.1', 'no flows in the window: the device exporters, still qualified')
  eq(L.netflowInterfaceLinks(3, null, { key: '1h', hours: 1, fromISO: 'a', toISO: 'b', isCustom: false, label: '1h' }, []), [])
}

/* ─── Options ──────────────────────────────────────────────────────── */
ok(M.SORT_OPTIONS[0].value === 'risk', 'risk ranking is the default sort')
eq(new Set(M.SORT_OPTIONS.map((o) => o.value)).size, M.SORT_OPTIONS.length)
eq(M.RESOLUTIONS, ['resolved', 'exporter_unmapped', 'no_interface_inventory', 'interface_not_found', 'speed_unknown'])

console.log(`test-netflow-capacity: ${checks} checks passed`)
