// NetFlow frontend foundation (contract §8, §10): filter contract, windows, formatting, panel
// states, API envelope, links, navigation, rendered state components and status-token contrast.
// Run from dashboard/: node scripts/test-netflow-foundation.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

// Deterministic default zone for the parts that use the viewer's timezone (links, labels).
process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')

// Bundle the modules under test (and React for the render checks) into one self-contained ESM
// module; `@/` resolves like the app's alias.
const entry = `
export * as filters from '@/lib/netflow/filters'
export * as win from '@/lib/netflow/window'
export * as fmt from '@/lib/netflow/format'
export * as status from '@/lib/netflow/status'
export * as links from '@/lib/netflow/links'
export * as nfapi from '@/lib/netflow/api'
export * as ip from '@/lib/netflow/ip'
export * as useFlowFilterModule from '@/lib/netflow/useFlowFilter'
export * as nav from '@/components/layout/navigation'
export { api } from '@/lib/api'
export { QueryPanel } from '@/components/netflow/QueryPanel'
export { StatusTile } from '@/components/netflow/StatusTile'
export { FilterChips } from '@/components/netflow/FilterChips'
export { DataQualityBar } from '@/components/netflow/DataQualityBar'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter } from 'react-router-dom'
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
const { filters: F, win: W, fmt, status: S, links: L, nfapi: A, ip: IP, nav } = m
assert.equal(typeof m.useFlowFilterModule.useFlowFilter, 'function')
assert.equal(typeof W.useNfWindow, 'function')
const sp = (q) => new URLSearchParams(q)

/* ─── IP parsing ───────────────────────────────────────────────────── */
for (const ok of ['10.0.0.1', '0.0.0.0', '255.255.255.255']) assert(IP.isIPv4(ok), ok)
for (const bad of ['10.1', '256.0.0.1', '010.0.0.1', '1.2.3.4.5', '1.2.3.']) assert(!IP.isIPv4(bad), bad)
for (const ok of ['::', '::1', '1::', '2001:db8::1', '1:2:3:4:5:6:7:8', '1:2:3:4:5:6:7::', '::ffff:10.0.0.1', '1:2:3:4:5:6:10.0.0.1', 'FE80::1']) {
  assert(IP.isIPv6(ok), ok)
}
for (const bad of ['1:2:3:4:5:6:7:8:9', '1:2:3:4:5:6:7:8::', ':1:2:3:4:5:6:7', '1:::2', 'fe80::1%eth0', '12345::', '::ffff:10.0.0', '10.0.0.1']) {
  assert(!IP.isIPv6(bad), bad)
}
assert.deepEqual(IP.parseIpOrCidr('10.0.0.0/8'), { address: '10.0.0.0', prefix: 8, version: 4 })
assert.equal(IP.parseIpOrCidr('10.0.0.0/33'), null)
assert.equal(IP.parseIpOrCidr('2001:db8::/129'), null)
assert.equal(IP.parseIpOrCidr('2001:db8::/32').prefix, 32)

/* ─── Filters: parse / write / API ─────────────────────────────────── */
// Numeric zero is a value, never "no filter" (NF-12).
let f = F.parseFlowFilter(sp('dscp=0&port=0&protocol=0&hour=0&src_port=0&min_bytes=0&asn=0'))
assert.deepEqual(f, { protocol: 0, port: 0, src_port: 0, dscp: 0, hour: 0, asn: 0, min_bytes: 0 })
assert.equal(F.writeFlowFilter(sp('range=24h'), f).toString(), 'range=24h&protocol=0&port=0&src_port=0&dscp=0&hour=0&asn=0&min_bytes=0')
assert.deepEqual(F.flowFilterToApiParams(f), { protocol: '0', port: '0', src_port: '0', dscp: '0', hour: '0', asn: '0', min_bytes: '0' })
assert.equal(F.isFlowFilterEmpty(f), false)
assert.equal(F.countFlowFilters(f), 7)

// Legacy read aliases; the canonical key wins; writing drops the alias.
assert.deepEqual(F.parseFlowFilter(sp('proto=17')), { protocol: 17 })
assert.deepEqual(F.parseFlowFilter(sp('protocol=6&proto=17')), { protocol: 6 })
assert.deepEqual(F.parseFlowFilter(sp('dscp_eq=46')), { dscp: 46 })
assert.equal(F.writeFlowFilter(sp('proto=17&dscp_eq=46&range=1h'), { protocol: 17 }).toString(), 'range=1h&protocol=17')

// CIDR and IPv6 on host keys; talker is IP or CIDR on either side.
f = F.parseFlowFilter(sp('talker=10.0.0.0/8&src=2001:DB8::/32&dst=::ffff:10.0.0.9&not_talker=192.0.2.7&exporter=2001:db8::1'))
assert.deepEqual(f, { exporter: '2001:db8::1', talker: '10.0.0.0/8', src: '2001:db8::/32', dst: '::ffff:10.0.0.9', not_talker: '192.0.2.7' })
let d = F.parseFlowFilterDetailed(sp('talker=10.1&exporter=10.0.0.0/8&src=010.0.0.1&dst=fe80::1%25eth0'))
assert.deepEqual(d.filter, {})
assert.deepEqual(Object.keys(d.errors).sort(), ['dst', 'exporter', 'src', 'talker'])
assert.equal(d.invalid.talker, '10.1')
assert.match(d.errors.talker, /IP address or CIDR/)
assert.match(d.errors.exporter, /IPv4 or IPv6 address/)

// Interfaces: full 32-bit ifIndex, optionally qualified by exporter (X-19, NF-33).
assert.deepEqual(F.parseFlowFilter(sp('iface=70000')), { iface: '70000' })
assert.deepEqual(F.parseFlowFilter(sp('iface=007&iface_dir=in')), { iface: '7', iface_dir: 'in' })
assert.deepEqual(F.parseFlowFilter(sp('iface=3@192.0.2.1')), { iface: '3@192.0.2.1' })
assert.deepEqual(F.parseFlowFilter(sp('iface=5@2001:DB8::1')), { iface: '5@2001:db8::1' })
assert.deepEqual(F.parseIfaceValue('3@192.0.2.1'), { ifindex: 3, exporter: '192.0.2.1' })
assert.deepEqual(F.parseIfaceValue('4294967295'), { ifindex: 4294967295 })
assert.equal(F.formatIfaceValue(3, '192.0.2.1'), '3@192.0.2.1')
for (const bad of ['4294967296', '3@nope', '-1', '3@', 'x']) {
  assert.ok(F.parseFlowFilterDetailed(sp(`iface=${encodeURIComponent(bad)}`)).errors.iface, bad)
}

// Enumerations, ranges, port sets, timezones.
d = F.parseFlowFilterDetailed(sp('tcpflag=synonly&hour=24&dow=0&ip_version=5&direction=in&fw_event=deny&count=some&tz=Mars/Base&protocol=256&dscp=64&port=65536'))
assert.deepEqual(d.filter, {})
assert.deepEqual(Object.keys(d.errors).sort(), ['count', 'direction', 'dow', 'dscp', 'fw_event', 'hour', 'ip_version', 'port', 'protocol', 'tcpflag', 'tz'])
assert.match(d.errors.dscp, /0 to 63/)
assert.match(d.errors.ip_version, /4 or 6/)
f = F.parseFlowFilter(sp('tcpflag=no_flags&dow=7&ip_version=6&direction=egress&fw_event=denied&count=edge&tz=Asia/Riyadh&dst_ports=22,%203389,22'))
assert.deepEqual(f, { dst_ports: [22, 3389], tcpflag: 'no_flags', dow: 7, tz: 'Asia/Riyadh', ip_version: 6, direction: 'egress', fw_event: 'denied', count: 'edge' })
assert.equal(F.flowFilterToApiParams(f).dst_ports, '22,3389')
assert.ok(F.parseFlowFilterDetailed(sp(`dst_ports=${Array.from({ length: 33 }, (_, i) => i + 1).join(',')}`)).errors.dst_ports)
assert.ok(F.parseFlowFilterDetailed(sp('dst_ports=443,abc')).errors.dst_ports)
for (const bad of ['1e6', '443,80', ' 1.5', '+1']) assert.ok(F.parseFlowFilterDetailed(sp(`min_bytes=${encodeURIComponent(bad)}`)).errors.min_bytes, bad)
// Empty values are absent, not errors.
assert.deepEqual(F.parseFlowFilterDetailed(sp('talker=&dscp=%20')), { filter: {}, errors: {}, invalid: {} })

// Cross-field rules.
d = F.parseFlowFilterDetailed(sp('iface_dir=out&tcpflag=rst&protocol=17&min_bytes=10&max_bytes=5'))
assert.deepEqual(Object.keys(d.errors).sort(), ['iface_dir', 'max_bytes', 'tcpflag'])
assert.deepEqual(F.validateFlowFilter({ protocol: 300, dscp: 1.5, talker: 'nope' }), {
  talker: 'Host must be an IP address or CIDR prefix, e.g. 10.0.0.5 or 10.0.0.0/8',
  protocol: 'Protocol must be a whole number from 0 to 255',
  dscp: 'DSCP must be a whole number',
})
assert.deepEqual(F.validateFlowFilter({ protocol: 6, tcpflag: 'syn_only', iface: '3@192.0.2.1', iface_dir: 'in' }), {})

// Every key survives write → parse unchanged.
const everything = {
  exporter: '192.0.2.1', iface: '70000@192.0.2.1', iface_dir: 'out', talker: '10.0.0.0/24', src: '10.0.0.5', dst: '2001:db8::7',
  not_talker: '10.9.9.9', not_src: '10.9.0.0/16', not_dst: '8.8.8.8', protocol: 6, port: 0, src_port: 1234, dst_port: 443,
  dst_ports: [22, 3389], service_port: 443, app: 'Web (HTTP/HTTPS)', dscp: 0, netclass: 'Private 10/8', tcpflag: 'syn_only',
  hour: 0, dow: 1, tz: 'UTC', group: 'Branch A', asn: 4200000000, ip_version: 4, direction: 'ingress', min_bytes: 0,
  max_bytes: 1048576, fw_event: 'teardown', count: 'all',
}
assert.deepEqual(Object.keys(everything).sort(), [...F.FLOW_FILTER_KEYS].sort(), 'fixture covers every canonical key')
assert.deepEqual(F.parseFlowFilter(F.writeFlowFilter(sp('range=7d'), everything)), everything)
assert.deepEqual(F.flowFilterFromDraft(F.flowFilterToDraft(everything)).filter, everything)
assert.deepEqual(F.validateFlowFilter(everything), {})
assert.ok(F.flowFiltersEqual(everything, { ...everything }))
assert.ok(!F.flowFiltersEqual(everything, { ...everything, dscp: 1 }))

// Merge / remove keep the interface and its direction together.
assert.deepEqual(F.removeFlowFilterKeys({ iface: '3', iface_dir: 'in', dscp: 0 }, ['iface']), { dscp: 0 })
assert.deepEqual(F.mergeFlowFilter({ talker: '10.0.0.5', iface: '3', iface_dir: 'in' }, { talker: undefined, iface: null, dscp: 0 }), { dscp: 0 })
assert.deepEqual(F.unappliedFilterKeys({ talker: '10.0.0.5', dscp: 0, tz: 'UTC', count: 'all' }, { talker: '10.0.0.5' }), ['dscp'])

// Invalid link values survive unrelated filter changes, and go when removed or replaced.
d = F.parseFlowFilterDetailed(sp('range=24h&port=abc&talker=10.0.0.5'))
assert.equal(F.writeFlowFilterKeepingInvalid(sp('range=24h&port=abc&talker=10.0.0.5'), { ...d.filter, dscp: 0 }, d.invalid).toString(), 'range=24h&talker=10.0.0.5&dscp=0&port=abc')
assert.equal(F.writeFlowFilterKeepingInvalid(sp('port=abc&talker=10.0.0.5'), d.filter, d.invalid, ['port']).toString(), 'talker=10.0.0.5')
assert.equal(F.writeFlowFilterKeepingInvalid(sp('port=abc'), { port: 443 }, d.invalid).toString(), 'port=443')

// Chips state what is matched.
const chips = F.describeFlowFilter({ talker: '::ffff:10.0.0.5', iface: '3@192.0.2.1', iface_dir: 'in', hour: 22, dow: 7, protocol: 89, dscp: 0, count: 'all', not_src: '10.9.9.9' }, { tz: 'Asia/Riyadh' })
const chip = (k) => chips.find((c) => c.key === k)
assert.equal(chip('talker').value, '10.0.0.5')
assert.equal(chip('talker').hint, 'source or destination')
assert.deepEqual(chip('iface').keys, ['iface', 'iface_dir'])
assert.equal(chip('iface').value, 'ifIndex 3 on 192.0.2.1')
assert.equal(chip('iface').hint, 'inbound (input) only')
assert.equal(chip('hour').value, '22:00–22:59 (Asia/Riyadh)')
assert.equal(chip('dow').value, 'Sunday (Asia/Riyadh)')
assert.equal(chip('protocol').value, 'OSPF (89)')
assert.equal(chip('dscp').value, 'CS0 / Best effort (0)')
assert.equal(chip('not_src').negated, true)
assert.equal(chip('count'), undefined, 'the default counting mode is not a filter chip')
assert.equal(F.describeFlowFilter({ hour: 3, tz: 'UTC' }, { tz: 'Asia/Riyadh' })[0].value, '03:00–03:59 (UTC)', "a filter's own tz wins")

/* ─── Saved queries ────────────────────────────────────────────────── */
// A v1 (flat, string-valued) forensics blob upgrades: proto → protocol, "0" stays DSCP 0, '' is absent.
let mig = F.migrateSavedQuery({ src: '10.0.0.5', dst: '', proto: '17', src_port: '', dst_port: '443', min_bytes: '', dscp: '0', exporter: '', sort: 'bytes', order: 'asc' })
assert.deepEqual(mig.query, { v: 2, kind: 'forensics', filter: { src: '10.0.0.5', protocol: 17, dst_port: 443, dscp: 0 }, window: { mode: 'relative', range: '1h' }, sort: 'bytes', order: 'asc' })
assert.equal(mig.legacyTime, true)
assert.deepEqual(mig.dropped, [])
mig = F.migrateSavedQuery({ dscp: 0, dst_port: 0 })
assert.deepEqual(mig.query.filter, { dst_port: 0, dscp: 0 }, 'numeric zeros from the raw API are kept')
mig = F.migrateSavedQuery({ dscp: 'EF', colour: 'red', src: '10.0.0.1', sort: 'DROP TABLE' })
assert.deepEqual(mig.query.filter, { src: '10.0.0.1' })
assert.deepEqual(mig.dropped.map((x) => x.key).sort(), ['colour', 'dscp', 'sort'])
const v2 = F.buildSavedQuery({ kind: 'forensics', filter: { dst_port: 443 }, window: { mode: 'relative', range: '7d' }, sort: 'bytes', order: 'asc', columns: ['timestamp', 'bytes'] })
mig = F.migrateSavedQuery(JSON.parse(JSON.stringify(v2)))
assert.deepEqual(mig.query, v2)
assert.equal(mig.legacyTime, false)
const fixed = F.buildSavedQuery({ kind: 'overview', filter: { dscp: 0 }, window: { mode: 'fixed', from: '2026-09-01T00:00:00Z', to: '2026-09-02T00:00:00Z' } })
assert.deepEqual(F.migrateSavedQuery(fixed).query.window, { mode: 'fixed', from: '2026-09-01T00:00:00.000Z', to: '2026-09-02T00:00:00.000Z' })
assert.equal(F.migrateSavedQuery({ v: 2, kind: 'forensics', filter: {}, window: { mode: 'fixed', from: '2026-09-02T00:00:00Z', to: '2026-09-01T00:00:00Z' } }).legacyTime, true)
// The run link reproduces the query exactly, including port-only and sort.
let link = new URL(L.savedQueryLink(v2), 'http://x')
assert.equal(link.pathname, '/netflow/forensics')
assert.deepEqual(Object.fromEntries(link.searchParams), { range: '7d', dst_port: '443', sort: 'bytes', order: 'asc', columns: 'timestamp,bytes', run: '1' })
link = new URL(L.savedQueryLink(fixed), 'http://x')
assert.deepEqual(Object.fromEntries(link.searchParams), { range: 'custom', from: '2026-09-01T00:00:00Z', to: '2026-09-02T00:00:00Z', dscp: '0' })
// Dialog fields: whitespace-only names are rejected.
assert.ok(F.validateSaveViewFields({ name: '   ' }).name)
assert.ok(F.validateSaveViewFields({ name: 'x'.repeat(121) }).name)
assert.ok(F.validateSaveViewFields({ name: 'ok', description: 'd'.repeat(501) }).description)
assert.deepEqual(F.validateSaveViewFields({ name: '  Branch VPN  ', description: '' }), {})

/* ─── Windows ──────────────────────────────────────────────────────── */
const range = (key, fromISO, toISO, hours, label = key) => ({ key, fromISO, toISO, hours, isCustom: key === 'custom', label })
// A custom 45-day range sends explicit bounds and never `hours` (X-20: hours > 720 was a 422).
let w = W.nfWindowFromRange(range('custom', '2026-08-01T00:00:00.000Z', '2026-09-15T00:00:00.000Z', 1080), 'Asia/Riyadh')
assert.equal(w.mode, 'fixed')
assert.deepEqual(W.windowParams(w), { from: '2026-08-01T00:00:00.000Z', to: '2026-09-15T00:00:00.000Z', tz: 'Asia/Riyadh' })
assert.equal(W.pollIntervalFor(w), false, 'fixed windows never poll')
// Calendar periods are sent as the period they name.
w = W.nfWindowFromRange(range('last_month', '2026-08-01T00:00:00.000Z', '2026-09-01T00:00:00.000Z', 744, 'Last month'), 'UTC')
assert.deepEqual(W.windowParams(w), { from: '2026-08-01T00:00:00.000Z', to: '2026-09-01T00:00:00.000Z', tz: 'UTC' })
assert.equal(W.pollIntervalFor(w), false)
w = W.nfWindowFromRange(range('month', '2026-09-01T00:00:00.000Z', '2026-09-30T14:03:00.000Z', 710, 'This month'), 'UTC')
assert.equal(w.to, '2026-09-30T14:00:00.000Z', 'month-to-date end snaps to 5 minutes')
assert.equal(w.mode, 'fixed')
// Rolling presets stay relative and poll by span.
w = W.nfWindowFromRange(range('24h', '2026-09-29T14:03:00.000Z', '2026-09-30T14:03:00.000Z', 24, 'Last 24h'), 'UTC')
assert.deepEqual(W.windowParams(w), { hours: '24', tz: 'UTC' })
assert.equal(W.pollIntervalFor(w), 60_000)
assert.equal(W.pollIntervalFor({ ...w, hours: 1 }), 15_000)
assert.equal(W.pollIntervalFor({ ...w, hours: 168 }), 300_000)
assert.deepEqual(W.windowSearchParams(w), { range: '24h' })
// Freezing pins the bounds of the data on screen, or the appliance "now".
let frozen = W.freezeWindow(w, null, Date.parse('2026-09-30T14:05:07.900Z'))
assert.deepEqual([frozen.key, frozen.mode, frozen.from, frozen.to], ['custom', 'fixed', '2026-09-29T14:05:07.000Z', '2026-09-30T14:05:07.000Z'])
frozen = W.freezeWindow(w, { from: '2026-09-29T14:00:00Z', to: '2026-09-30T14:00:00Z' })
assert.deepEqual(W.windowParams(frozen), { from: '2026-09-29T14:00:00.000Z', to: '2026-09-30T14:00:00.000Z', tz: 'UTC' })
assert.deepEqual(W.savedQueryWindow(w, 'relative'), { mode: 'relative', range: '24h' })
assert.equal(W.savedQueryWindow(frozen, 'relative').mode, 'fixed', 'a custom window cannot be saved as relative')
// Labels carry the timezone.
assert.equal(W.windowLabel({ from: '2026-09-30T14:00:00Z', to: '2026-09-30T15:00:00Z', mode: 'relative', tz: 'Asia/Riyadh' }), '2026-09-30 17:00 → 18:00 (Asia/Riyadh)')
assert.equal(W.windowLabel({ from: '2026-09-29T22:00:00Z', to: '2026-09-30T02:00:30Z', mode: 'fixed', tz: 'UTC' }), '2026-09-29 22:00:00 → 2026-09-30 02:00:30 (UTC)')
assert.equal(W.windowLabel(w), 'Last 24h · 2026-09-29 14:03 → 2026-09-30 14:03 (UTC)')
const shifted = { from: '2026-09-27T10:30:00Z', to: '2026-09-30T10:30:00Z', mode: 'fixed', tz: 'UTC', effective_from: '2026-09-27T10:00:00Z', effective_to: '2026-09-30T10:30:00Z' }
assert.ok(W.hasEffectiveShift(shifted))
assert.equal(W.windowLabel(shifted, { effective: true }), '2026-09-27 10:00 → 2026-09-30 10:30 (UTC)')
assert.ok(fmt.isValidTimeZone(W.displayTimeZone()))

/* ─── Links ────────────────────────────────────────────────────────── */
link = new URL(L.buildNetflowLink('/netflow/forensics', { filter: { talker: '10.0.0.5', iface: '3', dscp: 0 }, window: w, extra: { run: true, finding: '', page: 0 } }), 'http://x')
assert.deepEqual(Object.fromEntries(link.searchParams), { range: '24h', iface: '3', talker: '10.0.0.5', dscp: '0', run: '1', page: '0' })
link = new URL(L.buildNetflowLink('/netflow?compare=1&proto=6&from=x', { filter: { protocol: 17 }, window: frozen }), 'http://x')
assert.deepEqual(Object.fromEntries(link.searchParams), { compare: '1', range: 'custom', from: '2026-09-29T14:00:00.000Z', to: '2026-09-30T14:00:00.000Z', protocol: '17' })
assert.equal(L.buildNetflowLink('/netflow/anomalies'), '/netflow/anomalies')
assert.equal(L.exporterPath('2001:db8::1'), '/netflow/devices/2001%3Adb8%3A%3A1')

/* ─── Formatting ───────────────────────────────────────────────────── */
// Decimal SI units (the review found binary scaling under GB labels).
assert.equal(fmt.formatBytes(1000), '1.00 kB')
assert.equal(fmt.formatBytes(1024), '1.02 kB')
assert.equal(fmt.formatBytes(19_057_630_938), '19.1 GB')
assert.equal(fmt.formatBytes(1_510_000_000), '1.51 GB')
assert.equal(fmt.formatBytes(613_000_000_000), '613 GB')
assert.equal(fmt.formatBytes(999_999), '1.00 MB', 'rounding carries into the next unit')
assert.equal(fmt.formatBytes(999), '999 B')
assert.equal(fmt.formatBytes(0), '0 B')
assert.equal(fmt.formatBytes(0.5), '<1 B')
assert.equal(fmt.formatBytes(null), '—', 'unknown is not zero')
assert.equal(fmt.formatBytes(Number.NaN), '—')
assert.equal(fmt.formatBytesExact(19_057_630_938), '19,057,630,938 bytes')
assert.equal(fmt.formatBps(0.5), '<1 bps', 'sub-1 rates clamp instead of "500.00 undefined"')
assert.equal(fmt.formatBps(0.25), '<1 bps')
assert.equal(fmt.formatBps(0), '0 bps')
assert.equal(fmt.formatBps(39_090_000), '39.1 Mbps')
assert.equal(fmt.formatBps(1_200_000_000), '1.20 Gbps')
assert.equal(fmt.formatBps(undefined), '—')
assert.equal(fmt.formatBpsAxis(0.5), '0.5')
assert.equal(fmt.formatBpsAxis(1500), '1.5k')
assert.equal(fmt.formatBpsAxis(12_000_000), '12M')
assert.equal(fmt.formatBpsAxis(0), '0')
assert.equal(fmt.formatCount(999_999), '999,999')
assert.equal(fmt.formatCount(1_514_234), '1.5M')
assert.equal(fmt.formatCount(9_876_543_210), '9.9B')
assert.equal(fmt.formatCountExact(1_514_234), '1,514,234')
assert.equal(fmt.formatCount(null), '—')
assert.equal(fmt.formatPercent(0.05), '<0.1%')
assert.equal(fmt.formatPercent(50), '50%')
assert.equal(fmt.formatPercent(12.34), '12.3%')
assert.equal(fmt.formatPercent(0), '0%')
// NF-18 acceptance: 500 ms, 1500 ms and 60 000 ms read 500 ms, 1.5 s and 60 s.
assert.equal(fmt.formatDuration(500), '500 ms')
assert.equal(fmt.formatDuration(1500), '1.5 s')
assert.equal(fmt.formatDuration(60_000), '60 s')
assert.equal(fmt.formatDuration(90_000), '1.5 min')
assert.equal(fmt.formatDuration(5_400_000), '1.5 h')
assert.equal(fmt.formatDuration(3 * 86_400_000), '3 d')
assert.equal(fmt.formatDuration(999.7), '1 s')
assert.equal(fmt.formatDuration(0), '0 ms')
assert.equal(fmt.formatDuration(-5), '—')
assert.equal(fmt.formatDuration(null), '—')
assert.equal(fmt.formatInterval(3600), '1 h')
assert.equal(fmt.formatInterval(300), '5 min')
assert.equal(fmt.formatInterval(86_400), '1 d')
assert.equal(fmt.formatInterval(90), '90 s')
assert.equal(fmt.formatInterval(0), '—')
assert.equal(fmt.ipDisplay('::ffff:10.0.0.1'), '10.0.0.1')
assert.equal(fmt.ipDisplay('::FFFF:a00:1'), '10.0.0.1')
assert.equal(fmt.ipDisplay('::ffff:10.0.0.0/104'), '10.0.0.0/8')
assert.equal(fmt.ipDisplay('2001:db8::1'), '2001:db8::1')
assert.equal(fmt.ipDisplay('::'), '—')
assert.equal(fmt.ipDisplay(null), '—')
assert.equal(fmt.formatDateTime('2026-09-30T14:03:22Z', 'Asia/Riyadh'), '2026-09-30 17:03:22 UTC+03:00')
assert.equal(fmt.formatDateTime('2026-09-30T14:03:22Z', 'UTC'), '2026-09-30 14:03:22 UTC')
assert.equal(fmt.formatDateTime('2026-09-30T14:03:22Z', 'Asia/Kolkata', { seconds: false }), '2026-09-30 19:33 UTC+05:30')
assert.equal(fmt.formatDateTime('2026-09-30T14:03:22Z', 'Not/AZone', { zone: 'name' }), '2026-09-30 14:03:22 UTC')
assert.equal(fmt.formatDateTime(null, 'UTC'), '—')
assert.equal(fmt.tzOffsetLabel('America/New_York', '2026-01-15T12:00:00Z'), 'UTC−05:00')
assert.equal(fmt.protocolLabel(89), 'OSPF (89)')
assert.equal(fmt.protocolLabel(253), 'Protocol 253')
assert.equal(fmt.dscpLabel(46), 'EF (46)')
assert.equal(fmt.dowName(1), 'Monday')
assert.equal(fmt.dowName(8), '—')

/* ─── Panel states and errors ──────────────────────────────────────── */
const err = (status, data) => ({ response: { status, data } })
const matrix = [
  [{ status: 'pending', data: undefined, error: null }, {}, 'loading'],
  [{ status: 'error', data: [1], error: err(403, { detail: 'Missing permission: netflow.view' }) }, {}, 'forbidden'],
  [{ status: 'error', data: undefined, error: err(500, { detail: 'Query failed (ref nf-1a2b3c4d)' }) }, {}, 'error'],
  [{ status: 'error', data: [1], error: err(500, {}) }, {}, 'stale'],
  [{ status: 'success', data: [1], error: null, isPlaceholderData: true }, {}, 'loading'],
  [{ status: 'success', data: [], error: null }, { coverage: 'none' }, 'no_coverage'],
  [{ status: 'success', data: [1], error: null }, { unsupported: 'TCP flags are not exported' }, 'unsupported'],
  [{ status: 'success', data: [], error: null }, {}, 'empty'],
  [{ status: 'success', data: { data: { items: [] }, meta: {} }, error: null }, {}, 'empty'],
  [{ status: 'success', data: { flows: 0 }, error: null }, {}, 'ok'],
  [{ status: 'success', data: { flows: 0 }, error: null }, { isEmpty: (x) => x.flows === 0 }, 'empty'],
  [{ status: 'success', data: [1], error: null }, { coverage: 'covered' }, 'ok'],
]
for (const [q, opts, want] of matrix) assert.equal(S.panelStatus(q, opts), want, JSON.stringify([q, want]))
assert.equal(S.placeholderStatus({ status: 'success', data: [1], error: null, isPlaceholderData: true }), 'ok')
assert.equal(S.placeholderStatus({ status: 'success', data: [], error: null, isPlaceholderData: true }), 'empty')
assert.equal(S.coverageState({ totals: { records: 0 } }), 'none')
assert.equal(S.coverageState({ totals: { records: 12 } }), 'covered')
assert.equal(S.coverageState(null), 'unknown')
assert.deepEqual(S.tcpFlagSupport({ totals: { tcp_records: 100, tcp_flagged_records: 0 } }), { support: 'unsupported', pct: 0 })
assert.deepEqual(S.tcpFlagSupport({ totals: { tcp_records: 100, flag_coverage_pct: 60 } }), { support: 'partial', pct: 60 })
assert.equal(S.tcpFlagSupport({ totals: { tcp_records: 0 } }).support, 'unknown')

// Error messages are always strings: FastAPI 422 arrays never reach React as objects (X-21).
const e422 = err(422, { detail: [{ type: 'int_parsing', loc: ['query', 'src_port'], msg: 'Input should be a valid integer', input: 'a' }, { loc: ['query', 'dscp'], msg: 'Input should be less than or equal to 63' }] })
assert.equal(S.apiErrorMessage(e422), 'src_port: Input should be a valid integer; dscp: Input should be less than or equal to 63')
assert.equal(S.apiErrorMessage(err(500, { detail: 'The flow query failed (ref nf-1a2b3c4d)', error_id: 'nf-1a2b3c4d', code: 'query_failed' })), 'The flow query failed')
assert.equal(S.apiErrorRef(err(500, { detail: 'The flow query failed (ref nf-1a2b3c4d)' })), 'nf-1a2b3c4d')
assert.equal(S.apiErrorRef(err(504, { detail: 'x', error_id: 'nf-00ff00ff' })), 'nf-00ff00ff')
assert.equal(S.apiErrorCode(err(504, { code: 'query_timeout' })), 'query_timeout')
assert.equal(S.apiErrorMessage(err(400, { detail: { msg: 'Bad window' } })), 'Bad window')
assert.equal(typeof S.apiErrorMessage(err(400, { detail: { weird: { nested: true } } })), 'string')
assert.equal(S.apiErrorMessage(err(404, { detail: 'Not Found' })), 'Not found: this data does not exist or is outside your access')
assert.equal(S.apiErrorMessage({ code: 'ERR_NETWORK', message: 'Network Error' }), 'The appliance could not be reached')
assert.equal(S.apiErrorMessage({ code: 'ECONNABORTED', message: 'timeout of 35000ms exceeded' }), 'The query took too long and was stopped')
assert.equal(S.apiErrorMessage(err(502, '<html>bad gateway</html>')), 'The request failed (HTTP 502)')
assert.equal(S.isRetryableError(err(400, {})), false)
assert.equal(S.isRetryableError(err(403, {})), false)
assert.equal(S.isRetryableError(err(504, {})), false)
assert.equal(S.isRetryableError(err(500, {})), true)
assert.equal(S.isRetryableError({ code: 'ERR_NETWORK' }), true)
assert.equal(A.nfRetry(0, err(500, {})), true)
assert.equal(A.nfRetry(1, err(500, {})), false)
assert.equal(A.nfRetry(0, err(422, {})), false)

/* ─── API envelope and keys ────────────────────────────────────────── */
assert.equal(A.toSearchParams({ a: 0, b: '', c: null, d: [22, 3389], e: true, f: false, g: undefined, h: Number.NaN }).toString(), 'a=0&d=22%2C3389&e=1')
assert.deepEqual(A.nfKeys.query('/netflow/overview', { b: 1, a: 2 }), A.nfKeys.query('/netflow/overview', { a: 2, b: 1 }))
assert.deepEqual(A.nfParams(w, { tz: 'Asia/Riyadh', dscp: 0 }, { limit: 10 }), { hours: '24', tz: 'Asia/Riyadh', dscp: '0', limit: 10 })
assert.deepEqual(A.unwrapEnvelope([{ ip: '10.0.0.1' }]), { data: [{ ip: '10.0.0.1' }], meta: null }, 'legacy bodies have no meta')
const env = A.unwrapEnvelope({
  data: { flows: 3 },
  meta: {
    window: { from: '2026-09-30T14:00:00Z', to: '2026-09-30T15:00:00Z', mode: 'relative', tz: 'UTC', source: 'hybrid', granularity_s: 3600 },
    server_time: '2026-09-30T15:00:01Z', query_ms: 42, restricted: true, filters: { talker: '10.0.0.5' },
    retention: { raw_days: 30, rollup_days: 'x', interface_days: 400, limited: true },
    truncated: { limit: 50, returned: 50, more: true }, sampling: { sampled_share: 0.25, max_interval: 100 },
    warnings: [{ code: 'flags_unavailable', message: 'TCP flags not exported' }, { code: 'empty' }, 'junk'],
  },
})
assert.deepEqual(env.data, { flows: 3 })
assert.equal(env.meta.window.source, 'hybrid')
assert.equal(env.meta.retention.rollup_days, null, 'a malformed field is unknown, not a default')
assert.deepEqual(env.meta.warnings, [{ code: 'flags_unavailable', message: 'TCP flags not exported' }])
assert.equal(env.meta.restricted, true)
// nf.get always asks for the envelope and passes typed parameters as the API expects.
let captured
m.api.get = async (url, config) => {
  captured = { url, params: config.params.toString(), timeout: config.timeout }
  return { data: { data: [], meta: { server_time: '2026-09-30T15:00:01Z' } } }
}
const res = await A.nf.get('/netflow/top-talkers', { ...A.nfParams(w, { dst_ports: [22, 3389], dscp: 0 }), limit: 10 })
assert.equal(captured.url, '/netflow/top-talkers')
assert.equal(captured.params, 'hours=24&tz=UTC&dst_ports=22%2C3389&dscp=0&limit=10&envelope=1')
assert.equal(captured.timeout, 35_000)
assert.deepEqual(res.data, [])
assert.equal(res.meta.server_time, '2026-09-30T15:00:01Z')

/* ─── Navigation and routes ────────────────────────────────────────── */
const nfNode = nav.NAV_GROUPS.flatMap((g) => g.items).find((n) => n.to === '/netflow')
assert.equal(nfNode.permission, 'netflow.view', 'the NetFlow menu is hidden without netflow.view (X-01)')
assert.deepEqual(nfNode.children.map((c) => c.label), ['Overview', 'Forensics', 'Anomalies', 'Capacity', 'Saved Views', 'Collector health', 'Settings'])
const active = (label, pathname) => nav.isNodeActive(nfNode.children.find((c) => c.label === label), pathname, new URLSearchParams())
assert.ok(active('Overview', '/netflow'))
assert.ok(active('Overview', '/netflow/top-talkers'))
assert.ok(active('Overview', '/netflow/devices/192.0.2.1'))
assert.ok(active('Overview', '/netflow/hosts/10.0.0.5'))
assert.ok(!active('Overview', '/netflow/forensics'))
assert.ok(!active('Overview', '/netflow/settings'))
assert.ok(active('Collector health', '/netflow/setup'))
assert.ok(active('Settings', '/netflow/settings'))
const app = fs.readFileSync(path.join(src, 'App.tsx'), 'utf8')
for (const route of ['netflow/health', 'netflow/setup', 'netflow/settings', 'netflow/hosts/:ip']) {
  assert.ok(app.includes(`path="${route}"`), `route ${route}`)
  assert.ok(app.indexOf(`path="${route}"`) < app.indexOf('path="netflow/:section"'), `${route} is declared before the section catch-all`)
}

/* ─── Rendered states ──────────────────────────────────────────────── */
const h = m.createElement
const qc = new m.QueryClient({ defaultOptions: { queries: { enabled: false } } })
const render = (el) => m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, null, el)))
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
const panel = (query, opts = {}) => text(render(h(m.QueryPanel, { query, label: 'top talkers', ...opts, children: (d) => h('div', null, `DATA:${JSON.stringify(d)}`) })))

let out = panel({ status: 'error', data: undefined, error: e422, refetch: () => {} })
assert.match(out, /Could not load top talkers/)
assert.match(out, /src_port: Input should be a valid integer/)
assert.match(out, /Retry/)
assert.doesNotMatch(out, /DATA:/)
out = panel({ status: 'error', data: undefined, error: err(500, { detail: 'The flow query failed (ref nf-1a2b3c4d)' }), refetch: () => {} })
assert.match(out, /Reference nf-1a2b3c4d/)
out = panel({ status: 'error', data: [7], error: err(500, {}), dataUpdatedAt: Date.now() - 180_000, refetch: () => {} })
assert.match(out, /Refresh failed ?, showing data from \d\d:\d\d:\d\d/)
assert.match(out, /DATA:\[7\]/, 'a failed refresh keeps the last good data')
out = panel({ status: 'error', data: [7], error: err(403, {}) })
assert.match(out, /You do not have access to top talkers/)
assert.doesNotMatch(out, /DATA:/, 'forbidden hides retained data')
out = panel({ status: 'pending', data: undefined, error: null })
assert.match(out, /Loading top talkers/)
assert.doesNotMatch(out, /No flows|0 B|DATA:/, 'loading never looks like an empty or zero result')
out = panel({ status: 'success', data: [1], error: null, isPlaceholderData: true })
assert.match(out, /Loading the new selection/)
assert.match(out, /DATA:\[1\]/)
out = panel({ status: 'success', data: [], error: null })
assert.match(out, /No flows matched this window and filters/)
out = panel({ status: 'success', data: [1], error: null }, { coverage: 'none' })
assert.match(out, /No flow records were received for this window/)
assert.match(out, /unknown here, not zero/)
out = panel({ status: 'success', data: [1], error: null }, { unsupported: 'The exporters do not export TCP flags.' })
assert.match(out, /Top talkers is not available/)
assert.match(out, /do not export TCP flags/)
out = text(render(h(m.StatusTile, { tone: 'danger', label: 'Critical interfaces', value: null })))
assert.match(out, /Critical interfaces Critical —/, 'unknown values render as a dash with the visible status word')
out = text(render(h(m.StatusTile, { tone: 'success', label: 'Healthy', value: 12, word: 'OK' })))
assert.match(out, /OK 12/)
out = text(render(h(m.FilterChips, { filter: { talker: '10.0.0.5', dscp: 0 }, invalid: { port: 'abc' }, errors: { port: 'Port must be…' }, onRemove: () => {} })))
assert.match(out, /Host 10\.0\.0\.5/)
assert.match(out, /DSCP CS0 \/ Best effort \(0\)/)
assert.match(out, /Port “abc” not applied/)
out = text(render(h(m.DataQualityBar, { meta: env.meta, coverage: { exporters: [], totals: { records: 100, tcp_records: 50, tcp_flagged_records: 0, minutes_with_data: 40, minutes_in_window: 60 } }, filter: { talker: '10.0.0.5', dscp: 0 } })))
for (const want of [/hourly rollup with raw edges/, /1 h buckets/, /older than the data source keeps/, /25% of records sampled \(up to 1:100\)/, /Records in 40 of 60 minutes/, /TCP flags are not exported/, /Limited to the exporters/, /Showing the first 50/, /Not applied to this panel: DSCP/, /TCP flags not exported/]) {
  assert.match(out, want)
}

/* ─── Status token contrast (NF-29) ────────────────────────────────── */
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
const tailwind = fs.readFileSync(path.join(root, 'tailwind.config.js'), 'utf8')
const report = []
for (const [theme, t] of Object.entries(themes)) {
  for (const tone of ['danger', 'warning', 'success', 'info']) {
    const fg = t[`${tone}-fg`]
    assert.ok(fg, `--${tone}-fg defined in ${theme}`)
    assert.ok(tailwind.includes(`'${tone}-fg': 'rgb(var(--${tone}-fg) / <alpha-value>)'`), `tailwind colour ${tone}-fg`)
    let worst = Infinity
    // Tinted fills /5 … /15 over the page, card and secondary surfaces, plus the plain surfaces.
    for (const base of ['surface', 'bg', 'surface2']) {
      for (const alpha of [0, 0.05, 0.1, 0.15]) {
        const fill = blend(t[tone], t[base], alpha)
        worst = Math.min(worst, ratio(fg, fill))
        // Tile label, value and caption text sit on the same fills.
        for (const txt of ['text', 'text2']) assert.ok(ratio(t[txt], fill) >= 4.5, `${theme} ${txt} on ${tone}/${alpha} over ${base}`)
      }
    }
    assert.ok(worst >= 4.5, `${theme} ${tone}-fg worst ratio ${worst.toFixed(2)} < 4.5`)
    // The fill colour itself fails as small text in light mode, which is why *-fg exists.
    report.push(`${theme} ${tone}-fg ≥ ${worst.toFixed(2)}:1 (base ${ratio(t[tone], t.surface).toFixed(2)}:1)`)
  }
}

console.log(`NetFlow foundation tests passed: filters (every key round-trips, zeros, aliases, CIDR/IPv6, iface@exporter, cross-field), saved queries v1→v2, windows (custom/calendar never send hours), links, formatting (decimal SI, sub-1 clamps, durations, timezones), panel-state matrix, error strings, envelope, navigation, rendered states.
Contrast: ${report.join('; ')}`)
