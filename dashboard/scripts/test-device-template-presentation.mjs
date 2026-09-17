import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'
const compiled = await transformWithEsbuild(fs.readFileSync('src/components/devices/templatePresentation.ts', 'utf8'), 'templatePresentation.ts', { loader: 'ts', format: 'esm' })
const { filterInsightRows, insightRowSeverity, readableInstanceLabel, alignDeviceRange } = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
const row = (label, status, text = '') => ({ label, instance: label, cells: { state: {status, text} } })
const rows = [row('pool10','ok','Available'), row('pool2','ok'), row('offline','crit','Member failed'),row('warn','warn'),row('unknown','none')]
const before = JSON.stringify(rows)
assert.deepEqual(filterInsightRows(rows, '', true).map(r => r.label), ['offline','warn'])
assert.deepEqual(filterInsightRows(rows, ' POOL ', false).map(r => r.label), ['pool2','pool10'])
assert.deepEqual(filterInsightRows(rows, 'MEMBER FAILED', true).map(r => r.label), ['offline'])
assert.equal(filterInsightRows(rows, 'unmatched', false).length, 0)
assert.equal(filterInsightRows(rows, '', false).length, rows.length)
assert.equal(insightRowSeverity({label:'x',instance:'1',cells:{a:{status:'ok',text:''},b:{status:'crit',text:''}}}),4)
assert.equal(insightRowSeverity({label:'x',instance:'1',cells:{}}),0)
assert.equal(JSON.stringify(rows), before)
console.log('Template presentation tests passed: attention ranking, search, empty results, mixed states and evidence preservation.')

const encoded = `3.17.${[...'management_module'].map(c=>c.charCodeAt(0)).join('.')}.3.49.47.49`
assert.equal(readableInstanceLabel(encoded), '3 · management_module · 1/1')
for (const raw of ['1.3.6.1.4.1.3375.2', '10.10.101.227', 'Ethernet1/1', encoded + '.255', '3.17.109.97.110.97.103.101']) assert.equal(readableInstanceLabel(raw), raw)

const requested = {hours: 1, fromISO:'2026-09-17T19:00:00Z', toISO:'2026-09-17T20:00:00Z', isCustom:false}
const adjusted = alignDeviceRange(requested, Date.parse('2026-09-17T13:00:00Z'))
assert.equal(adjusted.toISO, '2026-09-17T13:00:00.000Z')
assert.equal(adjusted.fromISO, '2026-09-17T12:00:00.000Z')
assert.deepEqual(alignDeviceRange({...requested,isCustom:true},Date.parse(adjusted.toISO)),{...requested,isCustom:true})
assert.equal(alignDeviceRange(requested,null),requested)
assert.equal(alignDeviceRange(requested,NaN),requested)

assert.equal(filterInsightRows([row(encoded,'none')], 'management_module', false).length,1)
