// Error-issue triage status badge.

const STATUS: Record<string, { label: string; color: string }> = {
  unresolved: { label: 'Unresolved', color: '#ef4444' },
  resolved: { label: 'Resolved', color: '#22c55e' },
  resolved_in_version: { label: 'Resolved in version', color: '#3b82f6' },
  ignored: { label: 'Ignored', color: '#6b7280' },
}

export function ErrorStatusBadge({ status }: { status: string }) {
  const s = STATUS[status] || STATUS.unresolved
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ backgroundColor: `${s.color}1a`, color: s.color, border: `1px solid ${s.color}40` }}>
      {s.label}
    </span>
  )
}

export const ERROR_STATUSES = ['unresolved', 'resolved', 'resolved_in_version', 'ignored']
