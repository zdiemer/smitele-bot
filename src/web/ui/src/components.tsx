/**
 * The pieces every view is built from.
 *
 * There is no Card here any more, and that is the point. A card is a box, a box
 * costs horizontal space, and the two games could not line up across boxes —
 * which is the one comparison this page exists to make. `Band` is a card's
 * replacement: a hairline rule, a status character in a 2ch gutter, and content
 * on the shared grid.
 *
 * State is a character first and a colour second, everywhere. `Mark` renders
 * `·` `!` `×` with a colour that agrees, so the page still works screenshotted,
 * projected, printed, or read by someone who cannot separate red from green.
 */

import type { ReactNode } from 'react'
import type { Health } from './format'
import { failed } from './api'
import type { Failed } from './api'

const GLYPH: Record<Health, string> = {
  ok: '·',
  warn: '!',
  bad: '×',
  unknown: '?',
}

const WORD: Record<Health, string> = {
  ok: 'healthy',
  warn: 'behind schedule',
  bad: 'blocked',
  unknown: 'unknown',
}

/**
 * The site's mark, inline.
 *
 * The same shape as `public/icon.svg` and the polygon in `og.py` — three
 * transcriptions of one bolt, which is the cost of wanting a crisp favicon, a
 * Pillow-drawn preview card, and this. Change one and change all three.
 *
 * It carries its own dark tile rather than inheriting the page's ink, so the
 * mark is identical here, in a browser tab and on a preview card instead of
 * being a different colour in each.
 */
export function Bolt({ size = 30 }: { size?: number }) {
  return (
    <svg
      className="bolt"
      width={size}
      height={size}
      viewBox="0 0 64 64"
      aria-hidden="true"
      focusable="false"
    >
      <rect width="64" height="64" rx="12" fill="#14171a" />
      <g strokeLinecap="round" strokeWidth="3.5">
        <path d="M9 20h11" stroke="#4a9ad8" />
        <path d="M6 30h10" stroke="#4a9ad8" opacity=".7" />
        <path d="M11 40h7" stroke="#4a9ad8" opacity=".45" />
        <path d="M46 26h9" stroke="#d06aaa" opacity=".45" />
        <path d="M48 36h10" stroke="#d06aaa" opacity=".7" />
        <path d="M44 46h11" stroke="#d06aaa" />
      </g>
      <path d="M37 7L21 34h10l-4 23 20-31H34z" fill="#ffffff" />
    </svg>
  )
}

export function Mark({ health }: { health: Health }) {
  return (
    <span className={`mark mark-${health}`} title={WORD[health]}>
      <span aria-hidden="true">{GLYPH[health]}</span>
      <span className="sr-only">{WORD[health]}</span>
    </span>
  )
}

/**
 * A labelled register: rule, status gutter, content.
 *
 * `game` recolours the whole band — keyline, label, meters — so which pipeline
 * you are reading is carried by colour and not only by the word at the top of a
 * column. It is an identity, never a status: teal and violet sit off the
 * red-amber-green axis precisely so they cannot be mistaken for one.
 */
export function Band({
  label,
  qualifier,
  health = 'ok',
  game,
  children,
}: {
  label: string
  qualifier?: string
  health?: Health
  game?: 'smite' | 'smite2'
  children: ReactNode
}) {
  return (
    <section>
      <div className={`band${game === 'smite2' ? ' band-smite2' : ''}`}>
        <Mark health={health} />
        <div className="body">
          <h3 className="label">
            {label}
            {qualifier && <span className="qual">{qualifier}</span>}
          </h3>
          {children}
        </div>
      </div>
    </section>
  )
}

/** Two columns that collapse to one — how the games sit side by side. */
export function Pair({ children }: { children: ReactNode }) {
  return <div className="pair">{children}</div>
}

export function Rows({ children }: { children: ReactNode }) {
  return <dl>{children}</dl>
}

export function Row({
  label,
  value,
  unit,
  absent,
  hint,
}: {
  label: string
  value: ReactNode
  unit?: string
  /** Renders dim. For "never", "not recorded" — not for a real zero. */
  absent?: boolean
  hint?: string
}) {
  return (
    <div className="reg">
      <dt>
        {hint ? (
          <span className="hint" title={hint}>
            {label}
          </span>
        ) : (
          label
        )}
      </dt>
      <dd className={absent ? 'absent' : undefined}>
        {value}
        {unit && <span className="unit"> {unit}</span>}
      </dd>
    </div>
  )
}

/** A rule that fills. Not a pill, and never rounded. */
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
  const tone = health === 'ok' ? '' : health === 'warn' ? ' warn' : ' bad'
  return (
    <div className="meter">
      <div
        className="track"
        role="meter"
        aria-valuenow={used}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-label={unit}
      >
        <div className={`fill${tone}`} style={{ width: `${share * 100}%` }} />
      </div>
      <div className="read">
        <b>{used.toLocaleString()}</b> <span className="of">of {limit.toLocaleString()} {unit}</span>
      </div>
    </div>
  )
}

/**
 * A section the snapshot could not produce.
 *
 * Its own thing rather than empty state: "the share was unreachable" and "there
 * is nothing here yet" look identical as a dash, and only one is a problem.
 */
export function SectionError({ of }: { of: Failed }) {
  return (
    <p className="section-error">
      <b>×</b> couldn’t read this — <code>{of.error}</code>
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
  if (value === null || value === undefined) return <Empty>No data yet.</Empty>
  return <>{children(value as T)}</>
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted">{children}</p>
}
