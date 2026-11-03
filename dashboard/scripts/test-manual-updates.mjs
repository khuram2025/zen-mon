import assert from 'node:assert/strict'
import { build } from 'esbuild'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { JSDOM } from 'jsdom'
import React, { act } from 'react'

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true })
for (const name of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'Node', 'NodeFilter', 'CustomEvent', 'MutationObserver']) globalThis[name] = dom.window[name]
Object.assign(globalThis, { getComputedStyle: dom.window.getComputedStyle.bind(dom.window), requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window), cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window), IS_REACT_ACT_ENVIRONMENT: true })
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
const { createRoot } = await import('react-dom/client')
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const cache = path.resolve('node_modules/.cache')
await mkdir(cache, { recursive: true })
const temporary = await mkdtemp(path.join(cache, 'manual-updates-'))
const calls = []
const status = { current_version: '1.24.45', appliance_id: 'fixture', auto_update: false, check_interval_hours: 4, history: [], recent_log: [], updater_running: false, active_update: null }
let offered = { from_version: '1.24.45', checked_at: '2026-11-03T12:00:00Z', plan_id: 'a'.repeat(64), steps: [
  { id: 'bridge', version: '1.24.46', changelog: 'Required transition' },
  { id: 'latest', version: '1.24.48', min_version: '1.24.46', changelog: 'Cumulative release' },
] }
let failure = false
const notices = []
globalThis.__updatesFixture = {
  get: async () => ({ data: status }),
  post: async (url, body) => {
    calls.push({ url, body })
    if (failure) throw new Error('Catalog unavailable')
    return { data: url === '/system/check-update' ? offered : { status: 'queued' } }
  },
}
globalThis.__updatesToast = { success: (...args) => notices.push(args), error: (...args) => notices.push(args) }
const output = await build({ entryPoints: ['src/components/UpdatesTabContent.tsx'], bundle: true, write: false, format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent', plugins: [{ name: 'fixtures', setup(b) {
  b.onResolve({ filter: /^(react(?:\/.*)?|react-dom(?:\/.*)?|@tanstack\/react-query)$/ }, arg => ({ path: import.meta.resolve(arg.path), external: true }))
  b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'fixture' }))
  b.onResolve({ filter: /^@\/components\/ui\/Toast$/ }, () => ({ path: 'toast', namespace: 'fixture' }))
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, arg => ({ contents: arg.path === 'api' ? 'export const api=globalThis.__updatesFixture' : 'export const toast=globalThis.__updatesToast', loader: 'js' }))
  b.onResolve({ filter: /^@\// }, arg => b.resolve(`./${arg.path.slice(2)}`, { resolveDir: path.resolve('src'), kind: arg.kind }))
} }] })
const file = path.join(temporary, 'component.mjs')
await writeFile(file, output.outputFiles[0].text)
const { UpdatesTabContent } = await import(pathToFileURL(file).href)
const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { gcTime: Infinity } } })
const root = createRoot(document.getElementById('root'))
const button = label => [...document.querySelectorAll('button')].find(e => e.textContent.trim() === label)
const settle = async () => act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
async function click(label) { assert(button(label), label); await act(async () => button(label).click()); await settle() }
try {
  await act(async () => root.render(React.createElement(QueryClientProvider, { client }, React.createElement(UpdatesTabContent))))
  await settle()
  await click('Check for Updates')
  assert.deepEqual(calls.map(c => c.url), ['/system/check-update'])
  assert(document.body.textContent.includes('Required intermediate version'))
  assert(document.body.textContent.includes('Automatic installation is off'))
  assert.equal(document.getElementById('update-target').value, '1.24.48')
  await click('Review installation')
  assert(document.querySelector('[role=dialog]').textContent.includes('v1.24.45 → v1.24.46 → v1.24.48'))
  await click('Cancel')
  assert.equal(calls.length, 1, 'Review and cancel must never install')
  await act(async () => {
    const select = document.getElementById('update-target')
    select.value = '1.24.46'; select.dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  })
  await click('Review installation')
  assert(!document.querySelector('[role=dialog]').textContent.includes('v1.24.48'))
  await click('Install now')
  assert.deepEqual(calls.at(-1), { url: '/system/install-update', body: { plan_id: 'a'.repeat(64), target_version: '1.24.46' } })
  assert(!button('Review installation'))
  offered = { ...offered, steps: [] }
  await click('Check for Updates')
  assert(document.body.textContent.includes('No eligible updates available'))
  assert(!button('Review installation'))
  failure = true
  await click('Check for Updates')
  assert(!document.body.textContent.includes('No eligible updates available'), 'Failed check must clear stale success')
  assert.equal(notices.at(-1)[0], 'Check failed')
  assert.equal(calls.filter(c => c.url === '/system/install-update').length, 1)
  console.log('Passed: read-only check, required path, review/cancel, selected intermediate install, empty catalog and failed check')
} finally {
  await act(async () => root.unmount())
  client.clear(); dom.window.close(); await rm(temporary, { recursive: true, force: true })
}
