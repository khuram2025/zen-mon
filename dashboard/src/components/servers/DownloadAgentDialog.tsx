import { useMemo } from 'react'
import { Download, Package } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'

// Use the same stable aliases as the official download page, so appliance
// releases never pin users to an older installer from the local package cache.
const DOWNLOAD_BASE = 'https://zentryc.com/downloads/zenplus-agent/stable/'

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
      <DialogContent className="max-w-sm w-[calc(100%_-_2rem)]">
        <DialogHeader>
          <DialogTitle>Download ZenPlus Agent</DialogTitle>
          <DialogDescription className="sr-only">
            Download the latest Windows agent as an EXE or MSI installer.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
            <Button asChild className="w-full">
              <a href={`${DOWNLOAD_BASE}ZenPlusAgentSetup-latest.exe${downloadQuery}`} download referrerPolicy="no-referrer">
                <Download aria-hidden="true" /> Download EXE
              </a>
            </Button>
            <Button asChild variant="outline" className="w-full">
              <a href={`${DOWNLOAD_BASE}ZenPlusAgent-latest.msi${downloadQuery}`} download referrerPolicy="no-referrer">
                <Package aria-hidden="true" /> Download MSI
              </a>
            </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
