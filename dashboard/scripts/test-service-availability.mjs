import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const source = fs.readFileSync('src/pages/ServiceCheckDetail.tsx', 'utf8')
const section = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)))
const code = `const UP_COLOR='green', WARN_COLOR='orange', DOWN_COLOR='red';
const clamp=(n,a,b)=>Math.max(a,Math.min(b,n));
${section('function buildAvailabilityBuckets(', '/* ─── Performance chart')}
${section('function buildDailyUptime(', 'const WEEKDAYS')}
${section('const DEFAULT_HEALTH_SCORE_CONFIG:', 'const HEALTH_SCORE_CONFIG_KEY')}
${section('function computeHealthScore(', 'function HealthScoreDetailsDialog(')}
export {buildAvailabilityBuckets,buildDailyUptime,coveredWeightedUptime,computeHealthScore};`
const compiled = await transformWithEsbuild(code, 'availability.ts', { loader: 'ts', format: 'esm' })
const m = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
const now = Date.now(), from = now - 3600000
const check = {status:'unknown',check_interval:60,last_check_at:new Date(from-86400000).toISOString()}
const stale = [{timestamp:check.last_check_at,new_status:'up'}]
assert(m.buildAvailabilityBuckets([],stale,check,from,now).every(b=>b.state==='gap'))
assert(m.buildAvailabilityBuckets([{timestamp:new Date(now+60000).toISOString(),is_up:true}],[],check,from,now).every(b=>b.state==='gap'))
assert(m.buildAvailabilityBuckets([{timestamp:new Date(from+60000).toISOString(),is_up:null}],[],check,from,now).every(b=>b.state==='gap'))
assert.equal(m.computeHealthScore({uptime_pct:null,error_rate_pct:null,incident_count:0,p95_response_ms:null}).score,null)
const midnight=new Date(); midnight.setHours(0,0,0,0)
const days=m.buildDailyUptime([
  {ts:midnight.toISOString(),uptime_pct:0,covered_sec:60,sample_count:1},
  {ts:new Date(+midnight+3600000).toISOString(),uptime_pct:100,covered_sec:3600,sample_count:60},
],30)
assert.equal(days.at(-1).coveredSec,3660)
assert(Math.abs(days.at(-1).downtimeSec-60)<.00001)
assert(Math.abs(m.coveredWeightedUptime(days)-100*3600/3660)<.00001)
console.log('Service availability UI regressions passed: stale data, future samples, no-data health, partial-hour weighting.')
