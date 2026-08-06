"""Talking to wiki.smite2.com's MediaWiki API.

Unlike tracker.gg, this is a documented, public API on a wiki that wants to be
read, so it is used the way a well-behaved client should: an identifying
User-Agent with a contact, `maxlag` so a struggling database sheds our load
first, batched titles so 266 item pages cost six requests rather than 266, and
backoff that respects `Retry-After`.

MediaWiki caps `titles` at 50 per request for anonymous clients, which is where
the batch size comes from — asking for more silently drops the remainder rather
than erroring, so the limit is enforced here instead of trusted.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, Iterator, List, Optional

import aiohttp

API_URL = "https://wiki.smite2.com/api.php"

# MediaWiki's anonymous cap. Exceeding it truncates without complaint.
TITLES_PER_REQUEST = 50

DEFAULT_USER_AGENT = (
    "smitele-bot/1.0 (https://github.com/zdiemer/smitele-bot) python-aiohttp"
)


class WikiError(RuntimeError):
    pass


def batched(items: Iterable[str], size: int = TITLES_PER_REQUEST) -> Iterator[List[str]]:
    batch: List[str] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class WikiClient:
    """A read-only MediaWiki client scoped to what the static-data layer needs.

    One `aiohttp` session for the whole run, unlike `_Base`, which opens one per
    request — there are only ever a couple of dozen requests here but they are
    all to the same host, so connection reuse is free.
    """

    MAX_RETRIES = 4
    MAXLAG_SECONDS = 5

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        session: Optional[aiohttp.ClientSession] = None,
        silent: bool = False,
    ):
        self.__user_agent = user_agent
        self.__session = session
        self.__owns_session = session is None
        self.__silent = silent

    async def __aenter__(self) -> "WikiClient":
        if self.__session is None:
            self.__session = aiohttp.ClientSession(
                headers={"User-Agent": self.__user_agent}
            )
        return self

    async def __aexit__(self, *_) -> None:
        if self.__owns_session and self.__session is not None:
            await self.__session.close()
            self.__session = None

    def __log(self, message: str) -> None:
        if not self.__silent:
            print(message, flush=True)

    async def get(self, **params: Any) -> Dict[str, Any]:
        """One API call, retried on lag and rate limiting.

        `maxlag` makes the server reject us with a retryable error when its
        replicas are behind, which is the polite failure mode: better to wait
        than to add read load to a database that is already struggling.
        """
        if self.__session is None:
            raise WikiError("WikiClient used outside its context manager")

        query = {"format": "json", "formatversion": "2", "maxlag": self.MAXLAG_SECONDS}
        query.update({k: v for k, v in params.items() if v is not None})

        delay = 1.0
        for attempt in range(self.MAX_RETRIES):
            async with self.__session.get(API_URL, params=query) as response:
                if response.status in (429, 503):
                    wait = float(response.headers.get("Retry-After", delay))
                    self.__log(f"wiki: HTTP {response.status}, waiting {wait:.0f}s")
                    await asyncio.sleep(wait)
                    delay *= 2
                    continue
                body = await response.json()

            error = body.get("error")
            if error is not None:
                if error.get("code") == "maxlag":
                    self.__log(f"wiki: replica lag, waiting {delay:.0f}s")
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise WikiError(f"{error.get('code')}: {error.get('info')}")

            return body

        raise WikiError(f"gave up after {self.MAX_RETRIES} attempts: {query}")

    async def query_pages(
        self, titles: Iterable[str], content: bool = True
    ) -> Dict[str, Dict[str, Any]]:
        """Page content and revision ids, keyed by title.

        Missing pages are omitted rather than returned with a `missing` flag, so
        callers see an absent key and can decide whether that matters. Redirects
        are resolved, because item pages routinely point at renamed articles and
        a redirect returns no content of its own.

        With `content=False` this is the cheap revision-id sweep the cache
        invalidation check uses: same batching, a fraction of the bytes.
        """
        out: Dict[str, Dict[str, Any]] = {}

        for batch in batched(titles):
            body = await self.get(
                action="query",
                prop="revisions",
                rvslots="main",
                rvprop="ids|content" if content else "ids",
                redirects=1,
                titles="|".join(batch),
            )
            page_query = body.get("query", {})

            # A redirect changes the title we get back, so map it to what was
            # asked for; otherwise the caller looks up "Bumba's Spear" and finds
            # only the page it redirected to.
            aliases = {r["to"]: r["from"] for r in page_query.get("redirects", [])}
            aliases.update({n["to"]: n["from"] for n in page_query.get("normalized", [])})

            for page in page_query.get("pages", []):
                if page.get("missing"):
                    continue
                revisions = page.get("revisions") or []
                if not revisions:
                    continue
                revision = revisions[0]
                record = {
                    "title": page["title"],
                    "pageid": page.get("pageid"),
                    "revid": revision.get("revid"),
                    "content": revision.get("slots", {})
                    .get("main", {})
                    .get("content"),
                }
                out[page["title"]] = record
                requested = aliases.get(page["title"])
                while requested is not None:
                    out[requested] = record
                    requested = aliases.get(requested)

        return out

    async def bucket(self, query: str) -> List[Dict[str, Any]]:
        """Run a Bucket query and return its rows.

        Bucket is Weird Gloop's structured-data extension, and it is the only
        enumeration of items the wiki offers — `Category:Items` holds six
        container categories and no items at all. Absent columns are omitted
        from a row rather than nulled, so read with `.get`.
        """
        body = await self.get(action="bucket", query=query)
        return body.get("bucket", [])

    async def file_urls(self, files: Iterable[str]) -> Dict[str, str]:
        """`File:` titles to CDN URLs.

        The URL carries a cache-busting query (`…/T3_Book_of_Thoth.png?8326f`)
        that changes whenever the wiki rehashes the file. It has to stay on the
        URL to fetch, but must be kept off any cache key derived from it.
        """
        out: Dict[str, str] = {}
        titles = [f if f.startswith("File:") else f"File:{f}" for f in files]

        for batch in batched(titles):
            body = await self.get(
                action="query",
                prop="imageinfo",
                iiprop="url",
                redirects=1,
                titles="|".join(batch),
            )
            page_query = body.get("query", {})
            aliases = {r["to"]: r["from"] for r in page_query.get("redirects", [])}
            aliases.update({n["to"]: n["from"] for n in page_query.get("normalized", [])})

            for page in page_query.get("pages", []):
                info = page.get("imageinfo") or []
                if not info:
                    continue
                url = info[0].get("url")
                if not url:
                    continue
                out[page["title"]] = url
                requested = aliases.get(page["title"])
                while requested is not None:
                    out[requested] = url
                    requested = aliases.get(requested)

        return out

    async def category_members(self, category: str) -> List[str]:
        """Page titles in a category, following continuation."""
        title = category if category.startswith("Category:") else f"Category:{category}"
        out: List[str] = []
        cont: Optional[str] = None

        while True:
            body = await self.get(
                action="query",
                list="categorymembers",
                cmtitle=title,
                cmlimit=500,
                cmcontinue=cont,
            )
            out.extend(m["title"] for m in body.get("query", {}).get("categorymembers", []))
            cont = body.get("continue", {}).get("cmcontinue")
            if cont is None:
                return out


def cache_key(url: str) -> str:
    """The stable part of a wiki file URL — its basename without the hash.

    `art_cache` keys on the last path segment, so without this a redeploy of the
    same image churns the whole art cache.
    """
    return url.split("?")[0].split("/")[-1]
