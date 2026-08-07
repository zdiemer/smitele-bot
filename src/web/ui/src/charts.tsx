/**
 * Two chart forms, both single-series.
 *
 * That is the whole reason there is no categorical palette in here. Every
 * question these answer — how many plays per queue, per god, per role, per day —
 * is *magnitude across categories*, one measure at a time. The category is
 * already named on the row, so painting each bar a different colour would encode
 * nothing that isn't already written down and would spend a palette to do it.
 * Bars take the game's identity colour and nothing else.
 *
 * The identity colours themselves are validated rather than picked by eye: blue
 * and magenta measure ΔE 10.1 under deuteranopia and 22.3 for normal vision,
 * where the teal/violet pair they replaced measured 3.3 and 13.1 — two colours
 * most people cannot tell apart. See the comment in styles.css.
 */

import { useState } from 'react'
import { count } from './format'

export type BarDatum = {
  key: string
  name: string
  plays: number
  win_percent?: number | null
}

/**
 * Horizontal bars, sorted, with every value written out.
 *
 * Direct-labelling all of them rather than a few is right *here* specifically:
 * there are nine to twenty rows, the page is already a readout of label-and-
 * value pairs, and a bar chart nobody can read exact numbers off would be a
 * worse version of the table it replaced. The bar adds the comparison; the
 * number keeps the precision.
 */
export function Bars({
  rows,
  total,
  limit,
  showWinRate = false,
}: {
  rows: BarDatum[]
  /** Denominator for the share, when it differs from the visible rows' sum. */
  total?: number
  limit?: number
  showWinRate?: boolean
}) {
  const shown = limit ? rows.slice(0, limit) : rows
  if (!shown.length) return null

  const max = Math.max(...shown.map((r) => r.plays), 1)
  const denominator = total ?? shown.reduce((sum, r) => sum + r.plays, 0)

  return (
    <div className="bars" role="list">
      {shown.map((row) => {
        const share = denominator ? row.plays / denominator : 0
        return (
          <div
            className="bar-row"
            role="listitem"
            key={row.key}
            title={`${row.name} — ${count(row.plays)} plays (${(share * 100).toFixed(1)}%)${
              showWinRate && row.win_percent != null
                ? ` · ${(row.win_percent * 100).toFixed(1)}% wins`
                : ''
            }`}
          >
            <span className="bar-name">{row.name}</span>
            <span className="bar-track">
              <span
                className="bar-fill"
                style={{ width: `${Math.max((row.plays / max) * 100, 0.6)}%` }}
              />
            </span>
            <span className="bar-value">
              {count(row.plays)}
              {showWinRate && row.win_percent != null && (
                <span className="sub"> · {(row.win_percent * 100).toFixed(1)}%</span>
              )}
            </span>
          </div>
        )
      })}
    </div>
  )
}

export type Point = { date: string; matches: number }

/**
 * Matches per day, as an area with the last point marked.
 *
 * One series, so no legend — the heading names it. The crosshair exists because
 * an area chart without one is a shape you cannot read a number off, and the
 * numbers here (a day that collected 90 matches versus 900) are the point.
 */
export function DailyArea({ points, height = 130 }: { points: Point[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null)

  if (points.length < 2) return null

  const width = 1000 // viewBox units; the SVG scales to its container
  const top = 8
  const bottom = height - 20
  const max = Math.max(...points.map((p) => p.matches), 1)

  const x = (i: number) => (i / (points.length - 1)) * width
  const y = (v: number) => bottom - (v / max) * (bottom - top)

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.matches).toFixed(1)}`).join(' ')
  const area = `${line} L${width},${bottom} L0,${bottom} Z`

  const last = points[points.length - 1]
  const peak = points.reduce((a, b) => (b.matches > a.matches ? b : a))
  const totalMatches = points.reduce((sum, p) => sum + p.matches, 0)
  const active = hover == null ? null : points[hover]

  return (
    <div className="plot">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Matches collected per day across ${points.length} days. Peak ${peak.matches} on ${peak.date}. Most recent ${last.matches} on ${last.date}.`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect()
          const ratio = (event.clientX - box.left) / box.width
          const index = Math.round(ratio * (points.length - 1))
          setHover(Math.min(Math.max(index, 0), points.length - 1))
        }}
      >
        {/* Two guides, not a grid. The shape carries the reading. */}
        <line className="grid-line" x1="0" y1={bottom} x2={width} y2={bottom} />
        <line className="grid-line" x1="0" y1={y(max)} x2={width} y2={y(max)} opacity="0.6" />

        <path className="series-area" d={area} />
        <path className="series-line" d={line} vectorEffect="non-scaling-stroke" />

        {active && (
          <line
            className="crosshair"
            x1={x(hover!)}
            y1={top}
            x2={x(hover!)}
            y2={bottom}
            vectorEffect="non-scaling-stroke"
          />
        )}

        <circle
          className="endpoint"
          cx={x(points.length - 1)}
          cy={y(last.matches)}
          r="7"
          vectorEffect="non-scaling-stroke"
        />
      </svg>

      {active && (
        <div
          className="plot-tip"
          style={{ left: `${(hover! / (points.length - 1)) * 100}%` }}
        >
          {active.date} · {count(active.matches)}
        </div>
      )}

      <div className="plot-legend">
        <span>
          {points[0].date} → {last.date}
        </span>
        <span>
          peak <b>{count(peak.matches)}</b> on {peak.date}
        </span>
        <span>
          <b>{count(totalMatches)}</b> matches over {points.length} days
        </span>
      </div>
    </div>
  )
}
