function EmptyState({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`flex ${compact ? 'min-h-32' : 'h-full'} items-center justify-center rounded-md border border-dashed border-border bg-surface2/30 text-center text-xs text-muted`}>
      No NetFlow records collected for this range.
    </div>
  )
}

export { EmptyState }
