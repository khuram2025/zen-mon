// The Updates tab's upgrade path: why a stop is required, how many restarts the path means, and when the
// appliance is on hold after failed installs.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

async function load(path) {
  const src = fs.readFileSync(path, 'utf8')
  const compiled = await transformWithEsbuild(src, path.split('/').pop(), { loader: 'ts', format: 'esm' })
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
}

const m = await load('src/components/updatePath.ts')
const step = (version, min_version = null) => ({ id: version, version, min_version, changelog: '', severity: 'normal' })

/* ── Required stops ───────────────────────────────────────────────────── */
const path = [step('1.24.36', '1.23.9'), step('1.26.0', '1.24.36'), step('1.26.4', '1.26.0')]
const bridge = m.stepRole(path, 0)
assert.equal(bridge.required, true)
assert.equal(bridge.label, 'Required intermediate version')
assert.match(bridge.reason, /v1\.26\.0 can only be installed from v1\.24\.36 or later/)
assert.match(bridge.reason, /cannot be skipped/)
const last = m.stepRole(path, 2)
assert.equal(last.required, false)
assert.equal(last.label, 'Latest eligible version')
assert.match(last.reason, /after every required version/)
assert.equal(m.stepRole([step('1.26.4', '1.24.36')], 0).reason, '', 'a direct update needs no explanation')
// The server may offer a stop whose successor names an older minimum: still required, worded without the claim.
assert.match(m.stepRole([step('1.24.46'), step('1.24.48', '1.24.36')], 0).reason, /^Must be installed before v1\.24\.48/)

assert.equal(m.pathSummary([step('1.26.4')]), 'One installation. Monitoring services restart once.')
assert.match(m.pathSummary(path), /^3 installations in order: 2 required intermediate versions, then the latest\./)
assert.match(m.pathSummary(path.slice(1)), /1 required intermediate version,/)

/* ── On hold after failed installs (history is newest first) ──────────── */
const attempt = (version, from_version, status, error = '', completed_at = '2026-11-17T08:00:00Z') =>
  ({ version, from_version, status, error, completed_at })
assert.equal(m.pathHold([], '1.25.6'), null)
assert.equal(m.pathHold([attempt('1.25.6', '1.25.2', 'success')], '1.25.6'), null)

const once = m.pathHold([
  attempt('1.25.10', '1.25.6', 'failed', 'Update failed at step 19/39 (run_migration)'),
  attempt('1.25.6', '1.25.2', 'success'),
], '1.25.6')
assert.deepEqual({ version: once.version, attempts: once.attempts, exhausted: once.exhausted },
  { version: '1.25.10', attempts: 1, exhausted: false })
assert.match(once.error, /step 19\/39/)

const thrice = m.pathHold([
  attempt('1.25.10', '1.25.6', 'rolled_back'), attempt('1.25.10', '1.25.6', 'failed'),
  { version: '1.25.10', from_version: '1.25.6', status: 'downloading', error: '', completed_at: '' },
  attempt('1.25.10', '1.25.6', 'failed'), attempt('1.25.6', '1.25.2', 'success'),
  attempt('1.25.6', '1.25.2', 'failed'),
], '1.25.6')
assert.equal(thrice.attempts, 3, 'unfinished attempts and failures before the last success are not counted')
assert.equal(thrice.exhausted, true)

// A later success, or the appliance now running that version, ends the hold.
assert.equal(m.pathHold([attempt('1.25.13', '1.25.6', 'success'), attempt('1.25.10', '1.25.6', 'failed')], '1.25.13'), null)
assert.equal(m.pathHold([attempt('1.25.10', '1.25.6', 'failed')], '1.25.10'), null)
// Failures of another version are not added to this one.
assert.equal(m.pathHold([attempt('1.25.11', '1.25.6', 'failed'), attempt('1.25.10', '1.25.6', 'failed')], '1.25.6').attempts, 1)

console.log('Passed: required-stop reasons, path summary, on-hold state from update history')
