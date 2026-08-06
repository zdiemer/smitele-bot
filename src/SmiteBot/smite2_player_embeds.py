"""Discord embeds for the Smite 2 player commands.

Separate from `player_stats` because that module is a thousand lines of Hi-Rez
response handling — `Player`, `PlayerId`, `QueueStats`, portal ids, tier ids —
none of which exists on this side. The two share a command name and nothing
else, so they share no code either.
"""

from __future__ import annotations

import datetime
from typing import Iterable, List, Optional, Tuple

import discord

from smite2.players import MatchSummary, Segment, best_and_worst

MINIMUM_MATCHES = 10


def _handle(platform: str, handle: str) -> str:
    return f"{handle} ({platform.upper()})"


def _duration(seconds: int) -> str:
    if not seconds:
        return "?"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder:02d}s"


def _when(timestamp: str) -> str:
    """A Discord relative timestamp, which localises itself per reader."""
    if not timestamp:
        return "?"
    try:
        moment = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp[:16]
    return f"<t:{int(moment.timestamp())}:R>"


def unavailable(reason: str) -> discord.Embed:
    return discord.Embed(color=discord.Color.red(), description=reason)


def not_found(platform: str, handle: str) -> discord.Embed:
    return discord.Embed(
        color=discord.Color.red(),
        description=(
            f"Couldn't find **{_handle(platform, handle)}** on tracker.gg.\n"
            "Smite 2 players are identified per platform — try "
            "`steam:76561198000000000`, `xbl:GamerTag`, `psn:Handle` or "
            "`epic:Name`."
        ),
    )


def queue_stats(platform: str, handle: str, modes: List[Segment]) -> discord.Embed:
    embed = discord.Embed(
        color=discord.Color.blue(),
        title=f"{_handle(platform, handle)} — Smite 2 Mode Stats",
    )
    if not modes:
        embed.description = "No matches recorded in any mode."
        return embed

    for mode in sorted(modes, key=lambda s: -s.matches)[:10]:
        lines = [
            f"**{mode.matches:,}** played · **{mode.win_rate:.1%}** won "
            f"({mode.wins:,}W / {mode.losses:,}L)",
            f"KDA **{mode.kda:.2f}** · "
            f"{mode.display.get('killsPerMatch', '?')}/"
            f"{mode.display.get('deathsPerMatch', '?')}/"
            f"{mode.display.get('assistsPerMatch', '?')} per match",
        ]
        rating = mode.stats.get("skillRating")
        if rating:
            peak = mode.stats.get("peakSkillRating")
            lines.append(
                f"Skill Rating **{rating:,.0f}**"
                + (f" (peak {peak:,.0f})" if peak else "")
            )
        embed.add_field(name=mode.name, value="\n".join(lines), inline=False)
    return embed


def rank(platform: str, handle: str, modes: List[Segment]) -> discord.Embed:
    """Skill rating per ranked mode.

    Smite 2 does not publish a tier name the way Smite 1's TierId does — the
    ranked segments carry a numeric rating and its peak, so that is what this
    shows rather than inventing a division for it.
    """
    ranked = [
        m
        for m in modes
        if m.stats.get("skillRating") or m.metadata.get("isRanked")
    ]
    embed = discord.Embed(
        color=discord.Color.blue(),
        title=f"{_handle(platform, handle)} — Smite 2 Rank",
    )
    if not ranked:
        embed.color = discord.Color.gold()
        embed.description = "No ranked matches recorded."
        return embed

    for mode in sorted(ranked, key=lambda s: -(s.stats.get("skillRating") or 0)):
        rating = mode.stats.get("skillRating")
        peak = mode.stats.get("peakSkillRating")
        embed.add_field(
            name=mode.name,
            value=(
                (f"**{rating:,.0f}** SR" if rating else "Unrated")
                + (f" · peak **{peak:,.0f}**" if peak else "")
                + f"\n{mode.matches:,} played · {mode.win_rate:.1%} won"
            ),
            inline=True,
        )
    return embed


def worshippers(
    platform: str,
    handle: str,
    gods: List[Segment],
    god_name: Optional[str] = None,
) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.blue())

    if god_name:
        wanted = god_name.strip().lower()
        found = [g for g in gods if g.name.lower() == wanted or g.key == wanted]
        if not found:
            return unavailable(
                f"**{handle}** has no recorded matches on **{god_name}**."
            )
        god = found[0]
        embed.title = f"{_handle(platform, handle)} — {god.name}"
        if god.image_url:
            embed.set_thumbnail(url=god.image_url)
        embed.description = (
            f"**{god.matches:,}** played · **{god.win_rate:.1%}** won "
            f"({god.wins:,}W / {god.losses:,}L)\n"
            f"KDA **{god.kda:.2f}** · "
            f"{god.display.get('kills', '?')} kills, "
            f"{god.display.get('deaths', '?')} deaths, "
            f"{god.display.get('assists', '?')} assists\n"
            f"Time played: {god.display.get('timePlayed', '?')}"
        )
        return embed

    embed.title = f"{_handle(platform, handle)} — Smite 2 Gods"
    if not gods:
        embed.description = "No matches recorded on any god."
        return embed

    played = sorted(gods, key=lambda s: -s.matches)
    total = sum(g.matches for g in gods)
    embed.description = (
        f"**{total:,}** matches across **{len(gods)}** gods."
    )
    embed.add_field(
        name="Most played",
        value="\n".join(
            f"**{g.name}** — {g.matches:,} played, {g.win_rate:.1%} won"
            for g in played[:5]
        ),
        inline=False,
    )
    best, worst = best_and_worst(gods, MINIMUM_MATCHES)
    if best is not None:
        embed.add_field(
            name=f"Best (min {MINIMUM_MATCHES} matches)",
            value=f"**{best.name}** — {best.win_rate:.1%} over {best.matches:,}",
            inline=True,
        )
    if worst is not None and worst is not best:
        embed.add_field(
            name="Worst",
            value=f"**{worst.name}** — {worst.win_rate:.1%} over {worst.matches:,}",
            inline=True,
        )
    if played[0].image_url:
        embed.set_thumbnail(url=played[0].image_url)
    return embed


def match_history(
    platform: str, handle: str, matches: List[MatchSummary]
) -> discord.Embed:
    embed = discord.Embed(
        color=discord.Color.blue(),
        title=f"{_handle(platform, handle)} — Recent Smite 2 Matches",
    )
    if not matches:
        embed.description = "No recent matches."
        return embed

    if matches[0].god_image:
        embed.set_thumbnail(url=matches[0].god_image)

    for match in matches:
        outcome = "🟢 Win" if match.won else "🔴 Loss"
        rating = ""
        if match.skill_rating is not None:
            delta = (
                f" ({match.skill_rating_delta:+,.0f})"
                if match.skill_rating_delta is not None
                else ""
            )
            rating = f" · SR {match.skill_rating:,.0f}{delta}"
        embed.add_field(
            name=f"{match.god_name} — {match.mode_name}",
            value=(
                f"{outcome} · **{match.kills}/{match.deaths}/{match.assists}** · "
                f"{_duration(match.duration)}{rating}\n{_when(match.timestamp)}"
            ),
            inline=False,
        )
    return embed


def first_match(
    one: Tuple[str, str], two: Tuple[str, str], found: Optional[dict], searched: int
) -> discord.Embed:
    left = _handle(*one)
    right = _handle(*two)
    if found is None:
        return discord.Embed(
            color=discord.Color.gold(),
            title="No match found together",
            description=(
                f"Couldn't find a match **{left}** and **{right}** both played "
                f"in, within the oldest {searched} pages of each history."
            ),
        )
    return discord.Embed(
        color=discord.Color.blue(),
        title=f"{left} and {right} first played together",
        description=(
            f"{_when(found['timestamp'])} in **{found.get('mode') or 'a match'}**"
            f" lasting {_duration(found.get('duration') or 0)}."
        ),
    )
