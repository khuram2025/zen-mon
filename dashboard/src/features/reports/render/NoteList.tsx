import { Info } from 'lucide-react'
import { cn } from '@/lib/utils'

/** Section notes: context for the reader (data coverage, caveats), shown as info callouts. */
export function NoteList({ notes, className }: { notes: string[]; className?: string }) {
  const items = notes.map((n) => n.trim()).filter(Boolean)
  if (!items.length) return null
  return (
    <ul role="list" className={cn('space-y-1.5', className)}>
      {items.map((note, i) => (
        <li
          key={i}
          className="zp-avoid-break flex items-start gap-2 rounded-md border border-info/25 bg-info/5 px-3 py-2 text-xs leading-relaxed text-text2"
        >
          <Info className="mt-px h-3.5 w-3.5 shrink-0 text-info-fg" aria-hidden />
          <span className="sr-only">Note: </span>
          <span className="min-w-0">{note}</span>
        </li>
      ))}
    </ul>
  )
}
