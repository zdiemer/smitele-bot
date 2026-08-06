"""One provider per game, and the rule for deciding which one an interaction wants.

`SmiteProvider` is a `Smite` subclass that also caches gods and items and holds
the build aggregate. The Smite 2 equivalent shares none of its plumbing — no
Hi-Rez auth, no session file, a wiki instead of an API — but presents the same
handful of attributes the cogs actually touch. That overlap is expressed as a
`Protocol` rather than a base class precisely so `SmiteProvider` needs no
change: it already satisfies this, bar the `game` attribute.

The two name-resolution methods are the odd ones out. They are not new
behaviour; they are three copies of the same string mangling lifted out of
`Smitele.__god_by_name`, `GodOptions.set_option` and `BuildOptions.set_option`,
which each turn "Chang'e" into an enum member their own way. A god name has to
resolve against whichever game is in play, so it cannot keep living in three
places keyed to `GodId`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from game import DEFAULT_GAME, Game


@runtime_checkable
class GameProvider(Protocol):
    """What the cogs need from a provider, regardless of game."""

    game: Game

    # Keyed by that game's god-id enum member, which differs per game — Smite 1
    # has GodId, Smite 2 derives ids from slugs. Callers get the key from
    # `god_id_from_name` rather than constructing one.
    gods: Dict[Any, Any]
    items: Dict[int, Any]

    async def create(self) -> None:
        ...

    async def load_dataframe(self) -> None:
        ...

    def load_build_stats(self) -> bool:
        ...

    def god_by_name(self, name: str) -> Optional[Any]:
        ...

    def god_id_from_name(self, name: str) -> Optional[Any]:
        ...


class Providers:
    """The registered providers, and the resolution rule.

    Resolution is deliberately in one place. Every command needs it, the answer
    decides which game's data the user gets back, and the precedence has a
    subtlety worth not reimplementing per command: Discord omits an option the
    user never touched rather than sending its default, so an absent `game:`
    means "they did not say" and must fall through to the guild's preference.
    """

    def __init__(self, *providers: GameProvider, settings: Any = None):
        self.__by_game: Dict[Game, GameProvider] = {p.game: p for p in providers}
        self.__settings = settings

    def __getitem__(self, game: Game) -> GameProvider:
        return self.__by_game[game]

    def __contains__(self, game: Game) -> bool:
        return game in self.__by_game

    def __iter__(self):
        return iter(self.__by_game.values())

    @property
    def games(self):
        """Registered games, in enum order so choice lists are stable."""
        return [game for game in Game if game in self.__by_game]

    @property
    def choices(self):
        """The `game:` option's choices — only games actually registered.

        Offering Smite 2 before a Smite 2 provider exists would put a option in
        front of users that returns an error, so the choice list is derived
        rather than written down.
        """
        return [game.display_name for game in self.games]

    def resolve(self, option: Optional[str], guild_id: Optional[int]) -> Game:
        game = (
            self.__settings.resolve(option, guild_id)
            if self.__settings is not None
            else self.__fallback(option)
        )
        # A guild that chose Smite 2 before the provider was registered — or
        # after it was removed — should degrade rather than raise.
        return game if game in self.__by_game else self.__default()

    def for_ctx(self, ctx: Any, option: Optional[str] = None) -> GameProvider:
        """The provider one interaction is about."""
        return self[self.resolve(option, getattr(ctx, "guild_id", None))]

    def __fallback(self, option: Optional[str]) -> Game:
        if option:
            try:
                return Game.from_display_name(option)
            except ValueError:
                pass
        return DEFAULT_GAME

    def __default(self) -> Game:
        if DEFAULT_GAME in self.__by_game:
            return DEFAULT_GAME
        return next(iter(self.__by_game))
