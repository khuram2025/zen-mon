import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import os from 'node:os'
import { JSDOM } from 'jsdom'
import React, { act } from 'react'

const require = createRequire(import.meta.url)
const source = path.join(process.cwd(), 'src')
const compiledDir = await mkdtemp(path.join(os.tmpdir(), 'zenplus-list-tests-'))
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.HTMLInputElement = dom.window.HTMLInputElement
for (const name of ['DocumentFragment','MutationObserver','CustomEvent','Element','Node','Event']) globalThis[name] = dom.window[name]
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const { createRoot } = await import('react-dom/client')
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const { MemoryRouter } = await import('react-router-dom')
const requests = []
let writes = 0
globalThis.__listApi = {
  get: async (url, config) => {
    if (url === '/agent-fleet') {
      const params = { ...config.params }
      requests.push(params)
      const items = [
        { id: 'a', agent_uid: 'a', hostname: 'server2', version: '1.14.2', last_ip: '192.168.8.2' },
        { id: 'b', agent_uid: 'b', hostname: 'server10', version: '1.14.10', last_ip: '192.168.8.10' },
      ].map(a => ({ ...a, status: 'online', platform: 'windows', authorization_state: 'authorized', update_ring: 'stable', queue_depth: 0, spool_bytes: 0, tags: [], capabilities: [], apm_status: {}, created_at: '2026-11-01T10:00:00Z' }))
      if (params.order === 'desc') items.reverse()
      return { data: { items, total: 2, page: 1, page_size: 50 } }
    }
    return { data: { items: [] } }
  },
  post: () => { writes++; throw new Error('Read-only UI tests must not issue mutations') },
}
async function compile(entry) {
  const compiled = await build({ entryPoints: [entry], bundle: true, write: false, format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent', plugins: [{ name: 'test', setup(b) {
    b.onResolve({ filter: /^(react(?:\/.*)?|react-dom(?:\/.*)?|react-router-dom|@tanstack\/react-query)$/ }, a => ({ path: import.meta.resolve(a.path), external: true }))
    b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'mock' }))
    b.onResolve({ filter: /^@\/components\/(servers\/DownloadAgentDialog|license\/LicenseCapacityNotice)$/ }, a => ({ path: a.path.split('/').at(-1), namespace: 'empty' }))
    b.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({ contents: 'export const api=globalThis.__listApi', loader: 'js' }))
    b.onLoad({ filter: /.*/, namespace: 'empty' }, a => ({ contents: `export const ${a.path}=()=>null`, loader: 'js' }))
    b.onResolve({ filter: /^@\// }, a => b.resolve(`./${a.path.slice(2)}`, { resolveDir: source, kind: a.kind }))
  } }] })
  const file = path.join(compiledDir, path.basename(entry) + '.mjs')
  await writeFile(file, compiled.outputFiles[0].text)
  return import(pathToFileURL(file).href)
}
const { compareListValues, compareIpAddresses, searchRank } = await compile('src/lib/listOrdering.ts')
assert.deepEqual(['server10','Server2','server1'].sort(compareListValues), ['server1','Server2','server10'])
assert(compareIpAddresses('10.0.0.2','192.168.8.1') < 0, 'IPv4 addresses above 127 must not become negative')
assert(compareIpAddresses('192.168.8.2','192.168.8.10') < 0)
assert(compareIpAddresses('2001:db8::2','2001:db8::10') < 0)
assert.equal(compareIpAddresses('::ffff:192.168.8.2','0:0:0:0:0:ffff:c0a8:802'),0)
assert(compareIpAddresses(null, '10.0.0.1', -1) > 0)
assert.equal(searchRank(' SERVER ', ['server']),0)
assert.equal(searchRank('server', ['server2']),1)
assert.equal(searchRank('server', ['my-server']),2)

const { AgentFleetPage } = await compile('src/pages/servers/AgentFleetPage.tsx')
const root = createRoot(document.getElementById('root'))
let qc
async function render(initial) {
  qc?.clear()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  await act(async () => { root.render(React.createElement(QueryClientProvider, { client: qc, key: initial }, React.createElement(MemoryRouter, { initialEntries: [initial] }, React.createElement(AgentFleetPage)))) })
  await act(async () => { await new Promise(r => setTimeout(r,30)) })
}
const sortButton = label => document.querySelector(`button[aria-label="Sort by ${label}"]`)
try {
  await render('/server-agents?q=192.168.8.&sort=last_ip&order=desc')
  assert.equal(requests.at(-1).sort,'last_ip')
  assert.equal(requests.at(-1).q,'192.168.8.')
  assert.equal(document.querySelector('th[aria-sort="descending"]').textContent.trim(),'Last IP')
  const checkbox = document.querySelector('input[aria-label="Select server2"]')
  await act(async () => checkbox.click())
  assert(checkbox.checked)
  let query = qc.getQueryCache().findAll({ queryKey: ['agent-fleet'] }).at(-1)
  assert.equal([...query.observers][0].options.refetchInterval,false, 'Selected agents pause automatic refresh')
  assert.equal([...query.observers][0].options.refetchOnWindowFocus,false)
  const before = [...document.querySelectorAll('input[aria-label^="Select server"]')].map(e => e.getAttribute('aria-label'))
  await act(async () => qc.setQueryData(query.queryKey, data => ({ ...data, items: [...data.items].reverse().map(a => ({...a, queue_depth: 77})) })))
  await act(async () => { await new Promise(r => setTimeout(r,30)) })
  assert.deepEqual([...document.querySelectorAll('input[aria-label^="Select server"]')].map(e => e.getAttribute('aria-label')), before, 'An in-flight refresh cannot move selected rows')
  assert(document.querySelector('tbody').textContent.includes('77'), 'Holding row order does not discard updated cell values')
  await act(async () => sortButton('Hostname').click())
  await act(async () => { await new Promise(r => setTimeout(r,30)) })
  assert.equal(requests.at(-1).sort,'hostname')
  assert.equal(requests.at(-1).order,'asc')
  assert(document.querySelector('input[aria-label="Select server2"]').checked, 'Selection follows the agent identity across a re-sort')
  await act(async () => sortButton('Hostname').click())
  await act(async () => { await new Promise(r => setTimeout(r,30)) })
  assert.equal(requests.at(-1).order,'desc')
  assert(document.querySelector('input[aria-label="Select server2"]').checked)
  assert.equal(document.querySelectorAll('button[aria-label^="Sort by "]').length,11)
  await render('/server-agents?sort=hostname&order=desc&q=server')
  assert.equal(requests.at(-1).order,'desc', 'The sort and search survive reopening the URL')
  assert.equal(writes,0)
  console.log('List ordering: natural names, numeric IPv4/IPv6, match priority, column sorting, URL persistence and ID-based selection passed.')
} finally {
  await act(async () => root.unmount())
  qc?.clear()
  dom.window.close()
  delete globalThis.__listApi
  if (path.dirname(compiledDir) === os.tmpdir()) await rm(compiledDir, { recursive: true })
}
