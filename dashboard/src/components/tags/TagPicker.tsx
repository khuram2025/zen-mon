import { useEffect, useMemo, useRef, useState } from 'react'
import { Plus, Tag as TagIcon } from 'lucide-react'
import { tagColor, tagColorMap, useTags } from '@/hooks/useTags'
import { TagBadge } from './TagBadge'

/** Multi-select tag input: shows the current selection as removable chips,
 * suggests registry tags as you type, and (by default) lets you create a
 * brand-new tag by typing its name. New names are registered server-side
 * when the device saves, with a color derived from the same hash the UI
 * previews. */
export function TagPicker({
  value, onChange, placeholder = 'Add tags…', allowCreate = true, autoFocus,
}: {
  value: string[]
  onChange: (next: string[]) => void
  placeholder?: string
  allowCreate?: boolean
  autoFocus?: boolean
}) {
  const { data: defs } = useTags()
  const colors = useMemo(() => tagColorMap(defs), [defs])
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  const selectedLower = useMemo(() => new Set(value.map((t) => t.toLowerCase())), [value])
  const query = q.trim()
  const options = (defs || []).filter(
    (d) =>
      !selectedLower.has(d.name.toLowerCase()) &&
      d.name.toLowerCase().includes(query.toLowerCase()),
  )
  const exactExists =
    selectedLower.has(query.toLowerCase()) ||
    (defs || []).some((d) => d.name.toLowerCase() === query.toLowerCase())
  const canCreate = allowCreate && query.length > 0 && !exactExists

  function add(name: string) {
    onChange([...value, name])
    setQ('')
    inputRef.current?.focus()
  }
  function remove(name: string) {
    onChange(value.filter((t) => t !== name))
  }
  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (options.length > 0 && (!canCreate || !query)) add(options[0].name)
      else if (canCreate) add(query)
    } else if (e.key === 'Backspace' && !q && value.length > 0) {
      remove(value[value.length - 1])
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div ref={ref} className="relative">
      <div
        className="flex min-h-[38px] flex-wrap items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1.5 focus-within:border-primary/50"
        onClick={() => { inputRef.current?.focus(); setOpen(true) }}
      >
        <TagIcon className="h-3.5 w-3.5 shrink-0 text-muted" />
        {value.map((t) => (
          <TagBadge key={t} name={t} color={tagColor(t, colors)} onRemove={() => remove(t)} />
        ))}
        <input
          ref={inputRef}
          value={q}
          autoFocus={autoFocus}
          onChange={(e) => { setQ(e.target.value); setOpen(true) }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          placeholder={value.length === 0 ? placeholder : ''}
          className="min-w-[90px] flex-1 bg-transparent text-sm outline-none placeholder:text-muted"
        />
      </div>

      {open && (options.length > 0 || canCreate) && (
        <div className="absolute left-0 right-0 z-30 mt-1 max-h-48 overflow-y-auto rounded-md border border-border bg-surface shadow-lg">
          {options.map((d) => (
            <button
              key={d.id}
              type="button"
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-surface2"
              onClick={() => add(d.name)}
            >
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: tagColor(d.name, colors) }} />
              <span className="flex-1 truncate">{d.name}</span>
              <span className="text-[10px] text-muted">{d.device_count}</span>
            </button>
          ))}
          {canCreate && (
            <button
              type="button"
              className="flex w-full items-center gap-2 border-t border-border px-3 py-1.5 text-left text-xs text-primary hover:bg-surface2"
              onClick={() => add(query)}
            >
              <Plus className="h-3 w-3" />
              Create “{query}”
            </button>
          )}
        </div>
      )}
    </div>
  )
}
