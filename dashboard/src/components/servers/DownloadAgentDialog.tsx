import { Download, Package } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
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
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg w-[calc(100%_-_2rem)]">
        <DialogHeader>
          <DialogTitle>Download ZenPlus Agent</DialogTitle>
          <DialogDescription>
            Windows 10/11 and Windows Server · 64-bit
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="rounded-lg border border-primary/30 bg-primary/5 p-4">
            <div className="mb-3">
              <h3 className="text-sm font-semibold">Guided installer</h3>
              <p className="mt-1 text-sm text-muted">Recommended for installing on this computer.</p>
            </div>
            <Button asChild className="w-full">
              <a href={`${DOWNLOAD_BASE}ZenPlusAgentSetup-latest.exe`} download referrerPolicy="no-referrer">
                <Download aria-hidden="true" /> Download EXE
              </a>
            </Button>
          </div>

          <div className="rounded-lg border border-border p-4">
            <div className="mb-3">
              <h3 className="text-sm font-semibold">MSI package</h3>
              <p className="mt-1 text-sm text-muted">For enterprise and silent deployment.</p>
            </div>
            <Button asChild variant="outline" className="w-full">
              <a href={`${DOWNLOAD_BASE}ZenPlusAgent-latest.msi`} download referrerPolicy="no-referrer">
                <Package aria-hidden="true" /> Download MSI
              </a>
            </Button>
          </div>
          <p className="text-xs text-muted">Downloads the latest stable installer directly from Zentryc.</p>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
