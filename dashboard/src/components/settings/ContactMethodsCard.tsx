/**
 * My Profile → how escalation reaches me: email / SMS contact methods and my
 * personal time zone (GET/PUT /users/me/contact-methods). The PUT replaces the
 * whole list; the time zone is only changed when it is sent.
 */
import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Mail, MessageSquare, Plus, Save, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { TimezoneSelect } from '@/features/reports/schedules/TimezoneSelect'
import { useMyContactMethods, useOrgTimezone } from '@/features/escalation/api'
import { contactsPayload, sameContact, validateContact } from '@/features/escalation/model'
import type { ContactKind, ContactMethod } from '@/features/escalation/types'

const KEY = ['users', 'me', 'contact-methods']

export function ContactMethodsCard() {
  const qc = useQueryClient()
  const q = useMyContactMethods()
  const orgTz = useOrgTimezone()
  const [kind, setKind] = useState<ContactKind>('email')
  const [address, setAddress] = useState('')
  const [label, setLabel] = useState('')
  const [touched, setTouched] = useState(false)
  const [tz, setTz] = useState<string | null>(null)

  const save = useMutation({
    mutationFn: async (body: { methods: ContactMethod[]; timezone?: string | null }) =>
      (await api.put('/users/me/contact-methods', { methods: contactsPayload(body.methods), ...('timezone' in body ? { timezone: body.timezone } : {}) })).data,
    onSuccess: (_d, body) => {
      toast.success('timezone' in body ? 'Time zone saved' : 'Contact methods saved')
      qc.invalidateQueries({ queryKey: KEY })
      qc.invalidateQueries({ queryKey: ['escalation', 'coverage'] })
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (q.isLoading) return <Card><CardContent className="p-5"><Skeleton className="h-24 w-full" /></CardContent></Card>
  if (q.isError && !q.data) {
    return (
      <Card>
        <CardHeader><CardTitle>Contact methods</CardTitle></CardHeader>
        <CardContent><QueryFailure error={q.error} what="your contact methods" onRetry={() => void q.refetch()} retrying={q.isFetching} /></CardContent>
      </Card>
    )
  }
  const data = q.data!
  const methods = data.methods
  const error = validateContact(kind, address) ?? (methods.some((m) => sameContact(m, { kind, address })) ? 'Already listed' : null)
  const enabledEmail = methods.some((m) => m.kind === 'email' && m.enabled)
  const busy = save.isPending
  const zone = tz ?? data.timezone ?? ''

  const add = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (error) return
    save.mutate({ methods: [...methods, { kind, address: address.trim(), label: label.trim() || null, enabled: true }] }, {
      onSuccess: () => { setAddress(''); setLabel(''); setTouched(false) },
    })
  }

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle>On-call contact</CardTitle>
        <p className="text-xs text-muted">How escalation steps reach you when you are on call or named in a policy.</p>
      </CardHeader>
      <CardContent className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="space-y-3">
          {methods.length === 0 ? (
            <p className="rounded-md border border-dashed border-border p-3 text-sm text-muted">No contact methods yet.</p>
          ) : (
            <ul className="divide-y divide-border rounded-md border border-border">
              {methods.map((m, i) => {
                const Icon = m.kind === 'sms' ? MessageSquare : Mail
                return (
                  <li key={m.id ?? `${m.kind}-${m.address}`} className={cn('flex flex-wrap items-center gap-2 px-3 py-2 text-sm', !m.enabled && 'opacity-60')}>
                    <Icon className="h-4 w-4 shrink-0 text-muted" aria-hidden />
                    <span className="min-w-0 break-all font-medium">{m.address}</span>
                    {m.label && <span className="text-xs text-muted">{m.label}</span>}
                    <span className="ml-auto flex items-center gap-2">
                      <Switch
                        checked={m.enabled}
                        disabled={busy}
                        onCheckedChange={(v) => save.mutate({ methods: methods.map((x, k) => (k === i ? { ...x, enabled: v } : x)) })}
                        aria-label={`${m.enabled ? 'Disable' : 'Enable'} ${m.kind === 'sms' ? 'SMS' : 'email'} ${m.address}`}
                      />
                      <Button size="icon" variant="ghost" className="h-7 w-7 text-muted hover:text-danger" disabled={busy}
                        onClick={() => save.mutate({ methods: methods.filter((_, k) => k !== i) })}
                        aria-label={`Remove ${m.kind === 'sms' ? 'SMS' : 'email'} ${m.address}`}>
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
          {!enabledEmail && (
            <p className="text-xs text-muted">
              With no enabled email method, email pages go to your account address{data.account_email ? <> <span className="font-medium text-text">{data.account_email}</span></> : ''}.
            </p>
          )}
          <form onSubmit={add} className="grid gap-2 rounded-md border border-border bg-surface2/30 p-3 sm:grid-cols-[110px_minmax(0,1fr)_160px_auto]">
            <select aria-label="Contact method type" value={kind} onChange={(e) => setKind(e.target.value as ContactKind)} className="h-9 rounded-md border border-border bg-surface px-2 text-sm">
              <option value="email">Email</option>
              <option value="sms">SMS</option>
            </select>
            <div>
              <Input
                aria-label={kind === 'email' ? 'Email address' : 'Phone number'}
                aria-invalid={touched && !!error ? true : undefined}
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                placeholder={kind === 'email' ? 'name@example.com' : '+966 50 123 4567'}
                type={kind === 'email' ? 'email' : 'tel'}
                maxLength={255}
              />
              {touched && error && <p className="mt-1 text-[11px] text-danger">{error}</p>}
            </div>
            <Input aria-label="Label (optional)" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Label, e.g. Work mobile" maxLength={100} />
            <Button type="submit" size="sm" className="h-9" disabled={busy || (touched && !!error)}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Plus className="h-3.5 w-3.5" aria-hidden />} Add
            </Button>
          </form>
          <p className="text-[11px] text-muted">SMS uses the default SMS gateway. The same address listed by several targets is sent once per step.</p>
        </div>
        <div className="space-y-2">
          <div className="text-xs font-medium uppercase tracking-wider text-muted">Your time zone</div>
          <TimezoneSelect value={zone} orgTz={orgTz} at={new Date()} onChange={setTz} />
          <p className="text-[11px] text-muted">Used to show your on-call shifts and notification times. Audit records stay in UTC.</p>
          <div className="flex justify-end">
            <Button size="sm" disabled={busy || tz == null || tz === (data.timezone ?? '')} onClick={() => save.mutate({ methods, timezone: tz || null }, { onSuccess: () => setTz(null) })}>
              <Save className="h-3.5 w-3.5" aria-hidden /> Save time zone
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
