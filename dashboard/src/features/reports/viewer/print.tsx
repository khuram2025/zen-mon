import type { Branding } from '@/lib/branding'
import { useTheme } from '@/stores/theme'

/** A CSS string literal (for @page margin-box content). */
export function cssString(s: string): string {
  return `"${s.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[\r\n]+/g, ' ').slice(0, 160)}"`
}

/** @page rules from the organisation's paper size, footer and classification. */
export function pageCss(b: Pick<Branding, 'paper_size' | 'confidentiality' | 'report_footer' | 'company_name' | 'display_name'>, title: string): string {
  const size = b.paper_size === 'Letter' ? 'letter' : 'A4'
  const left = b.report_footer || b.company_name || b.display_name || ''
  const box = 'font-family: Inter, system-ui, sans-serif; font-size: 8pt;'
  return [
    '@media print {',
    `  @page { size: ${size} portrait; margin: 14mm 13mm 16mm 13mm;`,
    `    @top-left { content: ${cssString(title)}; ${box} }`,
    b.confidentiality ? `    @top-right { content: ${cssString(b.confidentiality.toUpperCase())}; ${box} font-weight: 700; letter-spacing: 0.08em; }` : '',
    left ? `    @bottom-left { content: ${cssString(left)}; ${box} }` : '',
    `    @bottom-right { content: "Page " counter(page) " of " counter(pages); ${box} }`,
    '  }',
    '  @page :first { @top-left { content: none; } }',
    '}',
  ].filter(Boolean).join('\n')
}

/** Page size and running header/footer for printing a report. */
export function ReportPrintStyle({ branding, title }: { branding: Branding; title: string }) {
  return <style>{pageCss(branding, title)}</style>
}

/**
 * Print the report on the light theme so charts drawn for the screen read on
 * paper; the screen theme is restored afterwards. (Ctrl+P also works: the
 * print stylesheet switches every token to the light palette.)
 */
export function printReport() {
  const theme = useTheme.getState().theme
  if (theme !== 'dark') {
    window.print()
    return
  }
  let restored = false
  const restore = () => {
    if (restored) return
    restored = true
    useTheme.getState().set('dark')
  }
  useTheme.getState().set('light')
  window.addEventListener('afterprint', restore, { once: true })
  // Let the charts re-render on the light palette before the print snapshot.
  window.setTimeout(() => {
    window.print()
    // Browsers whose print() blocks have fired afterprint by now; others fire it later.
    window.setTimeout(restore, 1000)
  }, 400)
}
