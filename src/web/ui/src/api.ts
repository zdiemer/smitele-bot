/**
 * The shapes `snapshot.py` writes, and the hook that reads them.
 *
 * Everything optional or nullable on purpose. The snapshot degrades section by
 * section — a share that went away leaves an `error` where a corpus should be,
 * and the rest of the document is still good — so a type that insisted every
 * field was present would be lying about the most important case.
 */

export type Failed = { error: string }

export function failed<T>(value: T | Failed | null | undefined): value is Failed {
  return !!value && typeof value === 'object' && 'error' in value
}

export type Corpus = {
  files: number
  newest: string | null
  newest_at: number | null
  newest_bytes?: number | null
}

export type Aggregate = {
  built: string | null
  newest: string | null
  files: number
  rows: number
  unknown_rows?: number
}

export type ModelFile = { at: number; bytes: number } | null
export type Models = Record<string, ModelFile>

export type Crawl = {
  frontier: {
    players: number
    unvisited: number
    last_queried: string | null
  } | null
  matches_collected: number
}

export type CoverageRow = {
  date: string
  seen: number
  half_a: number
  half_b: number
  both: number
  estimated_total: number | null
  coverage: number | null
}

export type LastRun = {
  version?: number
  started?: number
  finished?: number
  elapsed_seconds?: number
  exit_reason?: 'ok' | 'blocked' | 'standdown' | 'no_gods'
  egress?: string
  egress_changed?: boolean
  requests?: number
  bytes?: number
  budget?: number
  new_matches?: number
  rows_written?: number
  matches_known?: number
  players_visited?: number
  players_discovered?: number
  rate_limited?: number
  final_interval?: number
  unknown_items?: number
  item_slots?: number
  frontier?: { total: number; unvisited: number; dead: number; partied: number }
  coverage?: CoverageRow[]
  coverage_estimate?: number | null
  standdown?: { until: number; reason: string; remaining_seconds: number }
}

export type Scheduled = {
  /** null when the chart said nothing — "unknown", not "switched off". */
  collector: boolean | null
  aggregate: boolean | null
}

export type GameStatus = {
  scheduled?: Scheduled
  corpus: Corpus | Failed
  aggregate: Aggregate | Failed
  model: Models | Failed
  crawl?: Crawl | Failed
  last_run?: LastRun | null | Failed
}

export type Quota = {
  requests_today: number
  requests_limit: number
  sessions_today: number
  sessions_limit: number
  active_sessions: number
  concurrent_limit: number
}

export type Server = {
  platform: string | null
  environment: string | null
  status: string | null
  version: string | null
  limited_access: boolean
  entry_datetime: string | null
}

export type Tracker = {
  egress: string
  standdown: {
    active: boolean
    until: number
    remaining_seconds: number
    reason: string
    armed_at: number
  }
  clearance: {
    mints_today: number
    mints_limit: number
    blocked: boolean
    blocked_until: number
    cookie: {
      issued_at: number
      age_seconds: number
      last_ok: number
      observed_ip: string
    } | null
  }
}

export type Status = {
  version: number
  generated_at: number
  scheduled?: string[] | null
  stale_seconds: number | null
  games: Record<string, GameStatus>
  tracker: Tracker | Failed
  hirez: { quota: Quota | Failed; servers: Server[] | Failed } | Failed
}

export type Breakdown = {
  key: string
  name: string
  plays: number
  wins: number
  win_percent: number | null
  icon?: string
}

export type DayPoint = { date: string; matches: number }

export type PerDay = {
  all: DayPoint[]
  by_queue: Record<string, DayPoint[]>
  queues: { key: string; name: string; matches: number }[]
}

export type GameStats = {
  built: boolean
  total_plays?: number
  high_mmr_plays?: number
  distinct_gods?: number
  distinct_queues?: number
  queues?: Breakdown[]
  roles?: Breakdown[]
  gods?: Breakdown[]
  gods_total?: number
  gods_by_queue?: Record<string, Breakdown[]>

  matches_per_day?: PerDay | Failed
}

export type Stats = {
  version: number
  generated_at: number
  stale_seconds: number | null
  games: Record<string, GameStats | Failed>
}

export type RankedEntry = {
  queue: string
  tier: string
  tier_id: number
  mmr: number
  points: number
  wins: number
  losses: number
  leaves: number
}

export type GodEntry = {
  god_id: number
  god: string | null
  icon?: string | null
  worshippers: number
  rank: number
  wins: number
  losses: number
  kills: number
  deaths: number
  assists: number
}

export type Player = {
  name: string
  found: boolean
  private?: boolean
  error?: string
  avatar_url?: string
  level?: number
  platform?: string
  region?: string
  clan?: string | null
  created_at?: string | null
  last_login_at?: string | null
  last_played_at?: string | null
  leaves?: number
  total_worshippers?: number
  totals?: {
    kills: number
    deaths: number
    assists: number
    gold: number
    wins: number
    losses: number
    matches: number
    minutes: number
    kda: number
    win_percent: number
  }
  best_queue?: { queue: string; win_percent: number; matches: number } | null
  ranked?: RankedEntry[]
  top_gods?: GodEntry[]
}

export type Smite2Mode = {
  name: string
  matches: number
  wins: number
  losses: number
  win_percent: number
  kda: number
  skill_rating: number | null
}

export type Smite2God = {
  god: string
  icon?: string | null
  matches: number
  wins: number
  losses: number
  win_percent: number
  kda: number
}

export type Smite2Player = {
  id: string
  platform?: string
  handle?: string
  /** Steam display name — tracker.gg is the only source for it. */
  name?: string
  avatar_url?: string | null
  found: boolean
  error?: string
  matches?: number
  wins?: number
  losses?: number
  win_percent?: number | null
  kills?: number
  deaths?: number
  assists?: number
  kda?: number
  damage?: number
  gold?: number
  minutes?: number | null
  skill_rating?: number | null
  peak_skill_rating?: number | null
  best_mode?: { name: string; win_percent: number; matches: number } | null
  modes?: Smite2Mode[]
  top_gods?: Smite2God[]
}

export type Players = {
  version: number
  generated_at: number
  stale_seconds: number | null
  players: Player[]
  smite2?: {
    /** Why there is no Smite 2 data, when there isn't. Null means it ran. */
    skipped: string | null
    reason?: string
    until?: number
    players: Smite2Player[]
  }
}
