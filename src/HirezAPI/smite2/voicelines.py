"""Voice lines off wiki.smite2.com's `<God> voicelines` pages.

Smite 1 gets these by scraping smite.fandom.com for `<audio>` tags. That is not
reusable here for a reason worse than it failing: most of the roster exists in
both games, so the Smite 1 scrape would happily return Smite 1 Anubis's line as
a clue for Smite 2 Anubis — wrong in a way a player cannot detect.

The Smite 2 wiki publishes the same material as wikitext, one page per god:

    ==Taunts==
    * {{Ia|Anubis_Taunt_a.ogg}}  "I have weighed your heart..."

55 of 88 gods have a page. The rest have none, which the caller reads as an
empty list and turns into a skipped round.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from smite2 import wikitext
from smite2.wiki_client import WikiClient

# "God Selection" is the line that says the god's own name — the clue would be
# the answer. VGS is the team-callout wheel: "Attack!", "Fall back!", recorded
# per god but interchangeable, which makes for a guess with no signal in it.
_SKIP_SECTIONS = ("god selection", "vgs")

_LINE = re.compile(
    r"\{\{\s*Ia\s*\|\s*(?P<file>[^}|]+?\.ogg)\s*\}\}\s*(?P<text>[^\n]*)",
    re.IGNORECASE,
)
_TRANSCRIPT = re.compile(r"[\"“](?P<quote>[^\"”]+)[\"”]")


@dataclass(frozen=True)
class Voiceline:
    file: str
    transcript: str
    section: str
    url: Optional[str] = None


def page_title(god_name: str) -> str:
    return f"{god_name} voicelines"


def parse(page: str, god_name: str) -> List[Voiceline]:
    """Every usable line on a voicelines page.

    Lines whose transcript contains the god's own name are dropped along with
    the God Selection section — Ra saying "Ra!" is not a puzzle. The name is
    matched on whole words so that Ares is not filtered out of "ares" inside
    another word.
    """
    said = re.compile(rf"\b{re.escape(god_name)}\b", re.IGNORECASE)
    out: List[Voiceline] = []

    for heading, body in wikitext.sections(page).items():
        if heading.strip().lower() in _SKIP_SECTIONS:
            continue
        for match in _LINE.finditer(body):
            quote = _TRANSCRIPT.search(match.group("text"))
            transcript = quote.group("quote").strip() if quote else ""
            if transcript and said.search(transcript):
                continue
            out.append(
                Voiceline(
                    file=match.group("file").strip().replace("_", " "),
                    transcript=transcript,
                    section=heading.strip(),
                )
            )
    return out


async def load(client: WikiClient, god_name: str) -> List[Voiceline]:
    """A god's voice lines with playable URLs, or an empty list.

    Two requests, both cached: the page, then one batched `imageinfo` for its
    files. Fetched per god at round time rather than for all 88 up front, which
    would be ~110 requests for material a single game uses one line of.
    """
    title = page_title(god_name)
    pages = await client.query_pages([title])
    record = pages.get(title)
    if record is None or not record.get("content"):
        return []

    lines = parse(record["content"], god_name)
    if not lines:
        return []

    urls: Dict[str, str] = await client.file_urls({line.file for line in lines})
    out = []
    for line in lines:
        url = urls.get(f"File:{line.file}")
        if url:
            out.append(
                Voiceline(line.file, line.transcript, line.section, url)
            )
    return out
