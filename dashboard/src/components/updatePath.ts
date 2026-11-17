// What the Updates tab says about an upgrade path: why each stop is on it and
// whether this appliance is being held back after failed installs. Pure
// functions over the plan and the appliance's own update history.

export interface PathStep {
  id: string
  version: string
  min_version: string | null
  changelog: string
  severity: string
}

export interface PathAttempt {
  version: string
  from_version: string
  status: string
  error: string
  completed_at: string
}

/** The update server stops offering a version to an appliance after this many failed installs. */
export const MAX_FAILED_ATTEMPTS = 3

export interface StepRole {
  required: boolean
  label: string
  reason: string
}

/** Why a step is on the path. Every step before the last is a stop the next release cannot be installed without. */
export function stepRole(steps: PathStep[], index: number): StepRole {
  const step = steps[index]
  const next = steps[index + 1]
  if (!next) {
    return {
      required: false,
      label: 'Latest eligible version',
      reason: steps.length > 1 ? 'Installed last, after every required version before it.' : '',
    }
  }
  return {
    required: true,
    label: 'Required intermediate version',
    reason: next.min_version === step.version
      ? `v${next.version} can only be installed from v${step.version} or later, so this version cannot be skipped.`
      : `Must be installed before v${next.version}; it cannot be skipped.`,
  }
}

export interface PathHold {
  version: string
  attempts: number
  error: string
  lastAt: string
  /** True once the update server stops offering this version to this appliance. */
  exhausted: boolean
}

/**
 * Failed installs from the installed version that have not been followed by a
 * success. `history` is newest first. Returns the version this appliance keeps
 * failing on, or null when the newest finished attempt succeeded.
 */
export function pathHold(history: PathAttempt[], currentVersion: string): PathHold | null {
  const finished = history.filter(r => r.status === 'success' || r.status === 'failed' || r.status === 'rolled_back')
  const newest = finished[0]
  if (!newest || newest.status === 'success' || newest.version === currentVersion) return null
  let attempts = 0
  for (const record of finished) {
    if (record.status === 'success') break
    if (record.version === newest.version && record.from_version === newest.from_version) attempts += 1
  }
  return {
    version: newest.version,
    attempts,
    error: newest.error || '',
    lastAt: newest.completed_at || '',
    exhausted: attempts >= MAX_FAILED_ATTEMPTS,
  }
}

/** One line for the path as a whole: how many installs and restarts it means. */
export function pathSummary(steps: PathStep[]): string {
  if (steps.length <= 1) return 'One installation. Monitoring services restart once.'
  const required = steps.length - 1
  return `${steps.length} installations in order: ${required} required intermediate version${required === 1 ? '' : 's'}, then the latest. ` +
    'Monitoring services restart once per version.'
}
