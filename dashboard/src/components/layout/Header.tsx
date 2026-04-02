import { Bell, Search, User } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { AlertStats } from '@/types'

export function Header() {
  const { user, logout } = useAuthStore()

  const { data: alertStats } = useQuery({
    queryKey: ['alert-stats'],
    queryFn: () => api.get<AlertStats>('/alerts/stats'),
    refetchInterval: 15_000,
  })

  return (
    <header className="h-14 bg-[var(--bg-secondary)] border-b border-[var(--bg-elevated)] flex items-center justify-between px-6 fixed top-0 left-16 right-0 z-40">
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-semibold text-[var(--text-primary)]">ZenPlus</h1>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)]" />
          <input
            type="text"
            placeholder="Search devices..."
            className="bg-[var(--bg-tertiary)] text-[var(--text-secondary)] pl-10 pr-4 py-1.5 rounded-lg text-sm border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] w-72"
          />
        </div>
      </div>

      <div className="flex items-center gap-4">
        {/* Alert badge */}
        <button className="relative p-2 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors">
          <Bell className="w-5 h-5 text-[var(--text-secondary)]" />
          {alertStats && alertStats.active > 0 && (
            <span className="absolute -top-0.5 -right-0.5 bg-[var(--status-down)] text-white text-[10px] font-bold rounded-full w-5 h-5 flex items-center justify-center">
              {alertStats.active > 99 ? '99+' : alertStats.active}
            </span>
          )}
        </button>

        {/* User menu */}
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-[var(--accent)] rounded-full flex items-center justify-center">
            <User className="w-4 h-4 text-white" />
          </div>
          <span className="text-sm text-[var(--text-secondary)]">{user?.username || 'Admin'}</span>
          <button
            onClick={logout}
            className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] ml-2"
          >
            Logout
          </button>
        </div>
      </div>
    </header>
  )
}
