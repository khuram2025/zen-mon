import fs from 'node:fs'
import path from 'node:path'

const root = process.cwd()
const appPath = path.join(root, 'src/App.tsx')
const layoutPath = path.join(root, 'src/components/Layout.tsx')
const legacySidebarPath = path.join(root, 'src/components/layout/Sidebar.tsx')

const read = (file) => fs.readFileSync(file, 'utf8')

const app = read(appPath)
const layout = read(layoutPath)
const legacySidebar = read(legacySidebarPath)

function fail(message) {
  console.error(`route smoke failed: ${message}`)
  process.exitCode = 1
}

function routePathToUrl(routePath) {
  if (routePath === '/') return '/'
  const cleaned = routePath.replace(/^\/+/, '')
  return `/${cleaned}`
}

const routePaths = new Set()
for (const match of app.matchAll(/<Route\s+path="([^"]+)"/g)) {
  const routePath = match[1]
  if (routePath === '*') continue
  routePaths.add(routePathToUrl(routePath))
}
routePaths.add('/')

for (const parentMatch of app.matchAll(/<Route\s+path="([^"]+)"[\s\S]*?<\/Route>/g)) {
  const parent = routePathToUrl(parentMatch[1])
  for (const childMatch of parentMatch[0].matchAll(/<Route\s+path="([^"]+)"/g)) {
    const child = childMatch[1]
    if (child === parentMatch[1] || child === '*') continue
    routePaths.add(`${parent}/${child}`.replace(/\/+/g, '/'))
  }
}

if (app.includes('path="reports"')) {
  for (const child of ['executive', 'technical', 'business', 'inventory']) {
    if (app.includes(`path="${child}"`)) {
      routePaths.add(`/reports/${child}`)
    }
  }
}

const requiredRoutes = [
  '/',
  '/login',
  '/devices',
  '/devices/:id',
  '/devices/:id/interfaces',
  '/services',
  '/services/groups',
  '/services/maintenance',
  '/services/templates',
  '/services/:id',
  '/services/:id/incidents',
  '/maps',
  '/maps/automated',
  '/maps/manual',
  '/topology',
  '/discovery',
  '/mibs',
  '/alerts',
  '/alert-rules',
  '/reports',
  '/reports/executive',
  '/reports/technical',
  '/reports/business',
  '/reports/inventory',
  '/users',
  '/channels',
  '/notifications',
  '/gateways',
  '/snmp-profiles',
  '/subscription',
  '/settings/general',
]

for (const route of requiredRoutes) {
  if (!routePaths.has(route)) {
    fail(`missing App route ${route}`)
  }
}

const navigableRoutes = new Set([...routePaths].filter((route) => !route.includes(':')))

function assertNavigationLinks(sourceName, source) {
  const links = new Set()
  for (const match of source.matchAll(/\b(?:to|path):?\s*['"`]([^'"`]+)['"`]/g)) {
    links.add(match[1])
  }

  for (const link of links) {
    if (
      link.startsWith('http') ||
      link.startsWith('#') ||
      link.includes('?') ||
      link.includes(':')
    ) {
      continue
    }
    if (!navigableRoutes.has(link)) {
      fail(`${sourceName} links to ${link}, but App.tsx has no matching route`)
    }
  }
}

assertNavigationLinks('Layout', layout)
assertNavigationLinks('legacy Sidebar', legacySidebar)

if (legacySidebar.includes('/service-checks')) {
  fail('legacy Sidebar still links to /service-checks instead of /services')
}

if (!process.exitCode) {
  console.log(`route smoke passed: ${requiredRoutes.length} required routes and dashboard nav links verified`)
}
