// "Message sent" model (src/components/alerts/deliveryMessage.ts): SMS length
// and segment counts (GSM-7 160/153, UCS-2 70/67), the withheld state for users
// without alerts.manage, and the incident page wiring.
// Run from dashboard/: node scripts/test-delivery-message.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const src = fs.readFileSync('src/components/alerts/deliveryMessage.ts', 'utf8')
assert(!/^import /m.test(src), 'deliveryMessage.ts imports nothing, so it runs without the app')
const compiled = await transformWithEsbuild(src, 'deliveryMessage.ts', { loader: 'ts', format: 'esm' })
const M = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

// GSM-7: 160 in one message, 153 per part once split.
assert.deepEqual(M.smsLength(''), { encoding: 'GSM-7', chars: 0, units: 0, segments: 0, perSegment: 160 })
assert.equal(M.smsLength('a'.repeat(160)).segments, 1)
assert.deepEqual(M.smsLength('a'.repeat(161)), { encoding: 'GSM-7', chars: 161, units: 161, segments: 2, perSegment: 153 })
assert.equal(M.smsLength('a'.repeat(306)).segments, 2)
assert.equal(M.smsLength('a'.repeat(307)).segments, 3)
// Extension characters take two septets; accented GSM letters one.
assert.equal(M.smsLength('€'.repeat(80)).units, 160)
assert.equal(M.smsLength('€'.repeat(80)).segments, 1)
assert.equal(M.smsLength('{' + 'a'.repeat(159)).segments, 2)
assert.equal(M.smsLength('Über Größe, café').encoding, 'GSM-7')
assert.equal(M.smsLength('ï').encoding, 'UCS-2')

// UCS-2: one non-GSM character (the em dash the alert texts use) changes the whole text.
const dash = 'ZenPlus WARNING — Memory high on srv'
assert.equal(M.smsLength(dash).encoding, 'UCS-2')
assert.equal(M.smsLength('a'.repeat(69) + '—').segments, 1)
assert.deepEqual(M.smsLength('a'.repeat(70) + '—'), { encoding: 'UCS-2', chars: 71, units: 71, segments: 2, perSegment: 67 })
assert.equal(M.smsLength('ب'.repeat(134)).segments, 2)
assert.equal(M.smsLength('ب'.repeat(135)).segments, 3)
// An emoji is two UTF-16 units but one character on screen.
const emoji = M.smsLength('🔥')
assert.equal(emoji.chars, 1); assert.equal(emoji.units, 2); assert.equal(emoji.segments, 1)

// Labels and the split note say plainly what the phone received.
assert.equal(M.smsLengthLabel(M.smsLength('a'.repeat(161))), '161 characters · GSM-7 · 2 messages')
assert.equal(M.smsLengthLabel(M.smsLength('ok')), '2 characters · GSM-7 · 1 message')
assert.equal(M.smsSplitNote(M.smsLength('ok')), null)
assert.match(M.smsSplitNote(M.smsLength('a'.repeat(70) + '—')), /^Longer than 70: it contains characters outside the basic SMS alphabet/)
assert.match(M.smsSplitNote(M.smsLength('a'.repeat(200))), /^Longer than 160: one message holds 160\. The phone receives 2 parts/)

// The panel model.
assert.equal(M.deliveryMessage({ channel_type: 'email' }), null)                  // older rows: nothing recorded
assert.equal(M.deliveryMessage({ channel_type: 'email', subject: '', content: null }), null)
const mail = M.deliveryMessage({ channel_type: 'email', subject: 'S', content: 'Body', recipients_count: 2 })
assert.deepEqual(mail, { subject: 'S', content: 'Body', withheld: false, recipients: '2 recipients', sms: null })
const sms = M.deliveryMessage({ channel_type: 'sms', content: dash, recipients_count: 1 })
assert.equal(sms.recipients, '1 recipient'); assert.equal(sms.sms.encoding, 'UCS-2'); assert.equal(sms.subject, null)
// A viewer without alerts.manage: subject only, text withheld.
const viewer = M.deliveryMessage({ channel_type: 'email', subject: 'S', content: null, content_withheld: true })
assert.equal(viewer.withheld, true); assert.equal(viewer.content, null)
assert.equal(M.deliveryMessage({ channel_type: 'sms', content: null, content_withheld: true }).sms, null)

// The incident page renders it, and the server field names match.
const card = fs.readFileSync('src/components/alerts/RoutingCard.tsx', 'utf8')
assert.match(card, /deliveryMessage\(d\)/)
assert.match(card, /Message sent/)
assert.match(card, /font-mono/)
assert(!/text-\[\d+px\]/.test(card), 'rem type scale only')
const api = fs.readFileSync('../server/app/api/v1/alerts.py', 'utf8')
for (const f of ['"subject"', '"content"', '"recipients_count"', '"content_withheld"']) assert(api.includes(f), `server sends ${f}`)
const log = fs.readFileSync('../server/app/services/alert_delivery_log.py', 'utf8')
assert.match(log, /CONTENT_MAX = 8000/)

console.log('delivery message model OK')
