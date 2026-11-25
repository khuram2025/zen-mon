// Settings > General > Monitoring > "Service check connections" in a browser (jsdom), plus the rules behind it
// (docs/service-check-connections.md): the manual host address table with its warnings (a sensor too old to
// apply a row, a removed sensor), add / edit / remove with validation messages on the field, the trusted
// certificate summary with its link to Security & TLS and the link back, the section for accounts that are
// not administrators, and the hint a failed check test shows for certificate trust and name resolution.
import assert from 'node:assert/strict'
import { build } from 'esbuild'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { JSDOM } from 'jsdom'
import React, { act } from 'react'

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true })
for (const name of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLSelectElement', 'DocumentFragment', 'Node', 'NodeFilter', 'CustomEvent', 'MutationObserver', 'Element', 'Event', 'KeyboardEvent', 'MouseEvent', 'SubmitEvent']) globalThis[name] = dom.window[name]
Object.assign(globalThis, {
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
})
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
const scrolled = []
dom.window.HTMLElement.prototype.scrollIntoView = function () { scrolled.push(this.id) }
dom.window.HTMLElement.prototype.hasPointerCapture = () => false
dom.window.HTMLElement.prototype.releasePointerCapture = () => {}
globalThis.ResizeObserver = dom.window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
const { createRoot } = await import('react-dom/client')
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const { MemoryRouter } = await import('react-router-dom')

const SENSOR_OLD = '11111111-1111-4111-8111-111111111111'
const SENSOR_NEW = '22222222-2222-4222-8222-222222222222'
const SENSORS = [
  { id: SENSOR_OLD, name: 'Branch A', status: 'online', version: '1.25.0', applies: false },
  { id: SENSOR_NEW, name: 'Branch B', status: 'online', version: '1.26.0', applies: true },
]
const entry = (over) => ({ id: 'e1', host: 'portal.example.local', address: '10.1.2.3', scope: 'all', sensor_id: null, sensor_name: null,
  sensor_missing: false, note: 'Behind the WAF', updated_at: new Date(Date.now() - 3600_000).toISOString(), updated_by: 'admin', not_applied_by: [], ...over })
const DOC = () => ({
  max_entries: 200,
  sensors: SENSORS,
  entries: [
    entry({ id: 'e1', not_applied_by: ['Branch A'] }),
    entry({ id: 'e2', host: 'branch.example.local', address: '2001:db8::10', scope: 'sensor', sensor_id: SENSOR_NEW, sensor_name: 'Branch B', note: '' }),
    entry({ id: 'e3', host: 'gone.example.local', address: '10.9.9.9', scope: 'sensor', sensor_id: 'x', sensor_name: null, sensor_missing: true, note: '' }),
    entry({ id: 'e4', host: 'hq.example.local', address: '10.0.0.8', scope: 'central', note: '' }),
  ],
})
const TRUST = { auto_fetch_intermediates: true, certificates: [
  { id: 'a', label: 'Corp CA', subject: 'CN=Corp CA', issuer: 'CN=Corp CA', hosts: [], is_ca: true, not_after: new Date(Date.now() + 86400_000 * 300).toISOString() },
  { id: 'b', label: 'Old portal', subject: 'CN=portal', issuer: 'CN=portal', hosts: ['portal.example.local'], is_ca: false, not_after: new Date(Date.now() - 86400_000).toISOString() },
] }

const calls = []
let doc = DOC()
let refuse = null
globalThis.__connectionsFixture = {
  get: async (url) => {
    calls.push({ verb: 'get', url })
    if (url === '/system/monitoring/host-overrides') return { data: doc }
    if (url === '/system/security/probe-trust') return { data: TRUST }
    throw new Error(`unexpected GET ${url}`)
  },
  post: async (url, body) => {
    calls.push({ verb: 'post', url, body })
    if (refuse) throw Object.assign(new Error('refused'), { response: { status: refuse.status, data: { detail: refuse.detail } } })
    doc = { ...doc, entries: [...doc.entries, entry({ id: 'new', host: body.host.toLowerCase(), address: body.address, scope: body.scope, note: body.note })] }
    return { data: doc }
  },
  put: async (url, body) => { calls.push({ verb: 'put', url, body }); return { data: doc } },
  delete: async (url) => {
    calls.push({ verb: 'delete', url })
    doc = { ...doc, entries: doc.entries.filter((item) => !url.endsWith(`/${item.id}`)) }
    return { data: doc }
  },
}
const plugins = [{ name: 'fixtures', setup(b) {
  b.onResolve({ filter: /^(react(?:\/.*)?|react-dom(?:\/.*)?|@tanstack\/react-query|react-router-dom)$/ }, (arg) => ({ path: import.meta.resolve(arg.path), external: true }))
  b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'fixture' }))
  b.onResolve({ filter: /^@\/components\/ui\/Toast$/ }, () => ({ path: 'toast', namespace: 'fixture' }))
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, (arg) => ({
    contents: arg.path === 'api'
      ? 'export const api=globalThis.__connectionsFixture;export function setApiToken(){};export function getApiToken(){return null}'
      : 'export const toast={success(...a){globalThis.__toasts=(globalThis.__toasts||[]).concat([a])},error(...a){globalThis.__toastErrors=(globalThis.__toastErrors||[]).concat([a])}};export function Toaster(){return null}',
    loader: 'js',
  }))
  b.onResolve({ filter: /^@\// }, (arg) => b.resolve(`./${arg.path.slice(2)}`, { resolveDir: path.resolve('src'), kind: arg.kind }))
} }]
const cache = path.resolve('node_modules/.cache')
await mkdir(cache, { recursive: true })
const temporary = await mkdtemp(path.join(cache, 'service-check-connections-'))
const out = await build({
  stdin: { contents: `
    export { ServiceCheckConnectionsCard } from '@/components/settings/ServiceCheckConnectionsCard'
    export { ProbeTrustCard } from '@/components/ProbeTrustCard'
    export { ConnectionHints } from '@/components/services/ConnectionHints'
    export * as rules from '@/components/settings/hostOverrides'
    export * as hints from '@/components/services/connectionAdvice'
    export { useAuth } from '@/stores/auth'`,
    resolveDir: process.cwd(), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent', plugins,
  loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty', '.woff2': 'empty' },
  define: { 'process.env.NODE_ENV': '"production"' },
})
const file = path.join(temporary, 'ui.mjs')
await writeFile(file, out.outputFiles[0].text)
const M = await import(pathToFileURL(file).href)
const { rules, hints } = M
const ADMIN = { id: 'me', username: 'admin', role: 'admin', permissions: ['system.admin', 'settings.manage'] }
const SETTINGS_ONLY = { id: 'me', username: 'ops', role: 'custom', permissions: ['settings.manage', 'service_checks.manage'] }

const root = createRoot(document.getElementById('root'))
const settle = async () => act(async () => { await new Promise((r) => setTimeout(r, 30)) })
const text = () => document.body.textContent.replace(/\s+/g, ' ')
const button = (label) => [...document.querySelectorAll('button')].find((e) => e.textContent.trim() === label || e.getAttribute('aria-label') === label)
const typeInto = async (element, value) => act(async () => {
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(element, value)
  element.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
})
const field = (placeholder) => document.querySelector(`input[placeholder="${placeholder}"]`)
let client
async function mount(element, { user = ADMIN, at = '/settings/general?tab=monitoring' } = {}) {
  await act(async () => root.render(null))
  M.useAuth.setState({ user, token: 't' })
  client?.clear()
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { gcTime: Infinity } } })
  await act(async () => root.render(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, { initialEntries: [at] }, element))))
  await settle(); await settle()
}

try {
  // ── Form rules (the server validates again, with the same answers) ────────
  const valid = { host: 'New.Example.Local', address: '10.5.5.5', scope: 'all', sensor_id: '', note: '' }
  assert.deepEqual(rules.validateHostOverride(valid, doc.entries), {})
  for (const [host, expected] of [
    ['', /Enter the host name/], ['*.example.local', /Wildcards/], ['https://portal.example.local', /without http/],
    ['portal.example.local/login', /without http/], ['portal.example.local:8443', /without a port/], ['10.1.2.3', /an address, not a name/],
    ['2001:db8::1', /an address, not a name/], ['bad..name', /not a valid host name/], ['-bad.test', /not a valid host name/],
  ]) assert.match(rules.validateHostOverride({ ...valid, host }, []).host || '', expected, `host "${host}"`)
  for (const [address, expected] of [
    ['', /Enter the IPv4 or IPv6 address/], ['portal.example.local', /one IPv4 or IPv6 address/], ['10.1.2.3:443', /one IPv4 or IPv6 address/],
    ['10.1.2.0/24', /one IPv4 or IPv6 address/], ['300.1.1.1', /one IPv4 or IPv6 address/], ['fe80::1%eth0', /interface zone/], ['0.0.0.0', /cannot be connected/],
  ]) assert.match(rules.validateHostOverride({ ...valid, address }, []).address || '', expected, `address "${address}"`)
  for (const address of ['10.1.2.3', '2001:db8::10', '[2001:db8::10]', '::1']) assert.equal(rules.validateHostOverride({ ...valid, address }, []).address, undefined, address)
  assert.equal(rules.ipVersion('10.1.2.3'), 4)
  assert.equal(rules.ipVersion('2001:db8::10'), 6)
  assert.equal(rules.ipVersion('portal.example.local'), null)
  assert.match(rules.validateHostOverride({ ...valid, scope: 'sensor' }, []).sensor_id, /Choose the sensor/)
  assert.match(rules.validateHostOverride({ ...valid, note: 'x'.repeat(201) }, []).note, /within 200/)
  // One address per name and place; another place for the same name is fine, and a row may be saved over itself.
  assert.match(rules.validateHostOverride({ ...valid, host: 'PORTAL.example.local.' }, doc.entries).host, /already has an address for that place/)
  assert.deepEqual(rules.validateHostOverride({ ...valid, host: 'portal.example.local', scope: 'central' }, doc.entries), {})
  assert.deepEqual(rules.validateHostOverride({ ...valid, host: 'portal.example.local' }, doc.entries, 'e1'), {})
  assert.deepEqual(rules.payloadFromForm({ host: ' a.test ', address: ' 10.0.0.1 ', scope: 'central', sensor_id: SENSOR_NEW, note: ' n ' }),
    { host: 'a.test', address: '10.0.0.1', scope: 'central', sensor_id: null, note: 'n' })
  assert.equal(rules.scopeLabel(doc.entries[0]), 'Everywhere')
  assert.equal(rules.scopeLabel(doc.entries[1]), 'Sensor Branch B')
  assert.equal(rules.scopeLabel(doc.entries[3]), 'This appliance only')
  assert.equal(rules.rowWarning(doc.entries[1]), null)
  assert.match(rules.rowWarning(doc.entries[0]), /Not applied by Branch A: that sensor runs a version that cannot use manual addresses/)
  assert.match(rules.rowWarning(doc.entries[2]), /no longer exists/)
  assert.match(rules.trustSummary(TRUST).text, /^1 certificate authority and 1 self-signed service certificate trusted/)
  assert.equal(rules.trustSummary(TRUST).expired, 1)
  assert.match(rules.trustSummary({ auto_fetch_intermediates: false, certificates: [] }).text, /No additional certificates are trusted.*not fetched automatically/)

  // ── Which failures get a hint, and where it points ────────────────────────
  assert.equal(hints.connectionHint('tls_trust').kind, 'trust')
  assert.equal(hints.connectionHint('tls_trust').link.to, '/settings/general?tab=security#probe-trust')
  assert.equal(hints.connectionHint('dns').kind, 'resolution')
  assert.equal(hints.connectionHint('dns').link.to, '/settings/general?tab=monitoring#service-check-connections')
  assert.equal(hints.connectionHint('tls_name').kind, 'name')
  // Stored results carry only the poller's message.
  assert.equal(hints.connectionHint(null, 'request failed: Get "https://portal.example.local": x509: certificate signed by unknown authority').kind, 'trust')
  assert.equal(hints.connectionHint(null, 'tcp connect failed: dial tcp: lookup portal.example.local: no such host').kind, 'resolution')
  assert.equal(hints.connectionHint(null, 'x509: certificate is valid for a.test, not b.test').kind, 'name')
  assert.equal(hints.connectionHint('tls', 'TLS error: x509: certificate signed by unknown authority').kind, 'trust')
  for (const [diagnosis, error] of [['timeout', ''], ['connection_refused', ''], ['http_status', 'expected status 200, got 503'], ['tls', 'certificate has expired'], [null, ''], [null, 'connection refused']]) {
    assert.equal(hints.connectionHint(diagnosis, error), null, `${diagnosis} ${error}`)
  }
  // A DNS check tests resolution itself and never uses a manual address: no hint that points there.
  assert.equal(hints.connectionHint('dns', 'The hostname could not be resolved', 'dns'), null)
  assert.equal(hints.connectionHint(null, 'lookup intranet.example.local: no such host', 'dns'), null)
  assert.equal(hints.connectionHint(null, 'lookup intranet.example.local: no such host', 'tcp').kind, 'resolution')
  assert.equal(hints.manualAddressNote([]), null)
  assert.match(hints.manualAddressNote([{ host: 'portal.example.local', address: '10.1.2.3' }]), /manual address 10\.1\.2\.3 for portal\.example\.local\. DNS was not used/)
  assert.deepEqual(hints.resultManualAddress({ override_host: 'a.test', override_address: '10.0.0.1' }), { host: 'a.test', address: '10.0.0.1' })
  assert.equal(hints.resultManualAddress({ remote_ip: '10.0.0.1' }), null)

  // ── The section for an administrator ──────────────────────────────────────
  await mount(React.createElement(M.ServiceCheckConnectionsCard), { at: '/settings/general?tab=monitoring#service-check-connections' })
  let t = text()
  assert(t.includes('Service check connections') && t.includes('Manual host addresses') && t.includes('Trusted certificates'))
  assert(scrolled.includes('service-check-connections'), 'the address with the anchor scrolls to the section')
  assert(t.includes('DNS checks never do'), 'says DNS checks are not affected')
  assert(t.includes('sends its user name, password or token to the address'), 'says what the setting can expose')
  const rows = [...document.querySelectorAll('tbody tr')].map((row) => row.textContent.replace(/\s+/g, ' '))
  assert.equal(rows.length, 4)
  assert(rows[0].includes('portal.example.local') && rows[0].includes('10.1.2.3') && rows[0].includes('Everywhere') && rows[0].includes('Behind the WAF'))
  assert(rows[0].includes('Not applied by Branch A'), 'a sensor too old to apply the row is named on it')
  assert(rows[0].includes('by admin'), 'who changed it')
  assert(rows[1].includes('2001:db8::10') && rows[1].includes('Sensor Branch B') && !rows[1].includes('cannot use manual addresses'))
  assert(rows[2].includes('A sensor that was removed') && rows[2].includes('applies nowhere'))
  assert(rows[3].includes('This appliance only'))
  assert(t.includes('4 of 200 addresses'))
  assert(t.includes('1 certificate authority and 1 self-signed service certificate trusted') && t.includes('1 has expired'))
  const manage = [...document.querySelectorAll('a')].find((a) => a.textContent.includes('Manage trusted certificates'))
  assert.equal(manage?.getAttribute('href'), '/settings/general?tab=security#probe-trust')
  assert(!/system_settings|host_overrides|probe_trust/.test(t), 'no setting keys on screen')
  // Type scale: rem classes only (docs/design-system.md).
  assert(!/text-\[\d+px\]/.test(document.body.innerHTML), 'no pixel font sizes')

  // Add: errors appear on their fields only after an attempt, and nothing is sent while they stand.
  await act(async () => button('Add address').click())
  await settle()
  assert(text().includes('Add a manual address'))
  assert(!text().includes('Enter the host name the checks use'), 'no errors before the form is used')
  await act(async () => button('Add address').click()) // the header button is behind the dialog; the submit button shares the label
  const submit = () => [...document.querySelectorAll('[role="dialog"] button')].find((b) => b.getAttribute('type') === 'submit')
  await act(async () => submit().click())
  await settle()
  t = text()
  assert(t.includes('Enter the host name the checks use') && t.includes('Enter the IPv4 or IPv6 address to connect to'))
  assert.equal(calls.filter((c) => c.verb === 'post').length, 0)
  await typeInto(field('portal.example.local'), 'portal.example.local')
  await typeInto(field('10.20.30.40'), '10.1.2.3:443')
  t = text()
  assert(t.includes('This name already has an address for that place') && t.includes('without a port, range or name'))
  assert.equal(field('portal.example.local').getAttribute('aria-invalid'), 'true')
  await act(async () => submit().click())
  assert.equal(calls.filter((c) => c.verb === 'post').length, 0)

  // A refusal from the server lands on the field it names.
  await typeInto(field('portal.example.local'), 'Intranet.Example.Local')
  await typeInto(field('10.20.30.40'), '10.7.7.7')
  refuse = { status: 422, detail: { message: 'This address cannot be connected to', field: 'address' } }
  await act(async () => submit().click())
  await settle()
  assert(text().includes('This address cannot be connected to'))
  assert(text().includes('Add a manual address'), 'the dialog stays open')
  refuse = null
  await typeInto(field('10.20.30.40'), '10.7.7.8')
  assert(!text().includes('This address cannot be connected to'), 'editing clears the server message')
  await typeInto(field("Internal name, not in the appliance's DNS"), 'Not in DNS')
  await act(async () => submit().click())
  await settle()
  const posted = calls.filter((c) => c.verb === 'post').at(-1)
  assert.deepEqual(posted.body, { host: 'Intranet.Example.Local', address: '10.7.7.8', scope: 'all', sensor_id: null, note: 'Not in DNS' })
  assert.equal(posted.url, '/system/monitoring/host-overrides')
  assert(!text().includes('Add a manual address'), 'the dialog closes after saving')
  assert(text().includes('intranet.example.local') && text().includes('5 of 200 addresses'), 'the table shows what the server returned')

  // Edit: a row for a sensor that cannot apply it says so in the dialog.
  doc = { ...DOC(), entries: [entry({ id: 'e5', host: 'old.example.local', scope: 'sensor', sensor_id: SENSOR_OLD, sensor_name: 'Branch A', not_applied_by: ['Branch A'] })] }
  await mount(React.createElement(M.ServiceCheckConnectionsCard))
  assert(text().includes('Branch A runs a version that cannot use manual addresses: it still looks the name up itself'))
  await act(async () => button('Edit the address for old.example.local').click())
  await settle()
  assert(text().includes('Change manual address'))
  assert.equal(field('portal.example.local').value, 'old.example.local')
  assert(text().includes('takes effect once the sensor is updated'))
  await typeInto(field('10.20.30.40'), '10.1.2.50')
  await act(async () => submit().click())
  await settle()
  const put = calls.find((c) => c.verb === 'put')
  assert.equal(put.url, '/system/monitoring/host-overrides/e5')
  assert.deepEqual(put.body, { host: 'old.example.local', address: '10.1.2.50', scope: 'sensor', sensor_id: SENSOR_OLD, note: 'Behind the WAF' })

  // Remove asks first and says what happens to the checks.
  await act(async () => button('Remove the address for old.example.local').click())
  await settle()
  assert(text().includes('Remove this manual address?') && text().includes('look the name up in DNS again'))
  assert.equal(calls.filter((c) => c.verb === 'delete').length, 0)
  await act(async () => button('Remove address').click())
  await settle()
  assert.equal(calls.find((c) => c.verb === 'delete').url, '/system/monitoring/host-overrides/e5')
  assert(text().includes('No manual addresses. Every check resolves its host name'), 'the empty state says what happens instead')

  // ── An account that manages settings but is not an administrator ──────────
  calls.length = 0
  await mount(React.createElement(M.ServiceCheckConnectionsCard), { user: SETTINGS_ONLY })
  t = text()
  assert(t.includes('Service check connections') && t.includes('managed by an administrator'))
  assert(!button('Add address') && !document.querySelector('table'), 'nothing to edit')
  assert.equal(calls.length, 0, 'the administrator-only API is not called')

  // ── The link back from the trust card ─────────────────────────────────────
  scrolled.length = 0
  await mount(React.createElement(M.ProbeTrustCard), { at: '/settings/general?tab=security#probe-trust' })
  const back = [...document.querySelectorAll('a')].find((a) => a.textContent.includes('Service check connections'))
  assert.equal(back?.getAttribute('href'), '/settings/general?tab=monitoring#service-check-connections')
  assert(scrolled.includes('probe-trust'), 'a link to the trust card scrolls to it')
  assert(text().includes('set a manual host address'))

  // ── Hints under a failed test ─────────────────────────────────────────────
  await mount(React.createElement(M.ConnectionHints, { failed: true, diagnosis: 'tls_trust', error: 'x', newTab: true }))
  t = text()
  assert(t.includes('The certificate is not trusted by this appliance') && t.includes('Turning off certificate verification on the check hides the problem'))
  let link = document.querySelector('a')
  assert.equal(link.getAttribute('href'), '/settings/general?tab=security#probe-trust')
  assert.equal(link.getAttribute('target'), '_blank', 'an unsaved form stays open')
  await mount(React.createElement(M.ConnectionHints, { failed: true, diagnosis: 'dns', error: 'The hostname could not be resolved' }))
  assert(text().includes('The host name could not be resolved') && text().includes('give it a manual address'))
  link = document.querySelector('a')
  assert.equal(link.getAttribute('href'), '/settings/general?tab=monitoring#service-check-connections')
  assert.equal(link.getAttribute('target'), null)
  // Someone who cannot change it is told what to ask for, without a link that would be refused.
  await mount(React.createElement(M.ConnectionHints, { failed: true, diagnosis: 'dns' }), { user: SETTINGS_ONLY })
  assert(!document.querySelector('a') && text().includes('An administrator changes this under Settings > General > Manual host addresses (Monitoring)'))
  // A role limited to selected pages without the tab gets no link either.
  await mount(React.createElement(M.ConnectionHints, { failed: true, diagnosis: 'tls_trust' }), { user: { ...ADMIN, page_access: { pages: ['/settings/general?tab=monitoring'] } } })
  assert(!document.querySelector('a'))
  // A passed test through a manual address says DNS was not used; other results say nothing.
  await mount(React.createElement(M.ConnectionHints, { failed: false, manualAddresses: [{ host: 'portal.example.local', address: '10.1.2.3' }] }))
  assert(text().includes('Connected to the manual address 10.1.2.3 for portal.example.local. DNS was not used'))
  await mount(React.createElement(M.ConnectionHints, { failed: true, diagnosis: 'timeout', error: 'timed out' }))
  assert.equal(document.getElementById('root').textContent, '')
  await mount(React.createElement(M.ConnectionHints, { failed: true, diagnosis: 'dns', checkType: 'dns', error: 'no such host' }))
  assert.equal(document.getElementById('root').textContent, '', 'a failed DNS check is not sent to manual addresses')
  await mount(React.createElement(M.ConnectionHints, { failed: false, diagnosis: 'dns' }))
  assert.equal(document.getElementById('root').textContent, '')

  console.log('Service check connections passed: form rules, table and warnings, add/edit/remove, non-administrator view, links both ways, and test hints.')
} finally {
  await act(async () => root.unmount())
  client?.clear(); dom.window.close(); await rm(temporary, { recursive: true, force: true })
}
