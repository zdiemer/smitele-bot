/**
 * The handful of pieces every view is built from.
 *
 * Health is expressed with a colour *and* a word, never colour alone — the
 * whole page is a status board, and a status board that only works if you can
 * distinguish red from green is a status board that does not work.
 */

import type { ReactNode } from 'react'
import type { Health } from './format'
import { failed } from './api'
import type { Failed } from './api'

export function Dot({ health }: { health: Health }) {
  return <span className={`dot dot-${health}`} aria-hidden="true" />
}

const HEALTH_WORD: Record<Health, string> = {
  ok: 'OK',
  warn: 'Late',
  bad: 'Problem',
  unknown: 'Unknown',
}

export function Badge({ health, label }: { health: Health; label?: string }) {
  return (
    <span className={`badge badge-${health}`}>
      <Dot health={health} />
      {label ?? HEALTH_WORD[health]}
    </span>
  )
}

export function Card({
  title,
  health,
  badge,
  children,
  footer,
}: {
  title: string
  health?: Health
  badge?: string
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <section className={`card${health ? ` card-${health}` : ''}`}>
      <header className="card-head">
        <h2>{title}</h2>
        {health && <Badge health={health} label={badge} />}
      </header>
      <div className="card-body">{children}</div>
      {footer && <footer className="card-foot">{footer}</footer>}
    </section>
  )
}

export function Row({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="row">
      <dt>
        {label}
        {hint && <span className="hint" title={hint}>?</span>}
      </dt>
      <dd>{value}</dd>
    </div>
  )
}

export function Rows({ children }: { children: ReactNode }) {
  return <dl className="rows">{children}</dl>
}

/** A meter with its numbers written out, because a bar alone is not a number. */
export function Meter({
  used,
  limit,
  health,
  unit,
}: {
  used: number
  limit: number
  health: Health
  unit: string
}) {
  const share = limit ? Math.min(used / limit, 1) : 0
  return (
    <div className="meter-wrap">
      <div
        className={`meter meter-${health}`}
        role="meter"
        aria-valuenow={used}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-label={unit}
      >
        <span style={{ width: `${share * 100}%` }} />
      </div>
      <div className="meter-legend">
        <strong>{used.toLocaleString()}</strong>
        <span>
          of {limit.toLocaleString()} {unit}
        </span>
      </div>
    </div>
  )
}

/**
 * A section the snapshot could not produce.
 *
 * Rendered as its own thing rather than as empty state: "the share was
 * unreachable" and "there is no data yet" look identical if both render as a
 * dash, and only one of them is somebody's problem.
 */
export function SectionError({ of }: { of: Failed }) {
  return (
    <p className="section-error">
      <Dot health="bad" />
      Couldn’t read this: <code>{of.error}</code>
    </p>
  )
}

/** Render `children` unless the value is a failed section. */
export function Section<T>({
  value,
  children,
}: {
  value: T | Failed | null | undefined
  children: (value: T) => ReactNode
}) {
  if (failed(value)) return <SectionError of={value} />
  if (value === null || value === undefined) return <p className="muted">No data yet.</p>
  return <>{children(value as T)}</>
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted">{children}</p>
}
