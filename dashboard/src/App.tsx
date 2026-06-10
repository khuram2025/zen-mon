import { useEffect, lazy, Suspense } from 'react'
import { Routes, Route, Navigate, useParams } from 'react-router-dom'
import { useAuth } from '@/stores/auth'
import { Layout } from '@/components/Layout'
import { LoginPage } from '@/pages/LoginPage'
import { DashboardPage } from '@/pages/DashboardPage'
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
import { TopologyPage } from '@/pages/TopologyPage'
import { ManualMapsPage } from '@/pages/ManualMapsPage'
import { ReportsPage } from '@/pages/ReportsPage'
import { SensorsPage } from '@/pages/SensorsPage'
import { NetflowPage, NetflowDevicePage, NetflowSectionPage } from '@/pages/NetflowPage'
import { ServersPage } from '@/pages/servers/ServersPage'
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
import { SnmpProfilesPage } from '@/pages/SnmpProfilesPage'
import { WindowsCredentialsPage } from '@/pages/WindowsCredentialsPage'
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
  snmp: '/snmp-profiles',
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
        <Route path="devices" element={<DevicesPage />} />
        <Route path="devices/:id" element={<DeviceDetailPage />} />
        <Route path="devices/:id/interfaces" element={<DeviceInterfacesPage />} />
        <Route path="servers" element={<ServersPage />} />
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
        <Route path="maps" element={<Navigate to="/maps/automated" replace />} />
        <Route path="maps/automated" element={<TopologyPage />} />
        <Route path="maps/manual" element={<ManualMapsPage />} />
        <Route path="topology" element={<Navigate to="/maps/automated" replace />} />
        <Route path="netflow" element={<NetflowPage />} />
        <Route path="netflow/forensics" element={<NetflowForensicsPage />} />
        <Route path="netflow/saved-views" element={<NetflowSavedViewsPage />} />
        <Route path="netflow/capacity" element={<NetflowCapacityPage />} />
        <Route path="netflow/anomalies" element={<NetflowAnomaliesPage />} />
        <Route path="netflow/devices/:ip" element={<NetflowDevicePage />} />
        <Route path="netflow/:section" element={<NetflowSectionPage />} />
        <Route path="mibs" element={<MibLibraryPage />} />
        <Route path="sensors" element={<SensorsPage />} />
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
        <Route path="snmp-profiles" element={<SnmpProfilesPage />} />
        <Route path="windows-credentials" element={<WindowsCredentialsPage />} />
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
