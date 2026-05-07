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
import { AlertRulesPage } from '@/pages/AlertRulesPage'
import { ServicesPage } from '@/pages/ServicesPage'
import { ServiceCheckDetailPage } from '@/pages/ServiceCheckDetail'
import { ServiceIncidentsPage } from '@/pages/ServiceIncidentsPage'
import { ServiceCheckGroupsPage } from '@/pages/ServiceCheckGroupsPage'
import { ServiceMaintenancePage } from '@/pages/ServiceMaintenancePage'
import { ServiceCheckTemplatesPage } from '@/pages/ServiceCheckTemplatesPage'
import { DiscoveryPage } from '@/pages/DiscoveryPage'
import { MibLibraryPage } from '@/pages/MibLibraryPage'
import { ReportsPage } from '@/pages/ReportsPage'
import { NetflowPage, NetflowDevicePage } from '@/pages/NetflowPage'
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
import { NotificationsPage } from '@/pages/NotificationsPage'
import { GatewaysPage } from '@/pages/GatewaysPage'
import { SnmpProfilesPage } from '@/pages/SnmpProfilesPage'
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
  channels: '/notifications',
  gateways: '/gateways',
  users: '/users',
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
        <Route path="services" element={<ServicesPage />} />
        <Route path="services/groups" element={<ServiceCheckGroupsPage />} />
        <Route path="services/maintenance" element={<ServiceMaintenancePage />} />
        <Route path="services/templates" element={<ServiceCheckTemplatesPage />} />
        <Route path="services/:id" element={<ServiceCheckDetailPage />} />
        <Route path="services/:id/incidents" element={<ServiceIncidentsPage />} />
        <Route path="discovery" element={<DiscoveryPage />} />
        <Route path="netflow" element={<NetflowPage />} />
        <Route path="netflow/forensics" element={<NetflowForensicsPage />} />
        <Route path="netflow/saved-views" element={<NetflowSavedViewsPage />} />
        <Route path="netflow/capacity" element={<NetflowCapacityPage />} />
        <Route path="netflow/anomalies" element={<NetflowAnomaliesPage />} />
        <Route path="netflow/devices/:ip" element={<NetflowDevicePage />} />
        <Route path="mibs" element={<MibLibraryPage />} />
        <Route path="alerts" element={<AlertsPage />} />
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
        <Route path="notifications" element={<NotificationsPage />} />
        <Route path="gateways" element={<GatewaysPage />} />
        <Route path="snmp-profiles" element={<SnmpProfilesPage />} />
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
