// Role editor presets: None / View / Full per module never include sensitive
// or privileged permissions, keep dependencies consistent, and respect what
// the editing account may grant.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const src = fs.readFileSync('src/components/access/roleModel.ts', 'utf8')
const compiled = await transformWithEsbuild(src, 'roleModel.ts', { loader: 'ts', format: 'esm' })
const M = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

const P = (id, risk = 'standard', implies = []) => ({ id, label: id, description: id, risk, implies })
const netflow = { module: 'netflow', label: 'NetFlow', area: 'Network', permissions: [
  P('netflow.view'), P('netflow.manage', 'standard', ['netflow.view']),
  P('netflow.manage_shared', 'privileged', ['netflow.manage', 'netflow.view']), P('netflow.export', 'sensitive', ['netflow.view']),
] }
const dashboard = { module: 'dashboard', label: 'Dashboards', area: 'General', permissions: [P('dashboard.view')] }
const users = { module: 'users', label: 'Users & Roles', area: 'Administration', permissions: [
  P('users.view'), P('users.manage', 'privileged', ['users.view', 'roles.view']), P('roles.view'), P('roles.manage', 'privileged', ['roles.view']),
] }
const audit = { module: 'audit', label: 'Audit', area: 'Administration', permissions: [P('audit.view', 'sensitive')] }
const system = { module: 'system', label: 'System Administration', area: 'Administration', permissions: [P('system.admin', 'critical')] }
const servers = { module: 'servers', label: 'Servers & Agents', area: 'Systems', permissions: [P('servers.view'), P('servers.manage', 'standard', ['servers.view'])] }
const modules = [system, users, netflow, dashboard, audit, servers]
const set = (...ids) => new Set(ids)

// Which presets each module offers, and what "full" is called.
assert.deepEqual(M.levelsFor(netflow), ['none', 'view', 'full'])
assert.deepEqual(M.levelsFor(dashboard), ['none', 'full'])
assert.equal(M.fullLabel(dashboard), 'View')
assert.equal(M.fullLabel(netflow), 'Full')
assert.deepEqual(M.levelsFor(users), ['none', 'full'])            // only the two .view permissions are presets
assert.equal(M.fullLabel(users), 'View')
assert.deepEqual(M.levelsFor(audit), [])                           // sensitive only: ticked individually
assert.deepEqual(M.levelsFor(system), [])
assert.deepEqual(M.extraPermissions(netflow).map((p) => p.id), ['netflow.manage_shared', 'netflow.export'])

// Presets never pull in sensitive or privileged permissions.
const full = M.applyLevel(modules, netflow, 'full', set())
assert.deepEqual([...full].sort(), ['netflow.manage', 'netflow.view'])
assert.equal(M.moduleLevel(netflow, full), 'full')
assert.equal(M.moduleLevel(netflow, set('netflow.view')), 'view')
assert.equal(M.moduleLevel(netflow, set('netflow.view', 'netflow.export')), 'view')   // extras do not change the preset
assert.equal(M.moduleLevel(netflow, set('netflow.export')), 'custom')
assert.equal(M.moduleLevel(netflow, set()), 'none')

// Dropping to View removes manage, and with it anything that depended on manage.
const withShared = set('netflow.view', 'netflow.manage', 'netflow.manage_shared', 'netflow.export')
const view = M.applyLevel(modules, netflow, 'view', withShared)
assert.deepEqual([...view].sort(), ['netflow.export', 'netflow.view'])
// None clears the whole module, extras included, and leaves other modules alone.
assert.deepEqual([...M.applyLevel(modules, netflow, 'none', set(...withShared, 'servers.view'))], ['servers.view'])

// Only permissions the editor may grant are added.
const onlyView = M.applyLevel(modules, servers, 'full', set(), (id) => id === 'servers.view')
assert.deepEqual([...onlyView], ['servers.view'])

// Dependencies: adding pulls prerequisites in; pruning drops orphans.
assert.deepEqual([...M.withDependencies(modules, set(), 'netflow.manage_shared')].sort(), ['netflow.manage', 'netflow.manage_shared', 'netflow.view'])
assert.deepEqual([...M.pruneDangling(modules, set('users.manage', 'users.view'))], ['users.view'])

// Areas in sidebar order.
assert.deepEqual(M.groupByArea(modules).map((a) => a.area), ['General', 'Network', 'Systems', 'Administration'])

// Tags compare case-insensitively and ignore blanks and duplicates.
assert.deepEqual(M.cleanTags([' Network ', 'network', '', 'Riyadh']), ['Network', 'Riyadh'])
assert(M.sameTags(['Network', 'riyadh'], ['RIYADH', 'network ']))
assert(!M.sameTags(['network'], []))

console.log('Role model: presets stay standard-risk, dependencies stay consistent, grants stay within the editor’s own access.')
