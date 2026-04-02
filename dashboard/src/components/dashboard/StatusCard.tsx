import { cn } from '@/lib/utils'
import type { ReactNode } from 'react'

interface StatusCardProps {
  title: string
  value: number | string
  subtitle?: string
  icon?: ReactNode
  color?: string
  className?: string
}

export function StatusCard({ title, value, subtitle, icon, color, className }: StatusCardProps) {
  return (
    <div
      className={cn(
        'bg-[var(--bg-secondary)] rounded-xl p-5 border border-[var(--bg-elevated)] hover:border-[var(--bg-elevated)] transition-colors',
        className
      )}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm text-[var(--text-muted)]">{title}</span>
        {icon && <span style={{ color }}>{icon}</span>}
      </div>
      <div className="font-mono text-3xl font-semibold" style={{ color }}>
        {value}
      </div>
      {subtitle && (
        <div className="text-sm text-[var(--text-muted)] mt-1">{subtitle}</div>
      )}
    </div>
  )
}
