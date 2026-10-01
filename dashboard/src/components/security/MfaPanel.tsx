import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Download, Loader2, ShieldCheck, ShieldOff } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, copyText } from '@/lib/utils'
import { useAuth } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { FormField } from '@/components/ui/FormField'
import { Badge } from '@/components/ui/Badge'
import { toast } from '@/components/ui/Toast'
import { QueryBoundary } from '@/components/ui/QueryState'

type MfaStatus = {
  supported: boolean; enabled: boolean; enrolled_at: string | null
  recovery_codes_remaining: number; pending_setup: boolean; required: boolean
}
type Setup = { secret: string; otpauth_uri: string; issuer: string; account: string }

/** Authenticator-app (TOTP) enrolment for the signed-in local account:
 *  set up, confirm with a code, show the recovery codes once, disable, or
 *  regenerate the codes. Used on the Profile tab and by the enrolment gate. */
export function MfaPanel({ onEnabled, onHold }: { onEnabled?: () => void; onHold?: (holding: boolean) => void }) {
  const qc = useQueryClient()
  const hydrate = useAuth((s) => s.hydrateUser)
  const status = useQuery<MfaStatus>({ queryKey: ['mfa', 'status'], queryFn: async () => (await api.get('/auth/mfa/status')).data })
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [setup, setSetup] = useState<Setup | null>(null)
  const [codes, setCodes] = useState<string[] | null>(null)
  const [mode, setMode] = useState<'idle' | 'disable' | 'regen'>('idle')

  const refresh = () => { qc.invalidateQueries({ queryKey: ['mfa', 'status'] }); void hydrate() }

  const start = useMutation({
    mutationFn: async () => (await api.post('/auth/mfa/setup', { password })).data as Setup,
    onSuccess: (d) => { setSetup(d); setPassword('') },
    onError: (e: any) => toast.error('Could not start setup', apiErrorMessage(e)),
  })
  const enable = useMutation({
    mutationFn: async () => (await api.post('/auth/mfa/enable', { code })).data,
    onSuccess: (d) => { onHold?.(true); setCodes(d.recovery_codes); setSetup(null); setCode(''); refresh(); onEnabled?.() },
    onError: (e: any) => toast.error('Code not accepted', apiErrorMessage(e)),
  })
  const disable = useMutation({
    mutationFn: async () => api.post('/auth/mfa/disable', { password, code }),
    onSuccess: () => { toast.success('Multi-factor authentication disabled'); setMode('idle'); setPassword(''); setCode(''); refresh() },
    onError: (e: any) => toast.error('Could not disable', apiErrorMessage(e)),
  })
  const regen = useMutation({
    mutationFn: async () => (await api.post('/auth/mfa/recovery-codes', { password, code })).data,
    onSuccess: (d) => { setCodes(d.recovery_codes); setMode('idle'); setPassword(''); setCode(''); refresh() },
    onError: (e: any) => toast.error('Could not regenerate', apiErrorMessage(e)),
  })

  function download(list: string[]) {
    const blob = new Blob([`ZenPlus recovery codes\n\n${list.join('\n')}\n\nEach code works once.\n`], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob); a.download = 'zenplus-recovery-codes.txt'; a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <QueryBoundary query={status} what="your sign-in security" skeleton={<div className="h-16 animate-pulse rounded bg-surface2/50" />}>
      {(s) => !s.supported ? (
        <p className="text-sm text-muted">Your account signs in through a directory; multi-factor authentication is managed there.</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {s.enabled ? <Badge variant="success"><ShieldCheck className="h-3 w-3" /> Enabled</Badge> : <Badge variant="outline"><ShieldOff className="h-3 w-3" /> Not enabled</Badge>}
            {s.required && <Badge variant="warning">Required by policy</Badge>}
            {s.enabled && <span className="text-xs text-muted">{s.recovery_codes_remaining} recovery codes left</span>}
          </div>

          {codes && (
            <div role="status" className="rounded-md border border-warning/40 bg-warning/5 p-3">
              <p className="mb-2 text-sm font-medium">Save these recovery codes now</p>
              <p className="mb-2 text-xs text-muted">Each code signs you in once if you lose your authenticator. They are not shown again.</p>
              <div className="grid grid-cols-2 gap-1 font-mono text-xs sm:grid-cols-3">{codes.map((c) => <span key={c}>{c}</span>)}</div>
              <div className="mt-2 flex gap-2">
                <Button type="button" size="sm" variant="outline" onClick={async () => { (await copyText(codes.join('\n'))) ? toast.success('Copied') : toast.error('Copy failed') }}><Copy className="h-3.5 w-3.5" /> Copy</Button>
                <Button type="button" size="sm" variant="outline" onClick={() => download(codes)}><Download className="h-3.5 w-3.5" /> Download</Button>
                <Button type="button" size="sm" onClick={() => { setCodes(null); onHold?.(false) }}>I have saved them</Button>
              </div>
            </div>
          )}

          {!s.enabled && !setup && (
            <form onSubmit={(e) => { e.preventDefault(); start.mutate() }} className="max-w-sm space-y-2">
              <p className="text-xs text-muted">Use an authenticator app (Microsoft Authenticator, Google Authenticator, 1Password, …). Confirm your password to begin.</p>
              <FormField label="Current password" required><PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></FormField>
              <Button type="submit" disabled={!password || start.isPending}>{start.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Set up authenticator</Button>
            </form>
          )}

          {setup && (
            <form onSubmit={(e) => { e.preventDefault(); enable.mutate() }} className="max-w-md space-y-3 rounded-md border border-border p-3">
              <p className="text-sm">Add this account in your authenticator app, then enter the 6-digit code it shows.</p>
              <div className="space-y-1 text-xs">
                <div className="text-muted">Setup key (type it in, or open the link on a phone)</div>
                <div className="flex items-center gap-2">
                  <code className="select-all break-all rounded bg-surface2 px-2 py-1 font-mono text-sm tracking-wider">{setup.secret.match(/.{1,4}/g)?.join(' ')}</code>
                  <Button type="button" size="icon" variant="ghost" className="h-7 w-7" aria-label="Copy setup key" onClick={async () => { (await copyText(setup.secret)) ? toast.success('Copied') : toast.error('Copy failed') }}><Copy className="h-3.5 w-3.5" /></Button>
                </div>
                <a className="text-primary underline" href={setup.otpauth_uri}>Open in authenticator app</a>
                <div className="text-muted">Account: {setup.account} · Issuer: {setup.issuer} · 6 digits · 30 s</div>
              </div>
              <FormField label="6-digit code" required>
                <Input inputMode="numeric" autoComplete="one-time-code" pattern="[0-9 ]*" maxLength={7} value={code} onChange={(e) => setCode(e.target.value)} className="w-40 font-mono tracking-widest" autoFocus />
              </FormField>
              <div className="flex gap-2">
                <Button type="submit" disabled={code.replace(/\D/g, '').length !== 6 || enable.isPending}>{enable.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Turn on</Button>
                <Button type="button" variant="outline" onClick={() => { setSetup(null); setCode('') }}>Cancel</Button>
              </div>
            </form>
          )}

          {s.enabled && mode === 'idle' && (
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" onClick={() => setMode('regen')}>New recovery codes</Button>
              {!s.required && <Button type="button" variant="outline" size="sm" onClick={() => setMode('disable')}>Turn off</Button>}
            </div>
          )}
          {s.enabled && mode !== 'idle' && (
            <form onSubmit={(e) => { e.preventDefault(); (mode === 'disable' ? disable : regen).mutate() }} className="max-w-sm space-y-2 rounded-md border border-border p-3">
              <p className="text-xs text-muted">{mode === 'disable' ? 'Confirm with your password and a current code to turn multi-factor authentication off.' : 'Confirm with your password and a current code. Your old recovery codes stop working.'}</p>
              <FormField label="Password" required><PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></FormField>
              <FormField label="Authenticator or recovery code" required><Input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" className="font-mono" /></FormField>
              <div className="flex gap-2">
                <Button type="submit" variant={mode === 'disable' ? 'destructive' : 'default'} disabled={!password || !code || disable.isPending || regen.isPending}>{mode === 'disable' ? 'Turn off' : 'Generate codes'}</Button>
                <Button type="button" variant="outline" onClick={() => { setMode('idle'); setPassword(''); setCode('') }}>Cancel</Button>
              </div>
            </form>
          )}
        </div>
      )}
    </QueryBoundary>
  )
}
