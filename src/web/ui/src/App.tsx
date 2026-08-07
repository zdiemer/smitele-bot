/**
 * Shell, routing, and the freshness strip.
 *
 * The strip is above every view rather than tucked into one, because every
 * number on this site is as trustworthy as the snapshot behind it and a page
 * that renders confidently from a four-hour-old file is worse than one that
 * renders nothing.
 */

import { match, NavLink, usePath } from './router'
import type { Players, Status } from './api'
import { useEndpoint } from './useSnapshot'
import { duration, snapshotHealth } from './format'
import { Badge, Empty } from './components'
import Overview from './views/Overview'
import Data from './views/Data'
import ApiHealth from './views/ApiHealth'
import { PlayerDetail, PlayerList } from './views/Players'
import Docs from './views/Docs'

const TABS = [
  { to: '/', label: 'Overview', end: true },
  { to: '/data', label: 'Data' },
  { to: '/api', label: 'API health' },
  { to: '/players', label: 'Players' },
  { to: '/docs', label: 'Desktop API' },
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
        <Badge health="bad" label="No snapshot" />
        <span>{error}</span>
        <button onClick={refresh}>Retry</button>
      </div>
    )
  }

  const stale = status?.stale_seconds ?? null
  const health = snapshotHealth(stale)

  return (
    <div className="freshness">
      <Badge
        health={health}
        label={health === 'ok' ? 'Fresh' : health === 'warn' ? 'Stale' : 'Very stale'}
      />
      <span>
        Snapshot written <strong>{duration(stale)}</strong> ago. The job runs every 15
        minutes; nothing on this page calls Hi-Rez or tracker.gg directly.
      </span>
      <button onClick={refresh}>Refresh</button>
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

function Body({ status }: { status: Status | null }) {
  const { path } = usePath()
  const waiting = <Empty>Waiting for a snapshot…</Empty>

  if (match('/', path)) return status ? <Overview status={status} /> : waiting
  if (match('/data', path)) return status ? <Data status={status} /> : waiting
  if (match('/api', path)) return status ? <ApiHealth status={status} /> : waiting
  if (match('/docs', path)) return <Docs />
  if (match('/players', path)) return <PlayersRoute />

  const player = match('/players/:name', path)
  if (player) return <PlayersRoute name={player.name} />

  return <Empty>No such page.</Empty>
}

export default function App() {
  const { data, error, loading, refresh } = useEndpoint<Status>('/api/status')

  return (
    <div className="shell">
      <header className="masthead">
        <h1>Smite data &amp; API liveness</h1>
        <p>
          Scraper and upstream health for the Smite 1 and Smite 2 data behind Smite-le.
        </p>
        <nav>
          {TABS.map((tab) => (
            <NavLink key={tab.to} to={tab.to} end={tab.end}>
              {tab.label}
            </NavLink>
          ))}
        </nav>
      </header>

      <Freshness status={data} error={error} loading={loading} refresh={refresh} />

      <Body status={data} />
    </div>
  )
}
