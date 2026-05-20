import { useEffect, useRef, useState } from 'react'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { Input } from './Input'
import { cn } from '@/lib/utils'

/**
 * Password input with a built-in reveal/hide toggle.
 *
 * Security rules baked in (matching what 1Password, AWS, Cisco admin consoles
 * do):
 *   - Defaults to masked. Reveal only on explicit click.
 *   - Auto-reverts to masked on blur (focus leaves the field) so a quickly
 *     opened dropdown / lost focus never strands a secret on screen.
 *   - In edit mode with `hasStored`, never auto-populates from the backend.
 *     If the caller provides `onReveal`, that callback fires the *first*
 *     time the user clicks the eye — typically lazy-fetches the stored
 *     value from a dedicated /secrets endpoint and writes it back via
 *     onChange. After that the field behaves like a normal reveal.
 *   - The reveal button is `type="button"` so submitting the form by
 *     pressing Enter never triggers a stray reveal.
 *
 * Why a custom component and not just `type={show ? 'text' : 'password'}`:
 * we want every credential field in the app to behave identically and to
 * pick up future hardening (e.g. timed auto-hide) in one place.
 */
export interface PasswordInputProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type' | 'autoComplete'> {
  /** Current input value — caller owns state. */
  value: string
  /** Optional override of the autoComplete hint. Default 'new-password'. */
  autoComplete?: string
  /**
   * Set when editing an existing record with a saved secret. Renders the
   * "••••••••" placeholder and shows the eye button even when the field is
   * empty (so the user can reveal what's stored).
   */
  hasStored?: boolean
  /**
   * Called the first time the user clicks the eye in edit mode. Typically
   * fetches the stored plaintext from a dedicated endpoint, then writes it
   * back through onChange. If omitted, the eye just toggles
   * mask <-> typed-value visibility (still useful for create mode).
   */
  onReveal?: () => Promise<void>
  /** Optional flag used by callers that share one "revealing…" spinner. */
  revealing?: boolean
  /** Hide the eye button entirely (e.g. for read-only views). */
  noReveal?: boolean
}

export function PasswordInput({
  value,
  onChange,
  onBlur,
  hasStored,
  onReveal,
  revealing: externalRevealing,
  noReveal,
  autoComplete = 'new-password',
  className,
  placeholder,
  ...rest
}: PasswordInputProps) {
  const [revealed, setRevealed] = useState(false)
  const [internalLoading, setInternalLoading] = useState(false)
  const fetchedOnceRef = useRef(false)
  const loading = externalRevealing || internalLoading

  // Always re-mask when the input loses focus. This is the single biggest
  // protection against accidentally leaving a secret visible on screen
  // while switching to another tab.
  useEffect(() => {
    if (revealed) {
      const handler = () => setRevealed(false)
      window.addEventListener('blur', handler)
      return () => window.removeEventListener('blur', handler)
    }
  }, [revealed])

  async function toggle() {
    if (revealed) {
      setRevealed(false)
      return
    }
    // First-time reveal in edit mode: ask the parent to lazy-fetch the
    // stored value. Subsequent reveals just flip visibility.
    if (onReveal && !fetchedOnceRef.current) {
      try {
        setInternalLoading(true)
        await onReveal()
        fetchedOnceRef.current = true
      } catch {
        // Parent shows the toast; we just don't switch to revealed state.
        return
      } finally {
        setInternalLoading(false)
      }
    }
    setRevealed(true)
  }

  const showEye = !noReveal && (value.length > 0 || hasStored)
  // In edit mode with a stored secret we always want to render the masked
  // dots so the admin can tell "there's a value here, click the eye to see
  // it" — regardless of whatever placeholder the caller passed for the
  // create-mode hint. The caller's placeholder still applies in create
  // mode (e.g. "Min 8 chars").
  const effectivePlaceholder = hasStored ? '••••••••' : placeholder

  return (
    <div className="relative">
      <Input
        type={revealed ? 'text' : 'password'}
        value={value}
        onChange={onChange}
        onBlur={(e) => {
          setRevealed(false)
          onBlur?.(e)
        }}
        autoComplete={autoComplete}
        placeholder={effectivePlaceholder}
        className={cn(showEye && 'pr-9', className)}
        {...rest}
      />
      {showEye && (
        <button
          type="button"
          onClick={toggle}
          disabled={loading || rest.disabled}
          className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted hover:text-text disabled:opacity-50"
          aria-label={revealed ? 'Hide' : 'Reveal'}
          title={revealed ? 'Hide' : 'Reveal'}
          tabIndex={-1}
        >
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : revealed ? (
            <EyeOff className="h-3.5 w-3.5" />
          ) : (
            <Eye className="h-3.5 w-3.5" />
          )}
        </button>
      )}
    </div>
  )
}
