import { FormEvent, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Boxes, KeyRound, Loader2 } from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Card, CardContent } from '@/components/ui/Card'

export function LoginPage() {
  const { token, login, completeMfa, completeSso, loading, error } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [challenge, setChallenge] = useState<string | null>(null)
  const [code, setCode] = useState('')

  const [sso, setSso] = useState<{ enabled: boolean; display_name: string | null } | null>(null)
  const [ssoError, setSsoError] = useState<string | null>(null)

  useEffect(() => {
    if (token) navigate('/', { replace: true })
  }, [token, navigate])

  // Single sign-on: show the button when configured, finish a redirect that
  // just came back (?sso=<one-time code>), and surface a refusal (?sso_error=).
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const code = params.get('sso')
    const err = params.get('sso_error')
    if (code || err) window.history.replaceState({}, '', '/login')
    if (err) setSsoError(err)
    if (code) void completeSso(code).then(() => navigate('/', { replace: true })).catch(() => {})
    fetch('/api/v1/auth/oidc/config').then((r) => (r.ok ? r.json() : null)).then(setSso).catch(() => setSso(null))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    try {
      if (challenge) {
        await completeMfa(challenge, code.trim())
        navigate('/', { replace: true })
        return
      }
      const outcome = await login(username, password)
      if (outcome.status === 'mfa') {
        setChallenge(outcome.challenge)
        setPassword('')
        return
      }
      navigate('/', { replace: true })
    } catch (err: any) {
      // A spent or expired challenge cannot be retried: start over.
      if (challenge && err?.response?.status === 401 && /expired|start again/i.test(String(err?.response?.data?.detail || ''))) {
        setChallenge(null)
        setCode('')
      }
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <Boxes className="h-6 w-6" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">ZenPlus</h1>
            <p className="text-sm text-muted">Network Monitoring System</p>
          </div>
        </div>

        <Card>
          <CardContent className="pt-6">
            <form onSubmit={onSubmit} className="space-y-4">
              {!challenge ? (
                <>
                  <div className="space-y-2">
                    <label htmlFor="username" className="text-xs font-medium uppercase tracking-wider text-muted">
                      Username
                    </label>
                    <Input
                      id="username"
                      autoComplete="username"
                      required
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                    />
                  </div>
                  <div className="space-y-2">
                    <label htmlFor="password" className="text-xs font-medium uppercase tracking-wider text-muted">
                      Password
                    </label>
                    <Input
                      id="password"
                      type="password"
                      autoComplete="current-password"
                      required
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                    />
                  </div>
                </>
              ) : (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 text-sm font-medium"><KeyRound className="h-4 w-4 text-primary" /> Two-step verification</div>
                  <p className="text-xs text-muted">Enter the 6-digit code from your authenticator app, or one of your recovery codes.</p>
                  <label htmlFor="otp" className="text-xs font-medium uppercase tracking-wider text-muted">Verification code</label>
                  <Input
                    id="otp"
                    inputMode="text"
                    autoComplete="one-time-code"
                    autoFocus
                    required
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    className="font-mono tracking-widest"
                  />
                </div>
              )}
              {ssoError && (
                <div role="alert" className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
                  Single sign-on failed: {ssoError}
                </div>
              )}
              {error && (
                <div role="alert" className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
                  {error}
                </div>
              )}
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                {challenge ? 'Verify' : 'Sign In'}
              </Button>
              {!challenge && sso?.enabled && (
                <>
                  <div className="relative py-1 text-center text-[11px] uppercase tracking-wider text-muted before:absolute before:left-0 before:top-1/2 before:h-px before:w-[40%] before:bg-border after:absolute after:right-0 after:top-1/2 after:h-px after:w-[40%] after:bg-border">or</div>
                  <Button type="button" variant="outline" className="w-full" onClick={() => { window.location.href = '/api/v1/auth/oidc/login' }}>
                    <KeyRound className="h-4 w-4" /> {sso.display_name || 'Single sign-on'}
                  </Button>
                </>
              )}
              {challenge && (
                <Button type="button" variant="link" className="w-full" onClick={() => { setChallenge(null); setCode('') }}>
                  Use a different account
                </Button>
              )}
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
