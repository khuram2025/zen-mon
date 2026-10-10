import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { api, setApiToken } from '@/lib/api'

export type User = {
  id: string
  username: string
  email: string
  full_name?: string
  role: string
  auth_source?: 'local' | 'ldap' | 'radius' | 'oidc'
  permissions?: string[]
  /** Digest of role + permissions; changes when the account's effective access changes. */
  permissions_revision?: string
  scope_tags?: string[]
  /** Scope inherited from the role; the effective scope is both lists together. */
  role_scope_tags?: string[]
  must_change_password?: boolean
  mfa_enabled?: boolean
  /** Policy requires MFA and this account has not enrolled yet. */
  mfa_required?: boolean
  session_id?: string | null
}

/** Result of the first sign-in step. */
export type LoginOutcome = { status: 'signed-in' } | { status: 'mfa'; challenge: string }

type AuthState = {
  token: string | null
  user: User | null
  loading: boolean
  error: string | null
  login: (username: string, password: string) => Promise<LoginOutcome>
  completeMfa: (challenge: string, code: string) => Promise<void>
  completeSso: (code: string) => Promise<void>
  logout: () => void
  hydrateUser: () => Promise<void>
}

/** True when the signed-in user's role grants `permission`.
 * `system.admin` implies everything. Authorization is decided by the server;
 * this only decides what to show. An account whose permissions are not loaded
 * yet has none (never a role-name guess). */
export function hasPermission(user: User | null, permission: string): boolean {
  if (!user || !user.permissions) return false
  return user.permissions.includes('system.admin') || user.permissions.includes(permission)
}

export function hasAnyPermission(user: User | null, permissions: readonly string[]): boolean {
  return permissions.some((p) => hasPermission(user, p))
}

/** Hook: `const can = useCan(); can('users.manage')` */
export function useCan() {
  const user = useAuth((s) => s.user)
  return (permission: string) => hasPermission(user, permission)
}

/** Hook: true when the role grants at least one of the permissions. */
export function useCanAny() {
  const user = useAuth((s) => s.user)
  return (permissions: readonly string[]) => hasAnyPermission(user, permissions)
}

/** What `shouldResetQueryCache` looks at: the part of the session a cached response can depend on. */
export type SessionIdentity = {
  token: string | null
  user: Pick<User, 'id' | 'permissions_revision' | 'scope_tags' | 'role_scope_tags'> | null
}

/** Two scope lists differ; a list one side has not loaded yet (the login response carries fewer fields than /auth/me) is not a difference. */
const tagsDiffer = (a?: readonly string[], b?: readonly string[]) =>
  a !== undefined && b !== undefined && [...a].sort().join('\u0000') !== [...b].sort().join('\u0000')

/**
 * Whether the data fetched for `prev` must not be shown to `next`. Server responses (above all the tag-scoped
 * Inventory ones) depend on WHO asked, but a query key carries no identity, so the cache is thrown away
 * whenever the answer to "who is signed in, and what may they see" changes (review finding FE-01):
 * the token goes away (sign-out, an expired or rejected session), another account signs in, or the same
 * account's access changes (a role edit that moves its permissions or tag scope).
 */
export function shouldResetQueryCache(prev: SessionIdentity, next: SessionIdentity): boolean {
  if (prev.token && !next.token) return true
  if ((prev.user?.id ?? null) !== (next.user?.id ?? null)) return true
  if (!prev.user || !next.user) return false
  const a = prev.user
  const b = next.user
  return (
    (a.permissions_revision !== undefined && b.permissions_revision !== undefined && a.permissions_revision !== b.permissions_revision)
    || tagsDiffer(a.scope_tags, b.scope_tags)
    || tagsDiffer(a.role_scope_tags, b.role_scope_tags)
  )
}

/**
 * Empties `client` (a React Query `QueryClient`) whenever {@link shouldResetQueryCache} says the session
 * changed. Called once from main.tsx; returns the unsubscribe function. Covers login, logout and the
 * `zp-auth-expired` event (which logs out).
 */
export function resetQueryCacheOnSessionChange(
  store: { subscribe(listener: (state: SessionIdentity, prev: SessionIdentity) => void): () => void },
  client: { clear(): void },
): () => void {
  return store.subscribe((state, prev) => {
    if (shouldResetQueryCache(prev, state)) client.clear()
  })
}

export const useAuth = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      loading: false,
      error: null,

      async login(username: string, password: string) {
        set({ loading: true, error: null })
        try {
          const { data } = await api.post('/auth/login', { username, password })
          if (data.mfa_required && data.challenge) {
            set({ loading: false })
            return { status: 'mfa', challenge: data.challenge } as LoginOutcome
          }
          setApiToken(data.access_token)
          set({ token: data.access_token, user: data.user ?? null, loading: false, error: null })
          await get().hydrateUser()
          return { status: 'signed-in' } as LoginOutcome
        } catch (e: any) {
          const detail = e?.response?.data?.detail || e?.message || 'Login failed'
          set({ loading: false, error: typeof detail === 'string' ? detail : 'Login failed' })
          throw e
        }
      },

      async completeMfa(challenge: string, code: string) {
        set({ loading: true, error: null })
        try {
          const { data } = await api.post('/auth/login/mfa', { challenge, code })
          setApiToken(data.access_token)
          set({ token: data.access_token, user: data.user ?? null, loading: false, error: null })
          await get().hydrateUser()
        } catch (e: any) {
          const detail = e?.response?.data?.detail || e?.message || 'Verification failed'
          set({ loading: false, error: typeof detail === 'string' ? detail : 'Verification failed' })
          throw e
        }
      },

      async completeSso(code: string) {
        set({ loading: true, error: null })
        try {
          const { data } = await api.post('/auth/oidc/exchange', { code })
          setApiToken(data.access_token)
          set({ token: data.access_token, user: data.user ?? null, loading: false, error: null })
          await get().hydrateUser()
        } catch (e: any) {
          const detail = e?.response?.data?.detail || e?.message || 'Single sign-on failed'
          set({ loading: false, error: typeof detail === 'string' ? detail : 'Single sign-on failed' })
          throw e
        }
      },

      logout() {
        const token = get().token
        if (token) void fetch('/api/v1/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, keepalive: true }).catch(() => {})
        setApiToken(null)
        set({ token: null, user: null, error: null })
      },

      async hydrateUser() {
        if (!get().token) return
        try {
          const { data } = await api.get('/auth/me')
          set({ user: data })
        } catch (e: any) {
          // A transient failure must not sign the user out; only a rejected
          // session does (the api layer already fires zp-auth-expired on 401).
          if (e?.response?.status === 401) {
            set({ token: null, user: null })
            setApiToken(null)
          }
        }
      },
    }),
    {
      name: 'zp-auth',
      partialize: (state) => ({ token: state.token, user: state.user }),
      onRehydrateStorage: () => (state) => {
        if (state?.token) {
          setApiToken(state.token)
        }
      },
    },
  ),
)

// Wire the global 401 event so axios can boot the user out.
if (typeof window !== 'undefined') {
  window.addEventListener('zp-auth-expired', () => {
    useAuth.getState().logout()
  })

  // Role edits are enforced server-side within seconds; keep what the UI shows
  // in step. Re-read the account when the tab regains focus and once a minute
  // while it is open, so grants and revocations appear without a re-login.
  const REFRESH_MS = 60_000
  let lastRefresh = Date.now()
  const refresh = () => {
    if (!useAuth.getState().token) return
    lastRefresh = Date.now()
    void useAuth.getState().hydrateUser()
  }
  window.setInterval(() => { if (document.visibilityState === 'visible') refresh() }, REFRESH_MS)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && Date.now() - lastRefresh > 5_000) refresh()
  })
}
