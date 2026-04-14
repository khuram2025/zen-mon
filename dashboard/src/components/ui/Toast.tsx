import { useEffect } from 'react'
import { create } from 'zustand'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'
import { cn } from '@/lib/utils'

type ToastKind = 'success' | 'error' | 'info'
type Toast = { id: number; kind: ToastKind; title: string; description?: string }

type ToastState = {
  toasts: Toast[]
  push: (t: Omit<Toast, 'id'>) => void
  dismiss: (id: number) => void
}

const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  push: (t) => {
    const id = Date.now() + Math.random()
    set((s) => ({ toasts: [...s.toasts, { id, ...t }] }))
    setTimeout(() => {
      set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) }))
    }, 4500)
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })),
}))

export function toast(opts: Omit<Toast, 'id'>) {
  useToastStore.getState().push(opts)
}

toast.success = (title: string, description?: string) =>
  useToastStore.getState().push({ kind: 'success', title, description })
toast.error = (title: string, description?: string) =>
  useToastStore.getState().push({ kind: 'error', title, description })
toast.info = (title: string, description?: string) =>
  useToastStore.getState().push({ kind: 'info', title, description })

export function ToastViewport() {
  const toasts = useToastStore((s) => s.toasts)
  const dismiss = useToastStore((s) => s.dismiss)

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-full max-w-sm flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            'pointer-events-auto flex items-start gap-3 rounded-lg border bg-surface p-3 shadow-elevated animate-slide-up',
            t.kind === 'success' && 'border-success/30',
            t.kind === 'error' && 'border-danger/30',
            t.kind === 'info' && 'border-info/30',
          )}
        >
          <div className="mt-0.5 shrink-0">
            {t.kind === 'success' && <CheckCircle2 className="h-4 w-4 text-success" />}
            {t.kind === 'error' && <AlertCircle className="h-4 w-4 text-danger" />}
            {t.kind === 'info' && <Info className="h-4 w-4 text-info" />}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-text">{t.title}</div>
            {t.description && <div className="mt-0.5 text-xs text-muted">{t.description}</div>}
          </div>
          <button
            onClick={() => dismiss(t.id)}
            className="shrink-0 rounded p-0.5 text-muted hover:bg-surface2 hover:text-text"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  )
}
