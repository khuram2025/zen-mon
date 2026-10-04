import { useState, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { KeyRound, Loader2, ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { useAuth } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { FormField } from '@/components/ui/FormField'
import { toast } from '@/components/ui/Toast'
import { NewPasswordFields, usePasswordCheck } from './PasswordFields'
import { MfaPanel } from './MfaPanel'

/** Interposes the sign-in obligations the API enforces: a required password
 *  change, then required multi-factor enrolment. Until both are met every other
 *  request is refused with 403, so the app would only show errors. */
export function AccountGate({ children }: { children: ReactNode }) {
  const user = useAuth((s) => s.user)
  // Stay on the enrolment screen until the recovery codes were acknowledged,
  // even though enrolment itself already cleared the requirement.
  const [holding, setHolding] = useState(false)
  if (user?.must_change_password) return <Frame icon={KeyRound} title="Choose a new password" blurb="An administrator set a temporary password for your account. Choose your own to continue."><ForcedPasswordChange /></Frame>
  if (user?.mfa_required || holding) return <Frame icon={ShieldCheck} title="Set up multi-factor authentication" blurb="Your organisation requires a second sign-in step. Set it up to continue."><MfaPanel onHold={setHolding} /></Frame>
  return <>{children}</>
}

function Frame({ icon: Icon, title, blurb, children }: { icon: typeof KeyRound; title: string; blurb: string; children: ReactNode }) {
  const logout = useAuth((s) => s.logout)
  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4">
      <div className="w-full max-w-md">
        <Card>
          <CardContent className="space-y-4 pt-6">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary"><Icon className="h-5 w-5" /></div>
              <div><h1 className="text-lg font-semibold">{title}</h1><p className="text-xs text-muted">{blurb}</p></div>
            </div>
            {children}
            <Button type="button" variant="link" size="sm" className="px-0" onClick={logout}>Sign out</Button>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function ForcedPasswordChange() {
  const user = useAuth((s) => s.user)
  const logout = useAuth((s) => s.logout)
  const [current, setCurrent] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const check = usePasswordCheck(pw, pw2, { username: user?.username, email: user?.email })
  const change = useMutation({
    mutationFn: async () => api.post('/users/me/change-password', { current_password: current, new_password: pw }),
    onSuccess: () => { toast.success('Password changed', 'Sign in again with your new password.'); logout() },
    onError: (e: any) => toast.error('Change failed', apiErrorMessage(e)),
  })
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (check.ok) change.mutate() }} className="space-y-3">
      <FormField label="Current (temporary) password" required><PasswordInput value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" autoFocus /></FormField>
      <NewPasswordFields password={pw} onPassword={setPw} confirm={pw2} onConfirm={setPw2} ctx={{ username: user?.username, email: user?.email }} />
      <Button type="submit" className="w-full" disabled={!current || !check.ok || change.isPending}>
        {change.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Change password
      </Button>
    </form>
  )
}

