import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as Popover from '@radix-ui/react-popover'
import {
  Building2, Check, ChevronsUpDown, Clock, FileText, Globe2, ImagePlus, Loader2, Palette,
  RotateCcw, Save, Search, ShieldCheck, Trash2, Upload,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import {
  BRAND_PRESETS, CONFIDENTIALITY_LEVELS, DATE_FORMATS, DEFAULT_BRAND_COLOR, formatDate, formatDateTime,
  formatPeriod, formatTime, logoUrl, onColor, timezoneList, zoneLabel,
  type Confidentiality, type DateFormat, type TimeFormat,
} from '@/lib/branding'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'

type LogoVariant = 'light' | 'dark'
type LogoMeta = { content_type: string; filename: string; size: number; updated_at: string } | null

export type CompanyForm = {
  company_name: string
  company_address: string
  company_email: string
  company_phone: string
  company_website: string
  base_url: string
  timezone: string
  date_format: DateFormat
  time_format: TimeFormat
  week_start: 'monday' | 'sunday' | 'saturday'
  brand_color: string
  report_footer: string
  report_disclaimer: string
  confidentiality: Confidentiality
  paper_size: 'A4' | 'Letter'
}

type CompanyResponse = CompanyForm & { logos?: Record<LogoVariant, LogoMeta> }

const EMPTY: CompanyForm = {
  company_name: '', company_address: '', company_email: '', company_phone: '', company_website: '', base_url: '',
  timezone: 'UTC', date_format: 'YYYY-MM-DD', time_format: '24h', week_start: 'monday',
  brand_color: DEFAULT_BRAND_COLOR, report_footer: '', report_disclaimer: '', confidentiality: '', paper_size: 'A4',
}

const FIELDS = Object.keys(EMPTY) as (keyof CompanyForm)[]
const LOGO_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml']
const LOGO_MAX = 2 * 1024 * 1024
const HEX = /^#[0-9a-fA-F]{6}$/

function pick(data?: Partial<CompanyResponse>): CompanyForm {
  const out = { ...EMPTY }
  for (const k of FIELDS) if (data && data[k] != null) (out as any)[k] = data[k]
  return out
}

function fmtBytes(n: number) {
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export function CompanySettings() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery<CompanyResponse>({
    queryKey: ['settings', 'company'],
    queryFn: async () => (await api.get('/settings/company')).data,
  })
  const saved = useMemo(() => pick(data), [data])
  const [form, setForm] = useState<CompanyForm>(EMPTY)
  // Refetches after a logo upload or removal change only the logo metadata:
  // keep the form as it is. When saved values do change, take them for the
  // fields the user has not edited since the last load.
  const lastSaved = useRef<CompanyForm | null>(null)
  useEffect(() => {
    if (!data) return
    const next = pick(data)
    const prev = lastSaved.current
    lastSaved.current = next
    if (prev && FIELDS.every((k) => prev[k] === next[k])) return
    setForm((f) => {
      if (!prev) return next
      const merged = { ...next }
      for (const k of FIELDS) if (f[k] !== prev[k]) (merged as any)[k] = f[k]
      return merged
    })
  }, [data])
  const set = <K extends keyof CompanyForm>(k: K, v: CompanyForm[K]) => setForm((f) => ({ ...f, [k]: v }))

  const dirty = FIELDS.some((k) => form[k] !== saved[k])
  const colorError = HEX.test(form.brand_color) ? null : 'Use a 6-digit hex colour such as #4F6BF6'
  const emailError = form.company_email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.company_email) ? 'Enter a valid email address' : null
  const websiteError = form.company_website && !/^https?:\/\/\S+$/i.test(form.company_website) ? 'Start with http:// or https://' : null
  const baseUrlError = form.base_url && !/^https?:\/\/\S+$/i.test(form.base_url) ? 'Start with http:// or https://' : null
  const invalid = Boolean(colorError || emailError || websiteError || baseUrlError)

  const save = useMutation({
    mutationFn: async () => (await api.put('/settings/company', form)).data,
    onSuccess: () => {
      toast.success('Company settings saved', 'New reports use the updated branding and formats.')
      qc.invalidateQueries({ queryKey: ['settings', 'company'] })
      qc.invalidateQueries({ queryKey: ['settings', 'timezone'] })
      qc.invalidateQueries({ queryKey: ['reports', 'branding'] })
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const logos = data?.logos ?? { light: null, dark: null }

  return (
    <form
      onSubmit={(e: FormEvent) => { e.preventDefault(); if (!invalid) save.mutate() }}
      className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_380px]"
    >
      <div className="min-w-0 space-y-4">
        {/* Organisation */}
        <Card>
          <CardHeader className="pb-3">
            <SectionTitle icon={Building2} title="Organisation" description="Shown on report covers, page footers and report emails." />
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="Company name" className="md:col-span-2" hint="Leave empty to show the product name.">
              <Input value={form.company_name} maxLength={120} onChange={(e) => set('company_name', e.target.value)} placeholder="Acme Corporation" />
            </FormField>
            <FormField label="Website" error={websiteError}>
              <Input value={form.company_website} maxLength={255} onChange={(e) => set('company_website', e.target.value)} placeholder="https://example.com" />
            </FormField>
            <FormField label="Email" error={emailError}>
              <Input type="email" value={form.company_email} maxLength={254} onChange={(e) => set('company_email', e.target.value)} placeholder="noc@example.com" />
            </FormField>
            <FormField label="Phone">
              <Input value={form.company_phone} maxLength={64} onChange={(e) => set('company_phone', e.target.value)} placeholder="+966 11 000 0000" />
            </FormField>
            <FormField
              label="Appliance URL"
              error={baseUrlError}
              hint="Used for “View full report” links in report emails, e.g. https://zenplus.example.com"
            >
              <Input value={form.base_url} maxLength={255} onChange={(e) => set('base_url', e.target.value.trim())} placeholder={window.location.origin} />
            </FormField>
            <FormField label="Address" className="md:col-span-2">
              <Textarea rows={2} value={form.company_address} maxLength={500} onChange={(e) => set('company_address', e.target.value)} />
            </FormField>
          </CardContent>
        </Card>

        {/* Logo */}
        <Card>
          <CardHeader className="pb-3">
            <SectionTitle
              icon={ImagePlus}
              title="Logo"
              description="PNG, SVG, JPEG or WebP up to 2 MB. A horizontal logo on a transparent background, at least 600 px wide, prints best."
            />
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <LogoSlot
              variant="light"
              meta={logos.light}
              title="Primary logo"
              hint="For light backgrounds: report pages, printouts and emails."
            />
            <LogoSlot
              variant="dark"
              meta={logos.dark}
              title="Logo for dark backgrounds"
              hint="Optional. Used on the report cover; the primary logo is used when empty."
            />
          </CardContent>
        </Card>

        {/* Regional */}
        <Card>
          <CardHeader className="pb-3">
            <SectionTitle icon={Globe2} title="Regional formats" description="Every report, schedule and export prints times in this timezone and format." />
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="Timezone" className="md:col-span-2" hint={<NowLine form={form} />}>
              <TimezonePicker value={form.timezone} onChange={(tz) => set('timezone', tz)} />
            </FormField>
            <FormField label="Date format">
              <Select value={form.date_format} onValueChange={(v) => set('date_format', v as DateFormat)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {DATE_FORMATS.map((f) => (
                    <SelectItem key={f.value} value={f.value}>
                      <span className="font-mono text-xs">{f.value}</span>
                      <span className="ml-2 text-muted">{f.example}</span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Time format">
              <Segmented
                value={form.time_format}
                onChange={(v) => set('time_format', v as TimeFormat)}
                options={[{ value: '24h', label: '24-hour', sub: '14:05' }, { value: '12h', label: '12-hour', sub: '2:05 PM' }]}
              />
            </FormField>
            <FormField label="First day of the week" hint="Used by weekly report periods.">
              <Select value={form.week_start} onValueChange={(v) => set('week_start', v as CompanyForm['week_start'])}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="monday">Monday</SelectItem>
                  <SelectItem value="sunday">Sunday</SelectItem>
                  <SelectItem value="saturday">Saturday</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
          </CardContent>
        </Card>

        {/* Report branding */}
        <Card>
          <CardHeader className="pb-3">
            <SectionTitle icon={Palette} title="Report branding" description="Colour, classification and footer applied to on-screen, PDF, HTML and emailed reports." />
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="Brand colour" error={colorError} className="md:col-span-2">
              <ColorField value={form.brand_color} onChange={(v) => set('brand_color', v)} />
            </FormField>
            <FormField label="Classification label" hint={CONFIDENTIALITY_LEVELS.find((c) => c.value === form.confidentiality)?.hint}>
              <Select value={form.confidentiality || 'none'} onValueChange={(v) => set('confidentiality', (v === 'none' ? '' : v) as Confidentiality)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {CONFIDENTIALITY_LEVELS.map((c) => (
                    <SelectItem key={c.label} value={c.value || 'none'}>{c.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Paper size" hint="PDF page size.">
              <Segmented
                value={form.paper_size}
                onChange={(v) => set('paper_size', v as 'A4' | 'Letter')}
                options={[{ value: 'A4', label: 'A4', sub: '210 × 297 mm' }, { value: 'Letter', label: 'Letter', sub: '8.5 × 11 in' }]}
              />
            </FormField>
            <FormField label="Footer text" className="md:col-span-2" hint={`Printed on every page. ${form.report_footer.length}/200`}>
              <Input value={form.report_footer} maxLength={200} onChange={(e) => set('report_footer', e.target.value)} placeholder="Prepared by the Network Operations Centre · noc@example.com" />
            </FormField>
            <FormField label="Disclaimer" className="md:col-span-2" hint={`Printed at the end of each report. ${form.report_disclaimer.length}/1000`}>
              <Textarea rows={3} value={form.report_disclaimer} maxLength={1000} onChange={(e) => set('report_disclaimer', e.target.value)} placeholder="This report contains information intended only for the named recipients…" />
            </FormField>
          </CardContent>
        </Card>
      </div>

      {/* Preview + actions */}
      <div className="space-y-3 xl:sticky xl:top-4 xl:self-start">
        <Card>
          <CardHeader className="pb-3">
            <SectionTitle icon={FileText} title="Report preview" description="How the cover and a content page will look." />
          </CardHeader>
          <CardContent>
            <BrandPreview form={form} logos={logos} />
          </CardContent>
        </Card>
        <div className="flex items-center justify-end gap-2 rounded-lg border border-border bg-surface p-3 shadow-card dark:shadow-card-dark">
          <span className={cn('mr-auto text-xs', dirty ? 'text-warning' : 'text-muted')}>
            {isLoading ? 'Loading…' : dirty ? 'Unsaved changes' : 'All changes saved'}
          </span>
          <Button type="button" variant="ghost" size="sm" disabled={!dirty || save.isPending} onClick={() => setForm(saved)}>
            <RotateCcw /> Reset
          </Button>
          <Button type="submit" size="sm" disabled={!dirty || invalid || save.isPending}>
            {save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save changes
          </Button>
        </div>
      </div>
    </form>
  )
}

/* ------------------------------------------------------------------ */
/*  Pieces                                                             */
/* ------------------------------------------------------------------ */

function SectionTitle({ icon: Icon, title, description }: { icon: typeof Building2; title: string; description: string }) {
  return (
    <div className="flex items-start gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
        <Icon className="h-4 w-4" />
      </div>
      <div className="min-w-0">
        <CardTitle className="text-sm">{title}</CardTitle>
        <CardDescription className="mt-1 text-xs">{description}</CardDescription>
      </div>
    </div>
  )
}

function NowLine({ form }: { form: CompanyForm }) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(t)
  }, [])
  return (
    <span className="inline-flex items-center gap-1">
      <Clock className="h-3 w-3" /> Now: {formatDateTime(now, form)} {zoneLabel(form.timezone, now)}
    </span>
  )
}

function Segmented({ value, onChange, options }: {
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string; sub?: string }[]
}) {
  return (
    <div role="radiogroup" className="grid grid-flow-col auto-cols-fr gap-1 rounded-md border border-border bg-surface2 p-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'rounded px-2 py-1 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
            value === o.value ? 'bg-surface font-medium text-text shadow-sm' : 'text-muted hover:text-text',
          )}
        >
          {o.label}
          {o.sub && <span className="ml-1.5 text-2xs text-muted">{o.sub}</span>}
        </button>
      ))}
    </div>
  )
}

function ColorField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const valid = HEX.test(value)
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="relative h-9 w-9 shrink-0 cursor-pointer overflow-hidden rounded-md border border-border" style={{ background: valid ? value : '#ffffff' }}>
        <span className="sr-only">Pick brand colour</span>
        <input
          type="color"
          value={valid ? value : DEFAULT_BRAND_COLOR}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
        />
      </label>
      <Input
        value={value}
        onChange={(e) => {
          const v = e.target.value.trim()
          onChange(v.startsWith('#') ? v.toUpperCase() : `#${v.toUpperCase()}`)
        }}
        maxLength={7}
        className="w-28 font-mono uppercase"
        aria-label="Brand colour hex"
      />
      <div className="flex flex-wrap items-center gap-1.5">
        {BRAND_PRESETS.map((c) => (
          <button
            key={c}
            type="button"
            title={c}
            aria-label={`Use ${c}`}
            onClick={() => onChange(c)}
            className={cn(
              'flex h-6 w-6 items-center justify-center rounded-full border border-black/10 transition-transform hover:scale-110',
              value.toUpperCase() === c && 'ring-2 ring-primary ring-offset-2 ring-offset-surface',
            )}
            style={{ background: c }}
          >
            {value.toUpperCase() === c && <Check className="h-3 w-3" style={{ color: onColor(c) }} />}
          </button>
        ))}
      </div>
    </div>
  )
}

function TimezonePicker({ value, onChange }: { value: string; onChange: (tz: string) => void }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const zones = useMemo(() => timezoneList(), [])
  const now = useMemo(() => new Date(), [open])
  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase().replace(/\s+/g, '_')
    return s ? zones.filter((z) => z.toLowerCase().includes(s) || zoneLabel(z, now).toLowerCase().includes(s)) : zones
  }, [q, zones, now])

  return (
    <Popover.Root open={open} onOpenChange={(o) => { setOpen(o); if (!o) setQ('') }}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="flex h-9 w-full items-center justify-between rounded-md border border-border bg-surface px-3 text-sm text-text hover:bg-surface2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        >
          <span className="truncate">{value.replace(/_/g, ' ')}</span>
          <span className="ml-2 flex items-center gap-2 text-xs text-muted">
            {zoneLabel(value)}
            <ChevronsUpDown className="h-3.5 w-3.5" />
          </span>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          className="z-50 w-[var(--radix-popover-trigger-width)] min-w-[280px] rounded-md border border-border bg-surface p-1 shadow-elevated"
        >
          <div className="flex items-center gap-2 border-b border-border px-2 pb-1.5 pt-1">
            <Search className="h-3.5 w-3.5 text-muted" />
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search city or offset (e.g. Riyadh, GMT+3)"
              className="h-7 w-full bg-transparent text-sm outline-none placeholder:text-muted"
            />
          </div>
          <div role="listbox" className="max-h-72 overflow-y-auto py-1">
            {filtered.length === 0 && <div className="px-2 py-3 text-center text-xs text-muted">No matching timezone</div>}
            {filtered.slice(0, 400).map((z) => (
              <button
                key={z}
                type="button"
                role="option"
                aria-selected={z === value}
                onClick={() => { onChange(z); setOpen(false); setQ('') }}
                className={cn(
                  'flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-sm hover:bg-surface2',
                  z === value && 'bg-primary/10 text-primary',
                )}
              >
                <span className="truncate">{z.replace(/_/g, ' ')}</span>
                <span className="ml-2 shrink-0 text-xs text-muted">{zoneLabel(z, now)}</span>
              </button>
            ))}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

function LogoSlot({ variant, meta, title, hint }: { variant: LogoVariant; meta: LogoMeta; title: string; hint: string }) {
  const qc = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const dark = variant === 'dark'

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['settings', 'company'] })
    qc.invalidateQueries({ queryKey: ['reports', 'branding'] })
  }
  const upload = useMutation({
    mutationFn: async (file: File) => {
      const fd = new FormData()
      fd.append('file', file)
      return (await api.post(`/settings/company/logo?variant=${variant}`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })).data
    },
    onSuccess: () => { toast.success('Logo uploaded'); refresh() },
    onError: (e: any) => toast.error('Upload failed', apiErrorMessage(e)),
  })
  const remove = useMutation({
    mutationFn: async () => (await api.delete(`/settings/company/logo?variant=${variant}`)).data,
    onSuccess: () => { toast.success('Logo removed'); setConfirm(false); refresh() },
    onError: (e: any) => toast.error('Could not remove logo', apiErrorMessage(e)),
  })

  const accept = (file?: File | null) => {
    if (!file) return
    if (file.type && !LOGO_TYPES.includes(file.type)) {
      toast.error('Unsupported file', 'Upload a PNG, SVG, JPEG or WebP image.')
      return
    }
    if (file.size > LOGO_MAX) {
      toast.error('File too large', 'The logo must be 2 MB or smaller.')
      return
    }
    upload.mutate(file)
  }

  return (
    <div className="flex flex-col rounded-lg border border-border">
      <div
        role="button"
        tabIndex={0}
        aria-label={`${meta ? 'Replace' : 'Upload'} ${title.toLowerCase()}`}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.current?.click() } }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); accept(e.dataTransfer.files?.[0]) }}
        className={cn(
          'relative flex h-32 cursor-pointer items-center justify-center rounded-t-lg border-b border-border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
          dark ? 'bg-[#0b1220]' : 'bg-white',
          dragging && 'ring-2 ring-primary',
        )}
        style={dark ? undefined : { backgroundImage: 'radial-gradient(#e2e8f0 1px, transparent 1px)', backgroundSize: '12px 12px' }}
      >
        {upload.isPending ? (
          <Loader2 className={cn('h-6 w-6 animate-spin', dark ? 'text-white/70' : 'text-slate-400')} />
        ) : meta ? (
          <img src={logoUrl(variant, meta.updated_at, false)} alt={`${title} preview`} className="max-h-20 max-w-[85%] object-contain" />
        ) : (
          <div className={cn('flex flex-col items-center gap-1.5 text-center text-xs', dark ? 'text-white/60' : 'text-slate-500')}>
            <Upload className="h-5 w-5" />
            <span><span className="font-medium">Click to upload</span> or drag and drop</span>
            <span className="text-2xs opacity-80">PNG · SVG · JPEG · WebP · max 2 MB</span>
          </div>
        )}
        <input
          ref={input}
          type="file"
          accept={LOGO_TYPES.join(',')}
          className="hidden"
          onChange={(e) => { accept(e.target.files?.[0]); e.target.value = '' }}
        />
      </div>
      <div className="flex items-start justify-between gap-2 p-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">{title}</p>
          <p className="mt-0.5 text-xs text-muted">
            {meta ? <>{meta.filename || 'Uploaded image'} · {fmtBytes(meta.size)} · {meta.content_type.replace('image/', '').replace('+xml', '').toUpperCase()}</> : hint}
          </p>
        </div>
        <div className="flex shrink-0 gap-1">
          <Button type="button" size="sm" variant="outline" onClick={() => input.current?.click()} disabled={upload.isPending}>
            {meta ? 'Replace' : 'Upload'}
          </Button>
          {meta && (
            <Button type="button" size="icon" variant="ghost" className="h-8 w-8 text-muted hover:text-danger" aria-label={`Remove ${title.toLowerCase()}`} onClick={() => setConfirm(true)}>
              <Trash2 />
            </Button>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Remove ${title.toLowerCase()}?`}
        description="Reports generated after this will no longer show it."
        confirmText="Remove"
        destructive
        loading={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  )
}

function BrandPreview({ form, logos }: { form: CompanyForm; logos: Record<LogoVariant, LogoMeta> }) {
  const color = HEX.test(form.brand_color) ? form.brand_color : DEFAULT_BRAND_COLOR
  const name = form.company_name.trim() || 'ZenPlus'
  const to = new Date()
  const from = new Date(to.getTime() - 7 * 86400_000)
  const coverLogo = logos.dark ? logoUrl('dark', logos.dark.updated_at, false) : logos.light ? logoUrl('light', logos.light.updated_at, false) : null
  const pageLogo = logos.light ? logoUrl('light', logos.light.updated_at, false) : logos.dark ? logoUrl('dark', logos.dark.updated_at, false) : null
  const contact = [form.company_website, form.company_email, form.company_phone].filter(Boolean).join(' · ')
  const aspect = form.paper_size === 'Letter' ? 'aspect-[8.5/11]' : 'aspect-[210/297]'

  return (
    <div className="grid grid-cols-2 gap-3">
      {/* Cover */}
      <div className={cn('relative flex flex-col overflow-hidden rounded-md bg-[#0b1220] p-3 text-white shadow-elevated', aspect)}>
        <div className="absolute inset-x-0 top-0 h-1" style={{ background: color }} />
        <div className="flex items-center gap-1.5">
          {coverLogo ? <img src={coverLogo} alt="" className="h-4 max-w-[70%] object-contain" /> : <ShieldCheck className="h-3.5 w-3.5" style={{ color }} />}
          {!coverLogo && <span className="truncate text-[7px] font-semibold">{name}</span>}
        </div>
        <div className="mt-auto">
          <p className="text-[5.5px] font-semibold uppercase tracking-widest" style={{ color }}>Operations</p>
          <p className="mt-0.5 text-2xs font-semibold leading-tight">Availability &amp; SLA Report</p>
          <p className="mt-1 text-[5.5px] text-white/60">{formatPeriod(from, to, form)}</p>
        </div>
        <div className="mt-3 flex items-end justify-between border-t border-white/10 pt-1.5 text-[5px] text-white/50">
          <span className="truncate">{name}</span>
          {form.confidentiality && (
            <span className="rounded px-1 py-px font-semibold uppercase tracking-wider" style={{ background: color, color: onColor(color) }}>
              {form.confidentiality}
            </span>
          )}
        </div>
      </div>

      {/* Content page */}
      <div className={cn('flex flex-col overflow-hidden rounded-md border border-slate-200 bg-white p-2.5 text-slate-800 shadow-elevated', aspect)}>
        <div className="flex items-center justify-between border-b pb-1" style={{ borderColor: color }}>
          {pageLogo ? <img src={pageLogo} alt="" className="h-3 max-w-[50%] object-contain" /> : <span className="text-[6px] font-semibold">{name}</span>}
          <span className="text-[5px] text-slate-500">Availability &amp; SLA Report</span>
        </div>
        <p className="mt-1.5 text-[5px] font-semibold uppercase tracking-wider" style={{ color }}>01 · Availability summary</p>
        <div className="mt-1 grid grid-cols-3 gap-1">
          {['99.95%', '42', '3'].map((v, i) => (
            <div key={i} className="rounded-sm border border-slate-200 p-1">
              <div className="h-px w-3" style={{ background: color }} />
              <p className="mt-0.5 text-[7px] font-semibold">{v}</p>
            </div>
          ))}
        </div>
        <div className="mt-1.5 flex h-8 items-end gap-px">
          {[6, 8, 7, 9, 5, 8, 9, 7, 8, 9, 6, 9].map((h, i) => (
            <div key={i} className="flex-1 rounded-t-[1px]" style={{ height: `${h * 10}%`, background: color, opacity: 0.35 + h / 20 }} />
          ))}
        </div>
        <div className="mt-1.5 space-y-0.5">
          {[0, 1, 2, 3].map((i) => <div key={i} className="h-1 rounded-sm bg-slate-100" />)}
        </div>
        <div className="mt-auto border-t border-slate-200 pt-1 text-[4.5px] leading-snug text-slate-500">
          <div className="flex justify-between gap-1">
            <span className="truncate">{form.report_footer || contact || name}</span>
            <span className="shrink-0">Page 2 of 6</span>
          </div>
          <div className="flex justify-between gap-1">
            <span>Generated {formatDate(to, form)} {formatTime(to, form)} {zoneLabel(form.timezone, to)}</span>
            {form.confidentiality && <span className="font-semibold uppercase" style={{ color }}>{form.confidentiality}</span>}
          </div>
        </div>
      </div>
    </div>
  )
}
