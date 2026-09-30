import { useEffect, useRef, useCallback, useState } from 'react'

interface SSEOptions {
  onMessage: (event: string, data: unknown) => void
  enabled?: boolean
}

/**
 * Stream health, derived from the transport and the server's heartbeat:
 *  - `connecting` — no connection has opened yet
 *  - `live`       — open, and an event or heartbeat arrived recently
 *  - `delayed`    — open, but nothing has arrived for longer than expected
 *  - `lost`       — the browser reports an error and is retrying (or gave up)
 */
export type SSEConnectionState = 'connecting' | 'live' | 'delayed' | 'lost'

/** The status stream emits a heartbeat roughly every second; anything beyond
 *  this is a stalled stream even when the socket still reports open. */
const STALE_AFTER_MS = 15_000
const HEALTH_TICK_MS = 5_000

export function useSSE(url: string, options: SSEOptions) {
  const { onMessage, enabled = true } = options
  const eventSourceRef = useRef<EventSource | null>(null)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  const lastEventRef = useRef<number | null>(null)
  const erroredRef = useRef(false)
  const [connection, setConnection] = useState<SSEConnectionState>('connecting')
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)

  useEffect(() => {
    if (!enabled) return

    const eventSource = new EventSource(url)
    eventSourceRef.current = eventSource
    erroredRef.current = false
    setConnection('connecting')

    const touch = () => {
      lastEventRef.current = Date.now()
      if (erroredRef.current) {
        erroredRef.current = false
        setConnection('live')
      }
    }

    const evaluate = () => {
      if (eventSource.readyState === EventSource.CLOSED || erroredRef.current) {
        setConnection('lost')
      } else if (eventSource.readyState === EventSource.CONNECTING && lastEventRef.current == null) {
        setConnection('connecting')
      } else {
        const last = lastEventRef.current
        setConnection(last != null && Date.now() - last <= STALE_AFTER_MS ? 'live' : 'delayed')
      }
      setLastEventAt(lastEventRef.current)
    }

    eventSource.onopen = () => {
      erroredRef.current = false
      touch()
      evaluate()
    }

    eventSource.addEventListener('heartbeat', touch)

    eventSource.addEventListener('metric', (e) => {
      touch()
      try {
        onMessageRef.current('metric', JSON.parse((e as MessageEvent).data))
      } catch { /* ignore parse errors */ }
    })

    eventSource.addEventListener('status_change', (e) => {
      touch()
      try {
        onMessageRef.current('status_change', JSON.parse((e as MessageEvent).data))
      } catch { /* ignore */ }
    })

    eventSource.addEventListener('alert', (e) => {
      touch()
      try {
        onMessageRef.current('alert', JSON.parse((e as MessageEvent).data))
      } catch { /* ignore */ }
    })

    eventSource.onerror = () => {
      // EventSource auto-reconnects while readyState is CONNECTING; surface
      // the interruption instead of pretending the stream is still live.
      erroredRef.current = true
      setConnection('lost')
    }

    const tick = window.setInterval(evaluate, HEALTH_TICK_MS)

    return () => {
      window.clearInterval(tick)
      eventSource.close()
    }
  }, [url, enabled])

  const close = useCallback(() => {
    eventSourceRef.current?.close()
  }, [])

  return { close, connection, lastEventAt }
}
