import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export type PasswordPolicy = { min_length: number; max_length: number; rules: string[] }

// Shown until the server answers; the API remains the authority.
export const DEFAULT_PASSWORD_POLICY: PasswordPolicy = {
  min_length: 12,
  max_length: 128,
  rules: [
    'At least 12 characters (up to 128); a passphrase works well',
    'Not a commonly used or previously breached password',
    'Must not contain your username or email name',
    'No special characters or periodic changes are required',
  ],
}

/** The local-account password rules, shared by every password form so the UI
 *  never promises something the API rejects. */
export function usePasswordPolicy(): PasswordPolicy {
  const { data } = useQuery<PasswordPolicy>({
    queryKey: ['password-policy'],
    queryFn: async () => (await api.get('/auth/password-policy')).data,
    staleTime: 10 * 60_000,
    retry: 1,
  })
  return data || DEFAULT_PASSWORD_POLICY
}

const norm = (s: string) => s.replace(/[\s_.\-]+/g, '').toLowerCase()

/** Client-side mirror of the API's checks that can be answered locally. The
 *  server still enforces the breached/common-password list. */
export function passwordProblems(
  pw: string,
  policy: PasswordPolicy,
  ctx: { username?: string; email?: string } = {},
): string[] {
  const out: string[] = []
  if (pw.length < policy.min_length) out.push(`Use at least ${policy.min_length} characters (${pw.length} so far)`)
  if (pw.length > policy.max_length) out.push(`Use at most ${policy.max_length} characters`)
  const flat = norm(pw)
  for (const [label, raw] of [['username', ctx.username || ''], ['email name', (ctx.email || '').split('@')[0]]] as const) {
    const t = norm(raw)
    if (t.length >= 4 && flat.includes(t)) out.push(`Must not contain your ${label}`)
  }
  return out
}
