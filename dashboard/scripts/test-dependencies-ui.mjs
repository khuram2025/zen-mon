// Dependencies page model (src/pages/dependencies/dependenciesModel.ts): labels,
// ownership notes, filtering, grouping by parent, suggestion text and the
// accept body, plus the page and navigation wiring.
// Run from dashboard/: node scripts/test-dependencies-ui.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const src = fs.readFileSync('src/pages/dependencies/dependenciesModel.ts', 'utf8')
assert(!/^import /m.test(src), 'dependenciesModel.ts imports nothing, so it runs without the app')
const compiled = await transformWithEsbuild(src, 'dependenciesModel.ts', { loader: 'ts', format: 'esm' })
const M = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

assert.equal(M.endLabel({ name: 'core-1', ip: '10.0.0.1' }), 'core-1 (10.0.0.1)')
assert.equal(M.endLabel({ name: '10.0.0.1', ip: '10.0.0.1' }), '10.0.0.1')
assert.equal(M.endLabel({ name: null, ip: '10.0.0.1' }), '10.0.0.1')
assert.equal(M.endLabel({ name: 'web-01', ip: null }), 'web-01')

// Engine-owned rows say where to change them; operator rows have no note.
assert.equal(M.ownerNote({ source: 'manual', removable: true }), null)
assert.match(M.ownerNote({ source: 'discovery_rule', removable: false }), /discovery rule/)
assert.match(M.ownerNote({ source: 'controller', removable: false }), /controller/)
assert.equal(M.holdsAlerts({ enabled: true, suppress_alerts: true }), true)
assert.equal(M.holdsAlerts({ enabled: true, suppress_alerts: false }), false)

assert.equal(M.rootReasonText('gateway'), "this appliance's default gateway")
assert.equal(M.viaText({ parent_interface: 'Gi1/0/48', child_interface: 'Gi0/1', protocol: 'lldp' }), 'via Gi1/0/48 to Gi0/1 (LLDP)')
assert.equal(M.viaText({ parent_interface: null, child_interface: null, protocol: 'cdp' }), 'seen by CDP')
assert.equal(M.viaText({ parent_interface: null, child_interface: null, protocol: null }), '')

const end = (id, name, ip, kind = 'device') => ({ kind, id, name, ip, status: 'up', type: 'switch' })
const row = (id, p, c) => ({ id, parent: p, child: c, source: 'manual', removable: true, enabled: true, suppress_alerts: true })
const core = end('p1', 'core-1', '10.0.0.1')
const dist = end('p2', 'dist-1', '10.0.0.2')
const rows = [
  row('a', dist, end('c1', 'acc-9', '10.0.1.9')),
  row('b', core, end('c2', 'web-01', '10.0.2.1', 'server')),
  row('c', core, end('c3', 'acc-1', '10.0.1.1')),
]
const grouped = M.byParent(rows)
assert.deepEqual(grouped.map((g) => g.parent.name), ['core-1', 'dist-1'])
assert.deepEqual(grouped[0].children.map((d) => d.child.name), ['acc-1', 'web-01'])
assert.deepEqual(M.filterDependencies(rows, 'WEB').map((d) => d.id), ['b'])
assert.deepEqual(M.filterDependencies(rows, '10.0.0.2').map((d) => d.id), ['a'])
assert.equal(M.filterDependencies(rows, '  ').length, 3)

assert.deepEqual(M.acceptBody({ parent: core, child: dist }),
  { parent_device_id: 'p1', child_device_id: 'p2', dependency_type: 'uplink', source: 'topology' })
assert.deepEqual([...M.DEPENDENCY_TYPES], ['uplink', 'wan', 'power', 'site', 'service', 'manual'])

// Wiring: the route, the sidebar entry and its permission, no pixel font sizes.
const app = fs.readFileSync('src/App.tsx', 'utf8')
assert.match(app, /<Route path="dependencies" element={<DependenciesPage \/>} \/>/)
const nav = fs.readFileSync('src/components/layout/navigation.ts', 'utf8')
assert.match(nav, /to: '\/dependencies', label: 'Dependencies'.*permission: 'devices\.view'/)
const page = fs.readFileSync('src/pages/dependencies/DependenciesPage.tsx', 'utf8')
assert(!/text-\[\d+px\]/.test(page), 'use the rem type scale')
assert.match(page, /'\/topology-dependencies\/suggestions'/)
assert.match(page, /MANAGE_DEPENDENCIES = 'devices\.manage'/)

console.log('dependencies UI model: ok')
