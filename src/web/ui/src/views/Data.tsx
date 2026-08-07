/**
 * Per-game pipeline detail, and what last night's crawl actually did.
 *
 * The crawl report is the part that did not exist before this release.
 * `collect.py` always computed these numbers and printed them into a Job log
 * that outlives the run by about two days; now it writes `last_run.json`, so
 * "did last night work?" survives without kubectl.
 */

import type { GameStatus, LastRun, Status } from '../api'
import { failed } from '../api'
import {
  aggregateHealth,
  ago,
  bytes,
  corpusHealth,
  count,
  day,
  duration,
  percent,
  when,
} from '../format'
import type { Health } from '../format'
import { Band, Empty, Pair, Row, Rows, Section } from '../components'

const REASON_HEALTH: Record<string, Health> = {
  ok: 'ok',
  blocked: 'bad',
  standdown: 'bad',
  no_gods: 'warn',
}

const REASON_WORD: Record<string, string> = {
  ok: 'completed',
  blocked: 'blocked part-way',
  standdown: 'never started',
  no_gods: 'no god catalogue',
}

function GameDetail({ title, game }: { title: string; game: GameStatus }) {
  const corpusMark = failed(game.corpus) ? 'unknown' : corpusHealth(game.corpus.newest)
  const aggMark = failed(game.aggregate)
    ? 'unknown'
    : aggregateHealth(game.aggregate.built)

  return (
    <>
      <Band label={`${title} — corpus`} health={corpusMark}>
        <Section value={game.corpus}>
          {(corpus) => (
            <Pair>
              <Rows>
                <Row label="newest day" value={day(corpus.newest) ?? 'none'} absent={!corpus.newest} />
                <Row label="files" value={count(corpus.files)} />
              </Rows>
              <Rows>
                <Row label="last written" value={ago(corpus.newest_at)} />
                <Row label="size of newest" value={bytes(corpus.newest_bytes)} />
              </Rows>
            </Pair>
          )}
        </Section>
      </Band>

      <Band label={`${title} — aggregate & model`} health={aggMark}>
        <Pair>
          <Section value={game.aggregate}>
            {(aggregate) => (
              <Rows>
                <Row
                  label="built"
                  value={aggregate.built ?? 'never'}
                  absent={!aggregate.built}
                  hint="Date of the last full rebuild, carried across incremental runs."
                />
                <Row
                  label="newest day counted"
                  value={aggregate.newest ?? '—'}
                  absent={!aggregate.newest}
                  hint="What the recency weights are relative to."
                />
                <Row label="files counted" value={count(aggregate.files)} />
                <Row
                  label="rows"
                  value={aggregate.rows == null ? 'not recorded' : count(aggregate.rows)}
                  absent={aggregate.rows == null}
                  hint="Unknown is not zero: a manifest written before row counting reports no count at all."
                />
                {aggregate.unknown_rows ? (
                  <Row
                    label="files with no row count"
                    value={count(aggregate.unknown_rows)}
                  />
                ) : null}
              </Rows>
            )}
          </Section>
          <Section value={game.model}>
            {(model) => (
              <Rows>
                {Object.entries(model).map(([name, file]) => (
                  <Row
                    key={name}
                    label={name}
                    value={file ? `${bytes(file.bytes)} · ${ago(file.at)}` : 'not trained'}
                    absent={!file}
                  />
                ))}
              </Rows>
            )}
          </Section>
        </Pair>
      </Band>

      {game.crawl && (
        <Band label={`${title} — crawl frontier`} health="ok">
          <Section value={game.crawl}>
            {(crawl) => (
              <Pair>
                <Rows>
                  <Row label="matches collected" value={count(crawl.matches_collected)} />
                  <Row label="players known" value={count(crawl.frontier?.players)} />
                </Rows>
                <Rows>
                  <Row
                    label="never queried"
                    value={count(crawl.frontier?.unvisited)}
                    hint="The snowball's backlog: players seen in other people's matches but not yet read."
                  />
                  <Row
                    label="last queried"
                    value={crawl.frontier?.last_queried ?? '—'}
                    absent={!crawl.frontier?.last_queried}
                  />
                </Rows>
              </Pair>
            )}
          </Section>
        </Band>
      )}
    </>
  )
}

function CrawlReport({ run }: { run: LastRun }) {
  const reason = run.exit_reason ?? 'ok'
  const health = REASON_HEALTH[reason] ?? 'unknown'

  if (reason === 'standdown') {
    return (
      <Band
        label="Last crawl"
        qualifier={`never started · ${run.finished ? ago(run.finished) : ''}`}
        health={health}
      >
        <Rows>
          <Row label="refused because" value={run.standdown?.reason ?? '—'} />
          <Row label="time left then" value={duration(run.standdown?.remaining_seconds)} />
        </Rows>
      </Band>
    )
  }

  return (
    <>
      <Band
        label="Last crawl"
        qualifier={`${REASON_WORD[reason] ?? reason}${run.finished ? ` · ${ago(run.finished)}` : ''}`}
        health={health}
      >
        <Pair>
          <Rows>
            <Row label="ran for" value={duration(run.elapsed_seconds)} />
            <Row
              label="requests"
              value={`${count(run.requests)}${run.budget ? ` of ${count(run.budget)}` : ''}`}
              hint="Hitting the budget exactly means the crawl was cut off, not that it finished."
            />
            <Row label="transferred" value={bytes(run.bytes)} />
            <Row label="new matches" value={count(run.new_matches)} />
            <Row label="rows written" value={count(run.rows_written)} />
          </Rows>
          <Rows>
            <Row label="players visited" value={count(run.players_visited)} />
            <Row label="players discovered" value={count(run.players_discovered)} />
            <Row
              label="rate limits"
              value={count(run.rate_limited)}
              hint="A run that was rate limited and recovered otherwise reads exactly like a clean one."
            />
            <Row
              label="finished pacing at"
              value={run.final_interval ? `${run.final_interval.toFixed(2)}s` : '—'}
              absent={!run.final_interval}
            />
            {run.item_slots ? (
              <Row
                label="unnameable items"
                value={`${count(run.unknown_items)} of ${count(run.item_slots)}`}
                hint="Above about 2% means the wiki join needs looking at."
              />
            ) : null}
            {run.coverage_estimate != null && (
              <Row
                label="recent coverage"
                value={percent(run.coverage_estimate)}
                hint="Capture-recapture across two halves of the roster. An upper bound — premades bias it high."
              />
            )}
          </Rows>
        </Pair>
        {run.egress_changed && (
          <p className="section-error">
            <b>!</b> the outbound address changed mid-run. A clearance cookie is bound to
            the address that solved it, so a rotating exit cannot work here.
          </p>
        )}
        {run.frontier && (
          <p className="muted" style={{ marginBottom: 0 }}>
            roster after that run — {count(run.frontier.total)} known ·{' '}
            {count(run.frontier.unvisited)} never queried · {count(run.frontier.dead)}{' '}
            written off · {count(run.frontier.partied)} in a known party
          </p>
        )}
      </Band>

      {run.coverage && run.coverage.length > 0 && (
        <Band label="Coverage by day" qualifier="as of the last crawl" health="ok">
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>day</th>
                  <th>seen</th>
                  <th>half A</th>
                  <th>half B</th>
                  <th>both</th>
                  <th>estimated total</th>
                  <th>coverage</th>
                </tr>
              </thead>
              <tbody>
                {run.coverage.map((row) => (
                  <tr key={row.date}>
                    <td>{row.date}</td>
                    <td>{count(row.seen)}</td>
                    <td>{count(row.half_a)}</td>
                    <td>{count(row.half_b)}</td>
                    <td>{count(row.both)}</td>
                    <td>
                      {row.estimated_total
                        ? Math.round(row.estimated_total).toLocaleString()
                        : '—'}
                    </td>
                    <td>{percent(row.coverage)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted">
            An upper bound: premades break the independence the estimator assumes, which
            biases the total low.
          </p>
        </Band>
      )}
    </>
  )
}

export default function Data({ status }: { status: Status }) {
  const smite = status.games?.smite
  const smite2 = status.games?.smite2
  const lastRun = smite2?.last_run

  if (!smite && !smite2) return <Empty>The snapshot has no game data in it yet.</Empty>

  return (
    <>
      {smite && <GameDetail title="Smite 1" game={smite} />}
      {smite2 && <GameDetail title="Smite 2" game={smite2} />}

      {lastRun === null && (
        <Band label="Last crawl" qualifier="no run recorded yet" health="unknown">
          <p className="prose" style={{ marginBottom: 0 }}>
            The collector writes a record at the end of every night, including nights it
            refuses to start. Nothing here yet — the first run under this build has not
            landed. Last written {when(status.generated_at)}.
          </p>
        </Band>
      )}
      {lastRun && !failed(lastRun) && <CrawlReport run={lastRun} />}
      {lastRun && failed(lastRun) && (
        <Band label="Last crawl" health="unknown">
          <Section value={lastRun}>{() => null}</Section>
        </Band>
      )}
    </>
  )
}
