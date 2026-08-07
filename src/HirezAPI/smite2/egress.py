"""Which address our tracker.gg traffic leaves from, and how to name it.

A Cloudflare clearance cookie is bound to the address that solved the challenge
as much as to the user agent it was solved with. Two processes leaving from
different addresses therefore cannot share one, and handing a cookie across that
boundary is worse than having no cookie at all: every request 403s, every 403
discards the cookie and mints a replacement, and the twelve-solves-a-day cap
trips within minutes, arming a four-hour breaker.

So egress is a first-class key here rather than a deployment detail. The
clearance store is bucketed by it, and the two halves of the identity — the
browser that solves the challenge and the client that replays the cookie — are
configured from this one place so they cannot disagree.

**A rotating proxy pool cannot work with this design.** One cookie is held per
egress and replayed for hours; a pool gives a different exit per connection, so
a cookie minted on exit A is presented from exit B and refused. The proxy has to
hold one address for at least the cookie's useful life, which runs to six or
seven hours — a static address, or a provider's sticky-session endpoint.

**Several exits, walked in order, are a different thing and do work.** The
distinction is who chooses and when: a pool re-chooses per connection and the
process cannot tell, whereas `SMITELE_EGRESS_PROXY` may list several sticky
exits and the crawl moves to the next one only when tracker.gg has banned the
current address. Each entry keeps its own cookie, its own twelve-solves-a-day
budget and its own stand-down deadline, because all three are already filed
under `identity()`. That is what turns a ban from "stand down for four hours"
into "carry on from the next address", and it is why the entries have to differ
by host:port rather than only by credentials — same identity, same bucket, same
refusal.

The bucket key is the *configured* proxy, credentials stripped, rather than the
address actually observed. That is deliberate. The configured value is knowable
without a network round trip, so a cached-cookie lookup stays instant and does
not depend on an IP-echo service being up; it is stable where a real exit
address wobbles, and keying on something that wobbles would discard a good
cookie and mint a replacement every time it did — which is the exact
solver-farm signature the mint budget exists to avoid. And it changes exactly
when the operator changes something, which is the only time a bucket should
move.

The observed address is recorded on the clearance anyway, because it is free
during a mint and it is the difference between "every request 403s" and one
diagnostic line naming the two addresses that disagreed.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, List
from urllib.parse import urlsplit, urlunsplit

ENV_VAR = "SMITELE_EGRESS_PROXY"

# What the bucket is called when nothing is configured: the host's own address,
# which is what every deployment used before any of this existed.
DIRECT = "direct"

# Asked in order until one answers. More than one because a crawl that cannot
# name its own address is not fatal, but a single service being down should not
# be what decides that.
IP_ECHOES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


# The config.json fallback, read once. The environment is still consulted every
# call — it is a dict lookup — but this sits on the bot's command path, where a
# TrackerClient is built per lookup, and re-reading and re-parsing a file there
# to answer a question whose answer cannot change is pure waste.
_FROM_FILE: Optional[str] = None
_READ_FILE = False


def proxy_urls() -> List[str]:
    """Every configured exit, in preference order.

    `SMITELE_EGRESS_PROXY` may name more than one, comma-separated. One entry is
    the ordinary case and behaves exactly as it always has; several are what lets
    a crawl carry on after tracker.gg bans an address, by moving to the next one
    rather than standing down for four hours.

    THE ENTRIES MUST DIFFER BY host:port, not merely by credentials. Everything
    downstream — the clearance cookie, the twelve-solves-a-day budget, the
    stand-down deadline — is filed under `identity()`, which strips credentials
    and keeps `scheme://host:port`. Two entries that reduce to the same identity
    are the same bucket, so a cookie minted through one would be replayed from
    the other and refused every time. The cluster's egress-proxy gives each exit
    its own listener port for exactly this reason.

    Returns [] when nothing is configured, which means direct.
    """
    value = _configured()
    if not value:
        return []
    seen, out = set(), []
    for part in value.split(","):
        part = part.strip()
        # Deduplicated by identity rather than by string: the same exit named
        # twice is not two exits, and rotating onto it would only re-confirm a
        # ban it is already serving.
        if part and identity(part) not in seen:
            seen.add(identity(part))
            out.append(part)
    return out


def proxy_url() -> Optional[str]:
    """The preferred proxy, credentials and all, or None for direct.

    The first of `proxy_urls()`. Every caller that only knows about one exit —
    the bot, the one-shot checks — keeps working unchanged.
    """
    urls = proxy_urls()
    return urls[0] if urls else None


def _configured() -> Optional[str]:
    """The raw configured value.

    Read from the environment first and `config.json` second, like every other
    secret here, so a checkout keeps working from a file while the cluster is
    handed a Secret.
    """
    global _FROM_FILE, _READ_FILE  # noqa: PLW0603

    value = os.environ.get(ENV_VAR)
    if not value:
        if not _READ_FILE:
            _READ_FILE = True
            try:
                import credentials  # noqa: PLC0415

                _FROM_FILE = credentials.load().get("egressProxy")
            except Exception:  # noqa: BLE001
                # A missing or unreadable config file is not a reason to fail;
                # it just means there is no proxy, which is a valid answer.
                _FROM_FILE = None
        value = _FROM_FILE
    return value.strip() or None if value else None


def proxy_dict(url: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Playwright's proxy shape, which is what Camoufox forwards to.

    Credentials go in their own fields rather than inline in the server string:
    Playwright rejects `user:pass@host` there, and splitting it here is the only
    place that has to know.
    """
    url = proxy_url() if url is None else url
    if not url:
        return None
    parts = urlsplit(url)
    server = urlunsplit((parts.scheme, _hostport(parts), "", "", ""))
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = parts.username
    if parts.password:
        proxy["password"] = parts.password
    return proxy


def identity(url: Optional[str] = None) -> str:
    """A stable name for this egress, safe to write to disk and to log.

        http://bot:hunter2@gate.example.net:8080 -> http://gate.example.net:8080

    Credentials are stripped rather than hashed so the value stays readable in
    a state file and a log line; the whole point is that an operator can see
    which bucket is which.
    """
    url = proxy_url() if url is None else url
    if not url:
        return DIRECT
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        # Unparseable, but still configured — never fall back to DIRECT here, or
        # a typo would silently share the unproxied bucket's cookie.
        return "proxy"
    return urlunsplit((parts.scheme, _hostport(parts), "", "", ""))


async def observed_ip(
    url: Optional[str] = None, timeout: float = 10.0
) -> Optional[str]:
    """What the internet sees when we speak through this egress, or None.

    Uses the same HTTP client the crawl does, rather than camoufox's own
    `public_ip`, for three reasons: the address checked is then the one the crawl
    will actually leave from, the call does not block the event loop the way a
    synchronous `requests` walk through six URLs would, and nothing here depends
    on a camoufox internal.
    """
    url = proxy_url() if url is None else url
    try:
        from curl_cffi import requests as curl_requests  # noqa: PLC0415
    except ImportError:
        return None

    kwargs = {"trust_env": False}
    if url:
        kwargs["proxy"] = url

    try:
        async with curl_requests.AsyncSession(**kwargs) as session:
            for echo in IP_ECHOES:
                try:
                    response = await session.get(echo, timeout=timeout)
                except Exception:  # noqa: BLE001
                    continue
                if response.status_code == 200:
                    address = response.text.strip()
                    if address:
                        return address
    except Exception:  # noqa: BLE001
        return None
    return None


def _hostport(parts) -> str:
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)
