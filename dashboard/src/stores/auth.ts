import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { api, setApiToken } from '@/lib/api'

export type User = {
  id: string
  username: string
  email: string
  full_name?: string
  role: 'admin' | 'editor' | 'viewer'
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
