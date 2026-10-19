"""Public SmitePrime reads. Contracts observed 2026-10-19; see docs/smite2-sources.md.

A Prime person_id is local to Prime, NOT a SmiteSource publicUuid. Only a
verified platform/account pair may be used to bridge the two services.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from curl_cffi import requests

HOST = "https://smiteprime.com"
SOURCE_NAME = "SmitePrime"
PORTALS = {"steam": 5, "epic": 28, "psn": 9, "xbl": 10}
MODES = {
    "BP_GameMode_Joust_C": "casual_joust",
    "BP_GameMode_Conquest_C": "casual_conquest",
    "BP_GameMode_Arena_C": "casual_arena",
    "BP_GameMode_Conquest_Ranked_C": "ranked_conquest",
    "BP_GameMode_Assault_OB_C": "casual_assault",
    "BP_GameMode_Joust_Duel_C": "ranked_duel",
}


class SmitePrimeUnavailable(RuntimeError):
    pass


def exact_person(body: Any, platform: str, handle: str) -> dict:
    """Never select the first fuzzy result or silently cross a platform.

    A name on a truncated page cannot be proved unique. Numeric account IDs
    are unambiguous even if the search has further pages.
    """
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise SmitePrimeUnavailable("search: unexpected envelope")
    ids, names = {}, {}
    for person in body["results"]:
        for account in person.get("players") or []:
            if account.get("portal_id") != PORTALS.get(platform):
                continue
            person_id = person.get("person_id")
            if not person_id:
                continue
            if str(account.get("portal_user_id") or "").casefold() == handle.casefold():
                ids[person_id] = (person, account)
            if str(account.get("display_name") or "").casefold() == handle.casefold():
                names[person_id] = (person, account)
    candidates = ids or (names if not body.get("next") else {})
    if len(candidates) != 1:
        raise SmitePrimeUnavailable("search: no unique exact platform/account match")
    person, account = next(iter(candidates.values()))
    return {"person_id": person["person_id"], "platform_id": account["portal_user_id"]}


class SmitePrimeClient:
    async def __aenter__(self):
        self.session = requests.AsyncSession(impersonate="firefox", trust_env=False)
        await self.session.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.session.__aexit__(*args)

    async def get(self, path: str):
        try:
            response = await self.session.get(
                f"{HOST}/api/{path}", headers={"Accept": "application/json"}, timeout=4
            )
            if response.status_code != 200:
                raise SmitePrimeUnavailable(f"HTTP {response.status_code}")
            return response.json()
        except SmitePrimeUnavailable:
            raise
        except Exception as error:
            raise SmitePrimeUnavailable(f"request failed: {type(error).__name__}") from error

    async def resolve(self, platform: str, handle: str):
        if platform not in PORTALS or not handle:
            raise SmitePrimeUnavailable("unsupported platform or empty handle")
        body = await self.get(f"player-data/players/search/{quote(handle, safe='')}/?page=1")
        return exact_person(body, platform, handle)

    async def recent_matches(self, identity: dict, limit: int):
        body = await self.get(
            f"player-data/players/{quote(identity['person_id'], safe='')}/matches/?page=1"
        )
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise SmitePrimeUnavailable("matches: unexpected envelope")
        out = []
        for row in body["results"]:
            match = row.get("match") or {}
            if match.get("state") != "closed":
                continue
            # The endpoint returns one player's row, plus other_players. Do not
            # accidentally read the latter as the requested player's stats.
            if (row.get("person") or {}).get("person_id") != identity["person_id"]:
                raise SmitePrimeUnavailable("matches: wrong person")
            if str(row.get("team_id")) not in ("1", "2") or str(match.get("winning_team")) not in ("1", "2"):
                raise SmitePrimeUnavailable("matches: missing team or winner")
            from smite2.history import summary

            god = row.get("god") or {}
            out.append(summary(
                match_id=match.get("match_id"),
                mode=MODES.get(match.get("current_mode"), match.get("current_mode") or ""),
                timestamp=match.get("start_timestamp"), duration=match.get("duration_seconds"),
                won=str(row.get("team_id")) == str(match.get("winning_team")),
                god=god.get("god_id", "").removeprefix("gods."),
                god_name=god.get("god_display_name"),
                role=str(row.get("played_role") or row.get("assigned_role") or "").removeprefix("E"),
                kills=row.get("kills"), deaths=row.get("deaths"), assists=row.get("assists"),
                rating=row.get("finishing_rank"), starting_rating=row.get("starting_rank"),
                source=SOURCE_NAME,
            ))
        return out[:limit]
