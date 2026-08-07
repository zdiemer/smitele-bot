"""Per-item empirical value, loaded from the checked-in tables.

What an item is worth beyond what its stat line says — the execute threshold,
the cooldown refund, the aura — measured as win-rate lift over the corpus and
frozen into a JSON file per game by `src/tools/derive_item_value.py`.

Loaded from disk once, at import, rather than read from an aggregate at runtime.
That distinction is the whole point: `/build` answers "what wins" and needs a
corpus to do it, `/optimize` answers "what should work" and must keep working
for a god nobody has played and on an install with no corpus at all. A checked-in
table is calibration, the same as a hand-tuned weight, only measured.

Keyed by item name, because Smite 1's ids churn between patches and a stale name
is visible in review while a stale id silently scores zero.
"""

from __future__ import annotations

import json
import os
from typing import Dict

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str) -> Dict[str, float]:
    """One game's table, or nothing if it has not been generated yet.

    A missing file is normal rather than an error: the tables are optional
    calibration, and every optimizer works without them.
    """
    path = os.path.join(_HERE, name)
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {str(key): float(value) for key, value in loaded.items()}


SMITE: Dict[str, float] = _load("item_value_smite.json")
SMITE2: Dict[str, float] = _load("item_value_smite2.json")
