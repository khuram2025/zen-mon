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
  CreditCard,
  Download,
  FileBarChart,
  FileCode,
  FileText,
  Fingerprint,
  Gauge,
  GitBranch,
  HardDrive,
  HeartPulse,
  Inbox,
  Key,
  KeyRound,
  Layers,
  LayoutDashboard,
  LayoutTemplate,
  LifeBuoy,
  ListChecks,
  MapPinned,
  Mail,
  Network,
  Palette,
  PieChart,
  Plug,
  Radar,
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

export type NavIcon = React.ComponentType<{ className?: string }>

export type NavNode = {
  /** Router target, may carry a query string (e.g. `/settings/general?tab=users`). */
  to: string
  label: string
  icon: NavIcon
  /** Short line shown in the collapsed-rail flyout and the search results. */
  hint?: string
  /** Active only on an exact pathname match (for section landing pages). */
  end?: boolean
  /** Hidden unless the signed-in role grants this permission. */
  permission?: string
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
function settingsTab(tab: string, label: string, icon: NavIcon, permission?: string): NavNode {
  return {
    to: tab === 'company' ? '/settings/general' : `/settings/general?tab=${tab}`,
    label,
    icon,
    permission,
    match: ({ pathname, params }) =>
      pathname === '/settings/general' && (params.get('tab') || 'company') === tab,
  }
}

export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'overview',
    label: 'Overview',
    short: 'Home',
    icon: LayoutDashboard,
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true, hint: 'Network operations at a glance' },
    ],
  },
  {
    id: 'monitoring',
    label: 'Monitoring',
    short: 'Monitor',
    icon: Radar,
    items: [
      { to: '/devices', label: 'Devices', icon: Router, hint: 'Inventory, health and interfaces' },
      { to: '/link-utilization', label: 'Link Utilization', icon: Gauge, hint: 'Interface bandwidth and errors' },
      { to: '/availability', label: 'Availability', icon: HeartPulse, hint: 'Uptime and SLA compliance' },
      {
        to: '/services',
        label: 'Services',
        icon: Activity,
        hint: 'HTTP, TCP and DNS service checks',
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
        children: [
          { to: '/netflow', label: 'Overview', icon: Network, end: true },
          { to: '/netflow/forensics', label: 'Forensics', icon: Fingerprint },
          { to: '/netflow/anomalies', label: 'Anomalies', icon: AlertTriangle },
          { to: '/netflow/capacity', label: 'Capacity', icon: TrendingUp },
          { to: '/netflow/saved-views', label: 'Saved Views', icon: ListChecks },
        ],
      },
      {
        to: '/udt',
        label: 'User Devices',
        icon: ScanSearch,
        hint: 'Track endpoints to switch and port',
        children: [
          { to: '/udt', label: 'Endpoints', icon: ScanSearch, end: true },
          { to: '/udt/ports', label: 'Switch Ports', icon: Server },
          { to: '/udt/users', label: 'User Logins', icon: UserRound },
          { to: '/udt/classification', label: 'Classification', icon: Shapes },
          { to: '/udt/watch-lists', label: 'Watch Lists', icon: ListChecks },
          { to: '/udt/activity', label: 'Activity', icon: Activity },
          { to: '/udt/settings', label: 'UDT Settings', icon: SlidersHorizontal },
        ],
      },
      {
        to: '/discovery',
        label: 'Discovery',
        icon: Radar,
        hint: 'Scan the network for new devices',
        children: [
          { to: '/discovery', label: 'Profiles', icon: Layers, end: true },
          { to: '/discovery/scheduled', label: 'Scheduled', icon: CalendarClock },
          { to: '/discovery/reports', label: 'Scan Results', icon: FileBarChart },
          { to: '/discovery/imports', label: 'Import Queue', icon: Inbox },
          { to: '/discovery/ignored', label: 'Ignored', icon: SlashSquare },
          { to: '/discovery/credentials', label: 'Credentials', icon: Key },
        ],
      },
      { to: '/maps/manual', label: 'Network Maps', icon: MapPinned, hint: 'Hand-drawn topology views' },
    ],
  },
  {
    id: 'servers',
    label: 'Servers',
    short: 'Servers',
    icon: Server,
    items: [
      { to: '/servers', label: 'Server Overview', icon: Gauge, end: true, hint: 'Fleet CPU, memory and disk' },
      // `/servers/:id` detail pages have no row of their own — Inventory owns them.
      { to: '/servers/inventory', label: 'Inventory', icon: Server, hint: 'Every monitored host', extra: ['/servers/'] },
      { to: '/server-agents', label: 'Agent Fleet', icon: Bot, hint: 'Installed agents and versions' },
      { to: '/agent-policies', label: 'Agent Policies', icon: SlidersHorizontal, hint: 'What each agent collects' },
      { to: '/server-baselines', label: 'Baselines', icon: ClipboardCheck, hint: 'Config drift and compliance' },
    ],
  },
  {
    id: 'apm',
    label: 'Applications',
    short: 'Apps',
    icon: Layers,
    items: [
      { to: '/apm', label: 'APM Overview', icon: Layers, end: true, hint: 'Golden signals across services' },
      { to: '/apm/services', label: 'Services', icon: Boxes, hint: 'Latency, throughput and errors' },
      { to: '/apm/service-map', label: 'Service Map', icon: Network, hint: 'Dependencies between services' },
      { to: '/apm/slos', label: 'SLOs', icon: Target, hint: 'Objectives and error budgets' },
      { to: '/apm/synthetics', label: 'Synthetics', icon: Workflow, hint: 'Scripted uptime journeys' },
      { to: '/apm/errors', label: 'Errors', icon: Bug, hint: 'Grouped exception inbox' },
      { to: '/apm/traces', label: 'Traces', icon: GitBranch, hint: 'Distributed trace explorer' },
      { to: '/apm/usage', label: 'Ingest Usage', icon: BarChart3, hint: 'Span and log volume' },
      { to: '/apm/settings', label: 'APM Settings', icon: SlidersHorizontal },
    ],
  },
  {
    id: 'alerting',
    label: 'Alerting',
    short: 'Alerts',
    icon: Siren,
    items: [
      { to: '/alerts', label: 'Active Alerts', icon: AlertTriangle, badge: 'alerts', hint: 'Everything firing right now' },
      { to: '/alert-rules', label: 'Alert Rules', icon: Bell, hint: 'Thresholds and escalation' },
      { to: '/traps', label: 'SNMP Traps', icon: Inbox, hint: 'Unsolicited device events' },
      { to: '/channels', label: 'Channels', icon: BellRing, hint: 'Where notifications are delivered' },
      { to: '/gateways', label: 'Gateways', icon: Mail, hint: 'SMTP, SMS and webhook transports' },
    ],
  },
  {
    id: 'administration',
    label: 'Administration',
    short: 'Admin',
    icon: SettingsIcon,
    items: [
      {
        to: '/reports',
        label: 'Reports',
        icon: FileText,
        hint: 'Scheduled and on-demand reporting',
        children: [
          { to: '/reports', label: 'Report Library', icon: FileText, end: true },
          { to: '/reports/executive', label: 'Executive', icon: PieChart },
          { to: '/reports/technical', label: 'Technical', icon: BarChart3 },
          { to: '/reports/business', label: 'Business', icon: Building2 },
          { to: '/reports/inventory', label: 'Inventory', icon: Boxes },
          { to: '/reports/apm', label: 'Applications', icon: Layers },
          { to: '/reports/builder', label: 'Custom Builder', icon: LayoutTemplate },
          { to: '/reports/schedules', label: 'Schedules', icon: CalendarClock },
        ],
      },
      { to: '/ncm', label: 'Config Backup', icon: FileCode, hint: 'Device configuration archive' },
      { to: '/credentials', label: 'Credentials', icon: KeyRound, hint: 'SNMP and Windows credentials' },
      {
        to: '/settings/general',
        label: 'Settings',
        icon: SettingsIcon,
        hint: 'Appliance and company configuration',
        // Mirrors the tab strip on the Settings page, permissions included.
        children: [
          settingsTab('company', 'Company', Building2, 'settings.manage'),
          settingsTab('smtp', 'SMTP / Email', Mail, 'settings.manage'),
          settingsTab('appearance', 'Appearance', Palette),
          settingsTab('users', 'Users & Access', Users, 'users.view'),
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
      { to: '/subscription', label: 'Subscription', icon: CreditCard, hint: 'Plan, seats and billing' },
    ],
  },
]

/* ------------------------------------------------------------------ */
/*  Matching helpers                                                   */
/* ------------------------------------------------------------------ */

/** Does this node itself point at the current location? */
export function isNodeActive(node: NavNode, pathname: string, params: URLSearchParams): boolean {
  if (node.match) return node.match({ pathname, params })
  if (node.extra?.some((p) => pathname.startsWith(p))) return true
  const path = node.to.split('?')[0]
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
