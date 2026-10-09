import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bell,
  BellRing,
  Bot,
  Boxes,
  Bug,
  Building2,
  CalendarClock,
  ClipboardCheck,
  Cpu,
  CreditCard,
  Download,
  FileBarChart,
  FileCode,
  Fingerprint,
  Gauge,
  GitBranch,
  Globe2,
  HardDrive,
  HeartPulse,
  History,
  Inbox,
  Key,
  KeyRound,
  Layers,
  LayoutDashboard,
  LayoutTemplate,
  LibraryBig,
  LifeBuoy,
  ListChecks,
  MapPinned,
  Mail,
  MonitorCog,
  MousePointerClick,
  Network,
  Package,
  PackageSearch,
  Palette,
  Plug,
  Radar,
  Waypoints,
  Router,
  ScanSearch,
  Server,
  Settings as SettingsIcon,
  Shapes,
  ShieldCheck,
  Siren,
  SlashSquare,
  SlidersHorizontal,
  Target,
  TrendingUp,
  Upload,
  UserRound,
  Users,
  Workflow,
  Wrench,
} from 'lucide-react'

import type { NavGate } from './navAccess'

export type NavIcon = React.ComponentType<{ className?: string }>

// Every entry names the permission its page needs (the same one the server's
// route policy checks), so a role only sees what it can open. Children also
// need their parent's permission. scripts/test-nav-access.mjs keeps it so.
export type NavNode = NavGate & {
  /** Router target, may carry a query string (e.g. `/settings/general?tab=users`). */
  to: string
  label: string
  icon: NavIcon
  /** Short line shown in the collapsed-rail flyout and the search results. */
  hint?: string
  /** Active only on an exact pathname match (for section landing pages). */
  end?: boolean
  /** The page hides the tabs a role lacks itself (Settings): only the item is route-gated. */
  selfGated?: boolean
  /** Live counter rendered as a pill on the right of the row. */
  badge?: 'alerts'
  /** Extra path prefixes this node also owns (detail routes that have no row). */
  extra?: string[]
  /** Overrides the default path matching — used by query-string routes. */
  match?: (loc: { pathname: string; params: URLSearchParams }) => boolean
  children?: NavNode[]
}

export type NavGroup = {
  id: string
  label: string
  /** Fits under the icon on the collapsed rail. */
  short: string
  icon: NavIcon
  items: NavNode[]
}

/** Settings is one page with tabs, so its children match on `?tab=`. */
function settingsTab(tab: string, label: string, icon: NavIcon, permission?: string | readonly string[]): NavNode {
  return {
    to: tab === 'company' ? '/settings/general' : `/settings/general?tab=${tab}`,
    label,
    icon,
    permission,
    match: ({ pathname, params }) =>
      pathname === '/settings/general' && (params.get('tab') || 'company') === tab,
  }
}

function rumTab(tab: string, label: string, icon: NavIcon): NavNode {
  return {
    to: tab === 'overview' ? '/apm/rum' : `/apm/rum?tab=${tab}`,
    label,
    icon,
    match: ({ pathname, params }) =>
      pathname === '/apm/rum' && (params.get('tab') || 'overview') === tab,
  }
}

export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'overview',
    label: 'Overview',
    short: 'Home',
    icon: LayoutDashboard,
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true, hint: 'Network operations at a glance', requireAll: ['dashboard.view'], permission: 'devices.view' },
      { to: '/dashboards/network', label: 'Network & Security', icon: ShieldCheck, hint: 'Reachability, traffic, WAN paths and threat signals', requireAll: ['dashboard.view'], permission: ['devices.view', 'netflow.view'] },
      { to: '/dashboards/servers', label: 'Server Team', icon: Server, hint: 'Fleet health, resource pressure and agents', requireAll: ['dashboard.view'], permission: 'servers.view' },
      { to: '/dashboards/apps', label: 'Application Team', icon: Layers, hint: 'Golden signals, SLOs and user journeys', requireAll: ['dashboard.view'], permission: 'apm.view' },
    ],
  },
  {
    id: 'monitoring',
    label: 'Monitoring',
    short: 'Monitor',
    icon: Radar,
    items: [
      { to: '/devices', label: 'Devices', icon: Router, hint: 'Inventory, health and interfaces', permission: 'devices.view' },
      { to: '/link-utilization', label: 'Link Utilization', icon: Gauge, hint: 'Interface bandwidth and errors', permission: 'devices.view' },
      { to: '/availability', label: 'Availability', icon: HeartPulse, hint: 'Uptime and SLA compliance', permission: ['devices.view', 'service_checks.view', 'servers.view'] },
      {
        to: '/services',
        label: 'Services',
        icon: Activity,
        hint: 'HTTP, TCP and DNS service checks',
        permission: 'service_checks.view',
        children: [
          { to: '/services', label: 'All Checks', icon: Activity, end: true },
          { to: '/services/groups', label: 'Check Groups', icon: Boxes },
          { to: '/services/maintenance', label: 'Maintenance', icon: Wrench },
          { to: '/services/templates', label: 'Check Templates', icon: LayoutTemplate },
        ],
      },
      {
        to: '/netflow',
        label: 'NetFlow',
        icon: Network,
        hint: 'Traffic analysis and conversations',
        permission: 'netflow.view',
        children: [
          // Exporter deep dives, host profiles and the top-N section pages belong to the overview.
          { to: '/netflow', label: 'Overview', icon: Network, match: ({ pathname }) => isNetflowOverviewPath(pathname) },
          { to: '/netflow/forensics', label: 'Forensics', icon: Fingerprint },
          { to: '/netflow/anomalies', label: 'Anomalies', icon: AlertTriangle },
          { to: '/netflow/capacity', label: 'Capacity', icon: TrendingUp },
          { to: '/netflow/saved-views', label: 'Saved Views', icon: ListChecks },
          { to: '/netflow/health', label: 'Collector health', icon: HeartPulse, extra: ['/netflow/setup'] },
          { to: '/netflow/settings', label: 'Settings', icon: SlidersHorizontal },
        ],
      },
      {
        to: '/netpath',
        label: 'NetPath',
        icon: Waypoints,
        hint: 'Hop-by-hop path monitoring, inside your network and across the internet',
        permission: 'netpath.view',
        end: false,
      },
      {
        to: '/discovery',
        label: 'Discovery',
        icon: Radar,
        hint: 'Scan the network for new devices',
        permission: 'discovery.view',
        children: [
          { to: '/discovery', label: 'Profiles', icon: Layers, end: true },
          { to: '/discovery/scheduled', label: 'Scheduled', icon: CalendarClock },
          { to: '/discovery/reports', label: 'Scan Results', icon: FileBarChart },
          { to: '/discovery/imports', label: 'Import Queue', icon: Inbox },
          { to: '/discovery/ignored', label: 'Ignored', icon: SlashSquare },
          { to: '/discovery/credentials', label: 'Credentials', icon: Key, permission: ['devices.manage', 'service_checks.manage'] },
        ],
      },
      { to: '/maps/manual', label: 'Network Maps', icon: MapPinned, hint: 'Hand-drawn topology views', permission: 'maps.view', extra: ['/maps'] },
    ],
  },
  {
    id: 'nccm',
    label: 'Network Configuration & Compliance Manager (NCCM)',
    short: 'NCCM',
    icon: FileCode,
    items: [
      { to: '/ncm', label: 'NCCM Overview', icon: FileCode, hint: 'NCM configuration backups, change tracking and compliance', permission: 'ncm.view' },
    ],
  },
  {
    id: 'device-tracker',
    label: 'Device Tracker',
    short: 'Tracker',
    icon: ScanSearch,
    items: [
      { to: '/udt', label: 'Endpoints', icon: ScanSearch, end: true, extra: ['/udt/endpoints/'], hint: 'Track endpoints, IP history, switches and ports', permission: 'udt.view' },
      { to: '/udt/ports', label: 'Switch Ports', icon: Server, permission: 'udt.view' },
      { to: '/udt/users', label: 'User Logins', icon: UserRound, requireAll: ['udt.view'], permission: ['udt.view_users', 'udt.manage'] },
      { to: '/udt/classification', label: 'Classification', icon: Shapes, permission: 'udt.view' },
      { to: '/udt/watch-lists', label: 'Watch Lists', icon: ListChecks, permission: 'udt.view' },
      { to: '/udt/activity', label: 'Activity', icon: Activity, permission: 'udt.view' },
      { to: '/udt/settings', label: 'Tracker Settings', icon: SlidersHorizontal, permission: 'udt.manage' },
    ],
  },
  {
    id: 'servers',
    label: 'Servers',
    short: 'Servers',
    icon: Server,
    items: [
      { to: '/servers', label: 'Server Overview', icon: Gauge, end: true, hint: 'Fleet CPU, memory and disk', permission: 'servers.view' },
      // `/servers/:id` detail pages have no row of their own — All Servers owns them. ("Inventory"
      // now means the Inventory & Compliance module below, so this row is not called that.)
      { to: '/servers/inventory', label: 'All Servers', icon: Server, hint: 'Every monitored host', extra: ['/servers/'], permission: 'servers.view' },
      { to: '/server-agents', label: 'Agent Fleet', icon: Bot, hint: 'Installed agents and versions', permission: 'servers.view' },
      { to: '/agent-policies', label: 'Agent Policies', icon: SlidersHorizontal, hint: 'What each agent collects', permission: 'servers.view' },
      { to: '/server-baselines', label: 'Baselines', icon: ClipboardCheck, hint: 'Config drift and compliance', permission: 'servers.view' },
    ],
  },
  {
    // One module for what the estate is made of: operating systems, software and hardware across
    // servers and network devices (Documentation/Inventory/03-UI-UX-SPEC.md section 1.1). Rows ship
    // with their phase; Lifecycle (P4) and Compliance (P5) join this group when their pages exist.
    id: 'inventory',
    label: 'Inventory & Compliance',
    short: 'Inventory',
    icon: PackageSearch,
    items: [
      { to: '/inventory', label: 'Overview', icon: Gauge, end: true, hint: 'Every OS, software title and hardware model at a glance', permission: 'inventory.view' },
      { to: '/inventory/assets', label: 'Assets', icon: Boxes, hint: 'Servers and network devices in one list', permission: 'inventory.view' },
      { to: '/inventory/os', label: 'Operating Systems', icon: MonitorCog, hint: 'OS and firmware releases, versions and builds', permission: 'inventory.view' },
      { to: '/inventory/software', label: 'Software', icon: Package, extra: ['/inventory/software/'], hint: 'Installed software catalog and versions', permission: 'inventory.view' },
      { to: '/inventory/hardware', label: 'Hardware', icon: Cpu, hint: 'Vendors, models, modules and server hardware', permission: 'inventory.view' },
      { to: '/inventory/changes', label: 'Changes', icon: History, hint: 'Installs, removals, upgrades and firmware changes', permission: 'inventory.view' },
      // inventory.view opens the page; without inventory.manage it is read-only (a note names the permission). A role
      // that holds manage WITHOUT view sees nothing: the API refuses the settings read without view (review finding RG-06).
      { to: '/inventory/settings', label: 'Inventory Settings', icon: SlidersHorizontal, hint: 'Freshness targets and change retention', permission: 'inventory.view' },
    ],
  },
  {
    id: 'apm',
    label: 'Applications',
    short: 'Apps',
    icon: Layers,
    items: [
      { to: '/apm', label: 'APM Overview', icon: Layers, end: true, hint: 'Golden signals across services', permission: 'apm.view' },
      { to: '/apm/services', label: 'Services', icon: Boxes, hint: 'Latency, throughput and errors', permission: 'apm.view' },
      { to: '/apm/service-map', label: 'Service Map', icon: Network, hint: 'Dependencies between services', permission: 'apm.view' },
      { to: '/apm/slos', label: 'SLOs', icon: Target, hint: 'Objectives and error budgets', permission: 'apm.view' },
      { to: '/apm/synthetics', label: 'Synthetics', icon: Workflow, hint: 'Scripted uptime journeys', permission: 'apm.view' },
      { to: '/apm/errors', label: 'Errors', icon: Bug, hint: 'Grouped exception inbox', permission: 'apm.view' },
      { to: '/apm/traces', label: 'Traces', icon: GitBranch, hint: 'Distributed trace explorer', permission: 'apm.view' },
      {
        to: '/apm/rum',
        label: 'RUM',
        icon: Globe2,
        hint: 'Real-user vitals and sessions',
        permission: 'apm.view',
        children: [
          rumTab('overview', 'Overview', LayoutDashboard),
          rumTab('web-vitals', 'Web Vitals', Gauge),
          rumTab('views', 'Views', FileBarChart),
          rumTab('sessions', 'Sessions', Users),
          rumTab('errors', 'JS Errors', Bug),
          rumTab('resources', 'Resources', HardDrive),
          rumTab('actions', 'Actions', MousePointerClick),
        ],
      },
      { to: '/apm/usage', label: 'Usage', icon: BarChart3, hint: 'Pages, users and span volume', permission: 'apm.view' },
      { to: '/apm/settings', label: 'APM Settings', icon: SlidersHorizontal, permission: 'apm.manage' },
    ],
  },
  {
    id: 'alerting',
    label: 'Alerting',
    short: 'Alerts',
    icon: Siren,
    items: [
      { to: '/alerts', label: 'Active Alerts', icon: AlertTriangle, badge: 'alerts', hint: 'Everything firing right now', permission: 'alerts.view' },
      { to: '/alert-rules', label: 'Alert Rules', icon: Bell, hint: 'Thresholds and escalation', permission: 'alerts.view' },
      { to: '/traps', label: 'SNMP Traps', icon: Inbox, hint: 'Unsolicited device events', permission: ['alerts.view', 'devices.view'] },
      { to: '/network-events', label: 'Network Syslog', icon: Inbox, hint: 'Device log events and alert rules', permission: ['alerts.view', 'devices.view'] },
      { to: '/channels', label: 'Channels', icon: BellRing, hint: 'Where notifications are delivered', permission: ['alerts.manage', 'settings.manage'] },
      { to: '/gateways', label: 'Gateways', icon: Mail, hint: 'SMTP, SMS and webhook transports', permission: 'settings.manage' },
    ],
  },
  {
    // Reports are consumed, not configured, so they have their own group.
    // Every entry needs reports.view (the designer also reports.manage).
    // The viewer (/reports/view/*) belongs to the library; the legacy persona
    // paths only redirect to their templates.
    id: 'reports',
    label: 'Reports',
    short: 'Reports',
    icon: FileBarChart,
    items: [
      { to: '/reports', label: 'Report Library', icon: LibraryBig, end: true, extra: ['/reports/view/'], hint: 'Built-in and custom report templates', permission: 'reports.view' },
      { to: '/reports/builder', label: 'Template Designer', icon: LayoutTemplate, hint: 'Build and customize report templates', requireAll: ['reports.view'], permission: 'reports.manage' },
      { to: '/reports/schedules', label: 'Schedules', icon: CalendarClock, hint: 'Scheduled delivery by email and channels', permission: 'reports.view' },
    ],
  },
  {
    id: 'administration',
    label: 'Administration',
    short: 'Admin',
    icon: SettingsIcon,
    items: [
      { to: '/credentials', label: 'Credentials', icon: KeyRound, hint: 'SNMP, Windows, and service credentials', permission: ['devices.manage', 'service_checks.manage'] },
      {
        to: '/settings/general',
        label: 'Settings',
        icon: SettingsIcon,
        hint: 'Appliance and company configuration',
        // Mirrors the tab strip on the Settings page, permissions included.
        // The page falls back to a tab the role holds, so only the item is route-gated.
        selfGated: true,
        children: [
          settingsTab('company', 'Company', Building2, 'settings.manage'),
          settingsTab('smtp', 'SMTP / Email', Mail, 'settings.manage'),
          settingsTab('appearance', 'Appearance', Palette),
          settingsTab('users', 'Users & Access', Users, ['users.view', 'users.manage', 'roles.view', 'roles.manage', 'system.admin']),
          settingsTab('security', 'Security & TLS', ShieldCheck, 'system.admin'),
          settingsTab('licenses', 'Licenses', KeyRound, 'system.admin'),
          settingsTab('updates', 'Updates', Download, 'system.admin'),
          settingsTab('storage', 'Storage', HardDrive, 'system.admin'),
          settingsTab('sensors', 'Sensors', Plug, 'settings.manage'),
          settingsTab('templates', 'Monitoring Templates', LayoutTemplate, 'settings.manage'),
          settingsTab('mibs', 'MIB Library', Upload, 'settings.manage'),
          settingsTab('support', 'Support', LifeBuoy),
          settingsTab('profile', 'My Profile', UserRound),
        ],
      },
      { to: '/subscription', label: 'Subscription', icon: CreditCard, hint: 'Plan, seats and billing', permission: 'settings.view' },
    ],
  },
]

/* ------------------------------------------------------------------ */
/*  Matching helpers                                                   */
/* ------------------------------------------------------------------ */

/** Does this node itself point at the current location? */
/** NetFlow pages that have their own menu entry; every other /netflow/* page is part of the overview. */
const NETFLOW_OWN_PAGES = ['forensics', 'anomalies', 'capacity', 'saved-views', 'health', 'setup', 'settings']

function isNetflowOverviewPath(pathname: string): boolean {
  if (pathname === '/netflow' || pathname === '/netflow/') return true
  if (!pathname.startsWith('/netflow/')) return false
  return !NETFLOW_OWN_PAGES.includes(pathname.slice('/netflow/'.length).split('/')[0])
}

export function isNodeActive(node: NavNode, rawPathname: string, params: URLSearchParams): boolean {
  // The router matches paths case-insensitively (/Inventory/Settings is the Settings page), so the gate and the
  // sidebar must too, or a differently spelled address would skip the gate (review finding FE-10).
  // The router also ignores a trailing slash (/reports/ is the Reports page): an `end` node compared the raw text, so
  // that address matched no entry and the gate let the page render (review finding NF-FE-4).
  const pathname = (rawPathname.length > 1 ? rawPathname.replace(/\/+$/, '') : rawPathname).toLowerCase() || '/'
  if (node.match) return node.match({ pathname, params })
  if (node.extra?.some((p) => pathname.startsWith(p.toLowerCase()))) return true
  const path = node.to.split('?')[0].toLowerCase()
  if (node.end) return pathname === path
  return pathname === path || pathname.startsWith(`${path}/`)
}

/** True when the node or any descendant owns the current location. */
export function isBranchActive(node: NavNode, pathname: string, params: URLSearchParams): boolean {
  if (isNodeActive(node, pathname, params)) return true
  return (node.children || []).some((c) => isBranchActive(c, pathname, params))
}

/** The group that owns a location — used to auto-open the right section. */
export function groupForLocation(pathname: string, params: URLSearchParams): NavGroup | null {
  let best: { len: number; group: NavGroup } | null = null
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (!isBranchActive(item, pathname, params)) continue
      const len = item.to.split('?')[0].length
      if (!best || len > best.len) best = { len, group }
    }
  }
  return best?.group ?? null
}

/** Breadcrumb trail: [group, item, child?] for the current location. */
export function trailForLocation(
  pathname: string,
  params: URLSearchParams,
): { group: NavGroup; item: NavNode; child?: NavNode } | null {
  let best: { len: number; trail: { group: NavGroup; item: NavNode; child?: NavNode } } | null = null
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      for (const child of item.children || []) {
        if (isNodeActive(child, pathname, params)) {
          const len = child.to.length
          if (!best || len > best.len) best = { len, trail: { group, item, child } }
        }
      }
      if (isNodeActive(item, pathname, params)) {
        const len = item.to.split('?')[0].length
        if (!best || len > best.len) best = { len, trail: { group, item } }
      }
    }
  }
  return best?.trail ?? null
}
