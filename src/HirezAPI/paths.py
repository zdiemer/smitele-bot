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
