/**
 * The roster, and one member at a time.
 *
 * Smite 1 only. The map behind this is Discord id → Smite 1 name and there is
 * no Smite 2 equivalent — a tracker.gg identity is a platform/handle pair that
 * does not follow from a Hi-Rez name — so a Smite 2 column here would be
 * guesses. Only the game handles cross into this file; the Discord ids stay
 * server-side.
 */

import { useMemo, useState } from 'react'
import { Link } from '../router'
import type { Player, Players as PlayersDoc } from '../api'
import { count, duration, percent, when } from '../format'
import { Band, Empty, Pair, Row, Rows } from '../components'

type SortKey = 'name' | 'level' | 'matches' | 'win_percent' | 'kda' | 'last_played_at'

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: 'name', label: 'Player' },
  { key: 'level', label: 'Level' },
  { key: 'matches', label: 'Matches' },
  { key: 'win_percent', label: 'Win rate' },
  { key: 'kda', label: 'KDA' },
  { key: 'last_played_at', label: 'Last played' },
]

function sortValue(player: Player, key: SortKey): number | string {
  switch (key) {
    case 'name':
      return player.name.toLowerCase()
    case 'level':
      return player.level ?? -1
    case 'matches':
      return player.totals?.matches ?? -1
    case 'win_percent':
      return player.totals?.win_percent ?? -1
    case 'kda':
      return player.totals?.kda ?? -1
    case 'last_played_at':
      return player.last_played_at ? Date.parse(player.last_played_at) : -1
  }
}

export function PlayerList({ doc }: { doc: PlayersDoc }) {
  const [key, setKey] = useState<SortKey>('matches')
  const [descending, setDescending] = useState(true)

  const rows = useMemo(() => {
    const sorted = [...(doc.players ?? [])]
    sorted.sort((a, b) => {
      const left = sortValue(a, key)
      const right = sortValue(b, key)
      if (left === right) return a.name.localeCompare(b.name)
      return left < right ? -1 : 1
    })
    return descending ? sorted.reverse() : sorted
  }, [doc.players, key, descending])

  if (!rows.length) return <Empty>The roster snapshot is empty.</Empty>

  const toggle = (next: SortKey) => {
    if (next === key) setDescending(!descending)
    else {
      setKey(next)
      setDescending(next !== 'name')
    }
  }

  return (
    <>
      <div className="scroll">
        <table className="stack-sm">
          <thead>
            <tr>
              {COLUMNS.map((column) => (
                <th
                  key={column.key}
                  className="sortable"
                  onClick={() => toggle(column.key)}
                  aria-sort={
                    key === column.key
                      ? descending
                        ? 'descending'
                        : 'ascending'
                      : 'none'
                  }
                >
                  {column.label}
                  {key === column.key && (descending ? ' ↓' : ' ↑')}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((player) => (
              <tr key={player.name}>
                <td>
                  {player.found && !player.private ? (
                    <Link to={`/players/${encodeURIComponent(player.name)}`}>
                      {player.name}
                    </Link>
                  ) : (
                    player.name
                  )}
                  {player.private && <span className="muted"> · hidden profile</span>}
                  {!player.found && <span className="muted"> · not found</span>}
                </td>
                <td data-label="level">{count(player.level)}</td>
                <td data-label="matches">{count(player.totals?.matches)}</td>
                <td data-label="win rate">{percent(player.totals?.win_percent)}</td>
                <td data-label="kda">
                  {player.totals ? player.totals.kda.toFixed(2) : '—'}
                </td>
                <td data-label="last played">
                  {player.last_played_at
                    ? new Date(player.last_played_at).toLocaleDateString(undefined, {
                        dateStyle: 'medium',
                      })
                    : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">
        Smite 1, refreshed every six hours. Totals count every queue the account has
        played; “best queue” excludes bot and custom matches, where a 100% win rate is true
        and means nothing.
      </p>

      <Smite2Roster doc={doc} />
    </>
  )
}

/**
 * The same people in Smite 2, keyed on `platform:handle` rather than a name.
 *
 * A separate table rather than more columns on the one above, because the two
 * games share no numbers: Smite 2 has a skill rating and no tier, no
 * worshippers, and a different set of modes. Merging them would mean a row of
 * blanks wherever one game has a stat the other doesn't.
 */
function Smite2Roster({ doc }: { doc: PlayersDoc }) {
  const smite2 = doc.smite2
  if (!smite2) return null

  if (smite2.skipped) {
    return (
      <Band
        label="Smite 2"
        qualifier="not refreshed this run"
        game="smite2"
        health="warn"
      >
        <p className="prose" style={{ marginBottom: 0 }}>
          {smite2.skipped}. This job and the nightly crawl leave from the same
          address, so it stands down rather than firing into a live ban and
          costing the crawl a night.
          {smite2.reason && (
            <>
              {' '}
              <code>{smite2.reason}</code>
            </>
          )}
        </p>
      </Band>
    )
  }

  const rows = smite2.players ?? []
  if (!rows.length) return null

  return (
    <Band
      label="Smite 2"
      qualifier="from tracker.gg · skill rating, no tier"
      game="smite2"
      health="ok"
    >
      <div className="scroll">
        <table className="stack-sm">
          <thead>
            <tr>
              <th>player</th>
              <th>matches</th>
              <th>win rate</th>
              <th>rating</th>
              <th>peak</th>
              <th>modes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((entry) => (
              <tr key={entry.id}>
                <td>
                  {entry.handle ?? entry.id}
                  <span className="muted"> · {entry.platform ?? '?'}</span>
                  {!entry.found && <span className="muted"> · not found</span>}
                </td>
                <td data-label="matches">{count(entry.matches)}</td>
                <td data-label="win rate">{percent(entry.win_percent)}</td>
                <td data-label="rating">{count(entry.skill_rating)}</td>
                <td data-label="peak">{count(entry.peak_skill_rating)}</td>
                <td data-label="modes">{count(entry.modes?.length)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ marginBottom: 0 }}>
        Smite 2 publishes a numeric skill rating and no tier name, so that is
        what this shows rather than inventing a division for it. Rating is from
        the ranked mode the player has climbed highest in.
      </p>
    </Band>
  )
}

export function PlayerDetail({ doc, name }: { doc: PlayersDoc; name: string }) {
  const wanted = name.toLowerCase()
  const player = doc.players?.find((entry) => entry.name.toLowerCase() === wanted)

  if (!player) {
    return (
      <>
        <Link className="back" to="/players">
          ← All players
        </Link>
        <Empty>No one by that name is on the roster.</Empty>
      </>
    )
  }

  const totals = player.totals

  return (
    <>
      <Link className="back" to="/players">
        ← All players
      </Link>

      <div className="player-head">
        {player.avatar_url ? <img src={player.avatar_url} alt="" /> : null}
        <div>
          <h1>{player.name}</h1>
          <p className="muted" style={{ margin: 0 }}>
            {[player.platform, player.region, player.clan].filter(Boolean).join(' · ') ||
              'Smite 1'}
          </p>
        </div>
      </div>

      {player.private && <Empty>This profile is hidden, so there is nothing to show.</Empty>}

      <Band label="Account" health="ok">
        <Pair>
          <Rows>
            <Row label="level" value={count(player.level)} />
            <Row label="created" value={when(dateSeconds(player.created_at))} />
            <Row label="last login" value={when(dateSeconds(player.last_login_at))} />
          </Rows>
          <Rows>
            <Row label="last played" value={when(dateSeconds(player.last_played_at))} />
            <Row label="worshippers" value={count(player.total_worshippers)} />
            <Row label="disconnects" value={count(player.leaves)} />
          </Rows>
        </Pair>
      </Band>

      {totals && (
        <Band label="Lifetime totals" qualifier="every queue" health="ok">
          <Pair>
            <Rows>
              <Row label="matches" value={count(totals.matches)} />
              <Row
                label="wins / losses"
                value={`${count(totals.wins)} / ${count(totals.losses)}`}
              />
              <Row label="win rate" value={percent(totals.win_percent)} />
              <Row label="time played" value={duration(totals.minutes * 60)} />
            </Rows>
            <Rows>
              <Row
                label="K / D / A"
                value={`${count(totals.kills)} / ${count(totals.deaths)} / ${count(totals.assists)}`}
              />
              <Row label="KDA" value={totals.kda.toFixed(2)} />
              <Row label="gold" value={count(totals.gold)} />
            </Rows>
          </Pair>
        </Band>
      )}

      {player.best_queue && (
        <Band
          label="Best queue"
          qualifier="bot and custom excluded · ten matches minimum"
          health="ok"
        >
          <Pair>
            <Rows>
              <Row label="queue" value={player.best_queue.queue} />
              <Row label="win rate" value={percent(player.best_queue.win_percent)} />
            </Rows>
            <Rows>
              <Row label="matches" value={count(player.best_queue.matches)} />
            </Rows>
          </Pair>
        </Band>
      )}

      {player.ranked && player.ranked.length > 0 && (
        <Band label="Ranked" health="ok">
          <div className="scroll">
            <table className="stack-sm">
              <thead>
                <tr>
                  <th>queue</th>
                  <th>tier</th>
                  <th>mmr</th>
                  <th>tp</th>
                  <th>wins</th>
                  <th>losses</th>
                  <th>disconnects</th>
                </tr>
              </thead>
              <tbody>
                {player.ranked.map((entry) => (
                  <tr key={entry.queue}>
                    <td>{entry.queue}</td>
                    <td data-label="tier">{entry.tier}</td>
                    <td data-label="mmr">{Math.round(entry.mmr).toLocaleString()}</td>
                    <td data-label="tp">
                      {entry.tier_id < 25 ? `${entry.points}/100` : '—'}
                    </td>
                    <td data-label="wins">{count(entry.wins)}</td>
                    <td data-label="losses">{count(entry.losses)}</td>
                    <td data-label="disconnects">{count(entry.leaves)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Band>
      )}

      {player.top_gods && player.top_gods.length > 0 && (
        <Band label="Most worshipped" qualifier="top ten by worshippers" health="ok">
          <div className="scroll">
            <table className="stack-sm">
              <thead>
                <tr>
                  <th>god</th>
                  <th>worshippers</th>
                  <th>rank</th>
                  <th>wins</th>
                  <th>losses</th>
                  <th>win rate</th>
                  <th>kda</th>
                </tr>
              </thead>
              <tbody>
                {player.top_gods.map((god) => (
                  <tr key={god.god_id}>
                    <td>{god.god ?? `#${god.god_id}`}</td>
                    <td data-label="worshippers">{count(god.worshippers)}</td>
                    <td data-label="rank">{count(god.rank)}</td>
                    <td data-label="wins">{count(god.wins)}</td>
                    <td data-label="losses">{count(god.losses)}</td>
                    <td data-label="win rate">
                      {god.wins + god.losses
                        ? percent(god.wins / (god.wins + god.losses))
                        : '—'}
                    </td>
                    <td data-label="kda">
                      {((god.kills + god.assists / 2) / (god.deaths || 1)).toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Band>
      )}
    </>
  )
}

function dateSeconds(iso: string | null | undefined): number | null {
  if (!iso) return null
  const parsed = Date.parse(iso)
  return isNaN(parsed) ? null : parsed / 1000
}
