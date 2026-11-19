// The Alert Manager in a browser (jsdom): the rule list describes every family in words with its timers,
// routing ("No channel — notifies nobody") and last-fired time and filters by family; the Recommended tab
// lists available / covered / accepted rules and Accept posts enabled, channels and policy; the wizard opens a
// shipped "gt" Packet Loss rule with ">" selected and the Degraded explanation, offers the new families with
// their own inputs, and saves an appliance rule without device scope.
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
dom.window.HTMLElement.prototype.scrollIntoView = () => {}
dom.window.HTMLElement.prototype.hasPointerCapture = () => false
dom.window.HTMLElement.prototype.releasePointerCapture = () => {}
globalThis.ResizeObserver = dom.window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
const { createRoot } = await import('react-dom/client')
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const { MemoryRouter } = await import('react-router-dom')

const NOW = Date.now()
const ago = (min) => new Date(NOW - min * 60_000).toISOString()
const RULES = [
  { id: 'r-disk', name: 'Appliance disk high', enabled: true, metric: 'appliance_disk_pct', operator: '>', threshold: 85, severity: 'warning', priority: 'P3',
    min_duration: 600, clear_duration: null, recovery_alert: true, notify_channels: [], escalation_levels: [], family: 'appliance', last_triggered_at: ago(30) },
  { id: 'r-loss', name: 'Packet Loss', enabled: true, metric: 'packet_loss', operator: 'gt', threshold: 5, trigger_on: 'any', severity: 'warning',
    min_duration: 300, clear_duration: null, recovery_alert: true, notify_channels: ['ch-1'], escalation_levels: [], family: 'device', last_triggered_at: null },
  { id: 'r-util', name: 'Uplink busy', enabled: true, metric: 'if_util_pct', operator: '>', threshold: 80, severity: 'warning', min_duration: 600, clear_duration: 900,
    recovery_alert: true, notify_channels: ['ch-1'], escalation_levels: [], family: 'device', last_triggered_at: null },
  { id: 'r-flap', name: 'Interface flapping', enabled: true, metric: 'if_flaps', operator: '>=', threshold: 5, severity: 'warning', min_duration: 900,
    clear_duration: null, recovery_alert: true, notify_channels: ['ch-1'], escalation_levels: [], family: 'device', last_triggered_at: null },
  { id: 'r-trap', name: 'Cold start', enabled: true, metric: 'trap', operator: '==', threshold: 1, trap_oid: '1.3.6.1.6.3.1.1.5.1', severity: 'warning',
    min_duration: 0, recovery_alert: false, notify_channels: ['ch-1'], escalation_levels: [], family: 'device', last_triggered_at: null },
  { id: 'r-tun', name: 'IPsec Tunnels: Status is Down', enabled: true, metric: 'tpl_fgt_tun_status', operator: '==', threshold: 1, severity: 'warning',
    conditions: [{ metric: 'tpl_fgt_tun_status', operator: '==', threshold: 1, reset_threshold: null }], profile_id: 'tpl-1', pack_key: 'fgt_tun_status:state',
    min_duration: 120, clear_duration: null, recovery_alert: true, notify_channels: ['ch-1'], escalation_levels: [], family: 'device', last_triggered_at: null },
]
const TEMPLATES = [{ id: 'tpl-1', name: 'FortiGate', oid_groups: [
  { key: 'fgt_tunnels', name: 'IPsec Tunnels', kind: 'table', metrics: [{ key: 'fgt_tun_status', name: 'Status', type: 'enum',
    labels: { 1: { text: 'Down', sev: 'crit' }, 2: { text: 'Up', sev: 'ok' }, 3: { text: 'Negotiating', sev: 'warn' } } }] },
] }]
const RECOMMENDED = [
  { key: 'service_down', family: 'service', name: 'Service check down', description: 'Any service check confirmed down.', metric: 'service_status', operator: '==',
    threshold: 0, trigger_on: 'down', min_duration: 120, clear_duration: null, severity: 'critical', priority: 'P2', recovery_alert: true, cooldown: 0,
    status: 'covered', rule: null, covered_by: { id: 'r-svc', name: 'Service Down' } },
  { key: 'certificate_expiring', family: 'service', name: 'Certificate expiring', description: 'A certificate expires within 30 days.', metric: 'service_tls_days',
    operator: '<', threshold: 30, trigger_on: 'any', min_duration: 0, clear_duration: null, severity: 'warning', priority: 'P3', recovery_alert: true, cooldown: 0,
    status: 'available', rule: null, covered_by: null },
  { key: 'device_cpu_critical', family: 'device', name: 'Critical device CPU (SNMP)', description: 'CPU above 95%.', metric: 'cpu', operator: '>', threshold: 95,
    trigger_on: 'any', min_duration: 300, clear_duration: 300, severity: 'critical', priority: 'P2', recovery_alert: true, cooldown: 300,
    status: 'accepted', rule: { id: 'r-util', name: 'Critical device CPU (SNMP)', enabled: false }, covered_by: null },
]
const CHANNELS = [{ id: 'ch-1', name: 'NOC mail', type: 'email' }, { id: 'ch-2', name: 'On-call SMS', type: 'sms' }]
const writes = []
globalThis.__rulesFixture = {
  get: async (url) => {
    const u = String(url)
    if (u === '/alert-rules') return { data: { data: RULES } }
    if (u === '/alert-rules/recommended') return { data: { items: RECOMMENDED } }
    if (u === '/alert-rules/template-defaults') return { data: {
      device: { email_subject: 'S', email_body: '{event_sentence}', sms_template: 'x', recovery_email_subject: 'R', recovery_email_body: 'b', recovery_sms_template: 'y' },
      variables: { device: ['event_sentence', 'site', 'group', 'site_suffix', 'hostname', 'rule_name'], service: ['error', 'event_sentence'], trap: [], syslog: [] },
      extras: {},
    } }
    if (u === '/settings/channels') return { data: CHANNELS }
    if (u === '/snmp/profiles') return { data: TEMPLATES }
    if (u === '/escalation-policies' || u.startsWith('/escalation')) return { data: { items: [] } }
    if (u === '/alerts/event-expiry') return { data: { enabled: true, hours: 168 } }
    if (u.startsWith('/devices') || u.startsWith('/service-checks') || u.startsWith('/servers')) return { data: { data: [], items: [] } }
    return { data: [] }
  },
  post: async (url, body) => { writes.push({ verb: 'post', url, body }); return { data: { id: 'new', name: body?.name || 'rule' } } },
  put: async (url, body) => { writes.push({ verb: 'put', url, body }); return { data: body } },
  delete: async () => ({ data: null }),
}
const plugins = [{ name: 'fixtures', setup(b) {
  b.onResolve({ filter: /^(react(?:\/.*)?|react-dom(?:\/.*)?|@tanstack\/react-query|react-router-dom)$/ }, (arg) => ({ path: import.meta.resolve(arg.path), external: true }))
  b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'fixture' }))
  b.onResolve({ filter: /^@\/components\/ui\/Toast$/ }, () => ({ path: 'toast', namespace: 'fixture' }))
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, (arg) => ({
    contents: arg.path === 'api'
      ? 'export const api=globalThis.__rulesFixture;export function setApiToken(){};export function getApiToken(){return null}'
      : 'export const toast={success(){},error(...a){globalThis.__toastErrors=(globalThis.__toastErrors||[]).concat([a])}};export function Toaster(){return null}',
    loader: 'js',
  }))
  b.onResolve({ filter: /^@\// }, (arg) => b.resolve(`./${arg.path.slice(2)}`, { resolveDir: path.resolve('src'), kind: arg.kind }))
} }]
const cache = path.resolve('node_modules/.cache')
await mkdir(cache, { recursive: true })
const temporary = await mkdtemp(path.join(cache, 'alert-rules-ui-'))
const out = await build({
  stdin: { contents: `export { AlertRulesPage } from '@/pages/AlertRulesPage'; export { AlertRuleWizardDialog } from '@/components/forms/AlertRuleWizardDialog'; export { useAuth } from '@/stores/auth'`,
    resolveDir: process.cwd(), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent', plugins,
  loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty', '.woff2': 'empty' },
  define: { 'process.env.NODE_ENV': '"production"' },
})
const file = path.join(temporary, 'ui.mjs')
await writeFile(file, out.outputFiles[0].text)
const M = await import(pathToFileURL(file).href)
M.useAuth.setState({ user: { id: 'me', username: 'me', role: 'custom', permissions: ['alerts.view', 'alerts.manage'] }, token: 't' })

const root = createRoot(document.getElementById('root'))
const settle = async () => act(async () => { await new Promise((r) => setTimeout(r, 30)) })
const text = () => document.body.textContent.replace(/\s+/g, ' ')
const button = (label) => [...document.querySelectorAll('button')].find((e) => e.textContent.trim() === label || e.textContent.includes(label))
let client
async function mount(el) {
  await act(async () => root.render(null))
  client?.clear()
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { gcTime: Infinity } } })
  await act(async () => root.render(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, null, el))))
  await settle(); await settle()
}

try {
  // ── The rule list ─────────────────────────────────────────────────────────
  await mount(React.createElement(M.AlertRulesPage))
  let t = text()
  assert(t.includes('Data disk used > 85%'), 'appliance trigger in words')
  assert(t.includes('Packet loss > 5% (while Degraded)'), 'a stored "gt" reads as ">" and says it fires on Degraded')
  assert(t.includes('Interface utilisation > 80%'))
  assert(t.includes('Interface state changes ≥ 5 changes in 15 min'))
  assert(!/appliance_disk_pct|if_util_pct|uptime_reset|if_flaps/.test(t), 'no raw metric keys')
  assert(t.includes('hold 10 min · clear 5 min (default)'), 'timers of a metric rule')
  assert(t.includes('window 15 min'), 'the flapping hold is a window')
  assert(t.includes('Expires after 7 days without a new event'), 'trap alerts expire')
  assert(t.includes('No channel — notifies nobody'), 'a rule with no channel and no route is flagged')
  assert(t.includes('NOC mail'), 'channels are named')
  assert(t.includes('Never'), 'a rule that never fired')
  assert(/30 min(utes)? ago|30m ago/.test(t), `last fired: ${t.match(/Last fired.{0,80}/)?.[0]}`)
  assert(t.includes('IPsec Tunnels: Status is Down') && t.includes('Template state'), 'a template state rule reads as its states')
  assert(t.includes('From a template recommendation'))

  // ── The Recommended tab ───────────────────────────────────────────────────
  await act(async () => button('Recommended').click())
  await settle()
  t = text()
  assert(t.includes('Certificate expiring') && t.includes('Certificate expiry < 30 days'), 'recommendation condition in words')
  assert(t.includes('covered') && t.includes('Service Down'), 'covered names the covering rule')
  assert(t.includes('accepted') && t.includes('(off)'), 'accepted names its rule and whether it is off')
  const rowText = [...document.querySelectorAll('tbody tr')].map((r) => r.textContent)
  assert(rowText[0].includes('Certificate expiring'), 'available recommendations first')
  await act(async () => button('Accept').click())
  await settle()
  assert(text().includes('Accept “Certificate expiring”'), 'the accept dialog opens')
  await act(async () => [...document.querySelectorAll('button')].find((b) => b.getAttribute('aria-pressed') === 'false' && b.textContent.includes('On-call SMS')).click())
  await act(async () => button('Create rule').click())
  await settle()
  const accept = writes.find((w) => w.url === '/alert-rules/recommended/certificate_expiring/accept')
  assert(accept, 'Accept posts to the recommendation')
  assert.deepEqual(accept.body, { enabled: true, notify_channels: ['ch-2'], escalation_policy_id: null })

  // ── The wizard: the shipped Packet Loss rule ──────────────────────────────
  await mount(React.createElement(M.AlertRuleWizardDialog, { open: true, onOpenChange() {}, rule: RULES[1] }))
  await act(async () => button('Trigger').click())
  await settle()
  t = text()
  const op = document.querySelector('[aria-label="Operator of condition 1"]')
  assert(op && op.textContent.trim() === '>', `the stored "gt" shows as ">" (got "${op?.textContent}")`)
  assert(t.includes('Fires only when the device’s confirmed status is Degraded'), 'latency/loss rules explain Degraded')
  for (const label of ['Monitoring gap', 'Virtualisation', 'Device Tracker', 'NetPath', 'ZenPlus appliance']) assert(button(label), `the wizard offers ${label}`)

  // ── A template state rule: the state picker ───────────────────────────────
  await mount(React.createElement(M.AlertRuleWizardDialog, { open: true, onOpenChange() {}, rule: RULES.find((r) => r.id === 'r-tun') }))
  await act(async () => button('Trigger').click())
  await settle()
  const states = [...document.querySelectorAll('[aria-label="States of Status that alert"] button')]
  assert.deepEqual(states.map((b) => [b.textContent.replace(/ ·\w+$/, ''), b.getAttribute('aria-pressed')]), [['Down', 'true'], ['Up', 'false'], ['Negotiating', 'false']])
  assert(text().includes('Alerts when a row reports Down'))
  await act(async () => states[2].click())
  assert(text().includes('Alerts when a row reports not Up'), 'Down + Negotiating = everything but Up')
  await act(async () => button('Review').click())
  assert(text().includes('IPsec Tunnels: Status is not Up'), 'review says the states in words')
  writes.length = 0
  await act(async () => [...document.querySelectorAll('button[type="submit"]')].find((b) => b.textContent.includes('Save alert')).click())
  await settle()
  const tun = writes.find((w) => w.url === '/alert-rules/r-tun' && w.verb === 'put')
  assert(tun, `the state rule saves (${JSON.stringify(globalThis.__toastErrors || [])})`)
  // One condition travels in the flat columns (conditions null), as for every single-condition device rule.
  assert.deepEqual([tun.body.metric, tun.body.operator, tun.body.threshold, tun.body.conditions], ['tpl_fgt_tun_status', '!=', 2, null])

  // ── A new appliance rule ──────────────────────────────────────────────────
  await mount(React.createElement(M.AlertRuleWizardDialog, { open: true, onOpenChange() {} }))
  const nameInput = document.querySelector('input[placeholder="Email me when a core router goes down"]')
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set
  await act(async () => { setter.call(nameInput, 'Disk'); nameInput.dispatchEvent(new dom.window.Event('input', { bubbles: true })) })
  await act(async () => button('Trigger').click())
  await act(async () => button('ZenPlus appliance').click())
  await settle()
  t = text()
  assert(t.includes('Clear level (optional)') && t.includes('2 points on the healthy side'), 'appliance disk: clear level with its automatic default')
  assert(t.includes('Condition must exist for') && t.includes('Clear after'), 'appliance rules honour hold and clear')
  await act(async () => button('Scope').click())
  assert(text().includes('Appliance rules watch ZenPlus itself'), 'no device scope for appliance rules')
  await act(async () => button('Trigger Actions').click())
  t = text()
  assert(t.includes('No channel selected'), 'no channel warning')
  assert(t.includes('{site}') && t.includes('{group}') && !t.includes('{error}'), 'placeholders follow the server list for the kind')
  await act(async () => button('Review').click())
  await settle()
  assert(text().includes('Data disk used > 85%'), 'review trigger in words')
  writes.length = 0
  await act(async () => [...document.querySelectorAll('button[type="submit"]')].find((b) => b.textContent.includes('Create alert')).click())
  await settle()
  const saved = writes.find((w) => w.url === '/alert-rules' && w.verb === 'post')
  assert(saved, `the rule is created (${JSON.stringify(globalThis.__toastErrors || [])})`)
  assert.deepEqual([saved.body.metric, saved.body.operator, saved.body.threshold, saved.body.trigger_on], ['appliance_disk_pct', '>', 85, 'any'])
  for (const k of ['device_id', 'group_id', 'device_type', 'location', 'scope_tag', 'target', 'profile_id', 'if_role']) assert.equal(saved.body[k], null, `no ${k}`)

  console.log('Alert rules UI: list words, timers, routing, last fired; Recommended tab with covered/accepted and Accept; wizard opens "gt" as ">", explains Degraded, offers the new families and saves an appliance rule without device scope.')
} finally {
  await act(async () => root.unmount())
  client?.clear(); dom.window.close(); await rm(temporary, { recursive: true, force: true })
}
