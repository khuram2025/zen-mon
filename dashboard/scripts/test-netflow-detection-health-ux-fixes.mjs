// Regression tests for the detection / health / UX acceptance fixes (wave C):
//  NF-03  overview exporter list and telemetry insights never read as "none" / "0 actionable" when
//         their queries failed; Health Collector tile says "checking" while pending; security
//         dashboard NetFlow panels and IP-group usage keep the last good data with a stale chip;
//  NF-29  contrast of the theme-aware primary fill text, inactive tabs, time-range picker, insight
//         links on tints and the security dashboard traffic stats (≥ 4.5:1 in both themes);
//  X-09   every reduced-sensitivity (sampled) exporter is listed;
//  FE-09  reverse-DNS hint lists every use; suggestions say when reverse DNS is off;
//  CR-06  Setup live-check headline for late / stopped exporters and its poll interval;
//  a11y   dialogs opened from code return focus to their opener; theme toggle has a name.
// Run from dashboard/: node scripts/test-netflow-detection-health-ux-fixes.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')
const entry = `
export * as M from '@/pages/netflow/overviewModel'
export * as HM from '@/pages/netflow/health/model'
export * as AM from '@/pages/netflow/anomalies/model'
export * as S from '@/pages/netflow/SettingsPage'
export * as NS from '@/pages/dashboards/nfStale'
export { TelemetryInsights } from '@/pages/netflow/components/TelemetryInsights'
export { ExporterHealthCard } from '@/pages/netflow/components/ExporterHealthCard'
export { DetectorCoverage, DetectorCoverageRow } from '@/pages/netflow/anomalies/DetectorCoverage'
export { TeamHeader } from '@/pages/dashboards/shared'
export { StaleChip } from '@/components/netflow/QueryPanel'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter } from 'react-router-dom'
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
const { M, HM, AM, S, NS, createElement: h, renderToStaticMarkup, MemoryRouter } = m
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }
const render = (el) => renderToStaticMarkup(h(MemoryRouter, null, el))
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/\s+/g, ' ')

const links = { anomalies: '/netflow/anomalies', health: '/netflow/health', exporter: (ip) => `/netflow/devices/${ip}` }
const err500 = { response: { status: 500, data: { detail: 'Query failed (ref nf-deadbeef)', error_id: 'nf-deadbeef', code: 'query_failed' } } }
const err403 = { response: { status: 403, data: { detail: 'Forbidden' } } }

/* ─── NF-03: insights with failed inputs ───────────────────────────── */
{
  const base = { windowEndsNow: true, health: [], statusAvailable: false, device: null, coverage: null, totals: null, links }
  // Everything failed (the acceptance repro: every /netflow/* request 500 or 403).
  const all = M.buildTelemetryInsights({ ...base, findings: { available: false, data: null }, failed: { exporterState: true, signals: true, coverage: true } })
  ok(all.allUnavailable && all.actionable.length === 0, 'every input failed: nothing evaluated')
  eq(all.unavailable, ['exporter receipt state', 'TCP reset and flag signals', 'anomaly findings', 'delivery coverage'], 'lists what was not evaluated')
  const sAll = M.insightsSummary(all, false)
  ok(sAll.badge === 'Unavailable' && !/\d/.test(sAll.badge), 'badge has no count')
  ok(!/Nothing actionable/.test(sAll.empty) && /could not be evaluated/.test(sAll.empty), 'no "nothing actionable" claim')
  ok(all.context.some((c) => c.id === 'status-unavailable' && /Neither the collector status nor the exporter list/.test(c.body)), 'context does not claim estimated states from nothing')
  const html = text(render(h(m.TelemetryInsights, { insights: all, links: { ...links, alerts: '/alerts' }, onRetry: () => {}, errorRef: 'nf-deadbeef' })))
  ok(!/0 actionable/.test(html) && !/Nothing actionable/.test(html), `rendered card has no zero claim: ${html.slice(0, 200)}`)
  ok(/Unavailable/.test(html) && /Retry/.test(html) && /nf-deadbeef/.test(html), 'rendered card shows unavailable, Retry and reference')

  // Some inputs failed, none actionable in the rest: incomplete, still no zero.
  const some = M.buildTelemetryInsights({ ...base, statusAvailable: true, device: { rst: { status: 'ok', pct: 1, rst_flows: 1, tcp_flows_with_flags: 100 } }, findings: { available: false, data: null }, failed: {} })
  ok(!some.allUnavailable && some.unavailable.length === 1, 'findings alone failed')
  const sSome = M.insightsSummary(some, false)
  ok(sSome.badge === 'Incomplete' && /anomaly findings could not be evaluated/.test(sSome.empty), `incomplete wording: ${sSome.empty}`)
  // Some failed, but something actionable: a lower bound.
  const lower = M.buildTelemetryInsights({ ...base, statusAvailable: true, device: { rst: { status: 'ok', pct: 12, rst_flows: 12, tcp_flows_with_flags: 100 } }, findings: { available: false, data: null }, failed: {} })
  const sLower = M.insightsSummary(lower, false)
  ok(sLower.badge === '1+ actionable' && /At least 1/.test(sLower.aria), 'count marked as a lower bound')
  const lowerHtml = text(render(h(m.TelemetryInsights, { insights: lower, links: { ...links, alerts: '/alerts' } })))
  ok(/Not evaluated \(query failed\): anomaly findings/.test(lowerHtml), 'list shows what was not evaluated')
  // Everything answered: the plain count, and the neutral empty sentence.
  const fine = M.buildTelemetryInsights({ ...base, statusAvailable: true, device: { rst: { status: 'ok', pct: 1, rst_flows: 1, tcp_flows_with_flags: 100 } }, findings: { available: true, data: { counts: { open: 0, by_severity_open: {} }, detectors: [] } }, failed: {} })
  const sFine = M.insightsSummary(fine, false)
  ok(sFine.badge === '0 actionable' && /Nothing actionable/.test(sFine.empty) && !sFine.incomplete, 'all inputs answered: 0 is real')
  // Pending: no count yet.
  ok(M.insightsSummary(M.buildTelemetryInsights({ ...base, statusAvailable: null, findings: null }), true).badge === '…', 'pending shows no count')
  // Historical windows do not evaluate exporter state: three inputs.
  const hist = M.buildTelemetryInsights({ ...base, windowEndsNow: false, findings: { available: false, data: null }, failed: { exporterState: true, signals: true, coverage: true } })
  ok(hist.allUnavailable && !hist.unavailable.includes('exporter receipt state'), 'historical: exporter state is not an input')
}

/* ─── NF-03: exporter list unknown, never "Exporters (0) / no exporter" ── */
{
  ok(M.exporterListUnknown({ windowEndsNow: true, rows: 0, statusFailed: true, trafficFailed: true, coverageFailed: false }), 'both live sources failed')
  ok(M.exporterListUnknown({ windowEndsNow: true, rows: 0, statusFailed: true, trafficFailed: false, coverageFailed: false }), 'one failed, the other listed nothing')
  ok(!M.exporterListUnknown({ windowEndsNow: true, rows: 3, statusFailed: true, trafficFailed: false, coverageFailed: false }), 'estimated rows from traffic are shown')
  ok(!M.exporterListUnknown({ windowEndsNow: true, rows: 0, statusFailed: false, trafficFailed: false, coverageFailed: true }), 'coverage is not a live source')
  ok(M.exporterListUnknown({ windowEndsNow: false, rows: 0, statusFailed: false, trafficFailed: true, coverageFailed: true }), 'historical: traffic and coverage failed')
  const device = { data: undefined, error: err500, status: 'error', refetch: () => {} }
  const card = (rowsError) => text(render(h(m.ExporterHealthCard, {
    rows: [], device, windowEndsNow: true, exporterLink: links.exporter, healthLink: '/netflow/health', rowsError,
  })))
  const failed = card({ error: err500, onRetry: () => {} })
  ok(!/Exporters \(0\)/.test(failed) && !/No exporter has sent flows/.test(failed), `no zero claim under 500: ${failed}`)
  ok(/Could not load the exporter list/.test(failed) && /unknown, not none/.test(failed) && /nf-deadbeef/.test(failed) && /Retry/.test(failed), 'error with reference and Retry')
  const forbidden = card({ error: err403 })
  ok(!/Exporters \(0\)/.test(forbidden) && /do not have access to the exporter list/.test(forbidden), 'lock message under 403')
  const loadingCard = text(render(h(m.ExporterHealthCard, { rows: [], rowsLoading: true, device, windowEndsNow: true, exporterLink: links.exporter, healthLink: '/netflow/health' })))
  ok(!/Exporters \(0\)/.test(loadingCard) && /Loading exporters/.test(loadingCard), 'no count while loading')
  const empty = card(null)
  ok(/Exporters \(0\)/.test(empty) && /No exporter has sent flows/.test(empty), 'a successful empty list still says so')
}

/* ─── NF-03: Health Collector tile caption ──────────────────────────── */
{
  eq(HM.collectorCaption('pending', undefined), 'Checking the collector…', 'pending is not "did not answer"')
  eq(HM.collectorCaption('error', undefined), 'Health could not be loaded', 'request failed')
  eq(HM.collectorCaption('success', { reachable: false, reasons: [] }), 'The collector health endpoint did not answer', 'unreachable collector')
  eq(HM.collectorCaption('success', { reachable: true, listen: '[::]:12055', reasons: [] }), 'Listening on UDP 12055', 'listening')
  eq(HM.collectorCaption('success', { reachable: true, reasons: ['delivery failing'] }), 'delivery failing', 'reason first')
}

/* ─── NF-03: stale NetFlow panels (security dashboard) ──────────────── */
{
  const fresh = { data: { data: [] }, error: null, status: 'success', dataUpdatedAt: 1000 }
  const stale1 = { data: { data: [1] }, error: err500, status: 'error', dataUpdatedAt: 5000, refetch: () => 'a' }
  const stale2 = { data: { data: [2] }, error: err500, status: 'error', dataUpdatedAt: 3000, refetch: () => 'b' }
  ok(NS.combinedStale([fresh]) === null, 'nothing stale')
  const c = NS.combinedStale([fresh, stale1, stale2])
  ok(c && c.status === 'error' && c.dataUpdatedAt === 3000, 'oldest successful fetch time')
  ok(NS.hasNfData('stale') && NS.hasNfData('ok') && !NS.hasNfData('error') && !NS.hasNfData('loading'), 'stats follow the chart')
  const chip = text(renderToStaticMarkup(h(m.StaleChip, { query: c })))
  ok(/Refresh failed/.test(chip) && /Retry/.test(chip), 'stale chip with Retry')
  const live = renderToStaticMarkup(h(MemoryRouter, null, h(m.TeamHeader, { title: 'Network & Security' })))
  const staleHdr = renderToStaticMarkup(h(MemoryRouter, null, h(m.TeamHeader, { title: 'Network & Security', live: 'stale' })))
  ok(/LIVE/.test(live) && !/STALE/.test(live), 'default badge is LIVE')
  ok(/STALE/.test(staleHdr) && !/LIVE/.test(staleHdr), 'a failed refresh never says LIVE')
  const dash = fs.readFileSync(path.join(src, 'pages/dashboards/NetworkSecurityDashboard.tsx'), 'utf8')
  const mini = dash.slice(dash.indexOf('<MiniStat label="At range end"'), dash.indexOf('<MiniStat label="Peak"') + 200)
  ok(!/text-(info|warning|accent)\b/.test(mini), 'traffic stats use body text, not status colours')
  ok(/hasNfData\(nfState\.series\) \? fmtBps\(trafficStats\.peak\)/.test(mini), 'peak shown whenever the chart is')
  ok(/live=\{nfStale \? 'stale' : 'live'\}/.test(dash) && /StaleChip query=\{trafficStale\}/.test(dash), 'dashboard wires the stale state')
}

/* ─── NF-03: IP-group usage refresh failure keeps the last good figures ── */
{
  const data = { data: { total: { flows: 10 } } }
  eq(S.groupUsageView({ data, error: err500, status: 'error' }), 'stale', 'refresh failed with data: stale, not blank')
  eq(S.groupUsageView({ data: undefined, error: err500, status: 'error' }), 'failed', 'failed without data')
  eq(S.groupUsageView({ data: undefined, error: null, status: 'pending' }), 'loading', 'loading')
  eq(S.groupUsageView({ data, error: null, status: 'success' }), 'ok', 'ok')
  eq(S.groupUsageView({ data: { data: { total: { flows: 0 } } }, error: null, status: 'success' }), 'no_records', 'no records: unknown')
  eq(S.groupUsageView({ data, error: err403, status: 'error' }), 'forbidden', '403 hides retained data')
}

/* ─── FE-09: reverse-DNS hint and suggestions wording ───────────────── */
{
  ok(/host profiles/.test(S.RDNS_USES_HINT) && /Show reverse-DNS names/.test(S.RDNS_USES_HINT) && /exporter-to-device suggestions/.test(S.RDNS_USES_HINT), 'hint lists every use')
  ok(/top talkers, endpoints, conversations and next hops/.test(S.RDNS_USES_HINT), 'every page with the option')
  eq(HM.reverseDnsSourceText({ reverse_dns: null, sources: { reverse_dns_status: 'disabled' } }), 'reverse DNS (turned off in NetFlow settings, not looked up)', 'disabled')
  eq(HM.reverseDnsSourceText({ reverse_dns: 'r1.example', sources: { reverse_dns_status: 'ok' } }), 'reverse DNS (r1.example)', 'name')
  eq(HM.reverseDnsSourceText({ reverse_dns: null, sources: {} }), 'reverse DNS (no name)', 'older API')
}

/* ─── CR-06: live-check headline and polling ───────────────────────── */
{
  ok(HM.OVERALL_TEXT.stopped.word === 'Not receiving' && HM.OVERALL_TEXT.stopped.tone === 'danger', 'stopped headline')
  ok(HM.OVERALL_TEXT.late.word === 'Late' && !/Working/.test(HM.OVERALL_TEXT.late.word), 'late headline is not "Working"')
  eq(HM.checkPollMs(null), 5000, 'waiting for the first answer')
  eq(HM.checkPollMs({ overall: 'waiting' }), 5000, 'waiting for flows')
  eq(HM.checkPollMs({ overall: 'stopped' }), 5000, 'watching a stopped exporter come back')
  eq(HM.checkPollMs({ overall: 'ready' }), 15000, 'slower once flows arrive')
}

/* ─── X-09: every sampled exporter is listed ───────────────────────── */
{
  const rs = [{ exporter: '::1', sampling: 256 }, { exporter: '127.0.0.62', sampling: 100 }, { exporter: '127.0.0.63', sampling: 100 }, { exporter: '127.0.0.67', sampling: 512 }]
  const t = AM.reducedSensitivityText(rs)
  ok(t.startsWith('Reduced sensitivity on 4 sampled exporters: 127.0.0.67 1:512, ::1 1:256'), t)
  ok(/127\.0\.0\.62 1:100, 127\.0\.0\.63 1:100/.test(t), 'all four, heaviest first')
  const det = { kind: 'syn_scan', label: 'SYN scan', status: 'ok', reason: null, required_fields: [], reduced_sensitivity: rs,
    notes: ['Reduced sensitivity on sampled exporters (127.0.0.67 1:512, ::1 1:256, 127.0.0.62 1:100 and 1 more): record-count thresholds see only the sampled share of the traffic', 'TCP flags exported for 99% of TCP records'] }
  eq(AM.detectorNotes(det), ['TCP flags exported for 99% of TCP records'], 'the shortened note is replaced by the full list')
  const html = text(renderToStaticMarkup(h('ul', null, h(m.DetectorCoverageRow, { d: det }))))
  ok(/127\.0\.0\.67 1:512/.test(html) && /127\.0\.0\.63 1:100/.test(html) && !/and 1 more/.test(html), `coverage list names every sampled exporter: ${html}`)
}

/* ─── NF-29: contrast of the theme-aware tokens and the fixed classes ── */
const css = fs.readFileSync(path.join(src, 'styles/globals.css'), 'utf8')
const block = (selector) => {
  const start = css.indexOf(`${selector} {`)
  assert.ok(start >= 0, selector)
  const body = css.slice(start, css.indexOf('}', start))
  return Object.fromEntries([...body.matchAll(/--([a-z0-9-]+):\s*(\d+)\s+(\d+)\s+(\d+);/g)].map((x) => [x[1], [+x[2], +x[3], +x[4]]]))
}
const themes = { light: block(':root'), dark: { ...block(':root'), ...block('.dark') } }
const lin = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
const ratio = (a, b) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}
const blend = (fg, bg, alpha) => fg.map((c, i) => c * alpha + bg[i] * (1 - alpha))
const report = []
for (const [theme, t] of Object.entries(themes)) {
  ok(t['on-primary'], `--on-primary in ${theme}`)
  // Primary buttons / selected segments: text on the primary fill and on its hover fill.
  for (const fill of [t.primary, t['primary-hover']]) {
    const r = ratio(t['on-primary'], fill)
    ok(r >= 4.5, `${theme} on-primary on primary ${r.toFixed(2)}`)
  }
  // Inactive tabs (text2 on surface2) and time-range picker buttons (text2 on surface2/40 over bg / surface).
  ok(ratio(t.text2, t.surface2) >= 4.5, `${theme} inactive tab ${ratio(t.text2, t.surface2).toFixed(2)}`)
  for (const base of ['bg', 'surface']) ok(ratio(t.text2, blend(t.surface2, t[base], 0.4)) >= 4.5, `${theme} picker on ${base}`)
  // Insight links (body text, underlined) on every status tint, and the stats' body text on surface2/30.
  for (const tone of ['danger', 'warning', 'info']) {
    for (const base of ['surface', 'bg']) ok(ratio(t.text, blend(t[tone], t[base], 0.1)) >= 4.5, `${theme} link on ${tone} tint`)
  }
  ok(ratio(t.text, blend(t.surface2, t.surface, 0.3)) >= 4.5 && ratio(t.text2, blend(t.surface2, t.surface, 0.3)) >= 4.5, `${theme} mini stats`)
  report.push(`${theme} on-primary ${ratio(t['on-primary'], t.primary).toFixed(2)}:1, tab ${ratio(t.text2, t.surface2).toFixed(2)}:1`)
}
// The old pairs really failed (so the checks above are meaningful).
ok(ratio([255, 255, 255], themes.dark.primary) < 4.5, 'white on the dark primary fails (what the token fixes)')
ok(ratio(themes.light.muted, themes.light.surface2) < 4.5, 'muted on surface2 fails in light (what the tabs fix)')
const tailwind = fs.readFileSync(path.join(root, 'tailwind.config.js'), 'utf8')
ok(tailwind.includes("'on-primary': 'rgb(var(--on-primary) / <alpha-value>)'"), 'tailwind on-primary colour')
const button = fs.readFileSync(path.join(src, 'components/ui/Button.tsx'), 'utf8')
ok(/default: 'bg-primary text-on-primary hover:bg-primary-hover/.test(button), 'primary buttons use on-primary (and a hover fill it reads on)')
const tabs = fs.readFileSync(path.join(src, 'components/ui/Tabs.tsx'), 'utf8')
ok(/bg-surface2 p-1 text-text2/.test(tabs) && !/p-1 text-muted/.test(tabs), 'inactive tabs in text2')
const picker = fs.readFileSync(path.join(src, 'components/TimeRangePicker.tsx'), 'utf8')
ok(/active \? 'bg-primary text-on-primary' : 'text-text2 hover:text-text'/.test(picker) && !/text-black/.test(picker), 'time-range picker')
const insightsSrc = fs.readFileSync(path.join(src, 'pages/netflow/components/TelemetryInsights.tsx'), 'utf8')
const rowsPart = insightsSrc.slice(insightsSrc.indexOf('function InsightRow'))
ok(!/text-primary/.test(rowsPart), 'no primary-coloured links on insight tints')

/* ─── a11y: dialog focus return and the theme toggle's name ─────────── */
{
  const dialog = fs.readFileSync(path.join(src, 'components/ui/Dialog.tsx'), 'utf8')
  ok(/onCloseAutoFocus=\{handleCloseAutoFocus\}/.test(dialog) && /el\.isConnected/.test(dialog), 'dialogs return focus to their opener')
  const layout = fs.readFileSync(path.join(src, 'components/Layout.tsx'), 'utf8')
  ok(/aria-label=\{theme === 'dark' \? 'Switch to light theme' : 'Switch to dark theme'\}/.test(layout), 'theme toggle has an accessible name')
}

console.log(`NetFlow detection/health/UX fix tests passed (${checks} checks). Contrast: ${report.join('; ')}`)
