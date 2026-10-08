import { useId } from 'react'
import { ChevronDown, Globe2, Lock, SlidersHorizontal } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Textarea } from '@/components/ui/Textarea'
import { reportIcon } from '../icons'
import { PERIOD_LABEL } from '../periods'
import type { ReportCategory } from '../types'
import { idList, TagInput } from './fields'
import { DESCRIPTION_MAX, type Draft, type DraftField } from './model'
import { PeriodSelect, SegmentedRadio } from './ui'

/** DOM ids of the template fields (the validation summary moves focus to them). */
export const FIELD_IDS: Record<DraftField, string> = {
  name: 'dsn-name',
  description: 'dsn-description',
  tags: 'dsn-tags',
  sections: 'dsn-outline-heading',
  category: 'dsn-category',
  defaultPeriod: 'dsn-period',
}

const NO_CATEGORY = '__none__'

export interface TemplateSettingsProps {
  draft: Draft
  onChange: (patch: Partial<Draft>) => void
  categories: ReportCategory[]
  errors: Partial<Record<DraftField, string>>
  /** Show field errors (after a save attempt). */
  showErrors: boolean
  canChangeVisibility: boolean
  ownerName?: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** Description, category, tags, visibility and default period, in a collapsible panel. */
export function TemplateSettings({
  draft, onChange, categories, errors, showErrors, canChangeVisibility, ownerName, open, onOpenChange,
}: TemplateSettingsProps) {
  const bodyId = useId()
  const visLabel = useId()
  const descHint = `${FIELD_IDS.description}-hint`
  const descErr = `${FIELD_IDS.description}-err`
  const category = categories.find((c) => c.id === draft.category)
  const err = (f: DraftField) => (showErrors || f === 'description' || f === 'tags' ? errors[f] : undefined)

  const summary = [
    category?.title ?? 'No category',
    draft.defaultPeriod ? PERIOD_LABEL[draft.defaultPeriod] : 'Last 24 hours by default',
    draft.visibility === 'shared' ? 'Shared' : 'Private',
    draft.tags.length ? `${draft.tags.length} tag${draft.tags.length === 1 ? '' : 's'}` : null,
  ].filter(Boolean).join(' · ')
  const panelErrors = (['description', 'tags', 'category', 'defaultPeriod'] as DraftField[]).filter((f) => err(f)).length

  return (
    <section className="rounded-xl border border-border bg-surface shadow-card dark:shadow-card-dark">
      <h2>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => onOpenChange(!open)}
          className="flex w-full items-center gap-2.5 rounded-xl px-4 py-2.5 text-left transition-colors hover:bg-surface2/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/50"
        >
          <SlidersHorizontal className="h-4 w-4 shrink-0 text-primary" aria-hidden />
          <span className="shrink-0 text-sm font-semibold text-text">Template details</span>
          {!open && <span className="min-w-0 truncate text-xs text-muted">{summary}</span>}
          {!open && panelErrors > 0 && (
            <span className="shrink-0 rounded-full bg-danger/10 px-1.5 py-px text-[10px] font-semibold text-danger-fg">
              {panelErrors} to fix
            </span>
          )}
          <ChevronDown className={cn('ml-auto h-4 w-4 shrink-0 text-muted transition-transform', open && 'rotate-180')} aria-hidden />
        </button>
      </h2>

      {open && (
        <div
          id={bodyId}
          className="grid grid-cols-1 gap-x-4 gap-y-3 border-t border-border px-4 pb-4 pt-3 md:grid-cols-2 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_minmax(0,1fr)]"
        >
          <div className="space-y-1.5 md:col-span-2 xl:col-span-1 xl:row-span-2 xl:flex xl:flex-col">
            <label htmlFor={FIELD_IDS.description} className="text-xs font-medium text-text">Description</label>
            <Textarea
              id={FIELD_IDS.description}
              rows={2}
              value={draft.description}
              maxLength={DESCRIPTION_MAX}
              onChange={(e) => onChange({ description: e.target.value })}
              placeholder="What the report answers and who it is for, e.g. “Weekly uptime and incidents for the operations review.”"
              aria-describedby={idList(descHint, err('description') && descErr)}
              aria-invalid={!!err('description') || undefined}
              className="min-h-[3.75rem] resize-y text-sm xl:flex-1"
            />
            <p id={descHint} className="text-[11px] text-muted">
              Shown on the report cover and the library card.
              {draft.description.length > DESCRIPTION_MAX - 200 && (
                <span className="tabular-nums"> {draft.description.length}/{DESCRIPTION_MAX}</span>
              )}
            </p>
            {err('description') && <p id={descErr} className="text-[11px] font-medium text-danger-fg">{err('description')}</p>}
          </div>

          <div className="space-y-1.5">
            <label id={`${FIELD_IDS.category}-label`} htmlFor={FIELD_IDS.category} className="text-xs font-medium text-text">Category</label>
            <Select value={draft.category ?? NO_CATEGORY} onValueChange={(v) => onChange({ category: v === NO_CATEGORY ? null : v })}>
              <SelectTrigger
                id={FIELD_IDS.category}
                aria-labelledby={`${FIELD_IDS.category}-label`}
                aria-invalid={!!err('category') || undefined}
                className={cn(err('category') && 'border-danger/60')}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="max-h-80">
                <SelectItem value={NO_CATEGORY}>No category</SelectItem>
                {categories.map((c) => {
                  const Icon = reportIcon(c.icon)
                  return (
                    <SelectItem key={c.id} value={c.id}>
                      <span className="inline-flex items-center gap-2">
                        <Icon className="h-3.5 w-3.5 text-muted" aria-hidden />
                        {c.title}
                      </span>
                    </SelectItem>
                  )
                })}
              </SelectContent>
            </Select>
            <p className="text-[11px] text-muted">Where the template is listed in the library.</p>
            {err('category') && <p className="text-[11px] font-medium text-danger-fg">{err('category')}</p>}
          </div>

          <div className="space-y-1.5">
            <label id={`${FIELD_IDS.defaultPeriod}-label`} htmlFor={FIELD_IDS.defaultPeriod} className="text-xs font-medium text-text">
              Default period
            </label>
            <PeriodSelect
              id={FIELD_IDS.defaultPeriod}
              labelledBy={`${FIELD_IDS.defaultPeriod}-label`}
              value={draft.defaultPeriod}
              onChange={(p) => onChange({ defaultPeriod: p })}
              allowNone
            />
            <p className="text-[11px] text-muted">Used when the report opens; viewers can change it.</p>
            {err('defaultPeriod') && <p className="text-[11px] font-medium text-danger-fg">{err('defaultPeriod')}</p>}
          </div>

          <div className="space-y-1.5">
            <label htmlFor={FIELD_IDS.tags} className="text-xs font-medium text-text">Tags</label>
            <TagInput id={FIELD_IDS.tags} value={draft.tags} onChange={(tags) => onChange({ tags })} error={err('tags')} />
          </div>

          <div className="space-y-1.5">
            <span id={visLabel} className="block text-xs font-medium text-text">Who can see it</span>
            <SegmentedRadio
              labelledBy={visLabel}
              value={draft.visibility}
              onChange={(v) => onChange({ visibility: v })}
              disabled={!canChangeVisibility}
              options={[
                { value: 'private', label: 'Private', icon: Lock },
                { value: 'shared', label: 'Shared', icon: Globe2 },
              ]}
            />
            <p className="text-[11px] leading-snug text-muted">
              {!canChangeVisibility
                ? `Only ${ownerName ? `the owner (${ownerName})` : 'the owner'} or an administrator can change who sees this template.`
                : draft.visibility === 'private'
                  ? 'Only you and administrators can see, run and schedule it.'
                  : 'Everyone with report access can run it; report managers can edit it.'}
            </p>
          </div>
        </div>
      )}
    </section>
  )
}
