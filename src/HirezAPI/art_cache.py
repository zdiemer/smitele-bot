"""Fetch-and-cache for god, item and skin art.

The four call sites for this all had the same shape, and the same flaw: they
wrote the response body to the cache unconditionally. Hi-Rez's CDN serves 403
for some icons — every Y10 item, currently — so the error page was cached under
the icon's name and returned forever after, and the image never loaded again
even once the URL started working.

Only decodable images are cached now. A failed fetch still returns its bytes, so
callers keep the behaviour they had (their own try/except renders a blank tile),
but nothing poisons the cache and the next call retries the network.
"""

from __future__ import annotations

import io
import os
import re
from typing import List

import aiohttp

import paths

# Enough to recognise the formats Hi-Rez serves: PNG, JPEG, GIF, WEBP.
_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def looks_like_image(data: bytes) -> bool:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return any(data.startswith(prefix) for prefix in _MAGIC)


def candidate_urls(url: str) -> List[str]:
    """The URL, plus rewrites for the ways Hi-Rez misreports its own art paths.

    Their API returns icon paths carrying a season prefix — y10-bloodforge.jpg
    — for art that is only ever published unprefixed. Every one of the 44
    prefixed item icons in the current catalogue 403s as given, and every one
    resolves with the prefix removed, so this is the API describing its own CDN
    incorrectly rather than the art being gone.
    """
    candidates = [url]

    unprefixed = re.sub(r"(/)y\d+-", r"\1", url)
    if unprefixed != url:
        candidates.append(unprefixed)

    return candidates


async def fetch(url: str, *cache_parts: str) -> io.BytesIO:
    """Return the art at `url`, caching it under CACHE_DIR/<cache_parts>.

    The cache key is always derived from the URL the caller asked for, so a
    fallback resolving elsewhere still lands in the expected place.
    """
    path = paths.cache_file(*cache_parts)

    if os.path.isfile(path):
        with open(path, "rb") as cached:
            data = cached.read()
        if looks_like_image(data):
            return io.BytesIO(data)
        # A poisoned entry from before this check existed.
        try:
            os.remove(path)
        except OSError:
            pass

    data = b""
    async with aiohttp.ClientSession() as session:
        for candidate in candidate_urls(url):
            try:
                async with session.get(candidate) as response:
                    body = await response.content.read()
            except aiohttp.ClientError as error:
                print(f"Failed fetching {candidate}: {error}", flush=True)
                continue

            if response.status == 200 and looks_like_image(body):
                data = body
                break

            print(
                f"Not usable: {candidate} — HTTP {response.status}, "
                f"{len(body)} bytes, image={looks_like_image(body)}",
                flush=True,
            )
            data = data or body

    if looks_like_image(data):
        try:
            with open(path, "wb") as out:
                out.write(data)
        except OSError:
            # A read-only cache is not a reason to fail the request.
            pass

    return io.BytesIO(data)
