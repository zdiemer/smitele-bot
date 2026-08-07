# A build-advice API for a desktop client

**Nothing here is implemented.** This is a design sketch for an API that
`smite.diemer.codes` could serve, and for the desktop client that would call it.
No endpoint below exists; the only live routes are `/api/status`,
`/api/players`, `/api/players/{name}` and `/api/meta`, all of which serve a
snapshot and none of which compute anything.

It is written down now because the web tier is the first HTTP surface this
project has had that isn't the Discord gateway, and the choice of what that
surface looks like is easier to make before there is a client depending on it.

## The idea

A small application runs alongside Smite 1 or Smite 2, reads the screen during
god select and during the match, and answers one question continuously: *given
what is on screen right now, what should I buy next?*

The screen-reading half is the hard, unsolved half and lives entirely on the
client. The server's job is narrow: take a described game state and return a
build. That split matters — it means the server never sees a screenshot, never
needs to be trusted with one, and can be tested without a game running.

## The contract

```
POST /api/v1/advise
{
  "game":           "smite" | "smite2",
  "god_id":         1668,
  "role":           "mid",
  "queue_id":       451,
  "ally_god_ids":   [1699, 1782],
  "enemy_god_ids":  [1748, 1919, 2034],
  "items_owned":    [7545, 7538],
  "gold":           1600
}
```

```
200
{
  "build":      [7545, 7538, 19571, 19583, 19613, 19620],
  "relics":     [23049, 23050],
  "confidence": 0.61,
  "source":     "aggregate" | "model" | "optimizer",
  "next": {
    "item": 19571,
    "affordable": false,
    "gold_needed": 900
  },
  "path": [
    { "item": 7545, "spent": 700 },
    { "item": 7538, "spent": 1450 }
  ],
  "forks": {
    "ahead":  [19583, 19613],
    "behind": [19620, 19631]
  }
}
```

Everything is ids, not names. Names are localised, change between patches, and
are the client's problem — it is already doing OCR, so it is already mapping
text to something, and the mapping it should produce is an id.

`items_owned` and `gold` are what make this different from `/build` in Discord.
A recommendation that ignores what you have already bought is a recommendation
you can only act on at the fountain at minute zero.

## What already exists to wrap

All of this is Discord-free and directly callable today. None of it is exposed.

**`build_ranker.BuildStats`** — `src/SmiteBot/build_ranker.py`. Pure
pandas/numpy over the aggregate.

```python
stats = BuildStats.load(paths.game_model_dir(game))     # None if never built
stats.best_build(god_id, queue_id=451, role="mid", high_mmr=False)
stats.best_relics(god_id, queue_id, role, high_mmr)
```

`best_build` returns the build hash, item ids, plays, wins, win rate, rank, and
per-build averages. Ranking is the Agresti–Coull lower bound of a 95% interval
over recency-weighted counts, so a build with two lucky wins does not outrank
one with six hundred games. This is the highest-confidence source and the
obvious default.

**`ml.recommend.BuildRecommender`** — `src/ml/recommend.py`. The best fit for
this use case, because it is the only source that takes the *matchup*:

```python
recommender = BuildRecommender.load(paths.game_model_dir(game))
recommender.recommend(
    god_id, role="mid", opponent_god_id=1748,
    enemy_god_ids=(...), ally_god_ids=(...), top_n=3,
)  # -> [(item_ids, predicted_win_probability), ...]
```

The forward pass is numpy (`src/ml/model.py:NumpyScorer`), so serving it needs
no torch — the trainer needs torch, the server does not. Candidates are drawn
from builds actually played on that god rather than from all item combinations,
which is why it returns plausible builds rather than arithmetic ones. Measured
test AUC is about 0.675.

**`Smite2BuildOptimizer`** — `src/SmiteBot/smite2_optimizer.py`. The no-corpus
path: scores a build against measured stat profiles per `(role, attribute)`
rather than needing anyone to have played it. Mean overlap with the corpus's
top-6 is 1.95/6 over 56 gods, so it is a fallback and should be labelled as one
in `source`.

**`build_path`** — `src/SmiteBot/build_path.py`. Game-agnostic; needs only a
scorer and a pricer. This is what turns any of the above into the part a player
can actually use mid-match:

```python
build_path.order_from(items, already=items_owned, spent=gold_spent, score=..., price=...)
build_path.fork(neutral, ahead, behind, score, price)   # -> shared / ahead / behind
build_path.describe(path, currency="gold")
```

## What's in the way

**`GodBuilder` is shaped like a Discord command.** `src/SmiteBot/god_builder.py`
takes `BuildOptions` configured through `set_option("-g", "Anubis")` — option
strings parsed out of `$build -g Anubis -r Mid`. Wrapping that in HTTP would
mean an endpoint that builds a fake command line. The right move is a typed
entry point next to it that both the cog and the API call, rather than the API
calling the cog's front door.

**The desktop side is greenfield.** `src/BuildOverlay/BuildOverlay.py` is a
58-line stub that does not run — its `from ..SmiteBot.god_builder import
GodBuilder` is an invalid relative import outside a package, and `GodBuilder()`
is called with no arguments. There is no OCR, no screen capture, and no
dependency on either anywhere in the repo. Treat it as deleted.

**Smite 2 has no aggregate worth ranking from yet.** The corpus is crawled from
tracker.gg a page at a time; until it is deep enough, Smite 2 answers come from
the optimizer and should say so.

## Open questions

**Where inference runs.** `model.npz` is 0.20MB and `candidates.npz` is 0.12MB,
and the forward pass is numpy. Shipping both to the client and inferring locally
would make advice instant, work offline, and cost the server nothing — at the
price of the client going stale between releases and the model being trivially
extractable. Serving it keeps one source of truth. Leaning toward serving it
first and shipping the model later if latency turns out to matter.

**Authentication.** The read-only site is deliberately public and Authelia's
forward-auth would break a native client, which cannot complete an OIDC flow in
a useful way. An advice endpoint computes rather than reads, so it is not free
to serve. Options, roughly in order of preference: leave it public with a
per-IP rate limit; issue long-lived tokens by hand to the handful of people who
would use it; a device-code OIDC flow, which is correct and a lot of machinery
for fourteen people.

**Rate limiting.** None exists anywhere in this project — `_Base._make_request`
in `HirezAPI.py` has a note saying a throttle would go there if quota ever got
tight. An advice endpoint hit every few seconds by a client in a live match is
the first thing here that would need one, and it should be per-client rather
than global so one person's overlay cannot starve everyone else's.

**Whether it should be one request per purchase.** A client polling every two
seconds for a build that changes maybe six times a match is wasteful. A single
call at god select returning the whole path, with the client advancing through
it locally and only re-asking when the enemy build diverges, is a better shape —
and it is the shape `build_path.fork` already produces.
