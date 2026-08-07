/**
 * Both upstreams, and the two independent ways tracker.gg refuses us.
 *
 * The stand-down and the clearance backoff are kept visually apart for the same
 * reason they are separate files on disk: they fail and recover independently,
 * and the fix for one is not the fix for the other. Merging them into a single
 * "blocked?" would send someone to the wrong lever.
 */

import type { Status } from '../api'
import { ago, count, duration, quotaHealth, when } from '../format'
import type { Health } from '../format'
import { Card, Meter, Row, Rows, Section } from '../components'

export default function ApiHealth({ status }: { status: Status }) {
  const hirez = status.hirez
  const tracker = status.tracker

  return (
    <>
      <h3 className="section">Hi-Rez API — Smite 1</h3>
      <Section value={hirez}>
        {(value) => (
          <>
            <div className="grid">
              <Section value={value.quota}>
                {(quota) => (
                  <>
                    <Card
                      title="Daily request quota"
                      health={quotaHealth(quota.requests_today, quota.requests_limit)}
                    >
                      <Meter
                        used={quota.requests_today}
                        limit={quota.requests_limit}
                        health={quotaHealth(quota.requests_today, quota.requests_limit)}
                        unit="requests today"
                      />
                      <p className="muted" style={{ margin: 0 }}>
                        The bot, the nightly collector and this site’s own snapshot job all
                        draw on this.
                      </p>
                    </Card>

                    <Card
                      title="Daily session cap"
                      health={quotaHealth(quota.sessions_today, quota.sessions_limit)}
                    >
                      <Meter
                        used={quota.sessions_today}
                        limit={quota.sessions_limit}
                        health={quotaHealth(quota.sessions_today, quota.sessions_limit)}
                        unit="sessions today"
                      />
                      <Rows>
                        <Row label="Active now" value={count(quota.active_sessions)} />
                        <Row
                          label="Concurrent limit"
                          value={count(quota.concurrent_limit)}
                        />
                      </Rows>
                    </Card>
                  </>
                )}
              </Section>
            </div>

            <Section value={value.servers}>
              {(servers) =>
                servers.length ? (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Platform</th>
                          <th>Environment</th>
                          <th>Status</th>
                          <th>Version</th>
                          <th>Limited access</th>
                          <th>As of</th>
                        </tr>
                      </thead>
                      <tbody>
                        {servers.map((server, index) => (
                          <tr key={`${server.platform}-${server.environment}-${index}`}>
                            <td>{server.platform ?? '—'}</td>
                            <td>{server.environment ?? '—'}</td>
                            <td>{server.status ?? '—'}</td>
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
                )
              }
            </Section>
          </>
        )}
      </Section>

      <h3 className="section">tracker.gg — Smite 2</h3>
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
              <div className="grid">
                <Card
                  title="WAF stand-down"
                  health={standdownHealth}
                  badge={value.standdown.active ? 'Serving a ban' : 'Clear'}
                  footer="The API refusing to serve us. Armed by a Retry-After above five minutes, or by more than three rate limits in one run. The crawl refuses to start while one is in force."
                >
                  {value.standdown.active ? (
                    <Rows>
                      <Row
                        label="Time left"
                        value={duration(value.standdown.remaining_seconds)}
                      />
                      <Row label="Lifts at" value={when(value.standdown.until)} />
                      <Row label="Armed" value={ago(value.standdown.armed_at)} />
                      <Row
                        label="Because"
                        value={<span className="reason">{value.standdown.reason}</span>}
                      />
                    </Rows>
                  ) : (
                    <p className="muted" style={{ margin: 0 }}>
                      No recorded refusal for <code>{value.egress}</code>.
                    </p>
                  )}
                </Card>

                <Card
                  title="Cloudflare clearance"
                  health={clearanceHealth}
                  badge={value.clearance.blocked ? 'Backed off' : undefined}
                  footer="A separate failure with a separate fix: this is the challenge solver, not the API. A cookie is bound to the address and user agent that solved it."
                >
                  <Meter
                    used={value.clearance.mints_today}
                    limit={value.clearance.mints_limit}
                    health={clearanceHealth}
                    unit="solves in the last 24h"
                  />
                  <Rows>
                    <Row label="Egress" value={<code>{value.egress}</code>} />
                    {value.clearance.blocked && (
                      <Row
                        label="Backoff until"
                        value={when(value.clearance.blocked_until)}
                      />
                    )}
                    <Row
                      label="Cookie age"
                      value={
                        value.clearance.cookie
                          ? duration(value.clearance.cookie.age_seconds)
                          : 'none held'
                      }
                      hint="Measured lifetime is about 6.7 hours."
                    />
                    <Row
                      label="Last accepted"
                      value={
                        value.clearance.cookie?.last_ok
                          ? ago(value.clearance.cookie.last_ok)
                          : '—'
                      }
                    />
                    <Row
                      label="Solved at"
                      value={
                        value.clearance.cookie?.observed_ip ? (
                          <code>{value.clearance.cookie.observed_ip}</code>
                        ) : (
                          '—'
                        )
                      }
                    />
                  </Rows>
                </Card>
              </div>
              <p className="muted" style={{ maxWidth: '68ch' }}>
                These two are tracked separately on purpose. A stand-down is tracker.gg
                refusing to serve us and lifts on a deadline; a clearance backoff is the
                solver having failed too often and clears when whatever broke is fixed.
                Clearing one does not clear the other, and the button someone reaches for
                when the crawl will not run is usually the wrong one.
              </p>
            </>
          )
        }}
      </Section>
    </>
  )
}
