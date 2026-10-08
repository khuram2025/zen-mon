import { useEffect } from 'react'
import { flushSync } from 'react-dom'
import type { Branding } from '@/lib/branding'
import { useTheme } from '@/stores/theme'
import { pageCss } from './pageCss'

export { cssString, pageCss } from './pageCss'

/** Page size and running header/footer for printing a report. */
export function ReportPrintStyle({ branding, title }: { branding: Branding; title: string }) {
  return <style>{pageCss(branding, title)}</style>
}

/**
 * While a report is open, print on the light theme whatever the screen theme:
 * the print stylesheet switches every colour token to the light palette, but
 * chart colours are computed in script, so the theme itself is switched
 * synchronously before the browser takes its print snapshot (Ctrl+P too) and
 * restored afterwards.
 */
export function usePrintOnLightTheme() {
  useEffect(() => {
    let switched = false
    const before = () => {
      if (useTheme.getState().theme !== 'dark') return
      switched = true
      flushSync(() => useTheme.getState().set('light'))
    }
    const after = () => {
      if (!switched) return
      switched = false
      useTheme.getState().set('dark')
    }
    window.addEventListener('beforeprint', before)
    window.addEventListener('afterprint', after)
    return () => {
      window.removeEventListener('beforeprint', before)
      window.removeEventListener('afterprint', after)
      after()
    }
  }, [])
}

/** Print the report (the light-theme switch is handled by usePrintOnLightTheme). */
export function printReport() {
  window.print()
}
