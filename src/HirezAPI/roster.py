"""The handful of people this bot is actually run for.

This map used to be a private attribute on the `PlayerStats` cog, which was the
right place while Discord was the only thing that read it: the user commands
take a `discord.Member` and need the Smite name behind it. The web snapshot
needs the same list from a process that has no Discord at all — it runs as a
CronJob out of the image whose PYTHONPATH is `src/HirezAPI:src/ml`, and
`src/SmiteBot` is not on it — so the list moves here, where both can reach it.

The direction of the two exports is the whole point. `DISCORD_TO_SMITE` answers
"who is this member?", which is a question only Discord asks. `SMITE_USERNAMES`
answers "whose stats do we cache?", which is the only question the public site
gets to ask. **Nothing outside Discord should ever publish a Discord id**, and
keeping the public view a plain tuple of game handles — names these people
already play under, visible to anyone who queues with them — is what makes that
hard to get wrong by accident rather than merely discouraged.
"""

from __future__ import annotations

from typing import Dict, Tuple

# Discord user id → Smite 1 in-game name.
#
# Smite 1 only. Smite 2 identities are `platform:handle` pairs on tracker.gg and
# do not follow from these names — someone's Steam handle is frequently not
# their Hi-Rez one, and console players have neither — so a Smite 2 roster is a
# second map to be written by hand, not a transformation of this one.
DISCORD_TO_SMITE: Dict[int, str] = {
    269238299019706369: "starfoxa",
    231849691250294784: "rawlout",
    143592135730528256: "vinnied",
    269276185656164355: "jalbagel",
    294977341648797706: "artavious",
    325874261682290688: "nastrian",
    270012612048060416: "snootin",
    232171953845305344: "indelmaen",
    145309655122313216: "tyjelly69",
    269980529942593546: "zachjak",
    267050303902187520: "mehtev4s",
    250146567011434506: "doyleville",
    478381808912695298: "NDependntVariabl",
    475838616770314240: "Guenhywvar",
}

# Discord user id → Smite 2 identity, as `platform:handle`.
#
# A second map rather than a transformation of the first, because a tracker.gg
# identity does not follow from a Hi-Rez name. It is a platform plus a handle,
# and on Steam the handle is a **SteamID64** — the 17-digit `7656119…`, not a
# vanity URL, which tracker.gg will not resolve. Every other platform keys on the
# display name. Measured across the 40,859 players the crawler has successfully
# read: steam 22,677 (all numeric ids), psn 8,838, xbl 8,089, epic 1,240, all
# names.
#
# So `platform:` is not optional decoration here. Fifty-five percent of that
# population is on Steam and the rest is not, and a Steam-only map would simply
# have no entry for the console players rather than a wrong one.
#
# The strings are exactly what `smite2.players.parse_player` already accepts, so
# nothing new parses them:
#
#     269980529942593546: "steam:76561198012345678",
#     231849691250294784: "psn:SomeHandle",
#
# Keyed by the same Discord id as the map above; the trailing comment is that
# person's Smite 1 name, so the two rosters can be read against each other.
#
# Ten of the fourteen. The four without an entry are simply absent rather than
# guessed — every caller treats a missing id as "not on the Smite 2 roster",
# which is exactly true and better than a lookup that fails against tracker.gg.
DISCORD_TO_SMITE2: Dict[int, str] = {
    269238299019706369: "steam:76561197993375857",  # starfoxa
    232171953845305344: "steam:76561198063635647",  # indelmaen
    143592135730528256: "steam:76561197995320128",  # vinnied
    269276185656164355: "steam:76561198346779239",  # jalbagel
    294977341648797706: "steam:76561198131128167",  # artavious
    325874261682290688: "steam:76561198088830987",  # nastrian
    231849691250294784: "steam:76561198041032005",  # rawlout
    270012612048060416: "steam:76561198057648464",  # snootin
    269980529942593546: "steam:76561198047678579",  # zachjak
    145309655122313216: "steam:76561198068087809",  # tyjelly69
}

# Discord user id → RallyHere player uuid, for the Smite 2 roster.
#
# A RallyHere player uuid is a *deterministic* v5 hash of the platform identity,
# so it never changes and can be resolved once and kept here rather than looked
# up at runtime — which also means this map works with no RallyHere token at all,
# the same way the two above need no API. Resolved from the Steam ids in
# `DISCORD_TO_SMITE2` by `scripts/resolve_roster_uuids.py`; rerun it when that
# map changes. The comment on each line is the Smite 1 name, as above.
#
# Why it earns its place beside the Steam handle: a live RallyHere match session
# lists its players by uuid and nothing else — no names, no handles — so the only
# way to tell which of the ten are *ours* is to have their uuids in hand first.
DISCORD_TO_SMITE2_UUID: Dict[int, str] = {
    269238299019706369: "46e70392-2196-5dce-bb32-3ff7e888570d",  # starfoxa
    232171953845305344: "30fc511a-1eb1-5fe9-8cda-b81d0e44f645",  # indelmaen
    143592135730528256: "ed46b3ae-b74d-5a77-8a45-3ad21846ff67",  # vinnied
    269276185656164355: "1e8f1aac-b875-588e-a9cf-e55a241c42c4",  # jalbagel
    294977341648797706: "fe54a83b-4a50-5b0a-bc5f-ebb44458db35",  # artavious
    325874261682290688: "3abf7712-6e6d-5110-b483-8968beaa7537",  # nastrian
    231849691250294784: "94742a88-d287-58cd-8e15-11c253523bd9",  # rawlout
    270012612048060416: "7b9de45a-24e1-5a55-ae54-65f5446cfd46",  # snootin
    269980529942593546: "6b064917-7fb1-5e8e-92f6-ddf84fa9d031",  # zachjak
    145309655122313216: "9360bc44-f77e-5cf1-8a58-a0cc1c0b3939",  # tyjelly69
}

# uuid → the name to show for that player, for naming a live session's roster.
#
# Keyed by RallyHere uuid rather than Discord id, and valued with the Smite 1
# in-game name (the friendly handle these people are known by here), so this is
# safe to use anywhere — it carries no Discord id. `?` for the one roster member
# with a uuid but, somehow, no Smite 1 name.
SMITE2_UUID_TO_NAME: Dict[str, str] = {
    uuid: DISCORD_TO_SMITE.get(discord_id, "?")
    for discord_id, uuid in DISCORD_TO_SMITE2_UUID.items()
}

# The public view: game handles, in a stable order, with no way back to a
# Discord account. Sorted case-insensitively so the site's player list does not
# reorder itself when someone is added to the middle of the map above.
SMITE_USERNAMES: Tuple[str, ...] = tuple(
    sorted(DISCORD_TO_SMITE.values(), key=str.lower)
)

# The same, for Smite 2. Sorted on the handle rather than the whole string so
# the order does not group by platform, which is an implementation detail nobody
# reading a roster cares about.
SMITE2_PLAYERS: Tuple[str, ...] = tuple(
    sorted(DISCORD_TO_SMITE2.values(), key=lambda value: value.partition(":")[2].lower())
)


def for_game(game) -> Dict[int, str]:
    """The Discord map for one game.

    Takes the `Game` enum rather than a string so a caller cannot pass "smite2"
    and silently get the Smite 1 roster.
    """
    from game import Game  # noqa: PLC0415  (circular at module scope)

    return DISCORD_TO_SMITE2 if game is Game.SMITE_2 else DISCORD_TO_SMITE
