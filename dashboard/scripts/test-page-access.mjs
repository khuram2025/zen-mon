// Custom page-access roles: a role limited to selected pages sees only those pages (sidebar, direct URLs, landing
// page), and keeps only the permissions those pages need. The server enforces the same limit from
// server/app/core/page_catalog.json, which this script checks against navigation.ts.
//
//   node scripts/test-page-access.mjs           check (CI)
//   node scripts/test-page-access.mjs --write   regenerate server/app/core/page_catalog.json after a navigation change
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'

const root = process.cwd()
const CATALOG_JSON = path.join(root, '../server/app/core/page_catalog.json')

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
const { pageCatalog, restrictPermissions, filterNavGroups, firstAllowedPath, trailAllows, canFromPermissions, pageReachable } = access

// ── the server's copy of the page catalog is generated from the navigation tree ─────────────────────────────
const entries = pageCatalog(NAV_GROUPS)
const generated = {
  _comment: 'Generated from dashboard/src/components/layout/navigation.ts by dashboard/scripts/test-page-access.mjs --write. Do not edit by hand.',
  version: 1,
  pages: entries,
}
const text = JSON.stringify(generated, null, 2) + '\n'
if (process.argv.includes('--write')) {
  fs.writeFileSync(CATALOG_JSON, text)
  console.log(`Wrote ${path.relative(path.join(root, '..'), CATALOG_JSON)} (${entries.length} pages).`)
}
assert.equal(fs.existsSync(CATALOG_JSON) ? fs.readFileSync(CATALOG_JSON, 'utf8') : '', text,
  'server/app/core/page_catalog.json is out of date: run `node scripts/test-page-access.mjs --write` in dashboard/')

// ── catalog shape ─────────────────────────────────────────────────────────────────────────────────────────────
const permissionsPy = fs.readFileSync(path.join(root, '../server/app/core/permissions.py'), 'utf8')
const known = new Set([...permissionsPy.matchAll(/\(\s*"([a-z_]+\.[a-z_]+)",\s*"/g)].map((m) => m[1]))
const keys = entries.map((e) => e.key)
assert.equal(new Set(keys).size, keys.length, `page keys are unique: ${keys.filter((k, i) => keys.indexOf(k) !== i)}`)
for (const e of entries) {
  for (const g of e.gates) for (const p of [...(g.any || []), ...(g.all || [])]) assert(known.has(p), `${e.key}: unknown permission ${p}`)
  for (const p of e.uses || []) {
    assert(known.has(p), `${e.key}: unknown "uses" permission ${p}`)
    assert(p.endsWith('.view'), `${e.key}: "uses" is read-only data, never ${p}`)
  }
  assert(e.personal || e.gates.length > 0, `${e.key} has no permission and is not personal`)
}
assert.deepEqual(entries.filter((e) => e.personal).map((e) => e.key).sort(), [
  '/settings/general?tab=appearance', '/settings/general?tab=profile', '/settings/general?tab=support',
], 'only the three personal Settings tabs are open to every account')
// Parent gates travel with their children (NetFlow pages need netflow.view).
assert.deepEqual(entries.find((e) => e.key === '/netflow/forensics').gates, [{ any: ['netflow.view'] }])
assert.deepEqual(entries.find((e) => e.key === '/discovery/credentials').gates, [{ any: ['discovery.view'] }, { any: ['devices.manage', 'service_checks.manage'] }])
assert(entries.find((e) => e.key === '/').uses.includes('alerts.view'), 'the NOC dashboard reads alerts')

// ── restriction model (same vectors as server/tests/test_page_access.py) ─────────────────────────────────────
const implies = {}
for (const m of permissionsPy.matchAll(/^\s*"([a-z_]+\.[a-z_]+)": \[([^\]]*)\],/gm)) {
  implies[m[1]] = [...m[2].matchAll(/"([a-z_]+\.[a-z_]+)"/g)].map((x) => x[1])
}
assert.deepEqual(implies['devices.manage'], ['devices.view'])
const restrict = (perms, pages) => restrictPermissions(perms, pages ? new Set(pages) : null, entries, implies).sort()
const helpdesk = ['dashboard.view', 'devices.view', 'devices.manage', 'alerts.view', 'alerts.acknowledge', 'servers.view', 'apm.view', 'reports.view', 'ncm.view', 'audit.view']
// No limit: exactly the role's permissions.
assert.deepEqual(restrict(helpdesk, null), [...helpdesk].sort())
// Devices + Active Alerts: the pages' permissions and what extends them (manage, acknowledge); nothing else.
assert.deepEqual(restrict(helpdesk, ['/devices', '/alerts']), ['alerts.acknowledge', 'alerts.view', 'devices.manage', 'devices.view'])
// A NOC wall limited to the Network Operations dashboard keeps its widgets' read access, not manage or acknowledge.
assert.deepEqual(restrict(helpdesk, ['/']), ['alerts.view', 'apm.view', 'dashboard.view', 'devices.view', 'reports.view'])
// A listed page the permissions cannot open contributes nothing.
assert.deepEqual(restrict(['devices.view'], ['/servers', '/devices']), ['devices.view'])
assert.deepEqual(restrict(['servers.view'], ['/devices']), [])
// Personal pages need nothing; an empty list keeps nothing.
assert.deepEqual(restrict(helpdesk, ['/settings/general?tab=profile']), [])
assert.deepEqual(restrict(helpdesk, []), [])
// Child pages carry their parent gate: Discovery credentials needs discovery.view AND a manage permission.
assert.deepEqual(restrict(['discovery.view', 'discovery.run', 'devices.manage', 'devices.view'], ['/discovery/credentials']), ['devices.manage', 'devices.view', 'discovery.run', 'discovery.view'])
assert.deepEqual(restrict(['devices.manage', 'devices.view'], ['/discovery/credentials']), [])
// Full administration is never limited.
assert.deepEqual(restrict(['system.admin'], ['/devices']), ['system.admin'])

// ── sidebar, landing page and direct URLs follow the page list ───────────────────────────────────────────────
const viewer = ['dashboard.view', 'devices.view', 'alerts.view', 'alerts.acknowledge', 'netflow.view', 'servers.view', 'maps.view', 'service_checks.view']
const menu = (perms, pages) => {
  const groups = filterNavGroups(NAV_GROUPS, canFromPermissions(perms), pages ? new Set(pages) : null)
  const out = []
  for (const g of groups) for (const i of g.items) {
    if (!i.children) out.push(i.to)
    for (const c of i.children || []) out.push(`${i.to}>${c.to}`)
  }
  return out
}
const personal = ['/settings/general>/settings/general?tab=appearance', '/settings/general>/settings/general?tab=support', '/settings/general>/settings/general?tab=profile']
assert.deepEqual(menu(viewer, ['/devices', '/maps/manual']), ['/devices', '/maps/manual', ...personal])
assert.deepEqual(menu(viewer, ['/netflow/forensics']), ['/netflow>/netflow/forensics', ...personal], 'a section keeps only its listed pages')
assert.deepEqual(menu(viewer, ['/apm']), personal, 'a listed page the permissions do not open stays hidden')
assert.equal(menu(viewer, null).length, menu(viewer).length)
assert(menu(viewer, null).includes('/alerts'))
// Landing: the first listed page, not the 403 home.
assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions(viewer), new Set(['/maps/manual', '/devices'])), '/devices')
assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions(viewer), new Set(['/maps/manual'])), '/maps/manual')
assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions(viewer), new Set(['/dashboards/servers', '/maps/manual'])), '/dashboards/servers')
assert.equal(firstAllowedPath(NAV_GROUPS, canFromPermissions(viewer), new Set()), '/settings/general?tab=appearance')

const allows = (url, perms, pages) => {
  const u = new URL(url, 'http://x')
  return trailAllows(trailForLocation(u.pathname, u.searchParams), canFromPermissions(perms), pages ? new Set(pages) : null)
}
const wall = ['/maps/manual', '/dashboards/servers']
assert(allows('/maps/manual?map=abc&mode=live', viewer, wall) && allows('/dashboards/servers', viewer, wall))
assert(!allows('/', viewer, wall), 'the home dashboard is closed (RouteGate sends / to the first listed page)')
for (const url of ['/devices', '/devices/abc', '/alerts', '/servers/inventory', '/servers/abc', '/netflow', '/netflow/forensics', '/services'])
  assert(!allows(url, viewer, wall), `${url} is closed for the wall role`)
// Detail pages belong to their listed section.
assert(allows('/devices/abc', viewer, ['/devices']))
assert(allows('/netflow/exporter/1', viewer, ['/netflow']) && !allows('/netflow/exporter/1', viewer, ['/netflow/forensics']))
assert(allows('/netflow/forensics', viewer, ['/netflow/forensics']) && !allows('/netflow/anomalies', viewer, ['/netflow/forensics']))
// Settings decides its own tabs; personal tabs always open it.
assert(allows('/settings/general?tab=profile', viewer, wall) && allows('/settings/general', viewer, []))
// No list = today's behaviour.
assert(allows('/alerts', viewer, null) && allows('/devices/abc', viewer, null))
// Pages without a navigation entry are left to the page and the server.
assert(allows('/sensors/abc', viewer, wall))

// pageReachable matches the sidebar gate.
for (const e of entries) {
  const can = canFromPermissions(viewer)
  if (e.personal) continue
  const inMenu = menu(viewer, [e.key]).some((x) => x === e.key || x.endsWith(`>${e.key}`))
  assert.equal(pageReachable(e, can), inMenu, `${e.key}: reachable and visible agree`)
}

console.log(`Page access: ${entries.length} pages in the server catalog match navigation.ts; a page-limited role sees, lands on and opens only its pages and keeps only the permissions they need.`)
