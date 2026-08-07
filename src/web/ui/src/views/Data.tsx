/**
 * Per-game pipeline detail: corpus, aggregate, model, and — for Smite 2 — what
 * last night's crawl actually did.
 *
 * The crawl report is the part that did not exist before. `collect.py` printed
 * these numbers and the Job took them with it, so "did last night work?" had a
 * two-day shelf life and needed kubectl.
 */

import type { GameStatus, LastRun, Status } from '../api'
import { failed } from '../api'
import {
  aggregateHealth,
  ago,
  bytes,
  corpusHealth,
  count,
  duration,
  percent,
  when,
} from '../format'
import type { Health } from '../format'
import { Badge, Card, Empty, Row, Rows, Section } from '../components'

const REASON_HEALTH: Record<string, Health> = {
  ok: 'ok',
  blocked: 'bad',
  standdown: 'bad',
  no_gods: 'warn',
}

const REASON_WORD: Record<string, string> = {
  ok: 'Completed',
  blocked: 'Blocked',
  standdown: 'Never started',
  no_gods: 'No god catalogue',
}

function CrawlReport({ run }: { run: LastRun }) {
  const reason = run.exit_reason ?? 'ok'
  const health = REASON_HEALTH[reason] ?? 'unknown'

  return (
    <Card
      title="Last Smite 2 crawl"
      health={health}
      badge={REASON_WORD[reason] ?? reason}
      footer={
        run.finished ? (
          <>
            Finished {ago(run.finished)} · {when(run.finished)}
          </>
        ) : undefined
      }
    >
      {reason === 'standdown' ? (
        <Rows>
          <Row
            label="Refused because"
            value={<span className="reason">{run.standdown?.reason ?? '—'}</span>}
          />
          <Row
            label="Time left then"
            value={duration(run.standdown?.remaining_seconds)}
          />
        </Rows>
      ) : (
        <>
          <Rows>
            <Row label="Ran for" value={duration(run.elapsed_seconds)} />
            <Row
              label="Requests"
              value={`${count(run.requests)}${run.budget ? ` / ${count(run.budget)}` : ''}`}
              hint="Budget is per night. Hitting it exactly means the crawl was cut off, not that it finished."
            />
            <Row label="Transferred" value={bytes(run.bytes)} />
            <Row label="New matches" value={count(run.new_matches)} />
            <Row label="Rows written" value={count(run.rows_written)} />
          </Rows>
          <Rows>
            <Row label="Players visited" value={count(run.players_visited)} />
            <Row label="Players discovered" value={count(run.players_discovered)} />
            <Row
              label="Rate limits"
              value={count(run.rate_limited)}
              hint="A run that was rate limited and recovered otherwise reads exactly like a clean one."
            />
            <Row
              label="Finished pacing at"
              value={run.final_interval ? `${run.final_interval.toFixed(2)}s` : '—'}
            />
            {run.item_slots ? (
              <Row
                label="Unnameable items"
                value={`${count(run.unknown_items)} of ${count(run.item_slots)}`}
                hint="Above about 2% means the wiki join needs looking at."
              />
            ) : null}
            {run.coverage_estimate != null && (
              <Row
                label="Recent coverage"
                value={percent(run.coverage_estimate)}
                hint="Capture-recapture across two halves of the roster. An upper bound — premades bias it high."
              />
            )}
          </Rows>
          {run.egress_changed && (
            <p className="section-error">
              The outbound address changed mid-run. A clearance cookie is bound to the
              address that solved it, so a rotating exit cannot work here.
            </p>
          )}
        </>
      )}
    </Card>
  )
}

function CoverageTable({ rows }: { rows: NonNullable<LastRun['coverage']> }) {
  if (!rows.length) return null
  return (
    <>
      <h3 className="section">Coverage by day, as of the last crawl</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Day</th>
              <th>Seen</th>
              <th>Half A</th>
              <th>Half B</th>
              <th>Both</th>
              <th>Estimated total</th>
              <th>Coverage</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.date}>
                <td>{row.date}</td>
                <td>{count(row.seen)}</td>
                <td>{count(row.half_a)}</td>
                <td>{count(row.half_b)}</td>
                <td>{count(row.both)}</td>
                <td>
                  {row.estimated_total ? Math.round(row.estimated_total).toLocaleString() : '—'}
                </td>
                <td>{percent(row.coverage)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">
        Coverage is an upper bound: premades break the independence the estimator assumes,
        which biases the total low.
      </p>
    </>
  )
}

function GameDetail({ title, game }: { title: string; game: GameStatus }) {
  return (
    <>
      <h3 className="section">{title}</h3>
      <div className="grid">
        <Card
          title="Corpus"
          health={failed(game.corpus) ? 'unknown' : corpusHealth(game.corpus.newest)}
        >
          <Section value={game.corpus}>
            {(corpus) => (
              <Rows>
                <Row label="Files" value={count(corpus.files)} />
                <Row label="Newest file" value={corpus.newest ?? '—'} />
                <Row label="Written" value={ago(corpus.newest_at)} />
                <Row label="Size of newest" value={bytes(corpus.newest_bytes)} />
              </Rows>
            )}
          </Section>
        </Card>

        <Card
          title="Aggregate"
          health={failed(game.aggregate) ? 'unknown' : aggregateHealth(game.aggregate.built)}
        >
          <Section value={game.aggregate}>
            {(aggregate) => (
              <Rows>
                <Row
                  label="Built"
                  value={aggregate.built ?? 'never'}
                  hint="Date of the last full rebuild, carried across incremental runs."
                />
                <Row
                  label="Newest day counted"
                  value={aggregate.newest ?? '—'}
                  hint="What the recency weights are relative to."
                />
                <Row label="Files counted" value={count(aggregate.files)} />
                <Row label="Rows" value={count(aggregate.rows)} />
                {aggregate.unknown_rows ? (
                  <Row
                    label="Files with unreadable footers"
                    value={count(aggregate.unknown_rows)}
                  />
                ) : null}
              </Rows>
            )}
          </Section>
        </Card>

        <Card title="Model">
          <Section value={game.model}>
            {(model) => (
              <Rows>
                {Object.entries(model).map(([name, file]) => (
                  <Row
                    key={name}
                    label={name}
                    value={file ? `${bytes(file.bytes)} · ${ago(file.at)}` : 'not trained'}
                  />
                ))}
              </Rows>
            )}
          </Section>
        </Card>

        {game.crawl && (
          <Card title="Crawl frontier">
            <Section value={game.crawl}>
              {(crawl) => (
                <Rows>
                  <Row label="Matches collected" value={count(crawl.matches_collected)} />
                  <Row label="Players known" value={count(crawl.frontier?.players)} />
                  <Row
                    label="Never queried"
                    value={count(crawl.frontier?.unvisited)}
                    hint="The snowball's backlog. Players discovered in other people's matches but not yet read."
                  />
                  <Row
                    label="Last queried"
                    value={crawl.frontier?.last_queried ?? '—'}
                  />
                </Rows>
              )}
            </Section>
          </Card>
        )}
      </div>
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
      {smite && <GameDetail title="Smite 1 — Hi-Rez API" game={smite} />}
      {smite2 && <GameDetail title="Smite 2 — tracker.gg" game={smite2} />}

      {lastRun !== undefined && (
        <>
          <h3 className="section">Last crawl</h3>
          <Section value={lastRun}>
            {(run) => (
              <>
                <div className="grid">
                  <CrawlReport run={run} />
                  {run.frontier && (
                    <Card title="Roster after that run">
                      <Rows>
                        <Row label="Players known" value={count(run.frontier.total)} />
                        <Row label="Never queried" value={count(run.frontier.unvisited)} />
                        <Row
                          label="Written off"
                          value={count(run.frontier.dead)}
                          hint="Three barren visits in a row. Not deleted — people come back — but they stop competing for budget."
                        />
                        <Row
                          label="In a known party"
                          value={count(run.frontier.partied)}
                          hint="Querying both halves of a duo returns the same matches twice."
                        />
                      </Rows>
                    </Card>
                  )}
                </div>
                {run.coverage && <CoverageTable rows={run.coverage} />}
              </>
            )}
          </Section>
          {lastRun === null && (
            <p className="muted">
              <Badge health="unknown" /> No crawl has recorded a run yet. The collector
              writes one at the end of every night, including nights it refuses to start.
            </p>
          )}
        </>
      )}
    </>
  )
}
