/**
 * The one screen that answers "is anything wrong right now?"
 *
 * The two games sit in one register rather than two cards, so Smite 1 and Smite
 * 2 read straight across on the same rows — the comparison this page exists to
 * make, and the thing a card grid quietly prevented.
 *
 * An active stand-down takes a full-bleed inverted band above everything, with
 * a live countdown and the Retry-After reason verbatim. It is the only element
 * allowed to shout, which is what makes it legible when it happens.
 */

import { Link } from '../router'
import type { GameStatus, Status } from '../api'
import { failed } from '../api'
import {
  aggregateHealth,
  ago,
  corpusHealth,
  count,
  day,
  duration,
  quotaHealth,
  worst,
} from '../format'
import type { Health } from '../format'
import { Band, Empty, Meter, Pair, Row, Rows, Section } from '../components'

function gameHealth(game: GameStatus | undefined): Health {
  if (!game) return 'unknown'
  const corpus = failed(game.corpus) ? 'unknown' : corpusHealth(game.corpus.newest)
  const aggregate = failed(game.aggregate)
    ? 'unknown'
    : aggregateHealth(game.aggregate.built)
  return worst(corpus, aggregate)
}

function GameColumn({
  title,
  game,
  which,
}: {
  title: string
  game: GameStatus | undefined
  which: 'smite' | 'smite2'
}) {
  if (!game) return null
  const corpus = failed(game.corpus) ? null : game.corpus
  const aggregate = failed(game.aggregate) ? null : game.aggregate
  const crawl = game.crawl && !failed(game.crawl) ? game.crawl : null

  return (
    // Both games share one register so their rows line up, so the colour has to
    // move to the column rather than the band.
    <div className={which === 'smite2' ? 'band-smite2 col' : 'col'}>
      <h4 className="col-head">{title}</h4>
      <Rows>
      <Row label="newest day" value={day(corpus?.newest) ?? 'none'} absent={!corpus?.newest} />
      <Row label="corpus files" value={count(corpus?.files)} />
      <Row label="last written" value={ago(corpus?.newest_at)} />
      <Row
        label="aggregate built"
        value={aggregate?.built ?? 'never'}
        absent={!aggregate?.built}
      />
      <Row
        label="rows folded in"
        value={aggregate?.rows == null ? 'not recorded' : count(aggregate.rows)}
        absent={aggregate?.rows == null}
        hint={
          aggregate?.rows == null
            ? 'The manifest predates row counting, so the count is unknown — which is not the same as zero.'
            : undefined
        }
      />
      {crawl && <Row label="matches collected" value={count(crawl.matches_collected)} />}
      </Rows>
    </div>
  )
}

export default function Overview({ status }: { status: Status }) {
  const smite = status.games?.smite
  const smite2 = status.games?.smite2
  const tracker = status.tracker
  const hirez = status.hirez

  const standdown = !failed(tracker) && tracker.standdown.active ? tracker.standdown : null
  const clearanceBlocked = !failed(tracker) && tracker.clearance.blocked

  const trackerHealth: Health = failed(tracker)
    ? 'unknown'
    : standdown
      ? 'bad'
      : clearanceBlocked
        ? 'warn'
        : 'ok'

  const quota = failed(hirez) ? null : hirez.quota
  const hirezHealth: Health = failed(hirez)
    ? 'unknown'
    : failed(quota) || !quota
      ? 'unknown'
      : worst(
          quotaHealth(quota.requests_today, quota.requests_limit),
          quotaHealth(quota.sessions_today, quota.sessions_limit),
        )

  if (!smite && !smite2) return <Empty>The snapshot has no game data in it yet.</Empty>

  return (
    <>
      {standdown && (
        <div className="refusal">
          <span className="mark" aria-hidden="true">
            ×
          </span>
          <div>
            <h2>
              tracker.gg is refusing us for{' '}
              <span className="countdown">{duration(standdown.remaining_seconds)}</span>
            </h2>
            <div className="reason">{standdown.reason}</div>
            <div className="note">
              The crawl will not start while this is in force. It lifts on its own —
              clearing it early only confirms the ban.
            </div>
          </div>
        </div>
      )}

      <Band
        label="Corpus & aggregate"
        qualifier="both games, side by side"
        health={worst(gameHealth(smite), gameHealth(smite2))}
      >
        <Pair>
          <GameColumn title="Smite 1" game={smite} which="smite" />
          <GameColumn title="Smite 2" game={smite2} which="smite2" />
        </Pair>
        <p className="muted" style={{ marginBottom: 0 }}>
          <Link to="/data">full detail →</Link>
        </p>
      </Band>

      <Band label="Hi-Rez" qualifier="smite 1 · resets daily" health={hirezHealth}>
        <Section value={hirez}>
          {(value) => (
            <Section value={value.quota}>
              {(q) => (
                <>
                  <Meter
                    used={q.requests_today}
                    limit={q.requests_limit}
                    health={quotaHealth(q.requests_today, q.requests_limit)}
                    unit="requests"
                  />
                  <Meter
                    used={q.sessions_today}
                    limit={q.sessions_limit}
                    health={quotaHealth(q.sessions_today, q.sessions_limit)}
                    unit="sessions"
                  />
                </>
              )}
            </Section>
          )}
        </Section>
        <p className="muted" style={{ marginBottom: 0 }}>
          <Link to="/upstreams">server status →</Link>
        </p>
      </Band>

      <Band
        label="tracker.gg"
        qualifier={
          failed(tracker) ? 'smite 2' : `smite 2 · egress ${tracker.egress}`
        }
        health={trackerHealth}
      >
        <Section value={tracker}>
          {(value) => (
            <Pair>
              <Rows>
                <Row
                  label="stand-down"
                  value={
                    value.standdown.active
                      ? `${duration(value.standdown.remaining_seconds)} left`
                      : 'none in force'
                  }
                  absent={!value.standdown.active}
                  hint="A WAF refusal with a deadline. The crawl refuses to start inside one."
                />
                <Row
                  label="solves today"
                  value={value.clearance.mints_today}
                  unit={`of ${value.clearance.mints_limit}`}
                  hint="Cloudflare challenge solves. A separate budget from the stand-down, and a separate failure."
                />
              </Rows>
              <Rows>
                <Row
                  label="cookie age"
                  value={
                    value.clearance.cookie
                      ? duration(value.clearance.cookie.age_seconds)
                      : 'none held'
                  }
                  absent={!value.clearance.cookie}
                  hint="Measured lifetime is about 6.7 hours."
                />
                <Row
                  label="last accepted"
                  value={
                    value.clearance.cookie?.last_ok
                      ? ago(value.clearance.cookie.last_ok)
                      : '—'
                  }
                  absent={!value.clearance.cookie?.last_ok}
                />
              </Rows>
            </Pair>
          )}
        </Section>
        <p className="muted" style={{ marginBottom: 0 }}>
          <Link to="/upstreams">both block signals →</Link>
        </p>
      </Band>

      <Band label="What this is" health="ok">
        <p className="prose">
          Liveness for the two data pipelines behind{' '}
          <a href="https://github.com/zdiemer/smitele-bot">Smite-le</a>. Smite 1 comes
          from the Hi-Rez API; Smite 2 is crawled from tracker.gg, which is undocumented,
          sits behind a WAF, and can refuse us in two independent ways. Everything here is
          read from a snapshot written on a schedule — this page never calls either
          upstream, which is why it can be public.
        </p>
        <p className="prose" style={{ marginBottom: 0 }}>
          <span className="mark mark-ok">·</span> healthy ·{' '}
          <span className="mark mark-warn">!</span> behind schedule ·{' '}
          <span className="mark mark-bad">×</span> blocked or badly stale.
        </p>
      </Band>
    </>
  )
}
