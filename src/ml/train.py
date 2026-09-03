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
from game import Game  # noqa: E402  pylint: disable=wrong-import-position
import recommend  # noqa: E402  pylint: disable=wrong-import-position


# Rows a calibration window needs before its two parameters mean anything.
MIN_CALIBRATION_ROWS: int = 5_000


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


def full_builds_only(
    frame: pd.DataFrame, shape: "features.Shape" = None
) -> pd.DataFrame:
    columns = (shape or features.SMITE1).item_columns
    items = frame[columns].apply(pd.to_numeric, errors="coerce").fillna(0)
    return frame[(items > 0).sum(axis=1) == len(columns)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=4096)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=2_000_000,
        help="cap on training rows; sampled uniformly when the corpus exceeds it",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=900,
        help="cap on corpus files read; sampled evenly across the whole range",
    )
    parser.add_argument("--queue", type=int, action="append", default=None)
    parser.add_argument(
        "--min-candidate-support",
        type=int,
        default=recommend.MIN_CANDIDATE_SUPPORT,
        help=(
            "times a build must appear in the corpus to be a candidate. The "
            "floor is what stops the scorer recommending a build almost nobody "
            "has played, which scores well because nothing contradicts it"
        ),
    )
    parser.add_argument(
        "--until",
        default=None,
        help=(
            "YYYY-MM-DD; train on days strictly before it. Only for building a "
            "model build_eval can score without leaking its own holdout — the "
            "shipped model wants every day there is"
        ),
    )
    parser.add_argument(
        "--game",
        default=Game.SMITE.value,
        choices=[g.value for g in Game],
        help="which game's corpus to train on",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="where to write model.npz; defaults to the game's model dir",
    )
    args = parser.parse_args()

    # The two games share no god or item vocabulary, so a model trained on one
    # cannot score the other's builds — hence a model per game, in its own
    # directory, rather than one keyed on both.
    game = Game(args.game)
    corpus_dirs = (
        [paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR]
        if game is Game.SMITE
        else [paths.game_match_data_dir(game), paths.game_match_archive_dir(game)]
    )
    out_path = args.out or os.path.join(paths.game_model_dir(game), "model.npz")
    shape = features.shape_for(game)
    print(f"Training {game.display_name} from {corpus_dirs[0]}", flush=True)
    print(
        f"  context: {', '.join(shape.context_fields)}; "
        f"skill: {', '.join(shape.skill_features) or 'none'}"
        + (f" (+{len(shape.skill_indicators)} indicators)" if shape.skill_indicators else ""),
        flush=True,
    )

    import torch

    print("Loading corpus…", flush=True)
    raw = features.load_corpus(
        corpus_dirs,
        queue_ids=args.queue,
        limit_files=args.days,
        max_files=args.max_files,
        shape=shape,
        until=args.until,
    )
    if not raw.shape[0]:
        print("No corpus data found; nothing to train on.", file=sys.stderr)
        return 1

    # Sample before building matchups, not after: that step attaches allies,
    # enemies and the lane opponent with a Python loop per row, so it is the
    # expensive part and there is no reason to pay it for rows about to be
    # discarded. Whole matches are sampled rather than rows — a match missing
    # players would produce compositions padded with absences.
    if args.max_rows and raw.shape[0] > args.max_rows * 2:
        matches = raw["Match"].drop_duplicates()
        keep = matches.sample(
            n=max(1, int(len(matches) * (args.max_rows * 2) / raw.shape[0])),
            random_state=0,
        )
        print(
            f"Sampling {len(keep):,} of {len(matches):,} matches "
            f"({raw.shape[0]:,} rows)",
            flush=True,
        )
        raw = raw[raw["Match"].isin(set(keep))]

    frame = features.build_matchup_frame(raw)
    before = frame.shape[0]
    frame = full_builds_only(frame, shape)
    print(
        f"{frame.shape[0]:,} full-build rows "
        f"({before - frame.shape[0]:,} partial builds dropped)",
        flush=True,
    )

    # A day is now ~630k player rows, so even a fortnight is millions and the
    # corpus as a whole is 158M. Sampling caps the cost at a level where more
    # data has stopped moving the score — the model went to AUC 0.675 on under
    # a million rows — while keeping the split below meaningful.
    if args.max_rows and frame.shape[0] > args.max_rows:
        print(
            f"Sampling {args.max_rows:,} of {frame.shape[0]:,} rows", flush=True
        )
        frame = frame.sample(n=args.max_rows, random_state=0)

    # Temporal split: matches are ordered by id, so the tail is the future.
    #
    # Three ways, not two. Epoch selection already reads the test window — the
    # exported model is the best epoch *on it* — so a calibration fitted there
    # too would be fitted on a window the model was chosen against, and the
    # calibration error it reported would be the flattering number rather than
    # the true one. The middle slice fits the two Platt parameters and nothing
    # else touches it; the final slice is only ever read.
    frame = frame.sort_values("Match")
    train_cut = int(frame.shape[0] * 0.8)
    calibration_cut = int(frame.shape[0] * 0.9)
    train_frame = frame.iloc[:train_cut]
    calibration_frame = frame.iloc[train_cut:calibration_cut]
    test_frame = frame.iloc[calibration_cut:]

    gods, items, roles, aspects = features.build_vocabularies(frame, shape)

    def encode(subset, stats=None):
        return features.encode(
            subset, gods, items, roles, skill_stats=stats, shape=shape, aspects=aspects
        )

    train_x, train_y, skill_stats = encode(train_frame)
    calibration_x, calibration_y, _ = encode(calibration_frame, skill_stats)
    test_x, test_y, _ = encode(test_frame, skill_stats)

    vocab_sizes = {
        "god": len(gods),
        "opponent": len(gods),
        "role": len(roles),
        "allies": len(gods),
        "enemies": len(gods),
        "items": len(items),
        "relics": len(items),
        # The starter is an item id, so it indexes the item vocabulary even
        # though its embedding table is its own.
        "starter": len(items),
        "aspect": len(aspects),
    }
    net = model_module.build_torch_model(
        vocab_sizes,
        shape.skill_width,
        context_fields=shape.context_fields,
    )
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
        "aspects": aspects.to_dict(),
        "skill_stats": skill_stats,
        "skill_features": list(shape.skill_features),
        "skill_indicators": list(shape.skill_indicators),
        "context_fields": shape.context_fields,
        "pooled_fields": list(model_module.POOLED_FIELDS),
        "item_columns": list(shape.item_columns),
        "relic_columns": list(shape.relic_columns),
        "game": game.value,
        "test_auc": roc_auc(torch_scores, test_y),
        "train_rows": int(count),
    }
    scorer = model_module.NumpyScorer(net.export(), meta)

    # Fit the calibration on its own window, then report the error on the one
    # neither training nor epoch selection has seen.
    #
    # Both windows have to be worth the name. A few hundred rows fit two
    # parameters to their own noise, and a window that came out all wins or all
    # losses has no slope in it at all — so the fit is skipped rather than
    # attempted and then judged on a comparison that is itself noise.
    calibration_scores = scorer(calibration_x)
    test_scores = scorer(test_x)
    before_ece = model_module.expected_calibration_error(test_scores, test_y)
    meta["ece_raw"] = before_ece
    meta["calibration_rows"] = int(len(calibration_y))
    usable = (
        len(calibration_y) >= MIN_CALIBRATION_ROWS
        and len(test_y) >= MIN_CALIBRATION_ROWS
        and 0 < calibration_y.sum() < len(calibration_y)
    )
    if not usable:
        print(
            f"Calibration window too thin ({len(calibration_y):,} rows); "
            "saving the model uncalibrated.",
            flush=True,
        )
    else:
        meta["calibration"] = model_module.fit_calibration(
            calibration_scores, calibration_y
        )
        after_ece = model_module.expected_calibration_error(
            scorer.calibrate(test_scores), test_y
        )
        meta["ece_calibrated"] = after_ece
        print(
            f"calibration: slope={meta['calibration']['slope']:.3f} "
            f"intercept={meta['calibration']['intercept']:.3f}  "
            f"ECE {before_ece:.4f} -> {after_ece:.4f} "
            f"on {len(test_y):,} held-out rows",
            flush=True,
        )
        if not after_ece < before_ece:
            # Two parameters fitted on one window and scored on the next can
            # lose if the meta moved between them. Shipping the raw score as a
            # percentage is worse than shipping no percentage, so the model
            # keeps its ranking and loses its claim to a rate. Written as `not
            # <` rather than `>` so a NaN — an empty bin, a degenerate fit —
            # falls on the discarding side.
            print(
                "Calibration did not improve on held-out days; "
                "saving the model uncalibrated.",
                flush=True,
            )
            meta.pop("calibration")

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

    out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    scorer.save(out)

    # The search space the bot ranks over: builds people actually ran. Written
    # alongside the model because the two share an item vocabulary and are only
    # valid together.
    candidates = recommend.extract_candidates(
        frame,
        items,
        min_support=args.min_candidate_support,
        shape=shape,
        aspects=aspects,
    )
    candidates_path = os.path.join(os.path.dirname(out), "candidates.npz")
    np.savez_compressed(
        candidates_path, **{str(god): arr for god, arr in candidates.items()}
    )

    print(
        f"Wrote {out} ({os.path.getsize(out) / 1e6:.2f} MB), "
        f"test AUC {meta['test_auc']:.4f}, "
        + (
            f"calibrated (ECE {meta['ece_calibrated']:.4f})"
            if meta.get("calibration")
            else "uncalibrated"
        ),
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
