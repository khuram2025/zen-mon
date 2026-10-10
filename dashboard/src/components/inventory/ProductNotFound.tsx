import { Link } from 'react-router-dom'
import { PackageX } from 'lucide-react'
import { Button } from '@/components/ui/Button'

/**
 * The product endpoints answer 404 for a product that is unknown AND for one with no host in the caller's scope
 * (one body for both), so the page cannot say which: it names both possibilities and offers the way back.
 */
export function ProductNotFound({ productKey }: { productKey: string }) {
  return (
    <div role="status" data-state="not-found" className="mx-auto flex max-w-lg flex-col items-center gap-2 rounded-lg border border-border bg-surface px-6 py-12 text-center shadow-card">
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface2 text-muted">
        <PackageX className="h-6 w-6" aria-hidden />
      </span>
      <h1 className="text-base font-semibold text-text">Product not found</h1>
      <p className="text-sm text-text2">
        There is no software product <code className="rounded bg-surface2 px-1 py-0.5 font-mono text-xs text-text">{productKey}</code> on the servers you can see.
      </p>
      <p className="max-w-md text-xs text-muted">
        It may have been removed from every server, the address may be mistyped, or the servers that have it may be outside your tag scope.
      </p>
      <Button asChild size="sm" variant="outline" className="mt-2">
        <Link to="/inventory/software">Back to the software catalog</Link>
      </Button>
    </div>
  )
}
