import { FormEvent, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'

interface TrustCertificate {
  id: string; label: string; subject: string; issuer: string
  hosts: string[]; is_ca: boolean; not_after: string
}
interface ProbeTrust { auto_fetch_intermediates: boolean; certificates: TrustCertificate[] }

export function ProbeTrustCard() {
  const queryClient = useQueryClient()
  const [label, setLabel] = useState('')
  const [hosts, setHosts] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const key = ['security', 'probe-trust']
  const trust = useQuery<ProbeTrust>({ queryKey: key, queryFn: async () => (await api.get('/system/security/probe-trust')).data })
  const changed = (data: ProbeTrust) => { queryClient.setQueryData(key, data); toast.success('Service probe trust saved. Probes use it after their next configuration refresh.') }
  const failed = (error: unknown) => toast.error(apiErrorMessage(error))
  const toggle = useMutation({ mutationFn: async (enabled: boolean) => (await api.put('/system/security/probe-trust', { auto_fetch_intermediates: enabled })).data, onSuccess: changed, onError: failed })
  const upload = useMutation({
    mutationFn: async () => {
      const body = new FormData()
      body.append('certificate', file!); body.append('label', label); body.append('hosts', hosts)
      return (await api.post('/system/security/probe-trust/certificates', body, { headers: { 'Content-Type': 'multipart/form-data' } })).data
    },
    onSuccess: (data: ProbeTrust) => { changed(data); setFile(null); setLabel(''); setHosts(''); if (input.current) input.current.value = '' }, onError: failed,
  })
  const remove = useMutation({ mutationFn: async (id: string) => (await api.delete(`/system/security/probe-trust/certificates/${id}`)).data,
    onSuccess: (data: ProbeTrust) => { changed(data); setRemoving(null) }, onError: failed })
  const submit = (event: FormEvent) => { event.preventDefault(); if (file) upload.mutate() }

  return <Card>
    <CardHeader><CardTitle className="flex items-center gap-2"><ShieldCheck className="h-5 w-5" /> Service probe trust</CardTitle></CardHeader>
    <CardContent className="space-y-5">
      <p className="text-sm text-muted">Trust certificates used by monitored HTTPS services. These settings apply to central HTTP and TLS probes and compatible updated sensors.</p>
      {trust.isLoading && <p className="text-sm text-muted">Loading probe trust…</p>}
      {trust.isError && <p className="text-sm text-danger">{apiErrorMessage(trust.error)}</p>}
      {trust.data && <>
        <div className="flex items-start justify-between gap-4 rounded-lg border border-border p-4">
          <div><label htmlFor="probe-aia" className="text-sm font-medium">Recover missing intermediate certificates automatically</label>
            <p className="mt-1 text-sm text-muted">Fetch missing issuers from public certificate URLs. Certificate names, validity dates and the chain to a trusted root are still verified. Internal issuer URLs require a manual CA upload.</p></div>
          <Switch id="probe-aia" checked={trust.data.auto_fetch_intermediates} disabled={toggle.isPending} onCheckedChange={value => toggle.mutate(value)} />
        </div>
        <form onSubmit={submit} className="space-y-3">
          <h3 className="text-sm font-medium">Add a trusted certificate</h3>
          <p className="text-sm text-muted">Upload your internal CA’s public certificate, or a self-signed service certificate. A self-signed certificate requires exact host scopes matching its subject alternative names. An unscoped CA is trusted for all monitored services.</p>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="space-y-1 text-sm">Label<Input aria-label="Certificate label" maxLength={100} value={label} onChange={e => setLabel(e.target.value)} placeholder="Corporate monitoring CA" /></label>
            <label className="space-y-1 text-sm">Host scopes<Input aria-label="Certificate host scopes" maxLength={8192} value={hosts} onChange={e => setHosts(e.target.value)} placeholder="portal.example.local, 192.168.8.10" /></label>
          </div>
          <label className="block space-y-1 text-sm">Public certificate (.pem, .crt or .cer)
            <Input ref={input} aria-label="Public probe certificate" type="file" accept=".pem,.crt,.cer" onChange={e => { const next = e.target.files?.[0] || null; if (next && next.size > 65536) { toast.error('Certificate must be at most 64 KiB'); e.target.value = ''; setFile(null) } else setFile(next) }} />
          </label>
          <p className="text-xs text-muted">PEM or DER, one certificate per upload. Do not include a private key. Trust applies after the next probe configuration refresh.</p>
          <Button type="submit" disabled={!file || upload.isPending}>{upload.isPending ? 'Installing…' : 'Install certificate'}</Button>
        </form>
        <div className="space-y-3">
          <h3 className="text-sm font-medium">Installed certificates ({trust.data.certificates.length}/32)</h3>
          {trust.data.certificates.length === 0 && <p className="text-sm text-muted">No additional certificates installed. The system’s trusted public CAs are always available.</p>}
          {trust.data.certificates.map(cert => <div key={cert.id} className="space-y-2 rounded-lg border border-border p-3 text-sm">
            <div className="flex items-start justify-between gap-3"><div><div className="font-medium">{cert.label} <span className="font-normal text-muted">· {cert.is_ca ? 'CA' : 'Self-signed service'}</span></div>
              <div className="break-all text-muted">{cert.subject}</div></div>
              <Button variant="ghost" size="sm" aria-label={`Remove ${cert.label}`} onClick={() => setRemoving(cert.id)}><Trash2 className="h-4 w-4" /></Button></div>
            <div>Scope: {cert.hosts.length ? cert.hosts.join(', ') : 'All monitored services'}</div>
            <div className={Date.parse(cert.not_after) < Date.now() ? 'text-danger' : 'text-muted'}>Expires {new Date(cert.not_after).toLocaleDateString()} · Issuer: {cert.issuer}</div>
            <div className="break-all font-mono text-xs text-muted">SHA-256: {cert.id}</div>
            {removing === cert.id && <div className="flex flex-wrap items-center gap-2 text-warning">Services relying on this certificate may fail verification.
              <Button size="sm" disabled={remove.isPending} onClick={() => remove.mutate(cert.id)}>Remove trust</Button><Button size="sm" variant="ghost" onClick={() => setRemoving(null)}>Cancel</Button></div>}
          </div>)}
        </div>
      </>}
    </CardContent>
  </Card>
}
