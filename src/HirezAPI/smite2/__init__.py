"""Smite 2 static game data, read from wiki.smite2.com.

Hi-Rez has never opened a public Smite 2 API, and tracker.gg — the only source
of per-match Smite 2 builds — publishes no god or item metadata at all: no
abilities, no lore, no item tiers, costs or passives. The wiki has all of it,
including the item `tier` that `build_features.annotate` needs and tracker.gg
omits, so this package is what makes Smite 2 support possible rather than a
build-ranking feature in isolation.
"""
