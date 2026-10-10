import { Info } from 'lucide-react'

/**
 * Documentation link to the public Zentryc knowledge base, one article per Inventory tab.
 *
 * Same idea as the Device Tracker `KbLink`: articles live on zentryc.com rather than being bundled
 * with the appliance, so they can be corrected without shipping a release.
 */
export const KB_BASE = 'https://zentryc.com/kb/zenplus/inventory'

/**
 * Slug -> path for every Inventory article. Keys are stable; edit paths here only.
 *
 * The first block has one article per screen (`kbArticleForPath` picks it). The second block holds the
 * cross-cutting articles no single screen owns: they are reached from the articles themselves and are
 * valid targets for a link from any screen. The sources of every article are in `inventory-kb/templates`
 * (one file per key; `inventory-kb/README.md` lists the file and the website route for each), so adding a
 * key here means adding its article there.
 */
export const KB_ARTICLES = {
  overview: '/',
  assets: '/assets/',
  'operating-systems': '/operating-systems/',
  software: '/software/',
  hardware: '/hardware/',
  changes: '/changes/',
  settings: '/settings/',
  'getting-started': '/getting-started/',
  'health-and-freshness': '/health-and-freshness/',
  permissions: '/permissions/',
  export: '/export/',
  troubleshooting: '/troubleshooting/',
} as const

export type InventoryKbArticle = keyof typeof KB_ARTICLES

export function inventoryKbUrl(article: InventoryKbArticle): string {
  return `${KB_BASE}${KB_ARTICLES[article]}`
}

/** The article documenting the screen currently on show (the product page belongs to Software). */
export function kbArticleForPath(pathname: string): InventoryKbArticle {
  const path = pathname.replace(/\/+$/, '')
  if (path === '/inventory/assets') return 'assets'
  if (path === '/inventory/os') return 'operating-systems'
  if (path === '/inventory/software' || path.startsWith('/inventory/software/')) return 'software'
  if (path === '/inventory/hardware') return 'hardware'
  if (path === '/inventory/changes') return 'changes'
  if (path === '/inventory/settings') return 'settings'
  return 'overview'
}

export function InventoryKbLink({
  article,
  label,
  className = '',
}: {
  article: InventoryKbArticle
  /** Override the tooltip and accessible name. */
  label?: string
  className?: string
}) {
  const text = label || 'Documentation'
  return (
    <a
      href={inventoryKbUrl(article)}
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
