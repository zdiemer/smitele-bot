#!/usr/bin/env python3
"""Small read-only probe of the two public history adapters, without tracker."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "HirezAPI"))

from smite2.history import MultiSourcePlayerLookups, merge_histories


async def probe(platform, handle):
    lookup = MultiSourcePlayerLookups(None, silent=True)
    results = []
    for name, read in [("SmiteSource", lookup._source_history), ("SmitePrime", lookup._prime_history)]:
        try:
            rows = await asyncio.wait_for(read(platform, handle, 10), 12)
            results.append(rows)
            print(f"{name}: {len(rows)} matches; newest {rows[0].timestamp if rows else 'none'}")
        except Exception as error:
            print(f"{name}: unavailable ({type(error).__name__}: {error})")
    print(f"Combined: {len(merge_histories(results, 25))} unique matches on the sampled pages")
    return 0 if results else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=["steam", "xbl", "psn", "epic"])
    parser.add_argument("handle", help="Exact platform name or account ID; no fuzzy first-result selection")
    args = parser.parse_args()
    sys.exit(asyncio.run(probe(args.platform, args.handle)))
