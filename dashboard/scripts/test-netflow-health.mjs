// NetFlow collector & exporter health (WP B6) pure logic: exporter state ordering, summary and
// filters, receipt ageing on the appliance clock (NF-01 / X-24), clock offset wording, collector
// tones and drop totals, the statistics series grid (gaps never zero), onboarding check tones,
// duplicate-observation wording (FE-12 / CR-16), retention rows (FE-11), the mapping confirmation
// text (FE-08), the exporter settings form validation and the vendor configuration snippets (CR-06).
// Run from dashboard/: node scripts/test-netflow-health.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')
const entry = `
export * as M from '@/pages/netflow/health/model'
export * as Sn from '@/pages/netflow/health/snippets'
export { validateSettingsDraft } from '@/pages/netflow/health/ExporterSettingsForm'
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
const { M, Sn, validateSettingsDraft } = m
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }

const row = (over = {}) => ({
  exporter: '10.0.0.1', display_name: null, device_id: 'd1', device_hostname: 'r1', device_status: 'up',
  last_received_at: '2026-10-01T10:00:00Z', receipt_age_s: 10, state: 'receiving', records_15m: 100,
  expected_interval_s: 60, clock_offset_ms: 20, clock_suspect: false, export_protocols: ['ipfix'],
  loss_pct_1h: 0, flags_exported: true, issues: [], ...over,
})

/* ─── States, ordering, summary, filters ───────────────────────────── */
{
  const rows = [
    row({ exporter: '10.0.0.9', state: 'receiving' }),
    row({ exporter: '10.0.0.2', state: 'never', receipt_age_s: null, last_received_at: null }),
    row({ exporter: '10.0.0.3', state: 'silent', receipt_age_s: 4000 }),
    row({ exporter: '10.0.0.10', state: 'receiving', issues: [{ code: 'sequence_loss', severity: 'warning', message: 'x' }] }),
    row({ exporter: '10.0.0.4', state: 'late', receipt_age_s: 300, device_id: null }),
  ]
  eq(M.sortExporters(rows).map((r) => r.exporter), ['10.0.0.3', '10.0.0.4', '10.0.0.2', '10.0.0.10', '10.0.0.9'],
    'silent, late, never, receiving-with-warning, receiving')
  eq(M.sortExporters([row({ exporter: '10.0.0.10' }), row({ exporter: '10.0.0.9' })]).map((r) => r.exporter),
    ['10.0.0.9', '10.0.0.10'], 'numeric-aware address order')
  eq(M.sortExporters([row({ exporter: '10.0.0.1', display_name: 'zeta' }), row({ exporter: '10.0.0.2', display_name: 'alpha' })]).map((r) => r.exporter),
    ['10.0.0.2', '10.0.0.1'], 'display names sort before addresses')
  const s = M.summarizeExporters(rows)
  eq(s, { total: 5, receiving: 2, late: 1, silent: 1, never: 1, withProblems: 3, unmapped: 1 })
  eq(M.receivingTone(s), 'danger', 'any silent exporter -> danger')
  eq(M.receivingTone({ ...s, silent: 0 }), 'warning', 'late only -> warning')
  eq(M.receivingTone({ ...s, silent: 0, late: 0 }), 'success')
  eq(M.receivingTone({ total: 0, receiving: 0, late: 0, silent: 0, never: 0, withProblems: 0, unmapped: 0 }), 'unknown', 'no exporters is unknown, not healthy')
  eq(M.receivingTone({ total: 1, receiving: 0, late: 0, silent: 0, never: 1, withProblems: 0, unmapped: 0 }), 'unknown', 'only never-received exporters is unknown')
  eq(M.filterExporters(rows, 'problems').map((r) => r.exporter).sort(), ['10.0.0.10', '10.0.0.3', '10.0.0.4'])
  eq(M.filterExporters(rows, 'never').map((r) => r.exporter), ['10.0.0.2'])
  eq(M.filterExporters(rows, 'all', '0.0.1').map((r) => r.exporter), ['10.0.0.10'], 'search matches the address')
  eq(M.filterExporters([row({ device_hostname: 'Core-RTR' })], 'all', 'core').length, 1, 'search matches the device, case-insensitive')
  eq(M.filterExporters([row({ display_name: 'WAN edge' })], 'all', 'wan').length, 1, 'search matches the display name')
  eq(M.stateTone('silent'), 'danger')
  eq(M.stateTone('late'), 'warning')
  eq(M.stateTone('receiving'), 'success')
  eq(M.stateTone('never'), 'unknown')
  eq(M.worstIssue({ issues: [{ severity: 'info' }, { severity: 'danger' }] }), 'danger')
  eq(M.worstIssue({ issues: [] }), null)
  eq(M.exporterLabel({ exporter: '10.0.0.1', display_name: '' }), '10.0.0.1', 'empty display name falls back to the address')
}

/* ─── Receipt age on the appliance clock (NF-01 / X-24) ─────────────── */
{
  const serverTime = '2026-10-01T10:00:00.000Z'
  const at = Date.parse(serverTime)
  const r = row({ receipt_age_s: 30 })
  eq(M.currentAgeSeconds(r, serverTime, at), 30, 'age at the server snapshot')
  eq(M.currentAgeSeconds(r, serverTime, at + 20 * 60_000), 30 + 1200, 'a frozen response keeps ageing with serverNow (X-24)')
  eq(M.currentAgeSeconds(r, serverTime, at - 5_000), 30, 'a server clock behind the snapshot never makes the age smaller')
  eq(M.currentAgeSeconds(row({ receipt_age_s: null }), serverTime, at + 1e6), null, 'never received stays unknown')
  eq(M.currentAgeSeconds(r, null, at), 30, 'without a server time the server age is used as is')
  eq(M.formatAge(5.4), '5 s')
  eq(M.formatAge(89), '89 s')
  eq(M.formatAge(90), '2 min')
  eq(M.formatAge(5400), '1.5 h')
  eq(M.formatAge(3 * 86400), '3 d')
  eq(M.formatAge(null), '—')
  eq(M.formatAge(-1), '—')
  eq(M.formatAge(Number.NaN), '—')
}

/* ─── Clock offset (received − export header; negative = exporter ahead) ─ */
{
  eq(M.formatClockOffset(-180_000), '3 min ahead')
  eq(M.formatClockOffset(240), '240 ms behind')
  eq(M.formatClockOffset(-20), 'in sync')
  eq(M.formatClockOffset(0), 'in sync')
  eq(M.formatClockOffset(7_000), '7 s behind')
  eq(M.formatClockOffset(null), '—')
  eq(M.clockTone(4_999), 'success')
  eq(M.clockTone(-30_000), 'warning')
  eq(M.clockTone(-60_000), 'danger')
  eq(M.clockTone(null), 'unknown')
  eq(M.protocolList(['netflow9', 'ipfix', 'sflow5', 'netflow5', 'x']), 'NetFlow v9, IPFIX, sFlow v5, NetFlow v5, x')
  eq(M.protocolList([]), '—')
  eq(M.samplingLabel(1), 'Unsampled')
  eq(M.samplingLabel(1000), '1 in 1,000')
  eq(M.samplingLabel(null), '—')
  eq(M.intervalBasisLabel('learned'), 'learned from its cadence')
  eq(M.intervalBasisLabel('configured'), 'configured')
  eq(M.intervalBasisLabel('default'), 'default')
}

/* ─── Collector ─────────────────────────────────────────────────────── */
{
  eq(M.collectorTone('ok', true), 'success')
  eq(M.collectorTone('degraded', true), 'warning')
  eq(M.collectorTone('down', true), 'danger')
  eq(M.collectorTone('ok', false), 'unknown', 'an unreachable collector is unknown, never healthy')
  eq(M.collectorWord('ok', true), 'Healthy')
  eq(M.collectorWord('down', false), 'Unknown')
  eq(M.droppedRecords({ records_dropped_implausible: 1, records_dropped_queue: 2, records_dropped_unknown_exporter: 3, records_unsupported: 4 }), 10)
  eq(M.droppedRecords(null), 0)
  eq(M.droppedRecords({ records_dropped_queue: 5 }), 5, 'missing counters count as nothing dropped')
}

/* ─── Statistics series grid: half-open, gaps are gaps ──────────────── */
{
  const p = (ts, written) => ({ ts, datagrams: 1, records_decoded: written, records_written: written, dropped: 0, waiting: 0, sequence_gaps: 0, parse_errors: 0, insert_failures: 0, queue_len: 0 })
  const grid = M.fillSeries([p('2026-10-01T10:00:00Z', 5), p('2026-10-01T10:02:00Z', 0)], '2026-10-01T10:00:00Z', '2026-10-01T10:04:00Z', 60)
  eq(grid.length, 4, '[10:00, 10:04) has four 1-minute buckets (end excluded)')
  eq(grid.map((g) => g.missing), [false, true, false, true], 'buckets without a statistics row are missing')
  eq(grid[2].records_written, 0, 'a recorded zero stays a zero')
  ok(grid[1].records_written === undefined, 'a missing bucket carries no value (drawn as a gap)')
  const aligned = M.fillSeries([p('2026-10-01T10:00:30Z', 7)], '2026-10-01T10:00:40Z', '2026-10-01T10:01:30Z', 60)
  eq(aligned.map((g) => new Date(g.t).toISOString()), ['2026-10-01T10:00:00.000Z', '2026-10-01T10:01:00.000Z'], 'grid starts at the bucket containing from')
  eq(aligned[0].records_written, 7, 'points snap to their bucket')
  eq(M.fillSeries([], '2026-10-01T10:00:00Z', '2026-10-01T10:00:00Z', 60), [], 'empty window')
  eq(M.fillSeries(null, 'x', 'y', 60), [], 'unparseable bounds')
  eq(M.fillSeries(null, '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 60).length, 2000, 'grid is bounded')
}

/* ─── Onboarding checks ─────────────────────────────────────────────── */
{
  eq(['pass', 'warn', 'fail', 'info', 'pending', 'na'].map(M.checkTone), ['success', 'warning', 'danger', 'info', 'unknown', 'unknown'])
  // 'late' / 'stopped' (wave C fix): an exporter that sent before but not within its interval is never "working".
  eq(Object.keys(M.OVERALL_TEXT).sort(), ['failing', 'late', 'partial', 'ready', 'stopped', 'waiting'])
  eq(M.OVERALL_TEXT.failing.tone, 'danger')
  eq(M.OVERALL_TEXT.waiting.tone, 'info', 'waiting for the first datagram is not an error')
}

/* ─── Setup: appliance address to pre-fill (CR-06) ─────────────────── */
{
  const addrs = [
    { address: '172.17.0.1', interface: 'docker0', family: 4, virtual: true },
    { address: '2001:db8::5', interface: 'ens18', family: 6, virtual: false },
    { address: '192.168.8.221', interface: 'ens18', family: 4, virtual: false },
    { address: '10.79.0.1', interface: 'ens19', family: 4, virtual: false },
  ]
  eq(M.preferredCollectorAddress(addrs, '10.79.0.1'), '10.79.0.1', 'the address the browser used, when it is the appliance')
  eq(M.preferredCollectorAddress(addrs, '127.0.0.1'), '192.168.8.221', 'a loopback page URL is never the flow destination')
  eq(M.preferredCollectorAddress(addrs, 'zenplus.example.com'), '192.168.8.221', 'a DNS name falls back to the first physical IPv4')
  eq(M.preferredCollectorAddress(addrs, '203.0.113.7'), '192.168.8.221', 'a NATed address the appliance does not own is not assumed')
  eq(M.preferredCollectorAddress([addrs[1]], '127.0.0.1'), '2001:db8::5', 'IPv6-only appliance')
  eq(M.preferredCollectorAddress([], '127.0.0.1'), '', 'nothing known: empty rather than loopback')
  eq(M.preferredCollectorAddress([], '198.51.100.4'), '198.51.100.4', 'no address list: the browser host when usable')
}

/* ─── Counting explanation (FE-12 / CR-16) ──────────────────────────── */
{
  eq(M.duplicationSentence({ records: 0, duplicate_observation_pct: null, multi_point_key_pct: null, byte_inflation_pct: null }),
    'No flow records arrived in the measured window, so duplication cannot be estimated.')
  ok(M.duplicationSentence({ records: 100, duplicate_observation_pct: 0.4, multi_point_key_pct: 0.4, byte_inflation_pct: 0 }).startsWith('Almost every flow'))
  const s = M.duplicationSentence({ records: 1000, duplicate_observation_pct: 48.7, multi_point_key_pct: 95, byte_inflation_pct: 96.4 })
  ok(s.includes('About 49%') && s.includes('95%') && s.includes('overstates volume by about 96%'), s)
  const t = M.duplicationSentence({ records: 1000, duplicate_observation_pct: 5.25, multi_point_key_pct: null, byte_inflation_pct: 0.2 })
  ok(t.includes('About 5.3%') && !t.includes('overstates') && t.endsWith('.'), t)
  eq(M.ROLE_LABELS.edge, 'Edge (counted)')
  eq(Object.keys(M.DIRECTION_LABELS), ['auto', 'ingress', 'egress', 'both'])
}

/* ─── Retention (FE-11) ─────────────────────────────────────────────── */
{
  const rows = M.retentionRows({
    raw_days: 30, rollup_days: 90, qos_days: 90, interface_days: 400, exporter_minutes_days: 400,
    earliest_raw: '2026-09-01T00:00:00Z', earliest_rollup: null, earliest_interface: null,
    configured: { raw_days: 14 },
  })
  eq(rows.map((r) => r.key), ['raw', 'rollup', 'qos', 'interface', 'minutes'])
  eq(rows[0].configured, 14, 'a configured value that differs from the applied TTL is shown')
  eq(rows[1].configured, null)
  eq(rows[4].earliest, null, 'missing earliest stays null (no data yet), never a fabricated date')
  eq(M.retentionText(null), 'No limit')
  eq(M.retentionText(30), '30 days')
  eq(M.retentionRows({ raw_days: null, rollup_days: null, interface_days: null, exporter_minutes_days: null, earliest_raw: null, earliest_rollup: null, earliest_interface: null })[2].days, null, 'unknown QoS retention stays null')
}

/* ─── Mapping confirmation (FE-08) ──────────────────────────────────── */
{
  const target = { device_id: 't', hostname: 'edge-1', ip_address: '10.0.0.5', vendor: null, model: null, status: 'up', snmp_enabled: true, tags: ['team-a'], interfaces: 4, interfaces_with_speed: 2 }
  const base = {
    exporter: '10.0.0.1', target, unchanged: false, overrides_ip_match: null,
    current_owner: { device_id: 'c', hostname: 'core-1', source: 'ip-match', tags: ['team-b'] },
    interfaces: { seen_24h: 3, resolve_on_target: 2, with_speed_on_target: 1, resolve_on_current: 0, unresolved_sample: [9] },
    visibility: { current_tags: ['team-b'], target_tags: ['team-a'], note: '' }, guidance: [],
  }
  const lines = M.impactLines(base)
  eq(lines[0], 'Interface names, speeds and ownership move from core-1 (management-IP match) to edge-1.')
  eq(lines[1], '2 of the 3 interface indexes this exporter reported in the last 24 hours exist on edge-1, 1 with a known speed.')
  eq(lines[2], 'Visibility: users limited to team-a will see its flows; users limited only to team-b will no longer see them.')
  eq(M.impactLines({ ...base, unchanged: true }), ['10.0.0.1 is already linked to edge-1.'])
  const fresh = M.impactLines({ ...base, current_owner: null, visibility: { current_tags: [], target_tags: [], note: '' }, target: { ...target, tags: [] },
    interfaces: { seen_24h: 0, resolve_on_target: 0, with_speed_on_target: 0, resolve_on_current: null, unresolved_sample: [] } })
  eq(fresh, [
    'Interface names, speeds and ownership of 10.0.0.1 will come from edge-1.',
    'The exporter reported no interface indexes in the last 24 hours, so names cannot be checked yet.',
    "edge-1 has no tags: only unrestricted users will see this exporter's flows.",
  ])
  const none = M.impactLines({ ...base, interfaces: { ...base.interfaces, resolve_on_target: 0 } })
  eq(none[1], '0 of the 3 interface indexes this exporter reported in the last 24 hours exist on edge-1.', 'no speed clause when nothing resolves')
}

/* ─── Exporter settings form (FE-12) ────────────────────────────────── */
{
  const d = { display_name: 'WAN', role: 'edge', count_direction: 'auto', expected_interval_s: '', note: '' }
  eq(validateSettingsDraft(d), {})
  eq(Object.keys(validateSettingsDraft({ ...d, expected_interval_s: '0' })), ['expected_interval_s'])
  eq(Object.keys(validateSettingsDraft({ ...d, expected_interval_s: '1.5' })), ['expected_interval_s'], 'whole seconds only')
  eq(Object.keys(validateSettingsDraft({ ...d, expected_interval_s: '86401' })), ['expected_interval_s'])
  eq(validateSettingsDraft({ ...d, expected_interval_s: ' 300 ' }), {}, 'surrounding spaces are fine')
  eq(Object.keys(validateSettingsDraft({ ...d, display_name: 'x'.repeat(121) })), ['display_name'])
  eq(Object.keys(validateSettingsDraft({ ...d, note: 'x'.repeat(2001) })), ['note'])
}

/* ─── Vendor configuration snippets (CR-06) ─────────────────────────── */
{
  const ids = Sn.VENDORS.map((v) => v.id)
  for (const want of ['cisco-iosxe', 'cisco-nxos', 'cisco-asa', 'fortigate', 'paloalto', 'juniper', 'mikrotik', 'aruba-cx', 'huawei']) {
    ok(ids.includes(want), `vendor ${want} is offered`)
  }
  for (const v of Sn.VENDORS) {
    const o = { ...Sn.defaultSnippetOptions(v.id, '192.0.2.10', 2055, 6343), interfaces: [], source: '' }
    const text = Sn.buildSnippet(v.id, o)
    ok(text.includes('192.0.2.10'), `${v.id}: appliance address in the snippet`)
    const port = o.format === 'sflow' ? '6343' : '2055'
    ok(text.includes(port), `${v.id}: ${o.format} uses UDP ${port}`)
    ok(!/undefined|NaN|null/.test(text), `${v.id}: no undefined values`)
    eq(Sn.buildSnippet(v.id, o), text, `${v.id}: deterministic`)
    ok(v.formats.includes(v.defaultFormat), `${v.id}: default format is supported`)
  }
  const ios = Sn.buildSnippet('cisco-iosxe', { ...Sn.defaultSnippetOptions('cisco-iosxe', '192.0.2.10', 2055, 6343), interfaces: ['Gi0/0/0', 'Gi0/0/1'], source: 'Loopback0' })
  for (const needle of ['collect transport tcp flags', 'match interface input', 'collect interface output', 'collect timestamp absolute first',
    'cache timeout active 60', 'cache timeout inactive 15', 'template data timeout 60', 'export-protocol ipfix', 'transport udp 2055',
    'source Loopback0', 'interface Gi0/0/1', 'ipv6 flow monitor ZENPLUS-V6 input', 'option sampler-table']) {
    ok(ios.includes(needle), `IOS-XE: ${needle}`)
  }
  const iosV4 = Sn.buildSnippet('cisco-iosxe', { ...Sn.defaultSnippetOptions('cisco-iosxe', '192.0.2.10', 2055, 6343), includeIpv6: false, format: 'netflow9' })
  ok(!iosV4.includes('ZENPLUS-V6') && iosV4.includes('export-protocol netflow-v9'), 'IOS-XE: IPv6 off and v9')
  ok(iosV4.includes('interface GigabitEthernet0/0/0'), 'IOS-XE: interface placeholder when none given')
  const asa = Sn.buildSnippet('cisco-asa', { ...Sn.defaultSnippetOptions('cisco-asa', '192.0.2.10', 9995, 6343), source: 'mgmt' })
  ok(asa.includes('flow-export destination mgmt 192.0.2.10 9995') && asa.includes('flow-export template timeout-rate 1') && asa.includes('event-type all'), 'ASA NSEL')
  const fgt = Sn.buildSnippet('fortigate', { ...Sn.defaultSnippetOptions('fortigate', '192.0.2.10', 2055, 6343), interfaces: ['port1'], source: '10.1.1.1' })
  ok(fgt.includes('set collector-ip 192.0.2.10') && fgt.includes('set source-ip 10.1.1.1') && fgt.includes('edit "port1"') && fgt.includes('set netflow-sampler both'), 'FortiGate')
  const pan = Sn.buildSnippet('paloalto', Sn.defaultSnippetOptions('paloalto', '192.0.2.10', 2055, 6343))
  ok(pan.includes('server-profile netflow ZENPLUS server ZENPLUS host 192.0.2.10 port 2055') && pan.trim().endsWith('commit'), 'PAN-OS')
  const jun = Sn.buildSnippet('juniper', { ...Sn.defaultSnippetOptions('juniper', '192.0.2.10', 2055, 6343), samplingRate: 500, interfaces: ['ge-0/0/0'] })
  ok(jun.includes('input rate 500') && jun.includes('version-ipfix template ZENPLUS-V4') && jun.includes('set interfaces ge-0/0/0 unit 0 family inet6 sampling input'), 'Junos')
  const mt = Sn.buildSnippet('mikrotik', { ...Sn.defaultSnippetOptions('mikrotik', '192.0.2.10', 2055, 6343), inactiveTimeoutS: 15 })
  ok(mt.includes('active-flow-timeout=1m') && mt.includes('inactive-flow-timeout=15s') && mt.includes('interfaces=all') && mt.includes('version=ipfix'), 'RouterOS')
  const aruba = Sn.buildSnippet('aruba-cx', Sn.defaultSnippetOptions('aruba-cx', '192.0.2.10', 2055, 6343))
  ok(aruba.includes('sflow collector 192.0.2.10 port 6343') && aruba.includes('sflow sampling 1000'), 'Aruba CX sFlow on 6343')
  const arubaIpfix = Sn.buildSnippet('aruba-cx', { ...Sn.defaultSnippetOptions('aruba-cx', '192.0.2.10', 2055, 6343), format: 'ipfix' })
  ok(arubaIpfix.includes('transport udp 2055') && !arubaIpfix.includes('sflow'), 'Aruba CX IPFIX on 2055')
  const hw = Sn.buildSnippet('huawei', Sn.defaultSnippetOptions('huawei', '192.0.2.10', 2055, 6343))
  ok(hw.includes('ip netstream export host 192.0.2.10 2055') && !hw.includes('sampler'), 'Huawei: no sampler when unsampled')
  ok(Sn.buildSnippet('huawei', { ...Sn.defaultSnippetOptions('huawei', '192.0.2.10', 2055, 6343), samplingRate: 100 }).includes('fix-packets 100'), 'Huawei sampler')
  const nx = Sn.buildSnippet('cisco-nxos', { ...Sn.defaultSnippetOptions('cisco-nxos', '192.0.2.10', 2055, 6343), format: 'sflow' })
  ok(nx.includes('version 9') && nx.includes('2055'), 'an unsupported format falls back to the vendor default')
  const generic = Sn.buildSnippet('generic', { ...Sn.defaultSnippetOptions('generic', '192.0.2.10', 2055, 6343), format: 'sflow', samplingRate: 2000 })
  ok(generic.includes('UDP 6343') && generic.includes('1 in 2000'), 'generic sFlow')
  eq(Sn.vendorInfo('nope').id, 'generic', 'unknown vendor -> generic')
  ok(Sn.RECOMMENDED_FIELDS.filter((f) => f.required).length >= 5, 'required fields listed')
}

console.log(`test-netflow-health: ${checks} checks passed`)
