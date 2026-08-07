/**
 * Shell, routing, and the freshness strip.
 *
 * The strip is above every view rather than tucked into one, because every
 * number on this site is as trustworthy as the snapshot behind it and a page
 * that renders confidently from a four-hour-old file is worse than one that
 * renders nothing.
 */

import { Link, match, NavLink, usePath } from './router'
import type { Players, Stats, Status } from './api'
import { useEndpoint } from './useSnapshot'
import { duration, snapshotHealth } from './format'
import { Bolt, Empty, Mark } from './components'
import Overview from './views/Overview'
import Data from './views/Data'
import ApiHealth from './views/ApiHealth'
import { PlayerDetail, PlayerList } from './views/Players'
import StatsView from './views/Stats'

const TABS = [
  { to: '/', label: 'Overview', end: true },
  { to: '/data', label: 'Data' },
  { to: '/stats', label: 'Corpus' },
  { to: '/upstreams', label: 'Upstreams' },
  { to: '/players', label: 'Players' },
]

function Freshness({
  status,
  error,
  loading,
  refresh,
}: {
  status: Status | null
  error: string | null
  loading: boolean
  refresh: () => void
}) {
  if (loading && !status && !error) return null

  if (error) {
    return (
      <div className="freshness">
        <Mark health="bad" />
        <span className="broken">
          no snapshot — <b>{error}</b>
        </span>
        <button onClick={refresh}>retry</button>
      </div>
    )
  }

  const stale = status?.stale_seconds ?? null
  const health = snapshotHealth(stale)

  return (
    <div className="freshness">
      <span className={health === 'ok' ? '' : health === 'warn' ? 'stale' : 'broken'}>
        snapshot written <b>{duration(stale)}</b> ago
      </span>
      <span>· every 15 min · nothing here calls Hi-Rez or tracker.gg</span>
      <button onClick={refresh}>refresh</button>
    </div>
  )
}

function PlayersRoute({ name }: { name?: string }) {
  // Its own endpoint and its own cadence — the roster refreshes every six
  // hours, so it must not ride the liveness poll or block it.
  const { data, error, loading } = useEndpoint<Players>('/api/players')

  if (loading && !data) return <Empty>Loading the roster…</Empty>
  if (error || !data) {
    return (
      <Empty>
        No roster snapshot yet{error ? `: ${error}` : ''}. The players job runs every six
        hours and writes its own file, separately from the liveness snapshot.
      </Empty>
    )
  }
  return name === undefined ? <PlayerList doc={data} /> : <PlayerDetail doc={data} name={name} />
}

function StatsRoute() {
  // Its own endpoint and its own cadence. The aggregate behind it rebuilds
  // daily, so this snapshot runs every six hours and must not ride the
  // fifteen-minute liveness poll.
  const { data, error, loading } = useEndpoint<Stats>('/api/stats')

  if (loading && !data) return <Empty>Loading the corpus breakdown…</Empty>
  if (error || !data) {
    return (
      <Empty>
        No corpus snapshot yet{error ? `: ${error}` : ''}. The stats job runs every
        six hours and writes its own file, separately from the liveness snapshot.
      </Empty>
    )
  }
  return <StatsView stats={data} />
}

function Body({ status }: { status: Status | null }) {
  const { path } = usePath()
  const waiting = <Empty>Waiting for a snapshot…</Empty>

  if (match('/', path)) return status ? <Overview status={status} /> : waiting
  if (match('/data', path)) return status ? <Data status={status} /> : waiting
  if (match('/upstreams', path)) return status ? <ApiHealth status={status} /> : waiting
  if (match('/stats', path)) return <StatsRoute />
  if (match('/players', path)) return <PlayersRoute />

  const player = match('/players/:name', path)
  if (player) return <PlayersRoute name={player.name} />

  return <Empty>No such page.</Empty>
}

export default function App() {
  const { data, error, loading, refresh } = useEndpoint<Status>('/api/status')

  return (
    <div className="shell">
      {/*
        The freshness strip sits in the masthead rather than below it: every
        number on this page is exactly as trustworthy as the snapshot behind it,
        so the age belongs next to the title, not in a bar you scroll past.
      */}
      <header className="masthead">
        {/*
          The wordmark is the way back to the overview — the convention every
          site has, and the one thing a reader will try after clicking into a
          player. `end` so it is only marked current on the overview itself.
        */}
        <h1 className="wordmark">
          <Link to="/" className="wordmark-link">
            <Bolt />
            <span>
              smite<span className="host">.diemer.codes</span>
            </span>
          </Link>
        </h1>
        <Freshness status={data} error={error} loading={loading} refresh={refresh} />
      </header>

      <nav>
        {TABS.map((tab) => (
          <NavLink key={tab.to} to={tab.to} end={tab.end}>
            {tab.label}
          </NavLink>
        ))}
      </nav>

      <Body status={data} />
    </div>
  )
}
