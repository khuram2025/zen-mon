// Event alert expiry UI model (src/features/escalation/eventExpiry.ts): parsing,
// validation and the plain-language summary match the server
// (app.services.alert_expiry), and the card is rendered on the storms tab.
// Run from dashboard/: node scripts/test-event-expiry-ui.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const src = fs.readFileSync('src/features/escalation/eventExpiry.ts', 'utf8')
assert(!/^import /m.test(src), 'eventExpiry.ts imports nothing, so it runs without the app')
const compiled = await transformWithEsbuild(src, 'eventExpiry.ts', { loader: 'ts', format: 'esm' })
const E = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

// Defaults and limits match the server: on, 168 hours, 1..8760.
const server = fs.readFileSync('../server/app/services/alert_expiry.py', 'utf8')
assert.match(server, /DEFAULT_SETTINGS: dict\[str, Any\] = \{"enabled": True, "hours": 168\}/)
assert.match(server, /HOURS_LIMITS = \(1, 8760\)/)
assert.deepEqual(E.EVENT_EXPIRY_DEFAULTS, { enabled: true, hours: 168 })
assert.deepEqual([...E.EVENT_EXPIRY_HOURS], [1, 8760])

// Parsing: missing -> defaults, out of range clamped, junk -> default.
assert.deepEqual(E.eventExpiryFromApi({}), { enabled: true, hours: 168 })
assert.deepEqual(E.eventExpiryFromApi(null), { enabled: true, hours: 168 })
assert.deepEqual(E.eventExpiryFromApi({ enabled: false, hours: 0 }), { enabled: false, hours: 1 })
assert.equal(E.eventExpiryFromApi({ hours: 99999 }).hours, 8760)
assert.equal(E.eventExpiryFromApi({ hours: '72' }).hours, 168)
assert.equal(E.eventExpiryFromApi({ hours: 36.7 }).hours, 36)

// Validation.
assert.deepEqual(E.validateEventExpiry({ enabled: true, hours: 168 }), {})
assert(E.validateEventExpiry({ enabled: true, hours: NaN }).hours)
assert(E.validateEventExpiry({ enabled: true, hours: 0 }).hours)
assert(E.validateEventExpiry({ enabled: true, hours: 1.5 }).hours)
assert(E.validateEventExpiry({ enabled: true, hours: 9000 }).hours)

// Wording: same window label and sentence as the server.
assert.equal(E.expiryWindowLabel(168), '7 days')
assert.equal(E.expiryWindowLabel(24), '1 day')
assert.equal(E.expiryWindowLabel(36), '36 hours')
assert.equal(E.expiryWindowLabel(1), '1 hour')
assert.equal(E.eventExpirySentence({ enabled: true, hours: 168 }),
  'Trap, syslog, Device Tracker and configuration-change alerts close by themselves after 7 days without a new event. Closing sends no notification.')
assert.match(E.eventExpirySentence({ enabled: false, hours: 168 }), /^Off: /)
assert(server.includes('configuration-change alerts close by themselves "'), 'server sentence, first half')
assert(server.includes('without a new event. Closing sends no notification.'), 'server sentence, second half')
assert(server.includes('stay open "'), 'server sentence when off')

// The card is its own component, rendered on the storms tab, and uses the rem scale.
const card = fs.readFileSync('src/pages/escalation/EventExpiryCard.tsx', 'utf8')
assert.match(card, /api\.get\('\/alerts\/event-expiry'\)/)
assert.match(card, /api\.put\('\/alerts\/event-expiry'/)
assert(!/text-\[\d+px\]/.test(card), 'no pixel font sizes')
const tab = fs.readFileSync('src/pages/escalation/StormsTab.tsx', 'utf8')
assert.match(tab, /<EventExpiryCard canManage=\{canManage\} \/>/)

// The closure reason is known to the alert types and the timeline.
const types = fs.readFileSync('src/components/alerts/types.ts', 'utf8')
assert.match(types, /\| 'flapping' \| 'expired'/)
const timeline = fs.readFileSync('src/components/alerts/AlertTimeline.tsx', 'utf8')
assert.match(timeline, /expired: \{ icon:/)
const wf = fs.readFileSync('../server/app/services/alert_workflow.py', 'utf8')
assert(wf.includes('"expired": "Expired — no new event within the window"'))

console.log('event expiry UI: ok')
