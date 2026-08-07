/**
 * Polling, and the three states every view has to render.
 *
 * `stale` is separate from `error` deliberately. A snapshot that has not been
 * refreshed in an hour is not a failed request — the fetch succeeded and the
 * data is real, it is just old — and a page that showed those the same way
 * would hide the single most useful fact this site reports.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

// The snapshot job runs every fifteen minutes and the edge caches for sixty
// seconds, so anything faster than this only re-reads Cloudflare's copy.
const POLL_MS = 30_000

export type Loaded<T> = {
  data: T | null
  error: string | null
  loading: boolean
  refresh: () => void
}

export function useEndpoint<T>(path: string, poll = true): Loaded<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  // So a poll landing after unmount, or after a manual refresh already
  // answered, cannot overwrite newer state with older.
  const generation = useRef(0)

  const load = useCallback(async () => {
    const mine = ++generation.current
    try {
      const response = await fetch(path, { headers: { Accept: 'application/json' } })
      const body = await response.json().catch(() => null)
      if (mine !== generation.current) return
      if (!response.ok) {
        setError(body?.error ?? `${response.status} ${response.statusText}`)
        setData(null)
      } else {
        setData(body as T)
        setError(null)
      }
    } catch (caught) {
      if (mine !== generation.current) return
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      if (mine === generation.current) setLoading(false)
    }
  }, [path])

  useEffect(() => {
    void load()
    if (!poll) return
    const timer = setInterval(() => void load(), POLL_MS)
    return () => clearInterval(timer)
  }, [load, poll])

  return { data, error, loading, refresh: () => void load() }
}
