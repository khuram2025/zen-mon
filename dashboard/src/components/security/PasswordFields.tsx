import { Check, Circle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { FormField } from '@/components/ui/FormField'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { passwordProblems, usePasswordPolicy } from '@/hooks/usePasswordPolicy'

/** New-password + confirmation with the live policy checklist. Callers receive
 *  validity through `usePasswordCheck` so Save can stay disabled until it holds. */
export function usePasswordCheck(pw: string, confirm: string, ctx: { username?: string; email?: string } = {}) {
  const policy = usePasswordPolicy()
  const problems = passwordProblems(pw, policy, ctx)
  const mismatch = confirm.length > 0 && pw !== confirm
  return { policy, problems, mismatch, ok: pw.length > 0 && problems.length === 0 && pw === confirm }
}

export function PasswordRequirements({ pw, ctx, className }: { pw: string; ctx?: { username?: string; email?: string }; className?: string }) {
  const policy = usePasswordPolicy()
  const problems = passwordProblems(pw, policy, ctx)
  const lengthOk = pw.length >= policy.min_length
  return (
    <ul className={cn('space-y-0.5 text-[11px]', className)} aria-label="Password requirements">
      <li className={cn('flex items-center gap-1.5', lengthOk ? 'text-success' : 'text-muted')}>
        {lengthOk ? <Check className="h-3 w-3" aria-hidden /> : <Circle className="h-3 w-3" aria-hidden />}
        At least {policy.min_length} characters {pw.length > 0 && !lengthOk && <span>({pw.length} so far)</span>}
      </li>
      <li className="flex items-center gap-1.5 text-muted">
        <Circle className="h-3 w-3" aria-hidden /> Not a common password, and nothing like your username (checked when you save)
      </li>
      {problems.filter((p) => !p.startsWith('Use at least')).map((p) => (
        <li key={p} className="text-danger">{p}</li>
      ))}
    </ul>
  )
}

export function NewPasswordFields({ password, onPassword, confirm, onConfirm, ctx, autoFocus, label = 'New password', requireConfirm = true }: {
  password: string
  onPassword: (v: string) => void
  confirm: string
  onConfirm: (v: string) => void
  ctx?: { username?: string; email?: string }
  autoFocus?: boolean
  label?: string
  requireConfirm?: boolean
}) {
  const { mismatch } = usePasswordCheck(password, confirm, ctx)
  return (
    <>
      <FormField label={label} required hint={<PasswordRequirements pw={password} ctx={ctx} className="mt-0.5" />}>
        <PasswordInput value={password} onChange={(e) => onPassword(e.target.value)} autoFocus={autoFocus} autoComplete="new-password" />
      </FormField>
      {requireConfirm && (
        <FormField label="Confirm password" required error={mismatch ? 'Passwords do not match' : null}>
          <PasswordInput value={confirm} onChange={(e) => onConfirm(e.target.value)} autoComplete="new-password" />
        </FormField>
      )}
    </>
  )
}
