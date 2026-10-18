// Escalation & On-call UI (1.24.17, work/ESCALATION-PLAN.md "UI"):
//   (a) the pure model (src/features/escalation/model.ts): priority derivation (P1 never derived), response targets
//       with legacy severity keys, policy drafts / payloads / validation against the server limits, the live outline,
//       default-policy routing and conflicts, the route explanation, the ?ack=1 confirmation state, time-zone
//       arithmetic, the 14-day timeline layout, grouping settings, coverage issues, contact methods and the
//       recommended matrix (mirrors escalation_policies._recommended),
//   (b) server rendering with seeded react-query data: the Matrix tab, the policy editor with its dry-run panel
//       (saved policy by id, unsaved draft through /escalation-policies/dry-run), read-only for alerts.view,
//   (c) the acknowledgement link never acknowledges: rendering /alerts/<id>?ack=1 sends nothing, the confirmation
//       body only calls its handler on an explicit click, and the page wires the POST to that button only,
//   (d) the rule wizard: priority picker and escalation mode, the legacy tier cap matches the server (5).
// Run from dashboard/: node scripts/test-escalation-ui.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'
import { transformWithEsbuild } from 'vite'

process.env.TZ = 'UTC'
const root = process.cwd()
const src = path.join(root, 'src')

/* ═══ (a) the pure model, compiled on its own (it imports types only) ═══════════════════════ */

const modelSrc = fs.readFileSync('src/features/escalation/model.ts', 'utf8')
assert(!/^import (?!type )/m.test(modelSrc), 'model.ts imports types only, so it runs without the app')
const compiled = await transformWithEsbuild(modelSrc, 'model.ts', { loader: 'ts', format: 'esm' })
const M = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

// Priority: severity derives P2/P3/P4; P1 only when a rule says so.
{
  assert.equal(M.derivePriority('critical'), 'P2')
  assert.equal(M.derivePriority('warning'), 'P3')
  assert.equal(M.derivePriority('info'), 'P4')
  assert.equal(M.derivePriority(undefined), 'P3')
  for (const sev of ['critical', 'warning', 'info']) assert.notEqual(M.derivePriority(sev), 'P1', 'P1 is never derived')
  assert.equal(M.effectivePriority('P1', 'warning'), 'P1')
  assert.equal(M.effectivePriority(null, 'critical'), 'P2')
  assert.equal(M.effectivePriority('p4', 'critical'), 'P4')
  assert.equal(M.normalizePriority(2), 'P2')
  assert.equal(M.normalizePriority('P5'), null)
  assert.match(M.autoPriorityText('critical'), /P2 High/)
  assert.deepEqual(M.PRIORITIES, ['P1', 'P2', 'P3', 'P4'])
  assert.equal(M.PRIORITY_META.P1.label, 'P1 Critical')
  assert.equal(M.PRIORITY_META.P4.label, 'P4 Information')
  assert(M.isUrgent('P1') && M.isUrgent('P2') && !M.isUrgent('P3') && !M.isUrgent('P4'))
}

// Response targets: P-keys win, legacy severity keys still read (critical -> P2 ...), defaults per the matrix.
{
  assert.deepEqual(M.targetsFromApi({}), { P1: 5, P2: 15, P3: 60, P4: null })
  assert.deepEqual(M.targetsFromApi({ critical: 20, warning: 90, info: null }), { P1: 5, P2: 20, P3: 90, P4: null })
  assert.deepEqual(M.targetsFromApi({ P1: 3, P2: 10, critical: 99, P3: null, P4: 240 }), { P1: 3, P2: 10, P3: null, P4: 240 })
  assert.equal(M.validateTargetMinutes(''), null)
  assert.equal(M.validateTargetMinutes('15'), null)
  assert(M.validateTargetMinutes('0') && M.validateTargetMinutes('1.5') && M.validateTargetMinutes('10081'))
}

// Durations.
{
  assert.equal(M.fmtMinutes(null), '—')
  assert.equal(M.fmtMinutes(0), '0 min')
  assert.equal(M.fmtMinutes(5), '5 min')
  assert.equal(M.fmtMinutes(60), '1 h')
  assert.equal(M.fmtMinutes(90), '1 h 30 min')
  assert.equal(M.fmtMinutes(240), '4 h')
  assert.equal(M.fmtMinutes(1440), '1 d')
  assert.equal(M.fmtMinutes(1800), '1 d 6 h')
  assert.equal(M.fmtOffset(0), 'At trigger')
  assert.equal(M.fmtOffset(15), '+15 min')
  assert.equal(M.fmtSeconds(60), '1 min')
  assert.equal(M.fmtSeconds(45), '45 s')
}

const SCHED = '11111111-1111-4111-8111-111111111111'
const BACKUP = '22222222-2222-4222-8222-222222222222'
const LEAD = '33333333-3333-4333-8333-333333333333'
const NOC = '44444444-4444-4444-8444-444444444444'
const TEAM = '55555555-5555-4555-8555-555555555555'
const DIR = {
  schedules: [{ id: SCHED, name: 'Network primary', enabled: true, members: 2 }, { id: BACKUP, name: 'Network backup', enabled: true, members: 0 }],
  users: [{ id: LEAD, name: 'Lina Lead', username: 'lina' }],
  channels: [{ id: NOC, name: 'NOC mailbox', type: 'email', enabled: true }, { id: TEAM, name: 'Team SMS', type: 'sms', enabled: false }],
}

// Targets: labels from the directory, embedded names, deleted targets, de-duplication and the picker.
{
  assert.equal(M.describeTarget({ type: 'schedule', id: SCHED }, DIR).label, 'Network primary')
  assert.equal(M.describeTarget({ type: 'schedule', id: BACKUP }, DIR).detail, 'No members')
  assert.equal(M.describeTarget({ type: 'channel', id: TEAM }, DIR).disabled, true)
  assert.equal(M.describeTarget({ type: 'user', id: 'gone' }, DIR).missing, true)
  assert.equal(M.describeTarget({ type: 'channel', id: NOC, name: 'Embedded' }, M.EMPTY_DIRECTORY).label, 'Embedded')
  assert.equal(M.describeTarget({ type: 'channel', id: NOC }, M.EMPTY_DIRECTORY).label, 'Channel', 'a read-only role without the channel directory sees the kind, not "deleted"')
  const once = M.addTarget(M.addTarget([], { type: 'user', id: LEAD }), { type: 'user', id: LEAD, name: 'x' })
  assert.deepEqual(once, [{ type: 'user', id: LEAD }])
  assert.deepEqual(M.removeTarget(once, { type: 'user', id: LEAD }), [])
  assert.deepEqual(M.pickerOptions(DIR, 'noc').map((o) => o.id), [NOC])
  assert.deepEqual(M.pickerOptions(DIR, '', ['schedule']).map((o) => o.id), [SCHED, BACKUP])
  assert.deepEqual(M.pickerOptions(DIR, '', ['schedule'], [{ type: 'schedule', id: SCHED }]).map((o) => o.id), [BACKUP], 'added targets leave the picker')
  assert.equal(M.normalizeTarget({ type: 'team', id: 'x' }), null)
}

// Policies: API -> draft -> payload (steps saved in delay order, positions renumbered), dirty, validation.
const API_POLICY = {
  id: 'pol-1', name: 'P1 Critical — major incident', description: 'Major outage', enabled: true, default_priorities: ['P1'],
  ack_target_minutes: 5, repeat_minutes: 5, max_repeats: 2, review_minutes: 15, ack_expiry_minutes: 60,
  fallback_targets: [{ type: 'channel', id: NOC }],
  steps: [
    { position: 0, delay_minutes: 0, name: 'Primary on-call and NOC', targets: [{ type: 'schedule', id: SCHED }, { type: 'channel', id: NOC }] },
    { position: 1, delay_minutes: 5, name: 'Backup on-call', targets: [{ type: 'schedule', id: BACKUP }] },
    { position: 2, delay_minutes: 15, name: 'Incident lead', targets: [{ type: 'user', id: LEAD }] },
  ],
  used_by_rules: 3,
}
{
  const p = M.policyFromApi(API_POLICY)
  assert.equal(p.steps.length, 3)
  assert.equal(p.used_by_rules, 3)
  assert.deepEqual(p.default_priorities, ['P1'])
  assert.equal(M.stepSummary(p.steps), '3 steps · 0 → 5 min → 15 min')
  const d = M.draftFromPolicy(p)
  assert.equal(M.policyDirty(d, M.draftFromPolicy(p)), false, 'a fresh draft is clean (step keys ignored)')
  const body = M.policyPayload(d)
  assert.deepEqual(body.steps.map((s) => [s.position, s.delay_minutes]), [[0, 0], [1, 5], [2, 15]])
  assert.equal(body.review_minutes, 15)
  assert.deepEqual(body.fallback_targets, [{ type: 'channel', id: NOC }])
  // Reordered rows are saved in delay order.
  const shuffled = { ...d, steps: [d.steps[2], d.steps[0], d.steps[1]] }
  assert.deepEqual(M.policyPayload(shuffled).steps.map((s) => s.delay_minutes), [0, 5, 15])
  assert.equal(M.policyDirty(shuffled, d), false, 'row order alone is not a change')
  assert.equal(M.policyDirty({ ...d, review_minutes: '30' }, d), true)
  assert.deepEqual(M.validatePolicy(d), {})
  // Empty fields are "none", not zero.
  const none = M.policyPayload({ ...d, ack_target_minutes: '', repeat_minutes: ' ', review_minutes: '', ack_expiry_minutes: '' })
  assert.equal(none.ack_target_minutes, null)
  assert.equal(none.repeat_minutes, null)
  assert.equal(none.review_minutes, null)
}
{
  const d = M.emptyPolicyDraft()
  assert.equal(d.steps.length, 1)
  assert.equal(d.steps[0].delay_minutes, '0')
  const e = M.validatePolicy(d)
  assert.equal(e.name, 'Name the policy')
  assert.match(e.stepErrors[d.steps[0].key], /at least one schedule, user or channel/)
  assert(M.hasErrors(e))
  const two = { ...d, name: 'x', steps: [{ ...d.steps[0], targets: [{ type: 'user', id: LEAD }] }, { key: 'k2', delay_minutes: '0', name: '', targets: [{ type: 'user', id: LEAD }] }] }
  assert.match(M.validatePolicy(two).stepErrors.k2, /same delay/, 'two steps due at the same minute are refused (the server requires increasing delays)')
  // Server bounds: review 5..1440, expiry 5..10080, max repeats 0..20, delay 0..10080.
  assert(M.validatePolicy({ ...two, steps: [two.steps[0]], review_minutes: '4' }).review_minutes)
  assert(M.validatePolicy({ ...two, steps: [two.steps[0]], review_minutes: '1441' }).review_minutes)
  assert.equal(M.validatePolicy({ ...two, steps: [two.steps[0]], review_minutes: '15' }).review_minutes, undefined)
  assert(M.validatePolicy({ ...two, steps: [two.steps[0]], ack_expiry_minutes: '3' }).ack_expiry_minutes)
  assert(M.validatePolicy({ ...two, steps: [two.steps[0]], max_repeats: '21' }).max_repeats)
  assert(M.validatePolicy({ ...two, steps: [two.steps[0]], repeat_minutes: '1441' }).repeat_minutes)
  assert(M.validatePolicy({ ...two, steps: [{ ...two.steps[0], delay_minutes: '10081' }] }).stepErrors)
  assert(!M.hasErrors(M.validatePolicy({ ...two, steps: [two.steps[0]] })))
  // Next step delay: 15 after the initial page, then doubling.
  const audit = { ...d, name: 'P4 Information — audit only', steps: [] }
  assert(!M.hasErrors(M.validatePolicy(audit)), 'a policy without steps (audit-only P4) can be saved, as the server allows')
  assert(M.draftOutline(audit, DIR).warnings.some((w) => /pages nobody/.test(w)))
  assert.equal(M.nextStepDelay({ ...d, steps: [{ ...d.steps[0], delay_minutes: '0' }] }), 15)
  assert.equal(M.nextStepDelay({ ...d, steps: [{ ...d.steps[0], delay_minutes: '15' }] }), 30)
}

// The live outline: names, gaps (no targets, disabled channel, empty schedule) and warnings.
{
  const d = M.draftFromPolicy(M.policyFromApi({ ...API_POLICY, fallback_targets: [], review_minutes: null }))
  d.steps.push({ key: 'x', delay_minutes: '30', name: '', targets: [{ type: 'channel', id: TEAM }] })
  const o = M.draftOutline(d, DIR)
  assert.deepEqual(o.steps.map((s) => s.delay), [0, 5, 15, 30])
  assert.equal(o.steps[3].name, 'Step 4')
  assert(o.steps[1].gaps.some((g) => /Network backup has no members/.test(g)))
  assert(o.steps[3].gaps.some((g) => /Team SMS is disabled/.test(g)))
  assert(o.warnings.some((w) => /No fallback route/.test(w)))
  assert(o.warnings.some((w) => /P1\/P2 policies normally send unresolved-incident updates/.test(w)))
  const late = M.draftOutline({ ...d, steps: [{ key: 'a', delay_minutes: '10', name: '', targets: [] }] }, DIR)
  assert(late.warnings.some((w) => /first step fires after 10 min/.test(w)))
  assert(late.steps[0].gaps.some((g) => /nobody is notified/.test(g)))
}

// Routing: the enabled default for a priority, first by name; conflicts are reported.
{
  const pols = [
    M.policyFromApi({ ...API_POLICY, id: 'b', name: 'B policy', default_priorities: ['P2', 'P3'] }),
    M.policyFromApi({ ...API_POLICY, id: 'a', name: 'A policy', default_priorities: ['P3'] }),
    M.policyFromApi({ ...API_POLICY, id: 'c', name: 'C disabled', enabled: false, default_priorities: ['P1'] }),
  ]
  assert.equal(M.policyForPriority(pols, 'P3').id, 'a')
  assert.equal(M.policyForPriority(pols, 'P2').id, 'b')
  assert.equal(M.policyForPriority(pols, 'P1'), null, 'a disabled policy routes nothing')
  assert.deepEqual(M.priorityConflicts(pols), [{ priority: 'P3', policies: ['A policy', 'B policy'] }])
}

// Dry run / route explanation (escalation_policy_service.preview + alert_escalation_service.explain_route shapes).
const ROUTE = {
  alert_id: 'al-1', priority: 'P1', status: 'active',
  policy: { id: 'pol-1', name: 'P1 Critical — major incident', source: 'priority_default' },
  notes: ['The rule is shared by 12 devices.'],
  state: { step: 0, repeats: 1, acknowledged: false, next_at: '2026-10-18T10:05:00Z', next_action: 'step:1' },
  steps: [
    { position: 0, name: 'Primary on-call and NOC', at_offset_minutes: 0, current: true, notified_at: '2026-10-18T10:00:05Z',
      recipients: [{ kind: 'email', address: 'a***@example.com', user_id: 'u1', user_name: 'Omar Ops', via: 'schedule:Network primary' },
        { kind: 'email', address: null, via: 'channel:NOC mailbox', channel_id: NOC }], gaps: [] },
    { position: 1, name: 'Backup on-call', at_offset_minutes: 5, recipients: [], gaps: ['Nobody is on call in Network backup at 2026-10-18 13:05 +03'],
      uses_fallback: true, fallback_recipients: [{ kind: 'email', address: null, via: 'channel:NOC mailbox' }] },
  ],
  review: { every_minutes: 15, recipients: 'The incident owner and the last notified step', continues_after_acknowledgement: true },
  warnings: ['Step 2 reaches nobody at 2026-10-18 10:05 UTC; the fallback route would be used.'],
}
{
  const r = M.routeFromApi(ROUTE)
  assert.equal(r.policy.source, 'priority_default')
  assert.equal(r.priority, 'P1')
  assert.equal(r.state.step, 0)
  assert.deepEqual(r.warnings, ['The rule is shared by 12 devices.', 'Step 2 reaches nobody at 2026-10-18 10:05 UTC; the fallback route would be used.'], 'server notes and warnings are both shown')
  assert.equal(M.currentStep(r).name, 'Primary on-call and NOC')
  assert.equal(r.steps[1].uses_fallback, true)
  assert.equal(M.nextActionLabel('step:1', r.steps), 'Step 2 (Backup on-call)')
  assert.equal(M.nextActionLabel('repeat:0', r.steps), 'Repeat of step 1 (Primary on-call and NOC)')
  assert.equal(M.nextActionLabel('review:3'), 'Unresolved-incident update 3')
  assert.equal(M.nextActionLabel('ack_expiry'), 'Acknowledgement expiry (re-opens escalation)')
  assert.equal(M.nextActionLabel('level:2'), 'Escalation level 2')
  assert.equal(M.nextEscalationText(r, (iso) => `<${iso}>`), 'Step 2 (Backup on-call) at <2026-10-18T10:05:00Z>.')
  assert.match(M.reviewText(r.review), /every 15 min to the incident owner and the last notified step, also after acknowledgement/)
  assert.equal(M.recipientLabel(r.steps[0].recipients[0]), 'Omar Ops <a***@example.com>')
  assert.equal(M.recipientLabel(r.steps[0].recipients[1]), 'NOC mailbox', 'a channel recipient is named by its channel')
  assert.equal(M.viaLabel('schedule:Network primary'), 'On call: Network primary')
  // Not started: policy step -1.
  const fresh = M.routeFromApi({ ...ROUTE, state: { ...ROUTE.state, step: -1 }, steps: ROUTE.steps.map((s) => ({ ...s, current: false })) })
  assert.equal(fresh.state.step, null)
  assert.equal(M.currentStep(fresh), null)
  // Acknowledged with nothing scheduled; no route at all.
  const acked = M.routeFromApi({ ...ROUTE, state: { ...ROUTE.state, acknowledged: true, next_at: null, next_action: null } })
  assert.match(M.nextEscalationText(acked, String), /Acknowledged — step escalation stopped/)
  assert.match(M.nextEscalationText(M.routeFromApi({ policy: { source: 'none' }, steps: [] }), String), /no escalation route/)
  const legacy = M.routeFromApi({ policy: { id: null, name: null, source: 'legacy_levels' }, state: { step: 0, repeats: 0, acknowledged: false, next_at: null },
    steps: [{ position: 1, name: 'Level 1', at_offset_minutes: 15, recipients: [], gaps: [] }] })
  assert.equal(legacy.policy.source, 'legacy_levels')
  assert.equal(M.ROUTE_SOURCE_LABEL.legacy_levels, 'Legacy escalation tiers on the rule')
  // A policy preview with channel recipients, review and fallback gaps.
  const pv = M.previewFromApi({ steps: ROUTE.steps, review: null, repeat: { every_minutes: 5, max_repeats: 2 }, fallback: [], fallback_gaps: ['Notification channel X is disabled.'], warnings: ['w'] })
  assert.equal(pv.repeat.every_minutes, 5)
  assert.deepEqual(pv.fallback_gaps, ['Notification channel X is disabled.'])
  assert.equal(M.reviewText(null), null)
}

// The email "Acknowledge" link: ?ack=1 only asks; the state decides what the dialog offers.
{
  assert.equal(M.ackRequested('?ack=1'), true)
  assert.equal(M.ackRequested('ack=true'), true)
  assert.equal(M.ackRequested(new URLSearchParams('tab=x&ack=1')), true)
  assert.equal(M.ackRequested('?ack=0'), false)
  assert.equal(M.ackRequested(''), false)
  assert.equal(M.ackDialogState({ status: 'active' }, true), 'confirm')
  assert.equal(M.ackDialogState({ status: 'acknowledged' }, true), 'already')
  assert.equal(M.ackDialogState({ status: 'resolved' }, true), 'closed')
  assert.equal(M.ackDialogState({ status: 'pending' }, true), 'pending')
  assert.equal(M.ackDialogState({ status: 'active' }, false), 'forbidden', 'without alerts.acknowledge there is no confirm button')
  assert.equal(M.withoutAck('?ack=1'), '')
  assert.equal(M.withoutAck('?x=1&ack=1'), '?x=1')
}

// Time zones: wall time in a zone <-> UTC, across DST.
{
  assert.equal(M.zonedWallToUtc('2026-10-19', '09:00', 'Asia/Riyadh'), '2026-10-19T06:00:00.000Z')
  assert.deepEqual(M.utcToZonedWall('2026-10-19T06:00:00Z', 'Asia/Riyadh'), { date: '2026-10-19', time: '09:00' })
  assert.equal(M.zonedWallToUtc('2026-07-01', '09:00', 'America/New_York'), '2026-07-01T13:00:00.000Z', 'EDT')
  assert.equal(M.zonedWallToUtc('2026-12-01', '09:00', 'America/New_York'), '2026-12-01T14:00:00.000Z', 'EST')
  assert.deepEqual(M.utcToZonedWall('2026-12-01T14:00:00Z', 'America/New_York'), { date: '2026-12-01', time: '09:00' })
  assert.equal(M.zonedWallToUtc('2026-10-19', '', 'UTC'), null)
  assert.equal(M.zonedWallToUtc('bad', '09:00', 'UTC'), null)
}

// Schedules: API -> draft -> payload (layer order = position; handoff in the schedule zone), validation, rotation.
{
  const api = {
    id: 's1', name: 'Network primary', description: null, timezone: 'Asia/Riyadh', enabled: true,
    layers: [{ id: 'l1', name: 'Weekly', position: 0, shift_hours: 168, handoff_at: '2026-10-19T06:00:00Z', members: ['u1', 'u2'], member_names: ['A', 'B'], restrict_to: [] },
      { id: 'l2', name: 'Business hours', position: 1, shift_hours: 24, handoff_at: '2026-10-19T05:00:00Z', members: ['u3'], restrict_to: [{ days: [4, 0, 1, 2, 3], start: '08:00', end: '17:00' }] }],
    overrides: [{ id: 'o1', user_id: 'u3', user_name: 'C', starts_at: '2026-10-20T00:00:00Z', ends_at: '2026-10-21T00:00:00Z', reason: 'Leave' }],
    now: { users: [{ id: 'u1', name: 'A' }], until: '2026-10-26T06:00:00Z' }, used_by: ['P1 Critical'],
  }
  const s = M.scheduleFromApi(api)
  assert.equal(M.memberCount(s), 3)
  assert.deepEqual(s.now.users, [{ id: 'u1', name: 'A' }])
  assert.deepEqual(s.used_by, ['P1 Critical'])
  assert.deepEqual(s.layers[1].restrict_to[0].days, [0, 1, 2, 3, 4])
  assert.equal(M.restrictionText(s.layers[1].restrict_to[0]), 'Mon–Fri 08:00–17:00')
  const d = M.draftFromSchedule(s)
  assert.equal(d.layers[0].handoff_time, '09:00', 'handoff shown in the schedule zone')
  assert.equal(d.layers[0].rotation, 'weekly')
  assert.equal(d.layers[1].rotation, 'daily')
  assert.equal(M.scheduleDirty(d, M.draftFromSchedule(s)), false)
  const body = M.schedulePayload(d)
  assert.equal(body.layers[0].handoff_at, '2026-10-19T06:00:00.000Z')
  assert.deepEqual(body.layers.map((l) => [l.id, l.position]), [['l1', 0], ['l2', 1]])
  const moved = { ...d, layers: M.moveItem(d.layers, 1, -1) }
  assert.deepEqual(M.schedulePayload(moved).layers.map((l) => [l.id, l.position]), [['l2', 0], ['l1', 1]], 'moving a layer changes which one wins')
  assert.equal(M.scheduleDirty(moved, d), true)
  assert.deepEqual(M.validateSchedule(d), {})
  const bad = { ...d, name: ' ', timezone: 'Mars/Base', layers: [{ ...d.layers[0], members: [] }] }
  const e = M.validateSchedule(bad)
  assert(e.name && e.timezone && e.layerErrors[d.layers[0].key])
  assert(M.hasScheduleErrors(e))
  assert(M.validateSchedule({ ...d, layers: [{ ...d.layers[0], shift_hours: '0' }] }).layerErrors)
  assert.deepEqual(M.moveItem([1, 2, 3], 0, -1), [1, 2, 3])
  assert.deepEqual(M.moveItem([1, 2, 3], 0, 1), [2, 1, 3])
  // Rotation: members take turns every shift from the handoff.
  const r = M.rotationOrder(['a', 'b'], 24, '2026-10-01T00:00:00Z', Date.parse('2026-10-03T12:00:00Z'), 3)
  assert.deepEqual(r.map((x) => x.user_id), ['a', 'b', 'a'])
  assert.equal(new Date(r[0].start).toISOString(), '2026-10-03T00:00:00.000Z')
  assert.equal(M.rotationLabel(168), 'Weekly rotation')
  assert.equal(M.rotationLabel(12), 'Every 12 h')
  const fresh = M.emptyScheduleDraft('Asia/Riyadh', Date.parse('2026-10-18T22:30:00Z'))
  assert.equal(fresh.layers[0].handoff_date, '2026-10-19', 'the default handoff date is today in the schedule zone')
}

// The 14-day timeline: clipped bars, one row per layer, overrides first, gaps, stable colours.
{
  const from = Date.parse('2026-10-18T00:00:00Z')
  const to = from + 14 * 86400_000
  const shifts = M.shiftsFromApi({ shifts: [
    { start: '2026-10-17T00:00:00Z', end: '2026-10-25T00:00:00Z', user_id: 'u1', user_name: 'A', layer: 'Weekly', override: false },
    { start: '2026-10-25T00:00:00Z', end: '2026-11-03T00:00:00Z', user_id: 'u2', user_name: 'B', layer: 'Weekly', override: false },
    { start: '2026-10-20T00:00:00Z', end: '2026-10-21T00:00:00Z', user_id: 'u3', user_name: 'C', layer: null, override: true },
    { start: 'junk', end: '2026-10-21T00:00:00Z', user_id: 'x' },
  ] })
  assert.equal(shifts.length, 3, 'invalid shifts are dropped')
  const rows = M.layoutShifts(shifts, from, to)
  assert.deepEqual(rows.map((r) => r.label), ['Overrides', 'Weekly'])
  const weekly = rows[1].bars
  assert.equal(weekly[0].leftPct, 0, 'a shift that began earlier is clipped to the window')
  assert.ok(Math.abs(weekly[0].widthPct - (7 / 14) * 100) < 1e-9)
  assert.ok(Math.abs(weekly[1].leftPct + weekly[1].widthPct - 100) < 1e-9, 'a shift past the window is clipped too')
  assert.equal(M.colorIndex('u1'), M.colorIndex('u1'))
  const colours = new Set(rows.flatMap((r) => r.bars.map((b) => `${b.user_id}:${b.colorIndex}`)))
  assert.equal(new Set([...colours].map((c) => c.split(':')[1])).size, 3, 'each person on the timeline has their own colour')
  const uuids = ['33333333-3333-4333-8333-333333333333', '66666666-6666-4666-8666-666666666666', '77777777-7777-4777-8777-777777777777']
  assert(new Set(uuids.map(M.colorIndex)).size > 1, 'similar UUIDs do not share one colour')
  assert.deepEqual(M.coverageGapsInWindow(shifts, from, to), [], 'continuous coverage has no gap')
  const short = shifts.map((x) => (x.user_id === 'u2' ? { ...x, end: '2026-10-30T00:00:00Z' } : x))
  assert.deepEqual(M.coverageGapsInWindow(short, from, to).map((g) => [new Date(g.start).toISOString(), new Date(g.end).toISOString()]), [['2026-10-30T00:00:00.000Z', '2026-11-01T00:00:00.000Z']], 'nobody on call after the last shift')
  assert.equal(M.dayTicks(from, 14).length, 14)
  assert.equal(M.layoutShifts([], from, to).length, 0)
}

// Grouping: OFF by default on upgrade, recommended preset, server bounds, the sentence says what is never held.
{
  const g = M.groupingFromApi({ enabled: false, group_by: ['site'], group_wait_seconds: 60, group_interval_seconds: 300, max_items: 50, budget_per_recipient_per_hour: null, critical_reserve: true, recommended: { enabled: true, group_by: ['site', 'event_family'], group_wait_seconds: 60, group_interval_seconds: 300, max_items: 50, budget_per_recipient_per_hour: 30, critical_reserve: true } })
  assert.equal(g.settings.enabled, false)
  assert.equal(g.settings.budget_per_recipient_per_hour, 0, 'null budget = no budget')
  assert.deepEqual(g.recommended.group_by, ['site', 'event_family'])
  assert.equal(g.recommended.enabled, true)
  assert.equal(M.groupingFromApi({}).settings.enabled, false)
  assert.deepEqual(M.validateGrouping(g.recommended), {})
  assert(M.validateGrouping({ ...g.recommended, group_by: [] }).group_by)
  assert(M.validateGrouping({ ...g.recommended, group_interval_seconds: 10 }).group_interval_seconds, 'interval at least 30 s')
  assert(M.validateGrouping({ ...g.recommended, max_items: 2 }).max_items, 'at least 5 items')
  assert.match(M.groupingSentence(g.recommended), /P1\/P2, recoveries, escalations and reviews are never held/)
  assert.match(M.groupingSentence(g.settings), /Grouping is off/)
  assert.equal(M.holdReasonLabel('grouping'), 'Grouped with related warnings')
  assert.equal(M.holdReasonLabel('budget'), 'Recipient message budget reached')
  const dd = M.digestDetailFromApi({ id: 'd1', group_key: 'site:riyadh|memory', group_label: 'Riyadh · memory', status: 'open', reason: 'grouping', release_condition: 'Sent 60 s after the first event', release_at: '2026-10-18T10:01:00Z', item_count: 2, status_detail: null,
    items: [{ id: 'i1', alert_id: 'a1', event: 'trigger', severity: 'warning', asset: 'server-a', condition: 'Memory 90.4% > 90%', already_recovered: false, priority: 'P3' },
      { id: 'i2', alert_id: 'a2', event: 'trigger', severity: 'warning', asset: 'server-b', condition: 'Memory 91%', alert_status: 'resolved', already_recovered: true }] })
  assert.equal(dd.items[0].message, 'server-a — Memory 90.4% > 90%')
  assert.equal(dd.items[1].already_recovered, true)
  assert.equal(dd.items[0].priority, 'P3')
  assert.equal(M.DIGEST_STATUS.open.label, 'Holding')
  assert.deepEqual(M.holdsFromApi([{ digest_id: 'd1', reason: 'budget', release_condition: 'x', release_at: null, status: 'open' }]).map((h) => h.reason), ['budget'])
}

// Coverage: actionable issues, worst first; P4 without paging is by design; no duplicate unrouted lines.
{
  const c = M.coverageFromApi({
    priorities: {
      P1: { policy_id: 'pol-1', policy_name: 'P1 policy', steps_ok: false, gaps: ['Step 2 reaches nobody now and there is no working fallback.'] },
      P2: { policy_id: null, policy_name: null, steps_ok: false, gaps: ['No escalation policy is the default for P2 High; …'] },
      P3: { policy_id: 'pol-3', policy_name: 'P3 policy', steps_ok: true, gaps: [] },
      P4: { policy_id: null, policy_name: null, steps_ok: false, gaps: ['No escalation policy is the default for P4 Information'] },
    },
    unrouted_priorities: ['P2', 'P4'],
    schedules_without_members: [{ id: BACKUP, name: 'Network backup' }],
    schedule_gaps: [{ schedule_id: SCHED, schedule_name: 'Network primary', start: '2026-10-20T00:00:00Z', end: '2026-10-20T06:00:00Z' }],
    users_without_contacts: [{ id: LEAD, name: 'Lina Lead', reason: 'No email or SMS contact method' }],
  })
  const issues = M.coverageIssues(c)
  assert.equal(issues[0].severity, 'danger')
  assert.equal(issues.filter((i) => /P2 High/.test(i.text)).length, 1, 'an unrouted priority is one line, not two')
  assert(!issues.some((i) => /P4/.test(i.text)), 'P4 without paging is the recommended design')
  assert(issues.some((i) => i.fix.tab === 'oncall' && i.fix.id === BACKUP))
  assert(issues.some((i) => i.fix.tab === 'oncall' && /Nobody is on call in “Network primary”/.test(i.text)))
  assert(issues.some((i) => i.fix.tab === 'profile' && i.fix.id === LEAD))
  assert(issues.some((i) => i.fix.tab === 'policies' && i.fix.id === 'pol-1'))
  assert.deepEqual(M.priorityCoverageStatus('P3', c, M.policyFromApi({ ...API_POLICY, id: 'pol-3' })), { tone: 'success', label: 'Covered' })
  assert.equal(M.priorityCoverageStatus('P2', c, null).label, 'Unrouted')
  assert.equal(M.priorityCoverageStatus('P4', c, null).label, 'No paging (by design)')
  assert.equal(M.priorityCoverageStatus('P1', c, M.policyFromApi(API_POLICY)).tone, 'danger')
}

// Contact methods and the recommended matrix.
{
  assert.equal(M.validateContact('email', 'ops@example.com'), null)
  assert(M.validateContact('email', 'not-an-email'))
  assert.equal(M.validateContact('sms', '+966 50 123 4567'), null)
  assert(M.validateContact('sms', 'call me'))
  assert(M.sameContact({ kind: 'email', address: 'Ops@Example.com ' }, { kind: 'email', address: 'ops@example.com' }))
  assert(M.sameContact({ kind: 'sms', address: '+966 50-123' }, { kind: 'sms', address: '+96650123' }))
  assert(!M.sameContact({ kind: 'sms', address: '1' }, { kind: 'email', address: '1' }))
  assert.deepEqual(M.contactsFromApi({ methods: [{ id: 'm1', kind: 'sms', address: '+1', label: null, enabled: false }] }), [{ id: 'm1', kind: 'sms', address: '+1', label: null, enabled: false }])
  assert.deepEqual(M.contactsPayload([{ kind: 'email', address: ' a@b.co ', label: ' ', enabled: true }]), [{ kind: 'email', address: 'a@b.co', label: null, enabled: true }])

  const f = { primary_schedule_id: SCHED, backup_schedule_id: BACKUP, lead_user_id: LEAD, noc_channel_ids: [NOC], team_channel_ids: [TEAM], replace_defaults: false }
  assert.equal(M.validateRecommended(f), null)
  assert(M.validateRecommended({ ...f, primary_schedule_id: '', noc_channel_ids: [], team_channel_ids: [] }))
  assert.equal(M.validateRecommended({ ...f, primary_schedule_id: '' }), null, 'channels alone are enough (the server accepts them)')
  assert(M.validateRecommended({ ...f, backup_schedule_id: SCHED }))
  assert.deepEqual(M.recommendedPayload({ ...f, backup_schedule_id: '', lead_user_id: '' }), { replace_defaults: false, primary_schedule_id: SCHED, backup_schedule_id: null, lead_user_id: null, noc_channel_ids: [NOC], team_channel_ids: [TEAM] })
  const names = (t) => M.describeTarget(t, DIR).label
  const plan = M.recommendedPlan(f, names)
  assert.deepEqual(plan.map((p) => [p.priority, p.ack, p.review]), [['P1', 5, 15], ['P2', 15, 30], ['P3', 60, null], ['P4', null, null]])
  assert.deepEqual(plan[0].steps.map((s) => s.delay), [0, 5, 15, 30])
  assert.equal(plan[0].steps[0].who, 'Network primary + NOC mailbox')
  assert.deepEqual(plan[2].steps.map((s) => s.delay), [0, 240], 'P3: team queue, then the lead after 4 hours')
  assert.deepEqual(M.recommendedPlan({ ...f, backup_schedule_id: '', lead_user_id: '' }, names)[0].steps.map((s) => s.delay), [0, 30], 'steps without a target are left out')
  assert.equal(plan[3].steps.length, 0, 'P4: no paging')
}

assert.equal(M.tabFromSearch('?tab=oncall'), 'oncall')
assert.equal(M.tabFromSearch('?tab=nope'), 'matrix')
assert.deepEqual(M.ESCALATION_TABS, ['matrix', 'policies', 'oncall', 'targets', 'grouping', 'coverage'])

console.log('Escalation model: priorities (P1 never derived), response targets, policies and server limits, outline, routing and conflicts, route explanation, ?ack=1 state, time zones and DST, timeline, grouping, coverage, contacts, recommended matrix.')

/* ═══ (b)–(d) server rendering with the app's own alias ════════════════════════════════════ */

const entry = `
export { MatrixTab, MatrixView } from '@/pages/escalation/MatrixTab'
export { PolicyEditorBody } from '@/pages/escalation/PoliciesTab'
export { AckConfirmBody } from '@/components/alerts/RoutePanel'
export { Dialog } from '@/components/ui/Dialog'
export { AlertDetailPage } from '@/pages/AlertDetailPage'
export { AlertRuleWizardDialog } from '@/components/forms/AlertRuleWizardDialog'
export { EscalationPage } from '@/pages/escalation/EscalationPage'
export * as EM from '@/features/escalation/model'
export { api } from '@/lib/api'
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
  loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty', '.woff2': 'empty' },
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const h = m.createElement

// Every request the rendered tree might send is recorded; rendering must send none.
const calls = []
for (const verb of ['get', 'post', 'put', 'delete', 'patch']) {
  m.api[verb] = (...args) => { calls.push([verb, args[0]]); return new Promise(() => {}) }
}

const USER = (permissions) => ({ id: 'me', username: 'me', email: 'me@example.com', role: 'custom', permissions })
function setUser(user) {
  m.useAuth.setState({ user, token: 't' })
  const initial = typeof m.useAuth.getInitialState === 'function' ? m.useAuth.getInitialState() : null
  if (initial) Object.assign(initial, { user, token: 't' })
}
const newClient = () => new m.QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false, staleTime: Infinity } } })
const renderAt = (qc, url, element) =>
  m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: [url] }, h(m.Routes, null, h(m.Route, { path: '*', element })))))
const strip = (html) => html.replace(/<[^>]*>/g, ' ').replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/\s+/g, ' ').trim()

const POLICIES = [
  M.policyFromApi(API_POLICY),
  M.policyFromApi({ ...API_POLICY, id: 'pol-3', name: 'P3 Warning — team queue', default_priorities: ['P3'], ack_target_minutes: 60, repeat_minutes: null, review_minutes: null, ack_expiry_minutes: null,
    steps: [{ position: 0, delay_minutes: 0, name: 'Owning team queue', targets: [{ type: 'channel', id: NOC }] }, { position: 1, delay_minutes: 240, name: 'Team lead', targets: [{ type: 'user', id: LEAD }] }] }),
]
const COVERAGE = M.coverageFromApi({
  priorities: {
    P1: { policy_id: 'pol-1', policy_name: API_POLICY.name, steps_ok: true, gaps: [] },
    P2: { policy_id: null, policy_name: null, steps_ok: false, gaps: ['No escalation policy is the default for P2 High'] },
    P3: { policy_id: 'pol-3', policy_name: 'P3 Warning — team queue', steps_ok: true, gaps: ['Step 2: Lina Lead has no email or SMS contact method.'] },
  },
  unrouted_priorities: ['P2', 'P4'],
})
const TARGETS = M.targetsFromApi({ P1: 5, P2: 15, P3: 60, P4: null })

function seed(qc) {
  qc.setQueryData(['escalation', 'policies'], POLICIES)
  qc.setQueryData(['escalation', 'coverage'], COVERAGE)
  qc.setQueryData(['alerts', 'response-targets'], TARGETS)
  qc.setQueryData(['escalation', 'schedules'], [M.scheduleFromApi({ id: SCHED, name: 'Network primary', timezone: 'Asia/Riyadh', layers: [{ name: 'Weekly', shift_hours: 168, handoff_at: '2026-10-19T06:00:00Z', members: ['u1'] }] }),
    M.scheduleFromApi({ id: BACKUP, name: 'Network backup', timezone: 'Asia/Riyadh', layers: [] })])
  qc.setQueryData(['alerts', 'assignees'], [{ id: LEAD, name: 'Lina Lead', username: 'lina' }])
  qc.setQueryData(['alerts', 'channel-directory'], { data: [{ id: NOC, name: 'NOC mailbox', type: 'email', enabled: true }] })
}

// (b) Matrix tab, rendered through its queries.
{
  setUser(USER(['alerts.view', 'alerts.manage']))
  const qc = newClient()
  seed(qc)
  calls.length = 0
  const html = renderAt(qc, '/escalation', h(m.MatrixTab, { canManage: true, go: () => {} }))
  const text = strip(html)
  for (const p of ['P1 Critical', 'P2 High', 'P3 Warning', 'P4 Information']) assert(text.includes(p), `matrix card ${p}`)
  assert(text.includes('P1 Critical — major incident'), 'P1 card names its default policy')
  assert(text.includes('Within 5 min') && text.includes('Within 15 min') && text.includes('Within 1 h'), 'acknowledgement targets per priority')
  assert(text.includes('No target'), 'P4 has no acknowledgement target')
  assert(text.includes('+5 min') && text.includes('+15 min') && text.includes('+4 h'), 'step timeline with delays')
  assert(text.includes('Updates every 15 min while unresolved'), 'review cadence')
  assert(text.includes('Re-notifies the current step every 5 min, up to 2×'))
  assert(text.includes('Acknowledgement expires after 1 h'))
  assert(text.includes('No default policy') && text.includes('Recommended: Owning service team and NOC'), 'an unrouted priority shows the report recommendation')
  assert(text.includes('Unrouted priorities: P2, P3') === false && text.includes('Unrouted priorities: P2.'), 'P2 is unrouted; P4 needs no paging')
  assert(text.includes('Unrouted') && text.includes('Covered') && text.includes('1 gap'), 'coverage status per card')
  assert(text.includes('Lina Lead has no email or SMS contact method'), 'coverage gaps on the card')
  assert(text.includes('Network primary') && text.includes('NOC mailbox'), 'owner and channel chips from the directory')
  assert(text.includes('Create recommended matrix'), 'alerts.manage can create the recommended matrix')
  assert.deepEqual(calls, [], 'seeded queries: rendering sent no request')
}
// Read-only (alerts.view): same matrix, no create button, channel names unknown without the directory.
{
  setUser(USER(['alerts.view']))
  const qc = newClient()
  seed(qc)
  qc.removeQueries({ queryKey: ['alerts', 'channel-directory'] })
  const text = strip(renderAt(qc, '/escalation', h(m.MatrixTab, { canManage: false, go: () => {} })))
  assert(text.includes('P1 Critical — major incident'))
  assert(!text.includes('Create recommended matrix'), 'no create action without alerts.manage')
  assert(!text.includes('Deleted channel'), 'a channel a read-only role cannot name is not called deleted')
  const page = strip(renderAt(qc, '/escalation', h(m.EscalationPage)))
  assert(page.includes('Read-only: changing policies, schedules and targets needs the alerts.manage permission'))
  for (const tab of ['Matrix', 'Policies', 'On-call', 'Response targets', 'Grouping', 'Coverage']) assert(page.includes(tab), `tab ${tab}`)
}
// Route conflicts (two enabled defaults for one priority) are spelled out.
{
  const text = strip(renderAt(newClient(), '/escalation', h(m.MatrixView, {
    policies: [...POLICIES, M.policyFromApi({ ...API_POLICY, id: 'pol-x', name: 'Another P1', default_priorities: ['P1', 'P2'] })],
    coverage: null, targets: TARGETS, dir: DIR, canManage: false, go: () => {}, onRecommended: () => {},
  })))
  assert(text.includes('Route conflict for P1: Another P1, P1 Critical — major incident all claim it; only “Another P1” is used.'))
  assert(text.includes('Coverage not checked'))
}

// (b) The policy editor: saved policy previewed by id; an edited draft through the dry run; read-only.
{
  setUser(USER(['alerts.view', 'alerts.manage']))
  const qc = newClient()
  seed(qc)
  const PREVIEW = M.previewFromApi({ steps: ROUTE.steps.map((s) => ({ ...s, current: false, notified_at: null })), review: ROUTE.review, fallback: [{ kind: 'email', address: null, via: 'channel:NOC mailbox' }], warnings: ['Step 2 reaches nobody at 2026-10-18 10:05 UTC; the fallback route would be used.'] })
  qc.setQueryData(['escalation', 'preview', 'pol-1', 'P1', 'rev-1'], PREVIEW)
  const draft = M.draftFromPolicy(POLICIES[0])
  const dir = { ...DIR }
  calls.length = 0
  const html = renderAt(qc, '/escalation?tab=policies', h(m.PolicyEditorBody, {
    draft, setDraft: () => {}, errors: {}, dir, readOnly: false, others: [POLICIES[1]], policyId: 'pol-1', revision: 'rev-1', dirty: false,
  }))
  const text = strip(html)
  assert(html.includes('value="P1 Critical — major incident"'), 'name field')
  for (const label of ['Step 1 delay in minutes', 'Step 2 delay in minutes', 'Step 3 delay in minutes', 'Step 1 name', 'Remove step 2', 'Add a target to step 1', 'Add a fallback target', 'Preview priority', 'Policy enabled']) {
    assert(html.includes(`aria-label="${label}"`), `aria-label ${label}`)
  }
  assert(html.includes('aria-pressed="true"') && text.includes('P1 Critical'), 'default priority toggles')
  assert(text.includes('Acknowledge within') && text.includes('Repeat step every') && text.includes('Max repeats') && text.includes('Unresolved updates every') && text.includes('Acknowledgement expires after'))
  assert(text.includes('Fallback route'))
  assert(text.includes('Draft outline') && text.includes('Dry run — who would be notified'))
  assert(text.includes('Omar Ops <a***@example.com>') && text.includes('On call: Network primary'), 'dry run lists who each step reaches')
  assert(text.includes('Reaches nobody — the fallback route would be used'), 'a step that reaches nobody shows its fallback')
  assert(text.includes('Nobody is on call in Network backup'), 'gaps per step')
  assert(text.includes('Unresolved updates every 15 min'), 'review cadence in the dry run')
  assert(text.includes('Network backup has no members'), 'local outline flags an empty schedule')
  assert(!text.includes('Previewing your unsaved changes'), 'a clean saved policy is previewed by id')
  assert.deepEqual(calls, [], 'the saved preview came from the cache; nothing was sent')

  // An edited draft is previewed through POST /escalation-policies/dry-run with the draft as the key.
  const edited = { ...draft, review_minutes: '30' }
  const body = M.policyPayload(edited)
  qc.setQueryData(['escalation', 'dry-run', 'P1', JSON.stringify(body)], PREVIEW)
  const t2 = strip(renderAt(qc, '/escalation?tab=policies', h(m.PolicyEditorBody, {
    draft: edited, setDraft: () => {}, errors: {}, dir, readOnly: false, others: [], policyId: 'pol-1', revision: 'rev-1', dirty: true,
  })))
  assert(t2.includes('Previewing your unsaved changes'))
  assert(t2.includes('Omar Ops <a***@example.com>'))
  assert.deepEqual(calls, [], 'the draft dry run came from the cache; nothing was sent')
  // An invalid draft does not run the dry run.
  const blank = M.emptyPolicyDraft()
  const t3 = strip(renderAt(qc, '/escalation?tab=policies', h(m.PolicyEditorBody, {
    draft: blank, setDraft: () => {}, errors: M.validatePolicy(blank), dir, readOnly: false, others: [], policyId: null, revision: null, dirty: true,
  })))
  assert(t3.includes('Complete the policy'))
  assert(t3.includes('Name the policy') && t3.includes('Pick at least one schedule, user or channel'), 'validation messages are shown')
  assert.deepEqual(calls, [], 'an incomplete draft sends no dry run')
  // Read-only: fields disabled, no pickers, no remove buttons.
  const ro = renderAt(qc, '/escalation?tab=policies', h(m.PolicyEditorBody, {
    draft, setDraft: () => {}, errors: {}, dir, readOnly: true, others: [], policyId: 'pol-1', revision: 'rev-1', dirty: false,
  }))
  assert(!ro.includes('aria-label="Add a target to step 1"') && !ro.includes('aria-label="Remove step 2"'), 'read-only has no edit controls')
  assert(/aria-label="Step 1 delay in minutes"[^>]*disabled|disabled[^>]*aria-label="Step 1 delay in minutes"/.test(ro), 'inputs are disabled')
  // A conflict with another policy's default is warned about before saving (the server refuses it with 409).
  const conflict = strip(renderAt(qc, '/escalation?tab=policies', h(m.PolicyEditorBody, {
    draft: { ...draft, default_priorities: ['P3'] }, setDraft: () => {}, errors: {}, dir, readOnly: false, others: [POLICIES[1]], policyId: 'pol-1', revision: 'rev-1', dirty: true,
  })))
  assert(conflict.includes('P3 is also the default in P3 Warning — team queue'))
}

// (c) /alerts/<id>?ack=1 never acknowledges by itself.
{
  setUser(USER(['alerts.view', 'alerts.acknowledge']))
  const qc = newClient()
  const ALERT = {
    id: 'al-1', status: 'active', severity: 'critical', priority: 'P1', message: 'Core router down', triggered_at: '2026-10-18T10:00:00Z',
    acknowledged_at: null, resolved_at: null, rule_id: null, rule_name: null, device_id: null, server_id: null, service_check_id: null, sensor_id: null,
    device_hostname: null, device_ip: null, server_hostname: null, service_check_name: null, sensor_name: null,
    entity: { kind: 'system', id: null, name: 'Appliance', subtitle: null, href: null }, acknowledged_by: null, assigned_to: null, assigned_at: null,
    closure: null, is_notice: false, notice_kind: null, suppression: null, snoozed: false, snoozed_until: null, ack_due_at: null, ack_overdue: false,
    channels: [], delivery: null, metadata: {}, server_time: '2026-10-18T10:02:00Z', permissions: { can_triage: true, can_manage_rules: false, can_suppress_forever: false, can_edit_rule: false },
    rule: null, policy_snapshot: null, escalation_state: null, evidence: [], recurrence: null, related: [], events: [], deliveries: [],
    notification_holds: [{ digest_id: 'd1', reason: 'budget', release_condition: 'Released with the next hourly summary', release_at: '2026-10-18T11:00:00Z', status: 'open' }],
  }
  qc.setQueryData(['alerts', 'detail', 'al-1'], ALERT)
  qc.setQueryData(['alerts', 'route', 'al-1'], M.routeFromApi(ROUTE))
  calls.length = 0
  const html = m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: ['/alerts/al-1?ack=1'] }, h(m.Routes, null, h(m.Route, { path: '/alerts/:id', element: h(m.AlertDetailPage) })))))
  assert.deepEqual(calls.filter(([verb]) => verb !== 'get'), [], 'opening the acknowledge link sends no POST')
  const text = strip(html)
  assert(text.includes('Priority P1 Critical') || html.includes('aria-label="Priority P1 Critical"'), 'priority badge in the header')
  assert(text.includes('Escalation route') && text.includes('Default policy for this priority'), 'route panel: policy and why')
  assert(text.includes('Step 1 · Primary on-call and NOC'), 'route panel: current step')
  assert(text.includes('Step 2 (Backup on-call) at'), 'route panel: next escalation time and action')
  assert(text.includes('Current step') && text.includes('Notified'), 'route panel: who was notified when')
  assert(text.includes('The rule is shared by 12 devices.'), 'route notes are shown')
  assert(text.includes('Notification held: Recipient message budget reached.') && text.includes('Released with the next hourly summary') && html.includes('href="/escalation?tab=grouping&amp;id=d1"'), 'held-in-digest notice with reason, release condition and link')

  // The confirmation body: the confirm button exists only for "confirm", and rendering never calls the handler.
  let confirmed = 0
  const body = (state) => strip(m.renderToStaticMarkup(h(m.Dialog, { open: true }, h(m.AckConfirmBody, { state, message: 'Core router down', onConfirm: () => { confirmed++ }, onCancel: () => {} }))))
  assert(body('confirm').includes('Acknowledge this incident?') && body('confirm').includes('Acknowledge this incident'))
  assert(body('confirm').includes('The incident stays open'))
  for (const s of ['already', 'closed', 'pending', 'forbidden']) assert(!/Acknowledge this incident$|Acknowledge this incident Cancel/.test(body(s)) && body(s).includes('Close'), `${s}: no confirm button`)
  assert.equal(confirmed, 0, 'rendering the dialog never acknowledges')

  // The page wires the POST to the dialog's confirm button only; nothing runs it on load.
  const page = fs.readFileSync('src/pages/AlertDetailPage.tsx', 'utf8')
  assert(page.includes('const askAck = ackRequested(location.search)'), 'the prompt is derived from the address')
  assert(/<AckConfirmDialog[\s\S]*?open=\{askAck\}[\s\S]*?onConfirm=\{\(\) => actions\.acknowledge\.mutate\(\{ id: alert\.id \}, \{ onSuccess: closeAck \}\)\}/.test(page), 'the POST is the confirm button handler')
  assert(!/useEffect\([^)]*\{[^}]*(askAck|ackRequested|acknowledge\.mutate)/s.test(page.slice(0, page.indexOf('if (q.isLoading)'))), 'no effect acknowledges')
  const mutateCalls = page.match(/acknowledge\.mutate\(/g) ?? []
  assert.equal(mutateCalls.length, 2, 'two explicit buttons acknowledge: the header button and the confirmation')
}

// (d) The rule wizard: priority picker, escalation modes, the legacy tier cap equals the server's 5.
{
  setUser(USER(['alerts.view', 'alerts.manage']))
  const qc = newClient()
  seed(qc)
  const html = renderAt(qc, '/alert-rules', h(m.AlertRuleWizardDialog, { open: true, onOpenChange: () => {} }))
  const text = strip(html)
  assert(html.includes('aria-label="Incident priority"') && html.includes('role="radiogroup"'), 'priority picker')
  assert(text.includes('Auto (P2)'), 'the default critical rule derives P2')
  assert(text.includes('P1 is never derived'))
  const wiz = fs.readFileSync('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
  assert(wiz.includes('const MAX_ESC_LEVELS = 5'), 'the legacy tier cap matches alert_rules.py (5)')
  assert(wiz.includes('s.escalation_levels.length < MAX_ESC_LEVELS') && !wiz.includes('s.escalation_levels.length < 4'), 'the add button uses the cap')
  assert(wiz.includes("priority: s.priority || null"), 'Auto saves NULL priority (derived on the server)')
  assert(wiz.includes("escalation_policy_id: s.escalation_mode === 'policy' ? s.escalation_policy_id || null : null"), 'only "choose a policy" names one')
  assert(wiz.includes("escalation_levels: s.escalation_mode === 'legacy' && s.escalation_levels.length"), 'legacy tiers are sent only in legacy mode')
  for (const label of ['Use priority default', 'Choose a policy', 'Legacy tiers']) assert(wiz.includes(`label: '${label}'`), `escalation mode ${label}`)
  // Round trip through the wizard's own ruleToState / stateToPayload (sliced like test-network-contracts.mjs).
  const start = wiz.indexOf('const TEMPLATE_FIELDS =')
  const end = wiz.indexOf('export function AlertRuleWizardDialog(')
  const code = `import { defaultNetflowForm, formFromRule, isNetflowMetric, netflowPayload } from './src/components/forms/netflowAlertRule';
const INTERFACE_METRICS = new Set(['if_util_pct','if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status']);
${wiz.slice(start, end)}
export { ruleToState, stateToPayload };`
  const out = await build({ stdin: { contents: code, resolveDir: root, loader: 'ts' }, bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', plugins: [aliasPlugin], logLevel: 'silent' })
  const W = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString('base64')}`)
  const LEVELS = [{ after_minutes: 15, notify_channels: [NOC], repeat_every_minutes: null }]
  const auto = W.ruleToState({ name: 'CPU', metric: 'cpu', operator: '>', threshold: 90, severity: 'critical' })
  assert.equal(auto.priority, '')
  assert.equal(auto.escalation_mode, 'default')
  let pl = W.stateToPayload(auto)
  assert.equal(pl.priority, null, 'Auto = NULL priority')
  assert.equal(pl.escalation_policy_id, null)
  assert.equal(pl.escalation_levels, null)
  const named = W.ruleToState({ name: 'Core', metric: 'ping_status', severity: 'critical', priority: 'p1', escalation_policy_id: 'pol-1', escalation_levels: LEVELS })
  assert.equal(named.priority, 'P1')
  assert.equal(named.escalation_mode, 'policy', 'a rule naming a policy opens in policy mode')
  pl = W.stateToPayload(named)
  assert.equal(pl.priority, 'P1')
  assert.equal(pl.escalation_policy_id, 'pol-1')
  assert.equal(pl.escalation_levels, null, 'a policy replaces the legacy tiers')
  const legacy = W.ruleToState({ name: 'Old', metric: 'ping_status', severity: 'warning', escalation_levels: LEVELS })
  assert.equal(legacy.escalation_mode, 'legacy', 'a rule with tiers keeps them (unchanged behaviour)')
  pl = W.stateToPayload(legacy)
  assert.deepEqual(pl.escalation_levels, LEVELS)
  assert.equal(pl.escalation_policy_id, null)
  pl = W.stateToPayload({ ...legacy, escalation_mode: 'default' })
  assert.equal(pl.escalation_levels, null, 'switching to the priority default clears the tiers')
  assert.equal(W.ruleToState({ name: 'x', metric: 'cpu', priority: 'P9' }).priority, '', 'an unknown priority is Auto')

  const host = fs.readFileSync('src/pages/servers/detail/tabs/AlertsTab.tsx', 'utf8')
  assert(host.includes("priority: priority === 'auto' ? null : priority") && host.includes("escalation_policy_id: policyId === 'default' ? null : policyId"), 'server rule dialog sends priority and policy')
}

console.log('Escalation UI: Matrix (policies, ack targets, step timeline, review cadence, coverage, conflicts; read-only), policy editor with saved and draft dry runs, alert route panel and digest holds, ?ack=1 confirmation never acknowledges, rule wizard priority/escalation with the 5-tier cap.')
