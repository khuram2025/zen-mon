import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

const source = fs.readFileSync('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
const start = source.indexOf('const TEMPLATE_FIELDS =')
const end = source.indexOf('export function AlertRuleWizardDialog(')
assert(start >= 0 && end > start)
// The extracted slice calls the NetFlow rule helpers the wizard imports, so the
// slice is bundled together with the real module rather than evaluated alone.
const code = `import { defaultNetflowForm, formFromRule, isNetflowMetric, netflowPayload } from './src/components/forms/netflowAlertRule';
const INTERFACE_METRICS = new Set(['if_util_pct','if_in_bps','if_out_bps','if_errors','if_discards','if_oper_status']);
${source.slice(start, end)}
export {ruleToState,stateToPayload,autoResetThreshold};`
const src = path.join(process.cwd(), 'src')
const aliasPlugin = {
  name: 'app-alias',
  setup(b) {
    b.onResolve({ filter: /^@\// }, (args) => b.resolve(`./${args.path.slice(2)}`, { resolveDir: src, kind: args.kind }))
  },
}
const compiled = await build({
  stdin: { contents: code, resolveDir: process.cwd(), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022',
  plugins: [aliasPlugin], logLevel: 'silent',
})
const {ruleToState,stateToPayload,autoResetThreshold} = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`)
const condition = {metric:'cpu',operator:'>',threshold:90,reset_threshold:80}
let payload = stateToPayload(ruleToState({name:'CPU',metric:'cpu',conditions:[condition]}))
assert.deepEqual(payload.conditions, [condition])
const syslog = {name:'Events',metric:'syslog',operator:'<=',threshold:4,target:'link',recovery_alert:true,min_duration:300,scope_tag:'branch',notify_channels:['local-sink']}
payload = stateToPayload(ruleToState(syslog))
assert.equal(payload.metric,'syslog'); assert.equal(payload.threshold,4); assert.equal(payload.operator,'<=')
assert.equal(payload.target,'link'); assert.equal(payload.scope_tag,'branch')
assert.equal(payload.conditions,null); assert.equal(payload.min_duration,0); assert.equal(payload.recovery_alert,false)
assert.deepEqual(payload.notify_channels,['local-sink'])
// Clear hold: metric rules round-trip clear_duration (null = the 300 s default,
// 0 = immediate); status rules never carry one.
payload = stateToPayload(ruleToState({name:'CPU',metric:'cpu',operator:'>',threshold:90,clear_duration:600}))
assert.equal(payload.clear_duration, 600)
payload = stateToPayload(ruleToState({name:'CPU',metric:'cpu',operator:'>',threshold:90,clear_duration:0}))
assert.equal(payload.clear_duration, 0)
payload = stateToPayload(ruleToState({name:'CPU',metric:'cpu',operator:'>',threshold:90}))
assert.equal(payload.clear_duration, null)
payload = stateToPayload(ruleToState({name:'Down',metric:'ping_status',operator:'==',threshold:0,trigger_on:'down',clear_duration:600}))
assert.equal(payload.clear_duration, null)
// Auto clear hint: device CPU/memory clear 2 points on the healthy side unless a reset value is set.
assert.equal(autoResetThreshold({metric:'memory',operator:'>',threshold:90}), 88)
assert.equal(autoResetThreshold({metric:'cpu',operator:'<',threshold:10}), 12)
assert.equal(autoResetThreshold({metric:'cpu',operator:'>',threshold:90,reset_threshold:80}), null)
assert.equal(autoResetThreshold({metric:'if_errors',operator:'>',threshold:100}), null)
console.log('Network editor contracts passed: single-condition reset round-trip, clear hold round-trip, auto clear hint, syslog filters/scope/channels and manual resolution.')
