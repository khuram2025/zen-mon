import { create } from 'zustand'
import { persist } from 'zustand/middleware'

type Theme = 'dark' | 'light'

type ThemeState = {
  theme: Theme
  toggle: () => void
  set: (t: Theme) => void
}

export const useTheme = create<ThemeState>()(
  persist(
    (set, get) => ({
      theme: 'dark',
      toggle: () => {
        const next: Theme = get().theme === 'dark' ? 'light' : 'dark'
        applyTheme(next)
        set({ theme: next })
      },
      set: (t) => {
        applyTheme(t)
        set({ theme: t })
      },
    }),
    {
      name: 'zp-theme',
      onRehydrateStorage: () => (state) => {
        if (state?.theme) applyTheme(state.theme)
      },
    },
  ),
)

function applyTheme(t: Theme) {
  if (typeof document === 'undefined') return
  if (t === 'dark') document.documentElement.classList.add('dark')
  else document.documentElement.classList.remove('dark')
}
