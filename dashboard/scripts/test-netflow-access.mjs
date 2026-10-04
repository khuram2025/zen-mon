// NetFlow access states (X-01, NF-04 acceptance): one access-denied state without netflow.view,
// one empty-scope state, one exporter-not-found state — never collection-gap wording for them.
// Run from dashboard/: node scripts/test-netflow-access.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'
const root = process.cwd()
const src = path.join(root, 'src')

const entry = `
export * as access from '@/components/netflow/NetflowAccess'
export { useAuth } from '@/stores/auth'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter, Routes, Route } from 'react-router-dom'
export { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
  plugins: [aliasPlugin],
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { access: X } = m
const h = m.createElement

const GAP_WORDS = [/collection gap/i, /No exporter has sent flows/i, /did not answer/i, /0 actionable/i]
const noGapWording = (html, label) => {
  for (const re of GAP_WORDS) assert.ok(!re.test(html), `${label}: ${re}`)
}

function setUser(user) {
  // Server rendering reads a zustand store's initial state (getServerSnapshot), so the signed-in
  // user is written there as well as into the live state.
  m.useAuth.setState({ user, token: 't' })
  const initial = typeof m.useAuth.getInitialState === 'function' ? m.useAuth.getInitialState() : null
  if (initial) Object.assign(initial, { user, token: 't' })
}

function renderAt(pathname, { permissions, scope }) {
  setUser({ id: 'u', username: 'u', email: 'u@x', role: 'custom', permissions })
  const qc = new m.QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (scope !== undefined) qc.setQueryData(['netflow', 'scope'], scope)
  return m.renderToStaticMarkup(
    h(
      m.QueryClientProvider,
      { client: qc },
      h(
        m.MemoryRouter,
        { initialEntries: [pathname] },
        h(
          m.Routes,
          null,
          h(
            m.Route,
            { element: h(X.NetflowAccessGate) },
            h(m.Route, { path: '/netflow', element: h('div', null, 'OVERVIEW-PAGE') }),
            h(m.Route, { path: '/netflow/forensics', element: h('div', null, 'FORENSICS-PAGE') }),
          ),
        ),
      ),
    ),
  )
}

/* ─── Helpers ───────────────────────────────────────────────────────── */
assert.equal(X.isNotFoundError({ response: { status: 404 } }), true)
assert.equal(X.isNotFoundError({ response: { status: 403 } }), false)
assert.equal(X.isNotFoundError(null), false)
assert.equal(X.isScopeEmpty({ restricted: true, scope_empty: true, flow_exporters: 0, can_manage: false, can_export: false }), true)
assert.equal(X.isScopeEmpty({ restricted: true, scope_empty: null, flow_exporters: null, can_manage: false, can_export: false }), false)
assert.equal(X.isScopeEmpty({ restricted: false, scope_empty: false, flow_exporters: null, can_manage: true, can_export: true }), false)
assert.equal(X.isScopeEmpty(undefined), false)

/* ─── No netflow.view: one access-denied state, the page never renders ─ */
let html = renderAt('/netflow', { permissions: ['dashboard.view', 'alerts.view'] })
assert.match(html, new RegExp(X.ACCESS_DENIED_TITLE))
assert.match(html, /netflow\.view/)
assert.ok(!html.includes('OVERVIEW-PAGE'))
noGapWording(html, 'denied overview')
html = renderAt('/netflow/forensics', { permissions: ['dashboard.view'] })
assert.match(html, new RegExp(X.ACCESS_DENIED_TITLE))
assert.ok(!html.includes('FORENSICS-PAGE') && !/<form|<input/i.test(html), 'no usable-looking search form')

/* ─── netflow.view: the page renders; system.admin implies it ─────── */
html = renderAt('/netflow/forensics', { permissions: ['netflow.view'], scope: { restricted: false, scope_empty: false, flow_exporters: null, can_manage: false, can_export: false } })
assert.ok(html.includes('FORENSICS-PAGE') && !html.includes(X.SCOPE_EMPTY_TITLE))
html = renderAt('/netflow', { permissions: ['system.admin'] })
assert.ok(html.includes('OVERVIEW-PAGE'))

/* ─── Empty scope: banner on other pages, the overview shows the state itself ─ */
const empty = { restricted: true, scope_empty: true, flow_exporters: 0, can_manage: false, can_export: false }
html = renderAt('/netflow/forensics', { permissions: ['netflow.view'], scope: empty })
assert.ok(html.includes(X.SCOPE_EMPTY_TITLE) && html.includes('FORENSICS-PAGE'))
html = renderAt('/netflow', { permissions: ['netflow.view'], scope: empty })
assert.ok(!html.includes(X.SCOPE_EMPTY_TITLE) && html.includes('OVERVIEW-PAGE'), 'overview renders ScopeEmptyState itself')

/* ─── The full-page states ──────────────────────────────────────────── */
const render = (el) => m.renderToStaticMarkup(h(m.MemoryRouter, null, el))
html = render(h(X.ScopeEmptyState))
assert.ok(html.includes(X.SCOPE_EMPTY_TITLE) && /access scope, not a collection problem/.test(html))
noGapWording(html.replace('not a collection problem', ''), 'scope-empty state')
html = render(h(X.ExporterNotFoundState, { ip: '127.0.0.92' }))
assert.match(html, /No exporter 127\.0\.0\.92 in your view/)
assert.match(html, /outside your\s+scope/)
noGapWording(html, 'not-found state')
html = render(h(X.NetflowAccessDenied))
assert.match(html, /role="status"/)

/* ─── Wiring: every /netflow route sits under the gate; the overview uses the states ─ */
const app = fs.readFileSync(path.join(src, 'App.tsx'), 'utf8')
const gate = app.indexOf('<Route element={<NetflowAccessGate />}>')
assert.ok(gate > 0, 'App.tsx wraps the NetFlow routes')
const close = app.indexOf('</Route>', gate)
const netflowRoutes = [...app.matchAll(/<Route path="netflow[^"]*"/g)].map((r) => r.index)
assert.ok(netflowRoutes.length >= 11)
for (const at of netflowRoutes) assert.ok(at > gate && at < close, 'netflow route outside the gate')
const overview = fs.readFileSync(path.join(src, 'pages/netflow/OverviewPage.tsx'), 'utf8')
assert.match(overview, /isNotFoundError\(overview\.error\)\) return <ExporterNotFoundState/)
assert.match(overview, /isScopeEmpty\(scope\.data\)\) return <ScopeEmptyState/)
const card = fs.readFileSync(path.join(src, 'pages/netflow/anomalies/FindingCard.tsx'), 'utf8')
assert.match(card, /findings-assignees', \{ params: \{ finding: f\.id \} \}/)

console.log('netflow access states: ok')
