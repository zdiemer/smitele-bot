/**
 * Formatting, and the thresholds that decide what colour something is.
 *
 * The thresholds live here rather than in the components because they are the
 * opinionated part. "Is this stale?" has no answer without knowing the job's
 * cadence, and the answer differs per job — a fifteen-minute liveness snapshot
 * and a nightly crawl are not late at the same age.
 */

export type Health = 'ok' | 'warn' | 'bad' | 'unknown'

export const HEALTH_ORDER: Record<Health, number> = {
  bad: 3,
  warn: 2,
  unknown: 1,
  ok: 0,
}

/** The worst of several signals — a card is as healthy as its sickest part. */
export function worst(...values: Health[]): Health {
  return values.reduce((a, b) => (HEALTH_ORDER[b] > HEALTH_ORDER[a] ? b : a), 'ok')
}

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return '—'
  const value = Math.max(seconds, 0)
  if (value < MINUTE) return `${Math.round(value)}s`
  if (value < HOUR) return `${Math.round(value / MINUTE)}m`
  if (value < DAY) {
    const hours = Math.floor(value / HOUR)
    const minutes = Math.round((value % HOUR) / MINUTE)
    return minutes ? `${hours}h ${minutes}m` : `${hours}h`
  }
  const days = Math.floor(value / DAY)
  const hours = Math.round((value % DAY) / HOUR)
  return hours ? `${days}d ${hours}h` : `${days}d`
}

export function ago(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return 'never'
  return `${duration(Date.now() / 1000 - epochSeconds)} ago`
}

export function when(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return '—'
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString()
}

export function bytes(value: number | null | undefined): string {
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(size >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** Days between a `YYYY-MM-DD` date and today, or null if unparseable. */
export function daysSince(isoDate: string | null | undefined): number | null {
  if (!isoDate) return null
  const parsed = Date.parse(`${isoDate.slice(0, 10)}T00:00:00Z`)
  if (isNaN(parsed)) return null
  return (Date.now() - parsed) / (DAY * 1000)
}

/**
 * How late a corpus is.
 *
 * A day's file appears the morning after that day — the Smite 1 collector runs
 * at 09:20 UTC for *yesterday* — so "newest is yesterday" is the healthy steady
 * state and one day behind that is normal for most of the morning. Two days is
 * a missed run.
 */
export function corpusHealth(newestDay: string | null | undefined): Health {
  const days = daysSince(newestDay)
  if (days === null) return 'unknown'
  if (days < 2) return 'ok'
  if (days < 4) return 'warn'
  return 'bad'
}

/**
 * How late an aggregate is.
 *
 * Looser than the corpus: the aggregate is a derived roll-up, and a day where
 * it did not rebuild costs freshness in `/build` rather than data.
 */
export function aggregateHealth(built: string | null | undefined): Health {
  const days = daysSince(built)
  if (days === null) return 'unknown'
  if (days < 2) return 'ok'
  if (days < 7) return 'warn'
  return 'bad'
}

/** How late the liveness snapshot itself is, against a 15-minute cadence. */
export function snapshotHealth(staleSeconds: number | null | undefined): Health {
  if (staleSeconds === null || staleSeconds === undefined) return 'unknown'
  if (staleSeconds < 20 * MINUTE) return 'ok'
  if (staleSeconds < 2 * HOUR) return 'warn'
  return 'bad'
}

/** Quota pressure. The caps are daily, so anything under half is unremarkable. */
export function quotaHealth(used: number, limit: number): Health {
  if (!limit) return 'unknown'
  const share = used / limit
  if (share < 0.6) return 'ok'
  if (share < 0.85) return 'warn'
  return 'bad'
}
