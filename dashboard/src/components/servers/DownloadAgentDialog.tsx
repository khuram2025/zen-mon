import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, ExternalLink, Package } from 'lucide-react'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'

// The appliance serves the packages published in its artifact store, the same
// ones Agent Fleet upgrades agents to, and works without internet access. The
// official download site is offered as well, and is the fallback when the
// appliance has no package of a flavour.
const APPLIANCE_BASE = '/api/v1/agents/packages/windows/latest'
const WEBSITE_BASE = 'https://zentryc.com/downloads/zenplus-agent/stable/'

// Two packages per release: server monitoring only, and server monitoring
// with the APM components. The APM package keeps the historical aliases.
const PACKAGES = [
  {
    key: 'core',
    title: 'Server monitoring',
    note: 'Recommended. Lightweight package for CPU, memory, disk, network, services and event logs.',
    exe: 'ZenPlusAgentCoreSetup-latest.exe',
    msi: 'ZenPlusAgentCore-latest.msi',
  },
  {
    key: 'full',
    title: 'Server monitoring + APM',
    note: 'Adds application performance monitoring for .NET, Java, Node.js and Python.',
    exe: 'ZenPlusAgentSetup-latest.exe',
    msi: 'ZenPlusAgent-latest.msi',
  },
] as const

interface PublishedPackage {
  platform: string
  version: string
  flavor?: 'core' | 'full'
  is_latest: boolean
}

export function DownloadAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  // The official latest aliases may carry a several-hour cache lifetime.
  // Refresh the website URL each time this dialog opens.
  const downloadQuery = useMemo(() => `?download=${Date.now()}`, [open])
  const { data } = useQuery<{ items: PublishedPackage[] }>({
    queryKey: ['agent-packages'],
    queryFn: async () => (await api.get('/agent-fleet/packages')).data,
    enabled: open,
  })
  const applianceVersion = (flavor: string) => (data?.items ?? []).find(
    (p) => p.platform === 'windows' && p.is_latest && (p.flavor ?? 'full') === flavor,
  )?.version

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md w-[calc(100%_-_2rem)]">
        <DialogHeader>
          <DialogTitle>Download ZenPlus Agent</DialogTitle>
          <DialogDescription>
            Choose server monitoring only, or add APM for servers that run
            applications you want to trace. You can add APM later by installing
            the APM package over the server-monitoring one.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {PACKAGES.map((pkg) => {
            const version = applianceVersion(pkg.key)
            return (
              <section key={pkg.key} className="space-y-2" aria-labelledby={`agent-package-${pkg.key}`}>
                <div>
                  <h3 id={`agent-package-${pkg.key}`} className="text-sm font-medium">
                    {pkg.title}
                    {version && <span className="ml-1.5 font-mono text-xs font-normal text-muted">v{version}</span>}
                  </h3>
                  <p className="text-xs text-muted">{pkg.note}</p>
                </div>
                {version ? (
                  <div className="grid grid-cols-2 gap-2">
                    <Button asChild variant={pkg.key === 'core' ? 'default' : 'outline'} className="w-full">
                      <a href={pkg.key === 'full' ? APPLIANCE_BASE : `${APPLIANCE_BASE}?flavor=${pkg.key}`} download>
                        <Download aria-hidden="true" /> Download from appliance
                      </a>
                    </Button>
                    <Button asChild variant="ghost" className="w-full">
                      <a href={`${WEBSITE_BASE}${pkg.msi}${downloadQuery}`} download referrerPolicy="no-referrer">
                        <ExternalLink aria-hidden="true" /> MSI from zentryc.com
                      </a>
                    </Button>
                  </div>
                ) : (
                  <div className="grid grid-cols-2 gap-2">
                    <Button asChild variant={pkg.key === 'core' ? 'default' : 'outline'} className="w-full">
                      <a href={`${WEBSITE_BASE}${pkg.exe}${downloadQuery}`} download referrerPolicy="no-referrer">
                        <Download aria-hidden="true" /> EXE
                      </a>
                    </Button>
                    <Button asChild variant="outline" className="w-full">
                      <a href={`${WEBSITE_BASE}${pkg.msi}${downloadQuery}`} download referrerPolicy="no-referrer">
                        <Package aria-hidden="true" /> MSI
                      </a>
                    </Button>
                  </div>
                )}
              </section>
            )
          })}
          <p className="text-xs text-muted">
            Packages are signed by Zentryc LLC. Agents installed from either
            source are upgraded from this appliance in Agent Fleet.
          </p>
        </div>
      </DialogContent>
    </Dialog>
  )
}
