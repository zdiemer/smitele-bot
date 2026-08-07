/**
 * What is actually in the corpus, rather than how fresh it is.
 *
 * Everything here comes from the aggregate's per-god table — 83KB for Smite 1,
 * 37KB for Smite 2 — and not from the corpus, which is 3,300 files and tens of
 * gigabytes. That is the only reason a scheduled job can produce it at all.
 *
 * One number needs saying out loud wherever it appears: `plays` counts *player
 * records*, not matches. The aggregate groups by (god, queue, role, mmr), so ten
 * of them come from one game. Nothing here divides by ten and calls the result
 * matches, because that would be a guess about premades and bots dressed up as a
 * measurement.
 */

import type { GameStats, Stats as StatsDoc } from '../api'
import { failed } from '../api'
import { count } from '../format'
import { Band, Empty, Pair, Row, Rows, Section } from '../components'
import { Bars, DailyArea } from '../charts'

function GameStatsView({
  title,
  which,
  stats,
}: {
  title: string
  which: 'smite' | 'smite2'
  stats: GameStats
}) {
  if (!stats.built) {
    return (
      <Band label={`${title} — corpus breakdown`} game={which} health="unknown">
        <p className="prose" style={{ marginBottom: 0 }}>
          No aggregate has been built for this game yet, so there is nothing to
          break down. The nightly aggregate job is what produces it.
        </p>
      </Band>
    )
  }

  const perDay =
    stats.matches_per_day && !failed(stats.matches_per_day) ? stats.matches_per_day : null

  return (
    <>
      <Band
        label={`${title} — what's in the corpus`}
        qualifier="from the aggregate"
        game={which}
        health="ok"
      >
        <Pair>
          <Rows>
            <Row
              label="player records"
              value={count(stats.total_plays)}
              hint="One per player per match, not one per match — ten of these come from one game."
            />
            <Row label="gods seen" value={count(stats.distinct_gods)} />
          </Rows>
          <Rows>
            <Row label="queues seen" value={count(stats.distinct_queues)} />
            <Row
              label="high-MMR records"
              value={count(stats.high_mmr_plays)}
              hint="Above 2000 MMR, which is the threshold the aggregate buckets on."
            />
          </Rows>
        </Pair>
      </Band>

      {stats.queues && stats.queues.length > 0 && (
        <Band
          label={`${title} — by queue`}
          qualifier="player records · win rate"
          game={which}
          health="ok"
        >
          <Bars rows={stats.queues} total={stats.total_plays} showWinRate />
        </Band>
      )}

      {stats.roles && stats.roles.length > 0 && (
        <Band label={`${title} — by role`} game={which} health="ok">
          <Bars rows={stats.roles} total={stats.total_plays} showWinRate />
          {stats.roles.some((r) => r.name === 'Unknown' && r.plays > 0) && (
            <p className="muted" style={{ marginBottom: 0 }}>
              “Unknown” is not a gap in the data — most queues have no lane
              assignment at all, so only Conquest-shaped modes report a role.
            </p>
          )}
        </Band>
      )}

      {stats.gods && stats.gods.length > 0 && (
        <Band
          label={`${title} — most played gods`}
          qualifier={`top ${stats.gods.length} of ${stats.gods_total ?? stats.gods.length}`}
          game={which}
          health="ok"
        >
          <Bars rows={stats.gods} total={stats.total_plays} showWinRate />
        </Band>
      )}

      {perDay && perDay.length > 1 && (
        <Band
          label={`${title} — matches collected per day`}
          qualifier="by the day played, not the day crawled"
          game={which}
          health="ok"
        >
          <DailyArea points={perDay} />
          <p className="muted" style={{ marginBottom: 0 }}>
            Rows are filed under the day the match was played, so one night's
            crawl backfills roughly three calendar days rather than closing one.
            A recent day keeps growing for a while after it.
          </p>
        </Band>
      )}
    </>
  )
}

export default function Stats({ stats }: { stats: StatsDoc }) {
  const smite = stats.games?.smite
  const smite2 = stats.games?.smite2

  if (!smite && !smite2) return <Empty>The stats snapshot has no games in it yet.</Empty>

  return (
    <>
      {smite && (
        <Section value={smite}>
          {(value) => <GameStatsView title="Smite 1" which="smite" stats={value} />}
        </Section>
      )}
      {smite2 && (
        <Section value={smite2}>
          {(value) => <GameStatsView title="Smite 2" which="smite2" stats={value} />}
        </Section>
      )}

      <Band label="Reading these numbers" health="ok">
        <p className="prose" style={{ marginBottom: 0 }}>
          Win rates sit near 50% across a whole queue because every match has a
          winner and a loser, and the corpus contains both sides of each one. The
          per-god figures are the interesting ones: they say how a god does
          relative to that floor. Percentages are of{' '}
          {count(
            (!failed(smite) && smite?.total_plays) ||
              (!failed(smite2) && smite2?.total_plays) ||
              0,
          )}{' '}
          player records, not of matches.
        </p>
      </Band>
    </>
  )
}
