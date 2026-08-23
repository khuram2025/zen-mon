import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'

/** Backward-compatible component name for callers that previously opened the
 * token-based download dialog. Deployment is now controller-only. */
export function DownloadAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return <InstallTokenDialog open={open} onOpenChange={onOpenChange} />
}
