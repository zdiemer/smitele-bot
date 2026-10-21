"""Collector identity catalogue: SmiteSource, last-good snapshot, then wiki.

This deliberately does not impersonate the bot's full stats/abilities provider.
Only the name-to-ID methods required by match normalization are provided.
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

from curl_cffi import requests
from smite2.ids import NameIndex, god_id, item_id, slugify, assert_no_collisions

# These are also the slugs in the existing wiki Data:Gods.json catalogue.
GOD_SLUGS = {'Hua Mulan': 'mulan', 'Princess Bari': 'bari'}


def page_rows(html, field):
    """Read the public page's serialized Next flight data, never execute JS."""
    chunks = []
    for raw in re.findall(r'self\.__next_f\.push\((.*?)\)</script>', html, re.S):
        try:
            chunk = json.loads(raw)
        except ValueError:
            continue
        if isinstance(chunk, list) and len(chunk) == 2 and chunk[0] == 1 and isinstance(chunk[1], str):
            chunks.append(chunk[1])
    stream = ''.join(chunks)
    marker = json.dumps(field) + ':'
    for match in re.finditer(re.escape(marker), stream):
        try:
            rows, _ = json.JSONDecoder().raw_decode(stream[match.end():].lstrip())
        except ValueError:
            continue
        if isinstance(rows, list) and rows and all(isinstance(r, dict) and 'name' in r and 'slug' in r for r in rows):
            return rows
    raise ValueError(f'SmiteSource page lacks {field} catalogue')


class Catalogue:
    def __init__(self, data):
        self.gods, self.items = {}, {}
        self._gods, self._items = NameIndex(), NameIndex()
        for kind, minimum, target, index, identifier in (
            ('gods', 50, self.gods, self._gods, god_id),
            ('items', 100, self.items, self._items, item_id),
        ):
            rows = data[kind]
            if not isinstance(rows, list) or len(rows) < minimum:
                raise ValueError(f'incomplete {kind} catalogue')
            slugs = []
            names = set()
            for row in rows:
                name, alias = row['name'], row['slug']
                if not isinstance(name, str) or not name.strip() or not isinstance(alias, str) or not alias.strip():
                    raise ValueError(f'invalid {kind} identity')
                canonical = GOD_SLUGS.get(name, slugify(name)) if kind == 'gods' else slugify(name)
                if name in names or canonical in slugs:
                    raise ValueError(f'duplicate {kind} identity')
                names.add(name); slugs.append(canonical)
                value = SimpleNamespace(id=identifier(canonical), name=name)
                target[name] = value
                index.add(name, alias)
            assert_no_collisions('god' if kind == 'gods' else 'item', slugs)

    def god_id_from_name(self, name):
        value = self.gods.get(self._gods.get(name))
        return value.id if value else None

    def item_by_name(self, name):
        return self.items.get(self._items.get(name))


async def fetch():
    async with requests.AsyncSession(impersonate='firefox', trust_env=False) as session:
        data = {}
        for path, field in [('gods', 'catalog'), ('items', 'items')]:
            response = await session.get(f'https://smitesource.com/{path}', timeout=25)
            if response.status_code != 200:
                raise RuntimeError(f'SmiteSource {path}: HTTP {response.status_code}')
            data[path] = [{"name": row["name"], "slug": row["slug"]}
                          for row in page_rows(response.text, field)]
        return data


async def load(cache_path, fetcher=fetch, wiki_factory=None):
    path = Path(cache_path)
    cached = None
    try:
        cached_data = json.loads(path.read_text())
        cached = Catalogue(cached_data)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        data = await fetcher()
        catalogue = Catalogue(data)
        if cached and any(len(data[k]) < len(cached_data[k]) * .8 for k in ('gods', 'items')):
            raise ValueError('catalogue unexpectedly shrank by more than 20%')
    except Exception as error:
        print(f'SmiteSource catalogue refresh failed: {type(error).__name__}: {error}', flush=True)
        if cached:
            return cached, 'SmiteSource-cache'
        if wiki_factory is None:
            from smite2.provider import Smite2Provider
            wiki_factory = lambda: Smite2Provider(silent=False)
        wiki = wiki_factory()
        await wiki.create()
        if not wiki.gods or not wiki.items:
            raise RuntimeError('no valid Source snapshot or wiki catalogue') from error
        return wiki, 'wiki'
    # Only a complete validated pair can replace the last-good snapshot.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.partial')
    temporary.write_text(json.dumps(data))
    temporary.replace(path)
    return catalogue, 'SmiteSource'
