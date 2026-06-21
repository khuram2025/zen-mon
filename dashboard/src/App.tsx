import { useEffect, lazy, Suspense } from 'react'
import { Routes, Route, Navigate, useParams } from 'react-router-dom'
import { useAuth } from '@/stores/auth'
import { Layout } from '@/components/Layout'
import { LoginPage } from '@/pages/LoginPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { AvailabilityPage } from '@/pages/AvailabilityPage'
import { DevicesPage } from '@/pages/DevicesPage'
import { DeviceDetailPage } from '@/pages/DeviceDetailPage'
import { DeviceInterfacesPage } from '@/pages/DeviceInterfacesPage'
import { AlertsPage } from '@/pages/AlertsPage'
import { AlertDetailPage } from '@/pages/AlertDetailPage'
import { AlertRulesPage } from '@/pages/AlertRulesPage'
import { TrapsPage } from '@/pages/TrapsPage'
import { NcmPage } from '@/pages/NcmPage'
import { NcmDevicePage } from '@/pages/NcmDevicePage'
import { ServicesPage } from '@/pages/ServicesPage'
import { ServiceCheckDetailPage } from '@/pages/ServiceCheckDetail'
import { ServiceIncidentsPage } from '@/pages/ServiceIncidentsPage'
import { ServiceCheckGroupsPage } from '@/pages/ServiceCheckGroupsPage'
import { ServiceMaintenancePage } from '@/pages/ServiceMaintenancePage'
import { ServiceCheckTemplatesPage } from '@/pages/ServiceCheckTemplatesPage'
import { DiscoveryLayout } from '@/pages/discovery/DiscoveryLayout'
import { ProfilesPage as DiscoveryProfilesPage } from '@/pages/discovery/ProfilesPage'
import { WizardPage as DiscoveryWizardPage } from '@/pages/discovery/WizardPage'
import { RunPage as DiscoveryRunPage } from '@/pages/discovery/RunPage'
import { ProfileDetailPage as DiscoveryProfileDetailPage } from '@/pages/discovery/ProfileDetailPage'
import { ScheduledPage as DiscoveryScheduledPage } from '@/pages/discovery/ScheduledPage'
import { ReportsPage as DiscoveryReportsPage } from '@/pages/discovery/ReportsPage'
import { ImportsPage as DiscoveryImportsPage } from '@/pages/discovery/ImportsPage'
import { IgnoredPage as DiscoveryIgnoredPage } from '@/pages/discovery/IgnoredPage'
import { CredentialsPage as DiscoveryCredentialsPage } from '@/pages/discovery/CredentialsPage'
import { MibLibraryPage } from '@/pages/MibLibraryPage'
import { ManualMapsEntry } from '@/pages/ManualMapsEntry'
import { ReportsPage } from '@/pages/ReportsPage'
import { SensorsPage } from '@/pages/SensorsPage'
import { NetflowPage, NetflowDevicePage, NetflowSectionPage } from '@/pages/NetflowPage'
import { ApmSettingsPage } from '@/pages/apm/ApmSettingsPage'
import { TraceExplorerPage } from '@/pages/apm/TraceExplorerPage'
import { TraceWaterfallPage } from '@/pages/apm/TraceWaterfallPage'
import { ApmOverviewPage } from '@/pages/apm/ApmOverviewPage'
import { ServicesPage as ApmServicesPage } from '@/pages/apm/ServicesPage'
import { ServiceDetailPage as ApmServiceDetailPage } from '@/pages/apm/ServiceDetailPage'
import { ServiceMapPage as ApmServiceMapPage } from '@/pages/apm/ServiceMapPage'
import { ErrorsInboxPage } from '@/pages/apm/ErrorsInboxPage'
import { ErrorIssueDetailPage } from '@/pages/apm/ErrorIssueDetailPage'
import { ServersDashboardPage } from '@/pages/servers/ServersDashboardPage'
import { ServerInventoryPage } from '@/pages/servers/ServersPage'
import { ServerDetailPage } from '@/pages/servers/ServerDetailPage'
import { AgentFleetPage } from '@/pages/servers/AgentFleetPage'
import { AgentPoliciesPage } from '@/pages/servers/AgentPoliciesPage'
import { BaselinesPage } from '@/pages/servers/BaselinesPage'
import { NetflowForensicsPage } from '@/pages/NetflowForensics'
import { NetflowSavedViewsPage } from '@/pages/NetflowSavedViews'
import { NetflowCapacityPage } from '@/pages/NetflowCapacity'
import { NetflowAnomaliesPage } from '@/pages/NetflowAnomalies'

const ExecutiveReport = lazy(() => import('@/pages/reports/ExecutiveReport'))
const TechnicalReport = lazy(() => import('@/pages/reports/TechnicalReport'))
const BusinessReport = lazy(() => import('@/pages/reports/BusinessReport'))
const InventoryReport = lazy(() => import('@/pages/reports/InventoryReport'))

function ReportTabFallback() {
  return <div className="py-10 text-center text-sm text-muted">Loading report…</div>
}
import { UsersPage } from '@/pages/UsersPage'
import { ChannelsPage } from '@/pages/ChannelsPage'
import { GatewaysPage } from '@/pages/GatewaysPage'
import { CredentialsPage } from '@/pages/CredentialsPage'
import { SubscriptionPage } from '@/pages/SubscriptionPage'
import { GeneralSettingsPage } from '@/pages/GeneralSettingsPage'
import { NotFoundPage } from '@/pages/NotFoundPage'

function Protected({ children }: { children: React.ReactNode }) {
  const token = useAuth((s) => s.token)
  if (!token) return <Navigate to="/login" replace />
  return <>{children}</>
}

/* Redirect old /settings/:tab to new routes */
const tabRedirects: Record<string, string> = {
  company: '/settings/general',
  channels: '/channels',
  gateways: '/gateways',
  users: '/users',
  sensors: '/sensors',
  snmp: '/credentials?tab=snmp',
  subscription: '/subscription',
  appearance: '/settings/general',
  profile: '/settings/general',
}

function SettingsRedirect() {
  const { tab } = useParams()
  const target = tab ? tabRedirects[tab] || '/settings/general' : '/settings/general'
  return <Navigate to={target} replace />
}

export default function App() {
  const { token, hydrateUser } = useAuth()

  useEffect(() => {
    if (token) hydrateUser()
  }, [token, hydrateUser])

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <Protected>
            <Layout />
          </Protected>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="availability" element={<AvailabilityPage />} />
        <Route path="devices" element={<DevicesPage />} />
        <Route path="devices/:id" element={<DeviceDetailPage />} />
        <Route path="devices/:id/interfaces" element={<DeviceInterfacesPage />} />
        <Route path="servers" element={<ServersDashboardPage />} />
        <Route path="servers/inventory" element={<ServerInventoryPage />} />
        <Route path="servers/:id" element={<ServerDetailPage />} />
        <Route path="server-agents" element={<AgentFleetPage />} />
        <Route path="agent-policies" element={<AgentPoliciesPage />} />
        <Route path="server-baselines" element={<BaselinesPage />} />
        <Route path="services" element={<ServicesPage />} />
        <Route path="services/groups" element={<ServiceCheckGroupsPage />} />
        <Route path="services/maintenance" element={<ServiceMaintenancePage />} />
        <Route path="services/templates" element={<ServiceCheckTemplatesPage />} />
        <Route path="services/:id" element={<ServiceCheckDetailPage />} />
        <Route path="services/:id/incidents" element={<ServiceIncidentsPage />} />
        <Route path="discovery" element={<DiscoveryLayout />}>
          <Route index element={<DiscoveryProfilesPage />} />
          <Route path="new" element={<DiscoveryWizardPage />} />
          <Route path="scheduled" element={<DiscoveryScheduledPage />} />
          <Route path="reports" element={<DiscoveryReportsPage />} />
          <Route path="imports" element={<DiscoveryImportsPage />} />
          <Route path="ignored" element={<DiscoveryIgnoredPage />} />
          <Route path="credentials" element={<DiscoveryCredentialsPage />} />
          <Route path="profiles/:id" element={<DiscoveryProfileDetailPage />} />
          <Route path="profiles/:id/edit" element={<DiscoveryWizardPage />} />
          <Route path="runs/:id" element={<DiscoveryRunPage />} />
        </Route>
        <Route path="maps" element={<Navigate to="/maps/manual" replace />} />
        <Route path="maps/manual" element={<ManualMapsEntry />} />
        <Route path="netflow" element={<NetflowPage />} />
        <Route path="netflow/forensics" element={<NetflowForensicsPage />} />
        <Route path="netflow/saved-views" element={<NetflowSavedViewsPage />} />
        <Route path="netflow/capacity" element={<NetflowCapacityPage />} />
        <Route path="netflow/anomalies" element={<NetflowAnomaliesPage />} />
        <Route path="netflow/devices/:ip" element={<NetflowDevicePage />} />
        <Route path="netflow/:section" element={<NetflowSectionPage />} />
        <Route path="mibs" element={<MibLibraryPage />} />
        <Route path="sensors" element={<SensorsPage />} />
        <Route path="apm" element={<ApmOverviewPage />} />
        <Route path="apm/services" element={<ApmServicesPage />} />
        <Route path="apm/services/:name" element={<ApmServiceDetailPage />} />
        <Route path="apm/service-map" element={<ApmServiceMapPage />} />
        <Route path="apm/errors" element={<ErrorsInboxPage />} />
        <Route path="apm/errors/:id" element={<ErrorIssueDetailPage />} />
        <Route path="apm/traces" element={<TraceExplorerPage />} />
        <Route path="apm/traces/:traceId" element={<TraceWaterfallPage />} />
        <Route path="apm/settings" element={<ApmSettingsPage />} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="alerts/:id" element={<AlertDetailPage />} />
        <Route path="alert-rules" element={<AlertRulesPage />} />
        <Route path="traps" element={<TrapsPage />} />
        <Route path="ncm" element={<NcmPage />} />
        <Route path="ncm/:deviceId" element={<NcmDevicePage />} />
        <Route path="reports" element={<ReportsPage />}>
          <Route index element={<Navigate to="executive" replace />} />
          <Route path="executive" element={<Suspense fallback={<ReportTabFallback />}><ExecutiveReport /></Suspense>} />
          <Route path="technical" element={<Suspense fallback={<ReportTabFallback />}><TechnicalReport /></Suspense>} />
          <Route path="business" element={<Suspense fallback={<ReportTabFallback />}><BusinessReport /></Suspense>} />
          <Route path="inventory" element={<Suspense fallback={<ReportTabFallback />}><InventoryReport /></Suspense>} />
        </Route>

        {/* Broken-out settings pages */}
        <Route path="users" element={<UsersPage />} />
        <Route path="channels" element={<ChannelsPage />} />
        <Route path="notifications" element={<Navigate to="/channels" replace />} />
        <Route path="gateways" element={<GatewaysPage />} />
        <Route path="credentials" element={<CredentialsPage />} />
        {/* Backwards-compat redirects so cross-links / bookmarks still work */}
        <Route path="snmp-profiles" element={<Navigate to="/credentials?tab=snmp" replace />} />
        <Route path="windows-credentials" element={<Navigate to="/credentials?tab=windows" replace />} />
        <Route path="subscription" element={<SubscriptionPage />} />
        <Route path="settings/general" element={<GeneralSettingsPage />} />

        {/* Backward-compat redirects */}
        <Route path="settings" element={<SettingsRedirect />} />
        <Route path="settings/:tab" element={<SettingsRedirect />} />

        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
