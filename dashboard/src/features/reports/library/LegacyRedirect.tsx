import { Navigate, useLocation } from 'react-router-dom'

/**
 * The legacy persona pages (/reports/executive, /technical, /business,
 * /inventory, /apm) now open their built-in templates in the viewer. The
 * query string (range, from, to — including the legacy month / last_month /
 * quarter keys the viewer maps) is kept so existing deep links still work.
 */
export function LegacyReportRedirect({ to }: { to: string }) {
  const { search, hash } = useLocation()
  return <Navigate to={`${to}${search}${hash}`} replace />
}
