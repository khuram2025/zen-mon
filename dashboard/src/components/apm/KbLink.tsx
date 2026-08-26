import { BookOpen } from 'lucide-react'

/**
 * Documentation link to the public Zentryc knowledge base.
 *
 * Every APM page carries one, anchored to the article that explains that
 * screen. Articles live on zentryc.com (not bundled with the appliance) so
 * they can be corrected without shipping a release, and so a customer can read
 * them before they have an appliance at all.
 */
export const KB_BASE = 'https://zentryc.com/kb/zenplus/apm'

/** Slug -> path for every APM article. Keys are stable; edit paths here only. */
export const KB_ARTICLES = {
  overview: '/',
  'getting-started': '/getting-started/',
  services: '/services/',
  'service-map': '/service-map/',
  traces: '/traces/',
  errors: '/errors/',
  slos: '/slos/',
  synthetics: '/synthetics/',
  usage: '/usage/',
  rum: '/rum/',
  settings: '/settings/',
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
  /** `icon` for a header affordance, `inline` for a text link inside content. */
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
        <BookOpen className="h-3.5 w-3.5" />
        {text}
      </a>
    )
  }

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={`${text} — opens zentryc.com knowledge base`}
      aria-label={`${text} (opens in a new tab)`}
      className={`inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface2/40 px-2.5 text-xs font-medium text-muted transition-colors hover:border-primary/40 hover:text-primary ${className}`}
    >
      <BookOpen className="h-3.5 w-3.5" />
      <span className="hidden sm:inline">Docs</span>
    </a>
  )
}
