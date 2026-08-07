/**
 * The one screen that answers "is anything wrong right now?"
 *
 * Four cards, one per thing that can independently break: each game's data
 * pipeline, the Hi-Rez API, and tracker.gg. Anything that stops a crawl outright
 * is also promoted to a banner above them, because a red card in a grid of four
 * is easy to walk past and a stand-down is the single fact worth interrupting
 * for.
 */

import { Link } from '../router'
import type { GameStatus, Status } from '../api'
import { failed } from '../api'
import {
  aggregateHealth,
  ago,
  corpusHealth,
  count,
  duration,
  quotaHealth,
  worst,
} from '../format'
import type { Health } from '../format'
import { Badge, Card, Dot, Empty, Row, Rows, Section } from '../components'

function gameHealth(game: GameStatus): Health {
  const corpus = failed(game.corpus) ? 'unknown' : corpusHealth(game.corpus.newest)
  const aggregate = failed(game.aggregate)
    ? 'unknown'
    : aggregateHealth(game.aggregate.built)
  return worst(corpus, aggregate)
}

function GameCard({ title, game, to }: { title: string; game: GameStatus; to: string }) {
  return (
    <Card title={title} health={gameHealth(game)}>
      <Section value={game.corpus}>
        {(corpus) => (
          <Rows>
            <Row
              label="Newest day"
              value={corpus.newest?.replace(/^match_details_|\.parquet$/g, '') ?? '—'}
              hint="A day's file lands the morning after that day, so yesterday is the healthy steady state."
            />
            <Row label="Corpus files" value={count(corpus.files)} />
            <Row label="Last written" value={ago(corpus.newest_at)} />
          </Rows>
        )}
      </Section>
      <Section value={game.aggregate}>
        {(aggregate) => (
          <Rows>
            <Row label="Aggregate built" value={aggregate.built ?? 'never'} />
            <Row label="Rows folded in" value={count(aggregate.rows)} />
          </Rows>
        )}
      </Section>
      <p className="muted" style={{ marginBottom: 0 }}>
        <Link to={to}>Details →</Link>
      </p>
    </Card>
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
    : failed(quota)
      ? 'unknown'
      : worst(
          quotaHealth(quota!.requests_today, quota!.requests_limit),
          quotaHealth(quota!.sessions_today, quota!.sessions_limit),
        )

  return (
    <>
      {standdown && (
        <div className="banner">
          <Dot health="bad" />
          <div>
            <strong>
              The Smite 2 crawl is standing down — {duration(standdown.remaining_seconds)}{' '}
              left.
            </strong>
            <span className="reason">{standdown.reason}</span>
          </div>
        </div>
      )}

      <div className="grid">
        {smite && <GameCard title="Smite 1 data" game={smite} to="/data" />}
        {smite2 && <GameCard title="Smite 2 data" game={smite2} to="/data" />}

        <Card title="Hi-Rez API" health={hirezHealth}>
          <Section value={hirez}>
            {(value) => (
              <Section value={value.quota}>
                {(q) => (
                  <Rows>
                    <Row
                      label="Requests today"
                      value={`${count(q.requests_today)} / ${count(q.requests_limit)}`}
                    />
                    <Row
                      label="Sessions today"
                      value={`${count(q.sessions_today)} / ${count(q.sessions_limit)}`}
                    />
                    <Row label="Active sessions" value={count(q.active_sessions)} />
                  </Rows>
                )}
              </Section>
            )}
          </Section>
          <p className="muted" style={{ marginBottom: 0 }}>
            <Link to="/api">Details →</Link>
          </p>
        </Card>

        <Card
          title="tracker.gg"
          health={trackerHealth}
          badge={standdown ? 'Standing down' : clearanceBlocked ? 'Backoff' : undefined}
        >
          <Section value={tracker}>
            {(value) => (
              <Rows>
                <Row label="Egress" value={<code>{value.egress}</code>} />
                <Row
                  label="Stand-down"
                  value={
                    value.standdown.active
                      ? `${duration(value.standdown.remaining_seconds)} left`
                      : 'none'
                  }
                  hint="A WAF refusal with a deadline. The crawl refuses to start inside one."
                />
                <Row
                  label="Solves today"
                  value={`${value.clearance.mints_today} / ${value.clearance.mints_limit}`}
                  hint="Cloudflare challenge solves. A separate budget from the stand-down, and a separate failure."
                />
                <Row
                  label="Cookie age"
                  value={
                    value.clearance.cookie
                      ? duration(value.clearance.cookie.age_seconds)
                      : 'none held'
                  }
                  hint="Measured lifetime is about 6.7 hours."
                />
              </Rows>
            )}
          </Section>
          <p className="muted" style={{ marginBottom: 0 }}>
            <Link to="/api">Details →</Link>
          </p>
        </Card>
      </div>

      {!smite && !smite2 && <Empty>The snapshot has no game data in it yet.</Empty>}

      <h3 className="section">What this is</h3>
      <p className="muted" style={{ maxWidth: '68ch' }}>
        Liveness for the two data pipelines behind{' '}
        <a href="https://github.com/zdiemer/smitele-bot">Smite-le</a>. Smite 1 comes from
        the Hi-Rez API; Smite 2 is crawled from tracker.gg, which is undocumented, behind a
        WAF, and can refuse us in two independent ways. Everything here is read from a
        snapshot written on a schedule — this page never calls either upstream, which is
        why it can be public.{' '}
        <Link to="/docs">The desktop build-advice API</Link> is a design sketch, not
        something you can call yet.
      </p>
      <p className="muted">
        <Badge health="ok" /> healthy · <Badge health="warn" /> behind schedule ·{' '}
        <Badge health="bad" /> blocked or badly stale
      </p>
    </>
  )
}
