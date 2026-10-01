// NetFlow breakdowns (WP B2) pure logic: whole-scope shares with a remainder and every slice in
// the legend (NF-25), stable category colours, ranking choices (NF-24), drill-down links that keep
// the full filter and a frozen window (NF-08), canonical conversation links, service labels for
// portless protocols and ICMP (NF-20), table sorting/paging, the unrecorded 0.0.0.0 address,
// X-51 sub-1 formatting in lib/utils, and rendered card parts.
// Run from dashboard/: node scripts/test-netflow-breakdowns.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')
const entry = `
export * as M from '@/pages/netflow/cards/model'
export { ShareLegend, ShareStrip, RankedBars, HostLabel, HostLink, CardFootnote, Segmented } from '@/pages/netflow/cards/parts'
export { conversationMatches } from '@/pages/netflow/cards/ConversationsCard'
export { interfaceTitle, UTILIZATION_LINK_TITLE } from '@/pages/netflow/cards/InterfacesCard'
export { capacityLink } from '@/lib/netflow/interfaceLinks'
export { flagEmoji } from '@/pages/netflow/cards/CountriesCard'
export { describeTcpFlags } from '@/pages/netflow/details/ConversationDetailPanel'
export { SECTIONS } from '@/pages/netflow/SectionPage'
export * as U from '@/lib/utils'
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
const { M, U, SECTIONS, createElement: h, renderToStaticMarkup, MemoryRouter } = m
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }
const render = (el) => renderToStaticMarkup(h(MemoryRouter, null, el))
const qs = (href) => new URLSearchParams(href.slice(href.indexOf('?') + 1))

/* ─── NF-25: whole-scope shares, remainder, every slice listed ─────── */
const items = [
  { name: 'Web', bytes: 600 },
  { name: 'DNS', bytes: 200 },
  { name: 'SSH', bytes: 100 },
]
const opt = { key: (i) => i.name, label: (i) => i.name, value: (i) => i.bytes, colors: true }
let r = M.shareRows(items, { ...opt, total: 1000 })
eq(r.basis, 'scope', 'a usable whole-scope total is the denominator')
eq(r.denominator, 1000)
eq(r.rows.map((x) => x.pct), [60, 20, 10], 'shares are of the whole scope, not of the listed rows')
eq(r.remainder?.value, 100, 'what the listed rows leave of the scope is an explicit remainder')
eq(r.remainder?.pct, 10)
ok(M.reconciles(r), 'listed + remainder = denominator')
// The listed subset only (max) folds the rest into the remainder too.
r = M.shareRows(items, { ...opt, total: 1000, max: 2 })
eq(r.rows.length, 2)
eq(r.remainder?.value, 200, 'rows beyond max join the remainder')
ok(M.reconciles(r))
// Without a scope total the shares say they are of the subtotal.
r = M.shareRows(items, { ...opt })
eq(r.basis, 'subtotal')
eq(r.remainder, null, 'no remainder against a subtotal of everything listed')
ok(Math.abs(r.rows[0].pct - 66.6667) < 0.01)
// A scope total smaller than the rows (inconsistent response) is never used.
eq(M.shareRows(items, { ...opt, total: 10 }).basis, 'subtotal')
// Nothing in scope: no percentages are invented.
r = M.shareRows([{ name: 'x', bytes: 0 }], { ...opt, total: 0 })
eq(r.rows[0].pct, null, 'zero denominator gives null shares, not 0% or NaN')
// Negative values (never expected) do not shrink the denominator.
eq(M.shareRows([{ name: 'a', bytes: -5 }, { name: 'b', bytes: 5 }], { ...opt, total: 5 }).rows[0].value, 0)

/* Stable colours: by entity name, never by rank. */
const slotsA = M.assignColorSlots(['DNS', 'Web', 'SSH'])
const slotsB = M.assignColorSlots(['SSH', 'DNS', 'Web'])
eq([...slotsA.entries()].sort(), [...slotsB.entries()].sort(), 'colour assignment does not depend on order')
eq(new Set(slotsA.values()).size, 3, 'distinct names get distinct slots while slots remain')
eq(M.assignColorSlots(['DNS']).get('DNS'), M.hashName('DNS') % M.SERIES_CLASSES.length, 'a name keeps its hashed slot when uncontested')
ok(M.hashName('DNS') === M.hashName('DNS') && M.hashName('DNS') !== M.hashName('Web'), 'stable FNV hash')
ok(M.seriesClass(undefined) === M.REMAINDER_CLASS, 'the remainder is neutral, never a category hue')
ok(M.SERIES_CLASSES.includes(M.seriesClass(9)), 'slot index wraps')
// Every slice appears in the legend, the remainder last.
const legendHtml = render(h(m.ShareLegend, { result: M.shareRows(items, { ...opt, total: 1000 }) }))
for (const name of ['Web', 'DNS', 'SSH', 'All other traffic']) ok(legendHtml.includes(name), `legend lists ${name}`)
ok(legendHtml.indexOf('All other traffic') > legendHtml.indexOf('SSH'), 'remainder row closes the legend')
ok(legendHtml.includes('60%') && legendHtml.includes('10%'), 'legend states shares of the scope')
ok(legendHtml.includes('600 bytes'), 'exact value available to screen readers / titles')
const stripHtml = render(h(m.ShareStrip, { result: M.shareRows(items, { ...opt, total: 1000 }) }))
eq((stripHtml.match(/flex-grow/g) || []).length, 4, 'strip has one segment per row plus the remainder')

/* Ranked (non-additive) bars: own share of scope, width relative to the top row. */
const rr = M.rankRows([{ ip: 'a', v: 50 }, { ip: 'b', v: 25 }], { key: (x) => x.ip, label: (x) => x.ip, value: (x) => x.v, total: 60 })
eq(rr.map((x) => x.width), [100, 50])
ok(Math.abs(rr[0].pct - 83.333) < 0.01, 'non-additive rows state their own share of the scope')
eq(M.rankRows([{ ip: 'a', v: 5 }], { key: (x) => x.ip, label: (x) => x.ip, value: (x) => x.v }).map((x) => x.pct), [null])
const barsHtml = render(h(m.RankedBars, { rows: rr, label: (row) => row.label }))
ok(barsHtml.includes('width:100%') && barsHtml.includes('width:50%'))

/* ─── NF-24: ranking choice ────────────────────────────────────────── */
eq(M.parseRankBy('dst', 'src'), 'dst')
eq(M.parseRankBy('bogus', 'total'), 'total', 'unknown values fall back')
eq(M.parseRankBy(null, 'src'), 'src')
eq(M.defaultRankBy('top-talkers'), 'src')
eq(M.defaultRankBy('top-endpoints'), 'total')
eq(M.parseRankMetric('flows'), 'flows')
eq(M.parseRankMetric('x'), 'bytes')
eq(M.rankField('src', 'bytes'), 'src_bytes')
eq(M.rankField('dst', 'flows'), 'dst_flows')
eq(M.rankField('total', 'packets'), 'packets')
eq(M.rankPhrase('src'), 'Ranked by bytes sent')
eq(M.rankPhrase('dst', 'flows'), 'Ranked by flows received')
ok(M.rankPhrase('total').includes('took part in'))
eq(M.RANK_BY_OPTIONS.map((o) => o.value), ['src', 'dst', 'total'])

/* ─── NF-08: links keep the whole filter and freeze the window ─────── */
const W = { key: '24h', mode: 'relative', hours: 24, from: '2026-09-30T00:00:00.000Z', to: '2026-10-01T00:00:00.000Z', tz: 'UTC', label: 'Last 24h' }
const base = { exporter: '10.0.0.1', protocol: 6, dscp: 0, iface: '3@10.0.0.1', netclass: 'Public', count: 'edge', hour: 0 }
const meta = { from: '2026-09-30T00:00:12.000Z', to: '2026-10-01T00:00:12.000Z' }
let link = M.forensicsLink(base, { talker: '192.0.2.5' }, W, meta)
let p = qs(link)
ok(link.startsWith('/netflow/forensics?'))
eq(p.get('exporter'), '10.0.0.1')
eq(p.get('protocol'), '6')
eq(p.get('dscp'), '0', 'numeric zero survives the link (never a truthiness test)')
eq(p.get('hour'), '0')
eq(p.get('iface'), '3@10.0.0.1', 'exporter-qualified interface kept')
eq(p.get('netclass'), 'Public')
eq(p.get('count'), 'edge')
eq(p.get('talker'), '192.0.2.5', 'the clicked host is "either side" (talker)')
eq(p.get('range'), 'custom', 'evidence links freeze the window')
eq(p.get('from'), '2026-09-30T00:00:12.000Z', 'frozen to the bounds of the data on screen (meta.window)')
eq(p.get('to'), '2026-10-01T00:00:12.000Z')
eq(p.get('run'), '1', 'forensics runs on open')
ok(!p.has('hours'), 'never hours together with explicit bounds')
// Without meta the relative window is frozen on the appliance clock (any bounds, 24 h apart).
p = qs(M.forensicsLink(base, {}, W, null))
eq(Date.parse(p.get('to')) - Date.parse(p.get('from')), 24 * 3600e3)
// Patch can remove keys (undefined) and keep zero.
p = qs(M.forensicsLink(base, { protocol: undefined, dst_port: 0 }, W, meta))
ok(!p.has('protocol'))
eq(p.get('dst_port'), '0')
// Host profile: page context without another host's keys.
link = M.hostLink('2001:db8::1', { ...base, talker: '10.9.9.9', src: '10.1.1.1' }, W, meta)
ok(link.startsWith('/netflow/hosts/2001%3Adb8%3A%3A1?'), 'host profile path is encoded')
p = qs(link)
ok(!p.has('talker') && !p.has('src'), 'host-specific keys are not inherited by another host')
eq(p.get('protocol'), '6')
eq(p.get('range'), 'custom')
// "View all": live window, full filter, extras.
link = M.sectionLink('top-talkers', base, W, { by: 'dst' })
p = qs(link)
eq(p.get('range'), '24h', 'section links keep the live preset')
eq(p.get('by'), 'dst', 'the ranking selected on the overview is carried (NF-24)')
eq(p.get('dscp'), '0')
eq(qs(M.sectionLink('protocols', {}, W, undefined, '10.0.0.9')).get('exporter'), '10.0.0.9', 'exporter deep-dive scope')

/* Canonical conversation links: client → server and the reverse half. */
const conv = { client: '10.1.0.10', server: '93.184.216.34', protocol: 6, drill: { protocol: 6, service_port: 443 }, orientation: 'client_server' }
const cl = M.conversationLinks(conv, { ...base, talker: '10.1.0.10', src: '1.1.1.1' }, W, meta)
p = qs(cl.forward)
eq([p.get('src'), p.get('dst'), p.get('service_port'), p.get('protocol')], ['10.1.0.10', '93.184.216.34', '443', '6'])
ok(!p.has('talker'), 'a pair query replaces the page host filter')
p = qs(cl.reverse)
eq([p.get('src'), p.get('dst')], ['93.184.216.34', '10.1.0.10'], 'the reverse half swaps the endpoints')
eq(cl.labels.forward, 'Client → server')
eq(M.orientationLabels('address_order').a, 'Endpoint A', 'unknown direction is not called client/server')
// ICMP conversations drill by type/code (dst_port), not by a port.
p = qs(M.conversationLinks({ client: 'a', server: 'b', protocol: 1, drill: { protocol: 1, dst_port: 2048 }, orientation: 'client_server' }, {}, W, meta).forward)
eq(p.get('dst_port'), '2048')
ok(!p.has('service_port'))

/* ─── NF-20: service labels ────────────────────────────────────────── */
eq(M.serviceText({ service: 'HTTPS', service_port: 443, protocol: 6, protocol_name: 'TCP', port_applicable: true }), 'HTTPS 443/tcp')
eq(M.serviceText({ service: null, service_port: null, protocol: 50, protocol_name: 'ESP', port_applicable: false }), 'Not applicable (ESP)', 'portless protocols never show port 0')
eq(M.serviceText({ service: 'Echo request', service_port: null, icmp_type_code: 2048, protocol: 1, protocol_name: 'ICMP', port_applicable: false }), 'Echo request (ICMP)', 'ICMP by message type')
eq(M.serviceText({ service: null, service_port: null, icmp_type_code: 768, protocol: 1, protocol_name: 'ICMP', port_applicable: false }), 'Type 3 (ICMP)')
eq(M.serviceText({ service: null, service_note: 'Ephemeral ports only', service_port: null, protocol: 17, protocol_name: 'UDP', port_applicable: true }), 'Ephemeral ports only')
eq(M.CONFIDENCE_LABELS.low, 'Low confidence (guess)')

/* TCP flags in the conversation detail never fabricate zero flags. */
eq(m.describeTcpFlags(null, false, 6).primary, 'Unknown', 'unexported flags are unknown, never "none"')
eq(m.describeTcpFlags(0, true, 6).primary, 'None set', 'exported all-zero flags are a real value')
ok(m.describeTcpFlags(0x12, true, 6).primary.includes('SYN') && m.describeTcpFlags(0x12, true, 6).primary.includes('ACK'))
ok(/not applicable|n\/a/i.test(m.describeTcpFlags(null, false, 17).primary), 'non-TCP has no flags')

/* ─── Sorting and paging ───────────────────────────────────────────── */
const rows = [{ k: 'b', v: 2 }, { k: 'a', v: 2 }, { k: 'c', v: null }, { k: 'd', v: 9 }]
eq(M.sortRowsBy(rows, (x) => x.v, 'desc').map((x) => x.k), ['d', 'b', 'a', 'c'], 'ties keep input order, nulls last')
eq(M.sortRowsBy(rows, (x) => x.v, 'asc').map((x) => x.k), ['b', 'a', 'd', 'c'], 'nulls stay last ascending too')
eq(M.sortRowsBy([{ s: 'port 10' }, { s: 'port 9' }], (x) => x.s, 'asc').map((x) => x.s), ['port 9', 'port 10'], 'numeric-aware text sort')
eq(M.pageCount(205), 5)
eq(M.pageCount(50), 1)
eq(M.pageCount(0), 1)
eq(M.pageCount(null), 1)
eq(M.pageSlice([...Array(120).keys()], 2).length, 20)
eq(M.showingText(50, 50, 205), 'Showing 51–100 of 205')
eq(M.showingText(0, 3, null), 'Showing 1–3')
ok(M.showingText(250, 0, 205).includes('205'))
eq(M.parsePage('3'), 2)
eq(M.parsePage('0'), 0)
eq(M.parsePage('-4'), 0)
eq(M.parsePage('abc'), 0)
eq(M.PAGE_SIZE, 50, 'section pages page at 50 rows')
eq(M.splitPct(3, 1), { a: 75, b: 25 })
eq(M.splitPct(0, 0), null)
eq(M.totalOf(null, 'bytes'), null)

/* ─── Section registry ─────────────────────────────────────────────── */
for (const key of ['top-talkers', 'top-endpoints', 'conversations', 'applications', 'protocols', 'ports', 'dscp', 'tcp-flags', 'network-classes', 'interfaces', 'countries', 'asns', 'next-hops', 'ip-groups']) {
  ok(SECTIONS[key]?.endpoint?.startsWith('/netflow/'), `section ${key} is registered`)
}
for (const [k, s] of Object.entries(SECTIONS)) {
  if (['top-talkers', 'top-endpoints', 'conversations', 'ports', 'interfaces', 'next-hops'].includes(k)) eq(s.paging, 'server', `${k} pages on the server`)
}
ok(SECTIONS['top-talkers'].controls.includes('by') && SECTIONS['top-endpoints'].controls.includes('by'), 'sent/received/total selector on host rankings')
ok(SECTIONS.countries.controls.includes('side') && SECTIONS.asns.controls.includes('side'))
ok(SECTIONS['top-talkers'].controls.includes('resolve') && SECTIONS.conversations.controls.includes('resolve'), 'rDNS is opt-in')

/* ─── Hosts: rDNS, profile link, unrecorded address ───────────────── */
let html = render(h(m.HostLabel, { ip: '10.1.0.10', hostname: 'files.corp', resolvedAt: new Date(Date.now() - 120e3).toISOString(), profileHref: '/netflow/hosts/10.1.0.10', onFilter: () => {} }))
ok(html.includes('files.corp') && html.includes('rDNS'), 'resolved name and its freshness are shown')
ok(html.includes('href="/netflow/hosts/10.1.0.10"'), 'host IPs link to the host profile')
ok(html.includes('aria-label="Filter the page to host 10.1.0.10"'), 'icon button is named')
html = render(h(m.HostLabel, { ip: '10.1.0.11', pending: true }))
ok(html.includes('Name lookup pending'))
ok(M.isUnspecifiedIp('0.0.0.0') && M.isUnspecifiedIp('::') && M.isUnspecifiedIp('::ffff:0.0.0.0') && !M.isUnspecifiedIp('10.0.0.0'))
html = render(h(m.HostLabel, { ip: '0.0.0.0', profileHref: '/netflow/hosts/0.0.0.0' }))
ok(html.includes('Address not recorded') && !html.includes('href='), '0.0.0.0 is never presented as a host with a profile')
html = render(h(m.HostLink, { ip: '0.0.0.0', href: '/x' }))
ok(!html.includes('href=') && html.includes('Address not recorded'))
html = render(h(m.HostLink, { ip: '::ffff:192.0.2.1', href: '/netflow/hosts/192.0.2.1' }))
ok(html.includes('>192.0.2.1<'), 'IPv4-mapped addresses display as IPv4')

/* Card footnote: the denominator, the source, and filters a card does not apply. */
const nfMeta = { window: { from: meta.from, to: meta.to, mode: 'relative', tz: 'UTC', source: 'rollup', granularity_s: 3600 }, filters: { protocol: 6 }, warnings: [{ code: 'x', message: 'Partial coverage' }], truncated: { limit: 5, returned: 5, more: true } }
html = render(h(m.CardFootnote, { meta: nfMeta, filter: { protocol: 6, dscp: 10 }, basis: 'Share of 1 TB', intentional: {} }))
ok(html.includes('Share of 1 TB'))
ok(html.includes('hourly rollup'), 'data source disclosed')
ok(html.includes('Not filtered by'), 'a requested filter the endpoint did not apply is disclosed')
ok(html.includes('Top 5 shown'))
ok(html.includes('Partial coverage'), 'API warnings shown')
html = render(h(m.CardFootnote, { meta: nfMeta, filter: { protocol: 6, dscp: 10 }, intentional: { dscp: 'this card breaks traffic down by DSCP' } }))
ok(html.includes('this card breaks traffic down by DSCP'), 'intentional self-filter drop explains itself')

/* Segmented control is a keyboard-operable radio group. */
html = render(h(m.Segmented, { label: 'Rank hosts by', value: 'dst', options: M.RANK_BY_OPTIONS, onChange: () => {} }))
ok(html.includes('role="radiogroup"') && html.includes('aria-label="Rank hosts by"'))
eq((html.match(/aria-checked="true"/g) || []).length, 1)

/* Misc helpers. */
eq(m.interfaceTitle({ display_name: 'Gi0/1', device_hostname: null, exporter_ip: '::ffff:10.0.0.1', ifindex: 2 }), 'Gi0/1 on 10.0.0.1', 'interface identity names its exporter')
eq(m.interfaceTitle({ display_name: '', device_hostname: 'edge-1', exporter_ip: '10.0.0.1', ifindex: 7 }), 'ifIndex 7 on edge-1')
/* Interfaces breakdown: no utilization % of its own (it counts count=all copies and gaps as idle);
   each row links to the Capacity page, the one utilization definition, for that exporter interface. */
{
  const w = { key: '24h', mode: 'relative', hours: 24, from: '2026-09-29T00:00:00Z', to: '2026-09-30T00:00:00Z', tz: 'UTC', label: 'Last 24h' }
  const link = m.capacityLink('1@198.18.220.8', w)
  ok(link.startsWith('/netflow/capacity?'), link)
  const q = new URLSearchParams(link.split('?')[1])
  eq(q.get('iface'), '1@198.18.220.8', 'capacity link names the exporter interface')
  eq(q.get('range'), '24h', 'and keeps the window')
  ok(/duplicate ingress and egress export counted once/.test(m.UTILIZATION_LINK_TITLE), 'the link says why utilization lives on Capacity')
}
eq(m.flagEmoji('GB'), '🇬🇧')
eq(m.flagEmoji(null), '')
ok(m.conversationMatches({ client: '10.0.0.1', server: '10.0.0.2', service: 'HTTPS', application: 'Web', protocol_name: 'TCP', service_port: 443, client_hostname: 'pc.corp', server_hostname: null }, 'pc.co'))
ok(!m.conversationMatches({ client: '10.0.0.1', server: '10.0.0.2', service: null, application: 'Web', protocol_name: 'TCP', service_port: null }, 'ssh'))

/* ─── X-51: lib/utils never prints 'undefined' units ───────────────── */
for (const v of [0.5, 0.001, 1e-9, -0.5]) {
  ok(!U.formatBps(v).includes('undefined'), `formatBps(${v})`)
  ok(!U.formatBytes(v).includes('undefined'), `formatBytes(${v})`)
  ok(!U.formatBpsAxis(v).includes('undefined'), `formatBpsAxis(${v})`)
}
eq(U.formatBps(0.5), '<1 bps')
eq(U.formatBytes(0.5), '<1 B')
eq(U.formatBpsAxis(0.25), '0.25')
eq(U.formatBps(1500), '1.50 Kbps', 'platform scaling unchanged')
eq(U.formatBytes(2048), '2.0 KB', 'platform 1024 scaling unchanged')
eq(U.formatBps(5e15).endsWith('Tbps'), true, 'clamped at the top unit')
eq(U.formatBytes(NaN), '0 B')

console.log(`test-netflow-breakdowns: ${checks} checks passed`)
