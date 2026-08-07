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
import { Card, Empty, Row, Rows } from '../components'

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
      <div className="table-wrap">
        <table>
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
                <td>{count(player.level)}</td>
                <td>{count(player.totals?.matches)}</td>
                <td>{percent(player.totals?.win_percent)}</td>
                <td>{player.totals ? player.totals.kda.toFixed(2) : '—'}</td>
                <td>
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
        Smite 1 only, refreshed every six hours. Totals count every queue the account has
        played; “best queue” excludes bot and custom matches, where a 100% win rate is true
        and means nothing.
      </p>
    </>
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

      <div className="grid">
        <Card title="Account">
          <Rows>
            <Row label="Level" value={count(player.level)} />
            <Row label="Created" value={when(dateSeconds(player.created_at))} />
            <Row label="Last login" value={when(dateSeconds(player.last_login_at))} />
            <Row label="Last played" value={when(dateSeconds(player.last_played_at))} />
            <Row label="Worshippers" value={count(player.total_worshippers)} />
            <Row label="Disconnects" value={count(player.leaves)} />
          </Rows>
        </Card>

        {totals && (
          <Card title="Lifetime totals">
            <Rows>
              <Row label="Matches" value={count(totals.matches)} />
              <Row
                label="Wins / losses"
                value={`${count(totals.wins)} / ${count(totals.losses)}`}
              />
              <Row label="Win rate" value={percent(totals.win_percent)} />
              <Row
                label="K / D / A"
                value={`${count(totals.kills)} / ${count(totals.deaths)} / ${count(totals.assists)}`}
              />
              <Row label="KDA" value={totals.kda.toFixed(2)} />
              <Row label="Gold" value={count(totals.gold)} />
              <Row label="Time played" value={duration(totals.minutes * 60)} />
            </Rows>
          </Card>
        )}

        {player.best_queue && (
          <Card title="Best queue">
            <Rows>
              <Row label="Queue" value={player.best_queue.queue} />
              <Row label="Win rate" value={percent(player.best_queue.win_percent)} />
              <Row label="Matches" value={count(player.best_queue.matches)} />
            </Rows>
            <p className="muted" style={{ marginBottom: 0 }}>
              Bot and custom queues excluded, and a queue needs at least ten matches.
            </p>
          </Card>
        )}
      </div>

      {player.ranked && player.ranked.length > 0 && (
        <>
          <h3 className="section">Ranked</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Queue</th>
                  <th>Tier</th>
                  <th>MMR</th>
                  <th>TP</th>
                  <th>Wins</th>
                  <th>Losses</th>
                  <th>Disconnects</th>
                </tr>
              </thead>
              <tbody>
                {player.ranked.map((entry) => (
                  <tr key={entry.queue}>
                    <td>{entry.queue}</td>
                    <td>{entry.tier}</td>
                    <td>{Math.round(entry.mmr).toLocaleString()}</td>
                    <td>{entry.tier_id < 25 ? `${entry.points}/100` : '—'}</td>
                    <td>{count(entry.wins)}</td>
                    <td>{count(entry.losses)}</td>
                    <td>{count(entry.leaves)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {player.top_gods && player.top_gods.length > 0 && (
        <>
          <h3 className="section">Most worshipped</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>God</th>
                  <th>Worshippers</th>
                  <th>Rank</th>
                  <th>Wins</th>
                  <th>Losses</th>
                  <th>Win rate</th>
                  <th>KDA</th>
                </tr>
              </thead>
              <tbody>
                {player.top_gods.map((god) => (
                  <tr key={god.god_id}>
                    <td>{god.god ?? `#${god.god_id}`}</td>
                    <td>{count(god.worshippers)}</td>
                    <td>{count(god.rank)}</td>
                    <td>{count(god.wins)}</td>
                    <td>{count(god.losses)}</td>
                    <td>
                      {god.wins + god.losses
                        ? percent(god.wins / (god.wins + god.losses))
                        : '—'}
                    </td>
                    <td>
                      {((god.kills + god.assists / 2) / (god.deaths || 1)).toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}

function dateSeconds(iso: string | null | undefined): number | null {
  if (!iso) return null
  const parsed = Date.parse(iso)
  return isNaN(parsed) ? null : parsed / 1000
}
