import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export type TagDef = {
  id: string
  name: string
  color: string | null
  description: string | null
  device_count: number
  server_count: number
  service_count: number
  app_count: number
  link_count: number
  user_count: number
  maintenance_count: number
}

/** Total assignments across every tagged surface. */
export function tagUsage(t: TagDef): number {
  return (t.device_count || 0) + (t.server_count || 0) + (t.service_count || 0)
    + (t.app_count || 0) + (t.link_count || 0)
}

// Kept in sync with the server palette (app/services/tag_service.py).
export const TAG_PALETTE = [
  '#6366f1', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444',
  '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#84cc16',
]

/** Deterministic fallback for names the registry hasn't stored yet — same
 * hash the server uses, so the color doesn't change after the tag saves. */
export function autoTagColor(name: string): string {
  let sum = 0
  for (const b of new TextEncoder().encode(name.toLowerCase())) sum += b
  return TAG_PALETTE[sum % TAG_PALETTE.length]
}

export function useTags(enabled = true) {
  return useQuery<TagDef[]>({
    queryKey: ['tags'],
    queryFn: async () => (await api.get('/tags')).data,
    enabled,
    staleTime: 30_000,
  })
}

/** name(lowercased) → display color, for O(1) chip rendering. */
export function tagColorMap(defs: TagDef[] | undefined): Record<string, string> {
  const map: Record<string, string> = {}
  for (const t of defs || []) map[t.name.toLowerCase()] = t.color || autoTagColor(t.name)
  return map
}

export function tagColor(name: string, map: Record<string, string>): string {
  return map[name.toLowerCase()] || autoTagColor(name)
}
