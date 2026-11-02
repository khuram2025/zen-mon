import assert from 'node:assert/strict'
import { build } from 'esbuild'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { JSDOM } from 'jsdom'
import React, { act } from 'react'

const cache = path.resolve('node_modules/.cache')
await mkdir(cache, { recursive: true })
const temporary = await mkdtemp(path.join(cache, 'baseline-details-'))
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true })
Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, MutationObserver: dom.window.MutationObserver, getComputedStyle: dom.window.getComputedStyle.bind(dom.window), requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window), cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window), IS_REACT_ACT_ENVIRONMENT: true })
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
const { createRoot } = await import('react-dom/client')
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const { MemoryRouter, Routes, Route, useLocation } = await import('react-router-dom')
const rules = [
  { id: 'r1', baseline_id: 'b', rule_type: 'required', package_match: 'Chrome', match_type: 'contains', min_version: '150', severity: 'critical', notes: 'Use the approved package', created_at: '2026-11-01T10:00:00Z' },
  { id: 'r2', baseline_id: 'b', rule_type: 'prohibited', package_match: 'Remote tool', match_type: 'exact', min_version: null, severity: 'warning', notes: null, created_at: '2026-11-01T10:00:00Z' },
]
const baseline = { id: 'b', name: 'Windows Core', description: 'Core software requirements', enabled: true, os_type: 'windows', site_id: null, site_name: null, match_tags: [], alerting: true, rules, rule_count: 2, servers_evaluated: 2, servers_compliant: 1, violations: 1, created_at: '2026-11-01T10:00:00Z', updated_at: '2026-11-01T10:00:00Z' }
const servers = [
  { server_id: 's2', server_name: 'server2', primary_ip: '192.168.8.2', compliance_status: 'violated', checked: 2, compliant: 1, violations: 1, critical: 1, not_evaluated: 0 },
  { server_id: 's3', server_name: 'server3', primary_ip: '192.168.8.3', compliance_status: 'not_evaluated', checked: 0, compliant: 0, violations: 0, critical: 0, not_evaluated: 2 },
  { server_id: 's10', server_name: 'server10', primary_ip: '192.168.8.10', compliance_status: 'compliant', checked: 2, compliant: 2, violations: 0, critical: 0, not_evaluated: 0 },
].map(row => ({ ...row, hostname: row.server_name, os_type: 'windows', server_status: 'healthy', last_seen: '2026-11-01T10:00:00Z', inventory_updated_at: '2026-11-01T10:00:00Z', missing: 0, outdated: row.violations, prohibited: 0, evaluated_at: row.checked ? '2026-11-01T10:00:00Z' : null, first_failed_at: row.violations ? '2026-10-30T10:00:00Z' : null }))
const calls = []
let writes = 0
let fail = false
let mockTotal = null
let pendingCoverage = null
globalThis.__baselineCanManage = true
globalThis.__baselineApi = {
  get: async (url, config) => {
    calls.push({ url, params: config?.params })
    if (fail) throw new Error('Report unavailable')
    if (url === '/server-baselines') return { data: { items: [baseline] } }
    if (url.endsWith('/checks')) return { data: { items: rules.map((rule, i) => ({ ...rule, rule_id: rule.id, status: i ? 'not_evaluated' : 'outdated', found_package: i ? null : 'Chrome Enterprise', found_version: i ? null : '149', expected: null, first_failed_at: i ? null : '2026-10-30T10:00:00Z', evaluated_at: i ? null : '2026-11-01T10:00:00Z' })) } }
    if (pendingCoverage) await pendingCoverage
    const p = config.params
    let items = servers.filter(row => (p.status === 'all' || p.status === row.compliance_status) && (!p.q || `${row.server_name} ${row.primary_ip}`.includes(p.q)))
    if (p.order === 'desc') items = [...items].reverse()
    return { data: { baseline, summary: { servers_in_scope: mockTotal || 3, compliant: 1, violated: 1, not_evaluated: (mockTotal || 3) - 2, evaluated: 2, violations: 1, last_evaluated: '2026-11-01T10:00:00Z' }, items, rule_summary: rules.map(rule => ({ rule_id: rule.id, compliant: 1, violated: 1, not_evaluated: (mockTotal || 3) - 2 })), total: mockTotal ?? items.length, page: p.page, page_size: p.page_size } }
  },
  post: () => { writes++; throw new Error('Viewing policy details must not issue writes') },
}
async function compile(entry) {
  const output = await build({ entryPoints: [entry], bundle: true, write: false, format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent', plugins: [{ name: 'test', setup(b) {
    b.onResolve({ filter: /^(react(?:\/.*)?|react-dom(?:\/.*)?|react-router-dom|@tanstack\/react-query)$/ }, arg => ({ path: import.meta.resolve(arg.path), external: true }))
    b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'mock' }))
    b.onResolve({ filter: /^@\/stores\/auth$/ }, () => ({ path: 'auth', namespace: 'mock' }))
    b.onLoad({ filter: /.*/, namespace: 'mock' }, arg => ({ contents: arg.path === 'api' ? 'export const api=globalThis.__baselineApi' : 'export const useCan=()=>()=>globalThis.__baselineCanManage', loader: 'js' }))
    b.onResolve({ filter: /^@\// }, arg => b.resolve(`./${arg.path.slice(2)}`, { resolveDir: path.resolve('src'), kind: arg.kind }))
  } }] })
  const file = path.join(temporary, path.basename(entry) + '.mjs')
  await writeFile(file, output.outputFiles[0].text)
  return import(pathToFileURL(file).href)
}
const { BaselineDetailPage } = await compile('src/pages/servers/BaselineDetailPage.tsx')
const { BaselinesPage } = await compile('src/pages/servers/BaselinesPage.tsx')
let client
const root = createRoot(document.getElementById('root'))
let counter = 0
function Location() { return React.createElement('output', { id: 'location' }, useLocation().pathname + useLocation().search) }
async function settle(ms = 30) { await act(async () => { await new Promise(resolve => setTimeout(resolve, ms)) }) }
async function eventually(predicate, message) {
  for (let i = 0; i < 50; i++) { await settle(20); if (predicate()) return }
  assert(predicate(), message)
}
async function render(url) {
  client?.clear()
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  await act(async () => root.render(React.createElement(QueryClientProvider, { client, key: ++counter }, React.createElement(MemoryRouter, { initialEntries: [url] }, React.createElement(Location), React.createElement(Routes, null,
    React.createElement(Route, { path: '/server-baselines', element: React.createElement(BaselinesPage) }),
    React.createElement(Route, { path: '/server-baselines/:baselineId', element: React.createElement(BaselineDetailPage) }))))))
  await settle()
}
const button = label => [...document.querySelectorAll('button')].find(element => element.textContent.trim() === label)
const click = async element => { assert(element, 'Expected control'); await act(async () => { element.dispatchEvent(new dom.window.MouseEvent('mousedown', { button: 0, bubbles: true })); element.click() }); await settle() }
function fill(input, value) {
  Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value)
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
}
try {
  await render('/server-baselines')
  const link = [...document.querySelectorAll('a')].find(element => element.textContent === 'Windows Core')
  assert.equal(link.getAttribute('href'), '/server-baselines/b')
  await click(link)
  assert.equal(document.querySelector('h1').textContent, 'Windows Core')
  assert.equal(calls.filter(call => call.url.endsWith('/checks')).length, 0, 'Rule evidence must be lazy loaded')
  await click(document.querySelector('button[aria-label="View checks for server2"]'))
  assert.equal(calls.filter(call => call.url.endsWith('/checks')).at(-1).url, '/server-baselines/b/servers/s2/checks')
  assert(document.body.textContent.includes('Chrome Enterprise'))
  assert(document.body.textContent.includes('version 150 or later'))
  assert(document.body.textContent.includes('No current result'))
  assert(document.body.textContent.includes('Use the approved package'))
  const activeQuery = client.getQueryCache().findAll({ queryKey: ['server-baselines', 'b', 'coverage'] }).at(-1)
  assert.equal([...activeQuery.observers][0].options.refetchInterval, false)
  const before = [...document.querySelectorAll('button[aria-label^="View checks for"]')].map(element => element.getAttribute('aria-label'))
  await act(async () => client.setQueryData(activeQuery.queryKey, previous => ({ ...previous, items: [...previous.items].reverse() })))
  assert.deepEqual([...document.querySelectorAll('button[aria-label^="View checks for"]')].map(element => element.getAttribute('aria-label')), before)
  await click(document.querySelector('button[aria-label="Sort by IP address"]'))
  assert.equal(calls.at(-1).params.sort, 'primary_ip')
  assert.equal(calls.at(-1).params.order, 'asc')
  await click(button('Compliant'))
  assert.equal(calls.at(-1).params.status, 'compliant')
  assert.equal(calls.at(-1).params.sort, 'primary_ip')
  assert.equal(document.querySelectorAll('button[aria-label^="View checks for"]').length, 1)
  await click(button('Policy rules (2)'))
  assert(document.querySelector('main')?.textContent.includes('Remote tool') || document.body.textContent.includes('Remote tool'))
  assert(document.body.textContent.includes('Must not be installed'))
  mockTotal = 100
  await render('/server-baselines/b?q=192.168.8.&sort=primary_ip&order=desc&page=2&size=25')
  assert.deepEqual(calls.at(-1).params, { q: '192.168.8.', status: 'all', sort: 'primary_ip', order: 'desc', page: 2, page_size: 25 })
  mockTotal = null
  await act(async () => fill(document.querySelector('input[aria-label="Search baseline servers"]'), 'no-match'))
  await settle(400)
  assert.equal(calls.at(-1).params.q, 'no-match')
  assert.equal(calls.at(-1).params.page, 1)
  assert.equal(calls.at(-1).params.sort, 'primary_ip')
  await eventually(() => document.body.textContent.includes('No servers match these filters'), 'An empty search must show the filtered empty state')
  await render('/server-baselines/b?sort=server_name&order=asc&size=25')
  const searchControl = document.querySelector('input[aria-label="Search baseline servers"]')
  searchControl.focus()
  let releaseCoverage
  pendingCoverage = new Promise(resolve => { releaseCoverage = resolve })
  await act(async () => fill(searchControl, 'server'))
  await settle(400)
  assert.equal(document.querySelector('input[aria-label="Search baseline servers"]'), searchControl, 'Search controls must remain mounted while a report refresh is pending')
  assert.equal(document.activeElement, searchControl, 'A debounced search must retain keyboard focus')
  await click(document.querySelector('button[aria-label="Sort by IP address"]'))
  const pendingLocation = new URL(document.querySelector('#location').textContent, 'http://localhost')
  assert.equal(pendingLocation.searchParams.get('q'), 'server')
  assert.equal(pendingLocation.searchParams.get('sort'), 'primary_ip')
  assert.equal(pendingLocation.searchParams.get('size'), '25')
  pendingCoverage = null
  await act(async () => releaseCoverage())
  await settle()
  assert.equal(calls.at(-1).params.sort, 'primary_ip')
  assert.equal(calls.at(-1).params.q, 'server')
  globalThis.__baselineCanManage = false
  await render('/server-baselines/b')
  assert(!button('Evaluate now') && !button('Edit policy'), 'Read-only users must not see mutation controls')
  await render('/server-baselines')
  assert(!button('New baseline') && !document.querySelector('button[title="Delete"]'))
  assert(document.querySelector('a[href="/server-baselines/b"]'))
  fail = true
  await render('/server-baselines/b')
  assert(document.body.textContent.includes('Could not load this data'))
  assert(!document.body.textContent.includes('No servers in scope'))
  assert.equal(writes, 0)
  console.log('Baseline policy navigation, grouped evidence, lazy reads, filters, sorting, URL restoration, row stability, permissions and error states passed')
} finally {
  await act(async () => root.unmount())
  client?.clear()
  assert(path.resolve(temporary).startsWith(cache + path.sep))
  await rm(temporary, { recursive: true, force: true })
}
