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
import type { Player, Players as PlayersDoc, Smite2Player } from '../api'
import { count, duration, percent, when } from '../format'
import { Band, Empty, Pair, Row, Rows } from '../components'

/**
 * Shared sorting for both rosters.
 *
 * The Smite 2 table was a plain list next to a sortable one, which reads as an
 * oversight rather than a decision — and the two answer the same question, so
 * they should behave the same way.
 */
function useSorted<T>(
  rows: T[],
  columns: { key: string; value: (row: T) => number | string }[],
  initial: string,
) {
  const [key, setKey] = useState(initial)
  const [descending, setDescending] = useState(true)

  const sorted = useMemo(() => {
    const column = columns.find((c) => c.key === key) ?? columns[0]
    const out = [...rows]
    out.sort((a, b) => {
      const left = column.value(a)
      const right = column.value(b)
      if (left === right) return 0
      return left < right ? -1 : 1
    })
    return descending ? out.reverse() : out
  }, [rows, columns, key, descending])

  const toggle = (next: string) => {
    if (next === key) setDescending(!descending)
    else {
      setKey(next)
      // Names read naturally A→Z; every other column is "most first".
      setDescending(next !== 'name')
    }
  }

  const header = (columnKey: string, label: string) => (
    <th
      key={columnKey}
      className="sortable"
      tabIndex={0}
      role="columnheader"
      aria-sort={key === columnKey ? (descending ? 'descending' : 'ascending') : 'none'}
      onClick={() => toggle(columnKey)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          toggle(columnKey)
        }
      }}
    >
      {label}
      {key === columnKey && (descending ? ' ↓' : ' ↑')}
    </th>
  )

  return { sorted, header }
}

/**
 * A player's picture, or the closest true thing to one.
 *
 * Exactly one of the fourteen has ever set a Hi-Rez avatar, so without a
 * fallback the roster is thirteen blanks and a photograph. Their most-played
 * god stands in — it is a fact about them rather than a placeholder, and on a
 * Smite site it reads as identity rather than as a missing image.
 */
function face(player: Player): string | null {
  return player.avatar_url || player.top_gods?.[0]?.icon || null
}

/**
 * Swap a broken image for the fallback rather than leaving a torn icon.
 *
 * Needed because the one Hi-Rez avatar in this roster 403s — the asset was a
 * 2017 upload to the old WordPress site and is simply gone. A URL existing is
 * not the same as an image existing, and only the browser finds out.
 */
function onBrokenImage(fallback: string | null) {
  return (event: React.SyntheticEvent<HTMLImageElement>) => {
    const img = event.currentTarget
    if (fallback && img.src !== fallback) {
      img.src = fallback
      return
    }
    img.style.visibility = 'hidden'
  }
}

export function PlayerList({ doc }: { doc: PlayersDoc }) {
  const columns = useMemo(
    () => [
      { key: 'name', label: 'player', value: (p: Player) => p.name.toLowerCase() },
      { key: 'level', label: 'level', value: (p: Player) => p.level ?? -1 },
      { key: 'matches', label: 'matches', value: (p: Player) => p.totals?.matches ?? -1 },
      {
        key: 'win_percent',
        label: 'win rate',
        value: (p: Player) => p.totals?.win_percent ?? -1,
      },
      { key: 'kda', label: 'kda', value: (p: Player) => p.totals?.kda ?? -1 },
      {
        key: 'last_played_at',
        label: 'last played',
        value: (p: Player) =>
          p.last_played_at ? Date.parse(p.last_played_at) : -1,
      },
    ],
    [],
  )
  const { sorted, header } = useSorted(doc.players ?? [], columns, 'matches')

  if (!sorted.length) return <Empty>The roster snapshot is empty.</Empty>

  return (
    <>
      {/* In a Band, like the Smite 2 table below it — otherwise the two sit on
          different left edges and the pair reads as a mistake. */}
      <Band
        label="Smite 1"
        qualifier="from the Hi-Rez API · refreshed every six hours"
        game="smite"
        health="ok"
      >
        <div className="scroll">
          <table className="stack-sm">
            <thead>
              <tr>{columns.map((column) => header(column.key, column.label))}</tr>
            </thead>
            <tbody>
              {sorted.map((player) => (
                <tr key={player.name}>
                  <td>
                    <span className="who">
                      {face(player) ? (
                        <img
                          className="face"
                          src={face(player)!}
                          alt=""
                          loading="lazy"
                          onError={onBrokenImage(player.top_gods?.[0]?.icon ?? null)}
                        />
                      ) : (
                        <span className="face face-blank" aria-hidden="true" />
                      )}
                      {player.found && !player.private ? (
                        <Link to={`/players/${encodeURIComponent(player.name)}`}>
                          {player.name}
                        </Link>
                      ) : (
                        player.name
                      )}
                    </span>
                    {player.private && <span className="muted"> · hidden</span>}
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
        <p className="muted" style={{ marginBottom: 0 }}>
          Totals count every queue the account has played; “best queue” excludes
          bot and custom matches, where a 100% win rate is true and means nothing.
        </p>
      </Band>

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

  return <Smite2Table rows={rows} />
}

function Smite2Table({ rows }: { rows: Smite2Player[] }) {
  const columns = useMemo(
    () => [
      {
        key: 'name',
        label: 'player',
        value: (p: Smite2Player) => (p.name ?? p.handle ?? p.id).toLowerCase(),
      },
      { key: 'matches', label: 'matches', value: (p: Smite2Player) => p.matches ?? -1 },
      {
        key: 'win_percent',
        label: 'win rate',
        value: (p: Smite2Player) => p.win_percent ?? -1,
      },
      { key: 'kda', label: 'kda', value: (p: Smite2Player) => p.kda ?? -1 },
      {
        key: 'rating',
        label: 'rating',
        value: (p: Smite2Player) => p.skill_rating ?? -1,
      },
      {
        key: 'modes',
        label: 'modes',
        value: (p: Smite2Player) => p.modes?.length ?? -1,
      },
    ],
    [],
  )
  const { sorted, header } = useSorted(rows, columns, 'matches')

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
            <tr>{columns.map((column) => header(column.key, column.label))}</tr>
          </thead>
          <tbody>
            {sorted.map((entry) => (
              <tr key={entry.id}>
                <td>
                  <span className="who">
                    {entry.avatar_url ? (
                      <img
                        className="face"
                        src={entry.avatar_url}
                        alt=""
                        loading="lazy"
                        onError={onBrokenImage(null)}
                      />
                    ) : (
                      <span className="face face-blank" aria-hidden="true" />
                    )}
                    {entry.found ? (
                      <Link to={`/smite2/${encodeURIComponent(entry.handle ?? entry.id)}`}>
                        {entry.name ?? entry.handle ?? entry.id}
                      </Link>
                    ) : (
                      (entry.name ?? entry.handle ?? entry.id)
                    )}
                  </span>
                  {!entry.found && <span className="muted"> · not found</span>}
                </td>
                <td data-label="matches">{count(entry.matches)}</td>
                <td data-label="win rate">{percent(entry.win_percent)}</td>
                <td data-label="kda">{entry.kda?.toFixed(2) ?? '—'}</td>
                <td data-label="rating">{entry.skill_rating ?? '—'}</td>
                <td data-label="modes">{count(entry.modes?.length)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ marginBottom: 0 }}>
        Smite 2 publishes a numeric skill rating and no tier name, so that is
        what this shows rather than inventing a division for it.
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
        {face(player) ? (
          <img
            src={face(player)!}
            alt=""
            onError={onBrokenImage(player.top_gods?.[0]?.icon ?? null)}
          />
        ) : null}
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

/**
 * One Smite 2 player, at the depth the Smite 1 page reaches.
 *
 * The panels deliberately mirror the Smite 1 detail — account, lifetime totals,
 * best mode, per-mode table, most-played gods — so the two are comparable
 * rather than merely adjacent. Where a stat has no Smite 2 counterpart it is
 * absent rather than blank: there are no worshippers, no account creation date
 * and no tier, because tracker.gg publishes none of them.
 */
export function Smite2Detail({ doc, handle }: { doc: PlayersDoc; handle: string }) {
  const wanted = handle.toLowerCase()
  const player = doc.smite2?.players?.find(
    (entry) => (entry.handle ?? entry.id).toLowerCase() === wanted,
  )

  if (!player || !player.found) {
    return (
      <>
        <Link className="back" to="/players">
          ← All players
        </Link>
        <Empty>No Smite 2 player by that id on the roster.</Empty>
      </>
    )
  }

  return (
    <>
      <Link className="back" to="/players">
        ← All players
      </Link>

      <div className="player-head">
        {player.avatar_url ? (
          <img src={player.avatar_url} alt="" onError={onBrokenImage(null)} />
        ) : null}
        <div>
          <h1>{player.name ?? player.handle}</h1>
          <p className="muted" style={{ margin: 0 }}>
            Smite 2 · {player.platform} · <code>{player.handle}</code>
          </p>
        </div>
      </div>

      <Band label="Lifetime totals" qualifier="every mode" game="smite2" health="ok">
        <Pair>
          <Rows>
            <Row label="matches" value={count(player.matches)} />
            <Row
              label="wins / losses"
              value={`${count(player.wins)} / ${count(player.losses)}`}
            />
            <Row label="win rate" value={percent(player.win_percent)} />
            <Row
              label="time played"
              value={player.minutes ? duration(player.minutes * 60) : '—'}
              absent={!player.minutes}
            />
          </Rows>
          <Rows>
            <Row
              label="K / D / A"
              value={`${count(player.kills)} / ${count(player.deaths)} / ${count(player.assists)}`}
            />
            <Row label="KDA" value={player.kda?.toFixed(2) ?? '—'} />
            <Row label="damage" value={count(player.damage)} />
            <Row label="gold" value={count(player.gold)} />
          </Rows>
        </Pair>
      </Band>

      <Band
        label="Ranked"
        qualifier="skill rating · Smite 2 publishes no tier"
        game="smite2"
        health="ok"
      >
        <Pair>
          <Rows>
            <Row
              label="skill rating"
              value={player.skill_rating ?? 'never rated'}
              absent={player.skill_rating == null}
              hint="From the ranked mode this player has climbed highest in."
            />
          </Rows>
          <Rows>
            <Row
              label="peak"
              value={player.peak_skill_rating ?? '—'}
              absent={player.peak_skill_rating == null}
            />
          </Rows>
        </Pair>
        {player.skill_rating == null && (
          <p className="muted" style={{ marginBottom: 0 }}>
            No ranked play recorded. Smite 2 reports a numeric rating rather than
            a division, so there is no tier name to show in its place.
          </p>
        )}
      </Band>

      {player.best_mode && (
        <Band
          label="Best mode"
          qualifier="ten matches minimum"
          game="smite2"
          health="ok"
        >
          <Pair>
            <Rows>
              <Row label="mode" value={player.best_mode.name} />
              <Row label="win rate" value={percent(player.best_mode.win_percent)} />
            </Rows>
            <Rows>
              <Row label="matches" value={count(player.best_mode.matches)} />
            </Rows>
          </Pair>
        </Band>
      )}

      {player.modes && player.modes.length > 0 && (
        <Band label="By mode" game="smite2" health="ok">
          <div className="scroll">
            <table className="stack-sm">
              <thead>
                <tr>
                  <th>mode</th>
                  <th>matches</th>
                  <th>wins</th>
                  <th>losses</th>
                  <th>win rate</th>
                  <th>kda</th>
                  <th>rating</th>
                </tr>
              </thead>
              <tbody>
                {player.modes.map((mode) => (
                  <tr key={mode.name}>
                    <td>{mode.name}</td>
                    <td data-label="matches">{count(mode.matches)}</td>
                    <td data-label="wins">{count(mode.wins)}</td>
                    <td data-label="losses">{count(mode.losses)}</td>
                    <td data-label="win rate">{percent(mode.win_percent)}</td>
                    <td data-label="kda">{mode.kda.toFixed(2)}</td>
                    <td data-label="rating">{mode.skill_rating ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Band>
      )}

      {player.top_gods && player.top_gods.length > 0 && (
        <Band
          label="Most played"
          qualifier="top ten by matches"
          game="smite2"
          health="ok"
        >
          <div className="scroll">
            <table className="stack-sm">
              <thead>
                <tr>
                  <th>god</th>
                  <th>matches</th>
                  <th>wins</th>
                  <th>losses</th>
                  <th>win rate</th>
                  <th>kda</th>
                </tr>
              </thead>
              <tbody>
                {player.top_gods.map((god) => (
                  <tr key={god.god}>
                    <td>
                      <span className="who">
                        {god.icon && (
                          <img
                            className="face"
                            src={god.icon}
                            alt=""
                            loading="lazy"
                            onError={onBrokenImage(null)}
                          />
                        )}
                        {god.god}
                      </span>
                    </td>
                    <td data-label="matches">{count(god.matches)}</td>
                    <td data-label="wins">{count(god.wins)}</td>
                    <td data-label="losses">{count(god.losses)}</td>
                    <td data-label="win rate">{percent(god.win_percent)}</td>
                    <td data-label="kda">{god.kda.toFixed(2)}</td>
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
