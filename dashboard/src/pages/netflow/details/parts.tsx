import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatBytes, formatBytesExact, ipDisplay } from '@/lib/netflow/format'
import type { InterfaceRef } from '../cards/shapes'

/** A labelled figure; the value is never truncated (it wraps on narrow screens). */
function DetailMetric({ label, value, sub, title }: { label: string; value: ReactNode; sub?: ReactNode; title?: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border/70 bg-surface2/30 p-3" title={title}>
      <div className="text-2xs font-semibold uppercase tracking-wider text-text2">{label}</div>
      <div className="mt-1 break-words text-lg font-semibold tabular-nums leading-tight text-text">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-text2">{sub}</div>}
    </div>
  )
}

function BytesMetric({ label, bytes, sub }: { label: string; bytes: number | null | undefined; sub?: ReactNode }) {
  return <DetailMetric label={label} value={formatBytes(bytes)} sub={sub} title={formatBytesExact(bytes)} />
}

function DetailListPanel({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section className={cn('min-w-0 rounded-md border border-border/70 bg-surface2/30 p-3', className)}>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-text2">{title}</h4>
      <div className="space-y-2">{children}</div>
    </section>
  )
}

function PillList({ label, values, empty, mono = false }: { label: string; values: string[]; empty: string; mono?: boolean }) {
  const clean = Array.from(new Set(values.filter(Boolean))).slice(0, 12)
  return (
    <div>
      <div className="mb-1 text-2xs uppercase tracking-wider text-text2">{label}</div>
      {clean.length ? (
        <ul role="list" className="flex flex-wrap gap-1.5">
          {clean.map((value) => (
            <li key={value} className={cn('rounded border border-border bg-surface px-2 py-0.5 text-xs text-text', mono && 'font-mono')}>
              {value}
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-xs text-text2">{empty}</div>
      )}
    </div>
  )
}

/** Interfaces as observed: each one names its exporter (never a cross product of lists). */
function InterfaceList({ title, values, empty, filterTo }: { title: string; values: InterfaceRef[]; empty: string; filterTo?: (i: InterfaceRef) => string }) {
  return (
    <div>
      <div className="mb-1 text-2xs uppercase tracking-wider text-text2">{title}</div>
      {values.length ? (
        <ul role="list" className="flex flex-wrap gap-1.5">
          {values.map((v) => {
            const text = (
              <>
                <span className="font-medium">{v.display_name || `ifIndex ${v.ifindex}`}</span>
                <span className="ml-1 font-mono text-text2">#{v.ifindex}</span>
                <span className="ml-1 text-text2">@ {v.device_hostname || ipDisplay(v.exporter_ip)}</span>
              </>
            )
            return (
              <li key={v.iface} className="rounded border border-border bg-surface px-2 py-0.5 text-xs text-text">
                {filterTo ? (
                  <Link to={filterTo(v)} className="hover:text-primary hover:underline" title={`Filter to interface ${v.iface}`}>
                    {text}
                  </Link>
                ) : (
                  text
                )}
              </li>
            )
          })}
        </ul>
      ) : (
        <div className="text-xs text-text2">{empty}</div>
      )}
    </div>
  )
}

/** A navigation action inside a detail panel (keyboard reachable, named). */
function DetailLink({ to, children, primary }: { to: string; children: ReactNode; primary?: boolean }) {
  return (
    <Link
      to={to}
      className={cn(
        'inline-flex items-center justify-center gap-1 rounded-md border px-2.5 py-1.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
        primary ? 'border-primary/40 bg-primary/10 text-primary hover:bg-primary/15' : 'border-border bg-surface text-text hover:bg-surface2',
      )}
    >
      {children}
      <ChevronRight className="h-3 w-3" aria-hidden />
    </Link>
  )
}

/** Horizontal two-part bar with its legend in words. */
function DirectionBar({ a, b, aLabel, bLabel }: { a: number; b: number; aLabel: string; bLabel: string }) {
  const sum = a + b
  return (
    <div>
      <div className="flex h-2 w-full gap-[2px] overflow-hidden rounded-full bg-surface" aria-hidden>
        {sum > 0 && a > 0 && <div className="h-full bg-[#2a78d6] dark:bg-[#3987e5]" style={{ flexGrow: a, flexBasis: 0 }} />}
        {sum > 0 && b > 0 && <div className="h-full bg-[#eb6834] dark:bg-[#d95926]" style={{ flexGrow: b, flexBasis: 0 }} />}
      </div>
      <div className="mt-1 flex flex-wrap justify-between gap-x-3 text-xs tabular-nums text-text2">
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-[#2a78d6] dark:bg-[#3987e5]" aria-hidden />{aLabel}</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-[#eb6834] dark:bg-[#d95926]" aria-hidden />{bLabel}</span>
      </div>
    </div>
  )
}

export { DetailMetric, BytesMetric, DetailListPanel, PillList, InterfaceList, DetailLink, DirectionBar }
