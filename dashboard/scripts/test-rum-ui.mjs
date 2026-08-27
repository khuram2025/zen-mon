import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { transformWithEsbuild } from 'vite'

const root = process.cwd()
const modelPath = path.join(root, 'src/pages/apm/rum/model.ts')
const modelSource = fs.readFileSync(modelPath, 'utf8')
const transformedModel = await transformWithEsbuild(modelSource, modelPath, {
  loader: 'ts',
  format: 'esm',
  target: 'es2020',
})
const modelDataUrl = `data:text/javascript;base64,${Buffer.from(transformedModel.code).toString('base64')}`
const {
  buildRumQuery,
  formatDurationMs,
  formatRumVital,
  normalizeVitalDistribution,
  vitalBand,
} = await import(modelDataUrl)

const filters = {
  application_id: 'store front',
  env: 'prod',
  view_name: '/checkout',
  browser: '',
  browser_version: '',
  os: '',
  device_type: 'mobile',
  country: 'SA',
  service_version: '2026.08.27',
}

assert.equal(formatRumVital('lcp', null), 'No data', 'missing LCP must not render as a healthy zero')
assert.equal(formatRumVital('cls', 0), '0.000', 'a measured zero CLS is valid data')
assert.equal(vitalBand('lcp', null), 'no-data')
assert.equal(vitalBand('lcp', 2500), 'good')
assert.equal(vitalBand('lcp', 2500.1), 'needs-improvement')
assert.equal(vitalBand('inp', 500.1), 'poor')
assert.equal(vitalBand('cls', 0.25), 'needs-improvement')
assert.equal(formatDurationMs(252_000), '4m 12s')
assert.deepEqual(
  normalizeVitalDistribution({ good_pct: 78, needs_improvement_pct: 17, poor_pct: 5 }),
  { good: 78, needsImprovement: 17, poor: 5 },
  'backend percentage points must not be multiplied again',
)
assert.deepEqual(
  normalizeVitalDistribution({ good_pct: 0.78, needs_improvement_pct: 0.17, poor_pct: 0.05 }),
  { good: 78, needsImprovement: 17, poor: 5 },
  'legacy fractional distributions remain compatible',
)

const query = new URLSearchParams(buildRumQuery('24h', filters, { page: 2, page_size: 50, sort: 'last_seen', order: 'desc' }))
assert.equal(query.get('application_id'), 'store front')
assert.equal(query.get('service_version'), '2026.08.27')
assert.equal(query.get('browser'), null, 'empty filters must not be sent')
assert.equal(query.get('page'), '2')

const app = fs.readFileSync(path.join(root, 'src/App.tsx'), 'utf8')
const page = fs.readFileSync(path.join(root, 'src/pages/apm/RumPage.tsx'), 'utf8')
const settings = fs.readFileSync(path.join(root, 'src/pages/apm/ApmSettingsPage.tsx'), 'utf8')
const tables = fs.readFileSync(path.join(root, 'src/pages/apm/rum/RumTables.tsx'), 'utf8')
const overview = fs.readFileSync(path.join(root, 'src/pages/apm/rum/RumOverview.tsx'), 'utf8')
const types = fs.readFileSync(path.join(root, 'src/types/apm.ts'), 'utf8')

assert.match(app, /<Route path="rum" element={<RumPage \/>} \/>/, 'RUM route must remain registered')
for (const endpoint of ['overview', 'timeseries', 'facets', 'views', 'sessions', 'errors', 'resources', 'actions', 'health']) {
  assert.match(page, new RegExp(`/apm/rum/${endpoint}`), `RUM page must query ${endpoint}`)
}
for (const tab of ['overview', 'web-vitals', 'views', 'sessions', 'errors', 'resources', 'actions']) {
  assert.match(types, new RegExp(`['"]${tab}['"]`), `RUM tab ${tab} must be URL-addressable`)
}
assert.match(page, /\/apm\/settings\?tab=keys&create=rum/, 'empty-state setup must deep-link to Browser RUM key creation')
assert.match(page, /MAX_SESSION_TIMELINE_EVENTS/, 'session drill-down must fetch a bounded multi-page timeline')
assert.match(tables, /RumCoverageNotice/, 'event explorers must disclose partial raw-data coverage')
assert.match(overview, /Release health/, 'overview must expose release regression context')
assert.match(settings, /<DialogDescription>/, 'RUM onboarding dialogs must expose an accessible description')

console.log('RUM UI tests passed: vital semantics, URL state, API surface, coverage, releases, tabs, and onboarding verified')
