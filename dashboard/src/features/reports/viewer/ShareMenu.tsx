import { CalendarRange, History, Link2 } from 'lucide-react'
import { copyText } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { toast } from '@/components/ui/Toast'

export interface ShareMenuProps {
  /** Link that opens exactly this period (absolute dates). */
  exactUrl: string
  /** Text describing the exact period, e.g. "01-Sep-2026 00:00 — 01-Oct-2026 00:00 GMT+3". */
  exactLabel: string
  /** Link that always opens the latest window of a relative period; omitted for custom ranges. */
  latestUrl?: string | null
  latestLabel?: string
}

async function copy(url: string, what: string) {
  if (await copyText(url)) toast.success('Link copied', what)
  else toast.error('Could not copy the link', 'Copy it from the address bar instead.')
}

/** Copy a shareable link: this exact period (absolute dates), or always the latest relative period. */
export function ShareMenu({ exactUrl, exactLabel, latestUrl, latestLabel }: ShareMenuProps) {
  if (!latestUrl) {
    return (
      <Button
        variant="outline"
        size="sm"
        className="h-8 gap-1.5"
        onClick={() => void copy(exactUrl, `Opens ${exactLabel}`)}
        aria-label="Copy link to this exact period"
        title="Copy link to this exact period"
      >
        <Link2 className="h-3.5 w-3.5" aria-hidden />
        <span className="hidden text-xs font-semibold 2xl:inline">Copy link</span>
      </Button>
    )
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 gap-1.5" aria-label="Copy link" title="Copy link">
          <Link2 className="h-3.5 w-3.5" aria-hidden />
          <span className="hidden text-xs font-semibold 2xl:inline">Copy link</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-72">
        <DropdownMenuLabel>Copy a link to this report</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => void copy(exactUrl, `Opens ${exactLabel}`)}>
          <CalendarRange className="text-primary" aria-hidden />
          <span className="min-w-0">
            <span className="block font-medium">This exact period</span>
            <span className="block truncate text-[11px] text-muted">{exactLabel}</span>
          </span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => void copy(latestUrl, `Always opens ${latestLabel?.toLowerCase() ?? 'the latest period'}`)}>
          <History className="text-primary" aria-hidden />
          <span className="min-w-0">
            <span className="block font-medium">Always the latest</span>
            <span className="block truncate text-[11px] text-muted">{latestLabel} when opened</span>
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
