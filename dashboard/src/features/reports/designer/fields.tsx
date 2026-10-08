/**
 * Form controls for the template designer: the tag chip input and the
 * generated section-option fields (number steppers, selects, switches).
 */
import { useId, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react'
import { Minus, Plus, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import type { SectionOption } from '../types'
import { addTags, stepValue, TAG_LEN_MAX, TAGS_MAX } from './model'

/** Space-separated ids for aria-describedby (falsy entries dropped). */
export function idList(...ids: (string | false | null | undefined)[]): string | undefined {
  const out = ids.filter(Boolean).join(' ')
  return out || undefined
}

/* ------------------------------------------------------------------ */
/*  Tags                                                                */
/* ------------------------------------------------------------------ */

export function TagInput({
  id, value, onChange, error: externalError, disabled,
}: {
  id: string
  value: string[]
  onChange: (tags: string[]) => void
  error?: string | null
  disabled?: boolean
}) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const hintId = `${id}-hint`
  const errId = `${id}-err`
  const shown = error || externalError || null

  const commit = (input: string) => {
    if (!input.trim()) {
      setText('')
      return
    }
    const r = addTags(value, input)
    if (r.tags.length !== value.length) onChange(r.tags)
    setText(r.rest)
    setError(r.error)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault()
      commit(text)
    } else if (e.key === 'Backspace' && !text && value.length) {
      e.preventDefault()
      onChange(value.slice(0, -1))
      setError(null)
    }
  }

  const onPaste = (e: ClipboardEvent<HTMLInputElement>) => {
    const pasted = e.clipboardData.getData('text')
    if (pasted.includes(',')) {
      e.preventDefault()
      commit(`${text}${pasted}`)
    }
  }

  return (
    <div>
      <div
        className={cn(
          'flex min-h-9 w-full flex-wrap items-center gap-1 rounded-md border bg-surface px-1.5 py-1 shadow-sm transition-colors',
          'focus-within:border-primary focus-within:ring-2 focus-within:ring-primary/40',
          shown ? 'border-danger/60' : 'border-border',
          disabled && 'opacity-50',
        )}
        onClick={() => inputRef.current?.focus()}
      >
        {value.length > 0 && (
          <ul aria-label="Tags" className="contents">
            {value.map((t) => (
              <li key={t} className="inline-flex max-w-full items-center gap-0.5 rounded bg-surface2 py-0.5 pl-2 pr-0.5 text-xs text-text">
                <span className="truncate">{t}</span>
                <button
                  type="button"
                  disabled={disabled}
                  aria-label={`Remove tag ${t}`}
                  onClick={(e) => {
                    e.stopPropagation()
                    onChange(value.filter((x) => x !== t))
                    setError(null)
                    inputRef.current?.focus()
                  }}
                  className="rounded p-0.5 text-muted hover:bg-surface3 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
        <input
          ref={inputRef}
          id={id}
          value={text}
          disabled={disabled || (value.length >= TAGS_MAX && !text)}
          maxLength={TAG_LEN_MAX * 4}
          onChange={(e) => { setText(e.target.value); if (error) setError(null) }}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onBlur={() => commit(text)}
          aria-invalid={!!shown || undefined}
          aria-describedby={idList(hintId, shown && errId)}
          placeholder={value.length >= TAGS_MAX ? 'Tag limit reached' : value.length ? 'Add another…' : 'e.g. weekly, sla, management'}
          className="h-6 min-w-[8rem] flex-1 bg-transparent px-1 text-sm text-text placeholder:text-muted focus:outline-none disabled:cursor-not-allowed"
        />
      </div>
      <p id={hintId} className="mt-1 text-[11px] text-muted">
        <span className="tabular-nums">{value.length}/{TAGS_MAX}</span> · Press Enter or comma to add; up to {TAG_LEN_MAX} characters each.
      </p>
      {shown && <p id={errId} className="mt-0.5 text-[11px] text-danger-fg">{shown}</p>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Section options                                                     */
/* ------------------------------------------------------------------ */

function rangeHint(o: SectionOption): string {
  if (o.min != null && o.max != null) return `${o.min}–${o.max}`
  if (o.min != null) return `≥ ${o.min}`
  if (o.max != null) return `≤ ${o.max}`
  return ''
}

function defaultText(o: SectionOption): string {
  if (o.type === 'bool') return o.default ? 'on' : 'off'
  if (o.type === 'select') return (o.choices ?? []).find(([v]) => v === o.default)?.[1] ?? String(o.default ?? '')
  return String(o.default ?? '')
}

export function OptionField({
  option: o, value, onChange, error, id, modified,
}: {
  option: SectionOption
  value: unknown
  onChange: (v: unknown) => void
  error?: string
  /** DOM id of the control (the validation summary focuses it). */
  id: string
  modified: boolean
}) {
  const labelId = useId()
  const hintId = `${id}-hint`
  const errId = `${id}-err`
  const describedBy = idList(hintId, error && errId)

  const label = (
    <span className="flex items-center gap-1.5">
      <span id={labelId} className="text-xs font-medium text-text">{o.label}</span>
      {modified && (
        <span className="inline-flex items-center gap-1 text-[10px] font-medium text-primary">
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-primary" />
          <span className="sr-only">(</span>changed<span className="sr-only">)</span>
        </span>
      )}
    </span>
  )

  const hint = (
    <p id={hintId} className="text-[11px] leading-snug text-muted">
      {o.help ? `${o.help} ` : ''}
      {(o.type === 'int' || o.type === 'number') && rangeHint(o) && <>Range {rangeHint(o)}. </>}
      Default {defaultText(o)}.
    </p>
  )
  const err = error ? <p id={errId} className="text-[11px] font-medium text-danger-fg">{error}</p> : null

  if (o.type === 'bool') {
    return (
      <div className="space-y-1">
        <div className="flex items-start justify-between gap-3">
          <label htmlFor={id} className="min-w-0 cursor-pointer">{label}</label>
          <Switch
            id={id}
            checked={value === true}
            onCheckedChange={(v) => onChange(v)}
            aria-describedby={describedBy}
            aria-invalid={!!error || undefined}
          />
        </div>
        {hint}
        {err}
      </div>
    )
  }

  if (o.type === 'select') {
    const choices = o.choices ?? []
    const known = choices.some(([v]) => v === value)
    return (
      <div className="space-y-1.5">
        {label}
        <Select value={known ? String(value) : undefined} onValueChange={(v) => onChange(v)}>
          <SelectTrigger
            id={id}
            aria-labelledby={labelId}
            aria-describedby={describedBy}
            aria-invalid={!!error || undefined}
            className={cn('h-8 text-xs', error && 'border-danger/60')}
          >
            <SelectValue placeholder="Choose…" />
          </SelectTrigger>
          <SelectContent>
            {choices.map(([v, l]) => <SelectItem key={v} value={v} className="text-xs">{l}</SelectItem>)}
          </SelectContent>
        </Select>
        {hint}
        {err}
      </div>
    )
  }

  // int | number
  return (
    <div className="space-y-1.5">
      <label htmlFor={id}>{label}</label>
      <NumberStepper option={o} id={id} value={value} onChange={onChange} invalid={!!error} describedBy={describedBy} />
      {hint}
      {err}
    </div>
  )
}

const parseNumber = (raw: string): unknown => (raw.trim() !== '' && Number.isFinite(Number(raw)) ? Number(raw) : raw)

/**
 * A spin button: − / + buttons, arrow keys (Page Up/Down for ten steps,
 * Home/End for the range ends) and free typing. The typed text is kept while
 * the field has focus so partial input such as "1." is not rewritten.
 */
function NumberStepper({
  option: o, id, value, onChange, invalid, describedBy,
}: {
  option: SectionOption
  id: string
  value: unknown
  onChange: (v: unknown) => void
  invalid: boolean
  describedBy?: string
}) {
  const [typing, setTyping] = useState<string | null>(null)
  const num = typeof value === 'number' && Number.isFinite(value) ? value : null
  const text = typing ?? (value == null ? '' : String(value))
  const atMin = num != null && o.min != null && num <= o.min
  const atMax = num != null && o.max != null && num >= o.max
  const step = (dir: 1 | -1, times = 1) => {
    let v: unknown = num
    for (let i = 0; i < times; i++) v = stepValue(o, v, dir)
    setTyping(null)
    onChange(v)
  }
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowUp') step(1)
    else if (e.key === 'ArrowDown') step(-1)
    else if (e.key === 'PageUp') step(1, 10)
    else if (e.key === 'PageDown') step(-1, 10)
    else if (e.key === 'Home' && o.min != null) { setTyping(null); onChange(o.min) }
    else if (e.key === 'End' && o.max != null) { setTyping(null); onChange(o.max) }
    else return
    e.preventDefault()
  }
  const stepBtn = 'flex h-8 w-8 shrink-0 items-center justify-center text-text2 transition-colors hover:bg-surface2 hover:text-text disabled:cursor-not-allowed disabled:opacity-40 focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/50'
  return (
    <div className={cn('flex w-full max-w-[12rem] items-stretch overflow-hidden rounded-md border bg-surface shadow-sm transition-colors focus-within:ring-2 focus-within:ring-primary/40', invalid ? 'border-danger/60' : 'border-border focus-within:border-primary')}>
      <button type="button" tabIndex={-1} className={cn(stepBtn, 'border-r border-border')} disabled={atMin} onClick={() => step(-1)} aria-label={`Decrease ${o.label}`}>
        <Minus className="h-3.5 w-3.5" aria-hidden />
      </button>
      <input
        id={id}
        type="text"
        role="spinbutton"
        inputMode={o.type === 'int' ? 'numeric' : 'decimal'}
        autoComplete="off"
        aria-valuenow={num ?? undefined}
        aria-valuemin={o.min}
        aria-valuemax={o.max}
        value={text}
        onChange={(e) => { setTyping(e.target.value); onChange(parseNumber(e.target.value)) }}
        onBlur={() => setTyping(null)}
        onKeyDown={onKeyDown}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        className="h-8 w-full min-w-0 flex-1 bg-transparent px-2 text-center text-sm tabular-nums text-text focus:outline-none"
      />
      <button type="button" tabIndex={-1} className={cn(stepBtn, 'border-l border-border')} disabled={atMax} onClick={() => step(1)} aria-label={`Increase ${o.label}`}>
        <Plus className="h-3.5 w-3.5" aria-hidden />
      </button>
    </div>
  )
}
