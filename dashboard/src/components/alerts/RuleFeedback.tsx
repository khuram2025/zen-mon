/**
 * Inline feedback for the rule editors: a persistent banner listing each field
 * error of a failed save (stays until the user edits, the dialog stays open),
 * and amber, non-blocking name/condition warnings.
 */
import { AlertCircle, AlertTriangle } from 'lucide-react'
import type { ApiFieldError } from '@/lib/utils'
import { fieldLabel, type RuleWarning } from '@/components/forms/alertRuleChecks'

export function FieldErrorBanner({ errors, title = 'The rule was not saved', onJump }: {
  errors: ApiFieldError[]
  title?: string
  /** Called with a field path when its line is clicked (e.g. to open the wizard step that owns it). */
  onJump?: (path: string) => void
}) {
  if (!errors.length) return null
  return (
    <div role="alert" aria-label={title} className="rounded-md border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-text2">
      <div className="mb-1 flex items-center gap-1.5 text-[13px] font-semibold text-danger">
        <AlertCircle className="h-4 w-4 shrink-0" aria-hidden /> {title}
      </div>
      <ul className="space-y-0.5">
        {errors.map((e, i) => (
          <li key={`${e.path}-${i}`} className="flex flex-wrap items-baseline gap-1">
            {e.path ? (
              onJump ? (
                <button type="button" onClick={() => onJump(e.path)} className="font-mono font-semibold text-danger hover:underline" title={`Go to ${fieldLabel(e.path) || e.path}`}>
                  {e.path}
                </button>
              ) : <span className="font-mono font-semibold text-danger">{e.path}</span>
            ) : null}
            {e.path ? <span aria-hidden>→</span> : null}
            <span className="min-w-0 break-words">{e.msg}</span>
          </li>
        ))}
      </ul>
      <p className="mt-1 text-[11px] text-muted">Fix the fields above and save again; this message clears when you edit.</p>
    </div>
  )
}

export function RuleWarnings({ warnings }: { warnings: RuleWarning[] }) {
  if (!warnings.length) return null
  return (
    <div role="status" aria-label="Rule warnings" className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
      <div className="mb-0.5 flex items-center gap-1.5 font-semibold">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden /> Check the name against the condition
      </div>
      <ul className="list-disc space-y-0.5 pl-5">
        {warnings.map((w) => <li key={w.message}>{w.message}</li>)}
      </ul>
      <p className="mt-1 text-[11px] opacity-80">Saving is still allowed.</p>
    </div>
  )
}
