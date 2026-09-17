import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'daily-probe-failures-'))
try {
  const outfile = path.join(dir, 'test.cjs')
  await build({ stdin: { contents: `export * from './probeFailures'; export * from './DailyProbeFailureDetails';`,
    resolveDir: path.resolve('src/components/services'), loader: 'tsx' }, outfile, bundle: true,
    platform: 'node', format: 'cjs', jsx: 'automatic' })
  const { buildProbeFailures: periods, loadProbeHistory, DailyProbeFailureDetails: View } = createRequire(import.meta.url)(outfile)
  const start = Date.parse('2026-09-17T01:23:28.360Z')
  const point = (offset, is_up, error_message = 'network is unreachable') => ({
    timestamp: new Date(start + offset * 1000).toISOString(), is_up, error_message,
    network_diagnostics: { failure_stage: 'connect' },
  })
  // Exact reported VDP incident: one failed probe, recovered before Down confirmation.
  const rows = [point(0, true), point(61.006, false), point(122.008, true)]
  const failed = periods(rows, start, start + 180000, 60)
  assert.equal(failed.length, 1)
  assert.equal(failed[0].samples, 1)
  assert.equal(failed[0].end - failed[0].start, 61002)
  assert.equal(failed[0].endKind, 'recovered')
  assert.equal(periods([point(0, true), point(60, null)], start, start + 180000, 60).length, 0)
  const merged = periods([point(0, false), point(60, false), point(120, true)], start, start + 180000, 60)
  assert.equal(merged.length, 1)
  assert.equal(merged[0].samples, 2)
  assert.equal(merged[0].end - merged[0].start, 120000)
  const gap = periods([point(0, false), point(600, true)], start, start + 660000, 60)[0]
  assert.equal(gap.end - gap.start, 120000)
  assert.equal(gap.endKind, 'gap')
  const carried = periods([point(-30, false), point(10, true)], start, start + 60000, 60)[0]
  assert.equal(carried.end - carried.start, 10000)
  assert.equal(carried.samples, 0)
  assert.equal(carried.carriedIn, true)
  const duplicate = periods([point(0, false), point(0, true), point(0, false), point(60, true)], start, start + 60000, 60)
  assert.equal(duplicate[0].samples, 1)
  assert.equal(periods([point(120, false)], start, start + 60000, 60).length, 0)
  assert.equal(periods([point(0, false)], start, start + 30000, 60)[0].endKind, 'window')
  let calls = 0
  const fetched = await loadProbeHistory(async (lo, hi) => {
    calls++
    return hi - lo > 60000 ? Array(5000).fill(point(0, true)) : [point((lo - start) / 1000, lo === start)]
  }, start, start + 120000)
  assert(calls >= 3)
  assert(fetched.some(p => p.is_up === false), 'Must fetch failures beyond the first full API page')
  await assert.rejects(loadProbeHistory(async () => Array(5000).fill(point(0, false)), start, start + 1))
  const render = (extra = {}) => renderToStaticMarkup(React.createElement(View, {
    failures: failed, loading: false, error: false, onRetry() {}, downtimeSeconds: 61.002, hasCoverage: true, ...extra,
  }))
  const html = render()
  assert.match(html, /1 failed check/)
  assert.match(html, /1m 1s/)
  assert.match(html, /Service unreachable/)
  assert.match(html, /Technical details/)
  assert(!html.includes('service stayed up all day'))
  assert.match(render({ loading: true }), /Loading failed checks/)
  assert.match(render({ error: true }), /Could not load failed checks/)
  assert(!render({ error: true }).includes('No failed checks'))
  assert.match(render({ failures: [] }), /individual probe details are unavailable/)
  assert.match(render({ failures: [], downtimeSeconds: 0, hasCoverage: false }), /No probe data/)
  console.log('Daily failure regressions passed: exact 61.002s incident, thresholds independent of evidence, grouping, gaps, boundaries, duplicates, full API pages, loading/error/empty states.')
} finally {
  fs.rmSync(dir, { recursive: true, force: true })
}
