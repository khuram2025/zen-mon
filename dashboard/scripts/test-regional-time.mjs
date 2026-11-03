import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

process.env.TZ = 'America/Los_Angeles'
await mkdir('node_modules/.cache', { recursive: true })
const dir = await mkdtemp(path.resolve('node_modules/.cache/regional-time-'))
try {
  const result = await build({ stdin: { contents: `
    export * from './src/lib/regionalTime';
    export { fmtAlertDate, timezoneName } from './src/components/alerts/format';
    export { toLocalInput, checkCustomRange, timezoneLabel } from './src/components/TimeRangePicker';
    export { displayTimeZone } from './src/lib/netflow/window';
    export { asOfLabel } from './src/components/inventory/model';
    export { formatDateTime } from './src/lib/branding';
    export { buildDaySeries } from './src/components/services/uptime';
    export { shortDate } from './src/components/inventory/softwareModel';
    export { timelineWindow } from './src/features/escalation/model';
  `, resolveDir: process.cwd() }, bundle: true, platform: 'node', format: 'esm', packages: 'external',
    alias: { '@': path.resolve('src') }, write: false, logLevel: 'silent' })
  const file = path.join(dir,'test.mjs'); await writeFile(file,result.outputFiles[0].text)
  const f = await import(pathToFileURL(file).href)
  const at = new Date('2026-11-03T13:00:00Z')
  f.setRegionalTime({ timezone:'Asia/Riyadh' })
  assert.equal(f.regionalDay(at).toISOString(),'2026-11-02T21:00:00.000Z')
  assert.equal(f.shortDate('2026-11-03T22:30:00Z'),'2026-11-04')
  assert.equal(f.buildDaySeries([],1,Date.parse('2026-11-03T22:30:00Z'))[0].key,'2026-11-04')
  assert.equal(f.timelineWindow(+at,1,'Asia/Riyadh').from,+f.regionalDay(at))
  f.setRegionalTime({ timezone:'America/New_York' })
  for (const [iso,hours] of [['2026-03-08T12:00:00Z',23],['2026-11-01T12:00:00Z',25]]) {
    const day=Date.parse(iso)
    assert.equal((+f.regionalDay(day,1)-+f.regionalDay(day))/3600000,hours)
    const window=f.timelineWindow(day,1,'America/New_York')
    assert.equal((window.to-window.from)/3600000,hours)
  }
  f.setRegionalTime({ timezone:'Asia/Riyadh' })
  assert.equal(f.regionalDate(at,'datetime'),'2026-11-03 16:00:00 GMT+3')
  assert.equal(f.regionalDate(at,'time','en-GB',{hour:'2-digit',minute:'2-digit',hour12:false}),'16:00')
  assert.match(f.fmtAlertDate(at.toISOString(),{year:'always'}),/16:00 UTC\+3/)
  assert.equal(f.timezoneName(),'Asia/Riyadh (UTC+3)')
  assert.equal(f.displayTimeZone(),'Asia/Riyadh')
  assert.equal(f.toLocalInput(at.getTime()),'2026-11-03T16:00')
  assert.deepEqual(f.checkCustomRange('2026-11-03T15:00','2026-11-03T16:00',at.getTime()+60_000),
    {ok:true,fromISO:'2026-11-03T12:00:00.000Z',toISO:'2026-11-03T13:00:00.000Z',clipped:false})
  assert.equal(f.asOfLabel('2026-11-03T22:30:00Z',Date.parse('2026-11-03T23:00:00Z')),'01:30')
  assert.equal(f.regionalDate(at,'time','en-GB',{timeZone:'UTC',hour:'2-digit',minute:'2-digit',hour12:false}),'13:00')
  f.setRegionalTime({timezone:'Asia/Kolkata'})
  assert.equal(f.regionalDate(at,'datetime'),'2026-11-03 18:30:00 GMT+5:30')
  f.setRegionalTime({timezone:'America/New_York'})
  assert.equal(f.regionalDate(new Date('2026-11-01T05:30:00Z'),'datetime'),'2026-11-01 01:30:00 GMT-4')
  assert.equal(f.regionalDate(new Date('2026-11-01T06:30:00Z'),'datetime'),'2026-11-01 01:30:00 GMT-5')
  assert(Number.isNaN(f.regionalInputMs('2026-03-08T02:30')))
  assert(Number.isNaN(f.regionalInputMs('2026-02-30T12:00')))
  f.setRegionalTime({timezone:'Asia/Riyadh',date_format:'DD/MM/YYYY',time_format:'12h'})
  assert.equal(f.regionalDate(at,'datetime'),'03/11/2026 4:00:00 PM GMT+3')
  assert.equal(f.formatDateTime('2026-11-03 13:00:00',f.regionalSettings(),{zone:true}),'03/11/2026 4:00 PM GMT+3')
  f.setRegionalTime({timezone:'invalid/zone'})
  assert.equal(f.regionalDate(at,'datetime'),'2026-11-03 13:00:00 UTC')
  console.log('Passed: configured dashboard, alert, report and input times; browser independence; half-hour offsets; DST folds/gaps; invalid-zone fallback')
} finally { await rm(dir,{recursive:true,force:true}) }
