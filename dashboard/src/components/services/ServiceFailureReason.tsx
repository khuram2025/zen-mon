import { explainServiceFailure, type FailureContext } from './failureReason'

export function ServiceFailureReason({ reason, failureStage, timeoutSeconds }: FailureContext & { reason?: string | null }) {
  const summary = explainServiceFailure(reason, { failureStage, timeoutSeconds })
  return (
    <div className="min-w-0 break-words text-xs">
      <div>{summary}</div>
      {reason && summary !== reason && (
        <details className="mt-1 text-muted">
          <summary className="w-fit cursor-pointer text-2xs hover:text-text">Technical details</summary>
          <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-2xs">{reason}</pre>
        </details>
      )}
    </div>
  )
}
