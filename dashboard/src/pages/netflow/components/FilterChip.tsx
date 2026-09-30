import { X } from 'lucide-react'

function FilterChip({ icon: Icon, label, value, onClear, mono }: { icon?: any; label: string; value: string; onClear: () => void; mono?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
      {Icon && <Icon className="h-3 w-3" />}
      <span className="text-muted">{label}:</span>
      <span className={mono ? 'font-mono' : ''}>{value}</span>
      <button onClick={onClear} className="ml-0.5 rounded-full p-0.5 hover:bg-primary/20" aria-label={`Clear ${label} filter`}>
        <X className="h-3 w-3" />
      </button>
    </span>
  )
}

export { FilterChip }
