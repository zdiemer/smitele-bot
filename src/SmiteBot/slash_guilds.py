"""The guilds slash commands are registered to.

Guild-scoped rather than global because guild commands appear the moment the bot
connects, where global ones take up to an hour to propagate.

This lived in `smitele_bot`, but only three of fourteen commands used it: the
rest carried an inline copy, usually two of these three ids, in inconsistent
order. Adding a guild meant editing twelve literals, and predictably some were
missed — `/build` and `/random_build` were registered to two guilds while
`/smitele` was registered to three, so a server could play the game but not ask
for a build.

It is its own module rather than living in `smitele_bot` because `smitetrivia`
and `player_stats` both need it and `smitele_bot` imports both of them.
"""

SLASH_COMMAND_GUILD_IDS = [
    845718807509991445,
    396874836250722316,
    480512578779611146,
]
