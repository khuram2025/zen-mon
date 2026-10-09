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

// ── Inventory: the real catalog entry and the built-in grants (owner decisions D4 and D9) ─────────────
// Built from server/app/core/permissions.py, so the editor's presets are checked against what the
// server really defines rather than against a hand-written copy.
{
  const py = fs.readFileSync('../server/app/core/permissions.py', 'utf8')
  const list = (name) => {
    const m = py.match(new RegExp(`^${name} = \\[([\\s\\S]*?)\\n\\]`, 'm'))
    assert(m, `${name} not found in permissions.py`)
    return [...m[1].matchAll(/"([a-z_]+\.[a-z_]+)"/g)].map((x) => x[1])
  }
  const ids = ['inventory.view', 'inventory.export', 'inventory.manage']
  const risk = (id) => (py.match(new RegExp(`"${id.replace('.', '\\.')}": "(sensitive|privileged|critical)"`)) || [])[1] || 'standard'
  const implies = (id) => {
    const m = py.match(new RegExp(`"${id.replace('.', '\\.')}": \\[([^\\]]*)\\]`))
    return m ? [...m[1].matchAll(/"([a-z_]+\.[a-z_]+)"/g)].map((x) => x[1]) : []
  }
  const inventory = { module: 'inventory', label: 'Inventory', area: 'Operations', permissions: ids.map((id) => P(id, risk(id), implies(id))) }
  assert.equal(risk('inventory.export'), 'sensitive', 'export is classified sensitive')
  assert.deepEqual(implies('inventory.manage'), ['inventory.view'])
  assert.deepEqual(M.levelsFor(inventory), ['none', 'view', 'full'])
  assert.equal(M.fullLabel(inventory), 'Full')
  assert.deepEqual(M.extraPermissions(inventory).map((p) => p.id), ['inventory.export'], 'export is ticked on its own, never by a preset')
  assert.deepEqual([...M.applyLevel([inventory], inventory, 'full', set())].sort(), ['inventory.manage', 'inventory.view'])
  assert.deepEqual([...M.applyLevel([inventory], inventory, 'view', set('inventory.manage', 'inventory.view'))], ['inventory.view'])

  // What each built-in role shows as its Inventory level in the editor.
  const level = (name) => M.moduleLevel(inventory, set(...list(name)))
  assert.equal(level('_OPERATOR'), 'full')
  assert.equal(level('_VIEWER'), 'view')
  assert.equal(level('_READ_ONLY'), 'view')
  // D9: network, systems and security teams view the inventory; the application team has none; no team role exports or manages.
  for (const team of ['_NETWORK_TEAM', '_SYSTEMS_TEAM', '_SECURITY_TEAM']) {
    assert.equal(level(team), 'view', `${team} gets the inventory view`)
    assert(!list(team).some((id) => id === 'inventory.export' || id === 'inventory.manage'), `${team} never exports or manages inventory`)
  }
  assert.equal(level('_APPLICATION_TEAM'), 'none')
  // D4: viewers and read-only roles never hold the sensitive export or any write.
  for (const name of ['_VIEWER', '_READ_ONLY']) assert(!list(name).some((id) => id === 'inventory.export' || id === 'inventory.manage'), name)
}

console.log('Role model: presets stay standard-risk, dependencies stay consistent, grants stay within the editor’s own access; Inventory levels follow the built-in roles.')
