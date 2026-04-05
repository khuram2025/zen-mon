import { useState, useCallback, useEffect, Fragment } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import {
  Settings,
  Mail,
  MessageSquare,
  Bell,
  ShieldAlert,
  Plus,
  Trash2,
  Pencil,
  Send,
  Check,
  X,
  AlertCircle,
  AlertTriangle,
  Info,
  Loader2,
  ChevronDown,
  ChevronRight,
  Globe,
  Phone,
  Webhook,
  Hash,
  BellRing,
  Inbox,
  Clock,
  Calendar,
  ToggleLeft,
  ToggleRight,
  Zap,
  Eye,
  EyeOff,
  CheckCircle2,
  XCircle,
  Building2,
  Upload,
  MapPin,
  Image,
} from 'lucide-react'

// ─── Types ────────────────────────────────────────────────────────────────────

interface SMTPConfig {
  host: string
  port: number
  username: string
  password: string
  from_email: string
  from_name: string
  encryption: 'tls' | 'ssl' | 'none'
  enabled: boolean
}

interface SMSConfig {
  provider: 'twilio' | 'vonage' | 'custom_http'
  account_sid: string
  auth_token: string
  from_number: string
  api_url: string
  http_method: 'GET' | 'POST'
  content_type: string
  auth_type: 'none' | 'basic' | 'bearer' | 'query_param'
  auth_username: string
  auth_password: string
  auth_token_value: string
  request_template: string
  custom_headers: Record<string, string>
  sender_name: string
  enabled: boolean
}

interface GatewaySettings {
  smtp: SMTPConfig
  sms: SMSConfig
}

interface NotificationChannel {
  id: string
  name: string
  type: 'email' | 'sms' | 'webhook' | 'slack' | 'telegram'
  config: Record<string, string>
  enabled: boolean
  gateway_id: string | null
  gateway_name: string | null
  created_at: string
}

interface Gateway {
  id: string
  name: string
  type: 'smtp' | 'sms'
  config: Record<string, unknown>
  is_default: boolean
  enabled: boolean
}

interface AlertRule {
  id: string
  name: string
  description: string | null
  enabled: boolean
  metric: string
  operator: string
  threshold: number
  duration: number
  device_id: string | null
  group_id: string | null
  severity: 'info' | 'warning' | 'critical'
  notify_channels: string[]
  cooldown: number
  device_type: string | null
  location: string | null
  trigger_on: string
  recovery_alert: boolean
  min_duration: number
  max_repeat: number
  schedule_start: string | null
  schedule_end: string | null
  schedule_days: number[]
  created_at: string
}

interface DeviceGroup {
  id: string
  name: string
}

interface LocationItem {
  location: string
}

// ─── Toast ────────────────────────────────────────────────────────────────────

interface Toast {
  id: number
  type: 'success' | 'error'
  message: string
}

let toastId = 0

function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            'flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg border text-sm font-medium animate-in slide-in-from-right',
            'transition-all duration-300',
            t.type === 'success'
              ? 'bg-green-500/10 border-green-500/30 text-green-400'
              : 'bg-red-500/10 border-red-500/30 text-red-400'
          )}
        >
          {t.type === 'success' ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
          {typeof t.message === 'string' ? t.message : JSON.stringify(t.message)}
          <button onClick={() => onDismiss(t.id)} className="ml-2 opacity-60 hover:opacity-100">
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  )
}

// ─── Shared Components ────────────────────────────────────────────────────────

function Toggle({
  checked,
  onChange,
  size = 'md',
}: {
  checked: boolean
  onChange: (v: boolean) => void
  size?: 'sm' | 'md'
}) {
  const w = size === 'sm' ? 'w-9 h-5' : 'w-11 h-6'
  const dot = size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4'
  const translate = checked ? (size === 'sm' ? 'translate-x-4' : 'translate-x-5') : 'translate-x-1'
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex items-center rounded-full transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/40',
        w,
        checked ? 'bg-[var(--accent)]' : 'bg-[var(--bg-elevated)]'
      )}
    >
      <span className={cn('inline-block rounded-full bg-white transition-transform duration-200', dot, translate)} />
    </button>
  )
}

function Input({
  label,
  type = 'text',
  value,
  onChange,
  placeholder,
  required,
  className,
  disabled,
}: {
  label?: string
  type?: string
  value: string | number
  onChange: (v: string) => void
  placeholder?: string
  required?: boolean
  className?: string
  disabled?: boolean
}) {
  const [showPassword, setShowPassword] = useState(false)
  const isPassword = type === 'password'
  return (
    <div className={cn('space-y-1.5', className)}>
      {label && (
        <label className="block text-xs font-medium text-[var(--text-secondary)]">
          {label}
          {required && <span className="text-red-400 ml-0.5">*</span>}
        </label>
      )}
      <div className="relative">
        <input
          type={isPassword && !showPassword ? 'password' : 'text'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          disabled={disabled}
          className={cn(
            'w-full px-3 py-2 rounded-lg text-sm',
            'bg-[var(--bg-primary)] border border-[var(--bg-elevated)]',
            'text-[var(--text-primary)] placeholder-[var(--text-muted)]',
            'focus:outline-none focus:border-[var(--accent)] focus:ring-1 focus:ring-[var(--accent)]/30',
            'transition-colors duration-150',
            'disabled:opacity-50 disabled:cursor-not-allowed',
            isPassword && 'pr-9'
          )}
        />
        {isPassword && (
          <button
            type="button"
            onClick={() => setShowPassword(!showPassword)}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
          >
            {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
          </button>
        )}
      </div>
    </div>
  )
}

function Select({
  label,
  value,
  onChange,
  options,
  className,
}: {
  label?: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  className?: string
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      {label && <label className="block text-xs font-medium text-[var(--text-secondary)]">{label}</label>}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          'w-full px-3 py-2 rounded-lg text-sm appearance-none',
          'bg-[var(--bg-primary)] border border-[var(--bg-elevated)]',
          'text-[var(--text-primary)]',
          'focus:outline-none focus:border-[var(--accent)] focus:ring-1 focus:ring-[var(--accent)]/30',
          'transition-colors duration-150'
        )}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  )
}

function Button({
  children,
  onClick,
  variant = 'primary',
  size = 'md',
  loading,
  disabled,
  className,
  type = 'button',
}: {
  children: React.ReactNode
  onClick?: () => void
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  size?: 'sm' | 'md'
  loading?: boolean
  disabled?: boolean
  className?: string
  type?: 'button' | 'submit'
}) {
  const base = 'inline-flex items-center justify-center gap-2 font-medium rounded-lg transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 disabled:opacity-50 disabled:cursor-not-allowed'
  const sizes = size === 'sm' ? 'px-3 py-1.5 text-xs' : 'px-4 py-2 text-sm'
  const variants = {
    primary: 'bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)]',
    secondary: 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-elevated)]',
    danger: 'bg-red-500/10 text-red-400 hover:bg-red-500/20',
    ghost: 'text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]',
  }
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || loading}
      className={cn(base, sizes, variants[variant], className)}
    >
      {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
      {children}
    </button>
  )
}

function Badge({ children, color }: { children: React.ReactNode; color: string }) {
  return (
    <span
      className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold uppercase"
      style={{ color, backgroundColor: `${color}20` }}
    >
      {children}
    </span>
  )
}

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5',
        className
      )}
    >
      {children}
    </div>
  )
}

function EmptyState({ icon: Icon, title, description }: { icon: React.ElementType; title: string; description: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="w-14 h-14 rounded-2xl bg-[var(--bg-tertiary)] flex items-center justify-center mb-4">
        <Icon className="w-7 h-7 text-[var(--text-muted)]" />
      </div>
      <h3 className="text-sm font-medium text-[var(--text-secondary)] mb-1">{title}</h3>
      <p className="text-xs text-[var(--text-muted)] max-w-xs">{description}</p>
    </div>
  )
}

// ─── Tab 1: Gateways ─────────────────────────────────────────────────────────

const defaultSMTP: SMTPConfig = {
  host: '',
  port: 587,
  username: '',
  password: '',
  from_email: '',
  from_name: '',
  encryption: 'tls',
  enabled: false,
}

const defaultSMS: SMSConfig = {
  provider: 'custom_http',
  account_sid: '',
  auth_token: '',
  from_number: '',
  api_url: '',
  http_method: 'GET',
  content_type: '',
  auth_type: 'none',
  auth_username: '',
  auth_password: '',
  auth_token_value: '',
  request_template: '',
  custom_headers: {},
  sender_name: '',
  enabled: false,
}

function GatewaysTab({ showToast }: { showToast: (type: 'success' | 'error', msg: string) => void }) {
  const queryClient = useQueryClient()

  const { data: gateways } = useQuery({
    queryKey: ['settings', 'gateways'],
    queryFn: () => api.get<GatewaySettings>('/settings/gateways'),
  })

  const [smtp, setSmtp] = useState<SMTPConfig>(defaultSMTP)
  const [sms, setSms] = useState<SMSConfig>(defaultSMS)

  useEffect(() => {
    if (gateways) {
      setSmtp(gateways.smtp || defaultSMTP)
      setSms(gateways.sms || defaultSMS)
    }
  }, [gateways])

  const smtpSave = useMutation({
    mutationFn: () => api.put('/settings/gateways/smtp', smtp),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings', 'gateways'] })
      showToast('success', 'SMTP settings saved successfully')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to save SMTP settings'),
  })

  const [smtpTestEmail, setSmtpTestEmail] = useState('')
  const smtpTest = useMutation({
    mutationFn: () => api.post('/settings/gateways/smtp/test', { recipient: smtpTestEmail || smtp.from_email || 'test@example.com' }),
    onSuccess: (data: unknown) => {
      const d = data as Record<string, unknown>
      showToast('success', d?.message ? String(d.message) : 'SMTP test sent')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'SMTP test failed'),
  })

  const smsSave = useMutation({
    mutationFn: () => api.put('/settings/gateways/sms', sms),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings', 'gateways'] })
      showToast('success', 'SMS settings saved successfully')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to save SMS settings'),
  })

  const [smsTestNumber, setSmsTestNumber] = useState('')
  const smsTest = useMutation({
    mutationFn: () => api.post('/settings/gateways/sms/test', { recipient: smsTestNumber || sms.from_number || '0000' }),
    onSuccess: (data: unknown) => {
      const d = data as Record<string, unknown>
      showToast('success', d?.message ? String(d.message) : 'SMS test sent')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'SMS test failed'),
  })

  const updateSmtp = <K extends keyof SMTPConfig>(key: K, value: SMTPConfig[K]) =>
    setSmtp((prev) => ({ ...prev, [key]: value }))

  const updateSms = <K extends keyof SMSConfig>(key: K, value: SMSConfig[K]) =>
    setSms((prev) => ({ ...prev, [key]: value }))

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
      {/* SMTP Card */}
      <Card>
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-blue-500/10 flex items-center justify-center">
              <Mail className="w-5 h-5 text-blue-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">SMTP Configuration</h3>
              <p className="text-xs text-[var(--text-muted)]">Email gateway for notifications</p>
            </div>
          </div>
          <Toggle checked={smtp.enabled} onChange={(v) => updateSmtp('enabled', v)} />
        </div>

        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-3">
            <Input
              label="Host"
              value={smtp.host}
              onChange={(v) => updateSmtp('host', v)}
              placeholder="smtp.example.com"
              required
              className="col-span-2"
            />
            <Input
              label="Port"
              type="text"
              value={smtp.port}
              onChange={(v) => updateSmtp('port', parseInt(v) || 0)}
              placeholder="587"
              required
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Username"
              value={smtp.username}
              onChange={(v) => updateSmtp('username', v)}
              placeholder="user@example.com"
            />
            <Input
              label="Password"
              type="password"
              value={smtp.password}
              onChange={(v) => updateSmtp('password', v)}
              placeholder="••••••••"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="From Email"
              value={smtp.from_email}
              onChange={(v) => updateSmtp('from_email', v)}
              placeholder="alerts@example.com"
              required
            />
            <Input
              label="From Name"
              value={smtp.from_name}
              onChange={(v) => updateSmtp('from_name', v)}
              placeholder="ZenPlus Alerts"
            />
          </div>
          <Select
            label="Encryption"
            value={smtp.encryption}
            onChange={(v) => updateSmtp('encryption', v as SMTPConfig['encryption'])}
            options={[
              { value: 'tls', label: 'TLS (recommended)' },
              { value: 'ssl', label: 'SSL' },
              { value: 'none', label: 'None' },
            ]}
          />
        </div>

        <div className="mt-6 pt-4 border-t border-[var(--bg-elevated)] space-y-3">
          <div className="flex items-center gap-2">
            <input
              type="email"
              value={smtpTestEmail}
              onChange={(e) => setSmtpTestEmail(e.target.value)}
              placeholder="Test email e.g. admin@company.com"
              className="flex-1 bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-xs font-mono placeholder:text-[var(--text-muted)]/40"
            />
            <Button
              variant="secondary"
              size="sm"
              onClick={() => smtpTest.mutate()}
              loading={smtpTest.isPending}
              disabled={!smtpTestEmail || !smtp.host}
            >
              <Send className="w-3.5 h-3.5" />
              Test
            </Button>
          </div>
          <div className="flex justify-end">
            <Button
              size="sm"
              onClick={() => smtpSave.mutate()}
              loading={smtpSave.isPending}
            >
              Save
            </Button>
          </div>
        </div>
      </Card>

      {/* SMS Card */}
      <Card>
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-emerald-500/10 flex items-center justify-center">
              <MessageSquare className="w-5 h-5 text-emerald-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">SMS / Message Gateway</h3>
              <p className="text-xs text-[var(--text-muted)]">Twilio, Vonage, or any custom HTTP SMS API</p>
            </div>
          </div>
          <Toggle checked={sms.enabled} onChange={(v) => updateSms('enabled', v)} />
        </div>

        <div className="space-y-4">
          <Select
            label="Provider"
            value={sms.provider}
            onChange={(v) => updateSms('provider', v as SMSConfig['provider'])}
            options={[
              { value: 'twilio', label: 'Twilio' },
              { value: 'vonage', label: 'Vonage' },
              { value: 'custom_http', label: 'Custom HTTP API' },
            ]}
          />

          {/* Twilio / Vonage fields */}
          {(sms.provider === 'twilio' || sms.provider === 'vonage') && (
            <>
              <Input label="Account SID" value={sms.account_sid} onChange={(v) => updateSms('account_sid', v)} placeholder="AC..." />
              <Input label="Auth Token" type="password" value={sms.auth_token} onChange={(v) => updateSms('auth_token', v)} placeholder="••••••••" />
              <Input label="From Number" value={sms.from_number} onChange={(v) => updateSms('from_number', v)} placeholder="+1234567890" />
            </>
          )}

          {/* Custom HTTP API fields */}
          {sms.provider === 'custom_http' && (
            <>
              <Input
                label="API URL"
                value={sms.api_url}
                onChange={(v) => updateSms('api_url', v)}
                placeholder="https://api.smsprovider.com/send"
              />
              <div className="grid grid-cols-2 gap-3">
                <Select
                  label="HTTP Method"
                  value={sms.http_method}
                  onChange={(v) => updateSms('http_method', v as 'GET' | 'POST')}
                  options={[{ value: 'GET', label: 'GET' }, { value: 'POST', label: 'POST' }]}
                />
                <Select
                  label="Content Type"
                  value={sms.content_type}
                  onChange={(v) => updateSms('content_type', v)}
                  options={[
                    { value: '', label: 'Query Parameters (GET)' },
                    { value: 'application/json', label: 'JSON Body' },
                    { value: 'application/x-www-form-urlencoded', label: 'Form URL Encoded' },
                  ]}
                />
              </div>
              <Select
                label="Authentication"
                value={sms.auth_type}
                onChange={(v) => updateSms('auth_type', v as SMSConfig['auth_type'])}
                options={[
                  { value: 'none', label: 'None (credentials in URL/body)' },
                  { value: 'basic', label: 'Basic Auth (username:password)' },
                  { value: 'bearer', label: 'Bearer Token' },
                  { value: 'query_param', label: 'Query Parameter Auth' },
                ]}
              />
              {sms.auth_type === 'basic' && (
                <div className="grid grid-cols-2 gap-3">
                  <Input label="Username" value={sms.auth_username} onChange={(v) => updateSms('auth_username', v)} placeholder="Username" />
                  <Input label="Password" type="password" value={sms.auth_password} onChange={(v) => updateSms('auth_password', v)} placeholder="Password" />
                </div>
              )}
              {sms.auth_type === 'bearer' && (
                <Input label="Bearer Token" type="password" value={sms.auth_token_value} onChange={(v) => updateSms('auth_token_value', v)} placeholder="Token..." />
              )}
              <Input label="Sender Name" value={sms.sender_name} onChange={(v) => updateSms('sender_name', v)} placeholder="ZenPlus" />
              <div>
                <label className="block text-xs font-medium text-[var(--text-muted)] mb-1.5">
                  Request Template
                </label>
                <textarea
                  value={sms.request_template}
                  onChange={(e) => updateSms('request_template', e.target.value)}
                  rows={4}
                  placeholder={'Example for GET query params:\nUserName=MyUser&Password=MyPass&MessageType=text&Recipients={recipients}&SenderName={sender}&MessageText={message}\n\nExample for JSON POST:\n{"to": "{recipients}", "from": "{sender}", "text": "{message}"}'}
                  className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-xs font-mono placeholder:text-[var(--text-muted)]/40 resize-none"
                />
                <p className="text-[10px] text-[var(--text-muted)] mt-1.5 leading-relaxed">
                  Available placeholders: <code className="bg-[var(--bg-tertiary)] px-1 rounded">{'{recipients}'}</code> (comma-separated numbers),
                  <code className="bg-[var(--bg-tertiary)] px-1 rounded ml-1">{'{message}'}</code> (alert text),
                  <code className="bg-[var(--bg-tertiary)] px-1 rounded ml-1">{'{sender}'}</code> (sender name),
                  <code className="bg-[var(--bg-tertiary)] px-1 rounded ml-1">{'{hostname}'}</code>,
                  <code className="bg-[var(--bg-tertiary)] px-1 rounded ml-1">{'{ip_address}'}</code>,
                  <code className="bg-[var(--bg-tertiary)] px-1 rounded ml-1">{'{status}'}</code>
                </p>
              </div>
            </>
          )}
        </div>

        <div className="mt-6 pt-4 border-t border-[var(--bg-elevated)] space-y-3">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={smsTestNumber}
              onChange={(e) => setSmsTestNumber(e.target.value)}
              placeholder="Test number e.g. 00966590964890"
              className="flex-1 bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-xs font-mono placeholder:text-[var(--text-muted)]/40"
            />
            <Button
              variant="secondary"
              size="sm"
              onClick={() => smsTest.mutate()}
              loading={smsTest.isPending}
              disabled={!smsTestNumber || (sms.provider === 'custom_http' ? !sms.api_url : !sms.account_sid)}
            >
              <Send className="w-3.5 h-3.5" />
              Test SMS
            </Button>
          </div>
          <div className="flex justify-end">
            <Button
              size="sm"
              onClick={() => smsSave.mutate()}
              loading={smsSave.isPending}
            >
              Save
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}

// ─── Tab 2: Notification Channels ─────────────────────────────────────────────

const channelTypeIcons: Record<string, React.ElementType> = {
  email: Mail,
  sms: Phone,
  webhook: Globe,
  slack: Hash,
  telegram: Send,
}

const channelTypeColors: Record<string, string> = {
  email: '#3B82F6',
  sms: '#22C55E',
  webhook: '#A855F7',
  slack: '#E74694',
  telegram: '#0EA5E9',
}

const channelTypes = [
  { value: 'email', label: 'Email' },
  { value: 'sms', label: 'SMS' },
  { value: 'webhook', label: 'Webhook' },
  { value: 'slack', label: 'Slack' },
  { value: 'telegram', label: 'Telegram' },
]

function ChannelFormFields({
  type,
  config,
  setConfig,
}: {
  type: string
  config: Record<string, string>
  setConfig: (c: Record<string, string>) => void
}) {
  const update = (key: string, val: string) => setConfig({ ...config, [key]: val })

  switch (type) {
    case 'email':
      return (
        <Input
          label="Recipients"
          value={config.recipients || ''}
          onChange={(v) => update('recipients', v)}
          placeholder="user1@example.com, user2@example.com"
          required
        />
      )
    case 'sms':
      return (
        <Input
          label="Phone Numbers"
          value={config.phone_numbers || ''}
          onChange={(v) => update('phone_numbers', v)}
          placeholder="+1234567890, +0987654321"
          required
        />
      )
    case 'webhook':
      return (
        <div className="space-y-3">
          <Input
            label="URL"
            value={config.url || ''}
            onChange={(v) => update('url', v)}
            placeholder="https://hooks.example.com/notify"
            required
          />
          <Select
            label="Method"
            value={config.method || 'POST'}
            onChange={(v) => update('method', v)}
            options={[
              { value: 'POST', label: 'POST' },
              { value: 'PUT', label: 'PUT' },
              { value: 'GET', label: 'GET' },
            ]}
          />
          <Input
            label="Headers (JSON)"
            value={config.headers || ''}
            onChange={(v) => update('headers', v)}
            placeholder='{"Authorization": "Bearer ..."}'
          />
        </div>
      )
    case 'slack':
      return (
        <div className="space-y-3">
          <Input
            label="Webhook URL"
            value={config.webhook_url || ''}
            onChange={(v) => update('webhook_url', v)}
            placeholder="https://hooks.slack.com/services/..."
            required
          />
          <Input
            label="Channel"
            value={config.channel || ''}
            onChange={(v) => update('channel', v)}
            placeholder="#alerts"
          />
        </div>
      )
    case 'telegram':
      return (
        <div className="space-y-3">
          <Input
            label="Bot Token"
            type="password"
            value={config.bot_token || ''}
            onChange={(v) => update('bot_token', v)}
            placeholder="123456:ABC-DEF..."
            required
          />
          <Input
            label="Chat ID"
            value={config.chat_id || ''}
            onChange={(v) => update('chat_id', v)}
            placeholder="-1001234567890"
            required
          />
        </div>
      )
    default:
      return null
  }
}

function ChannelsTab({ showToast }: { showToast: (type: 'success' | 'error', msg: string) => void }) {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [form, setForm] = useState({
    name: '',
    type: 'email' as string,
    config: {} as Record<string, string>,
    enabled: true,
  })

  const { data: channelsResp } = useQuery({
    queryKey: ['settings', 'channels'],
    queryFn: () => api.get<{ data: NotificationChannel[] }>('/settings/channels'),
  })
  const channels = channelsResp?.data || []

  const { data: gatewaysResp } = useQuery({
    queryKey: ['settings', 'gateways-list'],
    queryFn: () => api.get<{ data: Gateway[] }>('/settings/gateways/list'),
  })
  const gateways = gatewaysResp?.data || []
  const smtpGateways = gateways.filter(g => g.type === 'smtp')
  const smsGateways = gateways.filter(g => g.type === 'sms')

  const saveMutation = useMutation({
    mutationFn: () =>
      editId
        ? api.put(`/settings/channels/${editId}`, form)
        : api.post('/settings/channels', form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings', 'channels'] })
      showToast('success', editId ? 'Channel updated' : 'Channel created')
      resetForm()
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to save channel'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/settings/channels/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings', 'channels'] })
      showToast('success', 'Channel deleted')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to delete channel'),
  })

  const testMutation = useMutation({
    mutationFn: (id: string) => api.post(`/settings/channels/${id}/test`),
    onSuccess: (data: unknown) => {
      const d = data as Record<string, unknown>
      showToast('success', d?.message ? String(d.message) : 'Test notification sent')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Test failed'),
  })

  function resetForm() {
    setShowForm(false)
    setEditId(null)
    setForm({ name: '', type: 'email', config: {}, enabled: true })
  }

  function startEdit(ch: NotificationChannel) {
    setEditId(ch.id)
    setForm({ name: ch.name, type: ch.type, config: { ...ch.config }, enabled: ch.enabled })
    setShowForm(true)
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h3 className="text-sm font-semibold text-[var(--text-primary)]">Notification Channels</h3>
          <p className="text-xs text-[var(--text-muted)] mt-0.5">
            Configure where alert notifications are delivered
          </p>
        </div>
        {!showForm && (
          <Button size="sm" onClick={() => setShowForm(true)}>
            <Plus className="w-3.5 h-3.5" />
            Add Channel
          </Button>
        )}
      </div>

      {/* Add/Edit Form */}
      {showForm && (
        <Card className="mb-6 ring-1 ring-[var(--accent)]/20">
          <div className="flex items-center justify-between mb-4">
            <h4 className="text-sm font-semibold text-[var(--text-primary)]">
              {editId ? 'Edit Channel' : 'New Channel'}
            </h4>
            <button onClick={resetForm} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="space-y-4">
            <Input
              label="Name"
              value={form.name}
              onChange={(v) => setForm((f) => ({ ...f, name: v }))}
              placeholder="e.g. On-Call Team Email"
              required
            />
            <div className="space-y-1.5">
              <label className="block text-xs font-medium text-[var(--text-secondary)]">Type</label>
              <div className="flex gap-2 flex-wrap">
                {channelTypes.map((ct) => {
                  const Icon = channelTypeIcons[ct.value]
                  const active = form.type === ct.value
                  return (
                    <button
                      key={ct.value}
                      onClick={() => setForm((f) => ({ ...f, type: ct.value, config: {} }))}
                      className={cn(
                        'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all duration-150 border',
                        active
                          ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent-hover)]'
                          : 'border-[var(--bg-elevated)] bg-[var(--bg-primary)] text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
                      )}
                    >
                      <Icon className="w-3.5 h-3.5" />
                      {ct.label}
                    </button>
                  )
                })}
              </div>
            </div>
            {/* Gateway selector for email/sms */}
            {(form.type === 'email' || form.type === 'sms') && (
              <Select
                label="Gateway"
                value={form.config.gateway_id || ''}
                onChange={(v) => setForm((f) => ({ ...f, config: { ...f.config, gateway_id: v } }))}
                options={[
                  { value: '', label: 'Default Gateway' },
                  ...(form.type === 'email' ? smtpGateways : smsGateways).map(g => ({
                    value: g.id,
                    label: `${g.name}${g.is_default ? ' (default)' : ''}${!g.enabled ? ' [disabled]' : ''}`,
                  })),
                ]}
              />
            )}
            <ChannelFormFields
              type={form.type}
              config={form.config}
              setConfig={(c) => setForm((f) => ({ ...f, config: c }))}
            />
            <div className="flex items-center gap-2">
              <Toggle
                checked={form.enabled}
                onChange={(v) => setForm((f) => ({ ...f, enabled: v }))}
                size="sm"
              />
              <span className="text-xs text-[var(--text-secondary)]">Enabled</span>
            </div>
          </div>
          <div className="flex justify-end gap-2 mt-5 pt-4 border-t border-[var(--bg-elevated)]">
            <Button variant="secondary" size="sm" onClick={resetForm}>
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={() => saveMutation.mutate()}
              loading={saveMutation.isPending}
              disabled={!form.name}
            >
              {editId ? 'Update' : 'Create'} Channel
            </Button>
          </div>
        </Card>
      )}

      {/* Channel List */}
      {channels.length === 0 && !showForm ? (
        <Card>
          <EmptyState
            icon={BellRing}
            title="No channels configured"
            description="Add notification channels to start receiving alerts via email, SMS, Slack, and more."
          />
        </Card>
      ) : (
        <div className="space-y-2">
          {channels.map((ch) => {
            const Icon = channelTypeIcons[ch.type] || Bell
            const color = channelTypeColors[ch.type] || '#6B7280'
            return (
              <Card key={ch.id} className="!p-4">
                <div className="flex items-center gap-3">
                  <div
                    className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
                    style={{ backgroundColor: `${color}15` }}
                  >
                    <Icon className="w-4 h-4" style={{ color }} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-[var(--text-primary)] truncate">
                        {ch.name}
                      </span>
                      <Badge color={color}>{ch.type}</Badge>
                      {ch.gateway_name && (
                        <Badge color="#8B5CF6">{ch.gateway_name}</Badge>
                      )}
                      {ch.enabled ? (
                        <Badge color="#22C55E">active</Badge>
                      ) : (
                        <Badge color="#6B7280">disabled</Badge>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => testMutation.mutate(ch.id)}
                      loading={testMutation.isPending && testMutation.variables === ch.id}
                    >
                      <Send className="w-3.5 h-3.5" />
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => startEdit(ch)}>
                      <Pencil className="w-3.5 h-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        if (confirm('Delete this channel?')) deleteMutation.mutate(ch.id)
                      }}
                    >
                      <Trash2 className="w-3.5 h-3.5 text-red-400" />
                    </Button>
                  </div>
                </div>
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─── Tab 3: Alert Rules ───────────────────────────────────────────────────────

const triggerOptions = [
  { value: 'down', label: 'Device Goes Down' },
  { value: 'up', label: 'Device Comes Up' },
  { value: 'any', label: 'Any Status Change' },
  { value: 'degraded', label: 'Device Degraded' },
]

const metricOptions = [
  { value: 'ping_status', label: 'Ping Status' },
  { value: 'rtt', label: 'Round-Trip Time (RTT)' },
  { value: 'packet_loss', label: 'Packet Loss' },
  { value: 'jitter', label: 'Jitter' },
]

const operatorOptions = [
  { value: '>', label: '>' },
  { value: '>=', label: '>=' },
  { value: '<', label: '<' },
  { value: '<=', label: '<=' },
  { value: '==', label: '==' },
  { value: '!=', label: '!=' },
]

const scopeOptions = [
  { value: 'all', label: 'All Devices' },
  { value: 'device', label: 'Specific Device' },
  { value: 'group', label: 'Device Group' },
  { value: 'type', label: 'Device Type' },
  { value: 'location', label: 'Location' },
]

const cooldownOptions = [
  { value: '1', label: '1 minute' },
  { value: '5', label: '5 minutes' },
  { value: '10', label: '10 minutes' },
  { value: '30', label: '30 minutes' },
  { value: '60', label: '1 hour' },
]

const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

const defaultRule = {
  name: '',
  description: '',
  trigger_on: 'down',
  metric: 'ping_status',
  operator: '==',
  threshold: 0,
  recovery_alert: false,
  device_id: null as string | null,
  group_id: null as string | null,
  device_type: null as string | null,
  location: null as string | null,
  severity: 'critical' as 'info' | 'warning' | 'critical',
  notify_channels: [] as string[],
  cooldown: 300,
  min_duration: 0,
  max_repeat: 0,
  schedule_start: null as string | null,
  schedule_end: null as string | null,
  schedule_days: [1, 2, 3, 4, 5, 6, 7],
  enabled: true,
  email_subject: '[{severity}] {status}: {rule_name}',
  email_body: '{status_intro}\n\nRule: {rule_name}\nSeverity: {severity}\nDevice: {hostname} ({ip_address})\nStatus: {status}\nGroup: {group}\nLocation: {location}\nType: {device_type}\nMetric: {metric} {operator} {threshold}\nTime: {timestamp}\n\n--\nZenPlus Network Monitoring System',
  sms_template: '[ZenPlus {severity}] {hostname} ({ip_address}) is {status}. Rule: {rule_name}',
  recovery_email_subject: '[{severity}] RESOLVED: {rule_name}',
  recovery_email_body: '',
  recovery_sms_template: '[ZenPlus {severity}] {hostname} ({ip_address}) is {status}. RESOLVED: {rule_name}',
}

function getScopeType(rule: typeof defaultRule): string {
  if (rule.group_id) return 'group'
  if (rule.device_type) return 'type'
  if (rule.location) return 'location'
  if (rule.device_id) return 'device'
  return 'all'
}

interface AlertPreview {
  subject: string
  email_body: string
  sms_body: string
  severity: string
  device_status: string
  sim_hostname: string
  sim_ip: string
}

function PreviewModal({ preview, recovery, onClose }: {
  preview: AlertPreview
  recovery: AlertPreview | null
  onClose: () => void
}) {
  const [tab, setTab] = useState<'email' | 'sms'>('email')
  const [showRecovery, setShowRecovery] = useState(false)
  const current = showRecovery && recovery ? recovery : preview

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] rounded-2xl shadow-2xl w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--bg-elevated)]">
          <div>
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Message Preview</h3>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">How the alert notification will look</p>
          </div>
          <button onClick={onClose} className="p-1 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex items-center gap-1 px-6 pt-4">
          <button onClick={() => setTab('email')}
            className={cn('flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
              tab === 'email' ? 'bg-[var(--accent)]/10 text-[var(--accent)]' : 'text-[var(--text-muted)] hover:text-[var(--text-secondary)]')}>
            <Mail className="w-3.5 h-3.5" /> Email
          </button>
          <button onClick={() => setTab('sms')}
            className={cn('flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
              tab === 'sms' ? 'bg-emerald-500/10 text-emerald-400' : 'text-[var(--text-muted)] hover:text-[var(--text-secondary)]')}>
            <MessageSquare className="w-3.5 h-3.5" /> SMS
          </button>
          <div className="flex-1" />
          {recovery && (
            <button onClick={() => setShowRecovery(!showRecovery)}
              className={cn('flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
                showRecovery ? 'bg-green-500/10 text-green-400' : 'text-[var(--text-muted)] hover:text-[var(--text-secondary)]')}>
              {showRecovery ? 'Showing Recovery' : 'Show Recovery'}
            </button>
          )}
        </div>

        {/* Content */}
        <div className="p-6 overflow-y-auto flex-1">
          {tab === 'email' ? (
            <div className="bg-[var(--bg-primary)] rounded-xl border border-[var(--bg-elevated)] overflow-hidden">
              {/* Email header */}
              <div className="px-4 py-3 border-b border-[var(--bg-elevated)] space-y-1.5">
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-[var(--text-muted)] w-16">Subject:</span>
                  <span className="text-[var(--text-primary)] font-medium">{current.subject}</span>
                </div>
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-[var(--text-muted)] w-16">From:</span>
                  <span className="text-[var(--text-secondary)]">ZenPlus Alerts &lt;alerts@zenplus.io&gt;</span>
                </div>
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-[var(--text-muted)] w-16">To:</span>
                  <span className="text-[var(--text-secondary)]">ops@company.com</span>
                </div>
              </div>
              {/* Email body */}
              <pre className="px-4 py-4 text-xs text-[var(--text-secondary)] whitespace-pre-wrap font-mono leading-relaxed">
                {current.email_body}
              </pre>
            </div>
          ) : (
            <div className="max-w-sm mx-auto">
              {/* Phone mockup */}
              <div className="bg-[var(--bg-primary)] rounded-2xl border border-[var(--bg-elevated)] p-4">
                <div className="flex items-center gap-2 mb-3 pb-3 border-b border-[var(--bg-elevated)]">
                  <div className="w-8 h-8 rounded-full bg-[var(--accent)]/20 flex items-center justify-center">
                    <MessageSquare className="w-4 h-4 text-[var(--accent)]" />
                  </div>
                  <div>
                    <div className="text-xs font-medium text-[var(--text-primary)]">ZenPlus Alerts</div>
                    <div className="text-[10px] text-[var(--text-muted)]">SMS Notification</div>
                  </div>
                </div>
                <div className="bg-[var(--accent)]/5 rounded-xl p-3">
                  <p className="text-sm text-[var(--text-primary)] leading-relaxed">{current.sms_body}</p>
                </div>
                <div className="text-[10px] text-[var(--text-muted)] text-right mt-2">
                  {current.sms_body.length} characters
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function AlertRulesTab({ showToast }: { showToast: (type: 'success' | 'error', msg: string) => void }) {
  const queryClient = useQueryClient()
  const [showPanel, setShowPanel] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [form, setForm] = useState(defaultRule)
  const [scopeType, setScopeType] = useState('all')
  const [scheduleOpen, setScheduleOpen] = useState(false)
  const [templatesOpen, setTemplatesOpen] = useState(false)
  const [previewData, setPreviewData] = useState<{ alert: AlertPreview; recovery: AlertPreview | null } | null>(null)

  const { data: rulesData } = useQuery({
    queryKey: ['alert-rules'],
    queryFn: () => api.get<{ data: AlertRule[] }>('/alert-rules'),
  })
  const rules = rulesData?.data || []

  const { data: channelsData } = useQuery({
    queryKey: ['settings', 'channels'],
    queryFn: () => api.get<{ data: NotificationChannel[] }>('/settings/channels'),
  })
  const channels = channelsData?.data || []

  const { data: groups = [] } = useQuery({
    queryKey: ['devices', 'groups'],
    queryFn: () => api.get<DeviceGroup[]>('/devices/groups'),
  })

  const { data: rawLocations = [] } = useQuery({
    queryKey: ['devices', 'locations'],
    queryFn: () => api.get<string[]>('/devices/locations'),
  })
  const locations: LocationItem[] = rawLocations.map((l: string) => ({ location: l }))

  const saveMutation = useMutation({
    mutationFn: () =>
      editId ? api.put(`/alert-rules/${editId}`, form) : api.post('/alert-rules', form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] })
      showToast('success', editId ? 'Rule updated' : 'Rule created')
      resetPanel()
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to save rule'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/alert-rules/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] })
      showToast('success', 'Rule deleted')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to delete rule'),
  })

  const toggleMutation = useMutation({
    mutationFn: (id: string) => api.post(`/alert-rules/${id}/toggle`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alert-rules'] }),
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to toggle rule'),
  })

  const previewMutation = useMutation({
    mutationFn: (id: string) => api.post<{ alert: AlertPreview; recovery: AlertPreview | null }>(`/alert-rules/${id}/preview`),
    onSuccess: (data) => setPreviewData(data as { alert: AlertPreview; recovery: AlertPreview | null }),
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to load preview'),
  })

  const simulateMutation = useMutation({
    mutationFn: (id: string) => api.post<{ message: string; results: { channel: string; type: string; status: string; detail: string }[] }>(`/alert-rules/${id}/simulate`),
    onSuccess: (data: unknown) => {
      const d = data as { message: string; results: { channel: string; status: string; detail: string }[] }
      const details = d.results.map(r => `${r.channel}: ${r.status}`).join(', ')
      showToast('success', `${d.message}${details ? ` (${details})` : ''}`)
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Simulation failed'),
  })

  function resetPanel() {
    setShowPanel(false)
    setEditId(null)
    setForm(defaultRule)
    setScopeType('all')
    setScheduleOpen(false)
    setTemplatesOpen(false)
  }

  function startEdit(rule: AlertRule) {
    setEditId(rule.id)
    setForm({
      name: rule.name,
      description: rule.description || '',
      trigger_on: rule.trigger_on || 'any',
      metric: rule.metric,
      operator: rule.operator,
      threshold: rule.threshold,
      recovery_alert: rule.recovery_alert,
      device_id: rule.device_id,
      group_id: rule.group_id,
      device_type: rule.device_type,
      location: rule.location,
      severity: rule.severity,
      notify_channels: rule.notify_channels || [],
      cooldown: rule.cooldown,
      min_duration: rule.min_duration,
      max_repeat: rule.max_repeat,
      schedule_start: rule.schedule_start,
      schedule_end: rule.schedule_end,
      schedule_days: rule.schedule_days || [1, 2, 3, 4, 5, 6, 7],
      enabled: rule.enabled,
      email_subject: (rule as any).email_subject || defaultRule.email_subject,
      email_body: (rule as any).email_body || defaultRule.email_body,
      sms_template: (rule as any).sms_template || defaultRule.sms_template,
      recovery_email_subject: (rule as any).recovery_email_subject || defaultRule.recovery_email_subject,
      recovery_email_body: (rule as any).recovery_email_body || '',
      recovery_sms_template: (rule as any).recovery_sms_template || defaultRule.recovery_sms_template,
    })
    setScopeType(rule.group_id ? 'group' : rule.device_type ? 'type' : rule.location ? 'location' : rule.device_id ? 'device' : 'all')
    setShowPanel(true)
  }

  const updateForm = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }))

  const severityConfig = {
    info: { color: '#3B82F6', icon: Info },
    warning: { color: '#EAB308', icon: AlertTriangle },
    critical: { color: '#EF4444', icon: AlertCircle },
  }

  const scopeLabel = (rule: AlertRule) => {
    if (rule.group_id) return 'Group'
    if (rule.device_type) return rule.device_type.charAt(0).toUpperCase() + rule.device_type.slice(1).replace('_',' ')
    if (rule.location) return rule.location
    if (rule.device_id) return 'Device'
    return 'All Devices'
  }
  const triggerLabel = (s: string) => triggerOptions.find((o) => o.value === s)?.label || s

  return (
    <div className="relative">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h3 className="text-sm font-semibold text-[var(--text-primary)]">Alert Rules</h3>
          <p className="text-xs text-[var(--text-muted)] mt-0.5">
            Define conditions that trigger notifications
          </p>
        </div>
        {!showPanel && (
          <Button size="sm" onClick={() => setShowPanel(true)}>
            <Plus className="w-3.5 h-3.5" />
            Add Rule
          </Button>
        )}
      </div>

      {/* Rules List */}
      {rules.length === 0 && !showPanel ? (
        <Card>
          <EmptyState
            icon={ShieldAlert}
            title="No alert rules"
            description="Create alert rules to get notified when devices go down, metrics exceed thresholds, and more."
          />
        </Card>
      ) : (
        <div className="space-y-2 mb-6">
          {rules.map((rule) => {
            const sev = severityConfig[rule.severity]
            return (
              <Card key={rule.id} className="!p-4">
                <div className="flex items-center gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-[var(--text-primary)]">
                        {rule.name}
                      </span>
                      <Badge color={sev.color}>{rule.severity}</Badge>
                      <Badge color="#6366F1">{scopeLabel(rule)}</Badge>
                      <span className="text-[10px] text-[var(--text-muted)] bg-[var(--bg-tertiary)] px-2 py-0.5 rounded">
                        {triggerLabel(rule.trigger_on)}
                      </span>
                    </div>
                    {rule.description && (
                      <p className="text-xs text-[var(--text-muted)] mt-1 truncate">{rule.description}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-1">
                    <Button variant="ghost" size="sm" onClick={() => previewMutation.mutate(rule.id)}
                      title="Preview message">
                      <Eye className="w-3.5 h-3.5" />
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => {
                      if (confirm(`Send test notification for "${rule.name}" to all configured channels?`))
                        simulateMutation.mutate(rule.id)
                    }} title="Simulate alert">
                      <Zap className="w-3.5 h-3.5 text-amber-400" />
                    </Button>
                    <Toggle
                      checked={rule.enabled}
                      onChange={() => toggleMutation.mutate(rule.id)}
                      size="sm"
                    />
                    <Button variant="ghost" size="sm" onClick={() => startEdit(rule)}>
                      <Pencil className="w-3.5 h-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        if (confirm('Delete this rule?')) deleteMutation.mutate(rule.id)
                      }}
                    >
                      <Trash2 className="w-3.5 h-3.5 text-red-400" />
                    </Button>
                  </div>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {/* Slide-Out Panel */}
      {showPanel && (
        <>
          {/* Backdrop */}
          <div
            className="fixed inset-0 bg-black/40 z-40 transition-opacity"
            onClick={resetPanel}
          />
          {/* Panel */}
          <div className="fixed right-0 top-0 h-full w-full max-w-xl bg-[var(--bg-secondary)] border-l border-[var(--bg-elevated)] z-50 overflow-y-auto shadow-2xl animate-in slide-in-from-right duration-300">
            <div className="sticky top-0 bg-[var(--bg-secondary)] border-b border-[var(--bg-elevated)] px-6 py-4 flex items-center justify-between z-10">
              <h3 className="text-base font-semibold text-[var(--text-primary)]">
                {editId ? 'Edit Rule' : 'New Alert Rule'}
              </h3>
              <button onClick={resetPanel} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="px-6 py-5 space-y-6">
              {/* Basic Info */}
              <div className="space-y-4">
                <Input
                  label="Name"
                  value={form.name}
                  onChange={(v) => updateForm('name', v)}
                  placeholder="e.g. Core Router Down Alert"
                  required
                />
                <Input
                  label="Description"
                  value={form.description}
                  onChange={(v) => updateForm('description', v)}
                  placeholder="Alert when core routers become unreachable"
                />
              </div>

              {/* Trigger Section */}
              <div>
                <h4 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3 flex items-center gap-2">
                  <Zap className="w-3.5 h-3.5" />
                  Trigger
                </h4>
                <div className="space-y-3 bg-[var(--bg-primary)] rounded-lg p-4 border border-[var(--bg-elevated)]">
                  <Select
                    label="When"
                    value={form.trigger_on}
                    onChange={(v) => updateForm('trigger_on', v)}
                    options={triggerOptions}
                  />
                  <Select
                    label="Metric"
                    value={form.metric}
                    onChange={(v) => updateForm('metric', v)}
                    options={metricOptions}
                  />
                  {form.metric !== 'ping_status' && (
                    <div className="grid grid-cols-2 gap-3">
                      <Select
                        label="Condition"
                        value={form.operator}
                        onChange={(v) => updateForm('operator', v)}
                        options={operatorOptions}
                      />
                      <Input
                        label="Threshold"
                        type="text"
                        value={form.threshold}
                        onChange={(v) => updateForm('threshold', parseFloat(v) || 0)}
                        placeholder={form.metric === 'rtt' ? '100 (ms)' : form.metric === 'packet_loss' ? '10 (%)' : '50 (ms)'}
                      />
                    </div>
                  )}
                  <div className="flex items-center gap-2 pt-1">
                    <Toggle
                      checked={form.recovery_alert}
                      onChange={(v) => updateForm('recovery_alert', v)}
                      size="sm"
                    />
                    <span className="text-xs text-[var(--text-secondary)]">
                      Also notify when device recovers
                    </span>
                  </div>
                </div>
              </div>

              {/* Scope Section */}
              <div>
                <h4 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3 flex items-center gap-2">
                  <Globe className="w-3.5 h-3.5" />
                  Scope
                </h4>
                <div className="space-y-3 bg-[var(--bg-primary)] rounded-lg p-4 border border-[var(--bg-elevated)]">
                  <Select
                    label="Apply To"
                    value={scopeType}
                    onChange={(v) => {
                      setScopeType(v)
                      updateForm('device_id', null)
                      updateForm('group_id', null)
                      updateForm('device_type', null)
                      updateForm('location', null)
                    }}
                    options={scopeOptions}
                  />
                  {scopeType === 'group' && (
                    <Select
                      label="Device Group"
                      value={form.group_id || ''}
                      onChange={(v) => updateForm('group_id', v || null)}
                      options={[
                        { value: '', label: 'Select a group...' },
                        ...groups.map((g) => ({ value: g.id, label: g.name })),
                      ]}
                    />
                  )}
                  {scopeType === 'location' && (
                    <Select
                      label="Location"
                      value={form.location || ''}
                      onChange={(v) => updateForm('location', v || null)}
                      options={[
                        { value: '', label: 'Select a location...' },
                        ...locations.map((l) => ({ value: l.location, label: l.location })),
                      ]}
                    />
                  )}
                  {scopeType === 'type' && (
                    <Select
                      label="Device Type"
                      value={form.device_type || ''}
                      onChange={(v) => updateForm('device_type', v || null)}
                      options={[
                        { value: '', label: 'Select type...' },
                        { value: 'router', label: 'Router' },
                        { value: 'switch', label: 'Switch' },
                        { value: 'firewall', label: 'Firewall' },
                        { value: 'server', label: 'Server' },
                        { value: 'access_point', label: 'Access Point' },
                        { value: 'printer', label: 'Printer' },
                        { value: 'other', label: 'Other' },
                      ]}
                    />
                  )}
                  {scopeType === 'device' && (
                    <Input
                      label="Device ID"
                      value={form.device_id || ''}
                      onChange={(v) => updateForm('device_id', v || null)}
                      placeholder="Enter device UUID"
                    />
                  )}
                </div>
              </div>

              {/* Notification Section */}
              <div>
                <h4 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3 flex items-center gap-2">
                  <Bell className="w-3.5 h-3.5" />
                  Notification
                </h4>
                <div className="space-y-4 bg-[var(--bg-primary)] rounded-lg p-4 border border-[var(--bg-elevated)]">
                  {/* Severity */}
                  <div className="space-y-1.5">
                    <label className="block text-xs font-medium text-[var(--text-secondary)]">Severity</label>
                    <div className="flex gap-2">
                      {(['info', 'warning', 'critical'] as const).map((sev) => {
                        const cfg = severityConfig[sev]
                        const active = form.severity === sev
                        const SevIcon = cfg.icon
                        return (
                          <button
                            key={sev}
                            onClick={() => updateForm('severity', sev)}
                            className={cn(
                              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold capitalize transition-all duration-150 border',
                              active
                                ? 'border-current'
                                : 'border-transparent bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
                            )}
                            style={active ? { color: cfg.color, backgroundColor: `${cfg.color}15`, borderColor: `${cfg.color}40` } : undefined}
                          >
                            <SevIcon className="w-3.5 h-3.5" />
                            {sev}
                          </button>
                        )
                      })}
                    </div>
                  </div>

                  {/* Channels */}
                  <div className="space-y-1.5">
                    <label className="block text-xs font-medium text-[var(--text-secondary)]">Channels</label>
                    {channels.length === 0 ? (
                      <p className="text-xs text-[var(--text-muted)] italic">
                        No channels configured. Add channels in the Notification Channels tab.
                      </p>
                    ) : (
                      <div className="space-y-1.5">
                        {channels.map((ch) => {
                          const checked = form.notify_channels.includes(ch.id)
                          return (
                            <label
                              key={ch.id}
                              className={cn(
                                'flex items-center gap-2.5 px-3 py-2 rounded-lg cursor-pointer transition-colors',
                                checked ? 'bg-[var(--accent)]/10' : 'hover:bg-[var(--bg-tertiary)]'
                              )}
                            >
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => {
                                  updateForm(
                                    'notify_channels',
                                    checked
                                      ? form.notify_channels.filter((id) => id !== ch.id)
                                      : [...form.notify_channels, ch.id]
                                  )
                                }}
                                className="w-3.5 h-3.5 rounded border-[var(--bg-elevated)] bg-[var(--bg-primary)] text-[var(--accent)] focus:ring-[var(--accent)]/30"
                              />
                              <span className="text-xs text-[var(--text-primary)]">{ch.name}</span>
                              <Badge color={channelTypeColors[ch.type] || '#6B7280'}>{ch.type}</Badge>
                            </label>
                          )
                        })}
                      </div>
                    )}
                  </div>

                  {/* Cooldown / Duration / Repeat */}
                  <div className="grid grid-cols-3 gap-3">
                    <Select
                      label="Cooldown"
                      value={String(form.cooldown)}
                      onChange={(v) => updateForm('cooldown', parseInt(v))}
                      options={cooldownOptions}
                    />
                    <Input
                      label="Min Duration (sec)"
                      type="text"
                      value={form.min_duration}
                      onChange={(v) => updateForm('min_duration', parseInt(v) || 0)}
                      placeholder="0"
                    />
                    <Input
                      label="Max Repeat"
                      type="text"
                      value={form.max_repeat}
                      onChange={(v) => updateForm('max_repeat', parseInt(v) || 0)}
                      placeholder="0 = unlimited"
                    />
                  </div>
                </div>
              </div>

              {/* Schedule Section (collapsible) */}
              <div>
                <button
                  onClick={() => setScheduleOpen(!scheduleOpen)}
                  className="flex items-center gap-2 text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3 hover:text-[var(--text-primary)] transition-colors"
                >
                  {scheduleOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                  <Clock className="w-3.5 h-3.5" />
                  Schedule
                  <span className="text-[var(--text-muted)] normal-case font-normal">(optional)</span>
                </button>
                {scheduleOpen && (
                  <div className="space-y-4 bg-[var(--bg-primary)] rounded-lg p-4 border border-[var(--bg-elevated)] animate-in fade-in duration-200">
                    <div className="grid grid-cols-2 gap-3">
                      <div className="space-y-1.5">
                        <label className="block text-xs font-medium text-[var(--text-secondary)]">Start Time</label>
                        <input
                          type="time"
                          value={form.schedule_start}
                          onChange={(e) => updateForm('schedule_start', e.target.value)}
                          className="w-full px-3 py-2 rounded-lg text-sm bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]"
                        />
                      </div>
                      <div className="space-y-1.5">
                        <label className="block text-xs font-medium text-[var(--text-secondary)]">End Time</label>
                        <input
                          type="time"
                          value={form.schedule_end}
                          onChange={(e) => updateForm('schedule_end', e.target.value)}
                          className="w-full px-3 py-2 rounded-lg text-sm bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]"
                        />
                      </div>
                    </div>
                    <div className="space-y-1.5">
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">Active Days</label>
                      <div className="flex gap-1.5">
                        {dayNames.map((day, i) => {
                          const active = form.schedule_days.includes(i)
                          return (
                            <button
                              key={day}
                              onClick={() =>
                                updateForm(
                                  'schedule_days',
                                  active
                                    ? form.schedule_days.filter((d) => d !== i)
                                    : [...form.schedule_days, i].sort()
                                )
                              }
                              className={cn(
                                'w-10 h-8 rounded-md text-xs font-medium transition-all duration-150',
                                active
                                  ? 'bg-[var(--accent)] text-white'
                                  : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
                              )}
                            >
                              {day}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Message Templates Section (collapsible) */}
              <div>
                <button
                  onClick={() => setTemplatesOpen(!templatesOpen)}
                  className="flex items-center gap-2 text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3 hover:text-[var(--text-primary)] transition-colors"
                >
                  {templatesOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                  <Mail className="w-3.5 h-3.5" />
                  Message Templates
                </button>
                {templatesOpen && (
                  <div className="space-y-4 bg-[var(--bg-primary)] rounded-lg p-4 border border-[var(--bg-elevated)] animate-in fade-in duration-200">
                    <div className="bg-[var(--accent)]/5 rounded-lg p-3 border border-[var(--accent)]/10">
                      <p className="text-[10px] text-[var(--accent)] font-medium mb-1">Available Variables</p>
                      <div className="flex flex-wrap gap-1">
                        {['{hostname}','{ip_address}','{status}','{severity}','{rule_name}','{group}','{location}','{device_type}','{metric}','{operator}','{threshold}','{timestamp}','{duration}','{rtt}','{packet_loss}','{status_intro}'].map(v => (
                          <code key={v} className="text-[10px] bg-[var(--bg-secondary)] px-1.5 py-0.5 rounded text-[var(--text-secondary)] cursor-pointer hover:text-[var(--accent)]"
                            onClick={() => navigator.clipboard.writeText(v)}
                            title="Click to copy">{v}</code>
                        ))}
                      </div>
                    </div>

                    <div className="space-y-1.5">
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">Email Subject</label>
                      <input type="text" value={form.email_subject}
                        onChange={(e) => updateForm('email_subject', e.target.value)}
                        className="w-full px-3 py-2 rounded-lg text-xs font-mono bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]" />
                    </div>

                    <div className="space-y-1.5">
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">Email Body</label>
                      <textarea value={form.email_body}
                        onChange={(e) => updateForm('email_body', e.target.value)}
                        rows={6}
                        className="w-full px-3 py-2 rounded-lg text-xs font-mono bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)] resize-none" />
                    </div>

                    <div className="space-y-1.5">
                      <label className="block text-xs font-medium text-[var(--text-secondary)]">SMS Message</label>
                      <textarea value={form.sms_template}
                        onChange={(e) => updateForm('sms_template', e.target.value)}
                        rows={2}
                        className="w-full px-3 py-2 rounded-lg text-xs font-mono bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)] resize-none" />
                      <p className="text-[10px] text-[var(--text-muted)]">{form.sms_template.length} characters (160 max for single SMS)</p>
                    </div>

                    {form.recovery_alert && (
                      <>
                        <div className="border-t border-[var(--bg-elevated)] pt-3 mt-3">
                          <p className="text-[10px] font-semibold text-green-400 uppercase tracking-wider mb-3">Recovery Templates</p>
                        </div>
                        <div className="space-y-1.5">
                          <label className="block text-xs font-medium text-[var(--text-secondary)]">Recovery Email Subject</label>
                          <input type="text" value={form.recovery_email_subject}
                            onChange={(e) => updateForm('recovery_email_subject', e.target.value)}
                            className="w-full px-3 py-2 rounded-lg text-xs font-mono bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]" />
                        </div>
                        <div className="space-y-1.5">
                          <label className="block text-xs font-medium text-[var(--text-secondary)]">Recovery SMS Message</label>
                          <textarea value={form.recovery_sms_template}
                            onChange={(e) => updateForm('recovery_sms_template', e.target.value)}
                            rows={2}
                            className="w-full px-3 py-2 rounded-lg text-xs font-mono bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)] resize-none" />
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>

              {/* Enable Toggle */}
              <div className="flex items-center gap-2">
                <Toggle
                  checked={form.enabled}
                  onChange={(v) => updateForm('enabled', v)}
                  size="sm"
                />
                <span className="text-xs text-[var(--text-secondary)]">Rule enabled</span>
              </div>
            </div>

            {/* Panel Footer */}
            <div className="sticky bottom-0 bg-[var(--bg-secondary)] border-t border-[var(--bg-elevated)] px-6 py-4 flex justify-end gap-2">
              <Button variant="secondary" onClick={resetPanel}>
                Cancel
              </Button>
              <Button
                onClick={() => saveMutation.mutate()}
                loading={saveMutation.isPending}
                disabled={!form.name}
              >
                {editId ? 'Update Rule' : 'Create Rule'}
              </Button>
            </div>
          </div>
        </>
      )}

      {/* Preview Modal */}
      {previewData && (
        <PreviewModal
          preview={previewData.alert}
          recovery={previewData.recovery}
          onClose={() => setPreviewData(null)}
        />
      )}
    </div>
  )
}

// ─── Main Settings Page ───────────────────────────────────────────────────────

// ─── Tab: Company Settings ────────────────────────────────────────────────────

interface CompanyConfig {
  company_name: string
  company_address: string
  company_email: string
  company_phone: string
  company_website: string
  timezone: string
  date_format: string
  time_format: string
}

const defaultCompany: CompanyConfig = {
  company_name: '', company_address: '', company_email: '', company_phone: '',
  company_website: '', timezone: 'UTC', date_format: 'YYYY-MM-DD', time_format: '24h',
}

const timezones = [
  'UTC', 'Asia/Riyadh', 'Asia/Dubai', 'Asia/Karachi', 'Asia/Kolkata', 'Asia/Shanghai', 'Asia/Tokyo',
  'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Istanbul',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'Africa/Cairo', 'Africa/Johannesburg', 'Australia/Sydney', 'Pacific/Auckland',
]

function CompanyTab({ showToast }: { showToast: (type: 'success' | 'error', msg: string) => void }) {
  const queryClient = useQueryClient()
  const { data: companyData } = useQuery({
    queryKey: ['company-settings'],
    queryFn: () => api.get<CompanyConfig>('/settings/company'),
  })

  const [company, setCompany] = useState<CompanyConfig>(defaultCompany)
  const [logoPreview, setLogoPreview] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)

  useEffect(() => {
    if (companyData) setCompany(companyData)
  }, [companyData])

  // Try to load logo
  useEffect(() => {
    fetch('/api/v1/settings/company/logo')
      .then(r => { if (r.ok) return r.blob(); throw new Error('no logo') })
      .then(b => setLogoPreview(URL.createObjectURL(b)))
      .catch(() => setLogoPreview(null))
  }, [])

  const saveMutation = useMutation({
    mutationFn: () => api.put('/settings/company', company),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['company-settings'] })
      showToast('success', 'Company settings saved')
    },
    onError: (e: unknown) => showToast('error', e instanceof Error ? e.message : 'Failed to save'),
  })

  const handleLogoUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    if (file.size > 2 * 1024 * 1024) { showToast('error', 'Logo must be less than 2MB'); return }

    setUploading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const token = localStorage.getItem('token')
      const resp = await fetch('/api/v1/settings/company/logo', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body: formData,
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: 'Upload failed' }))
        throw new Error(err.detail || 'Upload failed')
      }
      setLogoPreview(URL.createObjectURL(file))
      showToast('success', 'Logo uploaded successfully')
    } catch (err) {
      showToast('error', err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  const update = <K extends keyof CompanyConfig>(key: K, value: CompanyConfig[K]) =>
    setCompany(prev => ({ ...prev, [key]: value }))

  const inputCls = 'w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--bg-elevated)] focus:border-[var(--accent)] focus:outline-none text-sm'

  return (
    <div className="space-y-6">
      {/* Company Identity */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-8 h-8 rounded-lg bg-[var(--accent)]/10 flex items-center justify-center">
            <Building2 className="w-4 h-4 text-[var(--accent)]" />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Company Information</h3>
            <p className="text-xs text-[var(--text-muted)]">Your organization identity shown across the system</p>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Logo Upload */}
          <div className="flex flex-col items-center">
            <div className="w-32 h-32 rounded-2xl bg-[var(--bg-tertiary)] border-2 border-dashed border-[var(--bg-elevated)] flex items-center justify-center overflow-hidden mb-3">
              {logoPreview ? (
                <img src={logoPreview} alt="Company Logo" className="w-full h-full object-contain p-2" />
              ) : (
                <div className="text-center">
                  <Image className="w-8 h-8 text-[var(--text-muted)] mx-auto mb-1" />
                  <span className="text-[10px] text-[var(--text-muted)]">No logo</span>
                </div>
              )}
            </div>
            <label className={cn(
              'flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-medium cursor-pointer transition-colors',
              uploading ? 'bg-[var(--bg-tertiary)] text-[var(--text-muted)]' : 'bg-[var(--accent)]/10 text-[var(--accent)] hover:bg-[var(--accent)]/20'
            )}>
              {uploading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Upload className="w-3.5 h-3.5" />}
              {uploading ? 'Uploading...' : 'Upload Logo'}
              <input type="file" accept="image/png,image/jpeg,image/svg+xml,image/webp" onChange={handleLogoUpload} className="hidden" />
            </label>
            <span className="text-[10px] text-[var(--text-muted)] mt-1">PNG, JPG, SVG. Max 2MB</span>
          </div>

          {/* Company Details */}
          <div className="lg:col-span-2 grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="sm:col-span-2">
              <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Company Name</label>
              <input className={inputCls} value={company.company_name} onChange={e => update('company_name', e.target.value)} placeholder="Acme Corporation" />
            </div>
            <div className="sm:col-span-2">
              <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Address</label>
              <input className={inputCls} value={company.company_address} onChange={e => update('company_address', e.target.value)} placeholder="123 Main Street, Jeddah, Saudi Arabia" />
            </div>
            <div>
              <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Email</label>
              <input className={inputCls} type="email" value={company.company_email} onChange={e => update('company_email', e.target.value)} placeholder="admin@company.com" />
            </div>
            <div>
              <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Phone</label>
              <input className={inputCls} value={company.company_phone} onChange={e => update('company_phone', e.target.value)} placeholder="+966 12 345 6789" />
            </div>
            <div className="sm:col-span-2">
              <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Website</label>
              <input className={inputCls} value={company.company_website} onChange={e => update('company_website', e.target.value)} placeholder="https://company.com" />
            </div>
          </div>
        </div>
      </div>

      {/* Regional & Display Settings */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-8 h-8 rounded-lg bg-emerald-500/10 flex items-center justify-center">
            <Globe className="w-4 h-4 text-emerald-400" />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">Regional & Display</h3>
            <p className="text-xs text-[var(--text-muted)]">Timezone and format settings applied across the entire system</p>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Timezone</label>
            <select className={inputCls} value={company.timezone} onChange={e => update('timezone', e.target.value)}>
              {timezones.map(tz => <option key={tz} value={tz}>{tz.replace(/_/g, ' ')}</option>)}
            </select>
            <p className="text-[10px] text-[var(--text-muted)] mt-1">All timestamps in the system will use this timezone</p>
          </div>
          <div>
            <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Date Format</label>
            <select className={inputCls} value={company.date_format} onChange={e => update('date_format', e.target.value)}>
              <option value="YYYY-MM-DD">2026-04-05 (ISO)</option>
              <option value="DD/MM/YYYY">05/04/2026 (UK)</option>
              <option value="MM/DD/YYYY">04/05/2026 (US)</option>
              <option value="DD.MM.YYYY">05.04.2026 (EU)</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1.5">Time Format</label>
            <select className={inputCls} value={company.time_format} onChange={e => update('time_format', e.target.value)}>
              <option value="24h">24-hour (14:30)</option>
              <option value="12h">12-hour (2:30 PM)</option>
            </select>
          </div>
        </div>
      </div>

      {/* Save Button */}
      <div className="flex justify-end">
        <button
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending}
          className="flex items-center gap-2 px-6 py-2.5 rounded-lg bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white text-sm font-medium transition-colors disabled:opacity-50"
        >
          {saveMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
          {saveMutation.isPending ? 'Saving...' : 'Save Company Settings'}
        </button>
      </div>
    </div>
  )
}


const tabs = [
  { id: 'company', label: 'Company', icon: Building2, description: 'Organization' },
  { id: 'gateways', label: 'Gateways', icon: Mail, description: 'Email & SMS' },
  { id: 'channels', label: 'Channels', icon: Bell, description: 'Notification' },
  { id: 'rules', label: 'Alert Rules', icon: ShieldAlert, description: 'Triggers' },
] as const

type TabId = (typeof tabs)[number]['id']

export function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>('company')
  const [toasts, setToasts] = useState<Toast[]>([])

  const showToast = useCallback((type: 'success' | 'error', message: unknown) => {
    const id = ++toastId
    const msg = typeof message === 'string' ? message : message instanceof Error ? message.message : JSON.stringify(message)
    setToasts((prev) => [...prev, { id, type, message: msg }])
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000)
  }, [])

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  return (
    <div>
      {/* Page Header */}
      <div className="flex items-center gap-3 mb-6">
        <div className="w-10 h-10 rounded-xl bg-[var(--accent)]/10 flex items-center justify-center">
          <Settings className="w-5 h-5 text-[var(--accent)]" />
        </div>
        <div>
          <h2 className="text-xl font-semibold text-[var(--text-primary)]">Settings</h2>
          <p className="text-xs text-[var(--text-muted)]">Company info, gateways, notification channels, and alert rules</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 bg-[var(--bg-secondary)] rounded-xl p-1 border border-[var(--bg-elevated)] w-fit">
        {tabs.map((t) => {
          const Icon = t.icon
          const active = activeTab === t.id
          return (
            <button
              key={t.id}
              onClick={() => setActiveTab(t.id)}
              className={cn(
                'flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium transition-all duration-200',
                active
                  ? 'bg-[var(--accent)] text-white shadow-lg shadow-[var(--accent)]/20'
                  : 'text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
              )}
            >
              <Icon className="w-4 h-4" />
              <span>{t.label}</span>
              <span className={cn('text-[10px] hidden sm:inline', active ? 'text-white/70' : 'text-[var(--text-muted)]')}>
                {t.description}
              </span>
            </button>
          )
        })}
      </div>

      {/* Tab Content */}
      <div className="transition-all duration-200">
        {activeTab === 'company' && <CompanyTab showToast={showToast} />}
        {activeTab === 'gateways' && <GatewaysTab showToast={showToast} />}
        {activeTab === 'channels' && <ChannelsTab showToast={showToast} />}
        {activeTab === 'rules' && <AlertRulesTab showToast={showToast} />}
      </div>

      {/* Toasts */}
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  )
}
