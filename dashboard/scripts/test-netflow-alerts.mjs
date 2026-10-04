// NetFlow alert rules in the wizard (CR-07) and NetFlow report scope (FE-07, NF-04 report side):
// form model, unit conversion, validation, payload round trip, scope helpers, parity with the
// server's metric catalogue, and static renders of the wizard fields.
// Run from dashboard/: node scripts/test-netflow-alerts.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')

const entry = `
export * as R from '@/components/forms/netflowAlertRule'
export * as Fields from '@/components/forms/NetflowAlertFields'
export * as Scope from '@/pages/reports/NetflowReportScope'
export * as filters from '@/lib/netflow/filters'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
const { R, Fields, Scope, filters: F } = m

let checks = 0
const ok = (cond, msg) => { assert(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }

/* ─── Defaults are valid for every metric ───────────────────────────── */
for (const info of R.NETFLOW_METRICS) {
  const form = R.defaultNetflowForm(info.metric)
  eq(R.validateNetflowForm(form), {}, `default ${info.metric} form validates`)
  const p = R.netflowPayload(form)
  eq(p.metric, info.metric)
  ok(Number.isFinite(p.threshold), `${info.metric} threshold is a number`)
  ok(R.isNetflowMetric(info.metric))
}
ok(!R.isNetflowMetric('ping_status') && !R.isNetflowMetric('') && !R.isNetflowMetric(null))

/* ─── Units: human units in the form, base units on the wire ────────── */
eq(R.bestUnit('bps', 1.5e9), 'Gbps')
eq(R.bestUnit('bps', 999), 'bps')
eq(R.bestUnit('bytes', 0), 'B')
eq(R.fromBase('bps', 1.5e9, 'Gbps'), '1.5')
eq(R.toBase('bytes', '2.5', 'GB'), 2.5e9)
eq(R.toBase('bps', '', 'Mbps'), null)
eq(R.toBase('bps', 'abc', 'Mbps'), null)
eq(R.formatThreshold('bps', 1e8), '100 Mbps')
eq(R.formatThreshold('seconds', 600), '10 min')
eq(R.formatThreshold('percent', 80), '80%')
eq(R.formatThreshold('bps', null), '—')

/* ─── Payloads ───────────────────────────────────────────────────────── */
let form = { ...R.defaultNetflowForm('netflow_traffic_bps'), threshold: '100', unit: 'Mbps', split: 'exporter',
  scope: { exporter: '10.0.0.1', app: 'DNS' } }
let p = R.netflowPayload(form)
eq(p.operator, '>')
eq(p.threshold, 1e8)
eq(p.flow_scope, { exporter: '10.0.0.1', app: 'DNS' })
eq(p.flow_params, { hold: 2, clear_hold: 2, flap_max: 4, flap_window_min: 60, window_min: 5, split: 'exporter' })

// Inclusive / below / outside band.
eq(R.netflowPayload({ ...form, inclusive: true }).operator, '>=')
eq(R.netflowPayload({ ...form, mode: 'below' }).operator, '<')
eq(R.netflowPayload({ ...form, mode: 'below', inclusive: true }).operator, '<=')
const band = R.netflowPayload({ ...form, mode: 'outside', threshold: '10', bandHigh: '200' })
eq([band.operator, band.threshold, band.flow_params.band_high], ['!=', 1e7, 2e8])

// Zero is a threshold, not "unset" (traffic below 0 bps is pointless but valid; below 0.5 Mbps is common).
const zero = { ...form, mode: 'below', threshold: '0' }
eq(R.validateNetflowForm(zero), {})
eq(R.netflowPayload(zero).threshold, 0)
eq(R.netflowPayload({ ...form, scope: { dst_port: '0' } }).flow_scope, { dst_port: '0' })

// Per-metric options only on their metric.
p = R.netflowPayload({ ...R.defaultNetflowForm('netflow_endpoint_bytes'), side: 'src', threshold: '5', unit: 'GB' })
eq([p.threshold, p.flow_params.side, 'split' in p.flow_params], [5e9, 'src', false])
p = R.netflowPayload({ ...R.defaultNetflowForm('netflow_interface_util_pct'), direction: 'out', scope: { iface: '3@10.0.0.1' } })
eq([p.operator, p.threshold, p.flow_params.direction, p.flow_params.window_min], ['>', 80, 'out', 15])
p = R.netflowPayload({ ...R.defaultNetflowForm('netflow_exporter_silent'), threshold: '10', unit: 'min' })
eq([p.threshold, 'window_min' in p.flow_params], [600, false])
p = R.netflowPayload({ ...R.defaultNetflowForm('netflow_finding'), kinds: ' syn_scan, icmp_flood ,', min_confidence: 'high' })
eq([p.operator, p.threshold], ['==', 1])
eq(p.flow_params.kinds, ['syn_scan', 'icmp_flood'])
eq([p.flow_params.min_severity, p.flow_params.min_confidence], ['warning', 'high'])
eq(R.netflowPayload({ ...R.defaultNetflowForm('netflow_finding'), min_severity: '' }).flow_params.min_severity, null)

/* ─── Stored rule -> editor -> payload round trip ───────────────────── */
const roundTrip = (f) => {
  const pay = R.netflowPayload(f)
  const again = R.formFromRule({ metric: pay.metric, operator: pay.operator, threshold: pay.threshold,
    flow_scope: pay.flow_scope, flow_params: pay.flow_params })
  eq(R.validateNetflowForm(again), {}, `round trip of ${f.metric} validates`)
  eq(R.netflowPayload(again), pay, `round trip of ${f.metric} is lossless`)
  return again
}
let back = roundTrip({ ...form, threshold: '1.5', unit: 'Gbps', inclusive: true })
eq([back.unit, back.threshold, back.inclusive, back.split], ['Gbps', '1.5', true, 'exporter'])
back = roundTrip({ ...form, mode: 'outside', threshold: '10', bandHigh: '200', unit: 'Mbps' })
eq([back.mode, back.unit, back.threshold, back.bandHigh], ['outside', 'Mbps', '10', '200'])
roundTrip({ ...R.defaultNetflowForm('netflow_endpoint_bytes'), threshold: '750', unit: 'MB', side: 'dst', window_min: '60' })
roundTrip({ ...R.defaultNetflowForm('netflow_interface_util_pct'), mode: 'below', threshold: '5', direction: 'in',
  scope: { exporter: '10.0.0.1', iface: '3' } })
roundTrip({ ...R.defaultNetflowForm('netflow_exporter_silent'), threshold: '2', unit: 'h', scope: { exporter: '2001:db8::1' } })
back = roundTrip({ ...R.defaultNetflowForm('netflow_finding'), kinds: 'syn_scan', min_severity: 'high', scope: { talker: '10.0.0.0/8' } })
eq([back.kinds, back.min_severity], ['syn_scan', 'high'])
// Advanced parameters set through the API survive an edit; switching metric drops them.
const stored = { metric: 'netflow_traffic_bps', operator: '>', threshold: 5e6,
  flow_scope: { exporter: '10.0.0.1' }, flow_params: { hold: 1, clear_hold: 1, window_min: 5, flap_max: 4,
    flap_window_min: 60, split: 'none', settle_s: 300, band_high: null } }
const edited = R.formFromRule(stored)
eq(R.netflowPayload({ ...edited, threshold: '6' }).flow_params.settle_s, 300)
eq(R.netflowPayload({ ...edited, threshold: '6' }).threshold, 6e6)
ok(!('settle_s' in R.netflowPayload(R.changeMetric(edited, 'netflow_endpoint_bytes')).flow_params), 'extras dropped on metric change')
ok(!('band_high' in R.netflowPayload(edited).flow_params), 'a null band is not stored')
// A rule stored with an unknown operator still opens (as "above").
eq(R.formFromRule({ metric: 'netflow_traffic_bps', operator: null, threshold: 5e6 }).mode, 'above')

/* ─── Validation mirrors the server ──────────────────────────────────── */
const errs = (patch, metric = 'netflow_traffic_bps') => R.validateNetflowForm({ ...R.defaultNetflowForm(metric), ...patch })
ok(errs({ threshold: '' }).threshold, 'empty threshold')
ok(errs({ threshold: '-1' }).threshold, 'negative threshold')
ok(errs({ threshold: '30', unit: 's' }, 'netflow_exporter_silent').threshold, 'silence < 60 s')
eq(errs({ threshold: '1', unit: 'min' }, 'netflow_exporter_silent'), {})
ok(errs({ threshold: '1001' }, 'netflow_interface_util_pct').threshold, 'util > 1000 %')
ok(errs({ mode: 'outside', threshold: '10', bandHigh: '10' }).bandHigh, 'band upper <= lower')
ok(errs({ mode: 'outside', threshold: '10', bandHigh: '' }).bandHigh, 'band upper missing')
ok(errs({ mode: 'below' }, 'netflow_endpoint_bytes').mode, 'host volume is above-only')
ok(errs({ window_min: '61' }).window_min, 'traffic window <= 60')
ok(errs({ window_min: '2.5' }).window_min, 'whole minutes')
ok(errs({ window_min: '12' }, 'netflow_interface_util_pct').window_min, 'util window multiple of 5')
eq(errs({ window_min: '1440' }, 'netflow_endpoint_bytes'), {})
ok(errs({ hold: '0' }).hold && errs({ hold: '61' }).hold, 'hold 1..60')
ok(errs({ clear_hold: '' }).clear_hold, 'clear hold required')
ok(errs({ flap_max: '1' }).flap_max, 'flap_max 1 is meaningless')
eq(errs({ flap_max: '0' }), {})
ok(errs({ flap_window_min: '4' }).flap_window_min, 'flap window >= 5')
// Scope keys a metric cannot use are reported, never silently dropped from the payload.
ok(errs({ scope: { app: 'DNS' } }, 'netflow_exporter_silent').scope, 'silence cannot be scoped by app')
ok(errs({ scope: { fw_event: 'denied' } }).scope, 'traffic cannot be scoped by firewall event')
ok(errs({ scope: { exporter: '10.0.0.300' } }).exporter, 'invalid exporter address')
ok(errs({ scope: { protocol: '999' } }).protocol, 'invalid protocol')
ok(errs({ scope: { iface: '3' } }, 'netflow_interface_util_pct').iface, 'util interface needs its exporter')
eq(errs({ scope: { iface: '3@10.0.0.1' } }, 'netflow_interface_util_pct'), {})
eq(errs({ scope: { iface: '3', exporter: '10.0.0.1' } }, 'netflow_interface_util_pct'), {})
ok(errs({ kinds: 'x'.repeat(49) }, 'netflow_finding').kinds, 'finding kind length')

/* ─── Switching metric keeps only what still applies ─────────────────── */
const switched = R.changeMetric({ ...form, scope: { exporter: '10.0.0.1', app: 'DNS', iface: '3@10.0.0.1' } }, 'netflow_exporter_silent')
eq(switched.scope, { exporter: '10.0.0.1' })
eq([switched.unit, switched.threshold], ['min', '10'])
eq(R.changeMetric(form, form.metric), form)

/* ─── Summaries ──────────────────────────────────────────────────────── */
eq(R.summarizeNetflowForm(form), 'Traffic rate above 100 Mbps over 5 min, held for 2 evaluations')
eq(R.summarizeNetflowForm({ ...form, mode: 'outside', threshold: '10', bandHigh: '200', hold: '1' }),
  'Traffic rate outside 10 Mbps – 200 Mbps over 5 min')
eq(R.summarizeNetflowForm(R.defaultNetflowForm('netflow_exporter_silent')), 'Exporter silent for more than 10 min')
eq(R.summarizeNetflowForm({ ...R.defaultNetflowForm('netflow_finding'), kinds: 'syn_scan' }),
  'Open detection finding (kinds syn_scan, severity ≥ warning)')
eq(R.describeScopeDraft(form), 'Exporter 10.0.0.1 · Application DNS')
eq(R.describeScopeDraft(R.defaultNetflowForm('netflow_exporter_silent')), 'Every flow exporter')
eq(R.describeScopeDraft(R.defaultNetflowForm('netflow_traffic_bps')), 'All flow traffic')

/* ─── Parity with the server catalogue (netflow_alert_service.py) ────── */
const py = fs.readFileSync(path.join(root, '..', 'server', 'app', 'services', 'netflow_alert_service.py'), 'utf8')
const pyName = { netflow_traffic_bps: 'TRAFFIC', netflow_endpoint_bytes: 'ENDPOINT', netflow_exporter_silent: 'SILENT',
  netflow_interface_util_pct: 'UTIL', netflow_finding: 'FINDING' }
const infoBlock = (name) => {
  const start = py.indexOf(`    ${name}: {\n        "label"`)
  ok(start > 0, `METRIC_INFO block for ${name}`)
  return py.slice(start, py.indexOf('\n    },', start))
}
const scopeBlock = py.slice(py.indexOf('_SCOPE_KEYS_BY_METRIC'), py.indexOf('def parse_scope'))
for (const info of R.NETFLOW_METRICS) {
  const name = pyName[info.metric]
  const block = infoBlock(name)
  const wr = block.match(/"window_range": (None|\[(\d+), (\d+)\])/)
  eq(wr[1] === 'None' ? null : [Number(wr[2]), Number(wr[3])], info.windowRange, `${info.metric} window range`)
  const ops = JSON.parse(block.match(/"operators": (\[[^\]]*\])/)[1])
  const uiOps = new Set()
  for (const mode of info.modes) {
    if (mode === 'outside') uiOps.add('!=')
    else if (info.metric === 'netflow_finding') uiOps.add('==')
    else for (const inclusive of [false, true]) {
      if (info.metric === 'netflow_exporter_silent' && inclusive) continue   // the UI hides "inclusive" there
      uiOps.add(mode === 'below' ? (inclusive ? '<=' : '<') : inclusive ? '>=' : '>')
    }
  }
  for (const op of uiOps) ok(ops.includes(op), `${info.metric}: the UI's operator ${op} is accepted by the server`)
  for (const key of ['hold', 'clear_hold', 'flap_max', 'flap_window_min', 'window_min']) {
    const mm = block.match(new RegExp(`"${key}": (\\d+)`))
    const ui = info.defaults[key]
    if (mm) eq(ui, Number(mm[1]), `${info.metric} default ${key}`)
  }
  const line = scopeBlock.split('\n').find((l) => l.trim().startsWith(`${name}:`))
  let serverKeys
  if (line.includes('FLOW_FILTER_KEYS')) {
    const minus = [...line.matchAll(/"(\w+)"/g)].map((x) => x[1])
    serverKeys = F.FLOW_FILTER_KEYS.filter((k) => !minus.includes(k))
  } else serverKeys = [...line.matchAll(/"(\w+)"/g)].map((x) => x[1])
  eq([...info.scopeKeys].sort(), [...serverKeys].sort(), `${info.metric} scope keys match the server`)
}
eq(JSON.parse(py.match(/FINDING_SEVERITIES = (\([^)]*\))/)[1].replace(/^\(/, '[').replace(/\)$/, ']')), [...R.FINDING_SEVERITIES])

/* ─── Report scope helpers ───────────────────────────────────────────── */
let rs = Scope.parseReportScope('exporter=10.0.0.1&dst_port=0&protocol=999&bogus=1')
eq(rs.filter, { exporter: '10.0.0.1', dst_port: 0 })
eq(rs.invalid, ['protocol'])
eq(Scope.parseReportScope(null), { filter: {}, invalid: [] })
eq(Scope.reportScopeParam({ exporter: '10.0.0.1', dst_port: 0, app: 'DNS' }), 'exporter=10.0.0.1&dst_port=0&app=DNS')
eq(Scope.reportScopeParam({}), '')
eq(Scope.reportScopeObject({ count: 'edge', group: 'Branch' }), { group: 'Branch', count: 'edge' })
// URL -> filter -> URL is stable (a shared link reproduces the same scope).
const q = 'exporter=2001%3Adb8%3A%3A1&talker=10.0.0.0%2F8&count=edge'
eq(Scope.reportScopeParam(Scope.parseReportScope(q).filter), q)
ok(Scope.REPORT_SCOPE_KEYS.every((k) => F.FLOW_FILTER_KEYS.includes(k)), 'report scope keys are canonical')

/* ─── Static renders: labelled, keyboard-reachable controls ─────────── */
const { createElement: h, renderToStaticMarkup, QueryClient, QueryClientProvider } = m
const wrap = (el) => renderToStaticMarkup(h(QueryClientProvider, { client: new QueryClient() }, el))
for (const info of R.NETFLOW_METRICS) {
  const f0 = R.defaultNetflowForm(info.metric)
  const html = wrap(h(Fields.NetflowConditionFields, { form: f0, errors: {}, onChange: () => {} }))
  ok(html.includes('role="radiogroup"') && html.includes('aria-label="NetFlow metric"'), 'metric radiogroup')
  ok((html.match(/role="radio"/g) || []).length >= R.NETFLOW_METRICS.length, 'every metric is a radio')
  ok(html.includes('aria-checked="true"'), 'selected metric is announced')
  ok(!html.includes('undefined') && !html.includes('NaN'), `${info.metric}: no undefined/NaN in the form`)
  for (const id of [...html.matchAll(/<label[^>]*for="([^"]+)"/g)].map((x) => x[1])) {
    ok(html.includes(`id="${id}"`), `${info.metric}: label for=${id} has a control`)
  }
  if (info.windowRange) ok(html.includes('Measured over'), `${info.metric} shows its window`)
  else ok(!html.includes('Measured over'), `${info.metric} has no window`)
}
let html = wrap(h(Fields.NetflowConditionFields, {
  form: { ...R.defaultNetflowForm(), threshold: '' }, errors: { threshold: 'Enter a number' }, onChange: () => {} }))
ok(/aria-describedby="[^"]+"[^>]*value=""/.test(html) || html.includes('aria-describedby'), 'error is described')
ok(html.includes('Enter a number') && html.includes('text-danger-fg'), 'field error uses the danger foreground token')

const scoped = { ...R.defaultNetflowForm(), scope: { exporter: '10.0.0.1', app: 'DNS' } }
html = wrap(h(Fields.NetflowScopeFields, { form: scoped, errors: {}, onChange: () => {}, open: false }))
ok(html.includes('aria-label="Remove Exporter filter"') && html.includes('aria-label="Remove Application filter"'),
  'icon-only remove buttons are named')
ok(html.includes('Add a scope filter'), 'add-filter select is labelled')
ok(html.includes('The rule will match'), 'chips preview the applied scope')
html = wrap(h(Fields.NetflowScopeFields, { form: R.defaultNetflowForm(), errors: {}, onChange: () => {}, open: false }))
ok(html.includes('No filter: whole visible scope'), 'empty scope is explicit')

html = wrap(h(Fields.NetflowDryRun, { form: { ...R.defaultNetflowForm(), threshold: '' } }))
ok(html.includes('disabled') && html.includes('Fix the highlighted fields'), 'dry run is disabled while invalid')
html = wrap(h(Fields.NetflowDryRun, { form: R.defaultNetflowForm() }))
ok(html.includes('Run test') && !html.includes('Fix the highlighted fields'), 'dry run is available when valid')

html = renderToStaticMarkup(h(Scope.NetflowScopeChips, { value: {}, onChange: () => {} }))
eq(html, '', 'no scope -> no chip row')
html = renderToStaticMarkup(h(Scope.NetflowScopeChips, { value: { exporter: '10.0.0.1' }, onChange: () => {} }))
ok(html.includes('NetFlow scope') && html.includes('10.0.0.1'), 'applied scope is shown')

console.log(`test-netflow-alerts: ${checks} checks passed`)
