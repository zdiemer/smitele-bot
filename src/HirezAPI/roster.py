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

# The public view: game handles, in a stable order, with no way back to a
# Discord account. Sorted case-insensitively so the site's player list does not
# reorder itself when someone is added to the middle of the map above.
SMITE_USERNAMES: Tuple[str, ...] = tuple(
    sorted(DISCORD_TO_SMITE.values(), key=str.lower)
)
