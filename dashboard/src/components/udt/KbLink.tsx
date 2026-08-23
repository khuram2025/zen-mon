import { Info } from 'lucide-react'

/**
 * Documentation link to the public Zentryc knowledge base.
 *
 * Mirrors the APM `KbLink`: articles live on zentryc.com rather than being
 * bundled with the appliance, so they can be corrected without shipping a
 * release and can be read before a customer has an appliance at all.
 */
export const KB_BASE = 'https://zentryc.com/kb/zenplus/udt'

/** Slug -> path for every UDT article. Keys are stable; edit paths here only. */
export const KB_ARTICLES = {
  overview: '/',
  'getting-started': '/getting-started/',
  endpoints: '/endpoints/',
  activity: '/endpoints/#activity',
  'switch-ports': '/switch-ports/',
  classification: '/classification/',
  'watch-lists': '/watch-lists/',
  'user-logins': '/user-logins/',
  troubleshooting: '/troubleshooting/',
} as const

export type KbArticle = keyof typeof KB_ARTICLES

export function kbUrl(article: KbArticle): string {
  return `${KB_BASE}${KB_ARTICLES[article]}`
}

interface Props {
  article: KbArticle
  /** Override the tooltip/aria text; defaults to a generic phrasing. */
  label?: string
  /** `icon` for a bare affordance next to a heading, `inline` for a text link. */
  variant?: 'icon' | 'inline'
  className?: string
}

export function KbLink({ article, label, variant = 'icon', className = '' }: Props) {
  const text = label || 'Documentation'
  const href = kbUrl(article)

  if (variant === 'inline') {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className={`inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline ${className}`}
      >
        <Info className="h-3.5 w-3.5" />
        {text}
      </a>
    )
  }

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={`${text} — opens the zentryc.com knowledge base`}
      aria-label={`${text} (opens in a new tab)`}
      className={`inline-flex h-6 w-6 items-center justify-center rounded-full border border-border text-muted transition-colors hover:border-primary/50 hover:text-primary ${className}`}
    >
      <Info className="h-3.5 w-3.5" />
    </a>
  )
}
