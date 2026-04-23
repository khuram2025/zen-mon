import { useEffect } from 'react'
import { Routes, Route, Navigate, useParams } from 'react-router-dom'
import { useAuth } from '@/stores/auth'
import { Layout } from '@/components/Layout'
import { LoginPage } from '@/pages/LoginPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { DevicesPage } from '@/pages/DevicesPage'
import { DeviceDetailPage } from '@/pages/DeviceDetailPage'
import { AlertsPage } from '@/pages/AlertsPage'
import { AlertRulesPage } from '@/pages/AlertRulesPage'
import { ServicesPage } from '@/pages/ServicesPage'
import { ServiceCheckDetailPage } from '@/pages/ServiceCheckDetail'
import { ServiceCheckGroupsPage } from '@/pages/ServiceCheckGroupsPage'
import { ServiceMaintenancePage } from '@/pages/ServiceMaintenancePage'
import { ServiceCheckTemplatesPage } from '@/pages/ServiceCheckTemplatesPage'
import { DiscoveryPage } from '@/pages/DiscoveryPage'
import { MibLibraryPage } from '@/pages/MibLibraryPage'
import { ReportsPage } from '@/pages/ReportsPage'
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
        <Route path="services" element={<ServicesPage />} />
        <Route path="services/groups" element={<ServiceCheckGroupsPage />} />
        <Route path="services/maintenance" element={<ServiceMaintenancePage />} />
        <Route path="services/templates" element={<ServiceCheckTemplatesPage />} />
        <Route path="services/:id" element={<ServiceCheckDetailPage />} />
        <Route path="discovery" element={<DiscoveryPage />} />
        <Route path="mibs" element={<MibLibraryPage />} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="alert-rules" element={<AlertRulesPage />} />
        <Route path="reports" element={<ReportsPage />} />

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
