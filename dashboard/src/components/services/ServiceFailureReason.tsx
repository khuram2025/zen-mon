import { explainServiceFailure, type FailureContext } from './failureReason'

export function ServiceFailureReason({ reason, failureStage, timeoutSeconds }: FailureContext & { reason?: string | null }) {
  const summary = explainServiceFailure(reason, { failureStage, timeoutSeconds })
  return (
    <div className="min-w-0 break-words text-[11px]">
      <div>{summary}</div>
      {reason && summary !== reason && (
        <details className="mt-1 text-muted">
          <summary className="w-fit cursor-pointer text-[10px] hover:text-text">Technical details</summary>
          <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[10px]">{reason}</pre>
        </details>
      )}
    </div>
  )
}
