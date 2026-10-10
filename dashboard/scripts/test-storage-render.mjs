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
const plugins = [{ name: 'storage-fixtures', setup(b) {
  b.onResolve({ filter: /(?:^|\/)useStorage$/ }, () => ({ path: 'hooks', namespace: 'fixture' }))
  b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'fixture' }))
  b.onLoad({ filter: /.*/, namespace: 'fixture' }, args => ({ contents: args.path === 'api' ? api : fakeHooks, loader: 'js', resolveDir: root }))
} }]
const common = `import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StorageTabContent } from '@/components/StorageTabContent';
import { backups } from './scripts/storage-fixture.mjs';
const query = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
query.setQueryData(['system','storage','backups'], backups);
const App = ({ section = 'overview' }) => <QueryClientProvider client={query}><MemoryRouter initialEntries={['/?tab=storage&section='+section]}><main className="mx-auto max-w-5xl p-6"><StorageTabContent /></main></MemoryRouter></QueryClientProvider>;`
const options = { bundle: true, jsx: 'automatic', alias: { '@': path.join(root, 'src') }, plugins, define: { 'process.env.NODE_ENV': '"production"' } }
const out = path.join(root, 'scripts/.storage-render-check.mjs')
try {
  await build({ ...options, stdin: { contents: common + `import { renderToStaticMarkup } from 'react-dom/server'; export const render = section => renderToStaticMarkup(<App section={section} />);`, resolveDir: root, loader: 'tsx' }, platform: 'node', format: 'esm', packages: 'external', outfile: out })
  const { render } = await import(pathToFileURL(out).href)
  for (const section of ['overview','disks','retention','backups','activity']) {
    const html = render(section)
    assert(html.includes('Storage sections'))
    assert(!/ClickHouse|PostgreSQL|LVM|ubuntu-vg|system\.text_log|\/dev\/|\/data\/|Traceback/.test(html), `Internal detail rendered in ${section}`)
  }
  console.log('All five rendered storage sections keep implementation detail out of the customer interface.')
} finally { fs.rmSync(out, { force: true }) }
// Optional isolated preview: synthetic fixtures, no appliance API or credentials.
if (process.env.STORAGE_PREVIEW_DIR) {
  const dest = process.env.STORAGE_PREVIEW_DIR
  fs.mkdirSync(dest, { recursive: true })
  await build({ ...options, stdin: { contents: common + `import { createRoot } from 'react-dom/client'; createRoot(document.getElementById('root')).render(<App />);`, resolveDir: root, loader: 'tsx' }, platform: 'browser', format: 'esm', outfile: path.join(dest, 'preview.js') })
  const css = fs.readdirSync('dist/assets').find(n => /^index-.*\.css$/.test(n))
  fs.copyFileSync(path.join('dist/assets', css), path.join(dest, 'preview.css'))
  fs.writeFileSync(path.join(dest, 'index.html'), '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Storage development preview</title><link rel="stylesheet" href="preview.css"><body><div style="padding:12px;background:#24344b;color:white">Development preview · synthetic data · operations disabled</div><div id="root"></div><script type="module" src="preview.js"></script></body></html>')
}
