import io
import os
from typing import Tuple

import aiohttp

import art_cache
from god_types import GodId


class Skin(object):
    card_url: str
    god_id: GodId
    obtainability: str
    price_favor: int
    price_gems: int
    id: Tuple[int, int]
    name: str

    def __init__(self):
        pass

    @staticmethod
    def from_json(obj):
        skin = Skin()

        skin.card_url = obj["godSkin_URL"]
        skin.god_id = GodId(obj["god_id"])
        skin.obtainability = obj["obtainability"]
        skin.price_favor = int(obj["price_favor"])
        skin.price_gems = int(obj["price_gems"])
        skin.id = (obj["skin_id1"], obj["skin_id2"])
        skin.name = obj["skin_name"]
        return skin

    @staticmethod
    def coerce(value) -> "Skin":
        """A Skin, from either a Hi-Rez payload or an already-built one.

        Smite 1's provider returns the API's JSON; Smite 2's returns Skins it
        parsed from the wiki. Callers should not have to know which.
        """
        return value if isinstance(value, Skin) else Skin.from_json(value)

    async def get_card_bytes(self) -> io.BytesIO:
        if not self.has_url:
            raise ValueError(f"{self.name} is missing a URL")

        return await art_cache.fetch(
            self.card_url, "skins", art_cache.cache_key(self.card_url)
        )

    @property
    def has_url(self) -> bool:
        return self.card_url is not None and self.card_url != ""
