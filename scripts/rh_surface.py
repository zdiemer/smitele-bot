#!/usr/bin/env python3
"""What can *this* token actually call? Map the RallyHere surface it unlocks.

The RallyHere OpenAPI spec is published per service
(github.com/RallyHereInteractive/openapi-spec-environment/schemas/*.json), and a
captured Smite 2 player token carries a specific set of permissions. This
cross-references the two: for every operation in every schema, it reads the
"Required Permissions" the spec names and decides whether the token holds one.

Why this exists rather than a generated client
----------------------------------------------
The spec is not trustworthy enough to build from. Measured against a live token,
it was wrong in both directions on the load-bearing question:
`/users/v1/platform-user` is documented as the handle lookup and 403s, while the
un-annotated `/users/v1/player` resolves handles freely; and the token holds
`session:read-player:any`, which the spec implies a player never gets. So this
tool does not trust its own verdict — it prints what the spec *claims* is
reachable, and (`--probe`) actually calls the safe, self-scoped GETs to see. The
report is a starting map for hand-wiring endpoints one at a time, each verified
live, not a license to generate a client that inherits the spec's mistakes.

Permission matching
-------------------
A token permission satisfies a required one if it is equal or broader:
`session:*` (token) covers `session:read-player:any` (required), and an exact
match covers itself. Holding a narrower permission never satisfies a broader
requirement. An operation whose description names no permission is reported
OPEN — needs only a bearer — which is exactly the class `/users/v1/player` falls
in and is the most interesting to probe, since the spec says least about it.

Usage
-----
    # First time: fetch the schema files (or pass a dir you already have):
    python scripts/rh_surface.py --fetch ./rhschemas

    # Map only (no requests):
    python scripts/rh_surface.py --schemas ./rhschemas --capture rh_capture.json

    # Map, then live-probe the self-scoped GETs the token should be able to call:
    python scripts/rh_surface.py --schemas ./rhschemas --capture rh_capture.json \\
        --probe --base-url https://api-smite2.titanforgegames.com

Read-only throughout. `--probe` only ever issues GETs, only to paths it can fill
entirely from the token's own identity (self uuid / player id), so it asks
nothing about anyone else.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# A permission is `service:action[:scope[:…]]` — lowercase words, colons, and a
# possible trailing `*`. This is what we pull out of the prose descriptions.
_PERM_RE = re.compile(r"`([a-z0-9][a-z0-9_-]*(?::[a-z0-9_*-]+)+|\*)`")


# The per-service schema files, and where they live upstream.
_SCHEMA_SERVICES = (
    "ad config custom events file friends guide inventory leaderboard match "
    "notification presence rank sanctions session settings stage users"
).split()
_SCHEMA_BASE = (
    "https://raw.githubusercontent.com/RallyHereInteractive/"
    "openapi-spec-environment/main/schemas/"
)


def fetch_schemas(dest: str) -> None:
    """Download the current schema files into `dest`. One-time, then cached."""
    import urllib.request  # noqa: PLC0415

    os.makedirs(dest, exist_ok=True)
    for service in _SCHEMA_SERVICES:
        url = _SCHEMA_BASE + service + ".json"
        urllib.request.urlretrieve(url, os.path.join(dest, service + ".json"))
        print(f"  fetched {service}.json")
    print(f"\n{len(_SCHEMA_SERVICES)} schemas in {dest}")


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    parts = token.strip().split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return {}


def token_permissions(claims: Dict[str, Any]) -> Set[str]:
    """Every permission-shaped string anywhere in the token payload."""
    found: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.count(":") >= 1 and " " not in node:
                found.add(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(claims)
    return found


def covers(held: str, required: str) -> bool:
    """Does a held permission satisfy a required one (equal or broader)?"""
    if held == required or held == "*":
        return True
    if held.endswith("*"):
        prefix = held[:-1]  # "session:*" -> "session:"
        return required.startswith(prefix)
    return False


def satisfies(held: Set[str], required: List[str]) -> List[str]:
    """The subset of `required` options the token can satisfy."""
    return [req for req in required if any(covers(h, req) for h in held)]


@dataclass
class Op:
    service: str
    method: str
    path: str
    operation_id: str
    summary: str
    required: List[str]
    query_params: List[str]
    path_params: List[str]
    satisfied: List[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.required:
            return "OPEN"
        return "CALLABLE" if self.satisfied else "BLOCKED"

    @property
    def cross_player(self) -> bool:
        """Whether a satisfied permission reaches beyond the caller (`:any`/`*`)."""
        return any(
            s.endswith(":any") or s.endswith(":*") or s == "*" for s in self.satisfied
        )


def extract_required(description: str) -> List[str]:
    """The distinct permission strings named in a Required-Permissions block."""
    if "Required Permission" not in description:
        # No permission block at all — treat as none-required (OPEN).
        return []
    perms = _PERM_RE.findall(description)
    # Stable, de-duplicated.
    return sorted(dict.fromkeys(perms))


def load_ops(schema_dir: str, held: Set[str]) -> List[Op]:
    ops: List[Op] = []
    for name in sorted(os.listdir(schema_dir)):
        if not name.endswith(".json"):
            continue
        service = name[:-5]
        with open(os.path.join(schema_dir, name), "r", encoding="utf-8") as handle:
            spec = json.load(handle)
        for path, methods in (spec.get("paths") or {}).items():
            for method, op in methods.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                params = op.get("parameters", []) or []
                required = extract_required(op.get("description", "") or "")
                entry = Op(
                    service=service,
                    method=method.upper(),
                    path=path,
                    operation_id=op.get("operationId", ""),
                    summary=op.get("summary", ""),
                    required=required,
                    query_params=[q["name"] for q in params if q.get("in") == "query"],
                    path_params=[q["name"] for q in params if q.get("in") == "path"],
                )
                entry.satisfied = satisfies(held, required)
                ops.append(entry)
    return ops


def report(ops: List[Op]) -> None:
    by_service: Dict[str, List[Op]] = {}
    for op in ops:
        by_service.setdefault(op.service, []).append(op)

    total = {"OPEN": 0, "CALLABLE": 0, "BLOCKED": 0}
    print("== reachable surface by service ==\n")
    for service in sorted(by_service):
        counts = {"OPEN": 0, "CALLABLE": 0, "BLOCKED": 0}
        for op in by_service[service]:
            counts[op.verdict] += 1
            total[op.verdict] += 1
        print(
            f"  {service:14s} open {counts['OPEN']:3d}   callable {counts['CALLABLE']:3d}"
            f"   blocked {counts['BLOCKED']:3d}"
        )
    print(
        f"\n  {'TOTAL':14s} open {total['OPEN']:3d}   callable {total['CALLABLE']:3d}"
        f"   blocked {total['BLOCKED']:3d}"
    )

    reads = [
        op
        for op in ops
        if op.method == "GET" and op.verdict in ("OPEN", "CALLABLE")
    ]
    cross = [op for op in reads if op.cross_player]
    print(
        f"\n== GET endpoints the token should reach: {len(reads)} "
        f"({len(cross)} reach other players) ==\n"
    )
    for op in sorted(reads, key=lambda o: (o.service, o.path)):
        flag = " [cross-player]" if op.cross_player else ""
        perms = "open" if not op.required else "via " + ", ".join(op.satisfied)
        print(f"  {op.service}: GET {op.path}{flag}")
        print(f"      {op.operation_id or op.summary}  ({perms})")


def self_probe_paths(ops: List[Op], self_uuid: str, self_player_id: str) -> List[Op]:
    """The GETs fillable entirely from the caller's own identity."""
    fillable_names = {
        "player_uuid": self_uuid,
        "player_id": self_player_id,
    }
    out = []
    for op in ops:
        if op.method != "GET" or op.verdict == "BLOCKED":
            continue
        if all(name in fillable_names for name in op.path_params):
            out.append(op)
    return out


def fill(path: str, self_uuid: str, self_player_id: str) -> str:
    return path.replace("{player_uuid}", self_uuid).replace(
        "{player_id}", self_player_id
    )


# Multi-megabyte static catalog dumps. They answer 200 and are not player data,
# so probing them each run just moves ~80MB for no new information; skip by path.
_HEAVY = ("/catalog", "/loot", "/vendor")


async def probe(ops: List[Op], base_url: str, token: str, self_uuid: str,
                self_player_id: str) -> None:
    import asyncio  # noqa: PLC0415
    import aiohttp  # noqa: PLC0415 — only needed with --probe

    targets = self_probe_paths(ops, self_uuid, self_player_id)
    # Only fully-fillable paths; anything with a param we cannot supply is out.
    targets = [op for op in targets if "{" not in fill(op.path, self_uuid, self_player_id)]
    skipped = [op for op in targets if any(h in op.path for h in _HEAVY)]
    targets = [op for op in targets if op not in skipped]
    print(f"\n== live self-probe: {len(targets)} GET(s) "
          f"({len(skipped)} heavy catalog dumps skipped) ==\n")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        for op in sorted(targets, key=lambda o: (o.service, o.path)):
            url = base_url.rstrip("/") + "/" + op.service + fill(
                op.path, self_uuid, self_player_id
            )
            try:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)
                ) as response:
                    body = await response.text()
                    status = response.status
            # Broad on purpose: a discovery sweep must not die on one slow or
            # misbehaving endpoint. A total-timeout can surface as TimeoutError
            # or, on some aiohttp builds, a bare CancelledError — catch both and
            # move on, but never swallow a real interrupt.
            except KeyboardInterrupt:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                print(f"  {op.service}: GET {op.path}  NETWORK {type(error).__name__}")
                continue
            except asyncio.CancelledError:
                print(f"  {op.service}: GET {op.path}  TIMEOUT")
                continue
            note = f"  {len(body)}b" if status == 200 else ""
            surprise = (
                "  <- spec said BLOCKED" if status == 200 and op.verdict == "BLOCKED"
                else "  <- spec said reachable" if status in (401, 403) and op.verdict != "BLOCKED"
                else ""
            )
            print(f"  {op.service}: GET {op.path}  -> {status}{note}{surprise}")


def load_capture(path: str) -> Tuple[str, str, str, str]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    token = data.get("token") or data.get("access_token") or ""
    claims = decode_jwt_claims(token)
    base_url = data.get("base_url") or "https://api-smite2.titanforgegames.com"
    self_uuid = str(claims.get("active_player_uuid") or claims.get("player_uuid") or "")
    self_player_id = str(claims.get("active_player_id") or "")
    return token, base_url, self_uuid, self_player_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fetch", metavar="DIR", help="download schema files here, then exit")
    parser.add_argument("--schemas", help="dir of RallyHere *.json schemas")
    parser.add_argument("--capture", help="rh_capture.json (for the token)")
    parser.add_argument("--probe", action="store_true", help="live-probe self-scoped GETs")
    parser.add_argument("--base-url", default=None, help="override the env host")
    args = parser.parse_args()

    if args.fetch:
        fetch_schemas(args.fetch)
        return 0
    if not args.schemas or not args.capture:
        parser.error("--schemas and --capture are required (or use --fetch first)")

    token, base_url, self_uuid, self_player_id = load_capture(args.capture)
    base_url = args.base_url or base_url
    held = token_permissions(decode_jwt_claims(token))
    print(f"token holds {len(held)} permissions; self {self_uuid or '?'}\n")

    ops = load_ops(args.schemas, held)
    report(ops)

    if args.probe:
        if not self_uuid:
            print("\n(no self uuid in the token — cannot self-probe)")
            return 0
        import asyncio  # noqa: PLC0415

        asyncio.run(probe(ops, base_url, token, self_uuid, self_player_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
