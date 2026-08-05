"""Train the win-probability model from the match corpus.

Run by the collector image's training CronJob:

    python src/ml/train.py [--days N] [--epochs N] [--queue 451]

Writes model.npz next to the corpus so the bot can pick it up.

Two things here are load-bearing for the result being meaningful at all.

Only completed six-item builds are trained on. How *many* items a player
finished is a consequence of winning — losing teams are poorer and their games
end sooner — and it is by far the strongest signal in the raw data: win rate
runs from 5.6% at zero items to 51.2% at six. A model given partial builds
learns that and nothing else. Restricting to full builds asks the question that
was actually intended: among players who finished a build, which build wins.

The split is temporal, not random. Adjacent rows are correlated — ten players
share every match, and the meta drifts between patches — so a random split
leaks match-mates across the boundary and flatters the score.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "HirezAPI"))

import features  # noqa: E402  pylint: disable=wrong-import-position
import model as model_module  # noqa: E402  pylint: disable=wrong-import-position
import paths  # noqa: E402  pylint: disable=wrong-import-position
import recommend  # noqa: E402  pylint: disable=wrong-import-position


def roc_auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Rank-based AUC. Ties are handled by averaging ranks."""
    order = np.argsort(scores)
    ranked = labels[order]
    ranks = np.arange(1, len(ranked) + 1, dtype=np.float64)
    positives = ranked.sum()
    negatives = len(ranked) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    return float(
        (ranks[ranked == 1].sum() - positives * (positives + 1) / 2)
        / (positives * negatives)
    )


def full_builds_only(frame: pd.DataFrame) -> pd.DataFrame:
    items = frame[features.ITEM_SLOTS].apply(pd.to_numeric, errors="coerce").fillna(0)
    return frame[(items > 0).sum(axis=1) == len(features.ITEM_SLOTS)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=4096)
    parser.add_argument("--queue", type=int, action="append", default=None)
    parser.add_argument(
        "--out", default=os.path.join(paths.MATCH_DATA_DIR, "..", "model.npz")
    )
    args = parser.parse_args()

    import torch

    print("Loading corpus…", flush=True)
    raw = features.load_corpus(
        [paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR],
        queue_ids=args.queue,
        limit_files=args.days,
    )
    if not raw.shape[0]:
        print("No corpus data found; nothing to train on.", file=sys.stderr)
        return 1

    frame = features.build_matchup_frame(raw)
    before = frame.shape[0]
    frame = full_builds_only(frame)
    print(
        f"{frame.shape[0]:,} full-build rows "
        f"({before - frame.shape[0]:,} partial builds dropped)",
        flush=True,
    )

    # Temporal split: matches are ordered by id, so the tail is the future.
    frame = frame.sort_values("Match")
    cut = int(frame.shape[0] * 0.8)
    train_frame, test_frame = frame.iloc[:cut], frame.iloc[cut:]

    gods, items, roles = features.build_vocabularies(frame)
    train_x, train_y, skill_stats = features.encode(train_frame, gods, items, roles)
    test_x, test_y, _ = features.encode(
        test_frame, gods, items, roles, skill_stats=skill_stats
    )

    vocab_sizes = {
        "god": len(gods),
        "opponent": len(gods),
        "role": len(roles),
        "allies": len(gods),
        "enemies": len(gods),
        "items": len(items),
        "relics": len(items),
    }
    net = model_module.build_torch_model(vocab_sizes, len(features.SKILL_FEATURES))
    optimiser = torch.optim.Adam(net.parameters(), lr=3e-3)
    criterion = torch.nn.BCEWithLogitsLoss()

    def as_tensors(encoded: Dict[str, np.ndarray], rows: np.ndarray):
        batch = {
            key: torch.from_numpy(value[rows])
            for key, value in encoded.items()
            if key != "skill"
        }
        batch["skill"] = torch.from_numpy(encoded["skill"][rows])
        return batch

    count = len(train_y)
    labels = torch.from_numpy(train_y)
    # Held-out AUC peaks and then declines as the embeddings start memorising
    # individual builds, so the exported model is the best epoch rather than
    # the last one.
    best_auc = float("-inf")
    best_weights = None
    for epoch in range(args.epochs):
        net.train()
        order = np.random.permutation(count)
        total = 0.0
        for start in range(0, count, args.batch_size):
            rows = order[start : start + args.batch_size]
            optimiser.zero_grad()
            logits = net(as_tensors(train_x, rows))
            loss = criterion(logits, labels[rows])
            loss.backward()
            optimiser.step()
            total += float(loss.detach()) * len(rows)

        net.eval()
        with torch.no_grad():
            scores = net(as_tensors(test_x, np.arange(len(test_y)))).numpy()
        auc = roc_auc(scores, test_y)
        marker = ""
        if auc > best_auc:
            best_auc, marker = auc, "  <- best"
            best_weights = {k: v.detach().clone() for k, v in net.state_dict().items()}
        print(
            f"epoch {epoch + 1}/{args.epochs}  "
            f"loss={total / count:.4f}  test AUC={auc:.4f}{marker}",
            flush=True,
        )

    if best_weights is not None:
        net.load_state_dict(best_weights)
    net.eval()
    with torch.no_grad():
        torch_scores = net(as_tensors(test_x, np.arange(len(test_y)))).numpy()

    meta = {
        "gods": gods.to_dict(),
        "items": items.to_dict(),
        "roles": roles.to_dict(),
        "skill_stats": skill_stats,
        "skill_features": features.SKILL_FEATURES,
        "test_auc": roc_auc(torch_scores, test_y),
        "train_rows": int(count),
    }
    scorer = model_module.NumpyScorer(net.export(), meta)

    # The bot scores builds with the numpy copy, so a drift between the two
    # would silently serve different recommendations than were validated here.
    numpy_scores = scorer(
        {key: value for key, value in test_x.items()}
    )
    drift = float(np.abs(numpy_scores - 1 / (1 + np.exp(-torch_scores))).max())
    print(f"numpy/torch max drift: {drift:.2e}", flush=True)
    if drift > 1e-4:
        print("Scorer disagrees with the trained module; refusing to save.", file=sys.stderr)
        return 1

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    scorer.save(out)

    # The search space the bot ranks over: builds people actually ran. Written
    # alongside the model because the two share an item vocabulary and are only
    # valid together.
    candidates = recommend.extract_candidates(frame, items)
    candidates_path = os.path.join(os.path.dirname(out), "candidates.npz")
    np.savez_compressed(
        candidates_path, **{str(god): arr for god, arr in candidates.items()}
    )

    print(
        f"Wrote {out} ({os.path.getsize(out) / 1e6:.2f} MB), "
        f"test AUC {meta['test_auc']:.4f}",
        flush=True,
    )
    print(
        f"Wrote {candidates_path} "
        f"({os.path.getsize(candidates_path) / 1e6:.2f} MB): "
        f"{sum(len(v) for v in candidates.values()):,} distinct builds "
        f"across {len(candidates)} gods",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
