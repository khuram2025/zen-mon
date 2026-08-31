/**
 * Message preview for an alert rule — shows the actual HTML email (and the
 * plain-text / SMS bodies) the rule will send, and lets the templates behind
 * them be edited and saved without leaving the dialog.
 *
 * The preview is rendered server-side by POST /alert-rules/{id}/preview, which
 * accepts unsaved template overrides, so what is on screen always matches what
 * the alert engine would build for those templates.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Mail, MessageSquare, RotateCcw, Save, Type, Zap } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/Dialog'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'

// Ordered by how often a template needs them. The phrasing variables at the
// top render finished English ("Interface utilisation on core-router-01 rose
// above 80%"); the raw ones below are the rule's stored values.
const TEMPLATE_VARS = [
  '{event_sentence}', '{condition}', '{condition_sentence}', '{reading}',
  '{duration}', '{duration_sentence}', '{duration_suffix}',
  '{hostname}', '{ip_address}', '{status}', '{severity}', '{rule_name}',
  '{group}', '{location}', '{device_type}', '{timestamp}',
  '{metric_label}', '{threshold_value}', '{metric}', '{operator}', '{threshold}',
  '{rtt}', '{packet_loss}',
]

type Edits = {
  email_subject: string
  email_body: string
  sms_template: string
  recovery_email_subject: string
  recovery_email_body: string
  recovery_sms_template: string
}

const EMPTY: Edits = {
  email_subject: '',
  email_body: '',
  sms_template: '',
  recovery_email_subject: '',
  recovery_email_body: '',
  recovery_sms_template: '',
}

/** The rule's message text as editor state: what it actually sends.
 *
 * Reads `effective_templates` (resolved server-side) rather than the stored
 * columns, which are NULL on any rule using the built-in wording — that left
 * every field blank behind a grey placeholder, so the text an operator wanted
 * to adjust was never actually on screen to adjust. */
function editsFromRule(rule: any): Edits {
  const eff = rule?.effective_templates || rule || {}
  return {
    email_subject: eff.email_subject || '',
    email_body: eff.email_body || '',
    sms_template: eff.sms_template || '',
    recovery_email_subject: eff.recovery_email_subject || '',
    recovery_email_body: eff.recovery_email_body || '',
    recovery_sms_template: eff.recovery_sms_template || '',
  }
}

function defaultsFromRule(rule: any): Partial<Edits> {
  return rule?.default_templates || {}
}

export function AlertMessagePreviewDialog({
  open,
  onOpenChange,
  rule,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  rule: any
}) {
  const qc = useQueryClient()
  const [edits, setEdits] = useState<Edits>(EMPTY)
  const [data, setData] = useState<any>(null)
  const [mode, setMode] = useState<'alert' | 'recovery'>('alert')
  const [view, setView] = useState<'email' | 'text' | 'sms'>('email')
  const lastFocused = useRef<keyof Edits | null>(null)
  const fieldRefs = useRef<Partial<Record<keyof Edits, HTMLTextAreaElement | HTMLInputElement | null>>>({})

  const hasRecovery = !!rule?.recovery_alert
  const dirty = JSON.stringify(edits) !== JSON.stringify(editsFromRule(rule))

  const preview = useMutation({
    mutationFn: async (body: Edits) => (await api.post(`/alert-rules/${rule.id}/preview`, body)).data,
    onSuccess: (d) => setData(d),
    onError: (e: any) => toast.error('Preview failed', apiErrorMessage(e)),
  })

  const save = useMutation({
    mutationFn: async (body: Edits) =>
      // Blank means "use the built-in default" — send null so the column is
      // cleared rather than storing an empty template that renders nothing.
      (await api.put(`/alert-rules/${rule.id}`, Object.fromEntries(
        Object.entries(body).map(([k, v]) => [k, v.trim() ? v : null]),
      ))).data,
    onSuccess: () => {
      toast.success('Message templates saved')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const simulate = useMutation({
    mutationFn: async () => (await api.post(`/alert-rules/${rule.id}/simulate`)).data,
    onSuccess: (d: any) => {
      const details = (d.results || []).map((r: any) => `${r.channel}: ${r.status}`).join(', ')
      toast.success(d.message || 'Test notification sent', details)
    },
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  // Reset to the stored templates every time the dialog opens on a rule.
  useEffect(() => {
    if (!open || !rule?.id) return
    const next = editsFromRule(rule)
    setEdits(next)
    setData(null)
    setMode('alert')
    setView('email')
    preview.mutate(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, rule?.id])

  // Re-render as the templates are typed, without a request per keystroke.
  useEffect(() => {
    if (!open || !rule?.id) return
    const t = setTimeout(() => preview.mutate(edits), 500)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edits])

  if (!rule?.id) return null

  const msg = mode === 'recovery' ? data?.recovery : data?.alert
  const defaults: any = data?.templates?.defaults || defaultsFromRule(rule)
  const subjectKey = mode === 'recovery' ? 'recovery_email_subject' : 'email_subject'
  const bodyKey = mode === 'recovery' ? 'recovery_email_body' : 'email_body'
  const smsKey = mode === 'recovery' ? 'recovery_sms_template' : 'sms_template'

  const setField = (key: keyof Edits, value: string) => setEdits((e) => ({ ...e, [key]: value }))

  const insertVar = (v: string) => {
    const key = lastFocused.current || bodyKey
    const el = fieldRefs.current[key]
    const cur = edits[key]
    if (!el || typeof el.selectionStart !== 'number') {
      setField(key, `${cur}${v}`)
      return
    }
    const start = el.selectionStart ?? cur.length
    const end = el.selectionEnd ?? start
    const next = cur.slice(0, start) + v + cur.slice(end)
    setField(key, next)
    requestAnimationFrame(() => {
      el.focus()
      const pos = start + v.length
      el.setSelectionRange(pos, pos)
    })
  }

  // The field holds real text now, so "custom" means it differs from the
  // built-in wording rather than merely being non-empty.
  const isCustom = (key: keyof Edits) =>
    edits[key].trim() !== ((defaults[key] as string) || '').trim()

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[88vh] max-w-[1180px] flex-col gap-0 overflow-hidden p-0">
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div>
            <DialogTitle className="flex items-center gap-2">
              <Mail className="h-4 w-4 text-primary" />
              Message preview
            </DialogTitle>
            <p className="mt-1 text-xs text-muted">
              How <span className="font-medium text-text">{rule.name}</span> will look when it notifies.
              Sample device and readings — nothing is sent until you click Send test.
            </p>
          </div>
          <div className="flex items-center gap-2 pr-8">
            {hasRecovery && (
              <Tabs value={mode} onValueChange={(v) => setMode(v as any)}>
                <TabsList className="h-8">
                  <TabsTrigger value="alert" className="py-1 text-xs">Trigger</TabsTrigger>
                  <TabsTrigger value="recovery" className="py-1 text-xs">Recovery</TabsTrigger>
                </TabsList>
              </Tabs>
            )}
          </div>
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
          {/* Editor */}
          <div className="min-h-0 space-y-4 overflow-y-auto border-r border-border p-5">
            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <label className="text-xs font-medium text-muted">
                  {mode === 'recovery' ? 'Recovery subject' : 'Email subject'}
                </label>
                <FieldState custom={isCustom(subjectKey)}
                  onReset={() => setField(subjectKey, (defaults[subjectKey] as string) || '')} />
              </div>
              <Input
                ref={(el) => { fieldRefs.current[subjectKey] = el }}
                value={edits[subjectKey]}
                onFocus={() => { lastFocused.current = subjectKey }}
                onChange={(e) => setField(subjectKey, e.target.value)}
                className="font-mono text-xs"
              />
            </div>

            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <label className="text-xs font-medium text-muted">
                  {mode === 'recovery' ? 'Recovery message' : 'Email message'}
                </label>
                <FieldState custom={isCustom(bodyKey)}
                  onReset={() => setField(bodyKey, (defaults[bodyKey] as string) || '')} />
              </div>
              <Textarea
                ref={(el) => { fieldRefs.current[bodyKey] = el }}
                value={edits[bodyKey]}
                onFocus={() => { lastFocused.current = bodyKey }}
                onChange={(e) => setField(bodyKey, e.target.value)}
                rows={6}
                className="font-mono text-xs"
              />
              <p className="mt-1 text-[11px] text-muted">
                This is the sentence in the highlighted callout. The rule, severity, device and time
                are added automatically by the email layout — no need to repeat them here.
              </p>
            </div>

            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <label className="text-xs font-medium text-muted">
                  {mode === 'recovery' ? 'Recovery SMS' : 'SMS'}
                </label>
                <FieldState custom={isCustom(smsKey)}
                  onReset={() => setField(smsKey, (defaults[smsKey] as string) || '')} />
              </div>
              <Textarea
                ref={(el) => { fieldRefs.current[smsKey] = el }}
                value={edits[smsKey]}
                onFocus={() => { lastFocused.current = smsKey }}
                onChange={(e) => setField(smsKey, e.target.value)}
                rows={3}
                className="font-mono text-xs"
              />
            </div>

            <div className="rounded-lg border border-primary/20 bg-primary/5 p-3">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-primary">
                Variables — click to insert
              </p>
              <div className="mt-2 flex flex-wrap gap-1">
                {TEMPLATE_VARS.map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => insertVar(v)}
                    title={msg?.variables?.[v.slice(1, -1)] ? `Sample: ${msg.variables[v.slice(1, -1)]}` : undefined}
                    className="rounded bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted hover:text-primary"
                  >
                    {v}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Rendered preview */}
          <div className="flex min-h-0 flex-col bg-surface2/40">
            <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-5 py-3">
              <Tabs value={view} onValueChange={(v) => setView(v as any)}>
                <TabsList className="h-8">
                  <TabsTrigger value="email" className="gap-1.5 py-1 text-xs"><Mail className="h-3 w-3" />Email</TabsTrigger>
                  <TabsTrigger value="text" className="gap-1.5 py-1 text-xs"><Type className="h-3 w-3" />Plain text</TabsTrigger>
                  <TabsTrigger value="sms" className="gap-1.5 py-1 text-xs"><MessageSquare className="h-3 w-3" />SMS</TabsTrigger>
                </TabsList>
              </Tabs>
              {preview.isPending && (
                <span className="flex items-center gap-1.5 text-[11px] text-muted">
                  <Loader2 className="h-3 w-3 animate-spin" />Rendering
                </span>
              )}
            </div>

            <div className="min-h-0 flex-1 overflow-auto p-5">
              {!data && preview.isPending && (
                <div className="flex h-full items-center justify-center text-sm text-muted">
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />Building preview…
                </div>
              )}
              {data && !msg && (
                <div className="rounded-md border border-border bg-surface p-4 text-sm text-muted">
                  Recovery notifications are off for this rule — turn on &quot;Reset when trigger clears&quot;
                  in the rule to send and preview a recovery message.
                </div>
              )}
              {msg && view === 'email' && (
                <div className="mx-auto max-w-[720px]">
                  <div className="rounded-t-md border border-b-0 border-border bg-surface px-4 py-3">
                    <div className="text-[10px] uppercase tracking-wider text-muted">Subject</div>
                    <div className="mt-0.5 break-words text-sm font-medium">{msg.subject}</div>
                  </div>
                  {msg.email_html ? (
                    <iframe
                      title="Email preview"
                      sandbox=""
                      srcDoc={msg.email_html}
                      className="h-[calc(88vh-260px)] w-full rounded-b-md border border-border bg-white"
                    />
                  ) : (
                    // An API that predates rendered previews returns the body
                    // text only — say so instead of showing a blank frame.
                    <div className="rounded-b-md border border-border bg-surface p-4">
                      <div className="mb-3 rounded-md border border-warning/30 bg-warning/10 p-2.5 text-xs text-warning">
                        The API service is running an older build and returned no rendered email.
                        Restart it (<code className="font-mono">systemctl restart zenplus-api</code>) to
                        see the full HTML message; the body text below is what it did return.
                      </div>
                      <pre className="whitespace-pre-wrap font-mono text-xs text-muted">{msg.email_body}</pre>
                    </div>
                  )}
                </div>
              )}
              {msg && view === 'text' && (
                <div className="mx-auto max-w-[720px] rounded-md border border-border bg-surface p-4">
                  <div className="text-[10px] uppercase tracking-wider text-muted">Subject</div>
                  <div className="mb-3 mt-0.5 break-words text-sm font-medium">{msg.subject}</div>
                  <pre className="whitespace-pre-wrap font-mono text-xs text-muted">{msg.email_text}</pre>
                </div>
              )}
              {msg && view === 'sms' && (
                <div className="mx-auto max-w-sm">
                  <div className="rounded-2xl bg-primary px-4 py-3 text-sm text-white shadow-sm">
                    {msg.sms_body}
                  </div>
                  <div className="mt-2 text-center text-[11px] text-muted">
                    {(msg.sms_body || '').length} characters · {Math.max(1, Math.ceil((msg.sms_body || '').length / 160))} SMS
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={simulate.isPending}
              onClick={() => { if (confirm(`Send a test notification for "${rule.name}"?`)) simulate.mutate() }}>
              {simulate.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5 text-amber-500" />}
              Send test
            </Button>
            {dirty && <Badge variant="warning" className="text-[10px]">Unsaved changes</Badge>}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" disabled={!dirty}
              onClick={() => setEdits(editsFromRule(rule))}>
              <RotateCcw className="h-3.5 w-3.5" />Discard edits
            </Button>
            <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
            <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate(edits)}>
              {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              Save templates
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function FieldState({ custom, onReset }: { custom: boolean; onReset: () => void }) {
  return (
    <div className="flex items-center gap-2">
      <span className={cn('text-[10px] uppercase tracking-wider', custom ? 'text-primary' : 'text-muted')}>
        {custom ? 'Custom' : 'Default'}
      </span>
      {custom && (
        <button type="button" className="text-[10px] text-muted underline-offset-2 hover:text-primary hover:underline"
          onClick={onReset}>
          Reset to default
        </button>
      )}
    </div>
  )
}
