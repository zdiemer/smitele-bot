"""Runtime filesystem layout, resolved once from the environment.

Every path the bot touches at runtime used to be a literal relative to the
working directory, several of them written with Windows separators
("cache\\items", ".\\src\\match_data_collector\\output"). Off Windows those are
not paths at all — they're single filenames containing backslashes — so the art
cache silently failed to write and the match-detail walk silently found nothing.

Centralising them here does two things: it makes the separators correct
everywhere, and it lets a deployment point each class of file somewhere
appropriate. There are three:

  DATA_DIR    small, private, writable — the Hi-Rez session token, the patch
              version marker, the gods/items API caches.
  CACHE_DIR   downloaded art. Regenerable, but worth keeping across restarts.
  MATCH_DATA  the match-detail corpus written by match_data_collector and read
              back by SmiteProvider. Large and grows daily, so this is the one
              that wants network storage, and it is shared between the two
              processes.

Defaults reproduce the original in-repo layout, so running from a checkout
behaves as it always did.
"""

from __future__ import annotations

import os


def _resolve(env_var: str, default: str) -> str:
    """Resolve a directory from the environment, creating it when we can.

    A read-only mount that already exists is fine — the bot only reads the
    match corpus, and only the collector writes it — so a failure to create is
    not fatal here. It surfaces later, as a real error, on the actual write.
    """
    path = os.environ.get(env_var) or default
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


DATA_DIR: str = _resolve("SMITELE_DATA_DIR", ".")
CACHE_DIR: str = _resolve("SMITELE_CACHE_DIR", os.path.join(DATA_DIR, "cache"))

MATCH_DATA_DIR: str = _resolve(
    "SMITELE_MATCH_DATA_DIR", os.path.join("src", "match_data_collector", "output")
)
MATCH_ARCHIVE_DIR: str = _resolve(
    "SMITELE_MATCH_ARCHIVE_DIR", os.path.join("src", "match_data_collector", "archive")
)

CONFIG_FILE: str = os.environ.get("SMITELE_CONFIG_FILE") or "config.json"

# Where the trainer writes model.npz and candidates.npz. Alongside the corpus
# rather than inside it, so a model file is never picked up by anything walking
# the match directories looking for corpus files.
MODEL_DIR: str = _resolve(
    "SMITELE_MODEL_DIR", os.path.dirname(MATCH_DATA_DIR.rstrip(os.sep)) or "."
)


def data_file(name: str) -> str:
    """A small state file living in DATA_DIR."""
    return os.path.join(DATA_DIR, name)


def cache_file(*parts: str) -> str:
    """A cached asset under CACHE_DIR, creating its parent directory.

    Callers pass the tree as separate components — cache_file("gods", "icons",
    name) — so the separator is never spelled out at the call site again.
    """
    path = os.path.join(CACHE_DIR, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


# --- Per-game layout -------------------------------------------------------
#
# Smite 1 keeps every path it already had, exactly. That is not tidiness; the
# corpus is 250 days deep on a network share and the aggregate is built from
# whatever `corpus_paths` finds, so moving Smite 1 under a `smite/` subtree
# would mean relocating the lot to gain symmetry and risking the bot reading a
# half-moved directory. Smite 2 gets its own subtree instead, which also means
# `corpus_paths(MATCH_DATA_DIR, MATCH_ARCHIVE_DIR)` can never see a Smite 2 file
# and the two aggregates cannot contaminate each other.


def _game_subdir(base: str, game) -> str:
    """`base` for Smite 1, `base/smite2` for Smite 2."""
    from game import Game  # noqa: PLC0415  (circular at module scope)

    if game is Game.SMITE:
        return base
    path = os.path.join(base, game.value)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def game_data_file(game, name: str) -> str:
    """A small state file for one game — session, version marker, caches."""
    return os.path.join(_game_subdir(DATA_DIR, game), name)


def game_match_data_dir(game) -> str:
    """Where that game's corpus is written and read.

    Smite 2 is overridable separately because the two collectors may not want
    the same storage: the Hi-Rez corpus is ~6MB a day, and tracker.gg's is a
    different shape entirely.
    """
    from game import Game  # noqa: PLC0415

    if game is Game.SMITE:
        return MATCH_DATA_DIR
    return _resolve(
        "SMITELE_S2_MATCH_DATA_DIR",
        os.path.join(os.path.dirname(MATCH_DATA_DIR.rstrip(os.sep)) or ".",
                     game.value, "output"),
    )


def game_match_archive_dir(game) -> str:
    from game import Game  # noqa: PLC0415

    if game is Game.SMITE:
        return MATCH_ARCHIVE_DIR
    return _resolve(
        "SMITELE_S2_MATCH_ARCHIVE_DIR",
        os.path.join(os.path.dirname(MATCH_DATA_DIR.rstrip(os.sep)) or ".",
                     game.value, "archive"),
    )


def game_model_dir(game) -> str:
    """Where the aggregate tables and the trained model live for one game."""
    return _game_subdir(MODEL_DIR, game)


def game_cache_parts(game) -> tuple:
    """Prefix for `art_cache.fetch`, so the two games' art cannot collide.

    Both games have an Anubis, and both name his icon after the last segment of
    its URL. Without a prefix the second one fetched wins.
    """
    from game import Game  # noqa: PLC0415

    return () if game is Game.SMITE else (game.value,)
