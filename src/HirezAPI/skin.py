import io
from typing import Tuple

import aiohttp

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

        skin.card_url = obj['godSkin_URL']
        skin.god_id = GodId(obj['god_id'])
        skin.obtainability = obj['obtainability']
        skin.price_favor = int(obj['price_favor'])
        skin.price_gems = int(obj['price_gems'])
        skin.id = (obj['skin_id1'], obj['skin_id2'])
        skin.name = obj['skin_name']
        return skin

    async def get_card_bytes(self) -> io.BytesIO:
        if not self.has_url:
            raise ValueError(f'{self.name} is missing a URL')

        async with aiohttp.ClientSession() as session:
            async with session.get(self.card_url) as res:
                return io.BytesIO(await res.content.read())

    @property
    def has_url(self) -> bool:
        return self.card_url is not None and self.card_url != ''
