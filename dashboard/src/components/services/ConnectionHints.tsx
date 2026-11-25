import { Link } from 'react-router-dom'
import { ExternalLink, Info, Wrench } from 'lucide-react'
import { useAllowedPages, useCan } from '@/stores/auth'
import { connectionHint, manualAddressNote, type ManualAddress } from './connectionAdvice'

/**
 * Under a failed (or passed) service check test: says in plain words when the failure is about certificate
 * trust or name resolution and where to fix it, and when the test connected to a manual host address instead
 * of using DNS (docs/service-check-connections.md). Renders nothing when neither applies.
 *
 * Both settings are administrator-only, so other accounts are told what to ask for instead of given a link.
 * `newTab` keeps an unsaved form open while the setting is changed.
 */
export function ConnectionHints({ failed, diagnosis, error, manualAddresses, newTab = false }: {
  failed: boolean
  diagnosis?: string | null
  error?: string | null
  manualAddresses?: readonly ManualAddress[] | null
  newTab?: boolean
}) {
  const admin = useCan()('system.admin')
  const pages = useAllowedPages()
  const hint = failed ? connectionHint(diagnosis, error) : null
  const manual = manualAddressNote(manualAddresses)
  if (!hint && !manual) return null
  const reachable = !!hint?.link && admin && (!pages || pages.has(hint.link.page))
  return (
    <div className="space-y-2">
      {manual && (
        <div role="note" className="flex items-start gap-2 rounded-md border border-info/30 bg-info/5 px-3 py-2 text-xs text-text2">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" aria-hidden />
          <span>{manual}</span>
        </div>
      )}
      {hint && (
        <div role="note" className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-text2">
          <Wrench className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
          <div className="min-w-0 space-y-1">
            <div className="font-semibold text-text">{hint.title}</div>
            <p>{hint.text}</p>
            {hint.link && (reachable
              ? (
                <Link to={hint.link.to} className="inline-flex items-center gap-1 font-medium text-primary underline-offset-2 hover:underline"
                  {...(newTab ? { target: '_blank', rel: 'noreferrer' } : {})}>
                  {hint.link.label}{newTab && <ExternalLink className="h-3 w-3" aria-label="opens in a new tab" />}
                </Link>
              )
              : <p className="text-muted">An administrator changes this under Settings &gt; General &gt; {hint.link.label}.</p>)}
          </div>
        </div>
      )}
    </div>
  )
}
