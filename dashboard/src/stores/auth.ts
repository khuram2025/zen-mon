import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { api, setApiToken } from '@/lib/api'

export type User = {
  id: string
  username: string
  email: string
  full_name?: string
  role: string
  auth_source?: 'local' | 'ldap' | 'radius'
  permissions?: string[]
}

type AuthState = {
  token: string | null
  user: User | null
  loading: boolean
  error: string | null
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  hydrateUser: () => Promise<void>
}

/** True when the signed-in user's role grants `permission`.
 * `system.admin` implies everything. Sessions persisted before RBAC have
 * no permissions array; treat their legacy admin role as full access. */
export function hasPermission(user: User | null, permission: string): boolean {
  if (!user) return false
  if (!user.permissions) return user.role === 'admin'
  return user.permissions.includes('system.admin') || user.permissions.includes(permission)
}

/** Hook: `const can = useCan(); can('users.manage')` */
export function useCan() {
  const user = useAuth((s) => s.user)
  return (permission: string) => hasPermission(user, permission)
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
          setApiToken(data.access_token)
          set({ token: data.access_token, loading: false, error: null })
          await get().hydrateUser()
        } catch (e: any) {
          const detail = e?.response?.data?.detail || e?.message || 'Login failed'
          set({ loading: false, error: typeof detail === 'string' ? detail : 'Login failed' })
          throw e
        }
      },

      logout() {
        setApiToken(null)
        set({ token: null, user: null, error: null })
      },

      async hydrateUser() {
        if (!get().token) return
        try {
          const { data } = await api.get('/auth/me')
          set({ user: data })
        } catch {
          set({ token: null, user: null })
          setApiToken(null)
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
}
