import assert from 'node:assert/strict'
import { build } from 'esbuild'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdir, mkdtemp, writeFile, readFile, rm } from 'node:fs/promises'
import { JSDOM } from 'jsdom'
import React, { act } from 'react'

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true })
for (const name of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'DocumentFragment', 'Node', 'NodeFilter', 'CustomEvent', 'MutationObserver']) globalThis[name] = dom.window[name]
Object.assign(globalThis, { getComputedStyle: dom.window.getComputedStyle.bind(dom.window), requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window), cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window), IS_REACT_ACT_ENVIRONMENT: true })
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
const { createRoot } = await import('react-dom/client')
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const cache = path.resolve('node_modules/.cache')
await mkdir(cache, { recursive: true })
const temporary = await mkdtemp(path.join(cache, 'apm-event-rules-'))
const configured = [{ id: 'email-1', name: 'NOC email', type: 'email', enabled: true }, { id: 'sms-1', name: 'NOC SMS', type: 'sms', enabled: true }]
let mode = 'wrapped', writes = [], reads = [], resolveRead
globalThis.__apmFixture = {
  get: async url => {
    reads.push(url)
    assert.equal(url, '/settings/channels')
    if (mode === 'error') throw new Error('Channel service unavailable')
    if (mode === 'loading') await new Promise(resolve => { resolveRead = resolve })
    return { data: mode === 'empty' ? { data: [] } : mode === 'array' ? configured : { data: configured } }
  },
  put: async (url, body) => { writes.push({ url, body }); return { data: body } },
}
const plugins = [{ name: 'fixtures', setup(b) {
  b.onResolve({ filter: /^(react(?:\/.*)?|react-dom(?:\/.*)?|@tanstack\/react-query)$/ }, arg => ({ path: import.meta.resolve(arg.path), external: true }))
  b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'fixture' }))
  b.onResolve({ filter: /^@\/components\/ui\/Toast$/ }, () => ({ path: 'toast', namespace: 'fixture' }))
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, arg => ({ contents: arg.path === 'api' ? 'export const api=globalThis.__apmFixture;export function setApiToken(){};export function getApiToken(){return null}' : 'export const toast={success(){},error(){}}', loader: 'js' }))
  b.onResolve({ filter: /^@\// }, arg => b.resolve(`./${arg.path.slice(2)}`, { resolveDir: path.resolve('src'), kind: arg.kind }))
} }]
async function compile(options, name) {
  const out = await build({ ...options, bundle: true, write: false, format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent', plugins })
  const file = path.join(temporary, name + '.mjs')
  await writeFile(file, out.outputFiles[0].text)
  return import(pathToFileURL(file).href)
}
const { ScenarioEditor } = await compile({ entryPoints: ['src/pages/apm/SyntheticsPage.tsx'] }, 'editor')
const source = await readFile('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
const W = await compile({ stdin: { loader: 'ts', resolveDir: process.cwd(), contents: `
import { defaultNetflowForm, formFromRule, isNetflowMetric, netflowPayload } from './src/components/forms/netflowAlertRule';
import { isApmEvent } from './src/components/forms/apmEventTriggers';
export * from './src/components/forms/apmEventTriggers';
const INTERFACE_METRICS = new Set(['if_util_pct','if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status']);
${source.slice(source.indexOf('const TEMPLATE_FIELDS ='), source.indexOf('export function AlertRuleWizardDialog('))}
export { ruleToState, stateToPayload };
` } }, 'wizard-contract')
const root = createRoot(document.getElementById('root'))
const button = label => [...document.querySelectorAll('button')].find(e => e.textContent.includes(label))
const settle = async () => act(async () => { await new Promise(resolve => setTimeout(resolve, 40)) })
let client
async function mount(cacheData) {
  await act(async () => root.render(null))
  client?.clear()
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { gcTime: Infinity } } })
  if (cacheData) client.setQueryData(['settings', 'channels'], cacheData)
  const monitor = { id: 'scenario-a', name: 'Checkout', retry_count: 2, enabled: true, notify_channels: [], steps: [{ name: 'Home', method: 'GET', url: 'https://example.invalid' }] }
  await act(async () => root.render(React.createElement(QueryClientProvider, { client }, React.createElement(ScenarioEditor, { monitor, onClose() {} }))))
  await settle()
}
try {
  // Cold entry uses the actual wrapped endpoint response, without visiting Channels.
  await mount()
  assert(button('NOC email') && button('NOC SMS'))
  assert.deepEqual(reads, ['/settings/channels'])
  await act(async () => button('NOC SMS').click())
  assert.equal(button('NOC SMS').getAttribute('aria-pressed'), 'true')
  await act(async () => button('Save changes').click())
  await settle()
  assert.deepEqual(writes[0].body.notify_channels, ['sms-1'])
  reads = []
  await mount(configured)
  assert(button('NOC email') && button('NOC SMS'))
  assert.deepEqual(reads, [], 'Warm normalized cache renders the same selector')
  mode = 'array'; await mount(); assert(button('NOC SMS'))
  mode = 'empty'; await mount(); assert(document.body.textContent.includes('No notification channels'))
  mode = 'error'; await mount(); assert(button('Retry'))
  mode = 'wrapped'; await act(async () => button('Retry').click()); await settle(); assert(button('NOC SMS'))
  mode = 'loading'; await mount(); assert(document.body.textContent.includes('Loading notification channels'))
  await act(async () => resolveRead()); await settle(); assert(button('NOC SMS'))

  for (const [metric, all] of [['apm_synthetic_down', 'All scenarios'], ['apm_slo_burn', 'All SLOs']]) {
    assert.equal(W.apmEventScope(metric).all, all)
    for (const target of [null, '125e1744-c1a6-4c57-9960-ec8f82001b50']) {
      const state = W.ruleToState({ name: 'Central APM', metric, operator: '==', threshold: 1, target, recovery_alert: true, notify_channels: ['sms-1'] })
      assert.equal(state.source, 'apm')
      const payload = W.stateToPayload(state)
      assert.deepEqual([payload.metric, payload.target, payload.operator, payload.threshold, payload.min_duration, payload.clear_duration], [metric, target, '==', 1, 0, null])
      assert.deepEqual(payload.notify_channels, ['sms-1'])
      assert.equal(payload.recovery_alert, true)
      assert.equal(payload.device_id, null)
    }
  }
  assert.notEqual(W.apmScopeKind('apm_synthetic_down'), W.apmScopeKind('apm_p95_ms'))
  console.log('Passed: real scenario editor cold/warm cache, selection/save, loading/empty/error/retry; event rule payloads and all/specific scopes.')
} finally {
  await act(async () => root.unmount())
  client?.clear(); dom.window.close(); await rm(temporary, { recursive: true, force: true })
}
