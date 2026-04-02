import { create } from 'zustand'
import type { User } from '@/types'
import { api } from '@/lib/api'

interface AuthState {
  user: User | null
  isAuthenticated: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  checkAuth: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: !!api.getToken(),

  login: async (username: string, password: string) => {
    const res = await api.post<{ access_token: string; user: User }>('/auth/login', {
      username,
      password,
    })
    api.setToken(res.access_token)
    set({ user: res.user, isAuthenticated: true })
  },

  logout: () => {
    api.setToken(null)
    set({ user: null, isAuthenticated: false })
  },

  checkAuth: async () => {
    try {
      const user = await api.get<User>('/auth/me')
      set({ user, isAuthenticated: true })
    } catch {
      api.setToken(null)
      set({ user: null, isAuthenticated: false })
    }
  },
}))
