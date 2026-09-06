import { FormEvent, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileKey2,
  Loader2,
  Lock,
  RefreshCw,
  ShieldCheck,
  Trash2,
  Upload,
} from 'lucide-react'
import { ManagementAccessCard } from '@/components/ManagementAccessCard'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, copyText } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'

// Backend: server/app/api/v1/security_settings.py (/api/v1/system/security/tls)

// ─── Types ──────────────────────────────────────────────────────────────────

interface CertificateInfo {
  subject: string
  issuer: string
  self_signed: boolean
  not_before: string
  not_after: string
  days_remaining: number
  san_dns: string[]
  san_ips: string[]
  fingerprint_sha256: string
  key_algorithm: string
  chain_installed?: boolean
}

interface TlsSettings {
  https_enabled: boolean
  redirect_http: boolean
  hsts_enabled: boolean
  min_tls_version: '1.2' | '1.3'
}

interface TlsStatus {
  helper_installed: boolean
  settings: TlsSettings
  applied: { https?: string; redirect?: string; hsts?: string; min_tls?: string } | null
  certificate: CertificateInfo | null
  pending_csr: { common_name: string; created_at: string; csr_pem: string } | null
}

const KEY_TYPES = [
  { value: 'rsa2048', label: 'RSA 2048' },
  { value: 'rsa4096', label: 'RSA 4096' },
  { value: 'ecdsa-p256', label: 'ECDSA P-256' },
]

type CertMode = 'self-signed' | 'csr' | 'upload' | 'pfx'

// ─── Component ──────────────────────────────────────────────────────────────

export function SecurityTabContent() {
  const status = useQuery<TlsStatus>({
    queryKey: ['security', 'tls'],
    queryFn: async () => (await api.get('/system/security/tls')).data,
  })

  if (status.isLoading) {
    return (
      <div className="flex items-center gap-2 py-10 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading security status…
      </div>
    )
  }
  if (status.isError || !status.data) {
    return (
      <div className="flex items-center gap-2 py-10 text-sm text-danger">
        <AlertTriangle className="h-4 w-4" /> Failed to load security status: {apiErrorMessage(status.error)}
      </div>
    )
  }

  const s = status.data
  return (
    <div className="space-y-4">
      {!s.helper_installed && <SetupRequiredBanner />}
      <StatusCard status={s} />
      <CertificateCard status={s} />
      <HardeningCard status={s} />
      <ManagementAccessCard />
      <AgentTrustNote status={s} />
    </div>
  )
}

// ─── Setup banner ───────────────────────────────────────────────────────────

function SetupRequiredBanner() {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
      <div>
        <div className="font-medium text-warning">One-time setup required on the appliance</div>
        <p className="mt-1 text-muted">
          The privileged security helper is not installed yet, so certificate installation and
          nginx changes cannot be applied. Run this once on the appliance console, then retry:
        </p>
        <code className="mt-2 block w-fit rounded bg-surface2 px-2 py-1 font-mono text-xs">
          sudo bash /opt/zenplus/scripts/setup-security.sh
        </code>
      </div>
    </div>
  )
}

// ─── Status card ────────────────────────────────────────────────────────────

function StatusCard({ status }: { status: TlsStatus }) {
  const qc = useQueryClient()
  const cert = status.certificate
  const httpsLive = status.applied?.https === 'on'

  const removeCert = useMutation({
    mutationFn: async () => (await api.delete('/system/security/tls/certificate')).data,
    onSuccess: () => {
      toast.success('Certificate removed')
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
    },
    onError: (e: any) => toast.error('Remove failed', apiErrorMessage(e)),
  })

  async function downloadCert() {
    try {
      const res = await api.get('/system/security/tls/certificate/download', { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = 'zenplus-server.crt'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error('Download failed', apiErrorMessage(e))
    }
  }

  const expiryVariant = !cert ? 'outline'
    : cert.days_remaining < 14 ? 'danger'
    : cert.days_remaining < 45 ? 'warning'
    : 'success'

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-primary" /> Transport Security Status
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          {httpsLive ? (
            <Badge variant="success"><Lock className="h-3 w-3" /> HTTPS enabled</Badge>
          ) : (
            <Badge variant="danger"><AlertTriangle className="h-3 w-3" /> HTTP only — traffic is unencrypted</Badge>
          )}
          {httpsLive && status.applied?.redirect === 'on' && <Badge variant="info">HTTP → HTTPS redirect</Badge>}
          {httpsLive && status.applied?.hsts === 'on' && <Badge variant="info">HSTS</Badge>}
          {httpsLive && <Badge variant="outline">Min TLS {status.applied?.min_tls ?? '1.2'}</Badge>}
        </div>

        {cert ? (
          <div className="rounded-lg border border-border bg-surface2/40 p-4 text-sm">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <span className="font-medium">{cert.subject}</span>
              {cert.self_signed
                ? <Badge variant="warning">Self-signed</Badge>
                : <Badge variant="success">CA-issued</Badge>}
              <Badge variant={expiryVariant}>
                {cert.days_remaining < 0 ? 'Expired' : `Expires in ${cert.days_remaining} days`}
              </Badge>
            </div>
            <dl className="grid grid-cols-1 gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
              <InfoRow label="Issued by" value={cert.issuer} />
              <InfoRow label="Key" value={cert.key_algorithm} />
              <InfoRow label="Valid from" value={new Date(cert.not_before).toLocaleDateString()} />
              <InfoRow label="Valid until" value={new Date(cert.not_after).toLocaleDateString()} />
              {(cert.san_dns.length > 0 || cert.san_ips.length > 0) && (
                <InfoRow label="Names (SAN)" value={[...cert.san_dns, ...cert.san_ips].join(', ')} />
              )}
              <InfoRow label="SHA-256" value={cert.fingerprint_sha256?.slice(0, 32) + '…'} mono />
            </dl>
            <div className="mt-3 flex gap-2">
              <Button size="sm" variant="outline" onClick={downloadCert}>
                <Download className="mr-1.5 h-3.5 w-3.5" /> Download certificate
              </Button>
              <Button
                size="sm" variant="outline"
                className="text-danger hover:text-danger"
                disabled={httpsLive || removeCert.isPending}
                title={httpsLive ? 'Disable HTTPS before removing the certificate' : undefined}
                onClick={() => removeCert.mutate()}
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" /> Remove
              </Button>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted">
            No certificate installed. Generate a self-signed certificate below for immediate
            protection, or issue one from your enterprise CA (Active Directory Certificate
            Services) via the CSR workflow for a certificate your domain already trusts.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2">
      <dt className="w-24 shrink-0 text-muted">{label}</dt>
      <dd className={cn('truncate', mono && 'font-mono')} title={value}>{value}</dd>
    </div>
  )
}

// ─── Certificate management card ────────────────────────────────────────────

function CertificateCard({ status }: { status: TlsStatus }) {
  const [mode, setMode] = useState<CertMode>(status.pending_csr ? 'csr' : 'self-signed')

  const MODES: { value: CertMode; label: string }[] = [
    { value: 'self-signed', label: 'Self-signed' },
    { value: 'csr', label: 'Enterprise CA / AD CS' },
    { value: 'upload', label: 'Upload PEM' },
    { value: 'pfx', label: 'Upload PFX' },
  ]

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileKey2 className="h-4 w-4 text-primary" /> Certificate
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-1 rounded-lg bg-surface2/50 p-1 w-fit">
          {MODES.map((m) => (
            <button
              key={m.value}
              type="button"
              onClick={() => setMode(m.value)}
              className={cn(
                'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                mode === m.value ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
              )}
            >
              {m.label}
            </button>
          ))}
        </div>

        {mode === 'self-signed' && <SelfSignedForm />}
        {mode === 'csr' && <CsrFlow status={status} />}
        {mode === 'upload' && <PemUploadForm />}
        {mode === 'pfx' && <PfxUploadForm />}
      </CardContent>
    </Card>
  )
}

function useSanState() {
  const [sanDns, setSanDns] = useState('')
  const [sanIps, setSanIps] = useState('')
  useEffect(() => {
    // Prefill with how the operator is reaching the appliance right now.
    const host = window.location.hostname
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) setSanIps(host)
    else setSanDns(host)
  }, [])
  const parse = (v: string) => v.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean)
  return { sanDns, setSanDns, sanIps, setSanIps, parse }
}

function SelfSignedForm() {
  const qc = useQueryClient()
  // Not `cn` — that is the classname helper imported above, and shadowing it
  // makes every cn(...) call in this component a TypeError at render time.
  const [commonName, setCommonName] = useState(() => window.location.hostname)
  const { sanDns, setSanDns, sanIps, setSanIps, parse } = useSanState()
  const [days, setDays] = useState('1095')
  const [keyType, setKeyType] = useState('rsa2048')

  const generate = useMutation({
    mutationFn: async () => (await api.post('/system/security/tls/self-signed', {
      common_name: commonName,
      san_dns: parse(sanDns),
      san_ips: parse(sanIps),
      days_valid: parseInt(days, 10) || 1095,
      key_type: keyType,
    })).data,
    onSuccess: () => {
      toast.success('Self-signed certificate generated and installed')
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
    },
    onError: (e: any) => toast.error('Generation failed', apiErrorMessage(e)),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    generate.mutate()
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      <p className="text-xs text-muted">
        Instantly enables encrypted access. Browsers and agents must be told to trust this
        certificate (download it from the status card above and distribute it, e.g. via AD GPO
        to <span className="font-mono">Trusted Root Certification Authorities</span>).
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <FormField label="Common name (hostname or IP)">
          <Input value={commonName} onChange={(e) => setCommonName(e.target.value)} required />
        </FormField>
        <FormField label="Validity (days)">
          <Input type="number" min={1} max={3650} value={days} onChange={(e) => setDays(e.target.value)} />
        </FormField>
        <FormField label="DNS names (SAN, comma-separated)">
          <Input value={sanDns} onChange={(e) => setSanDns(e.target.value)} placeholder="zenplus.corp.local" />
        </FormField>
        <FormField label="IP addresses (SAN, comma-separated)">
          <Input value={sanIps} onChange={(e) => setSanIps(e.target.value)} placeholder="10.12.50.81" />
        </FormField>
        <FormField label="Key type">
          <Select value={keyType} onValueChange={setKeyType}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {KEY_TYPES.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </FormField>
      </div>
      <Button type="submit" disabled={generate.isPending}>
        {generate.isPending
          ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Generating…</>
          : <><RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Generate &amp; install</>}
      </Button>
    </form>
  )
}

function CsrFlow({ status }: { status: TlsStatus }) {
  const qc = useQueryClient()
  const pending = status.pending_csr

  // Not `cn` — that is the classname helper imported above, and shadowing it
  // makes every cn(...) call in this component a TypeError at render time.
  const [commonName, setCommonName] = useState(() => window.location.hostname)
  const { sanDns, setSanDns, sanIps, setSanIps, parse } = useSanState()
  const [org, setOrg] = useState('')
  const [ou, setOu] = useState('')
  const [country, setCountry] = useState('')
  const [keyType, setKeyType] = useState('rsa2048')
  const [signedCert, setSignedCert] = useState('')
  const [chain, setChain] = useState('')
  const [issuedMode, setIssuedMode] = useState<'file' | 'paste'>('file')
  const certFileRef = useRef<HTMLInputElement>(null)
  const chainFileRef = useRef<HTMLInputElement>(null)

  const generateCsr = useMutation({
    mutationFn: async () => (await api.post('/system/security/tls/csr', {
      common_name: commonName,
      san_dns: parse(sanDns),
      san_ips: parse(sanIps),
      organization: org,
      organizational_unit: ou,
      country,
      key_type: keyType,
    })).data,
    onSuccess: () => {
      toast.success('CSR generated', 'Submit it to your CA, then paste the issued certificate here')
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
    },
    onError: (e: any) => toast.error('CSR generation failed', apiErrorMessage(e)),
  })

  const discardCsr = useMutation({
    mutationFn: async () => (await api.delete('/system/security/tls/csr')).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['security', 'tls'] }),
    onError: (e: any) => toast.error('Discard failed', apiErrorMessage(e)),
  })

  const installSigned = useMutation({
    mutationFn: async () => (await api.post('/system/security/tls/certificate', {
      certificate_pem: signedCert,
      chain_pem: chain,
    })).data,
    onSuccess: () => {
      toast.success('CA-issued certificate installed')
      setSignedCert(''); setChain('')
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
    },
    onError: (e: any) => toast.error('Install failed', apiErrorMessage(e)),
  })

  const installFile = useMutation({
    mutationFn: async () => {
      const f = certFileRef.current?.files?.[0]
      if (!f) throw new Error('Select the certificate file issued by your CA')
      const fd = new FormData()
      fd.append('file', f)
      const cf = chainFileRef.current?.files?.[0]
      if (cf) fd.append('chain_file', cf)
      return (await api.post('/system/security/tls/certificate/file', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })).data
    },
    onSuccess: (data: any) => {
      const n = data?.chain_certificates ?? 0
      toast.success('CA-issued certificate installed',
        n > 0 ? `${n} chain certificate${n === 1 ? '' : 's'} included` : undefined)
      if (certFileRef.current) certFileRef.current.value = ''
      if (chainFileRef.current) chainFileRef.current.value = ''
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
    },
    onError: (e: any) => toast.error('Install failed', apiErrorMessage(e)),
  })

  function downloadCsr() {
    if (!pending) return
    const blob = new Blob([pending.csr_pem], { type: 'application/pkcs10' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'zenplus.csr'
    a.click()
    URL.revokeObjectURL(url)
  }

  if (!pending) {
    return (
      <form onSubmit={(e) => { e.preventDefault(); generateCsr.mutate() }} className="space-y-3">
        <p className="text-xs text-muted">
          Generates a private key (kept on the appliance, never leaves it) and a Certificate
          Signing Request. Submit the CSR to your enterprise CA — for Active Directory
          Certificate Services use the <span className="font-mono">Web Server</span> template
          — then paste the issued certificate back here.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <FormField label="Common name (FQDN)">
            <Input value={commonName} onChange={(e) => setCommonName(e.target.value)} required placeholder="zenplus.corp.local" />
          </FormField>
          <FormField label="Key type">
            <Select value={keyType} onValueChange={setKeyType}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {KEY_TYPES.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="DNS names (SAN, comma-separated)">
            <Input value={sanDns} onChange={(e) => setSanDns(e.target.value)} placeholder="zenplus.corp.local" />
          </FormField>
          <FormField label="IP addresses (SAN, comma-separated)">
            <Input value={sanIps} onChange={(e) => setSanIps(e.target.value)} placeholder="10.12.50.81" />
          </FormField>
          <FormField label="Organization (optional)">
            <Input value={org} onChange={(e) => setOrg(e.target.value)} />
          </FormField>
          <FormField label="Org. unit (optional)">
            <Input value={ou} onChange={(e) => setOu(e.target.value)} />
          </FormField>
          <FormField label="Country code (optional)">
            <Input value={country} onChange={(e) => setCountry(e.target.value)} maxLength={2} placeholder="AE" />
          </FormField>
        </div>
        <Button type="submit" disabled={generateCsr.isPending}>
          {generateCsr.isPending
            ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Generating…</>
            : 'Generate CSR'}
        </Button>
      </form>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm">
        <CheckCircle2 className="h-4 w-4 text-success" />
        <span>
          Pending CSR for <span className="font-medium">{pending.common_name}</span> — the private
          key is stored on the appliance and will be paired with the issued certificate.
        </span>
      </div>
      <FormField label="CSR (submit this to your CA)">
        <Textarea readOnly value={pending.csr_pem} rows={6} className="font-mono text-xs" />
      </FormField>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" onClick={downloadCsr}>
          <Download className="mr-1.5 h-3.5 w-3.5" /> Download CSR
        </Button>
        <Button
          size="sm" variant="outline"
          onClick={async () => { if (await copyText(pending.csr_pem)) toast.success('CSR copied'); else toast.error('Could not copy', 'Select the text and copy manually.') }}
        >
          Copy
        </Button>
        <Button
          size="sm" variant="outline" className="text-danger hover:text-danger"
          disabled={discardCsr.isPending}
          onClick={() => discardCsr.mutate()}
        >
          Discard
        </Button>
      </div>
      <hr className="border-border" />

      <div>
        <div className="text-sm font-medium">Step 2 — install the certificate issued by your CA</div>
        <p className="mt-1 text-xs text-muted">
          Upload the file exactly as the CA produced it, or paste the certificate text.
        </p>
      </div>

      <div className="flex gap-1 rounded-lg bg-surface2/50 p-1 w-fit">
        {(['file', 'paste'] as const).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setIssuedMode(m)}
            className={cn(
              'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
              issuedMode === m ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
            )}
          >
            {m === 'file' ? 'Upload file' : 'Paste text'}
          </button>
        ))}
      </div>

      {issuedMode === 'file' ? (
        <form onSubmit={(e) => { e.preventDefault(); installFile.mutate() }} className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <FormField label="Issued certificate file">
              <Input ref={certFileRef} type="file" accept=".cer,.crt,.pem,.der,.p7b,.p7c,application/x-x509-ca-cert,application/pkcs7-mime" required />
            </FormField>
            <FormField label="CA chain file (optional)">
              <Input ref={chainFileRef} type="file" accept=".cer,.crt,.pem,.der,.p7b,.p7c" />
            </FormField>
          </div>
          <p className="text-xs text-muted">
            Accepts <span className="font-mono">.cer</span> / <span className="font-mono">.crt</span> in
            either Base-64 or DER encoding, and <span className="font-mono">.p7b</span> certificate
            chains — all four of the download options Active Directory Certificate Services offers.
            If you download the full chain, the intermediates are extracted automatically and no
            separate chain file is needed.
          </p>
          <Button type="submit" disabled={installFile.isPending}>
            {installFile.isPending
              ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Installing…</>
              : <><Upload className="mr-1.5 h-3.5 w-3.5" /> Install issued certificate</>}
          </Button>
        </form>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); installSigned.mutate() }} className="space-y-3">
          <FormField label="Issued certificate">
            <Textarea
              value={signedCert}
              onChange={(e) => setSignedCert(e.target.value)}
              rows={5}
              required
              placeholder="-----BEGIN CERTIFICATE-----"
              className="font-mono text-xs"
            />
          </FormField>
          <FormField label="CA chain / intermediates (optional)">
            <Textarea
              value={chain}
              onChange={(e) => setChain(e.target.value)}
              rows={4}
              placeholder="-----BEGIN CERTIFICATE-----"
              className="font-mono text-xs"
            />
          </FormField>
          <Button type="submit" disabled={installSigned.isPending || !signedCert.trim()}>
            {installSigned.isPending
              ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Installing…</>
              : 'Install issued certificate'}
          </Button>
        </form>
      )}
    </div>
  )
}

function PemUploadForm() {
  const qc = useQueryClient()
  const [cert, setCert] = useState('')
  const [key, setKey] = useState('')
  const [chain, setChain] = useState('')
  const [passphrase, setPassphrase] = useState('')

  const install = useMutation({
    mutationFn: async () => (await api.post('/system/security/tls/certificate', {
      certificate_pem: cert,
      private_key_pem: key,
      chain_pem: chain,
      key_passphrase: passphrase,
    })).data,
    onSuccess: () => {
      toast.success('Certificate installed')
      setCert(''); setKey(''); setChain(''); setPassphrase('')
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
    },
    onError: (e: any) => toast.error('Install failed', apiErrorMessage(e)),
  })

  return (
    <form onSubmit={(e) => { e.preventDefault(); install.mutate() }} className="space-y-3">
      <p className="text-xs text-muted">
        Install an existing certificate and private key in PEM format (e.g. exported from
        another system or issued out-of-band by your CA).
      </p>
      <FormField label="Certificate (PEM)">
        <Textarea value={cert} onChange={(e) => setCert(e.target.value)} rows={5} required
          placeholder="-----BEGIN CERTIFICATE-----" className="font-mono text-xs" />
      </FormField>
      <FormField label="Private key (PEM)">
        <Textarea value={key} onChange={(e) => setKey(e.target.value)} rows={5} required
          placeholder="-----BEGIN PRIVATE KEY-----" className="font-mono text-xs" />
      </FormField>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <FormField label="Key passphrase (if encrypted)">
          <Input type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} autoComplete="new-password" />
        </FormField>
      </div>
      <FormField label="CA chain / intermediates (PEM, optional)">
        <Textarea value={chain} onChange={(e) => setChain(e.target.value)} rows={4}
          placeholder="-----BEGIN CERTIFICATE-----" className="font-mono text-xs" />
      </FormField>
      <Button type="submit" disabled={install.isPending || !cert.trim() || !key.trim()}>
        {install.isPending
          ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Installing…</>
          : 'Install certificate'}
      </Button>
    </form>
  )
}

function PfxUploadForm() {
  const qc = useQueryClient()
  const fileRef = useRef<HTMLInputElement>(null)
  const [password, setPassword] = useState('')

  const install = useMutation({
    mutationFn: async () => {
      const file = fileRef.current?.files?.[0]
      if (!file) throw new Error('Select a .pfx / .p12 file')
      const fd = new FormData()
      fd.append('file', file)
      fd.append('password', password)
      return (await api.post('/system/security/tls/pfx', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })).data
    },
    onSuccess: () => {
      toast.success('Certificate installed from PFX bundle')
      if (fileRef.current) fileRef.current.value = ''
      setPassword('')
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
    },
    onError: (e: any) => toast.error('Install failed', apiErrorMessage(e)),
  })

  return (
    <form onSubmit={(e) => { e.preventDefault(); install.mutate() }} className="space-y-3">
      <p className="text-xs text-muted">
        Install a PKCS#12 bundle (.pfx / .p12) — the usual export format from Active Directory
        Certificate Services and Windows certificate stores. The bundle's certificate, key and
        any included chain are all installed.
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <FormField label="PFX / P12 file">
          <Input ref={fileRef} type="file" accept=".pfx,.p12" required />
        </FormField>
        <FormField label="Bundle password">
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </FormField>
      </div>
      <Button type="submit" disabled={install.isPending}>
        {install.isPending
          ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Installing…</>
          : 'Install from PFX'}
      </Button>
    </form>
  )
}

// ─── Hardening card ─────────────────────────────────────────────────────────

function HardeningCard({ status }: { status: TlsStatus }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<TlsSettings>(status.settings)
  useEffect(() => { setForm(status.settings) }, [status.settings])

  const hasCert = !!status.certificate
  const dirty = JSON.stringify(form) !== JSON.stringify(status.settings)

  const save = useMutation({
    mutationFn: async () => (await api.put('/system/security/tls/config', form)).data,
    onSuccess: (_data, _vars) => {
      toast.success('TLS configuration applied', 'nginx has been reloaded')
      qc.invalidateQueries({ queryKey: ['security', 'tls'] })
      // If we just enabled HTTPS while browsing over HTTP, take the operator
      // to the secure origin (they may need to accept/trust the certificate).
      if (form.https_enabled && window.location.protocol === 'http:' && form.redirect_http) {
        setTimeout(() => { window.location.href = `https://${window.location.host}${window.location.pathname}` }, 1500)
      }
    },
    onError: (e: any) => toast.error('Apply failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Lock className="h-4 w-4 text-primary" /> HTTPS &amp; TLS Hardening
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <ToggleRow
          label="Enable HTTPS"
          hint={hasCert
            ? 'Serve the dashboard, API, and agent endpoints over TLS on port 443'
            : 'Install a certificate first'}
          checked={form.https_enabled}
          disabled={!hasCert}
          onChange={(v) => setForm({ ...form, https_enabled: v })}
        />
        <ToggleRow
          label="Redirect HTTP to HTTPS"
          hint="Port 80 answers with a 301 to the secure origin (the /health probe stays on HTTP). Agents configured with an http:// controller URL should be re-pointed to https:// before enabling."
          checked={form.redirect_http}
          disabled={!form.https_enabled}
          onChange={(v) => setForm({ ...form, redirect_http: v })}
        />
        <ToggleRow
          label="HSTS (Strict-Transport-Security)"
          hint="Instructs browsers to always use HTTPS for this host (max-age 6 months). Enable only once the certificate is trusted by your clients."
          checked={form.hsts_enabled}
          disabled={!form.https_enabled}
          onChange={(v) => setForm({ ...form, hsts_enabled: v })}
        />
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="text-sm font-medium">Minimum TLS version</div>
            <div className="text-xs text-muted">TLS 1.2 is broadly compatible; TLS 1.3-only is the strictest setting</div>
          </div>
          <Select
            value={form.min_tls_version}
            onValueChange={(v) => setForm({ ...form, min_tls_version: v as '1.2' | '1.3' })}
            disabled={!form.https_enabled}
          >
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="1.2">TLS 1.2+</SelectItem>
              <SelectItem value="1.3">TLS 1.3 only</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {dirty && form.https_enabled && !status.settings.https_enabled && (
          <div className="flex items-start gap-2 rounded-md border border-info/30 bg-info/10 p-3 text-xs">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" />
            <span>
              After applying, your browser will move to the <span className="font-mono">https://</span> origin
              — you may need to accept the certificate and sign in again. With a self-signed
              certificate, distribute it to clients (browser trust store / AD GPO) to avoid warnings.
            </span>
          </div>
        )}

        <div>
          <Button onClick={() => save.mutate()} disabled={save.isPending || !dirty}>
            {save.isPending
              ? <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Applying…</>
              : 'Apply configuration'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function ToggleRow({ label, hint, checked, disabled, onChange }: {
  label: string
  hint: string
  checked: boolean
  disabled?: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        <div className="text-sm font-medium">{label}</div>
        <div className="text-xs text-muted">{hint}</div>
      </div>
      <Switch checked={checked} disabled={disabled} onCheckedChange={onChange} />
    </div>
  )
}

// ─── Agent trust note ───────────────────────────────────────────────────────

function AgentTrustNote({ status }: { status: TlsStatus }) {
  if (status.applied?.https !== 'on') return null
  return (
    <div className="flex items-start gap-3 rounded-lg border border-border bg-surface2/40 p-4 text-xs text-muted">
      <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
      <div className="space-y-1">
        <div className="text-sm font-medium text-text">Securing agents &amp; sensors</div>
        <p>
          Host agents and remote sensors connect to the controller URL they were enrolled with.
          Newly generated install commands will use HTTPS automatically; existing agents keep
          their configured URL — update <span className="font-mono">ZENPLUS_CONTROLLER_URL</span>{' '}
          (agents) or the sensor controller URL to <span className="font-mono">https://…</span> and,
          for self-signed certificates, install the downloaded certificate into the host's trust
          store so connections verify.
        </p>
      </div>
    </div>
  )
}
