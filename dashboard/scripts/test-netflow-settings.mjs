// NetFlow settings + host profile (WP B7) pure logic: settings tab parsing, the CIDR editor
// (IPv4 + IPv6, zone ids, separators), share-of-total without fabricated zeros, colour and
// error helpers, application-rule form validation and payloads (FE-09 / CR-12), rule
// descriptions, the IP-group tree order and search (CR-13), GeoIP tones, and the host profile's
// chart rates (edge buckets, uncovered gaps), filter links and section messages (CR-14).
// Run from dashboard/: node scripts/test-netflow-settings.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')
const entry = `
export * as S from '@/pages/netflow/SettingsPage'
export * as H from '@/pages/netflow/HostProfilePage'
export { buildNetflowLink } from '@/lib/netflow/links'
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
const { S, H, buildNetflowLink } = m
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }

/* ─── Tabs ─────────────────────────────────────────────────────────── */
eq(S.normalizeSettingsTab('applications'), 'applications')
eq(S.normalizeSettingsTab('enrichment'), 'enrichment')
eq(S.normalizeSettingsTab(null), 'groups', 'no tab -> IP groups')
eq(S.normalizeSettingsTab('detection'), 'groups', 'unknown tab -> IP groups (detection lives on the Anomalies page)')

/* ─── CIDR editor (CR-13) ──────────────────────────────────────────── */
{
  const r = S.parseCidrLines('10.0.0.0/8\n 192.168.1.10 , 2001:db8::/32;fe80::1%eth0\n\nnope  10.1.2.3/40')
  eq(r.valid, ['10.0.0.0/8', '192.168.1.10', '2001:db8::/32'])
  eq(r.invalid, 3)
  eq(r.lines.find((l) => l.raw === 'fe80::1%eth0').error, 'Zone ids are not allowed')
  eq(r.lines.find((l) => l.raw === 'nope').error, 'Not an IP address or CIDR')
  ok(!r.lines.find((l) => l.raw === '10.1.2.3/40').ok, 'IPv4 prefix above /32 is invalid')
  eq(S.parseCidrLines('   \n ').lines, [], 'blank input has no lines')
  eq(S.parseCidrLines('2001:db8:40::/48').valid, ['2001:db8:40::/48'], 'IPv6 /48')
}

/* ─── Shares never invent a denominator ───────────────────────────── */
eq(S.shareOf(25, 100), 25)
eq(S.shareOf(0, 100), 0, 'measured zero stays zero')
eq(S.shareOf(10, 0), null, 'zero total -> unknown, not 0 %')
eq(S.shareOf(null, 100), null)
eq(S.shareOf(10, undefined), null)
eq(S.shareOf(Number.NaN, 100), null)

/* ─── Colours and API field errors ────────────────────────────────── */
ok(S.isHexColor('#1BAF7a'))
ok(!S.isHexColor('#fff') && !S.isHexColor('blue') && !S.isHexColor('#12345g'))
ok(S.GROUP_COLORS.every((c) => S.isHexColor(c.hex)), 'palette is valid hex')
eq(S.responseFieldErrors({ response: { data: { detail: 'x', code: 'invalid_value', fields: { cidrs: 'bad', n: 3 } } } }), { cidrs: 'bad' })
eq(S.responseFieldErrors(new Error('network')), {})
eq(S.responseFieldErrors(null), {})
ok(S.onlyKnownFields({ cidrs: 'bad', color: 'x' }, S.GROUP_FIELDS), 'field errors shown inline: no banner')
ok(!S.onlyKnownFields({ document: 'bad' }, S.GROUP_FIELDS), 'an error with no input gets the banner')
ok(!S.onlyKnownFields({}, S.RULE_FIELDS), 'no field errors: the banner explains')
ok(S.onlyKnownFields({ port_hi: 'x' }, S.RULE_FIELDS))

/* ─── Application-rule form (CR-12) ───────────────────────────────── */
{
  const base = { ...S.EMPTY_RULE_FORM, application: '  Payroll API ', port_lo: '9443', cidrs: '10.0.10.0/24\n2001:db8::/32' }
  const { payload, errors } = S.ruleFormToPayload(base)
  eq(errors, {})
  eq(payload, { application: 'Payroll API', service: null, category: null, protocol: 6, port_lo: 9443, port_hi: null,
    cidrs: ['10.0.10.0/24', '2001:db8::/32'], priority: 100, enabled: true })
  // Priority 0 is a valid value (numeric zero is never "empty").
  eq(S.ruleFormToPayload({ ...base, priority: '0' }).payload.priority, 0)
  const err = (patch) => S.ruleFormToPayload({ ...base, ...patch }).errors
  ok(err({ application: ' ' }).application, 'application required')
  ok(err({ port_lo: '0' }).port_lo, 'port 0 refused')
  ok(err({ port_lo: '70000' }).port_lo, 'port above 65535 refused')
  ok(err({ port_lo: '80', port_hi: '79' }).port_hi, 'reversed range refused')
  eq(err({ protocol: '', port_lo: '', port_hi: '90', cidrs: '' }).port_lo, 'A range needs its first port',
    'a last port without a first keeps its own message')
  ok(/no ports/.test(err({ protocol: '1' }).protocol), 'ICMP port rule refused')
  ok(/no ports/.test(err({ protocol: '50' }).protocol), 'ESP port rule refused')
  eq(err({ protocol: '132' }), {}, 'SCTP carries ports')
  ok(err({ cidrs: '10.0.0.0/8\nbad' }).cidrs.includes('bad'), 'invalid CIDR named')
  ok(/needs a port/.test(err({ protocol: '', port_lo: '', cidrs: '' }).port_lo), 'empty rule refused')
  eq(err({ protocol: '47', port_lo: '', cidrs: '' }), {}, 'protocol-only rule')
  eq(err({ protocol: '', port_lo: '', cidrs: '192.0.2.0/24' }), {}, 'address-only rule')
  ok(err({ priority: '' }).priority && err({ priority: '-1' }).priority && err({ priority: '100001' }).priority)
  ok(err({ port_lo: '8o' }).port_lo, 'non-digits refused')
  // Round trip through a saved rule.
  const saved = { id: 'r1', application: 'Legacy ERP', service: null, category: 'Business apps', protocol: null,
    protocol_name: null, port_lo: 3200, port_hi: 3299, cidrs: [], priority: 40, enabled: false, kind: 'port',
    created_at: null, updated_at: null, created_by: null, updated_by: null }
  const form = S.ruleToForm(saved)
  eq(form.protocol, '', 'any transport')
  const back = S.ruleFormToPayload(form)
  eq(back.errors, {})
  eq(back.payload, { application: 'Legacy ERP', service: null, category: 'Business apps', protocol: null, port_lo: 3200,
    port_hi: 3299, cidrs: [], priority: 40, enabled: false })
}

/* ─── Rule descriptions ───────────────────────────────────────────── */
eq(S.describeRuleMatch({ protocol: 6, port_lo: 8080, port_hi: 8090, cidrs: ['10.0.0.0/8'] }), 'TCP 8080–8090 · 10.0.0.0/8')
eq(S.describeRuleMatch({ protocol: null, port_lo: 443, port_hi: null, cidrs: [] }), 'Any transport 443')
eq(S.describeRuleMatch({ protocol: 17, port_lo: 53, port_hi: 53, cidrs: [] }), 'UDP 53', 'single-port range')
ok(/^ESP|^IPsec ESP/.test(S.describeRuleMatch({ protocol: 50, port_lo: null, port_hi: null, cidrs: [] })), 'protocol rule')
eq(S.describeRuleMatch({ protocol: null, port_lo: null, port_hi: null, cidrs: ['a', 'b', 'c', 'd'] }), 'Addresses a, b +2')
eq(S.describeRuleMatch({ protocol: null, port_lo: null, port_hi: null, cidrs: [] }), '—')

/* ─── Stable preview keys ─────────────────────────────────────────── */
eq(S.stableKey({ b: 1, a: { d: [2, 1], c: null } }), S.stableKey({ a: { c: null, d: [2, 1] }, b: 1 }))
ok(S.stableKey({ a: [1, 2] }) !== S.stableKey({ a: [2, 1] }), 'array order matters')

/* ─── IP-group tree and search (CR-13) ────────────────────────────── */
{
  const g = (id, name, parent_id = null, extra = {}) => ({ id, name, parent_id, description: null, cidrs: [], tags: [], ...extra })
  const groups = [
    g('c2', 'HQ users', 'p'), g('x', 'Backup'), g('p', 'Internal LAN'), g('c1', 'Data center', 'p'),
    g('gc', 'Racks', 'c1'), g('o', 'Orphan', 'gone'), g('l1', 'Loop A', 'l2'), g('l2', 'Loop B', 'l1'),
  ]
  const order = S.orderGroupsAsTree(groups).map((x) => x.name)
  eq(order.slice(0, 6), ['Backup', 'Internal LAN', 'Data center', 'Racks', 'HQ users', 'Orphan'],
    'parents first, children under them by name; an unknown parent is top level')
  eq(order.length, groups.length, 'a parent loop still lists every group once')
  eq(new Set(order).size, groups.length)
  const lan = g('p', 'Internal LAN', null, { description: 'RFC 1918', cidrs: ['10.0.0.0/8', '2001:DB8::/32'], tags: ['Site'] })
  ok(S.groupMatchesSearch(lan, ''))
  ok(S.groupMatchesSearch(lan, 'internal'))
  ok(S.groupMatchesSearch(lan, 'rfc'))
  ok(S.groupMatchesSearch(lan, 'site'))
  ok(S.groupMatchesSearch(lan, '2001:db8'), 'CIDR search ignores case')
  ok(!S.groupMatchesSearch(lan, '172.16'))
}

/* ─── GeoIP status tones ──────────────────────────────────────────── */
eq(S.geoipTone({ present: false, valid: null, stale: null }), 'warning')
eq(S.geoipTone({ present: true, valid: false, stale: null }), 'danger')
eq(S.geoipTone({ present: true, valid: true, stale: true }), 'warning')
eq(S.geoipTone({ present: true, valid: true, stale: false }), 'success')

/* ─── Host profile (CR-14) ────────────────────────────────────────── */
{
  const pts = [
    { t: '2026-09-30T10:00:00.000Z', bytes_in: 900, bytes_out: 1800, packets_in: 1, packets_out: 2, flows: 3, covered: true, partial: true },
    { t: '2026-09-30T10:15:00.000Z', bytes_in: 0, bytes_out: 0, packets_in: 0, packets_out: 0, flows: 0, covered: true, partial: false },
    { t: '2026-09-30T10:30:00.000Z', bytes_in: null, bytes_out: null, packets_in: null, packets_out: null, flows: null, covered: false, partial: false },
  ]
  const c = H.toChartPoints(pts, 900, '2026-09-30T10:10:00.000Z', '2026-09-30T10:45:00.000Z')
  // The first bucket is cut: 300 s of it are inside the window, so the rate uses 300 s, not 900 s.
  eq(c[0].in_bps, (900 * 8) / 300, 'edge bucket rate over its covered part')
  eq(c[0].out_bps, (1800 * 8) / 300)
  eq(c[1].in_bps, 0, 'measured zero')
  eq(c[2].in_bps, null, 'uncovered bucket is a gap')
  eq(c[2].covered, false)
  const whole = H.toChartPoints(pts, 900)
  eq(whole[0].in_bps, (900 * 8) / 900, 'without effective bounds the whole bucket is used')
}
{
  const filter = { talker: '10.9.9.9', protocol: 6, dscp: 0, exporter: '127.0.0.61' }
  eq(H.hostContextFilter(filter), { protocol: 6, dscp: 0, exporter: '127.0.0.61' }, 'the host is the path, not a filter')
  eq(H.hostFlowFilter(filter, '10.30.0.40'), { protocol: 6, dscp: 0, exporter: '127.0.0.61', talker: '10.30.0.40' })
  const link = buildNetflowLink('/netflow/forensics', { filter: H.hostFlowFilter(filter, '2001:db8::5') })
  ok(link.startsWith('/netflow/forensics?'), link)
  const qs = new URLSearchParams(link.split('?')[1])
  eq(qs.get('talker'), '2001:db8::5', 'IPv6 host carried in the link')
  eq(qs.get('dscp'), '0', 'numeric zero survives in links (FE-02)')
  eq(qs.get('protocol'), '6')
}
eq(H.roleLabel('server'), 'Serves')
eq(H.roleLabel('client'), 'Uses')
eq(H.roleLabel('unknown'), 'Unclear side')
eq(H.sectionProblem(undefined), null)
eq(H.sectionProblem({ status: 'ok' }), null)
ok(/narrow the time range/.test(H.sectionProblem({ status: 'timeout', error_id: 'nf-1', message: 'x' })))
eq(H.sectionProblem({ status: 'failed', error_id: 'nf-1a2b3c4d', message: 'This part could not be loaded (ref nf-1a2b3c4d)' }),
  'This part could not be loaded', 'the ref id is shown separately, not twice')
eq(H.sectionProblem({ status: 'failed', error_id: null }), 'This part could not be loaded.')

console.log(`test-netflow-settings: ${checks} checks passed`)
