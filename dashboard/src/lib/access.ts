// Shared helpers for Settings → Users & Access (roles, users, authentication).
import type { PermissionCatalogModule, PermissionRisk } from '@/types'

export type Catalog = { modules: PermissionCatalogModule[]; superuser_permission: string }

export const ACCESS_CONTROL_PERMISSIONS = ['users.manage', 'roles.manage', 'system.admin']

export const RISK_META: Record<PermissionRisk, { label: string; tone: string; hint: string }> = {
  standard: { label: 'Standard', tone: 'text-muted', hint: 'Ordinary operational access' },
  sensitive: { label: 'Sensitive', tone: 'text-info', hint: 'Exposes data most operators should not read by default' },
  privileged: { label: 'Privileged', tone: 'text-warning', hint: 'Changes who can do what, or shared infrastructure' },
  critical: { label: 'Critical', tone: 'text-danger', hint: 'Unrestricted control of the appliance' },
}

export function riskOf(catalog: Catalog | undefined, id: string): PermissionRisk {
  for (const m of catalog?.modules || []) {
    const p = m.permissions.find((x) => x.id === id)
    if (p) return p.risk || 'standard'
  }
  return 'standard'
}

export function isElevated(risk: PermissionRisk) {
  return risk === 'privileged' || risk === 'critical'
}

/** Add a permission together with the ones it depends on (view for manage, …). */
export function withImplied(catalog: Catalog | undefined, granted: Set<string>, id: string): Set<string> {
  const next = new Set(granted)
  const byId = new Map<string, string[]>()
  for (const m of catalog?.modules || []) for (const p of m.permissions) byId.set(p.id, p.implies || [])
  const stack = [id]
  while (stack.length) {
    const cur = stack.pop()!
    if (next.has(cur) && cur !== id) continue
    next.add(cur)
    for (const dep of byId.get(cur) || []) if (!next.has(dep)) stack.push(dep)
  }
  return next
}

/** Permissions that depend on `id` and are currently granted (removing `id` would orphan them). */
export function dependentsOf(catalog: Catalog | undefined, granted: Set<string>, id: string): string[] {
  const out: string[] = []
  for (const m of catalog?.modules || []) {
    for (const p of m.permissions) if (granted.has(p.id) && (p.implies || []).includes(id)) out.push(p.id)
  }
  return out
}

export type ApiFailure = {
  kind: 'forbidden' | 'conflict' | 'validation' | 'network' | 'timeout' | 'server' | 'unknown'
  status?: number
  message: string
  code?: string
  detail?: any
}

/** Classify a failed request so screens can say what actually went wrong
 *  instead of rendering an empty list or an endless skeleton. */
export function describeFailure(e: any): ApiFailure {
  const status: number | undefined = e?.response?.status
  const detail = e?.response?.data?.detail
  const code = typeof detail === 'object' && detail ? detail.code : undefined
  const message =
    typeof detail === 'string' ? detail
    : detail?.message ? String(detail.message)
    : Array.isArray(detail) ? detail.map((d: any) => d?.msg || String(d)).join('; ')
    : e?.message || 'Request failed'
  if (e?.code === 'ECONNABORTED' || /timeout/i.test(e?.message || '')) {
    return { kind: 'timeout', message: 'The server took too long to answer.', detail }
  }
  if (!e?.response) return { kind: 'network', message: 'Cannot reach the appliance. Check your connection.', detail }
  if (status === 403) return { kind: 'forbidden', status, message: message || 'Your role does not allow this.', code, detail }
  if (status === 409) return { kind: 'conflict', status, message, code, detail }
  if (status === 400 || status === 422) return { kind: 'validation', status, message, code, detail }
  if (status && status >= 500) return { kind: 'server', status, message: 'The appliance reported an error.', detail }
  return { kind: 'unknown', status, message, code, detail }
}
