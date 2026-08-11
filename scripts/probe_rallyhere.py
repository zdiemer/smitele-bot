#!/usr/bin/env python3
"""Can a player's own Smite 2 token read *another* player's live match?

That is the whole question. Smite 2 runs on RallyHere, whose API is the source
tracker.gg consumes and is fresh to the second — no ten-minute snapshot. The
endpoints that would answer "who is in this lobby right now" exist and are
documented (github.com/RallyHereInteractive/openapi-spec-environment):

    GET /session/v1/player/{uuid}/session      the sessions a player is in
    GET /session/v1/session/{id}/player        who else is in that session
    GET /presence/v1/player/uuid/{uuid}/presence   coarse online/in-game state

The catch the bot would live or die on is authorization, not reachability. The
spec gates reading *another* player's session behind `session:read-player:any`;
an ordinary player token is expected to carry only `session:read-player:self`.
If that holds, the complete-lobby dream is dead for a bot no matter how the
token is obtained, and the coarse Steam/Discord signals are the ceiling. If it
does *not* hold, that is worth knowing before ruling the approach out.

This script settles it against your own account, on your own machine, and puts
nothing account-bound anywhere near the deployed bot. It is a one-shot
experiment, not a bot component — hence `scripts/`, not `src/`.

What you have to supply, because only the running game client knows them
--------------------------------------------------------------------------

  --token    A bearer token minted by *your* client. Capture it by pointing
             mitmproxy (or Fiddler/Charles) at the machine running Smite 2,
             trusting its cert, and reading the `Authorization: Bearer …`
             header off any request to `*.rally-here.io`. It is short-lived;
             capture it warm and run this within the hour.

  --base-url The RallyHere environment host, e.g.
             `https://<env-id>.rally-here.io`. The `<env-id>` subdomain is not
             published; it is the host those same sniffed requests go to. Pass
             the whole URL and this script needs to know nothing secret itself.

  --other-uuid  Another player's RallyHere player-uuid — the load-bearing
             input. Get one by sniffing a *friend's* client, or read it off a
             lobby the `:any` path returns if your token turns out to have it.
             Without it the cross-player question cannot be asked and the
             script says so rather than guessing.

The token is inspected before anything is sent: a RallyHere bearer is a JWT,
and its middle segment lists the very permissions this is about. Often the
verdict is readable straight off the token with no request made at all.

    python scripts/probe_rallyhere.py \
        --base-url https://<env-id>.rally-here.io \
        --token "$RH_TOKEN" \
        --self-uuid <your-uuid> \
        --other-uuid <a-friends-uuid>

Nothing here is wired into the bot, and nothing should be: the token is one
account's, expires fast, and reading other players' sessions is very likely
unauthorized — which is exactly the thing being measured.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover - the script says how to fix it
    raise SystemExit("this probe needs aiohttp: pip install aiohttp")


def decode_jwt_claims(token: str) -> Optional[Dict[str, Any]]:
    """The middle segment of a JWT, or None if it does not look like one.

    No signature check and none wanted: this is read to see what the token
    *claims* it may do, not to trust it. A RallyHere access token carries its
    granted permissions here, so the whole verdict is often legible before a
    single request leaves the machine.
    """
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    # JWT uses base64url without padding; restore it before decoding.
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload)
        return json.loads(raw)
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return None


def permission_strings(claims: Dict[str, Any]) -> List[str]:
    """Every permission-looking string in the token, however it is nested.

    RallyHere has moved these around across versions — a flat `permissions`
    list, a scope string, per-service claims — so this walks the whole payload
    and collects anything shaped like `service:action[:scope]` rather than
    trusting one field to be there.
    """
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.count(":") >= 1 and " " not in node:
                found.append(node)
            else:
                # A space-joined scope string ("session:read-player:self a:b").
                found.extend(
                    part for part in node.split() if part.count(":") >= 1
                )
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(claims)
    # Stable, de-duplicated.
    return sorted(set(found))


def reads_any_session(permissions: List[str]) -> Optional[bool]:
    """Whether the token claims it can read *any* player's session.

    True if an `:any`-scoped or wildcard session-read permission is present,
    False if only a `:self` one is, None if the token says nothing recognisable
    about session reads at all (in which case only a live request can tell).
    """
    session_reads = [p for p in permissions if p.startswith("session:")]
    if not session_reads:
        return None
    for perm in session_reads:
        tail = perm.split(":")[-1]
        if tail in ("any", "*") or perm in ("session:*", "*:*"):
            return True
    if any(p.endswith(":self") for p in session_reads):
        return False
    # A session read with no scope suffix is ambiguous; let the request decide.
    return None


async def probe(
    session: aiohttp.ClientSession, base_url: str, path: str, token: str
) -> Tuple[int, Any]:
    """One GET, returning (status, parsed-body-or-text). Never raises."""
    url = base_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            text = await response.text()
            try:
                return response.status, json.loads(text)
            except json.JSONDecodeError:
                return response.status, text
    except aiohttp.ClientError as error:
        return -1, f"request failed: {error}"


def summarise(status: int, body: Any) -> str:
    if status == -1:
        return f"NETWORK  {body}"
    verdict = {
        200: "OK",
        401: "UNAUTHENTICATED (token bad or expired)",
        403: "FORBIDDEN (token lacks the permission)",
        404: "NOT FOUND (no such session/player, or route moved)",
    }.get(status, f"HTTP {status}")
    shape = ""
    if isinstance(body, (dict, list)):
        shape = f" — {json.dumps(body)[:160]}"
    return f"{verdict}{shape}"


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe what a self-minted RallyHere token can read.",
    )
    parser.add_argument("--base-url", required=True, help="https://<env>.rally-here.io")
    parser.add_argument("--token", required=True, help="a Bearer token from your client")
    parser.add_argument("--self-uuid", default=None, help="your RallyHere player uuid")
    parser.add_argument(
        "--other-uuid",
        default=None,
        help="another player's uuid — the cross-player question",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="a known session id to test the membership route directly",
    )
    args = parser.parse_args()

    print("== token, before any request ==")
    claims = decode_jwt_claims(args.token)
    token_verdict: Optional[bool] = None
    if claims is None:
        print("  not a decodable JWT — cannot read permissions; requests will tell.")
    else:
        perms = permission_strings(claims)
        session_perms = [p for p in perms if p.startswith("session:")] or ["(none)"]
        print(f"  subject:     {claims.get('sub') or claims.get('player_uuid') or '?'}")
        print(f"  expires:     {claims.get('exp') or '?'}")
        print(f"  session perms: {', '.join(session_perms)}")
        token_verdict = reads_any_session(perms)
        if token_verdict is True:
            print("  -> token CLAIMS it can read any player's session.")
        elif token_verdict is False:
            print("  -> token claims only its OWN session (self-scope).")
        else:
            print("  -> token says nothing definite about session reads.")

    # Self uuid may be discoverable from the token when not passed.
    self_uuid = args.self_uuid
    if self_uuid is None and claims is not None:
        self_uuid = claims.get("player_uuid") or claims.get("sub")
        if self_uuid:
            print(f"  (using self-uuid {self_uuid} from the token)")

    checks: List[Tuple[str, str]] = []
    if self_uuid:
        checks.append(("self session", f"/session/v1/player/{self_uuid}/session"))
        checks.append(
            ("self presence", f"/presence/v1/player/uuid/{self_uuid}/presence")
        )
    if args.other_uuid:
        checks.append(
            ("OTHER session", f"/session/v1/player/{args.other_uuid}/session")
        )
        checks.append(
            ("OTHER presence", f"/presence/v1/player/uuid/{args.other_uuid}/presence")
        )
    if args.session_id:
        checks.append(
            ("session members", f"/session/v1/session/{args.session_id}/player")
        )

    if not checks:
        print("\nNo uuids to probe. Pass --self-uuid and --other-uuid to ask the")
        print("question live; the token inspection above is all there is otherwise.")
        return 0

    print("\n== live requests ==")
    other_status: Optional[int] = None
    async with aiohttp.ClientSession() as session:
        for label, path in checks:
            status, body = await probe(session, args.base_url, path, args.token)
            print(f"  {label:16s} {path}")
            print(f"    {summarise(status, body)}")
            if label.startswith("OTHER session"):
                other_status = status

    print("\n== verdict ==")
    if args.other_uuid is None:
        print("  Cross-player read UNTESTED — pass --other-uuid to settle it.")
    elif other_status == 200:
        print("  A self-minted token READ another player's session.")
        print("  The complete-lobby path is technically open; weigh it against the")
        print("  token being account-bound, short-lived and unsupported (see docstring).")
    elif other_status in (401, 403):
        print("  A self-minted token CANNOT read another player's session"
              f" (HTTP {other_status}).")
        print("  This is the expected outcome, and it closes the RallyHere path for a")
        print("  bot: the coarse Steam/Discord 'is-playing' signals are the ceiling.")
    else:
        print(f"  Inconclusive (HTTP {other_status}); the route may have moved —")
        print("  re-check the path against the current OpenAPI spec and retry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
