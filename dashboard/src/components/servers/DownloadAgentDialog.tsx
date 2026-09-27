import { useMemo } from 'react'
import { Download, Package } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'

// Use the same stable aliases as the official download page, so appliance
// releases never pin users to an older installer from the local package cache.
const DOWNLOAD_BASE = 'https://zentryc.com/downloads/zenplus-agent/stable/'

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

export function DownloadAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  // The official latest aliases may carry a several-hour cache lifetime.
  // Refresh the download URL each time this dialog opens.
  const downloadQuery = useMemo(() => `?download=${Date.now()}`, [open])
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
          {PACKAGES.map((pkg) => (
            <section key={pkg.key} className="space-y-2" aria-labelledby={`agent-package-${pkg.key}`}>
              <div>
                <h3 id={`agent-package-${pkg.key}`} className="text-sm font-medium">{pkg.title}</h3>
                <p className="text-xs text-muted">{pkg.note}</p>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Button asChild variant={pkg.key === 'core' ? 'default' : 'outline'} className="w-full">
                  <a href={`${DOWNLOAD_BASE}${pkg.exe}${downloadQuery}`} download referrerPolicy="no-referrer">
                    <Download aria-hidden="true" /> EXE
                  </a>
                </Button>
                <Button asChild variant="outline" className="w-full">
                  <a href={`${DOWNLOAD_BASE}${pkg.msi}${downloadQuery}`} download referrerPolicy="no-referrer">
                    <Package aria-hidden="true" /> MSI
                  </a>
                </Button>
              </div>
            </section>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
