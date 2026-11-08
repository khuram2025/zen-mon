// Alert storm policy UI model (src/features/escalation/storm.ts): parsing,
// validation, the plain-language summary and the "what will I receive" example
// match the server (app.services.alert_storm), and the tab is registered.
// Run from dashboard/: node scripts/test-alert-storm-ui.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const src = fs.readFileSync('src/features/escalation/storm.ts', 'utf8')
assert(!/^import /m.test(src), 'storm.ts imports nothing, so it runs without the app')
const compiled = await transformWithEsbuild(src, 'storm.ts', { loader: 'ts', format: 'esm' })
const S = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

// Defaults match the server: off, more than 5 assets in 5 min, down + reboots, all assets.
assert.deepEqual(S.stormPolicyFromApi({}), { ...S.STORM_DEFAULTS, enabled: false })
assert.deepEqual(S.STORM_DEFAULTS.families, ['reachability', 'reboot'])
const server = fs.readFileSync('../server/app/services/alert_storm.py', 'utf8')
assert.match(server, /"threshold": 5,\s*\n\s*"window_minutes": 5,\s*\n\s*"families": \["reachability", "reboot"\]/)
assert.match(server, /"update_minutes": 15/)
for (const f of S.STORM_FAMILIES) assert(server.includes(`"${f.value}"`), `server knows family ${f.value}`)

// Parsing clamps and drops unknown values like the server.
const p = S.stormPolicyFromApi({ enabled: true, threshold: 1, window_minutes: 500, families: ['reboot', 'bogus', 'reachability'], scope: 'moon', update_minutes: 2, hold_escalations: false })
assert.equal(p.threshold, 2); assert.equal(p.window_minutes, 120); assert.equal(p.update_minutes, 5)
assert.deepEqual(p.families, ['reachability', 'reboot']); assert.equal(p.scope, 'all'); assert.equal(p.hold_escalations, false)

// Validation.
assert.deepEqual(S.validateStormPolicy(S.STORM_DEFAULTS), {})
const bad = S.validateStormPolicy({ ...S.STORM_DEFAULTS, enabled: true, threshold: NaN, window_minutes: 0, families: [] })
assert(bad.threshold && bad.window_minutes && bad.families)
assert.equal(S.validateStormPolicy({ ...S.STORM_DEFAULTS, enabled: false, families: [] }).families, undefined)

// Sentences say plainly what happens.
assert.equal(S.stormSentence(S.STORM_DEFAULTS), 'Off: every asset is notified on its own.')
const on = { ...S.STORM_DEFAULTS, enabled: true, scope: 'site' }
assert.equal(S.stormSentence(on), 'When more than 5 assets report devices down, reboots and restarts within 5 min (per site), each channel gets one combined notification instead of one per asset, then an update every 15 min and a closing summary.')
assert.equal(S.stormExample(on, 40), '5 individual notifications, then 1 combined notification listing all 40 assets (instead of 35 more)')
assert.equal(S.stormExample(on, 4), '4 individual notifications')
assert.equal(S.stormExample(S.STORM_DEFAULTS, 40), '40 individual notifications')

// API mapping.
const st = S.stormFromApi({ id: 's1', family: 'reachability', family_label: 'Devices down', scope_label: 'Riyadh DC', status: 'active', threshold: 5, window_minutes: 5, started_at: '2026-11-08T07:00:00Z', member_count: 12, recovered_count: 3, members: [{ alert_id: 'a1', asset: 'sw-01', held: true, recovered: false }] })
assert.equal(st.status, 'active'); assert.equal(st.members.length, 1); assert.equal(st.members[0].held, true)
assert.equal(S.stormFromApi({ id: 's2', status: 'weird' }).status, 'ended')

// The tab is wired into Escalation & On-call and deep links from storm notifications open it.
const page = fs.readFileSync('src/pages/escalation/EscalationPage.tsx', 'utf8')
assert.match(page, /storms: \{ label: 'Alert storms'/)
assert.match(page, /params\.get\('storm'\)/)
assert.match(server, /\/escalation\?tab=storms&storm=/)
const tab = fs.readFileSync('src/pages/escalation/StormsTab.tsx', 'utf8')
assert(tab.includes("api.put('/alerts/storm-policy'") && tab.includes("'/alerts/storms'"))
assert(!/text-\[\d+px\]/.test(tab), 'rem type scale only')
console.log('alert storm UI model: ok')
