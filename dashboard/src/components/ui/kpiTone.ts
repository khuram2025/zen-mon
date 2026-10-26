/**
 * Stat-tile tones, shared by every page's KPI tiles. The top rule and the icon chip carry state (success / warning /
 * danger) in the theme's status colours; tiles without a state stay quiet (primary or neutral). One look across the
 * dashboards instead of a gradient per page.
 */
export const KPI_TONE = {
  success: { bar: 'bg-success', chip: 'bg-success/10 text-success-fg' },
  danger: { bar: 'bg-danger', chip: 'bg-danger/10 text-danger-fg' },
  warning: { bar: 'bg-warning', chip: 'bg-warning/10 text-warning-fg' },
  info: { bar: 'bg-info', chip: 'bg-info/10 text-info-fg' },
  primary: { bar: 'bg-primary/70', chip: 'bg-primary/10 text-primary' },
  accent: { bar: 'bg-primary/70', chip: 'bg-primary/10 text-primary' },
  muted: { bar: 'bg-border-strong', chip: 'bg-surface2 text-muted' },
} as const

export type KpiTone = keyof typeof KPI_TONE

/** Tile label, value and caption classes (uppercase label, large tabular value). */
export const KPI_LABEL = 'truncate text-2xs font-semibold uppercase tracking-wider text-muted'
export const KPI_VALUE = 'whitespace-nowrap text-3xl font-semibold tabular-nums leading-none tracking-tight text-text'

/** Tone classes for a tone name; unknown names fall back to the quiet primary tone. */
export function kpiTone(tone: string): { bar: string; chip: string } {
  return (KPI_TONE as Record<string, { bar: string; chip: string }>)[tone] ?? KPI_TONE.primary
}
