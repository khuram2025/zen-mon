import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const source = fs.readFileSync('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
const start = source.indexOf('const TEMPLATE_FIELDS =')
const end = source.indexOf('export function AlertRuleWizardDialog(')
assert(start >= 0 && end > start)
const code = `const INTERFACE_METRICS = new Set(['if_util_pct','if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status']);
${source.slice(start, end)}
export {ruleToState,stateToPayload};`
const compiled = await transformWithEsbuild(code, 'network-contracts.ts', {loader:'ts',format:'esm'})
const {ruleToState,stateToPayload} = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
const condition = {metric:'cpu',operator:'>',threshold:90,reset_threshold:80}
let payload = stateToPayload(ruleToState({name:'CPU',metric:'cpu',conditions:[condition]}))
assert.deepEqual(payload.conditions, [condition])
const syslog = {name:'Events',metric:'syslog',operator:'<=',threshold:4,target:'link',recovery_alert:true,min_duration:300,scope_tag:'branch',notify_channels:['local-sink']}
payload = stateToPayload(ruleToState(syslog))
assert.equal(payload.metric,'syslog'); assert.equal(payload.threshold,4); assert.equal(payload.operator,'<=')
assert.equal(payload.target,'link'); assert.equal(payload.scope_tag,'branch')
assert.equal(payload.conditions,null); assert.equal(payload.min_duration,0); assert.equal(payload.recovery_alert,false)
assert.deepEqual(payload.notify_channels,['local-sink'])
console.log('Network editor contracts passed: single-condition reset round-trip, syslog filters/scope/channels and manual resolution.')
