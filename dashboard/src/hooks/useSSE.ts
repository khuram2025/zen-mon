import { useEffect, useRef, useCallback, useState } from 'react'
import { api } from '@/lib/api'

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

    let cancelled = false
    let eventSource: EventSource | null = null
    let retryTimer: number | undefined
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
      const es = eventSource
      if (!es || es.readyState === EventSource.CLOSED || erroredRef.current) {
        setConnection(es && lastEventRef.current == null ? 'connecting' : 'lost')
      } else if (es.readyState === EventSource.CONNECTING && lastEventRef.current == null) {
        setConnection('connecting')
      } else {
        const last = lastEventRef.current
        setConnection(last != null && Date.now() - last <= STALE_AFTER_MS ? 'live' : 'delayed')
      }
      setLastEventAt(lastEventRef.current)
    }

    const forward = (name: string) => (e: Event) => {
      touch()
      try {
        onMessageRef.current(name, JSON.parse((e as MessageEvent).data))
      } catch { /* ignore parse errors */ }
    }

    // The stream endpoints require authentication, but a browser EventSource
    // cannot send an Authorization header. Exchange the session for a
    // one-minute ticket and put only that in the URL; a dropped connection
    // gets a fresh ticket instead of replaying an expired one.
    const connect = async () => {
      if (cancelled) return
      let ticket: string
      try {
        ticket = (await api.post('/auth/stream-ticket')).data.ticket
      } catch {
        erroredRef.current = true
        setConnection('lost')
        retryTimer = window.setTimeout(connect, 10_000)
        return
      }
      if (cancelled) return
      const sep = url.includes('?') ? '&' : '?'
      const es = new EventSource(`${url}${sep}ticket=${encodeURIComponent(ticket)}`)
      eventSource = es
      eventSourceRef.current = es

      es.onopen = () => {
        erroredRef.current = false
        touch()
        evaluate()
      }
      es.addEventListener('heartbeat', touch)
      es.addEventListener('metric', forward('metric'))
      es.addEventListener('status_change', forward('status_change'))
      es.addEventListener('alert', forward('alert'))
      es.onerror = () => {
        // The browser retries on its own while readyState is CONNECTING, but
        // with the same (soon expired) ticket; close and reconnect with a new one.
        erroredRef.current = true
        setConnection('lost')
        es.close()
        if (!cancelled) retryTimer = window.setTimeout(connect, 3_000)
      }
    }

    void connect()
    const tick = window.setInterval(evaluate, HEALTH_TICK_MS)

    return () => {
      cancelled = true
      window.clearInterval(tick)
      if (retryTimer) window.clearTimeout(retryTimer)
      eventSource?.close()
    }
  }, [url, enabled])

  const close = useCallback(() => {
    eventSourceRef.current?.close()
  }, [])

  return { close, connection, lastEventAt }
}
