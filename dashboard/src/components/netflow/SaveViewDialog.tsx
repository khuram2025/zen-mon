import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Switch } from '@/components/ui/Switch'
import { cn } from '@/lib/utils'
import {
  isFlowFilterEmpty,
  SAVED_VIEW_DESCRIPTION_MAX,
  SAVED_VIEW_NAME_MAX,
  validateSaveViewFields,
  type FlowFilter,
} from '@/lib/netflow/filters'
import { canSaveRelative, freezeWindow, windowLabel, type NfMetaWindow, type NfWindow } from '@/lib/netflow/window'
import { FilterChips } from './FilterChips'

export type SavedViewVisibility = 'private' | 'shared'
export type SavedViewTimeMode = 'relative' | 'fixed'

export interface SaveViewDraft {
  /** Trimmed. */
  name: string
  description: string
  visibility: SavedViewVisibility
  timeMode: SavedViewTimeMode
  pinned: boolean
}

export interface SaveViewDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  mode?: 'create' | 'edit'
  initial?: Partial<SaveViewDraft>
  /** The applied filter being saved (shown read-only). */
  filter: FlowFilter
  /** The page window: relative windows can be kept relative or pinned to their current bounds. */
  window: NfWindow
  /** The window of the data on screen (`meta.window`), so "fixed" pins exactly those bounds. */
  metaWindow?: Pick<NfMetaWindow, 'from' | 'to'> | null
  /** Further saved settings to list, e.g. "Sorted by bytes, descending". */
  extraSummary?: ReactNode
  /** The user may share views with the team; the API enforces it either way. */
  canShare?: boolean
  pending?: boolean
  /** The save failed: the dialog stays open with this message. */
  error?: string | null
  onSubmit: (draft: SaveViewDraft) => void
}

function initialDraft(initial: Partial<SaveViewDraft> | undefined, relativeAllowed: boolean): SaveViewDraft {
  return {
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    visibility: initial?.visibility ?? 'private',
    timeMode: relativeAllowed ? (initial?.timeMode ?? 'relative') : 'fixed',
    pinned: initial?.pinned ?? false,
  }
}

/**
 * Save or edit a NetFlow view (NF-34, FE-10). The shell owns the form: whitespace-only names are
 * rejected inline, sharing and the time mode are explicit choices, and a failed save keeps the
 * dialog open with the reason. The caller performs the request and passes `pending`/`error`.
 */
export function SaveViewDialog({
  open,
  onOpenChange,
  mode = 'create',
  initial,
  filter,
  window: w,
  metaWindow,
  extraSummary,
  canShare = true,
  pending = false,
  error,
  onSubmit,
}: SaveViewDialogProps) {
  const relativeAllowed = canSaveRelative(w)
  const [draft, setDraft] = useState<SaveViewDraft>(() => initialDraft(initial, relativeAllowed))
  const [touched, setTouched] = useState(false)
  const nameRef = useRef<HTMLInputElement>(null)
  const uid = useId()

  // Each opening starts from the caller's initial values, not a previous attempt.
  useEffect(() => {
    if (open) {
      setDraft(initialDraft(initial, relativeAllowed))
      setTouched(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const fixedLabel = useMemo(() => windowLabel(freezeWindow(w, metaWindow)), [w, metaWindow, open])
  const errors = validateSaveViewFields(draft)
  const showErrors = touched
  const set = <K extends keyof SaveViewDraft>(key: K, value: SaveViewDraft[K]) => setDraft((d) => ({ ...d, [key]: value }))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (errors.name || errors.description) {
      if (errors.name) nameRef.current?.focus()
      return
    }
    onSubmit({ ...draft, name: draft.name.trim(), description: draft.description.trim() })
  }

  const ids = {
    name: `${uid}-name`,
    nameErr: `${uid}-name-err`,
    nameHint: `${uid}-name-hint`,
    desc: `${uid}-desc`,
    descErr: `${uid}-desc-err`,
    pinned: `${uid}-pinned`,
    error: `${uid}-error`,
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!pending) onOpenChange(next) }}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{mode === 'edit' ? 'Edit saved view' : 'Save view'}</DialogTitle>
          <DialogDescription>
            A saved view reopens exactly the query below: these filters, this time setting and the settings listed with them.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} noValidate className="space-y-4">
          <div className="space-y-1.5">
            <div className="flex items-baseline justify-between">
              <label htmlFor={ids.name} className="text-xs font-medium text-text">
                Name <span className="text-danger-fg" aria-hidden>*</span>
              </label>
              <span id={ids.nameHint} className="text-[11px] text-text2">
                {draft.name.trim().length}/{SAVED_VIEW_NAME_MAX}
              </span>
            </div>
            <Input
              id={ids.name}
              ref={nameRef}
              value={draft.name}
              autoFocus
              required
              aria-invalid={showErrors && !!errors.name}
              aria-describedby={showErrors && errors.name ? `${ids.nameErr} ${ids.nameHint}` : ids.nameHint}
              onChange={(e) => set('name', e.target.value)}
              onBlur={() => draft.name && setTouched(true)}
              className={cn(showErrors && errors.name && 'border-danger focus-visible:border-danger')}
              placeholder="e.g. Branch VPN traffic"
            />
            {showErrors && errors.name && (
              <p id={ids.nameErr} className="text-[11px] font-medium text-danger-fg">{errors.name}</p>
            )}
          </div>

          <div className="space-y-1.5">
            <div className="flex items-baseline justify-between">
              <label htmlFor={ids.desc} className="text-xs font-medium text-text">Description</label>
              <span className="text-[11px] text-text2">{draft.description.trim().length}/{SAVED_VIEW_DESCRIPTION_MAX}</span>
            </div>
            <Textarea
              id={ids.desc}
              value={draft.description}
              rows={2}
              aria-invalid={showErrors && !!errors.description}
              aria-describedby={showErrors && errors.description ? ids.descErr : undefined}
              onChange={(e) => set('description', e.target.value)}
              placeholder="What this view is for (optional)"
            />
            {showErrors && errors.description && (
              <p id={ids.descErr} className="text-[11px] font-medium text-danger-fg">{errors.description}</p>
            )}
          </div>

          <ChoiceGroup
            legend="Who can see it"
            name={`${uid}-visibility`}
            value={draft.visibility}
            onChange={(v) => set('visibility', v)}
            options={[
              { value: 'private', label: 'Only me', hint: 'Private: only you can open, change or delete it.' },
              {
                value: 'shared',
                label: 'Shared with the team',
                hint: canShare
                  ? 'Everyone with NetFlow access can open it and sees you as its owner; only you and NetFlow managers can change or delete it. Each viewer still sees only the exporters they are allowed to see.'
                  : 'Your role cannot share views with the team.',
                disabled: !canShare,
              },
            ]}
          />

          <ChoiceGroup
            legend="Time"
            name={`${uid}-time`}
            value={draft.timeMode}
            onChange={(v) => set('timeMode', v)}
            options={[
              {
                value: 'relative',
                label: relativeAllowed ? `Keep "${w.label}"` : 'Relative period',
                hint: relativeAllowed
                  ? 'Re-evaluated each time the view opens.'
                  : 'A custom range is always saved as the exact period.',
                disabled: !relativeAllowed,
              },
              { value: 'fixed', label: 'This exact period', hint: fixedLabel },
            ]}
          />

          <div className="flex items-center justify-between gap-3">
            <label htmlFor={ids.pinned} className="text-xs text-text">
              Pin to the top of Saved Views
            </label>
            <Switch id={ids.pinned} checked={draft.pinned} onCheckedChange={(v) => set('pinned', v)} />
          </div>

          <div className="space-y-1.5 rounded-md border border-border bg-surface2/50 p-2.5">
            <div className="text-[11px] font-medium text-text2">Saved query</div>
            {isFlowFilterEmpty(filter) ? (
              <p className="text-[11px] text-text2">No filters: all traffic you can see in the period.</p>
            ) : (
              <FilterChips filter={filter} tz={w.tz} />
            )}
            {extraSummary && <div className="text-[11px] text-text2">{extraSummary}</div>}
          </div>

          {error && (
            <div id={ids.error} role="alert" className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-text">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger-fg" aria-hidden />
              <span>
                <span className="font-semibold text-danger-fg">The view was not saved.</span> {error}
              </span>
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" size="sm" disabled={pending} onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={pending} aria-describedby={error ? ids.error : undefined}>
              {pending && <Loader2 className="animate-spin" aria-hidden />}
              {pending ? 'Saving…' : mode === 'edit' ? 'Save changes' : 'Save view'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

interface ChoiceOption<V extends string> {
  value: V
  label: string
  hint?: ReactNode
  disabled?: boolean
}

function ChoiceGroup<V extends string>({
  legend,
  name,
  value,
  onChange,
  options,
}: {
  legend: string
  name: string
  value: V
  onChange: (v: V) => void
  options: ChoiceOption<V>[]
}) {
  return (
    <fieldset className="space-y-1.5">
      <legend className="mb-1.5 text-xs font-medium text-text">{legend}</legend>
      {options.map((o) => {
        const id = `${name}-${o.value}`
        return (
          <label
            key={o.value}
            htmlFor={id}
            className={cn(
              'flex cursor-pointer items-start gap-2 rounded-md border px-2.5 py-2',
              value === o.value ? 'border-primary/60 bg-primary/5' : 'border-border',
              o.disabled && 'cursor-not-allowed opacity-60',
            )}
          >
            <input
              id={id}
              type="radio"
              name={name}
              value={o.value}
              checked={value === o.value}
              disabled={o.disabled}
              onChange={() => onChange(o.value)}
              className="mt-0.5 accent-[rgb(var(--primary))]"
            />
            <span className="min-w-0">
              <span className="block text-xs font-medium text-text">{o.label}</span>
              {o.hint && <span className="block text-[11px] leading-snug text-text2">{o.hint}</span>}
            </span>
          </label>
        )
      })}
    </fieldset>
  )
}
