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
  buildRumHref,
  buildRumQuery,
  coreWebVitalsAssessment,
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

assert.equal(buildRumHref('overview', '24h', filters), `/apm/rum?${new URLSearchParams({
  range: '24h',
  application_id: 'store front',
  env: 'prod',
  view_name: '/checkout',
  device_type: 'mobile',
  country: 'SA',
  service_version: '2026.08.27',
}).toString()}`)
assert.match(buildRumHref('sessions', '1h', { ...filters, browser: '', browser_version: '', os: '', device_type: '', country: '', service_version: '' }), /tab=sessions/)

const allGood = coreWebVitalsAssessment({
  lcp: { p75: 1800, samples: 10, good_pct: 90, needs_improvement_pct: 8, poor_pct: 2 },
  inp: { p75: 120, samples: 10, good_pct: 80, needs_improvement_pct: 15, poor_pct: 5 },
  cls: { p75: 0.05, samples: 10, good_pct: 70, needs_improvement_pct: 20, poor_pct: 10 },
})
assert.equal(allGood.band, 'good')
assert.equal(allGood.score, 80)
assert.equal(coreWebVitalsAssessment({
  lcp: { p75: null, samples: 0 },
  inp: { p75: null, samples: 0 },
  cls: { p75: null, samples: 0 },
}).band, 'no-data')

const app = fs.readFileSync(path.join(root, 'src/App.tsx'), 'utf8')
const page = fs.readFileSync(path.join(root, 'src/pages/apm/RumPage.tsx'), 'utf8')
const settings = fs.readFileSync(path.join(root, 'src/pages/apm/ApmSettingsPage.tsx'), 'utf8')
const tables = fs.readFileSync(path.join(root, 'src/pages/apm/rum/RumTables.tsx'), 'utf8')
const overview = fs.readFileSync(path.join(root, 'src/pages/apm/rum/RumOverview.tsx'), 'utf8')
const types = fs.readFileSync(path.join(root, 'src/types/apm.ts'), 'utf8')
const nav = fs.readFileSync(path.join(root, 'src/components/layout/navigation.ts'), 'utf8')

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
assert.match(overview, /RumExperienceCard/, 'overview must surface a field-experience score')
assert.match(settings, /<DialogDescription>/, 'RUM onboarding dialogs must expose an accessible description')
assert.match(nav, /rumTab\('web-vitals'/, 'sidebar must expose RUM Web Vitals navigation')
assert.match(nav, /rumTab\('sessions'/, 'sidebar must expose RUM session navigation')
assert.match(page, /RumTabBar/, 'RUM page must keep in-page section navigation')
assert.match(page, /isErr=\{\(row\) => row\.error_count/, 'actions explorer must classify rows without a free `action` identifier')
assert.doesNotMatch(page, /isErr=\{\(row\) => action\./, 'actions histogram must not reference an undefined `action`')
assert.match(tables, /DurationTimeline/, 'session and resource explorers must show duration timelines')
assert.match(tables, /aria-label=\{`Open action/, 'action rows must be named so they can be opened from the keyboard and a11y tree')
assert.match(tables, /onOpen\(action\)/, 'action name must be a real button so the row can be opened from the a11y tree')
const rumUi = fs.readFileSync(path.join(root, 'src/pages/apm/rum/RumUi.tsx'), 'utf8')
assert.match(rumUi, /shown === 1 && noun\.endsWith\('s'\)/, 'explorer summaries must singularize a count of one')

const layout = fs.readFileSync(path.join(root, 'src/pages/apm/ApmLayout.tsx'), 'utf8')
const explorer = fs.readFileSync(path.join(root, 'src/components/apm/explorer.tsx'), 'utf8')
const traces = fs.readFileSync(path.join(root, 'src/pages/apm/TraceExplorerPage.tsx'), 'utf8')
const errors = fs.readFileSync(path.join(root, 'src/pages/apm/ErrorsInboxPage.tsx'), 'utf8')
const waterfall = fs.readFileSync(path.join(root, 'src/pages/apm/TraceWaterfallPage.tsx'), 'utf8')
const dialog = fs.readFileSync(path.join(root, 'src/pages/apm/rum/RumDetailDialog.tsx'), 'utf8')
assert.match(layout, /ApmUnderlineNav/, 'APM module chrome must use underline tabs')
assert.match(explorer, /export function RequestFlow/, 'shared request-path visualization must exist')
assert.match(explorer, /export function hopsFromTraceSpans/, 'trace hops helper must exist')
assert.match(traces, /ApmExplorerFrame/, 'trace explorer must use the analytics frame')
assert.match(traces, /RequestFlow/, 'trace rows must expand into a request path')
assert.match(traces, /\/apm\/traces\//, 'trace expand must still fetch the existing trace detail API')
assert.match(errors, /ApmUnderlineNav/, 'error inbox statuses must use underline tabs')
assert.match(waterfall, /RequestFlow/, 'waterfall must show the request path above spans')
assert.match(dialog, /RequestFlow/, 'RUM session detail must show the client-to-backend path')

console.log('RUM UI tests passed: vital semantics, URL state, API surface, coverage, releases, tabs, and onboarding verified')
