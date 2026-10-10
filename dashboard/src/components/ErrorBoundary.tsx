import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { ChunkLoadNotice } from '@/components/ChunkLoadNotice'
import { browserSessionStorage, isChunkLoadError, recoverFromChunkError, type ChunkRecovery } from '@/lib/chunkRecovery'

/**
 * Catches render errors from the page tree so a single bad component shows an
 * inline message instead of unmounting the whole app and leaving a blank screen.
 *
 * `resetKey` (typically the route path) clears the error when the user navigates
 * away, so a broken page does not stay broken for the rest of the session.
 *
 * A page whose lazy chunk cannot be downloaded (the files were replaced by an update while the tab was open, or the
 * link dropped) is a different failure: React.lazy remembers the rejection, so "Try again" can never work. Such an
 * error gets its own notice whose button reloads the page, and the page reloads itself once (guarded, see
 * lib/chunkRecovery.ts) the first time it happens in a session window (review finding NF-01).
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; resetKey?: string },
  { error: Error | null; recovery: ChunkRecovery | null }
> {
  state: { error: Error | null; recovery: ChunkRecovery | null } = { error: null, recovery: null }

  static getDerivedStateFromError(error: Error) {
    return { error, recovery: null }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Page render error:', error, info.componentStack)
    if (isChunkLoadError(error)) {
      const recovery = recoverFromChunkError(error, {
        storage: browserSessionStorage(),
        now: Date.now(),
        reload: () => window.location.reload(),
      })
      this.setState({ recovery })
    }
  }

  componentDidUpdate(prev: { children: ReactNode; resetKey?: string }) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null, recovery: null })
    }
  }

  render() {
    const { error, recovery } = this.state
    if (!error) return this.props.children

    if (isChunkLoadError(error)) {
      return <ChunkLoadNotice reloading={recovery === 'reloading'} onReload={() => window.location.reload()} />
    }

    return (
      <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
        <AlertTriangle className="h-8 w-8 text-danger" />
        <div className="text-sm font-medium">This page failed to render</div>
        <div className="max-w-lg text-xs text-muted break-words">{error.message}</div>
        <button
          className="mt-1 rounded-md border border-white/10 px-3 py-1.5 text-xs hover:bg-white/5"
          onClick={() => this.setState({ error: null, recovery: null })}
        >
          Try again
        </button>
      </div>
    )
  }
}
