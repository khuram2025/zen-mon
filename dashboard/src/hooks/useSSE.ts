import { useEffect, useRef, useCallback } from 'react'

interface SSEOptions {
  onMessage: (event: string, data: unknown) => void
  enabled?: boolean
}

export function useSSE(url: string, options: SSEOptions) {
  const { onMessage, enabled = true } = options
  const eventSourceRef = useRef<EventSource | null>(null)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  useEffect(() => {
    if (!enabled) return

    const eventSource = new EventSource(url)
    eventSourceRef.current = eventSource

    eventSource.addEventListener('metric', (e) => {
      try {
        onMessageRef.current('metric', JSON.parse(e.data))
      } catch { /* ignore parse errors */ }
    })

    eventSource.addEventListener('status_change', (e) => {
      try {
        onMessageRef.current('status_change', JSON.parse(e.data))
      } catch { /* ignore */ }
    })

    eventSource.addEventListener('alert', (e) => {
      try {
        onMessageRef.current('alert', JSON.parse(e.data))
      } catch { /* ignore */ }
    })

    eventSource.onerror = () => {
      // EventSource auto-reconnects
    }

    return () => {
      eventSource.close()
    }
  }, [url, enabled])

  const close = useCallback(() => {
    eventSourceRef.current?.close()
  }, [])

  return { close }
}
