import { useMemo } from 'react'
import { X } from 'lucide-react'

const PATTERN_RE = /^([1-5][0-9xX]{2}|[1-5][0-9]{2}-[1-5][0-9]{2})$/

export function isValidStatusPattern(p: string): boolean {
  return PATTERN_RE.test(p.trim())
}

export function parseStatusList(value: string): string[] {
  return value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

interface Props {
  value: string
  onChange: (next: string) => void
  inputClass?: string
  /** When true, render a more compact variant (used inside dialogs). */
  compact?: boolean
}

/**
 * Input for one or more HTTP expected status patterns.
 *
 * Accepts: exact codes (200), wildcards (2xx, 4xx) or ranges (200-299).
 * Stores and emits a comma-separated string. Invalid chips are highlighted
 * but not blocked — final validation happens server-side.
 */
export function ExpectedStatusInput({ value, onChange, inputClass, compact }: Props) {
  const chips = useMemo(() => parseStatusList(value), [value])
  const cls =
    inputClass ||
    'bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:border-[var(--accent)] focus:outline-none w-full text-sm'

  function removeAt(idx: number) {
    const next = chips.filter((_, i) => i !== idx).join(',')
    onChange(next)
  }

  return (
    <div className="space-y-2">
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="200, 2xx, 403, 200-299"
        className={cls}
        spellCheck={false}
        autoComplete="off"
      />
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {chips.map((c, i) => {
            const valid = isValidStatusPattern(c)
            return (
              <span
                key={`${c}-${i}`}
                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-mono ${
                  valid
                    ? 'bg-[var(--accent)]/10 text-[var(--accent)] border border-[var(--accent)]/30'
                    : 'bg-red-500/10 text-red-400 border border-red-500/30'
                }`}
                title={valid ? '' : 'Invalid pattern. Use 200, 2xx or 200-299.'}
              >
                {c}
                <button
                  type="button"
                  onClick={() => removeAt(i)}
                  className="opacity-60 hover:opacity-100"
                  aria-label={`Remove ${c}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            )
          })}
        </div>
      )}
      {!compact && (
        <p className="text-xs text-[var(--text-muted)]">
          Comma-separated. Use exact codes (<code className="font-mono">200</code>), wildcards
          (<code className="font-mono">2xx</code>) or ranges (<code className="font-mono">200-299</code>).
          Any match marks the check as up.
        </p>
      )}
    </div>
  )
}
