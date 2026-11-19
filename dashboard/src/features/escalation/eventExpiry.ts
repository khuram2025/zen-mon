/**
 * Event alert expiry policy — pure model for the card on the Escalation &
 * On-call "Alert storms" tab. Mirrors app.services.alert_expiry: settings
 * shape, limits and the plain-language summary. Imports nothing, so
 * scripts/test-event-expiry-ui.mjs can run it on its own.
 */

export interface EventExpiryPolicy {
  enabled: boolean
  hours: number
}

export const EVENT_EXPIRY_DEFAULTS: EventExpiryPolicy = { enabled: true, hours: 168 }
export const EVENT_EXPIRY_HOURS: readonly [number, number] = [1, 8760]

export function eventExpiryFromApi(raw: Record<string, any> | null | undefined): EventExpiryPolicy {
  const r = raw ?? {}
  const n = typeof r.hours === 'number' ? r.hours : NaN
  const [lo, hi] = EVENT_EXPIRY_HOURS
  return {
    enabled: r.enabled === undefined ? EVENT_EXPIRY_DEFAULTS.enabled : !!r.enabled,
    hours: Number.isFinite(n) ? Math.max(lo, Math.min(hi, Math.trunc(n))) : EVENT_EXPIRY_DEFAULTS.hours,
  }
}

export function validateEventExpiry(p: EventExpiryPolicy): Partial<Record<keyof EventExpiryPolicy, string>> {
  const [lo, hi] = EVENT_EXPIRY_HOURS
  if (!Number.isInteger(p.hours) || p.hours < lo || p.hours > hi) {
    return { hours: `Hours must be a whole number from ${lo} to ${hi}` }
  }
  return {}
}

/** "7 days", "36 hours", "1 hour" — same wording as the server. */
export function expiryWindowLabel(hours: number): string {
  if (!Number.isFinite(hours)) return ''
  if (hours % 24 === 0) {
    const days = hours / 24
    return `${days} day${days === 1 ? '' : 's'}`
  }
  return `${hours} hour${hours === 1 ? '' : 's'}`
}

export function eventExpirySentence(p: EventExpiryPolicy): string {
  if (!p.enabled) {
    return 'Off: trap, syslog, Device Tracker and configuration-change alerts stay open until someone closes them.'
  }
  return `Trap, syslog, Device Tracker and configuration-change alerts close by themselves after `
    + `${expiryWindowLabel(p.hours)} without a new event. Closing sends no notification.`
}
