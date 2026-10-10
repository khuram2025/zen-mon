import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/Select'
import { cn } from '@/lib/utils'

export interface FilterSelectOption {
  value: string
  label: string
  /** Muted text on the right of the item (a count). */
  hint?: string
}

const ALL = '__all__'

/**
 * A filter as a select: the filter's name in muted text, its current value in bold, the choices in a list with
 * "All ..." first. The Radix Select cannot hold an empty value, so "all" travels as a sentinel and leaves as
 * `''`. A value that is not among the options (several categories in the address, a publisher the facet lacks)
 * is still shown in the trigger through `valueText`.
 */
export function FilterSelect({
  label, allLabel, value, valueText, options, onChange, disabled, className, contentClassName,
}: {
  label: string
  /** "All categories": the first item, which clears the filter. */
  allLabel: string
  /** The chosen value; `''` for none. */
  value: string
  /** What the trigger shows for the chosen value; defaults to its option's label. */
  valueText?: string
  options: readonly FilterSelectOption[]
  onChange: (value: string) => void
  disabled?: boolean
  className?: string
  contentClassName?: string
}) {
  const chosen = options.find((o) => o.value === value)
  const text = valueText ?? chosen?.label ?? (value || 'All')
  return (
    <Select value={value || ALL} onValueChange={(v) => onChange(v === ALL ? '' : v)} disabled={disabled}>
      <SelectTrigger
        aria-label={`${label}: ${value ? text : allLabel}`}
        className={cn('h-9 gap-2 px-3 text-sm', value && 'border-primary/40 bg-primary/5', className)}
      >
        <span className="flex min-w-0 items-center gap-2">
          {/* text-text2: muted on the selected state's tinted fill is 4.2:1 in the light theme. */}
          <span className="shrink-0 text-text2">{label}</span>
          <span className="truncate font-medium">{value ? text : 'All'}</span>
        </span>
      </SelectTrigger>
      <SelectContent className={cn('min-w-[var(--radix-select-trigger-width)]', contentClassName)}>
        <SelectItem value={ALL}>
          <span className="text-muted">{allLabel}</span>
        </SelectItem>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            <span className="flex w-full items-baseline justify-between gap-4">
              <span className="truncate">{o.label}</span>
              {o.hint && <span className="shrink-0 text-[11px] tabular-nums text-muted">{o.hint}</span>}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
