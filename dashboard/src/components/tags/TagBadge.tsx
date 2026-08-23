import { X } from 'lucide-react'
import { autoTagColor } from '@/hooks/useTags'

/** Colored tag chip. Pass either onClick (filter chip) or onRemove
 * (picker selection) — not both, since the chip itself becomes a button
 * when clickable. */
export function TagBadge({
  name, color, active, onClick, onRemove, className = '', title,
}: {
  name: string
  color?: string | null
  active?: boolean
  onClick?: () => void
  onRemove?: () => void
  className?: string
  title?: string
}) {
  const c = color || autoTagColor(name)
  const style = {
    color: c,
    borderColor: active ? c : `${c}66`,
    backgroundColor: active ? `${c}2E` : `${c}1A`,
  }
  const base = `inline-flex max-w-[150px] items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium leading-4 ${className}`

  if (onClick) {
    return (
      <button type="button" onClick={onClick} title={title || name} style={style}
        className={`${base} cursor-pointer transition-all hover:brightness-125`}>
        <span className="truncate">{name}</span>
      </button>
    )
  }
  return (
    <span title={title || name} style={style} className={base}>
      <span className="truncate">{name}</span>
      {onRemove && (
        <button type="button" onClick={onRemove} title={`Remove ${name}`}
          className="-mr-1 rounded-full p-0.5 opacity-70 hover:opacity-100">
          <X className="h-2.5 w-2.5" />
        </button>
      )}
    </span>
  )
}
