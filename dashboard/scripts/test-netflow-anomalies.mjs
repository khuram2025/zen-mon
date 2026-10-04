// NetFlow anomalies / detection findings (WP B5) pure logic: an empty findings list is never a
// security all-clear and names the detectors that did not run (NF-03, NF-07, X-33), Investigate
// opens Forensics on the finding's frozen evidence window with the detector's own predicates and
// the finding id, separately from "all traffic for the host" (NF-09), team-dashboard links carry
// the clicked range (X-44), lifecycle actions follow permissions (FE-06), settings/exception/feed
// form validation mirrors the server, and the same activity seen by several exporters is grouped.
// Run from dashboard/: node scripts/test-netflow-anomalies.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'

const root = process.cwd()
const src = path.join(root, 'src')
const entry = `
export * as M from '@/pages/netflow/anomalies/model'
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
const { M, parseFlowFilter } = m
let checks = 0
const ok = (cond, msg) => { assert.ok(cond, msg); checks++ }
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++ }

const det = (kind, status, extra = {}) => ({ kind, label: kind.replace(/_/g, ' '), category: 'scan', status, reason: null, last_run_at: null, ...extra })
const ALL_OK = M.DETECTOR_KINDS.map((k) => det(k, 'ok'))
const query = (link) => new URL(link, 'http://x').searchParams

/* ─── NF-03 / X-33: an empty list is never "all checks passed" ─────── */
{
  const done = M.assessEmpty({ detectors: ALL_OK, complete: true, mode: 'run' })
  eq(done.tone, 'success')
  ok(done.title.startsWith('No findings from the 10 detectors that ran'), done.title)
  ok(!/pass|secure|clean|no threat|hostile/i.test(done.title + done.detail), 'never claims a security all-clear')
  ok(/not a security guarantee/.test(done.detail), 'says these are heuristics')

  // A template without TCP flags: SYN/RST checks are unavailable and named, not "passed" (NF-07).
  const noFlags = ALL_OK.map((d) => (['syn_scan', 'port_scan', 'rst_flood'].includes(d.kind)
    ? { ...d, status: 'unavailable', reason: 'TCP flags are not exported by 10.0.0.1' } : d))
  const a = M.assessEmpty({ detectors: noFlags, complete: false, mode: 'run' })
  eq(a.tone, 'warning')
  ok(a.title.includes('7 of 10 detectors that ran'), a.title)
  ok(/Not evaluated: syn scan, port scan, rst flood/.test(a.detail), a.detail)
  eq(a.notRun.map((d) => d.kind), ['syn_scan', 'port_scan', 'rst_flood'])

  // Nothing ran at all: warning with the first reason.
  const none = M.assessEmpty({ detectors: M.DETECTOR_KINDS.map((k) => det(k, 'no_coverage', { reason: 'No flow records' })), complete: false, mode: 'run' })
  eq(none.tone, 'warning')
  eq(none.title, 'No detector could evaluate this window')
  ok(none.detail.includes('No flow records'))

  // Failed detectors are listed as not evaluated.
  const failed = M.assessEmpty({ detectors: ALL_OK.map((d, i) => (i === 0 ? { ...d, status: 'failed', reason: 'The detector query failed' } : d)), complete: false, mode: 'run' })
  eq(failed.tone, 'warning')
  ok(failed.detail.startsWith('Not evaluated: syn scan'), failed.detail)

  // Stored mode: an engine that never ran, or stopped, is not "no findings".
  const never = M.assessEmpty({ detectors: ALL_OK, complete: false, mode: 'stored', engine: { status: 'never', last_run_at: null, last_bucket_end: null, age_s: null } })
  eq(never.tone, 'unknown')
  ok(/has not evaluated/.test(never.title))
  const stale = M.assessEmpty({ detectors: ALL_OK, complete: false, mode: 'stored', engine: { status: 'stale', last_run_at: '2026-10-01T00:00:00Z', last_bucket_end: null, age_s: 9000 } })
  eq(stale.tone, 'danger')
  ok(/missing, not absent/.test(stale.detail))

  // The engine only evaluated part of the window: not complete even if every detector ran.
  const partial = M.assessEmpty({ detectors: ALL_OK, complete: true, mode: 'stored', engine: { status: 'running', last_run_at: 'x', last_bucket_end: 'x', age_s: 10 }, evaluatedPct: 22.6 })
  eq(partial.tone, 'warning')
  ok(partial.detail.includes('evaluated 22.6% of this window'), partial.detail)

  // Ran on part of the exporters (flags missing on one, sampling on another).
  const some = ALL_OK.map((d) => (d.kind === 'syn_scan' ? { ...d, exporters_unavailable: ['10.0.0.9'] } : d))
  const sp = M.assessEmpty({ detectors: some, complete: true, mode: 'run' })
  eq(sp.tone, 'warning')
  ok(/syn scan ran on part of the traffic only/.test(sp.detail), sp.detail)
  eq(M.summarizeDetectors(some).partial.map((d) => d.kind), ['syn_scan'])

  eq(M.coverageLine(noFlags.map((d, i) => (i === 9 ? { ...d, status: 'disabled' } : d))), '6 of 9 detectors ran · 3 unavailable · 1 off')
  eq(M.coverageLine([]), 'Detector status unknown')
}

/* ─── NF-09: Investigate keeps the evidence ─────────────────────────── */
const icmp = {
  id: '11111111-2222-4333-8444-555555555555', persisted: true, kind: 'icmp_flood', src: '10.12.50.81', dst: null, host: '10.12.50.81',
  first_seen: '2026-09-29T10:00:00Z', last_seen: '2026-09-30T10:00:00Z', occurrences: 3,
  evidence: {
    window: { from: '2026-09-29T10:00:00Z', to: '2026-09-30T10:00:00Z' },
    latest: { from: '2026-09-30T09:55:00Z', to: '2026-09-30T10:00:00Z' },
    filter: { exporter: '10.9.0.1', src: '10.12.50.81', protocol: '1' },
    host: '10.12.50.81',
  },
}
{
  const link = M.investigateLink(icmp)
  ok(link.startsWith('/netflow/forensics?'), link)
  const q = query(link)
  eq(q.get('from'), '2026-09-29T10:00:00Z', 'the frozen 24 h evidence window, not a live relative range')
  eq(q.get('to'), '2026-09-30T10:00:00Z')
  ok(q.get('range') === 'custom' && !q.has('hours'), 'never a relative window')
  eq(q.get('protocol'), '1', 'ICMP selected')
  eq(q.get('src'), '10.12.50.81', 'the right host')
  eq(q.get('exporter'), '10.9.0.1')
  eq(q.get('finding'), icmp.id, 'the finding id for the evidence banner')
  eq(q.get('finding_kind'), 'icmp_flood')
  eq(q.get('run'), '1', 'Forensics runs the query on open')
  const f = parseFlowFilter(q)
  eq(f.protocol, 1)
  eq(f.src, '10.12.50.81')

  // Sensitive egress keeps destination and service ports.
  const egress = {
    ...icmp, kind: 'sensitive_egress', dst: '203.0.113.7',
    evidence: { ...icmp.evidence, filter: { exporter: '10.9.0.1', src: '10.0.0.5', dst: '198.51.100.20', dst_ports: '22,3389' } },
  }
  const eq2 = query(M.investigateLink(egress))
  eq(eq2.get('dst'), '198.51.100.20')
  eq(eq2.get('dst_ports'), '22,3389')

  // The broader action is separate: every protocol and peer of the host, same window.
  const host = query(M.hostTrafficLink(icmp))
  eq(host.get('talker'), '10.12.50.81')
  ok(!host.has('protocol') && !host.has('src') && !host.has('finding'), 'no detector predicates')
  eq(host.get('from'), '2026-09-29T10:00:00Z')

  const latest = query(M.latestLink(icmp))
  eq([latest.get('from'), latest.get('to')], ['2026-09-30T09:55:00Z', '2026-09-30T10:00:00Z'])
  eq(latest.get('protocol'), '1')

  // An ad-hoc candidate links with its persisted id when the engine has one, else none.
  const cand = { ...icmp, id: 'f13b50d9f3f2a5b6ea71a3c6afbce8eec63268d6', persisted: false, persisted_id: null }
  ok(!query(M.investigateLink(cand)).has('finding'), 'a fingerprint is not a finding id')
  eq(query(M.investigateLink({ ...cand, persisted_id: icmp.id })).get('finding'), icmp.id)

  // Invalid evidence values are dropped, not forwarded.
  const bad = { ...icmp, evidence: { ...icmp.evidence, filter: { src: 'not-an-ip', protocol: '1', tcpflag: '' } } }
  const bq = query(M.investigateLink(bad))
  ok(!bq.has('src') && !bq.has('tcpflag'), bq.toString())
  eq(bq.get('protocol'), '1')

  // No evidence window and no first/last span: no link (rather than a misleading one).
  eq(M.investigateLink({ ...icmp, evidence: { filter: icmp.evidence.filter }, first_seen: null, last_seen: null }), null)
  eq(M.evidenceWindow({ evidence: null, first_seen: '2026-09-30T09:00:00Z', last_seen: '2026-09-30T10:00:00Z' }), { from: '2026-09-30T09:00:00Z', to: '2026-09-30T10:00:00Z' })
  eq(M.hostProfileLink(icmp), '/netflow/hosts/10.12.50.81')
}

/* ─── X-44: dashboard links keep the clicked range ─────────────────── */
{
  const now = Date.parse('2026-10-01T03:58:41Z')
  eq(M.anomaliesLinkForRange('24h', 24, now), '/netflow/anomalies?range=24h')
  const six = query(M.rangeLink('/netflow/anomalies', '6h', 6, now))
  eq(six.get('from'), '2026-09-30T21:58:00.000Z')
  eq(six.get('to'), '2026-10-01T03:58:00.000Z')
  eq(query(M.rangeLink('/netflow', '1h', 1, now, { exporter: '10.0.0.1' })).get('exporter'), '10.0.0.1')
}

/* ─── Lifecycle permissions (FE-06, contract §6) ───────────────────── */
{
  const viewer = { acknowledge: false, suppress: false }
  const ack = { acknowledge: true, suppress: false }
  const mgr = { acknowledge: true, suppress: true }
  eq(M.allowedActions('open', viewer), [], 'netflow.view alone is read-only')
  eq(M.allowedActions('open', ack), ['acknowledge', 'assign', 'resolve', 'note'])
  ok(!M.allowedActions('open', ack).includes('suppress'), 'suppress needs netflow.manage')
  eq(M.allowedActions('open', mgr), ['acknowledge', 'assign', 'resolve', 'note', 'suppress'])
  eq(M.allowedActions('acknowledged', ack), ['assign', 'resolve', 'reopen', 'note'])
  eq(M.allowedActions('resolved', ack), ['reopen', 'note'])
  eq(M.allowedActions('suppressed', ack), ['assign', 'note'], 'lifting a suppression needs netflow.manage')
  eq(M.allowedActions('suppressed', mgr), ['assign', 'note', 'unsuppress', 'resolve'])
  eq(M.allowedActions('candidate', mgr), [], 'ad-hoc candidates have no lifecycle')
}

/* ─── Settings validation mirrors the server ───────────────────────── */
{
  const p = (o) => ({ key: 'k', label: 'K', kind: 'int', default: 100, min: 10, max: 1000, unit: '', help: '', value: 100, ...o })
  eq(M.validateParam(p(), '250'), { value: 250 })
  eq(M.validateParam(p(), '0'), { error: 'Must be at least 10' }, 'zero is a value, checked against the range')
  eq(M.validateParam(p(), '12.5'), { error: 'Must be a whole number' })
  eq(M.validateParam(p(), ''), { error: 'Required' })
  eq(M.validateParam(p({ kind: 'float', min: 1, max: 100 }), '50.5'), { value: 50.5 })
  eq(M.validateParam(p({ kind: 'severity' }), 'critical'), { value: 'critical' })
  ok('error' in M.validateParam(p({ kind: 'severity' }), 'high'))
  eq(M.validateParam(p({ kind: 'bool' }), true), { value: true })
  eq(M.parsePorts('3389, 22 445,22'), { ports: [22, 445, 3389] })
  eq(M.parsePorts('22, 70000'), { error: 'Port 70000 is outside 0–65535' })
  eq(M.parsePorts('ssh'), { error: '"ssh" is not a port number' })
  eq(M.parsePorts(' '), { error: 'Give at least one port' })
  const rows = [
    { kind: 'syn_scan', enabled: true, params: [p({ key: 'min_destinations', value: 100 }), p({ key: 'severity', kind: 'severity', value: 'warning' })] },
    { kind: 'icmp_sweep', enabled: true, params: [p({ key: 'min_destinations', value: 100 })] },
  ]
  eq(M.settingsPatch(rows, { syn_scan: { min_destinations: 100, severity: 'critical' }, icmp_sweep: { enabled: false } }),
    { detectors: { syn_scan: { severity: 'critical' }, icmp_sweep: { enabled: false } } }, 'only what changed is sent')
  eq(M.settingsPatch(rows, { syn_scan: { min_destinations: 100 } }), { detectors: {} })
}

/* ─── Exceptions ───────────────────────────────────────────────────── */
{
  const base = { kind: '', src: '', dst: '', port: '', reason: '', expires: '' }
  const e = M.exceptionBody(base)
  ok('errors' in e && e.errors.src && e.errors.reason, 'an exception cannot match everything and needs a reason')
  const good = M.exceptionBody({ ...base, kind: 'icmp_flood', src: '10.20.1.5', reason: ' Monitoring host ', expires: '2026-12-01T00:00' })
  eq(good.body, { kind: 'icmp_flood', src_cidr: '10.20.1.5', dst_cidr: null, dst_port: null, reason: 'Monitoring host', expires_at: '2026-12-01T00:00:00.000Z' })
  eq(M.exceptionBody({ ...base, port: '0', reason: 'x' }).body.dst_port, 0, 'port 0 is a value')
  ok('errors' in M.exceptionBody({ ...base, port: '65536', reason: 'x' }))
  ok('errors' in M.exceptionBody({ ...base, src: 'host.example', reason: 'x' }))
  eq(M.exceptionScope({ src_cidr: '10.20.1.5/32', dst_cidr: '2001:db8::/32', dst_port: 443 }), 'from 10.20.1.5 to 2001:db8::/32 port 443')
  eq(M.exceptionScope({ src_cidr: null, dst_cidr: null, dst_port: null }), 'any traffic')
  eq(M.cidrText('2001:db8::1/128'), '2001:db8::1')
  // Exporter scope (migrate-140): optional on the form, shown on every row.
  ok(!('exporters' in good.body), 'no exporters typed: the server binds the scope (every exporter, or the caller\'s)')
  const scoped = M.exceptionBody({ ...base, src: '10.20.1.5', reason: 'x', exporters: '127.0.0.91, 127.0.0.92 127.0.0.91' })
  eq(scoped.body.exporters, ['127.0.0.91', '127.0.0.92'], 'comma/space separated, de-duplicated')
  eq(M.exceptionBody({ ...base, src: '10.20.1.5', reason: 'x', exporters: '10.0.0.0/24' }).errors.exporters, '"10.0.0.0/24" is not an exporter IP address')
  eq(M.parseExporterList('2001:db8::1'), { exporters: ['2001:db8::1'] })
  const row = (o) => ({ scope: 'exporters', exporters: ['127.0.0.91'], exporters_outside_scope: false, ...o })
  eq(M.exceptionExporters(row({ scope: 'global', exporters: null })), 'Every exporter')
  eq(M.exceptionExporters(row({})), 'Exporter 127.0.0.91')
  eq(M.exceptionExporters(row({ exporters: ['10.0.0.1', '10.0.0.2', '10.0.0.3', '10.0.0.4', '10.0.0.5'] })), 'Exporters 10.0.0.1, 10.0.0.2, 10.0.0.3 and 2 more')
  eq(M.exceptionExporters(row({ exporters_outside_scope: true })), 'Exporter 127.0.0.91, and exporters outside your scope', 'other teams\' exporters are never named')
}

/* ─── Threat-intel feed state ──────────────────────────────────────── */
{
  const f = (o) => ({ id: '1', name: 'f', source: 'url', url: 'https://x', category: 'botnet_c2', enabled: true, refresh_hours: 24, last_refreshed_at: null, last_status: null, entry_count: 10, ...o })
  eq(M.feedState(f({ enabled: false })).label, 'Disabled')
  eq(M.feedState(f({ last_status: 'Download failed (HTTP 503); previous entries kept' })).tone, 'danger')
  eq(M.feedState(f({ entry_count: 0 })).label, 'Empty')
  eq(M.feedState(f()).label, 'Active')
}

/* ─── Grouping across exporters, focus links, kinds ─────────────────── */
{
  const fi = (id, kind, src, dst, exporter, metrics = {}) => ({ id, kind, src, dst, exporter, metrics })
  const items = [
    fi('a', 'threat_intel', '10.0.0.1', '151.101.1.140', '127.0.0.61', { ti_address: '151.101.1.140' }),
    fi('b', 'syn_scan', '10.20.3.66', null, '127.0.0.61'),
    fi('c', 'threat_intel', '10.0.0.7', '151.101.1.140', '127.0.0.62', { ti_address: '151.101.1.140' }),
    fi('d', 'syn_scan', '10.20.3.66', null, '127.0.0.62'),
    fi('e', 'syn_scan', '10.20.3.67', null, '127.0.0.62'),
  ]
  const g = M.groupFindings(items)
  eq(g.map((x) => [x.primary.id, x.others.map((o) => o.id)]), [['a', ['c']], ['b', ['d']], ['e', []]],
    'one group per detector and subject, order kept, a listed address groups its internal hosts')
  eq(M.findingFocusId(new URLSearchParams('finding=11111111-2222-4333-8444-555555555555')), '11111111-2222-4333-8444-555555555555')
  eq(M.findingFocusId(new URLSearchParams('finding=icmp_flood')), null, 'a detector name is not a finding id')
  ok(M.isDetectorKind('port_scan') && !M.isDetectorKind('exfil') && !M.isDetectorKind('beacon'), 'no detectors that do not exist (X-33)')
  eq(M.subjectText({ src: '10.0.0.1', dst: null, host: null }), '10.0.0.1 →')
}

/* ─── Threshold and observed wording ───────────────────────────────── */
{
  eq(M.thresholdPhrases({ min_pps: 1000, effective_pps: 2500, min_minutes: 3, baseline_multiplier: 5 }),
    ['≥ 1,000 packets/s', 'effective 2,500 packets/s (incl. baseline)', 'for 3 consecutive minutes', 'above 5× own baseline'])
  eq(M.thresholdPhrases({ ports: [22, 3389], min_bytes_without_ack: 10000 }), ['ports 22, 3389', '≥ 10,000 bytes when no ACK was seen'])
  eq(M.observedPairs({ peak_pps: 6000, ports: [], sent_bytes: 5 }).map((x) => x.label), ['Peak packets/s', 'Bytes sent to it'])
}

/* ─── Timeline: unknown is not zero (NF-03) ─────────────────────────── */
{
  const pt = (o) => ({ t: '2026-10-01T00:00:00Z', covered: true, records: 0, bytes: 0, packets: 0, pps: 0, bps: 0, dsts: 0, srcs: 0, dports: 0, rsts: 0, ...o })
  eq(M.timelineValue(pt({ pps: 5000 }), 'pps'), 5000)
  eq(M.timelineValue(pt({}), 'pps'), 0, 'a measured zero is a zero')
  eq(M.timelineValue(pt({ covered: false, pps: null, records: null }), 'pps'), null, 'nothing received is a gap')
  eq(M.timelineValue(pt({ covered: null, bps: 12 }), 'bps'), 12, 'coverage unknown keeps the measured value')
}

console.log(`test-netflow-anomalies: ${checks} checks passed`)
