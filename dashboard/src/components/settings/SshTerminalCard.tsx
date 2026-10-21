import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { Terminal as TerminalIcon, Loader2, PlugZap, Power, ShieldAlert, ShieldQuestion } from 'lucide-react'
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
type HostKeyPrompt = {
  kind: 'host_key_unknown' | 'host_key_changed'
  host: string
  port: number
  key_type: string
  fingerprint: string
  previous_fingerprint?: string
}

const END_TEXT: Record<string, string> = {
  user_closed: 'Session closed.',
  remote_closed: 'The device closed the session.',
  idle_timeout: 'Session closed after 10 minutes without input.',
  max_duration: 'Session closed at the 60-minute limit.',
  lost: 'The terminal service lost this session.',
}

const DIM = '\x1b[90m'
const RESET = '\x1b[0m'
const IDLE_BANNER = `${DIM}Not connected. Enter a host, username and password above and click Connect.${RESET}`

const toBase64 = (text: string) => {
  const bytes = new TextEncoder().encode(text)
  let bin = ''
  bytes.forEach((b) => { bin += String.fromCharCode(b) })
  return btoa(bin)
}
const fromBase64 = (b64: string) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))

/**
 * PuTTY-style SSH shell for testing reachability and logins from the appliance.
 * Credentials are sent once to open the session and are never stored. The
 * device host key is confirmed on first use: the API returns the fingerprint of
 * an unknown key and the password is only sent once the operator trusts it.
 */
export function SshTerminalCard() {
  const [form, setForm] = useState({ host: '', port: '22', username: '', password: '' })
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState('')
  const [hostKey, setHostKey] = useState<HostKeyPrompt | null>(null)
  const [forgetting, setForgetting] = useState(false)
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

  // The terminal window exists from the start, like an empty PuTTY window.
  useEffect(() => {
    if (!screen.current) return
    const t = new Terminal({
      cursorBlink: true, cursorStyle: 'block', fontSize: 14, scrollback: 5000,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace',
      theme: { background: '#000000', foreground: '#bbbbbb', cursor: '#bbbbbb', selectionBackground: '#3b5ba5' },
    })
    const f = new FitAddon()
    t.loadAddon(f)
    t.open(screen.current)
    t.writeln(IDLE_BANNER)
    term.current = t
    fit.current = f
    const fitNow = () => { try { f.fit() } catch { /* hidden tab */ } }
    fitNow()
    const data = t.onData((d) => {
      if (!sessionId.current) return
      pending.current += d
      if (!flushTimer.current) flushTimer.current = window.setTimeout(flush, 15)
    })
    let resizeTimer: number | undefined
    const resize = t.onResize(({ cols, rows }) => {
      window.clearTimeout(resizeTimer)
      resizeTimer = window.setTimeout(() => send('resize', { cols, rows }), 150)
    })
    const ro = new ResizeObserver(fitNow)
    ro.observe(screen.current)
    return () => {
      ro.disconnect(); data.dispose(); resize.dispose()
      window.clearTimeout(resizeTimer)
      teardown(true)
      t.dispose()
      term.current = null
    }
  }, [flush, send, teardown])

  const finish = useCallback((reason: string) => {
    sessionId.current = null
    term.current?.writeln(`\r\n${DIM}[${END_TEXT[reason] ?? 'Session closed.'}]${RESET}`)
    setPhase('ended')
  }, [])

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
            if (msg.e === 'closed') { finish(msg.r); return }
          }
        }
      } catch {
        if (controller.signal.aborted) return
        if (++retries > 5) { finish('lost'); return }
        await new Promise((r) => setTimeout(r, 1000 * retries))
      }
    }
  }, [finish])

  const openSession = async (trustFingerprint?: string) => {
    const t = term.current
    if (!t) return
    setError('')
    setHostKey(null)
    setPhase('connecting')
    t.reset()
    t.writeln(`${DIM}Connecting to ${form.username}@${form.host}:${form.port} …${RESET}`)
    fit.current?.fit()
    const host = form.host.trim()
    const port = Number(form.port) || 22
    try {
      const res = await api.post(BASE, {
        host, port, username: form.username,
        password: form.password, cols: Math.max(t.cols, 20), rows: Math.max(t.rows, 5),
        ...(trustFingerprint ? { trust_fingerprint: trustFingerprint } : {}),
      })
      sessionId.current = res.data.session_id
      setForm((s) => ({ ...s, password: '' }))
      setPhase('open')
      t.reset()
      t.focus()
      void readStream(res.data.session_id)
    } catch (err: any) {
      const detail = err?.response?.status === 409 ? err.response.data?.detail : null
      if (detail?.code === 'host_key_unknown' || detail?.code === 'host_key_changed') {
        // The password was not sent: the host key must be trusted first.
        setHostKey({ kind: detail.code, host, port, key_type: detail.key_type, fingerprint: detail.fingerprint,
                     previous_fingerprint: detail.previous_fingerprint })
        t.writeln(`\x1b[33m${apiErrorMessage(err)}${RESET}`)
        setPhase('idle')
        return
      }
      const message = apiErrorMessage(err)
      t.writeln(`\x1b[31m${message}${RESET}`)
      setError(message)
      setPhase('idle')
    }
  }

  const connect = (e: FormEvent) => {
    e.preventDefault()
    void openSession()
  }

  const forgetKey = async () => {
    if (!hostKey) return
    if (!window.confirm(`Forget the stored host key for ${hostKey.host}:${hostKey.port}? Only do this when you know the device was replaced or re-keyed.`)) return
    setForgetting(true)
    try {
      await api.delete('/system/ssh-terminal/host-keys', { data: { host: hostKey.host, port: hostKey.port } })
      setHostKey(null)
      term.current?.writeln(`${DIM}Stored host key forgotten. Connect again to review the new fingerprint.${RESET}`)
    } catch (err) {
      setError(apiErrorMessage(err))
    } finally {
      setForgetting(false)
    }
  }

  const disconnect = () => {
    teardown(true)
    finish('user_closed')
  }

  const active = phase === 'connecting' || phase === 'open'

  return (
    <Card>
      <CardHeader><CardTitle className="flex items-center gap-2"><TerminalIcon className="h-4 w-4" /> SSH terminal</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted">
          Open an SSH shell from this appliance to a device to test reachability, credentials and commands.
          The username and password are used once to sign in and are not stored. The first connection to a device shows its
          host key fingerprint for you to confirm; a key that later changes is refused.
          Sessions close after 10 minutes without input and are recorded in the audit log (who, which host and account — never the password).
        </p>
        <form onSubmit={connect} className="grid grid-cols-1 items-end gap-3 md:grid-cols-[2fr_1fr_1.5fr_1.5fr_auto]" autoComplete="off">
          <FormField label="Host or IP address">
            <Input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} placeholder="10.0.0.1" required disabled={active} autoComplete="off" />
          </FormField>
          <FormField label="Port">
            <Input type="number" min={1} max={65535} value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} required disabled={active} />
          </FormField>
          <FormField label="Username">
            <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required disabled={active} autoComplete="off" />
          </FormField>
          <FormField label="Password">
            <Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required disabled={active} autoComplete="new-password" />
          </FormField>
          {active ? (
            <Button type="button" variant="outline" onClick={disconnect}><Power className="h-4 w-4" /> Disconnect</Button>
          ) : (
            <Button type="submit"><PlugZap className="h-4 w-4" /> Connect</Button>
          )}
        </form>
        {hostKey?.kind === 'host_key_unknown' && (
          <div role="alert" className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
            <p className="flex items-center gap-2 font-medium"><ShieldQuestion className="h-4 w-4" /> Confirm the host key of {hostKey.host}:{hostKey.port}</p>
            <p className="text-muted">
              This appliance has not connected to this host before. Compare the fingerprint below with the one the device
              reports (for example <code>show ip ssh</code> / <code>show crypto key mypubkey</code>, or{' '}
              <code>ssh-keygen -lf</code> on the host). Your password is only sent after you trust it.
            </p>
            <p className="break-all font-mono text-xs">{hostKey.key_type} {hostKey.fingerprint}</p>
            <div className="flex gap-2">
              <Button type="button" onClick={() => void openSession(hostKey.fingerprint)}>Trust and connect</Button>
              <Button type="button" variant="outline" onClick={() => setHostKey(null)}>Cancel</Button>
            </div>
          </div>
        )}
        {hostKey?.kind === 'host_key_changed' && (
          <div role="alert" className="space-y-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm">
            <p className="flex items-center gap-2 font-medium text-danger"><ShieldAlert className="h-4 w-4" /> The host key of {hostKey.host}:{hostKey.port} has changed</p>
            <p className="text-muted">
              Another device may be answering at this address. The connection was stopped and your password was not sent.
              If the device was replaced or its SSH key regenerated, forget the stored key and confirm the new one.
            </p>
            <p className="break-all font-mono text-xs">Trusted: {hostKey.key_type} {hostKey.previous_fingerprint}</p>
            <p className="break-all font-mono text-xs">Presented: {hostKey.key_type} {hostKey.fingerprint}</p>
            <div className="flex gap-2">
              <Button type="button" variant="outline" onClick={() => void forgetKey()} disabled={forgetting}>Forget stored key</Button>
              <Button type="button" variant="outline" onClick={() => setHostKey(null)}>Close</Button>
            </div>
          </div>
        )}
        <div className="flex items-center gap-2 text-xs text-muted" aria-live="polite">
          {phase === 'connecting' && <><Loader2 className="h-3.5 w-3.5 animate-spin" /> Connecting…</>}
          {phase === 'open' && <><span className="h-2 w-2 rounded-full bg-success" /> Connected to {form.username}@{form.host}:{form.port}</>}
          {phase === 'idle' && !error && 'Not connected'}
          {phase === 'ended' && 'Disconnected'}
          {error && <span role="alert" className="text-danger">{error}</span>}
        </div>
        <div ref={screen} className="h-[480px] w-full overflow-hidden rounded-md border border-border bg-black p-2" aria-label="SSH terminal" />
      </CardContent>
    </Card>
  )
}
