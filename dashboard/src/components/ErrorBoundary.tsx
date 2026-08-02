import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'

/**
 * Catches render errors from the page tree so a single bad component shows an
 * inline message instead of unmounting the whole app and leaving a blank screen.
 *
 * `resetKey` (typically the route path) clears the error when the user navigates
 * away, so a broken page does not stay broken for the rest of the session.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; resetKey?: string },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Page render error:', error, info.componentStack)
  }

  componentDidUpdate(prev: { children: ReactNode; resetKey?: string }) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
        <AlertTriangle className="h-8 w-8 text-danger" />
        <div className="text-sm font-medium">This page failed to render</div>
        <div className="max-w-lg text-xs text-muted break-words">{error.message}</div>
        <button
          className="mt-1 rounded-md border border-white/10 px-3 py-1.5 text-xs hover:bg-white/5"
          onClick={() => this.setState({ error: null })}
        >
          Try again
        </button>
      </div>
    )
  }
}
