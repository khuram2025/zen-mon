/**
 * Remembers which contract endpoints this appliance lacks, so a page keeps
 * its fallback without re-requesting a known-missing route on every range
 * change. A reload re-probes (e.g. after an upgrade).
 */
import { isUnsupported } from './format'

export type Capability =
  | 'incidents'
  | 'latest-probes'
  | 'check-maintenance'
  | 'maintenance-end'
  | 'notes'
  | 'history'
  | 'report'
  | 'ack-bulk'
  | 'system-time'

const missing = new Set<Capability>()

export function knownUnsupported(c: Capability): boolean {
  return missing.has(c)
}

export function markUnsupported(c: Capability) {
  missing.add(c)
}

/**
 * Run `fn`; a 404/405/501 marks the capability missing and resolves to
 * `null`. Other errors propagate so the caller can show a real failure.
 */
export async function withCapability<T>(c: Capability, fn: () => Promise<T>): Promise<T | null> {
  if (missing.has(c)) return null
  try {
    return await fn()
  } catch (e) {
    if (isUnsupported(e)) {
      missing.add(c)
      return null
    }
    throw e
  }
}
