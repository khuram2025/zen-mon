import { useId } from 'react'
import { AlertTriangle, CopyPlus, MousePointerClick, RotateCcw, Trash2, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import type { SectionDef } from '../types'
import { idList, OptionField } from './fields'
import {
  effectiveOptions, isCustomized, permissionLabel, sparseOptions, TITLE_MAX, type DesignerEntry, type EntryProblems,
} from './model'
import { SectionBadges } from './ui'

/** DOM id of an option control (the validation summary moves focus to it). */
export const optionFieldId = (uid: string, key: string) => `dsn-opt-${uid}-${key}`
export const titleFieldId = (uid: string) => `dsn-title-${uid}`

export interface InspectorProps {
  entry: DesignerEntry | null
  def: SectionDef | undefined
  index: number
  total: number
  problems?: EntryProblems
  full: boolean
  onChange: (patch: Partial<Pick<DesignerEntry, 'title' | 'options'>>) => void
  onDuplicate: () => void
  onRemove: () => void
  className?: string
}

/** Settings for the selected section: title override, generated options and what the section needs. */
export function Inspector({ entry, def, index, total, problems, full, onChange, onDuplicate, onRemove, className }: InspectorProps) {
  const optsHeading = useId()
  const aboutHeading = useId()

  if (!entry) {
    return (
      <div className={cn('flex flex-1 flex-col items-center justify-center gap-2 px-6 py-12 text-center', className)}>
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-surface2 text-muted">
          <MousePointerClick className="h-5 w-5" aria-hidden />
        </span>
        <p className="text-sm font-medium text-text">No section selected</p>
        <p className="max-w-[16rem] text-xs leading-relaxed text-muted">
          Choose a section in the outline to rename it and set its options, such as how many rows to list.
        </p>
      </div>
    )
  }

  const eff = effectiveOptions(def, entry.options)
  const changed = sparseOptions(def, entry.options)
  const customized = isCustomized(def, entry.options)
  const optionIssues = !!problems && Object.keys(problems.options).length > 0
  const titleId = titleFieldId(entry.uid)
  const titleHint = `${titleId}-hint`
  const titleErr = `${titleId}-err`

  return (
    <div className={cn('flex min-h-0 flex-1 flex-col', className)}>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="space-y-5 p-4">
          {/* Identity */}
          <div>
            <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
              Section <span className="tabular-nums">{index + 1}</span> of <span className="tabular-nums">{total}</span>
              {def?.category ? <> · {def.category}</> : null}
            </p>
            <h3 className="mt-1 text-sm font-semibold leading-snug text-text">{def?.title ?? entry.id}</h3>
            {def && <SectionBadges def={def} className="mt-2" />}
          </div>

          {!def ? (
            <div role="alert" className="flex items-start gap-2.5 rounded-lg border border-danger/40 bg-danger/5 p-3 text-xs text-text2">
              <AlertTriangle className="mt-px h-4 w-4 shrink-0 text-danger-fg" aria-hidden />
              <div className="space-y-2">
                <p>
                  <span className="font-semibold text-text">This section is no longer available.</span> It was removed from the
                  appliance or renamed (<code className="font-mono text-[11px]">{entry.id}</code>). Remove it to save the template.
                </p>
                <Button type="button" size="sm" variant="outline" onClick={onRemove}>
                  <Trash2 aria-hidden /> Remove section
                </Button>
              </div>
            </div>
          ) : (
            <>
              {/* Title override */}
              <div className="space-y-1.5">
                <label htmlFor={titleId} className="text-xs font-medium text-text">Title in the report</label>
                <div className="relative">
                  <Input
                    id={titleId}
                    value={entry.title}
                    maxLength={TITLE_MAX}
                    placeholder={def.title}
                    onChange={(e) => onChange({ title: e.target.value })}
                    aria-invalid={!!problems?.title || undefined}
                    aria-describedby={idList(titleHint, problems?.title && titleErr)}
                    className={cn('h-8 pr-8 text-sm', problems?.title && 'border-danger/60')}
                  />
                  {entry.title && (
                    <button
                      type="button"
                      onClick={() => onChange({ title: '' })}
                      aria-label="Use the default title"
                      title="Use the default title"
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
                    >
                      <X className="h-3.5 w-3.5" aria-hidden />
                    </button>
                  )}
                </div>
                <p id={titleHint} className="text-[11px] text-muted">
                  Leave empty to use the default title.
                  {entry.title.length > TITLE_MAX - 20 && <span className="tabular-nums"> {entry.title.length}/{TITLE_MAX}</span>}
                </p>
                {problems?.title && <p id={titleErr} className="text-[11px] font-medium text-danger-fg">{problems.title}</p>}
              </div>

              {/* Options */}
              <fieldset aria-labelledby={optsHeading} className="space-y-3">
                <div className="flex items-center justify-between gap-2 border-b border-border pb-1.5">
                  <h4 id={optsHeading} className="text-[11px] font-semibold uppercase tracking-wider text-text2">Options</h4>
                  {def.options.length > 0 && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      disabled={!customized && !optionIssues}
                      onClick={() => onChange({ options: {} })}
                    >
                      <RotateCcw aria-hidden /> Reset to defaults
                    </Button>
                  )}
                </div>
                {def.options.length === 0 ? (
                  <p className="text-xs text-muted">This section has no options; it always shows the same content for the period and scope.</p>
                ) : (
                  def.options.map((o) => (
                    <OptionField
                      key={o.key}
                      id={optionFieldId(entry.uid, o.key)}
                      option={o}
                      value={eff[o.key]}
                      modified={o.key in changed}
                      error={problems?.options[o.key]}
                      onChange={(v) => onChange({ options: { ...entry.options, [o.key]: v } })}
                    />
                  ))
                )}
              </fieldset>

              {/* About */}
              <section aria-labelledby={aboutHeading} className="space-y-2">
                <h4 id={aboutHeading} className="border-b border-border pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-text2">
                  About this section
                </h4>
                <p className="text-xs leading-relaxed text-text2">{def.description}</p>
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[11px]">
                  <dt className="text-muted">Time</dt>
                  <dd className="text-text2">
                    {def.semantics === 'snapshot' ? 'Snapshot of the state when the report runs' : 'Covers the report period'}
                  </dd>
                  <dt className="text-muted">Scope</dt>
                  <dd className="text-text2">
                    {def.scope === 'device' ? 'Follows the device scope of the viewer or schedule'
                      : def.scope === 'netflow' ? 'Follows the NetFlow scope of the viewer or schedule'
                        : 'Fleet-wide, within each viewer’s visibility'}
                  </dd>
                  <dt className="text-muted">Data access</dt>
                  <dd className="text-text2">
                    {def.requires.length ? def.requires.map(permissionLabel).join(', ') : 'Report access only'}
                  </dd>
                </dl>
                {!def.available && (
                  <p className="rounded-md border border-warning/40 bg-warning/5 px-2.5 py-2 text-[11px] leading-relaxed text-text2">
                    Your role lacks this data permission, so your preview shows the section as restricted. Viewers who have it
                    see the data; others see it as restricted.
                  </p>
                )}
              </section>
            </>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 border-t border-border px-4 py-2.5">
        <Button type="button" variant="outline" size="sm" onClick={onDuplicate} disabled={full || !def} title={full ? 'The template is at the section limit' : undefined}>
          <CopyPlus aria-hidden /> Duplicate
        </Button>
        <Button type="button" variant="ghost" size="sm" className="ml-auto text-danger-fg hover:bg-danger/10 hover:text-danger-fg" onClick={onRemove}>
          <Trash2 aria-hidden /> Remove
        </Button>
      </div>
    </div>
  )
}
