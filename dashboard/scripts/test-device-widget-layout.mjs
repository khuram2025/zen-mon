import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'
const compiled = await transformWithEsbuild(fs.readFileSync('src/components/devices/widgetLayout.ts','utf8'),'widgetLayout.ts',{loader:'ts',format:'esm'})
const {defaultWidgetLayout,restoreWidgetLayout,changeWidgetLayout,overlaps,readWidgetLayout,mergeWidgetLayout,widgetLayoutKey,widgetTemplateKey,effectiveWidgetLayout} = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
const definitions = [
 ...Array.from({length:6},(_,i)=>({id:`kpi-${i}`,width:2,height:4,minWidth:2,minHeight:4})),
 {id:'health',width:8,height:10,minWidth:4,minHeight:5},
 {id:'alerts',width:4,height:10,minWidth:4,minHeight:5},
 {id:'chart',width:8,height:10,minWidth:4,minHeight:8},
]
function valid(items) {
 assert.equal(new Set(items.map(i=>i.id)).size,items.length)
 for(const item of items) {
  assert(item.x>=0 && item.y>=0 && item.x+item.w<=12)
  assert(item.w>=2 && item.h>=3)
  for(const other of items) if(item!==other) assert(!overlaps(item,other),`${item.id} overlaps ${other.id}`)
 }
}
const original=defaultWidgetLayout(definitions);valid(original)
assert.deepEqual(original.slice(0,6).map(i=>[i.x,i.y]),[[0,0],[2,0],[4,0],[6,0],[8,0],[10,0]])
const moved=changeWidgetLayout(original,definitions[7],{x:0,y:0});valid(moved)
assert.equal(moved.find(i=>i.id==='alerts').y,0)
assert.equal(original.find(i=>i.id==='alerts').x,8)
const resized=changeWidgetLayout(moved,definitions[8],{w:12,h:18});valid(resized)
assert.equal(resized.find(i=>i.id==='chart').w,12)
assert.equal(resized.find(i=>i.id==='chart').h,18)
assert.deepEqual(restoreWidgetLayout(definitions,readWidgetLayout(JSON.stringify({version:1,items:resized}))),resized)
assert.deepEqual(restoreWidgetLayout(definitions,null),original)
const malformed=restoreWidgetLayout(definitions,[{id:'chart',x:999,y:-80,w:100,h:0},{id:'kpi-0',x:NaN,w:'bad'},null]);valid(malformed)
assert.equal(malformed.find(i=>i.id==='chart').h,8)
for(const raw of [null,'bad json','{}','{"version":9,"items":[]}']) assert.equal(readWidgetLayout(raw),null)
const hidden=mergeWidgetLayout(resized,resized.filter(i=>i.id!=='chart'));assert(hidden.some(i=>i.id==='chart'))
const added=restoreWidgetLayout([...definitions,{id:'new',width:4,height:5}],resized);valid(added)
assert.deepEqual(added.find(i=>i.id==='alerts'),resized.find(i=>i.id==='alerts'))
assert.notEqual(widgetLayoutKey('user-a','device-a'),widgetLayoutKey('user-b','device-a'))
assert.notEqual(widgetLayoutKey('user-a','device-a'),widgetLayoutKey('user-a','device-b'))
// Repeated moves/resizes must never create overlapping or out-of-bounds cards.
let layout=original
for(let n=0;n<120;n++) {const def=definitions[n%definitions.length];layout=changeWidgetLayout(layout,def,{x:n%14-1,y:n%20,w:n%15,h:n%28});valid(layout)}
console.log('Widget layout regressions passed: collisions, dimensions, persistence, reset, malformed storage, new/hidden widgets and account/device isolation.')

// A compact reboot card can shrink below the former 25% floor and survives reload.
const reboot = {id:'boot-time',width:2,height:2,minWidth:1,minHeight:2}
const compact = changeWidgetLayout(defaultWidgetLayout([reboot]),reboot,{w:1,h:2})
assert.equal(compact[0].w,1)
assert.equal(compact[0].h,2)
assert.deepEqual(restoreWidgetLayout([reboot],readWidgetLayout(JSON.stringify({version:1,items:compact}))),compact)
assert.equal(changeWidgetLayout(compact,reboot,{w:0,h:0})[0].w,1)
assert.equal(changeWidgetLayout(compact,reboot,{w:0,h:0})[0].h,2)
const oldReboot = [{id:'boot-time',x:8,y:19,w:3,h:3}]
assert.deepEqual(restoreWidgetLayout([reboot],oldReboot),oldReboot)
console.log('Compact widget dimensions and existing saved sizes passed.')

// Template matching is vendor + device type + tab + account, not device identity.
assert.equal(widgetLayoutKey('u','d'), 'zenplus:device-widgets:v1:u:d')
assert.notEqual(widgetLayoutKey('u','d'), widgetLayoutKey('u','d','metrics'))
const templateKey = widgetTemplateKey('u','Palo Alto Networks','firewall','metrics')
assert.equal(templateKey, widgetTemplateKey('u','  PALO   ALTO NETWORKS  ',' Firewall ','metrics'))
for (const other of [widgetTemplateKey('u','Cisco','firewall','metrics'), widgetTemplateKey('u','Palo Alto Networks','switch','metrics'),widgetTemplateKey('u','Palo Alto Networks','firewall','events'),widgetTemplateKey('other','Palo Alto Networks','firewall','metrics')]) assert.notEqual(templateKey,other)
assert.equal(widgetTemplateKey('u','','firewall'),null)
assert.equal(widgetTemplateKey('u','unknown','firewall'),null)
assert.equal(effectiveWidgetLayout(null,resized),resized)
assert.equal(effectiveWidgetLayout(original,resized),original)
assert.equal(effectiveWidgetLayout(null,null),null)
console.log('Tab isolation, legacy layout compatibility, vendor/type normalization, template inheritance and device overrides passed.')
