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
import { NetflowForensicsPage } from '@/pages/NetflowForensics'
import { NetflowSavedViewsPage } from '@/pages/NetflowSavedViews'
import { NetflowCapacityPage } from '@/pages/NetflowCapacity'
import { NetflowAnomaliesPage } from '@/pages/NetflowAnomalies'

const ExecutiveReport = lazy(() => import('@/pages/reports/ExecutiveReport'))
const TechnicalReport = lazy(() => import('@/pages/reports/TechnicalReport'))
const BusinessReport = lazy(() => import('@/pages/reports/BusinessReport'))
const InventoryReport = lazy(() => import('@/pages/reports/InventoryReport'))
const ServersPage = lazy(() => import('@/pages/ServersPage'))
const AddServer = lazy(() => import('@/pages/AddServer'))
const ServerDetail = lazy(() => import('@/pages/ServerDetail'))
const AgentFleetPage = lazy(() => import('@/pages/AgentFleetPage'))
const AgentPoliciesPage = lazy(() => import('@/pages/AgentPoliciesPage'))

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
        <Route path="devices" element={<DevicesPage />} />
        <Route path="devices/:id" element={<DeviceDetailPage />} />
        <Route path="devices/:id/interfaces" element={<DeviceInterfacesPage />} />
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
        <Route path="servers" element={<Suspense fallback={<ReportTabFallback />}><ServersPage /></Suspense>} />
        <Route path="servers/new" element={<Suspense fallback={<ReportTabFallback />}><AddServer /></Suspense>} />
        <Route path="servers/:id" element={<Suspense fallback={<ReportTabFallback />}><ServerDetail /></Suspense>} />
        <Route path="agent-fleet" element={<Suspense fallback={<ReportTabFallback />}><AgentFleetPage /></Suspense>} />
        <Route path="agent-policies" element={<Suspense fallback={<ReportTabFallback />}><AgentPoliciesPage /></Suspense>} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="alerts/:id" element={<AlertDetailPage />} />
        <Route path="alert-rules" element={<AlertRulesPage />} />
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
