/**
 * Both upstreams, and the two independent ways tracker.gg refuses us.
 *
 * The stand-down and the clearance backoff get their own registers for the same
 * reason they are separate files on disk: they fail and recover independently,
 * and the fix for one is not the fix for the other. A single merged "blocked?"
 * would send someone to the wrong lever, which is the failure this layout is
 * built to prevent.
 */

import type { Status } from '../api'
import { ago, count, duration, quotaHealth, when } from '../format'
import type { Health } from '../format'
import { Band, Meter, Pair, Row, Rows, Section } from '../components'

export default function ApiHealth({ status }: { status: Status }) {
  const hirez = status.hirez
  const tracker = status.tracker

  return (
    <>
      <Section value={hirez}>
        {(value) => (
          <>
            <Section value={value.quota}>
              {(quota) => {
                const reqHealth = quotaHealth(quota.requests_today, quota.requests_limit)
                const sessHealth = quotaHealth(quota.sessions_today, quota.sessions_limit)
                return (
                  <Band
                    label="Hi-Rez quota"
                    qualifier="smite 1 · resets daily"
                    health={reqHealth === 'ok' ? sessHealth : reqHealth}
                  >
                    <Meter
                      used={quota.requests_today}
                      limit={quota.requests_limit}
                      health={reqHealth}
                      unit="requests today"
                    />
                    <Meter
                      used={quota.sessions_today}
                      limit={quota.sessions_limit}
                      health={sessHealth}
                      unit="sessions today"
                    />
                    <Pair>
                      <Rows>
                        <Row label="active sessions" value={count(quota.active_sessions)} />
                      </Rows>
                      <Rows>
                        <Row
                          label="concurrent limit"
                          value={count(quota.concurrent_limit)}
                        />
                      </Rows>
                    </Pair>
                    <p className="muted" style={{ marginBottom: 0 }}>
                      The bot, the nightly collector and this site’s own snapshot job all
                      draw on the same allowance.
                    </p>
                  </Band>
                )
              }}
            </Section>

            <Section value={value.servers}>
              {(servers) => (
                <Band
                  label="Hi-Rez servers"
                  qualifier="as Hi-Rez reports them"
                  health={
                    servers.some((s) => s.environment === 'live' && s.status !== 'UP')
                      ? 'warn'
                      : 'ok'
                  }
                >
                  {servers.length ? (
                    <div className="scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>platform</th>
                            <th>env</th>
                            <th>status</th>
                            <th>version</th>
                            <th>limited</th>
                            <th>as of</th>
                          </tr>
                        </thead>
                        <tbody>
                          {servers.map((server, index) => (
                            <tr key={`${server.platform}-${server.environment}-${index}`}>
                              <td>{server.platform ?? '—'}</td>
                              <td>{server.environment ?? '—'}</td>
                              <td className={server.status === 'UP' ? undefined : 'down'}>
                                {server.status ?? '—'}
                              </td>
                              <td>{server.version ?? '—'}</td>
                              <td>{server.limited_access ? 'yes' : 'no'}</td>
                              <td>{server.entry_datetime ?? '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="muted">Hi-Rez reported no server rows.</p>
                  )}
                </Band>
              )}
            </Section>
          </>
        )}
      </Section>

      <Section value={tracker}>
        {(value) => {
          const standdownHealth: Health = value.standdown.active ? 'bad' : 'ok'
          const clearanceHealth: Health = value.clearance.blocked
            ? 'bad'
            : value.clearance.mints_today >= value.clearance.mints_limit
              ? 'warn'
              : 'ok'

          return (
            <>
              <Band
                label="tracker.gg — WAF stand-down"
                qualifier={`egress ${value.egress}`}
                health={standdownHealth}
              >
                {value.standdown.active ? (
                  <>
                    <Pair>
                      <Rows>
                        <Row
                          label="time left"
                          value={duration(value.standdown.remaining_seconds)}
                        />
                        <Row label="lifts at" value={when(value.standdown.until)} />
                      </Rows>
                      <Rows>
                        <Row label="armed" value={ago(value.standdown.armed_at)} />
                      </Rows>
                    </Pair>
                    <p className="prose" style={{ marginBottom: 0 }}>
                      <code>{value.standdown.reason}</code>
                    </p>
                  </>
                ) : (
                  <p className="muted" style={{ marginBottom: 0 }}>
                    No recorded refusal for <code>{value.egress}</code>.
                  </p>
                )}
                <p className="muted">
                  The API refusing to serve us. Armed by a <code>Retry-After</code> above
                  five minutes, or more than three rate limits in one run. The crawl
                  refuses to start while one is in force.
                </p>
              </Band>

              <Band
                label="tracker.gg — Cloudflare clearance"
                qualifier="the challenge solver, not the API"
                health={clearanceHealth}
              >
                <Meter
                  used={value.clearance.mints_today}
                  limit={value.clearance.mints_limit}
                  health={clearanceHealth}
                  unit="solves in the last 24h"
                />
                <Pair>
                  <Rows>
                    {value.clearance.blocked && (
                      <Row
                        label="backoff until"
                        value={when(value.clearance.blocked_until)}
                      />
                    )}
                    <Row
                      label="cookie age"
                      value={
                        value.clearance.cookie
                          ? duration(value.clearance.cookie.age_seconds)
                          : 'none held'
                      }
                      absent={!value.clearance.cookie}
                      hint="Measured lifetime is about 6.7 hours."
                    />
                  </Rows>
                  <Rows>
                    <Row
                      label="last accepted"
                      value={
                        value.clearance.cookie?.last_ok
                          ? ago(value.clearance.cookie.last_ok)
                          : '—'
                      }
                      absent={!value.clearance.cookie?.last_ok}
                    />
                    <Row
                      label="solved at"
                      value={value.clearance.cookie?.observed_ip || 'not recorded'}
                      absent={!value.clearance.cookie?.observed_ip}
                    />
                  </Rows>
                </Pair>
                <p className="prose" style={{ marginBottom: 0 }}>
                  These two are tracked separately on purpose. A stand-down is tracker.gg
                  refusing to serve us and lifts on a deadline; a backoff is the solver
                  having failed too often and clears when whatever broke is fixed.
                  Clearing one does not clear the other, and the lever someone reaches for
                  first is usually the wrong one.
                </p>
              </Band>
            </>
          )
        }}
      </Section>
    </>
  )
}
