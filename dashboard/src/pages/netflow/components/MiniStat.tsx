function MiniStat({ icon: Icon, label, value, tone }: { icon: React.ComponentType<{ className?: string }>; label: string; value: string; tone: 'cyan' | 'emerald' | 'rose' }) {
  const toneMap = { cyan: 'text-cyan-400 bg-cyan-500/10', emerald: 'text-emerald-400 bg-emerald-500/10', rose: 'text-rose-400 bg-rose-500/10' }
  return (
    <div className="rounded-md border border-border bg-surface2/30 p-2.5">
      <div className="flex items-center justify-between">
        <div className={`flex h-6 w-6 items-center justify-center rounded ${toneMap[tone]}`}>
          <Icon className="h-3 w-3" />
        </div>
      </div>
      <div className="mt-1.5 text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="text-sm font-semibold">{value}</div>
    </div>
  )
}

export { MiniStat }
