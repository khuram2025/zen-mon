// Calendar and rolling report periods in the organisation timezone (features/reports/periods.ts).
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'
const brand = await transformWithEsbuild(fs.readFileSync('src/lib/branding.ts','utf8'),'b.ts',{loader:'ts',format:'esm'})
const bURL = `data:text/javascript;base64,${Buffer.from(brand.code).toString('base64')}`
let src = fs.readFileSync('src/features/reports/periods.ts','utf8').replace("from '@/lib/branding'", `from '${bURL}'`)
const c = await transformWithEsbuild(src,'p.ts',{loader:'ts',format:'esm'})
const p = await import(`data:text/javascript;base64,${Buffer.from(c.code).toString('base64')}`)
const now = new Date('2026-10-07T11:05:00Z') // Wed 14:05 Riyadh
const r = (id, ws='monday') => { const x = p.periodRange(id, 'Asia/Riyadh', ws, now); return [x.from.toISOString(), x.to.toISOString()] }
assert.deepEqual(r('yesterday'), ['2026-10-05T21:00:00.000Z','2026-10-06T21:00:00.000Z'])
assert.deepEqual(r('previous_week'), ['2026-09-27T21:00:00.000Z','2026-10-04T21:00:00.000Z'])
assert.deepEqual(r('previous_week','sunday'), ['2026-09-26T21:00:00.000Z','2026-10-03T21:00:00.000Z'])
assert.deepEqual(r('previous_month'), ['2026-08-31T21:00:00.000Z','2026-09-30T21:00:00.000Z'])
assert.deepEqual(r('previous_quarter'), ['2026-06-30T21:00:00.000Z','2026-09-30T21:00:00.000Z'])
assert.deepEqual(r('month_to_date'), ['2026-09-30T21:00:00.000Z','2026-10-07T11:05:00.000Z'])
// Previous work week: five days from the organisation week start, the latest that has ended.
assert.deepEqual(r('previous_work_week','sunday'), ['2026-09-26T21:00:00.000Z','2026-10-01T21:00:00.000Z']) // Sun 27 Sep – Thu 1 Oct
assert.deepEqual(r('previous_work_week'), ['2026-09-27T21:00:00.000Z','2026-10-02T21:00:00.000Z'])          // Mon 28 Sep – Fri 2 Oct
assert.deepEqual(r('previous_work_week','saturday'), ['2026-09-25T21:00:00.000Z','2026-09-30T21:00:00.000Z']) // Sat 26 Sep – Wed 30 Sep
const ww = (ws, at) => { const x = p.periodRange('previous_work_week', 'Asia/Riyadh', ws, new Date(at)); return [x.from.toISOString(), x.to.toISOString()] }
// Thursday 23:59: this Sun–Thu week has not ended yet.
assert.deepEqual(ww('sunday', '2026-10-08T20:59:00Z'), ['2026-09-26T21:00:00.000Z','2026-10-01T21:00:00.000Z'])
// Friday 00:00 and Sunday 08:00: this week's Sun–Thu.
assert.deepEqual(ww('sunday', '2026-10-08T21:00:00Z'), ['2026-10-03T21:00:00.000Z','2026-10-08T21:00:00.000Z'])
assert.deepEqual(ww('sunday', '2026-10-09T07:00:00Z'), ['2026-10-03T21:00:00.000Z','2026-10-08T21:00:00.000Z'])
assert.deepEqual(ww('sunday', '2026-10-11T05:00:00Z'), ['2026-10-03T21:00:00.000Z','2026-10-08T21:00:00.000Z'])
assert.deepEqual(ww('saturday', '2026-10-08T07:00:00Z'), ['2026-10-02T21:00:00.000Z','2026-10-07T21:00:00.000Z']) // Thu: Sat 3 – Wed 7 Oct
assert.equal(p.PERIOD_LABEL.previous_work_week, 'Previous work week')
const ny = p.periodRange('previous_month','America/New_York','monday', new Date('2026-11-15T12:00:00Z'))
assert.deepEqual([ny.from.toISOString(), ny.to.toISOString()], ['2026-10-01T04:00:00.000Z','2026-11-01T04:00:00.000Z'])
console.log('periods ok')
