import { Card, CardContent } from '@/components/ui/Card'

// ─────────────────────────────────────────────────────────────────
// KPI Cards
// ─────────────────────────────────────────────────────────────────

function KpiCard({
  icon: Icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: string
  sub?: React.ReactNode
  tone: 'cyan' | 'violet' | 'amber' | 'emerald' | 'pink'
}) {
  const ringMap: Record<typeof tone, string> = {
    cyan: 'bg-cyan-500/15 text-cyan-300 ring-cyan-400/30 shadow-[0_0_22px_-4px_rgba(34,211,238,0.55)]',
    violet: 'bg-violet-500/15 text-violet-300 ring-violet-400/30 shadow-[0_0_22px_-4px_rgba(167,139,250,0.55)]',
    amber: 'bg-amber-500/15 text-amber-300 ring-amber-400/30 shadow-[0_0_22px_-4px_rgba(251,191,36,0.55)]',
    emerald: 'bg-emerald-500/15 text-emerald-300 ring-emerald-400/30 shadow-[0_0_22px_-4px_rgba(52,211,153,0.55)]',
    pink: 'bg-pink-500/15 text-pink-300 ring-pink-400/30 shadow-[0_0_22px_-4px_rgba(244,114,182,0.55)]',
  }
  return (
    <Card className="relative overflow-hidden">
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-full ring-1 ${ringMap[tone]}`}>
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
          <div className="mt-0.5 truncate text-2xl font-semibold leading-tight text-text">{value}</div>
          <div className="mt-0.5 truncate text-[10px] text-muted">{sub}</div>
        </div>
      </CardContent>
    </Card>
  )
}

export { KpiCard }
