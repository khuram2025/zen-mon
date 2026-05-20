import { useSearchParams } from 'react-router-dom'
import { Key, ShieldCheck } from 'lucide-react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { SnmpProfilesPage } from './SnmpProfilesPage'
import { WindowsCredentialsPage } from './WindowsCredentialsPage'

/**
 * Unified Credentials page hosting SNMP and Windows credential management
 * as tabs. Each tab embeds the original page component with `hideHeader`
 * so the per-page h1/description doesn't double up under this page's
 * heading. Tab selection is reflected in the URL via ?tab= so deep links
 * and the legacy /snmp-profiles / /windows-credentials redirects all land
 * on the right tab.
 */

const TABS = [
  { value: 'snmp', label: 'SNMP', icon: Key },
  { value: 'windows', label: 'Windows', icon: ShieldCheck },
] as const

type TabValue = typeof TABS[number]['value']

export function CredentialsPage() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab') as TabValue | null
  const active: TabValue = TABS.some((t) => t.value === requested) ? (requested as TabValue) : 'snmp'

  function setActive(v: string) {
    const next = new URLSearchParams(params)
    if (v === 'snmp') next.delete('tab')
    else next.set('tab', v)
    setParams(next, { replace: true })
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          <Key className="h-5 w-5 text-primary" /> Credentials
        </h1>
        <p className="text-xs text-muted">
          Reusable credentials used by monitoring, discovery, and device
          management. SNMP communities/USM users for network devices,
          WMI/WinRM credentials for Windows hosts.
        </p>
      </div>

      <Tabs value={active} onValueChange={setActive}>
        <TabsList className="h-auto flex-wrap gap-1 bg-surface2/50 p-1">
          {TABS.map((t) => (
            <TabsTrigger key={t.value} value={t.value} className="gap-1.5">
              <t.icon className="h-3.5 w-3.5" />
              <span>{t.label}</span>
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="snmp">
          <SnmpProfilesPage hideHeader />
        </TabsContent>
        <TabsContent value="windows">
          <WindowsCredentialsPage hideHeader />
        </TabsContent>
      </Tabs>
    </div>
  )
}
