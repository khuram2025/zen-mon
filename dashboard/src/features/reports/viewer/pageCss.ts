/**
 * Print page rules for a report: paper size from the organisation's branding,
 * running title and classification in the top margin, footer text and
 * "Page X of Y" in the bottom margin. Pure (types-only imports) so
 * scripts/test-report-render.mjs can load it.
 */
import type { Branding } from '@/lib/branding'

/** A CSS string literal (for @page margin-box content). */
export function cssString(s: string): string {
  return `"${s.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[\r\n]+/g, ' ').slice(0, 160)}"`
}

/** @page rules from the organisation's paper size, footer and classification. */
export function pageCss(b: Pick<Branding, 'paper_size' | 'confidentiality' | 'report_footer' | 'company_name' | 'display_name'>, title: string): string {
  const size = b.paper_size === 'Letter' ? 'letter' : 'A4'
  const left = b.report_footer || b.company_name || b.display_name || ''
  const box = 'font-family: Inter, system-ui, sans-serif; font-size: 8pt; color: rgb(var(--muted));'
  return [
    '@media print {',
    `  @page { size: ${size} portrait; margin: 14mm 13mm 16mm 13mm;`,
    `    @top-left { content: ${cssString(title)}; ${box} }`,
    b.confidentiality ? `    @top-right { content: ${cssString(b.confidentiality.toUpperCase())}; ${box} color: rgb(var(--danger-fg)); font-weight: 700; letter-spacing: 0.08em; }` : '',
    left ? `    @bottom-left { content: ${cssString(left)}; ${box} }` : '',
    `    @bottom-right { content: "Page " counter(page) " of " counter(pages); ${box} }`,
    '  }',
    // The cover carries the title already; keep the classification on every page.
    '  @page :first { @top-left { content: none; } }',
    '}',
  ].filter(Boolean).join('\n')
}
