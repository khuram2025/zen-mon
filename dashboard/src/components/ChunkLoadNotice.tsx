import { Loader2, RefreshCw } from 'lucide-react'

/**
 * Shown in place of a page whose files could not be downloaded (see lib/chunkRecovery.ts). It names what happened
 * without claiming a cause it cannot know, and its button does the one thing that works: load the page again.
 * `reloading` is the moment between the automatic reload being started and the browser leaving the page.
 */
export function ChunkLoadNotice({ reloading, onReload }: { reloading: boolean; onReload: () => void }) {
  if (reloading) {
    return (
      <div role="status" data-chunk-notice="reloading" className="flex flex-col items-center justify-center gap-3 py-16 text-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary motion-reduce:animate-none" aria-hidden />
        <div className="text-sm font-medium">Loading the current version of this page</div>
        <button type="button" onClick={onReload} className="rounded-md border border-border px-3 py-1.5 text-xs outline-none hover:bg-surface2 focus-visible:ring-2 focus-visible:ring-primary">
          Reload now
        </button>
      </div>
    )
  }
  return (
    <div role="alert" data-chunk-notice="manual" className="mx-auto flex max-w-lg flex-col items-center justify-center gap-3 py-16 text-center">
      <RefreshCw className="h-8 w-8 text-warning" aria-hidden />
      <h2 className="text-sm font-semibold">This page could not be loaded</h2>
      <p className="text-xs text-text2">
        A file this page needs could not be downloaded. That usually means ZenPlus was updated while this tab was open, or the
        connection to the appliance dropped. Reload to get the current version; reloading does not sign you out.
      </p>
      <button
        type="button"
        onClick={onReload}
        className="mt-1 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-on-primary outline-none hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
      >
        <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Reload page
      </button>
    </div>
  )
}
