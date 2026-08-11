"""mitmproxy addon: lift the RallyHere token off the live Smite 2 client.

Run under mitmproxy on the Windows box that runs the game (the launcher
`Capture-RHToken.ps1` sets the proxy up for you):

    mitmdump -s capture_rh_token.py

It watches every flow to `*.rally-here.io` and pulls out the three things
`scripts/probe_rallyhere.py` needs, none of which the game hands you any other
way:

  * the **bearer token** off the `Authorization` header,
  * the **env host** (`https://<env-id>.rally-here.io`) off the request URL —
    the `<env-id>` subdomain that is otherwise unpublished, and
  * the **refresh token** off any auth/token *response* body, which is the
    thing that lets you re-mint access tokens yourself instead of re-sniffing
    the client every hour.

It decodes the (unsigned-read) JWT to show the granted session permissions and
your own player-uuid, writes everything to `rh_capture.json` (path overridable
with `RH_CAPTURE_OUT`), and prints a ready-to-paste probe command — both direct
and out the egress VPS lane.

Nothing here is signed or trusted; the JWT middle segment is read only to see
what the token *claims*, exactly as the probe does. This is a capture tool for
your own account, not a bot component.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from mitmproxy import http

RH_SUFFIX = ".rally-here.io"
# Endpoints whose *response* carries the refresh token. Kept broad on purpose:
# RallyHere has shuffled the auth path across versions, so match on any of the
# words a token endpoint tends to use rather than one fixed route.
AUTH_HINTS = ("token", "oauth", "/auth", "login", "session-ticket")


def _decode_jwt_claims(token: str) -> Optional[Dict[str, Any]]:
    """The middle segment of a JWT as a dict, or None if it isn't one.

    No signature check — this reads what the token claims, same as the probe.
    """
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return None


def _permission_strings(claims: Dict[str, Any]) -> List[str]:
    """Every `service:action[:scope]`-shaped string anywhere in the payload."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.count(":") >= 1 and " " not in node:
                found.append(node)
            else:
                found.extend(p for p in node.split() if p.count(":") >= 1)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(claims)
    return sorted(set(found))


class RallyHereCapture:
    def __init__(self) -> None:
        self.out_path = os.environ.get("RH_CAPTURE_OUT", "rh_capture.json")
        self.state: Dict[str, Any] = {}
        self._last_token: Optional[str] = None
        self._seen_hosts: set[str] = set()

    # --- diagnostics: are we even seeing the game's traffic? ----------------
    def _note_host(self, host: str) -> None:
        if host in self._seen_hosts:
            return
        self._seen_hosts.add(host)
        tag = "  <-- RallyHere" if host.endswith(RH_SUFFIX) else ""
        print(f"[capture] first flow to {host}{tag}", file=sys.stderr)

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        self._note_host(host)
        if not host.endswith(RH_SUFFIX):
            return
        auth = flow.request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return
        token = auth[7:].strip()
        if token and token != self._last_token:
            self._last_token = token
            self._on_token(token, f"{flow.request.scheme}://{host}")

    def response(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if not host.endswith(RH_SUFFIX):
            return
        path = flow.request.path.lower()
        if not any(hint in path for hint in AUTH_HINTS):
            return
        if not flow.response or not flow.response.content:
            return
        try:
            body = json.loads(flow.response.get_text())
        except (ValueError, TypeError):
            return
        if not isinstance(body, dict):
            return
        changed = False
        for key in ("refresh_token", "expires_in", "token_type"):
            if key in body and self.state.get(key) != body[key]:
                self.state[key] = body[key]
                changed = True
        # Some responses also carry a fresh access_token here — take it too.
        access = body.get("access_token")
        if isinstance(access, str) and access and access != self._last_token:
            self._last_token = access
            self._on_token(access, f"{flow.request.scheme}://{host}", write=False)
            changed = True
        if changed:
            self._write()

    # --- the capture itself -------------------------------------------------
    def _on_token(self, token: str, base_url: str, write: bool = True) -> None:
        claims = _decode_jwt_claims(token) or {}
        perms = _permission_strings(claims)
        self.state.update(
            {
                "base_url": base_url,
                "token": token,
                "self_uuid": claims.get("player_uuid") or claims.get("sub"),
                "session_perms": [p for p in perms if p.startswith("session:")],
                "exp": claims.get("exp"),
                "captured_at": int(time.time()),
            }
        )
        if write:
            self._write()
        self._announce()

    def _write(self) -> None:
        try:
            with open(self.out_path, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, sort_keys=True)
        except OSError as error:
            print(f"[capture] could not write {self.out_path}: {error}", file=sys.stderr)

    def _announce(self) -> None:
        base = self.state.get("base_url", "https://<env-id>.rally-here.io")
        uuid = self.state.get("self_uuid") or "<your-uuid>"
        exp = self.state.get("exp")
        left = f"{int(exp - time.time())}s left" if isinstance(exp, (int, float)) else "unknown ttl"
        perms = ", ".join(self.state.get("session_perms") or ["(none)"])
        refresh = "yes" if self.state.get("refresh_token") else "not seen yet"
        print("\n" + "=" * 70, file=sys.stderr)
        print(f"[capture] token refreshed  ({left})", file=sys.stderr)
        print(f"          base-url:    {base}", file=sys.stderr)
        print(f"          self-uuid:   {uuid}", file=sys.stderr)
        print(f"          session perms: {perms}", file=sys.stderr)
        print(f"          refresh token: {refresh}", file=sys.stderr)
        print(f"          written to:  {self.out_path}", file=sys.stderr)
        print("\n  probe it directly:", file=sys.stderr)
        print(
            f'    python scripts/probe_rallyhere.py --base-url {base} \\\n'
            f'        --token "$(python -c \'import json;print(json.load(open("{self.out_path}"))["token"])\')" \\\n'
            f"        --self-uuid {uuid} --other-uuid <a-friends-uuid>",
            file=sys.stderr,
        )
        print("\n  or out the egress VPS lane (set RH_PROXY_AUTH=homecluster:...):", file=sys.stderr)
        print(
            "    ... --proxy http://100.121.204.109:3129", file=sys.stderr
        )
        print("=" * 70 + "\n", file=sys.stderr)


addons = [RallyHereCapture()]
