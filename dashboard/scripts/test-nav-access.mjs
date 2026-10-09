// Role-based navigation: a role only sees (and can open) what its permissions
// allow. Bundles the real navigation tree with icons stubbed, then checks it
// against the server's permission catalog and built-in team roles.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

const root = process.cwd()

// Bundle a module with lucide-react left external, then swap its icon
// imports for stubs so the result runs in plain Node.
async function load(entry) {
  const out = await build({
    entryPoints: [path.join(root, entry)],
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'neutral',
    external: ['lucide-react'],
    logLevel: 'silent',
  })
  const code = out.outputFiles[0].text.replace(/import\s*\{([^}]*)\}\s*from\s*"lucide-react";?/g, (_m, names) =>
    names.split(',').map((n) => n.trim()).filter(Boolean).map((n) => {
      const parts = n.split(/\s+as\s+/)
      return `const ${(parts[1] || parts[0]).trim()} = () => null;`
    }).join('\n'))
  return import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
}

const nav = await load('src/components/layout/navigation.ts')
const access = await load('src/components/layout/navAccess.ts')
const { NAV_GROUPS, trailForLocation } = nav
const { filterNavGroups, firstAllowedPath, trailAllows, gateAllows, gatePermissions, canFromPermissions } = access

// ── the server's vocabulary ────────────────────────────────────────────────
const permissionsPy = fs.readFileSync(path.join(root, '../server/app/core/permissions.py'), 'utf8')
const catalog = new Set([...permissionsPy.matchAll(/\(\s*"([a-z_]+\.[a-z_]+)",\s*"/g)].map((m) => m[1]))
assert(catalog.has('servers.view') && catalog.has('apm.view') && catalog.has('system.admin'))

function pyList(name) {
  const m = permissionsPy.match(new RegExp(`^${name} = \\[([\\s\\S]*?)\\n\\]`, 'm'))
  assert(m, `${name} not found in permissions.py`)
  return [...m[1].matchAll(/"([a-z_]+\.[a-z_]+)"/g)].map((x) => x[1])
}

// ── every entry is gated by real permissions ───────────────────────────────
const OPEN_TO_EVERY_ROLE = new Set([
  // Personal pages any signed-in account may use.
  '/settings/general?tab=appearance', '/settings/general?tab=support', '/settings/general?tab=profile',
])
for (const group of NAV_GROUPS) {
  for (const item of group.items) {
    for (const p of gatePermissions(item)) assert(catalog.has(p), `${item.to}: unknown permission ${p}`)
    const itemGated = gatePermissions(item).length > 0
    if (!item.children) {
      assert(itemGated, `${group.label} → ${item.label} (${item.to}) has no permission`)
      continue
    }
    for (const child of item.children) {
      for (const p of gatePermissions(child)) assert(catalog.has(p), `${child.to}: unknown permission ${p}`)
      const gated = itemGated || gatePermissions(child).length > 0
      assert(gated || OPEN_TO_EVERY_ROLE.has(child.to), `${item.label} → ${child.label} (${child.to}) has no permission`)
    }
  }
}

// ── helpers ─────────────────────────────────────────────────────────────────
const visible = (perms) => {
  const groups = filterNavGroups(NAV_GROUPS, canFromPermissions(perms))
  const labels = new Set()
  for (const g of groups) for (const i of g.items) {
    labels.add(`${g.id}:${i.label}`)
    for (const c of i.children || []) labels.add(`${g.id}:${i.label}:${c.label}`)
  }
  return { groups: groups.map((g) => g.id), labels }
}

// A child needs its parent's permission too, and a parent with no visible child disappears.
{
  const tree = [{ id: 'g', items: [
    { to: '/a', permission: 'a.view', children: [{ to: '/a/x' }, { to: '/a/y', permission: 'a.manage' }] },
    { to: '/b', children: [{ to: '/b/x', permission: 'b.view' }] },
  ] }, { id: 'empty', items: [{ to: '/c', permission: 'c.view' }] }]
  const only = filterNavGroups(tree, canFromPermissions(['a.manage']))
  assert.equal(only.length, 0, 'a child without its parent permission is hidden')
  const some = filterNavGroups(tree, canFromPermissions(['a.view']))
  assert.deepEqual(some.map((g) => g.id), ['g'])
  assert.deepEqual(some[0].items.map((i) => i.to), ['/a'])
  assert.deepEqual(some[0].items[0].children.map((c) => c.to), ['/a/x'])
  assert.equal(firstAllowedPath(tree, canFromPermissions(['b.view'])), '/b/x')
  assert.equal(firstAllowedPath(tree, canFromPermissions([])), null)
  assert(gateAllows({ requireAll: ['x', 'y'], permission: ['z'] }, canFromPermissions(['x', 'y', 'z'])))
  assert(!gateAllows({ requireAll: ['x', 'y'], permission: ['z'] }, canFromPermissions(['x', 'z'])))
  assert(canFromPermissions(['system.admin'])('anything.at_all'))
}

// ── full administration sees everything ────────────────────────────────────
{
  const all = visible(['system.admin'])
  assert.deepEqual(all.groups, NAV_GROUPS.map((g) => g.id))
}

// ── a role with no permissions only keeps its personal pages ───────────────
{
  const none = visible([])
  assert.deepEqual(none.groups, ['administration'])
  assert.deepEqual([...none.labels].sort(), [
    'administration:Settings', 'administration:Settings:Appearance', 'administration:Settings:My Profile', 'administration:Settings:Support',
  ])
  assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions([])), '/settings/general?tab=appearance')
}

// ── the reported bug: a narrow custom role sees only its areas ─────────────
{
  const v = visible(['dashboard.view', 'devices.view'])
  for (const hidden of ['servers', 'apm', 'device-tracker', 'nccm', 'reports', 'inventory']) assert(!v.groups.includes(hidden), `devices-only role sees ${hidden}`)
  assert(v.labels.has('monitoring:Devices') && v.labels.has('monitoring:Link Utilization'))
  // Device events are readable with devices.view (the server's "events" policy); alerts are not.
  assert(v.labels.has('alerting:SNMP Traps') && v.labels.has('alerting:Network Syslog'))
  assert(!v.labels.has('alerting:Active Alerts') && !v.labels.has('alerting:Alert Rules') && !v.labels.has('alerting:Channels'))
  for (const hidden of ['monitoring:NetFlow', 'monitoring:Services', 'monitoring:Discovery', 'monitoring:Network Maps', 'reports:Report Library', 'reports:Schedules', 'administration:Credentials', 'administration:Subscription'])
    assert(!v.labels.has(hidden), `devices-only role sees ${hidden}`)
}

// ── Reports: its own group between Alerting and Administration ─────────────
{
  const ids = NAV_GROUPS.map((g) => g.id)
  assert.equal(ids.indexOf('reports'), ids.indexOf('alerting') + 1, 'Reports follows Alerting')
  assert.equal(ids.indexOf('administration'), ids.indexOf('reports') + 1, 'Reports precedes Administration')
  assert(!NAV_GROUPS.find((g) => g.id === 'administration').items.some((i) => i.to.startsWith('/reports')), 'Reports left Administration')
  const viewer = visible(['reports.view'])
  assert.deepEqual(viewer.groups.filter((g) => g === 'reports'), ['reports'])
  assert(viewer.labels.has('reports:Report Library') && viewer.labels.has('reports:Schedules'))
  assert(!viewer.labels.has('reports:Template Designer'), 'the designer needs reports.manage')
  assert(visible(['reports.view', 'reports.manage']).labels.has('reports:Template Designer'))
  assert(!visible(['reports.manage']).labels.has('reports:Template Designer'), 'the designer also needs reports.view')
}

// ── Inventory & Compliance: after Servers, before Applications; every row (settings included) needs only view ──
{
  const ids = NAV_GROUPS.map((g) => g.id)
  assert.equal(ids.indexOf('inventory'), ids.indexOf('servers') + 1, 'Inventory follows Servers')
  assert.equal(ids.indexOf('apm'), ids.indexOf('inventory') + 1, 'Inventory precedes Applications')
  const group = NAV_GROUPS.find((g) => g.id === 'inventory')
  assert.equal(group.label, 'Inventory & Compliance')
  assert.equal(group.short, 'Inventory', 'UI-23: the collapsed rail names the module, not its first tab')
  assert.notEqual(group.short, group.items[1].label)
  assert.deepEqual(group.items.map((i) => i.label), ['Overview', 'Assets', 'Operating Systems', 'Software', 'Hardware', 'Changes', 'Inventory Settings'])
  assert.deepEqual(group.items.map((i) => i.to), [
    '/inventory', '/inventory/assets', '/inventory/os', '/inventory/software', '/inventory/hardware', '/inventory/changes', '/inventory/settings',
  ])
  // "Inventory" means one thing: the Servers row is All Servers (same route).
  const servers = NAV_GROUPS.find((g) => g.id === 'servers').items
  assert(servers.some((i) => i.label === 'All Servers' && i.to === '/servers/inventory'))
  assert(!servers.some((i) => i.label === 'Inventory'))
  // Every row needs inventory.view, the settings row too: it opens read-only without inventory.manage (UI-07), and
  // a role that holds manage WITHOUT view cannot read the settings at all, so it sees nothing (RG-06).
  for (const item of group.items) {
    assert.deepEqual(gatePermissions(item), ['inventory.view'], item.to)
    assert(!item.children, `${item.to} is a plain row`)
  }
  const viewer = visible(['dashboard.view', 'inventory.view'])
  assert(viewer.groups.includes('inventory'))
  for (const label of ['Overview', 'Assets', 'Operating Systems', 'Software', 'Hardware', 'Changes', 'Inventory Settings']) assert(viewer.labels.has(`inventory:${label}`), label)
  assert(!visible(['inventory.manage']).groups.includes('inventory'), 'manage without view shows nothing (the API refuses the read)')
  assert(visible(['inventory.view', 'inventory.manage']).labels.has('inventory:Inventory Settings'))
  assert(!visible(['inventory.export']).groups.includes('inventory'), 'export alone shows nothing')
  assert(!visible(['servers.view', 'devices.view', 'apm.view']).groups.includes('inventory'), 'estate access alone is not inventory access')
  // The group disappears for a role with none of it.
  assert(!visible([]).groups.includes('inventory'))
  assert(visible(['system.admin']).labels.has('inventory:Inventory Settings'))
}

// ── team roles from the server's built-in definitions ──────────────────────
const teams = {
  network: visible(pyList('_NETWORK_TEAM')),
  systems: visible(pyList('_SYSTEMS_TEAM')),
  application: visible(pyList('_APPLICATION_TEAM')),
  security: visible(pyList('_SECURITY_TEAM')),
}
assert(teams.application.groups.includes('apm') && !teams.application.groups.includes('servers'), 'Application team is separate from Servers')
assert(teams.application.labels.has('overview:Application Team') && !teams.application.labels.has('overview:Dashboard'))
assert(!teams.application.labels.has('monitoring:Devices') && !teams.application.groups.includes('nccm'))
assert(teams.systems.groups.includes('servers') && !teams.systems.groups.includes('apm'), 'Systems team is separate from APM')
assert(teams.systems.labels.has('overview:Server Team') && !teams.systems.labels.has('monitoring:Devices'))
assert(teams.network.labels.has('monitoring:Devices') && teams.network.groups.includes('nccm') && teams.network.groups.includes('device-tracker'))
assert(!teams.network.groups.includes('servers') && !teams.network.groups.includes('apm'))
assert(!teams.network.labels.has('device-tracker:User Logins'), 'user activity is sensitive and not part of the network team')
assert(teams.security.labels.has('device-tracker:User Logins') && teams.security.labels.has('monitoring:NetFlow'))
assert(!teams.security.groups.includes('servers') && !teams.security.groups.includes('apm'))
// Owner decision D9: network, systems and security teams see the inventory (view only); the application team does not.
for (const name of ['network', 'systems', 'security']) {
  assert(teams[name].groups.includes('inventory'), `${name} team sees Inventory`)
  for (const label of ['Overview', 'Assets', 'Operating Systems', 'Software', 'Hardware', 'Changes', 'Inventory Settings']) {
    assert(teams[name].labels.has(`inventory:${label}`), `${name} team: ${label}`)
  }
  assert(!pyList(`_${name.toUpperCase()}_TEAM`).includes('inventory.manage'), `${name} team can read the inventory settings but never change them`)
}
assert(!teams.application.groups.includes('inventory'), 'the application team sees applications, not the estate')
for (const [name, v] of Object.entries(teams)) {
  assert(!v.labels.has('administration:Settings:Users & Access'), `${name} team can open Users & Access`)
  assert(!v.labels.has('alerting:Gateways'), `${name} team can open Gateways`)
}
// Landing pages: each team lands on its own dashboard.
assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions(pyList('_APPLICATION_TEAM'))), '/dashboards/apps')
assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions(pyList('_SYSTEMS_TEAM'))), '/dashboards/servers')
assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions(pyList('_NETWORK_TEAM'))), '/')

// ── direct URLs follow the same rules ──────────────────────────────────────
const allows = (url, perms) => {
  const u = new URL(url, 'http://x')
  return trailAllows(trailForLocation(u.pathname, u.searchParams), canFromPermissions(perms))
}
const app = pyList('_APPLICATION_TEAM')
assert(allows('/apm/services', app) && allows('/apm/rum?tab=sessions', app))
assert(!allows('/servers/inventory', app) && !allows('/servers/123e4567', app), 'server detail pages are gated like Inventory')
assert(!allows('/devices/abc', app) && !allows('/ncm', app) && !allows('/udt/users', app))
assert(!allows('/apm/settings', ['dashboard.view', 'apm.view']), 'APM settings need apm.manage')
// The report viewer belongs to the library (reports.view); the designer needs reports.manage.
assert(allows('/reports/view/availability', ['reports.view']) && !allows('/reports/view/availability', ['devices.view']))
assert(!allows('/reports/builder', ['reports.view']) && allows('/reports/builder?from=availability', ['reports.view', 'reports.manage']))
assert(allows('/reports/schedules', ['reports.view']) && !allows('/reports/schedules', ['devices.view']))
// Inventory: every page needs inventory.view, the product page included; the settings page opens read-only with view only.
const inv = ['inventory.view']
for (const url of ['/inventory', '/inventory/assets?os_family=windows', '/inventory/os', '/inventory/software', '/inventory/software/google-chrome?tab=versions', '/inventory/hardware', '/inventory/changes']) {
  assert(allows(url, inv), `${url} opens with inventory.view`)
  assert(!allows(url, ['devices.view', 'servers.view']), `${url} is closed without inventory.view`)
}
assert(allows('/inventory/settings', inv) && allows('/inventory/settings', ['inventory.view', 'inventory.manage']), 'UI-07: a view-only role reaches the read-only settings page')
assert(!allows('/inventory/settings', ['inventory.manage']) && !allows('/inventory/settings', ['devices.view']), 'RG-06: manage without view opens nothing')
// FE-10: the router matches paths case-insensitively, so the gate must too.
for (const url of ['/Inventory/Settings', '/INVENTORY/SETTINGS', '/Inventory/Assets', '/inventory/Software/Google-Chrome']) {
  assert(!allows(url, ['devices.view']), `${url} is closed without inventory.view, whatever its case`)
  assert(allows(url, inv), `${url} opens with inventory.view`)
}
assert(!allows('/Servers/Inventory', app) && !allows('/DEVICES/abc', app), 'case variants of other gated pages are closed too')
for (const [name, perms] of [['network', pyList('_NETWORK_TEAM')], ['systems', pyList('_SYSTEMS_TEAM')], ['security', pyList('_SECURITY_TEAM')]]) {
  assert(allows('/inventory/assets', perms) && allows('/inventory/software/some-product', perms), `${name} team opens Inventory`)
  assert(allows('/inventory/settings', perms), `${name} team reads the Inventory settings (read-only)`)
}
assert(!allows('/inventory', app) && !allows('/inventory/assets', app), 'the application team has no Inventory')
// Breadcrumbs and group auto-open: the product page belongs to Software, All Servers keeps its own routes.
{
  const at = (url) => { const u = new URL(url, 'http://x'); return trailForLocation(u.pathname, u.searchParams) }
  assert.equal(at('/inventory').item.label, 'Overview')
  assert.equal(at('/Inventory/Settings').item.label, 'Inventory Settings', 'FE-10: a differently cased address finds the same entry')
  assert.equal(at('/inventory/assets?health=stale').item.label, 'Assets')
  assert.equal(at('/inventory/software/google-chrome').item.label, 'Software')
  assert.equal(at('/inventory/software/google-chrome').group.id, 'inventory')
  assert.equal(at('/servers/inventory').group.id, 'servers')
  assert.equal(at('/servers/inventory').item.label, 'All Servers')
  assert.equal(at('/servers/3f2a0000-0000-4000-8000-000000000000').item.label, 'All Servers', 'server detail still belongs to All Servers')
}

// Settings hides its own tabs and falls back, so only the item is route-gated.
assert(allows('/settings/general', []) && allows('/settings/general?tab=company', []))
// Pages without a navigation entry are left to the page and the server.
assert(allows('/sensors/abc', []))

// RG-04, (h), (u): the Servers list is "All Servers" everywhere it is named, so "Inventory" means the module only.
// Grep-style: the labels live in page sources, outside the navigation tree.
{
  const page = (rel) => fs.readFileSync(path.join(root, rel), 'utf8')
  const team = page('src/pages/dashboards/ServerTeamDashboard.tsx')
  assert(team.includes('>All servers →</Link>'), 'Server Team dashboard: the Fleet Status link reads "All servers"')
  assert(team.includes('add one from Servers → All Servers'), 'Server Team dashboard: the empty state names the page that exists')
  assert(!/>Inventory →</.test(team) && !team.includes('Servers → Inventory'), 'Server Team dashboard: no "Inventory" for the Servers list')
  const list = page('src/pages/servers/ServersPage.tsx')
  assert(/<span className="text-text2">All Servers<\/span>/.test(list), 'Servers list: the breadcrumb says All Servers')
  assert(/<Server className="h-5 w-5 text-primary" \/>\s*All Servers\s*<\/h1>/.test(list), 'Servers list: the heading says All Servers')
  assert(!list.includes('Server Inventory</h1>') && !/<span className="text-text2">Inventory<\/span>/.test(list), 'Servers list: the old heading and breadcrumb are gone')
  const fleet = page('src/pages/servers/ServersDashboardPage.tsx')
  assert(fleet.includes('View all servers') && !fleet.includes('View inventory'), 'Server Fleet: the button says View all servers')
  // And the sidebar row they point at.
  const row = NAV_GROUPS.find((g) => g.id === 'servers').items.find((i) => i.to === '/servers/inventory')
  assert.equal(row.label, 'All Servers')
}

console.log('Navigation access: every entry gated by catalog permissions; children inherit; team roles (network, systems, application, security) see only their areas, Inventory included per D9; direct URLs follow the sidebar.')
