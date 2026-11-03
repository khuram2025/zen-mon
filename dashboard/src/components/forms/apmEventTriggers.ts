export const APM_EVENT_TRIGGERS = [
  { value: 'apm_synthetic_down', label: 'Synthetic Scenario Down' },
  { value: 'apm_slo_burn', label: 'SLO Burn' },
] as const

export function apmScopeKind(metric: string) {
  if (metric === 'apm_synthetic_down') return 'scenario'
  if (metric === 'apm_slo_burn') return 'slo'
  return metric.startsWith('apm_rum_') ? 'rum' : 'service'
}
export function isApmEvent(metric: string) {
  return APM_EVENT_TRIGGERS.some(trigger => trigger.value === metric)
}
export function apmEventScope(metric: string) {
  return metric === 'apm_synthetic_down'
    ? { label: 'Synthetic scenario', all: 'All scenarios', hint: 'Covers every enabled scenario, including scenarios added later. Alerts only after each scenario exhausts its configured retries; recovery follows a successful run.' }
    : { label: 'SLO', all: 'All SLOs', hint: 'Uses each SLO’s existing multi-window burn thresholds. One incident per rule and SLO; missing traffic does not count as recovery.' }
}
