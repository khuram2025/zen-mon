import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
// React's production server renderer avoids expected useLayoutEffect SSR warnings.
process.env.NODE_ENV = 'production'
const root = process.cwd()
const fixture = path.join(root, 'scripts/storage-fixture.mjs')
const fakeHooks = `import { overview, status, events } from ${JSON.stringify(fixture)};
export const STORAGE_KEY = ['system','storage'];
export const useOverview = () => ({ data: overview, isLoading: false, refetch: () => {} });
export const useStorageStatus = () => ({ data: status, isLoading: false, refetch: () => {} });
export const useStorageEvents = () => ({ data: events, isLoading: false });
export const useStorageAction = () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false });`
const api = `import { backups } from ${JSON.stringify(fixture)}; export const api = { get: async () => ({ data: backups }), post: async () => { throw new Error('Fixture: operations disabled') }, put: async () => { throw new Error('Fixture: operations disabled') }, delete: async () => { throw new Error('Fixture: operations disabled') } };`
const fakeSystemHooks = `import { systemStatus, systemHistory, systemHistoryFirst } from ${JSON.stringify(fixture)};
// The preview shows the first minutes of history with ?history=first.
const history = () => typeof location !== 'undefined' && new URLSearchParams(location.search).get('history') === 'first' ? systemHistoryFirst : systemHistory;
export const SYSTEM_STATUS_KEY = ['system','status'];
export const useSystemStatus = () => ({ data: systemStatus, isLoading: false, isError: false, isFetching: false, refetch: () => {} });
export const useSystemHistory = () => ({ data: history(), isLoading: false, isError: false, refetch: () => {} });`
const plugins = [{ name: 'storage-fixtures', setup(b) {
  b.onResolve({ filter: /(?:^|\/)useSystemStatus$/ }, () => ({ path: 'system-hooks', namespace: 'fixture' }))
  b.onResolve({ filter: /(?:^|\/)useStorage$/ }, () => ({ path: 'hooks', namespace: 'fixture' }))
  b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'fixture' }))
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, args => ({ contents: args.path === 'api' ? api : args.path === 'system-hooks' ? fakeSystemHooks : fakeHooks, loader: 'js', resolveDir: root }))
} }]
const common = `import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SystemStatusTabContent } from '@/components/SystemStatusTabContent';
import { backups } from './scripts/storage-fixture.mjs';
const query = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
query.setQueryData(['system','storage','backups'], backups);
const App = ({ section = '' }) => <QueryClientProvider client={query}><MemoryRouter initialEntries={['/?tab=storage'+(section ? '&section='+section : '')]}><main className="mx-auto max-w-5xl p-6"><SystemStatusTabContent /></main></MemoryRouter></QueryClientProvider>;`
const options = { bundle: true, jsx: 'automatic', alias: { '@': path.join(root, 'src') }, plugins, define: { 'process.env.NODE_ENV': '"production"' } }
const out = path.join(root, 'scripts/.storage-render-check.mjs')
try {
  await build({ ...options, stdin: { contents: common + `import { renderToStaticMarkup } from 'react-dom/server'; export const render = section => renderToStaticMarkup(<App section={section} />);`, resolveDir: root, loader: 'tsx' }, platform: 'node', format: 'esm', packages: 'external', outfile: out })
  const { render } = await import(pathToFileURL(out).href)
  // The five storage sections keep product-level wording: no component, volume, table or path names.
  for (const section of ['storage','disks','retention','backups','activity']) {
    const html = render(section)
    assert(html.includes('System status sections'))
    assert(!/ClickHouse|PostgreSQL|LVM|ubuntu-vg|system\.text_log|\/dev\/|\/data\/|Traceback/.test(html), `Internal detail rendered in ${section}`)
  }
  // `section=overview` was the storage overview's own name; it still opens it.
  assert(render('overview').includes('Appliance storage'))
  console.log('All five rendered storage sections keep implementation detail out of the customer interface.')

  // The default section is the appliance's own health. Services are named by what they do; which software is
  // behind them (database engines, the web server), unit names, paths and errors are not shown, even when sent.
  const health = render('')
  for (const text of ['Appliance health', 'Web application', 'Configuration database', 'Metrics database', 'Web server', 'Monitoring engine',
    'Cache and live updates is not answering.', 'Memory use is high (93%).', 'Additional services', 'Switched off', 'Could not be checked',
    'Down (not answering)', '1.25.17', 'Synchronisation could not be checked']) {
    assert(health.includes(text), `Health overview is missing: ${text}`)
  }
  assert(!/ClickHouse|PostgreSQL|Postgres|Redis|nginx|uvicorn|systemd|16\.4|24\.10/i.test(health), 'The health overview names the software behind a service')
  assert(!/data-service="(postgresql|clickhouse|redis|nginx)"/.test(health))
  // Only storage findings that need the operator are repeated here; an informational one stays on its own section.
  assert(!health.includes('Additional disk capacity detected'))
  assert(!/\.service|ubuntu-vg|system\.text_log|\/dev\/|\/data\/|Traceback|LVM/.test(health), 'Internal detail rendered in the health overview')
  // A reading that could not be taken is a dash, never zero: storage (disk: null), swap use (null), the unchecked poller.
  const tile = (label) => health.split(`data-kpi="${label}"`)[1].split('data-kpi=')[0]
  assert(tile('Storage').includes('—') && tile('Storage').includes('could not be read'), 'missing storage reading must not render as a number')
  assert(tile('Swap').includes('—') && !/>0(\.0)?%</.test(tile('Swap')), 'missing swap reading must not render as 0%')
  assert(tile('Memory').includes('93%') && tile('CPU').includes('4.2%'))
  const engine = health.split('data-service="monitoring_engine"')[1].split('</tr>')[0]
  assert(!/\d/.test(engine.replace(/<[^>]+>/g, '').replace('Monitoring engine', '')), 'an unchecked service shows no figures')
  assert(health.includes('Unavailable'), 'a data store that did not answer says Unavailable')
  console.log('The health overview names services by what they do, shows missing readings as unavailable and keeps software names, unit names, paths and errors out.')
} finally { fs.rmSync(out, { force: true }) }
// Optional isolated preview: synthetic fixtures, no appliance API or credentials.
if (process.env.STORAGE_PREVIEW_DIR) {
  const dest = process.env.STORAGE_PREVIEW_DIR
  fs.mkdirSync(dest, { recursive: true })
  await build({ ...options, stdin: { contents: common + `import { createRoot } from 'react-dom/client'; createRoot(document.getElementById('root')).render(<App />);`, resolveDir: root, loader: 'tsx' }, platform: 'browser', format: 'esm', outfile: path.join(dest, 'preview.js') })
  const css = fs.readdirSync('dist/assets').find(n => /^index-.*\.css$/.test(n))
  fs.copyFileSync(path.join('dist/assets', css), path.join(dest, 'preview.css'))
  // ?theme=light or ?theme=dark picks the colour scheme (the app keeps it in browser storage; dark is its default).
  fs.writeFileSync(path.join(dest, 'index.html'), '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>System status development preview</title><link rel="stylesheet" href="preview.css"><script>var t=new URLSearchParams(location.search).get("theme");if(t)localStorage.setItem("zp-theme",JSON.stringify({state:{theme:t},version:0}))</script><body><div style="padding:12px;background:#24344b;color:white">Development preview · synthetic data · operations disabled</div><div id="root"></div><script type="module" src="preview.js"></script></body></html>')

}
