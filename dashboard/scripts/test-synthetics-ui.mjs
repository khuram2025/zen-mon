// APM Synthetics UI (list + scenario detail):
//   (a) the pure model (src/features/apm/synthetics.ts): URL state (range kept on links, ?step / ?run parsing,
//       runs query encoding), formatting (uptime, durations, intervals, bytes, host+path), the 48-bucket sparkline,
//       the timing breakdown and run waterfall, per-step chips and run grouping, chart shaping, outage windows,
//       assertion text with masking, variables text, and the save payload that keeps every stored field,
//   (b) wiring: the detail route, the 24h default on both pages while other pages keep 1h, the list links to the
//       detail page, the old 200-run history strip is gone, the rem type scale, and no internal component names,
//   (c) rendering with fixture data (charts stubbed): the list asks for 24h by default and links each name to its
//       detail page with the range; read-only users get no actions; on the detail page a step opens ?step=<index>
//       with that step's failures, a run opens ?run=<timestamp> with its waterfall and assertions, an error group
//       filters the runs table, and enable/disable sends the whole configuration back.
// Run from dashboard/: node scripts/test-synthetics-ui.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { build } from 'esbuild'
import { JSDOM } from 'jsdom'
import React, { act } from 'react'
import { transformWithEsbuild } from 'vite'

process.env.TZ = 'UTC'

/* ═══ (a) the pure model ═════════════════════════════════════════════════════════════════════ */

const modelSrc = fs.readFileSync('src/features/apm/synthetics.ts', 'utf8')
assert(!/^import (?!type )/m.test(modelSrc), 'synthetics.ts has no runtime imports, so it runs without the app')
const compiled = await transformWithEsbuild(modelSrc, 'synthetics.ts', { loader: 'ts', format: 'esm' })
const M = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

// URL state: links keep only the time range; drawers use ?step=<index> and ?run=<timestamp>.
{
  assert.equal(M.rangeQuery('?range=7d&step=2&run=x'), 'range=7d')
  assert.equal(M.rangeQuery('range=custom&from=2026-11-01T00%3A00%3A00.000Z&to=2026-11-02T00%3A00%3A00.000Z&foo=1'),
    'range=custom&from=2026-11-01T00%3A00%3A00.000Z&to=2026-11-02T00%3A00%3A00.000Z')
  assert.equal(M.detailHref('abc', '?range=24h&step=1'), '/apm/synthetics/abc?range=24h')
  assert.equal(M.detailHref('a b'), '/apm/synthetics/a%20b')
  assert.equal(M.listHref(new URLSearchParams('range=1M&run=2026')), '/apm/synthetics?range=1M')
  assert.equal(M.listHref(''), '/apm/synthetics')
  assert.equal(M.withParam('range=7d', 'step', 3).toString(), 'range=7d&step=3')
  assert.equal(M.withParam('range=7d&step=3', 'step', null).toString(), 'range=7d')
  assert.equal(M.withParam('step=3', 'step', '').toString(), '')
  assert.equal(M.parseStepParam('0', 3), 0)
  assert.equal(M.parseStepParam('2', 3), 2)
  assert.equal(M.parseStepParam('3', 3), null, 'past the last step')
  assert.equal(M.parseStepParam('-1', 3), null)
  assert.equal(M.parseStepParam('1.5', 3), null)
  assert.equal(M.parseStepParam(null, 3), null)
  assert.equal(M.parseRunParam('2026-11-08T10:00:00.123+00:00'), '2026-11-08T10:00:00.123+00:00')
  assert.equal(M.parseRunParam('yesterday'), null)
  assert.equal(M.parseRunParam(''), null)
  // a run timestamp with "+00:00" survives a round trip through the query string
  const at = '2026-11-08T10:00:00.123456+00:00'
  assert.equal(new URLSearchParams(M.withParam('', 'run', at).toString()).get('run'), at)
}

// Runs query: offsets encoded, limit bounded by the server (≤ 500), step filter only when set.
{
  const q = new URLSearchParams(M.runsQuery({ from: '2026-11-07T10:00:00+00:00', to: '2026-11-08T10:00:00+00:00', status: 'failed', step: 'Log in', offset: 50 }))
  assert.equal(q.get('from'), '2026-11-07T10:00:00+00:00')
  assert.equal(q.get('status'), 'failed')
  assert.equal(q.get('step'), 'Log in')
  assert.equal(q.get('limit'), '50')
  assert.equal(q.get('offset'), '50')
  assert(M.runsQuery({ from: 'a', to: 'b' }).includes('%2B') === false)
  assert(M.runsQuery({ from: '2026-11-07T10:00:00+00:00', to: 'b' }).includes('from=2026-11-07T10%3A00%3A00%2B00%3A00'), 'plus sign encoded, never a space')
  const big = new URLSearchParams(M.runsQuery({ from: 'a', to: 'b', limit: 9999, offset: -4 }))
  assert.equal(big.get('limit'), '500')
  assert.equal(big.get('offset'), '0')
  assert.equal(big.get('status'), 'all')
  assert.equal(big.get('step'), null)
  assert.deepEqual(M.pageInfo(0, 0), { page: 1, pages: 1, first: 0, last: 0, hasPrev: false, hasNext: false })
  assert.deepEqual(M.pageInfo(120, 50), { page: 2, pages: 3, first: 51, last: 100, hasPrev: true, hasNext: true })
  assert.deepEqual(M.pageInfo(120, 100), { page: 3, pages: 3, first: 101, last: 120, hasPrev: true, hasNext: false })
}

// Outage window: span plus a margin, an ongoing outage runs to now, never past now.
{
  const now = Date.parse('2026-11-08T12:00:00Z')
  assert.deepEqual(M.outageWindow({ start: '2026-11-08T10:00:00+00:00', end: '2026-11-08T10:30:00+00:00', ongoing: false }, now),
    { from: '2026-11-08T09:50:00.000Z', to: '2026-11-08T10:40:00.000Z' })
  assert.deepEqual(M.outageWindow({ start: '2026-11-08T11:30:00+00:00', end: null, ongoing: true }, now),
    { from: '2026-11-08T11:20:00.000Z', to: '2026-11-08T12:00:00.000Z' })
  assert.equal(M.outageWindow({ start: 'nope', end: null, ongoing: true }, now), null)
}

// Formatting.
{
  assert.equal(M.fmtUptime(null), '—')
  assert.equal(M.fmtUptime(100), '100%')
  assert.equal(M.fmtUptime(99.999), '99.99%', 'never rounds up to 100')
  assert.equal(M.fmtUptime(97.5), '97.50%')
  assert.equal(M.fmtDuration(0), '0s')
  assert.equal(M.fmtDuration(59), '59s')
  assert.equal(M.fmtDuration(60), '1m')
  assert.equal(M.fmtDuration(200), '3m 20s')
  assert.equal(M.fmtDuration(3900), '1h 5m')
  assert.equal(M.fmtDuration(90_000), '1d 1h')
  assert.equal(M.fmtDuration(null), '—')
  assert.equal(M.fmtInterval(60), '60s')
  assert.equal(M.fmtInterval(300), '5m')
  assert.equal(M.fmtInterval(3600), '1h')
  assert.equal(M.fmtInterval(90), '90s')
  assert.equal(M.fmtBytes(512), '512 B')
  assert.equal(M.fmtBytes(2048), '2.0 KB')
  assert.equal(M.fmtBytes(3 * 1024 * 1024), '3.0 MB')
  assert.equal(M.hostPath('https://shop.example.com/api/login?x=1'), 'shop.example.com/api/login')
  assert.equal(M.hostPath('https://shop.example.com/'), 'shop.example.com')
  assert.equal(M.hostPath('http://10.0.0.5:8080/health#a'), '10.0.0.5:8080/health')
  assert.equal(M.hostPath('{{base}}/cart'), '{{base}}/cart', 'template URLs shown as written')
  assert.equal(M.uptimeTone(100), 'ok')
  assert.equal(M.uptimeTone(99.5), 'ok')
  assert.equal(M.uptimeTone(95), 'warn')
  assert.equal(M.uptimeTone(50), 'bad')
  assert.equal(M.uptimeTone(null), 'none')
}

// Sparkline: always 48 bars; missing buckets are "none", a failed bucket still has a visible height.
{
  const bars = M.sparkBars([100, 0, null, 96.7, 120])
  assert.equal(bars.length, M.SPARK_BUCKETS)
  assert.equal(M.SPARK_BUCKETS, 48)
  assert.deepEqual(bars.slice(0, 5).map((b) => b.tone), ['ok', 'bad', 'none', 'warn', 'ok'])
  assert.equal(bars[1].height, 15)
  assert.equal(bars[4].pct, 100, 'clamped')
  assert(bars.slice(5).every((b) => b.tone === 'none' && b.pct === null))
  assert.equal(M.sparkBars(undefined).length, 48)
  const span = M.sparkBucketRange(1, '2026-11-07T00:00:00Z', '2026-11-08T00:00:00Z')
  assert.equal(span.to - span.from, 30 * 60_000, '24h / 48 = 30 minute buckets')
  assert.equal(new Date(span.from).toISOString(), '2026-11-07T00:30:00.000Z')
  assert.equal(M.sparkBucketRange(0, 'x', 'y'), null)
}

// Timing breakdown: connect / TLS / send / wait / download shares; missing phases left out.
{
  const segs = M.timingSegments({ connect_ms: 10, tls_ms: 30, ttfb_ms: 50, download_ms: 10, send_ms: null })
  assert.deepEqual(segs.map((s) => [s.label, s.pct]), [['Connect', 10], ['TLS', 30], ['Wait', 50], ['Download', 10]])
  assert.deepEqual(M.timingSegments(null), [])
  assert.deepEqual(M.timingSegments({ connect_ms: 0, tls_ms: 0 }), [])
  assert.equal(M.timingText({ connect_ms: 12, ttfb_ms: 80 }), 'Connect 12 ms · Wait 80 ms')
  const w = M.waterfall([{ ms: 100, timing: { connect_ms: 20, ttfb_ms: 80 } }, { ms: 300 }, { ms: 0 }])
  assert.deepEqual(w.map((r) => [r.offsetPct, r.widthPct, r.startMs]), [[0, 25, 0], [25, 75, 100], [100, 0.5, 400]])
  assert.equal(w[0].segments.length, 2)
  assert.deepEqual(w[1].segments, [])
  assert.deepEqual(M.waterfall([]), [])
}

// Run chips: one per step; steps after the failure were not run.
{
  const chips = M.stepChips(
    { steps_total: 3, steps: [{ name: 'Home', ok: true, status_code: 200, ms: 40 }, { name: 'Log in', ok: false, status_code: 401, ms: 90 }] },
    [{ name: 'Home' }, { name: 'Log in' }, { name: 'Checkout' }],
  )
  assert.deepEqual(chips.map((c) => [c.name, c.state, c.status_code]), [['Home', 'ok', 200], ['Log in', 'fail', 401], ['Checkout', 'skipped', null]])
  assert.equal(M.stepChips({ steps_total: 0, steps: [] }, [{ name: 'A' }, { name: 'B' }]).length, 2)
  assert.deepEqual(M.stepChips({ steps_total: 1, steps: [{ name: '', ok: true, status_code: 0, ms: 5 }] }).map((c) => [c.name, c.status_code]), [['Step 1', null]])
  const groups = M.groupRunsByDay([
    { timestamp: '2026-11-08T02:00:00+00:00', success: true },
    { timestamp: '2026-11-08T01:00:00+00:00', success: false },
    { timestamp: '2026-11-07T23:00:00+00:00', success: false },
  ], (iso) => iso.slice(0, 10))
  assert.deepEqual(groups.map((g) => [g.day, g.runs.length, g.failed]), [['2026-11-08', 2, 1], ['2026-11-07', 1, 1]])
}

// Chart shaping: per-step series aligned by bucket and sorted; availability passed/failed split.
{
  const rows = M.stackStepSeries([
    { index: 0, series: [{ t: '2026-11-08T01:00:00+00:00', avg_ms: 10, failed: 0 }, { t: '2026-11-08T00:00:00+00:00', avg_ms: 12, failed: 0 }] },
    { index: 1, series: [{ t: '2026-11-08T00:00:00+00:00', avg_ms: null, failed: 1 }] },
  ])
  assert.deepEqual(rows, [
    { t: '2026-11-08T00:00:00+00:00', s0: 12, s1: null },
    { t: '2026-11-08T01:00:00+00:00', s0: 10 },
  ])
  assert.deepEqual(M.availabilityBars([{ t: 'a', runs: 30, failed: 2, uptime_pct: 93.3, avg_ms: 1, p95_ms: 1 }, { t: 'b', runs: 0, failed: 0, uptime_pct: null }]),
    [{ t: 'a', passed: 28, failed: 2, uptime_pct: 93.3 }, { t: 'b', passed: 0, failed: 0, uptime_pct: null }])
}

// Assertions: readable text; text values hidden for read-only users, numbers stay.
{
  assert.equal(M.describeAssertion({ type: 'status_code', operator: 'eq', value: 200 }), 'Status code = 200')
  assert.equal(M.describeAssertion({ type: 'latency_ms', operator: 'lt', value: 800 }, true), 'Latency (ms) < 800')
  assert.equal(M.describeAssertion({ type: 'json_path', path: 'user.id', operator: 'exists' }), 'JSON path user.id exists')
  assert.equal(M.describeAssertion({ type: 'json_path', path: 'token', operator: 'eq', value: 'abc' }, true), `JSON path token = ${M.MASK}`)
  assert.equal(M.describeAssertion({ type: 'body_contains', value: 'Welcome' }), 'Body contains "Welcome"')
  assert.equal(M.describeAssertion({ type: 'body_contains', value: 'Welcome' }, true), `Body contains "${M.MASK}"`)
}

// Variables text and the save payload: every stored field goes back, so a toggle never wipes variables or TLS.
{
  assert.deepEqual(M.parseVariables('base=https://x.example/?a=b\n\n=nokey\n user = monitor \nnoequals'), { base: 'https://x.example/?a=b', user: 'monitor' })
  assert.equal(M.formatVariables({ a: '1', b: 'x=y' }), 'a=1\nb=x=y')
  assert.deepEqual(M.parseVariables(M.formatVariables({ a: '1', b: 'x=y' })), { a: '1', b: 'x=y' })
  const m = {
    id: 's1', name: 'Checkout', enabled: true, status: 'up', check_interval: 120, timeout: 20, retry_count: 0,
    variables: { password: 'secret' }, verify_tls: false, notify_channels: ['c1'], tags: ['shop'],
    steps: [{ name: 'Login', method: 'POST', url: 'https://x/login', headers: {}, body: '{"p":"{{password}}"}', assertions: [{ type: 'status_code', operator: 'eq', value: 200 }, { type: '' }], extract: [{ var: 't', from: 'json', path: '' }] }],
    runs: 10, spark: [], uptime_pct: 100,
  }
  const p = M.monitorPayload(m, { enabled: false })
  assert.deepEqual(Object.keys(p).sort(), ['check_interval', 'enabled', 'name', 'notify_channels', 'retry_count', 'steps', 'tags', 'timeout', 'variables', 'verify_tls'])
  assert.equal(p.enabled, false)
  assert.deepEqual(p.variables, { password: 'secret' })
  assert.equal(p.verify_tls, false)
  assert.equal(p.retry_count, 0, 'zero retries stays zero')
  assert.deepEqual(p.tags, ['shop'])
  assert.deepEqual(p.steps, [{ name: 'Login', method: 'POST', url: 'https://x/login', body: '{"p":"{{password}}"}', assertions: [{ type: 'status_code', operator: 'eq', value: 200 }], extract: [] }])
  const fresh = M.monitorPayload({ name: 'New', steps: [{ name: 'A', method: '', url: 'https://a' }] })
  assert.deepEqual([fresh.verify_tls, fresh.enabled, fresh.check_interval, fresh.timeout, fresh.retry_count], [true, true, 60, 30, 1])
  assert.deepEqual(fresh.variables, {})
  assert.equal(fresh.steps[0].method, 'GET')
  assert.equal(M.hasMaskedValues(m), false)
  assert.equal(M.hasMaskedValues({ ...m, secrets_masked: true }), true)
  assert.equal(M.hasMaskedValues({ ...m, variables: { a: M.MASK } }), true)
  assert.equal(M.hasMaskedValues({ ...m, steps: [{ ...m.steps[0], body: M.MASK }] }), true)
}

/* ═══ (b) wiring ═════════════════════════════════════════════════════════════════════════════ */

const read = (f) => fs.readFileSync(f, 'utf8')
const app = read('src/App.tsx')
assert.match(app, /<Route path="synthetics\/:id" element={<SyntheticDetailPage \/>} \/>/, 'detail route registered')
assert.match(read('src/pages/apm/ApmLayout.tsx'), /synthetics\\\/\[\^\/\]\+/, 'detail page hides the module tab strip like other APM details')

const picker = read('src/components/TimeRangePicker.tsx')
assert.match(picker, /export function useTimeRange\(defaultPreset: [^=]+= '1h'\)/, 'other pages keep the 1h default')
const list = read('src/pages/apm/SyntheticsPage.tsx')
const detail = read('src/pages/apm/SyntheticDetailPage.tsx')
assert(list.includes("useTimeRange('24h')") && detail.includes("useTimeRange('24h')"), 'Synthetics defaults to 24h')
assert(list.includes('<TimeRangePicker') && detail.includes('<TimeRangePicker'))
assert(list.includes('detailHref(m.id, searchParams)'), 'scenario name links to the detail page with the range')
assert(!list.includes('/results?hours=24') && !/slice\(0, 120\)/.test(list), 'the capped 24h history strip is gone')
assert(list.includes("can('apm.manage')") && detail.includes("can('apm.manage')"), 'actions only for managers')
assert(list.includes('export function ScenarioEditor'), 'editor still exported for the alert-rule test')
assert(list.includes('monitorPayload('), 'editor saves every stored field')
for (const k of ["'step'", "'run'"]) assert(detail.includes(k), `detail drawer URL state ${k}`)
assert(detail.includes("status: 'failed', step: name"), 'step drawer lists that step\'s failures')

const files = [
  'src/pages/apm/SyntheticsPage.tsx', 'src/pages/apm/SyntheticDetailPage.tsx',
  'src/pages/apm/synthetics/parts.tsx', 'src/pages/apm/synthetics/charts.tsx', 'src/features/apm/synthetics.ts',
]
for (const f of files) {
  const src = read(f)
  assert(!/text-\[\d+(\.\d+)?px\]/.test(src), `${f}: rem type scale only`)
  assert(!/fontSize:\s*\d/.test(src), `${f}: chart text goes through chartFont()`)
  const visible = src.replace(/^\s*(\/\/|\*|\/\*).*$/gm, '')
  assert(!/ClickHouse|PostgreSQL|Postgres\b|Redis|\bGolang\b/i.test(visible), `${f}: no internal component names in the UI`)
  assert(!/https?:\/\/(cdn|unpkg|fonts\.googleapis)/.test(src), `${f}: no CDN assets`)
}

/* ═══ (c) rendering ═════════════════════════════════════════════════════════════════════════ */

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true })
for (const name of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'DocumentFragment', 'Node', 'NodeFilter', 'CustomEvent', 'MutationObserver', 'Element', 'KeyboardEvent', 'MouseEvent', 'PointerEvent']) {
  if (dom.window[name]) globalThis[name] = dom.window[name]
}
class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
dom.window.ResizeObserver = ResizeObserverStub
dom.window.HTMLElement.prototype.scrollIntoView = function () {}
Object.assign(globalThis, {
  ResizeObserver: ResizeObserverStub,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
})
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
const { createRoot } = await import('react-dom/client')
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const { MemoryRouter, Routes, Route, useLocation } = await import('react-router-dom')

const T0 = '2026-11-08T10:00:00.000000+00:00'
const T1 = '2026-11-08T09:59:00.000000+00:00'
const monitor = {
  id: 's1', name: 'Checkout journey', enabled: true, status: 'down', check_interval: 60, timeout: 20, retry_count: 1,
  variables: { password: 'hunter2' }, verify_tls: false, notify_channels: [], tags: ['shop'], created_at: '2026-11-01T00:00:00+00:00',
  last_check_at: T0, runs: 120, failures: 3, uptime_pct: 97.5, avg_ms: 420, p95_ms: 900, last_run_at: T0,
  last_failure_at: T0, last_error: 'expected 200, got 401', last_failed_step: 'Log in',
  spark: Array.from({ length: 48 }, (_, i) => (i === 47 ? 50 : 100)),
  steps: [
    { name: 'Home', method: 'GET', url: 'https://shop.example.com/', assertions: [{ type: 'status_code', operator: 'eq', value: 200 }] },
    { name: 'Log in', method: 'POST', url: 'https://shop.example.com/api/login', body: '{"p":"{{password}}"}', assertions: [{ type: 'body_contains', value: 'Welcome' }], extract: [{ var: 'token', from: 'json', path: 'token' }] },
  ],
}
const summary = {
  range: { from: '2026-11-07T10:00:00+00:00', to: '2026-11-08T10:00:00+00:00', bucket_seconds: 1800 },
  totals: { runs: 120, passed: 117, failed: 3, uptime_pct: 97.5, avg_ms: 420, p50_ms: 400, p95_ms: 900, max_ms: 1500, first_run_at: T1, last_run_at: T0, outages: 1, downtime_s: 180 },
  series: [{ t: '2026-11-08T09:30:00+00:00', runs: 30, failed: 3, uptime_pct: 90, avg_ms: 420, p95_ms: 900 }],
  steps: [
    { index: 0, name: 'Home', method: 'GET', url: 'https://shop.example.com/', executed: 120, passed: 120, failed: 0, skipped: 0, pass_pct: 100, avg_ms: 120, p95_ms: 200, last_status: 200, status_codes: [200], timing: { connect_ms: 10, tls_ms: 20, ttfb_ms: 80, download_ms: 10 }, top_errors: [], series: [{ t: '2026-11-08T09:30:00+00:00', avg_ms: 120, failed: 0 }] },
    { index: 1, name: 'Log in', method: 'POST', url: 'https://shop.example.com/api/login', executed: 120, passed: 117, failed: 3, skipped: 0, pass_pct: 97.5, avg_ms: 300, p95_ms: 700, last_status: 401, status_codes: [200, 401], timing: null, top_errors: [{ error: 'expected 200, got 401', count: 3, last_seen: T0 }], series: [{ t: '2026-11-08T09:30:00+00:00', avg_ms: 300, failed: 3 }] },
  ],
  errors: [{ failed_step: 'Log in', error: 'expected 200, got 401', count: 3, first_seen: T1, last_seen: T0 }],
  outages: [{ start: T1, end: null, ongoing: true, duration_s: 180, failed_runs: 3, failed_step: 'Log in', error: 'expected 200, got 401' }],
}
const runRow = { timestamp: T0, status: 'down', success: false, total_ms: 410, steps_total: 2, steps_passed: 1, failed_step: 'Log in', error: 'expected 200, got 401', steps: [{ name: 'Home', ok: true, status_code: 200, ms: 110 }, { name: 'Log in', ok: false, status_code: 401, ms: 300 }] }
const runDetail = {
  ...runRow, location: 'appliance',
  steps: [
    { name: 'Home', method: 'GET', url: 'https://shop.example.com/', ok: true, status_code: 200, ms: 110, error: '', asserts: [{ type: 'status_code', ok: true, detail: 'status 200 eq 200' }], timing: { connect_ms: 10, tls_ms: 20, send_ms: 1, ttfb_ms: 70, download_ms: 9 }, server_ip: '203.0.113.7', response_bytes: 2048, content_type: 'text/html', redirects: 0, http_version: 'HTTP/1.1' },
    { name: 'Log in', method: 'POST', url: 'https://shop.example.com/api/login', ok: false, status_code: 401, ms: 300, error: 'expected 200, got 401', asserts: [{ type: 'body_contains', ok: false, detail: 'body does not contain the expected text' }], body_snippet: '{"error":"bad credentials"}' },
  ],
}
const calls = []
const writes = []
globalThis.__synCanManage = true
globalThis.__synApi = {
  get: async (url) => {
    calls.push(url)
    const u = new URL(url, 'http://x')
    const p = u.pathname
    if (p === '/apm/synthetics') return { data: { monitors: [globalThis.__synCanManage ? monitor : { ...monitor, variables: { password: M.MASK }, secrets_masked: true }], range: { from: u.searchParams.get('from'), to: u.searchParams.get('to') }, summary: { total: 1, up: 0, down: 1, disabled: 0, runs: 120, failures: 3 } } }
    if (p === '/apm/synthetics/s1') return { data: globalThis.__synCanManage ? monitor : { ...monitor, variables: { password: M.MASK }, secrets_masked: true } }
    if (p === '/apm/synthetics/s1/summary') return { data: summary }
    if (p === '/apm/synthetics/s1/runs') return { data: { total: 1, runs: [runRow] } }
    if (p === '/apm/synthetics/s1/run-detail') return { data: runDetail }
    if (p === '/settings/channels') return { data: { data: [] } }
    throw new Error(`unexpected GET ${url}`)
  },
  put: async (url, body) => { writes.push({ url, body }); return { data: { message: 'ok' } } },
  post: async (url) => { writes.push({ url }); return { data: runDetail } },
  delete: async (url) => { writes.push({ url }); return { data: {} } },
}
const cache = path.resolve('node_modules/.cache')
await mkdir(cache, { recursive: true })
const temporary = await mkdtemp(path.join(cache, 'synthetics-ui-'))
async function compile(entry) {
  const out = await build({ entryPoints: [entry], bundle: true, write: false, format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent', loader: { '.css': 'empty' }, plugins: [{ name: 'fixtures', setup(b) {
    b.onResolve({ filter: /^(react(?:\/.*)?|react-dom(?:\/.*)?|react-router-dom|@tanstack\/react-query)$/ }, (arg) => ({ path: import.meta.resolve(arg.path), external: true }))
    b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'fx' }))
    b.onResolve({ filter: /^@\/stores\/auth$/ }, () => ({ path: 'auth', namespace: 'fx' }))
    b.onResolve({ filter: /^@\/components\/ui\/Toast$/ }, () => ({ path: 'toast', namespace: 'fx' }))
    b.onResolve({ filter: /^echarts-for-react$/ }, () => ({ path: 'echarts', namespace: 'fx' }))
    b.onLoad({ filter: /.*/, namespace: 'fx' }, (arg) => ({ loader: 'js', resolveDir: process.cwd(), contents: {
      api: 'export const api = globalThis.__synApi; export function setApiToken() {}; export function getApiToken() { return null }',
      auth: 'export const useCan = () => (p) => globalThis.__synCanManage && p === "apm.manage"; export const useAuth = () => null',
      toast: 'export const toast = { success() {}, error() {} }',
      echarts: 'import { createElement } from "react"; export default function Chart() { return createElement("div", { "data-chart": "1" }) }',
    }[arg.path] }))
    b.onResolve({ filter: /^@\// }, (arg) => b.resolve(`./${arg.path.slice(2)}`, { resolveDir: path.resolve('src'), kind: arg.kind }))
  } }] })
  const file = path.join(temporary, `${path.basename(entry)}.mjs`)
  await writeFile(file, out.outputFiles[0].text)
  return import(pathToFileURL(file).href)
}
const { SyntheticsPage } = await compile('src/pages/apm/SyntheticsPage.tsx')
const { SyntheticDetailPage } = await compile('src/pages/apm/SyntheticDetailPage.tsx')
const root = createRoot(document.getElementById('root'))
let client
let key = 0
function Location() { const l = useLocation(); return React.createElement('output', { id: 'location' }, l.pathname + l.search) }
const settle = async (ms = 30) => act(async () => { await new Promise((r) => setTimeout(r, ms)) })
const location = () => new URL(document.getElementById('location').textContent, 'http://x')
const text = () => document.body.textContent
async function eventually(pred, message) {
  for (let i = 0; i < 60; i++) { await settle(20); if (pred()) return }
  assert(pred(), message)
}
async function render(url) {
  client?.clear()
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, refetchInterval: false } } })
  await act(async () => root.render(React.createElement(QueryClientProvider, { client, key: ++key },
    React.createElement(MemoryRouter, { initialEntries: [url] }, React.createElement(Location), React.createElement(Routes, null,
      React.createElement(Route, { path: '/apm/synthetics', element: React.createElement(SyntheticsPage) }),
      React.createElement(Route, { path: '/apm/synthetics/:id', element: React.createElement(SyntheticDetailPage) }))))))
  await settle()
}
const click = async (el, message = 'expected control') => {
  assert(el, message)
  await act(async () => { el.dispatchEvent(new dom.window.MouseEvent('mousedown', { button: 0, bubbles: true })); el.click() })
  await settle()
}
const byText = (sel, t) => [...document.querySelectorAll(sel)].find((e) => e.textContent.includes(t))
const spanHours = (url) => { const q = new URL(url, 'http://x').searchParams; return (Date.parse(q.get('to')) - Date.parse(q.get('from'))) / 3_600_000 }

try {
  // List: 24h by default, the name links to the detail page, actions for managers.
  await render('/apm/synthetics')
  await eventually(() => text().includes('Checkout journey'), 'list renders the scenario')
  const listCall = calls.find((c) => c.startsWith('/apm/synthetics?'))
  assert.equal(spanHours(listCall), 24, 'the list asks for the last 24h by default')
  const link = byText('a', 'Checkout journey')
  assert.equal(link.getAttribute('href'), '/apm/synthetics/s1')
  assert(document.querySelector('button[aria-label="Run Checkout journey now"]'), 'managers can run now')
  assert(byText('button', 'New scenario'))
  assert.equal(document.querySelector('[aria-label="Availability over the selected range"]').children.length, 48)
  assert(text().includes('97.50%') && text().includes('expected 200, got 401'))

  await render('/apm/synthetics?range=7d')
  await eventually(() => byText('a', 'Checkout journey'), 'list with 7d')
  assert.equal(byText('a', 'Checkout journey').getAttribute('href'), '/apm/synthetics/s1?range=7d', 'the link keeps the range')
  assert.equal(spanHours(calls.filter((c) => c.startsWith('/apm/synthetics?')).at(-1)), 168)

  // Read-only: no actions anywhere, masked configuration.
  globalThis.__synCanManage = false
  await render('/apm/synthetics')
  await eventually(() => text().includes('Checkout journey'), 'read-only list')
  assert(!document.querySelector('button[aria-label^="Run "]') && !byText('button', 'New scenario'), 'read-only users see no actions')
  await render('/apm/synthetics/s1')
  await eventually(() => text().includes('Configuration'), 'read-only detail')
  assert(!byText('button', 'Run now') && !byText('button', 'Edit'), 'no run/edit for read-only users')
  assert(text().includes(M.MASK) && !text().includes('hunter2'), 'variables masked')
  assert(text().includes(`Body contains "${M.MASK}"`), 'text assertion values masked for read-only users')
  globalThis.__synCanManage = true

  // Detail.
  calls.length = 0
  await render('/apm/synthetics/s1?range=7d')
  await eventually(() => text().includes('Duration by step'), 'detail renders')
  assert.equal(document.querySelector('h1').textContent, 'Checkout journey')
  assert.equal(byText('nav a', 'Synthetics').getAttribute('href'), '/apm/synthetics?range=7d', 'breadcrumb keeps the range')
  for (const p of ['/apm/synthetics/s1?', '/apm/synthetics/s1/summary?', '/apm/synthetics/s1/runs?']) {
    const c = calls.find((x) => x.startsWith(p))
    assert(c, `requested ${p}`)
    assert.equal(spanHours(c), 168, `${p} follows the range`)
  }
  assert(text().includes('Failing at step “Log in”'))
  assert(text().includes('1 outage') || text().includes('3m downtime'))
  assert(text().includes('hunter2'), 'managers see variable values')

  // A step opens ?step=1 with its failures, top errors and configured checks.
  calls.length = 0
  await click(byText('tbody tr', 'shop.example.com/api/login'), 'step row')
  assert.equal(location().searchParams.get('step'), '1')
  assert.equal(location().searchParams.get('range'), '7d', 'drawer keeps the range')
  await eventually(() => text().includes('Failures in this range'), 'step drawer')
  const failed = calls.map((c) => new URL(c, 'http://x')).find((u) => u.pathname.endsWith('/runs') && u.searchParams.get('status') === 'failed')
  assert(failed && failed.searchParams.get('step') === 'Log in', "the step drawer lists that step's failures")
  assert(text().includes('Body contains "Welcome"') && text().includes('{{token}}'), 'configured assertions and extracts')
  await click(document.querySelector('[aria-label="Close"]'), 'close')
  assert.equal(location().searchParams.get('step'), null)

  // A run opens ?run=<timestamp> with the waterfall, assertion results and the failure body.
  calls.length = 0
  await click(byText('tbody tr', 'Failed'), 'run row')
  assert.equal(location().searchParams.get('run'), T0)
  await eventually(() => text().includes('Waterfall'), 'run drawer')
  const detailCall = calls.find((c) => c.includes('/run-detail?'))
  assert.equal(new URL(detailCall, 'http://x').searchParams.get('at'), T0, 'run timestamp sent with its offset intact')
  assert(text().includes('203.0.113.7') && text().includes('2.0 KB') && text().includes('HTTP/1.1'))
  assert(text().includes('✗') && text().includes('body does not contain the expected text'))
  assert(text().includes('bad credentials'), 'failure body snippet')
  await click(document.querySelector('[aria-label="Close"]'), 'close run')
  assert.equal(location().searchParams.get('run'), null)

  // An error group filters the runs table to that step's failures.
  calls.length = 0
  await click(byText('button', 'expected 200, got 401'), 'error group')
  await eventually(() => calls.some((c) => c.includes('/runs?') && c.includes('status=failed')), 'runs refetched')
  const filtered = new URL(calls.find((c) => c.includes('/runs?') && c.includes('status=failed')), 'http://x')
  assert.equal(filtered.searchParams.get('step'), 'Log in')
  assert(text().includes('failed at “Log in”'), 'step filter chip shown')

  // Disabling sends the full configuration (variables and TLS setting kept).
  await click(document.querySelector('button[aria-label="Enabled"]'), 'enable switch')
  await eventually(() => writes.length > 0, 'PUT sent')
  assert.equal(writes[0].url, '/apm/synthetics/s1')
  assert.equal(writes[0].body.enabled, false)
  assert.deepEqual(writes[0].body.variables, { password: 'hunter2' })
  assert.equal(writes[0].body.verify_tls, false)
  assert.equal(writes[0].body.steps.length, 2)
} finally {
  await act(async () => root.unmount())
  client?.clear()
  dom.window.close()
  await rm(temporary, { recursive: true, force: true })
}

console.log('Passed: synthetics model (URL state, runs query, outage windows, formatting, sparkline, timing/waterfall, chips, chart shaping, assertion masking, save payload) and page wiring (detail route, 24h default, links, manager-only actions, rem scale, no internal names); rendered list/detail with step and run drawers, error filter and full-config enable toggle.')
// The rendered pages leave timers (range clock, polling) behind; every assertion has run, so end here.
process.exit(0)
