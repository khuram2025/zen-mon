import { useNavigate } from 'react-router-dom'
import { ArrowUpRight, Key, ShieldCheck } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'

/**
 * Discovery uses two credential stores: SNMP (for SNMP-enabled
 * network devices) and Windows (for WMI / WinRM). Both have their
 * own dedicated management pages.
 */
export function CredentialsPage() {
  const navigate = useNavigate()
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Card>
        <CardContent className="flex flex-col items-start gap-3 p-6">
          <div className="flex items-center gap-2">
            <Key className="h-5 w-5 text-primary" />
            <h2 className="text-lg font-semibold">SNMP credentials</h2>
          </div>
          <p className="text-sm text-muted">
            v1/v2c communities and v3 user accounts used for SNMP discovery and
            ongoing polling.
          </p>
          <Button onClick={() => navigate('/snmp-profiles')}>
            <Key className="h-4 w-4" /> Manage SNMP credentials
            <ArrowUpRight className="h-3.5 w-3.5" />
          </Button>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="flex flex-col items-start gap-3 p-6">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-primary" />
            <h2 className="text-lg font-semibold">Windows credentials</h2>
          </div>
          <p className="text-sm text-muted">
            Service accounts used by WMI / WinRM probes to inventory Windows
            hosts (NTLM, Kerberos, Basic, CredSSP).
          </p>
          <Button onClick={() => navigate('/windows-credentials')}>
            <ShieldCheck className="h-4 w-4" /> Manage Windows credentials
            <ArrowUpRight className="h-3.5 w-3.5" />
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
