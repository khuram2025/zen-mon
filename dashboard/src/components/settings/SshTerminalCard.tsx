import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { Terminal as TerminalIcon, Loader2, PlugZap, Power } from 'lucide-react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { api, getApiToken } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'

const BASE = '/system/ssh-terminal/sessions'
type Phase = 'idle' | 'connecting' | 'open' | 'ended'

const END_TEXT: Record<string, string> = {
  user_closed: 'Session closed.',
  remote_closed: 'The device closed the session.',
  idle_timeout: 'Session closed after 10 minutes without input.',
  max_duration: 'Session closed at the 60-minute limit.',
  lost: 'The terminal service lost this session.',
}

const toBase64 = (text: string) => {
  const bytes = new TextEncoder().encode(text)
  let bin = ''
  bytes.forEach((b) => { bin += String.fromCharCode(b) })
  return btoa(bin)
}
const fromBase64 = (b64: string) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))

/**
 * Interactive SSH shell for testing reachability and logins from the appliance.
 * Credentials are sent once to open the session and are never stored. The
 * device host key is not checked.
 */
export function SshTerminalCard() {
  const [form, setForm] = useState({ host: '', port: '22', username: '', password: '' })
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const screen = useRef<HTMLDivElement>(null)
  const term = useRef<Terminal | null>(null)
  const fit = useRef<FitAddon | null>(null)
  const sessionId = useRef<string | null>(null)
  const abort = useRef<AbortController | null>(null)
  const pending = useRef('')
  const flushTimer = useRef<number | null>(null)
  const chain = useRef<Promise<unknown>>(Promise.resolve())

  const send = useCallback((path: string, body: unknown) => {
    const id = sessionId.current
    if (!id) return
    // Serialized so keystrokes always arrive in order.
    chain.current = chain.current.then(() => api.post(`${BASE}/${id}/${path}`, body)).catch(() => undefined)
  }, [])

  const flush = useCallback(() => {
    flushTimer.current = null
    if (!pending.current) return
    send('input', { data: toBase64(pending.current) })
    pending.current = ''
  }, [send])

  const teardown = useCallback((close: boolean) => {
    abort.current?.abort()
    abort.current = null
    if (flushTimer.current) window.clearTimeout(flushTimer.current)
    flushTimer.current = null
    pending.current = ''
    const id = sessionId.current
    sessionId.current = null
    if (close && id) api.delete(`${BASE}/${id}`).catch(() => undefined)
  }, [])

  useEffect(() => () => { teardown(true); term.current?.dispose(); term.current = null }, [teardown])

  const readStream = useCallback(async (id: string) => {
    let after = '0'
    let retries = 0
    const controller = new AbortController()
    abort.current = controller
    while (!controller.signal.aborted && sessionId.current === id) {
      try {
        const res = await fetch(`/api/v1${BASE}/${id}/stream?after=${encodeURIComponent(after)}`, {
          headers: { Authorization: `Bearer ${getApiToken() ?? ''}` }, signal: controller.signal,
        })
        if (!res.ok || !res.body) throw new Error(String(res.status))
        retries = 0
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let nl: number
          while ((nl = buffer.indexOf('\n')) >= 0) {
            const line = buffer.slice(0, nl).trim()
            buffer = buffer.slice(nl + 1)
            if (!line) continue
            const msg = JSON.parse(line)
            if (msg.id) after = msg.id
            if (msg.d) term.current?.write(fromBase64(msg.d))
            if (msg.e === 'closed') {
              sessionId.current = null
              setStatus(END_TEXT[msg.r] ?? 'Session closed.')
              setPhase('ended')
              return
            }
          }
        }
      } catch {
        if (controller.signal.aborted) return
        if (++retries > 5) {
          sessionId.current = null
          setStatus('Connection to the appliance was lost.')
          setPhase('ended')
          return
        }
        await new Promise((r) => setTimeout(r, 1000 * retries))
      }
    }
  }, [])

  const connect = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setStatus('')
    setPhase('connecting')
    // The terminal element exists only while connecting/open; wait for it.
    await new Promise((r) => requestAnimationFrame(() => r(null)))
    if (!screen.current) return
    term.current?.dispose()
    const t = new Terminal({
      cursorBlink: true, fontSize: 13, scrollback: 5000, convertEol: false,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      theme: { background: '#0b0f17', foreground: '#e5e7eb' },
    })
    const f = new FitAddon()
    t.loadAddon(f)
    t.open(screen.current)
    f.fit()
    term.current = t
    fit.current = f
    t.onData((d) => {
      pending.current += d
      if (!flushTimer.current) flushTimer.current = window.setTimeout(flush, 15)
    })
    let resizeTimer: number | undefined
    t.onResize(({ cols, rows }) => {
      window.clearTimeout(resizeTimer)
      resizeTimer = window.setTimeout(() => send('resize', { cols, rows }), 150)
    })
    t.writeln(`Connecting to ${form.username}@${form.host}:${form.port} …`)
    try {
      const res = await api.post(BASE, {
        host: form.host.trim(), port: Number(form.port) || 22, username: form.username,
        password: form.password, cols: Math.max(t.cols, 20), rows: Math.max(t.rows, 5),
      })
      sessionId.current = res.data.session_id
      setForm((s) => ({ ...s, password: '' }))
      setPhase('open')
      t.focus()
      void readStream(res.data.session_id)
    } catch (err) {
      t.dispose()
      term.current = null
      setPhase('idle')
      setError(apiErrorMessage(err))
    }
  }

  useEffect(() => {
    const el = screen.current
    if (!el || phase !== 'open') return
    const ro = new ResizeObserver(() => { try { fit.current?.fit() } catch { /* hidden */ } })
    ro.observe(el)
    return () => ro.disconnect()
  }, [phase])

  const disconnect = () => {
    teardown(true)
    setStatus(END_TEXT.user_closed)
    setPhase('ended')
  }

  const active = phase === 'connecting' || phase === 'open'

  return (
    <Card>
      <CardHeader><CardTitle className="flex items-center gap-2"><TerminalIcon className="h-4 w-4" /> SSH terminal</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted">
          Open an SSH shell from this appliance to a device to test reachability, credentials and commands.
          The username and password are used once to sign in and are not stored. The device&apos;s host key is not checked.
          Sessions close after 10 minutes without input and are recorded in the audit log (who, which host and account — never the password).
        </p>
        {!active && (
          <form onSubmit={connect} className="grid grid-cols-1 gap-3 md:grid-cols-4" autoComplete="off">
            <FormField label="Host or IP address">
              <Input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="10.0.0.1" required autoComplete="off" />
            </FormField>
            <FormField label="Port">
              <Input type="number" min={1} max={65535} value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} required />
            </FormField>
            <FormField label="Username">
              <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required autoComplete="off" />
            </FormField>
            <FormField label="Password">
              <Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required autoComplete="new-password" />
            </FormField>
            <div className="flex items-center gap-3 md:col-span-4">
              <Button type="submit"><PlugZap className="h-4 w-4" /> Connect</Button>
              {error && <span role="alert" className="text-sm text-danger">{error}</span>}
              {phase === 'ended' && status && <span className="text-sm text-muted">{status}</span>}
            </div>
          </form>
        )}
        {(active || phase === 'ended') && (
          <div className="space-y-2">
            {active && (
              <div className="flex items-center justify-between gap-3">
                <span className="flex items-center gap-2 text-sm text-muted">
                  {phase === 'connecting' ? <Loader2 className="h-4 w-4 animate-spin" /> : <span className="h-2 w-2 rounded-full bg-success" />}
                  {form.username}@{form.host}:{form.port}
                </span>
                <Button variant="outline" size="sm" onClick={disconnect}><Power className="h-4 w-4" /> Disconnect</Button>
              </div>
            )}
            <div ref={screen} className="h-[420px] w-full overflow-hidden rounded-md border border-border bg-[#0b0f17] p-2" aria-label="SSH terminal" />
          </div>
        )}
      </CardContent>
    </Card>
  )
}
