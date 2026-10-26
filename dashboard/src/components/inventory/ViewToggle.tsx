import { BarChart3, Table2 } from 'lucide-react'
import { cn } from '@/lib/utils'

export type ChartView = 'chart' | 'table'

/**
 * "View as table" for a chart (doc 03 section 6): the same numbers as a real table, for assistive
 * technology and for copying. Two buttons in a group, the pressed one marked by state and by fill.
 */
export function ViewToggle({ value, onChange, label }: { value: ChartView; onChange: (v: ChartView) => void; label: string }) {
  const item = (v: ChartView, text: string, Icon: typeof BarChart3) => (
    <button
      type="button"
      aria-pressed={value === v}
      onClick={() => onChange(v)}
      className={cn(
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary',
        value === v ? 'bg-primary text-on-primary shadow-sm' : 'text-text2 hover:bg-surface3 hover:text-text',
      )}
    >
      <Icon className="h-3 w-3" aria-hidden />
      {text}
    </button>
  )
  return (
    <div role="group" aria-label={label} className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
      {item('chart', 'Chart', BarChart3)}
      {item('table', 'Table', Table2)}
    </div>
  )
}
