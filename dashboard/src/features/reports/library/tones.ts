/**
 * Category tints for library icons. Theme tokens only (no palette classes),
 * written out in full so Tailwind keeps every class. Icons use the *-fg
 * tokens where they exist so they keep 3:1 contrast on the light theme.
 */
export type CategoryTone = 'primary' | 'info' | 'success' | 'warning' | 'danger' | 'accent' | 'neutral'

const CATEGORY_TONES: Record<string, CategoryTone> = {
  executive: 'primary',
  availability: 'success',
  performance: 'info',
  interfaces: 'info',
  hardware: 'warning',
  capacity: 'accent',
  services: 'success',
  traffic: 'accent',
  paths: 'info',
  alerts: 'danger',
  configuration: 'warning',
  device_tracker: 'primary',
  inventory: 'primary',
  servers: 'info',
  applications: 'accent',
  experience: 'accent',
  security: 'danger',
  platform: 'neutral',
  other: 'neutral',
}

const FALLBACK: CategoryTone[] = ['primary', 'info', 'success', 'accent', 'warning']

export function categoryTone(category: string | null | undefined): CategoryTone {
  if (!category) return 'neutral'
  const known = CATEGORY_TONES[category]
  if (known) return known
  let h = 0
  for (const ch of category) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return FALLBACK[h % FALLBACK.length]
}

/** Icon tile: tinted background plus icon colour. */
export const TONE_TILE: Record<CategoryTone, string> = {
  primary: 'bg-primary/10 text-primary',
  info: 'bg-info/10 text-info-fg',
  success: 'bg-success/10 text-success-fg',
  warning: 'bg-warning/10 text-warning-fg',
  danger: 'bg-danger/10 text-danger-fg',
  accent: 'bg-accent/10 text-accent',
  neutral: 'bg-surface3 text-text2',
}

/** Thin accent used on the card's top edge. */
export const TONE_BAR: Record<CategoryTone, string> = {
  primary: 'bg-primary/70',
  info: 'bg-info/70',
  success: 'bg-success/70',
  warning: 'bg-warning/70',
  danger: 'bg-danger/70',
  accent: 'bg-accent/70',
  neutral: 'bg-border-strong',
}
