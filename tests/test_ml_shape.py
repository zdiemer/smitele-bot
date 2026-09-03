"""The model's view of a corpus row, per game.

Two things here are silent when wrong, which is why they are pinned rather than
left to the training run to notice.

A shape decides which columns become inputs. Feed the Smite 1 shape to a Smite 2
frame and nothing raises: `ActiveId2` is a real column full of zeros, the three
missing skill columns are real columns full of zeros, and the starter and the
Aspect are simply never read. The model trains, reports a plausible AUC, and has
spent a quarter of its skill block on constants while ignoring the game's own
signal.

And a model file carries the fields it was trained with. If the scorer stopped
reading them back and used the module defaults instead, a Smite 2 model would be
indexed as though it had no starter and no Aspect — reading two embedding tables
through the wrong names, or falling off the end of the concatenation. The
Smite 1 model.npz on the share predates that metadata entirely, so the fallback
has to stay right as well.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import features
import model as model_module
import recommend


def test_smite1_shape_is_unchanged():
    """The Smite 1 layout is what it was before shapes existed."""
    assert features.SMITE1.item_columns == [f"ItemId{i}" for i in range(1, 7)]
    assert features.SMITE1.relic_columns == ["ActiveId1", "ActiveId2"]
    assert features.SMITE1.skill_features == features.SKILL_FEATURES
    assert features.SMITE1.skill_width == 4
    assert features.SMITE1.starter_column is None
    assert features.SMITE1.aspect_column is None
    assert features.SMITE1.context_fields == ["god", "opponent", "role"]


def test_smite2_shape_drops_constants_and_adds_its_own():
    """One relic, no dead skill columns, and the two Smite 2 has of its own."""
    assert features.SMITE2.relic_columns == ["ActiveId1"]
    # tracker.gg fills none of these, so none of them may be inputs.
    for dead in ("Account_Level", "Mastery_Level", "Conquest_Tier"):
        assert dead not in features.SMITE2.skill_features
    assert features.SMITE2.starter_column == "StarterId"
    assert features.SMITE2.aspect_column == "Aspect"
    assert features.SMITE2.context_fields == [
        "god",
        "opponent",
        "role",
        "starter",
        "aspect",
    ]
    # One rating column plus its "was this recorded at all" indicator.
    assert features.SMITE2.skill_width == 2


@pytest.mark.parametrize(
    "game, expected",
    [("smite", features.SMITE1), ("smite2", features.SMITE2), (None, features.SMITE1)],
)
def test_shape_for_takes_a_value_or_an_enum(game, expected):
    assert features.shape_for(game) is expected

    class Game:
        value = game

    assert features.shape_for(Game()) is expected


def smite2_frame(rows: int = 10) -> pd.DataFrame:
    """Two teams of five, laid out the way `rows.py` writes them."""
    frame = pd.DataFrame(
        {
            "Match": [1] * rows,
            "TaskForce": [1] * (rows // 2) + [2] * (rows // 2),
            "Winning_TaskForce": [1] * rows,
            "GodId": list(range(100, 100 + rows)),
            "Role": (["Solo", "Jungle", "Mid", "Support", "Carry"] * 2)[:rows],
            "match_queue_id": [2100002] * rows,
            "StarterId": [900 + (i % 3) for i in range(rows)],
            # Aspects are on 15% of real rows; zero means the player took none.
            "Aspect": [0, 7001, 0, 0, 7002] * (rows // 5),
            "ActiveId1": [800] * rows,
            "ActiveId2": [0] * rows,
            "Account_Level": [0] * rows,
            "Mastery_Level": [0] * rows,
            "Conquest_Tier": [0] * rows,
            "Rank_Stat_Conquest": [0, 1500, 0, 1800, 0] * (rows // 5),
        }
    )
    for slot in range(1, 7):
        frame[f"ItemId{slot}"] = [200 + slot] * rows
    return frame


def test_corpus_columns_ask_for_what_the_shape_reads():
    columns = features.corpus_columns(features.SMITE2)
    assert "StarterId" in columns and "Aspect" in columns
    assert "ActiveId2" not in columns
    assert "Account_Level" not in columns
    # And Smite 1 still asks for exactly what it always did.
    smite1 = features.corpus_columns(features.SMITE1)
    assert "ActiveId2" in smite1 and "Account_Level" in smite1
    assert "StarterId" not in smite1


def test_encode_smite2_produces_the_starter_and_aspect_fields():
    frame = features.build_matchup_frame(smite2_frame())
    gods, items, roles, aspects = features.build_vocabularies(frame, features.SMITE2)

    # The starter shares the item vocabulary rather than getting its own.
    assert all(starter in items.index_by_id for starter in (900, 901, 902))
    # Absent Aspects are not vocabulary entries; index 0 already means absent.
    assert set(aspects.index_by_id) == {7001, 7002}

    encoded, labels, _ = features.encode(
        frame, gods, items, roles, shape=features.SMITE2, aspects=aspects
    )
    assert encoded["relics"].shape[1] == 1
    assert encoded["skill"].shape[1] == 2
    assert encoded["starter"].shape == (len(frame),)
    assert (encoded["starter"] > 0).all()
    assert len(labels) == len(frame)


def test_skill_indicator_separates_unrated_from_average():
    """A zero rating is not a mid-table one, and the flag is what says so."""
    frame = smite2_frame()
    skill, stats = features.encode_skill(frame, features.SMITE2)
    rated = frame["Rank_Stat_Conquest"].to_numpy() > 0
    assert (skill[rated, 1] == 1.0).all()
    assert (skill[~rated, 1] == 0.0).all()
    # Normalisation stats are handed to inference unchanged, so the second
    # call must reuse them rather than recompute on a different window.
    again, _ = features.encode_skill(frame.iloc[:2], features.SMITE2, stats)
    assert np.allclose(again[:, 0], skill[:2, 0])


def test_smite1_encoding_is_untouched_by_the_shape_default():
    """The Smite 1 path must produce exactly the fields it produced before."""
    frame = features.build_matchup_frame(smite2_frame())
    gods, items, roles, aspects = features.build_vocabularies(frame)
    assert not aspects.index_by_id
    encoded, _, _ = features.encode(frame, gods, items, roles)
    assert "starter" not in encoded and "aspect" not in encoded
    assert encoded["relics"].shape[1] == 2
    assert encoded["skill"].shape[1] == 4


def scorer_for(fields, skill_width, calibration=None):
    """A NumpyScorer with random weights, shaped for the given fields."""
    rng = np.random.default_rng(0)
    pooled = list(model_module.POOLED_FIELDS)
    weights = {
        f"emb_{field}": rng.normal(size=(20, model_module.EMBEDDING_DIM))
        for field in list(fields) + pooled
    }
    width = model_module.EMBEDDING_DIM * len(list(fields) + pooled) + skill_width
    weights["fc1_w"] = rng.normal(size=(model_module.HIDDEN_DIM, width)) * 0.1
    weights["fc1_b"] = np.zeros(model_module.HIDDEN_DIM)
    weights["fc2_w"] = rng.normal(size=(1, model_module.HIDDEN_DIM)) * 0.1
    weights["fc2_b"] = np.zeros(1)
    meta = {"context_fields": list(fields), "pooled_fields": pooled}
    if calibration:
        meta["calibration"] = calibration
    return model_module.NumpyScorer(weights, meta)


def batch_for(fields, skill_width, rows=8):
    batch = {field: np.ones(rows, np.int64) for field in fields}
    batch.update(
        {
            "allies": np.ones((rows, 4), np.int64),
            "enemies": np.ones((rows, 5), np.int64),
            "items": np.ones((rows, 6), np.int64),
            "relics": np.ones((rows, 1), np.int64),
            "skill": np.zeros((rows, skill_width), np.float32),
        }
    )
    return batch


def test_scorer_reads_its_fields_from_its_own_metadata():
    fields = ["god", "opponent", "role", "starter", "aspect"]
    scorer = scorer_for(fields, 2)
    scores = scorer(batch_for(fields, 2))
    assert scores.shape == (8,)
    assert ((scores > 0) & (scores < 1)).all()


def test_a_model_without_field_metadata_reads_as_smite_1():
    """The model.npz already on the share has no `context_fields` key."""
    scorer = scorer_for(model_module.CONTEXT_FIELDS, 4)
    scorer.meta.pop("context_fields")
    scorer.meta.pop("pooled_fields")
    reloaded = model_module.NumpyScorer(scorer.w, scorer.meta)
    assert reloaded.context_fields == model_module.CONTEXT_FIELDS
    assert reloaded.pooled_fields == model_module.POOLED_FIELDS
    assert reloaded(batch_for(model_module.CONTEXT_FIELDS, 4)).shape == (8,)


def test_calibration_recovers_a_skewed_score():
    """Platt scaling on a deliberately overconfident score."""
    rng = np.random.default_rng(1)
    truth = rng.uniform(0.2, 0.8, 20000)
    labels = (rng.uniform(size=truth.shape) < truth).astype(float)
    # Overconfident: pushed away from 0.5, the way an over-trained sigmoid is.
    skewed = model_module._sigmoid(2.5 * model_module._logit(truth))

    before = model_module.expected_calibration_error(skewed, labels)
    fitted = model_module.fit_calibration(skewed, labels)
    scorer = model_module.NumpyScorer({}, {"calibration": fitted})
    after = model_module.expected_calibration_error(scorer.calibrate(skewed), labels)

    assert before > 0.05
    assert after < 0.01
    # A monotone map cannot reorder anything, so the ranking is untouched.
    assert (np.argsort(scorer.calibrate(skewed)) == np.argsort(skewed)).all()


def test_an_uncalibrated_model_returns_its_raw_score():
    """No calibration means no percentage, not a guessed one."""
    scorer = model_module.NumpyScorer({}, {})
    raw = np.array([0.1, 0.5, 0.9])
    assert not scorer.is_calibrated
    assert np.allclose(scorer.calibrate(raw), raw)


def test_candidates_carry_the_starter_and_aspect():
    frame = features.build_matchup_frame(smite2_frame())
    gods, items, roles, aspects = features.build_vocabularies(frame, features.SMITE2)
    candidates = recommend.extract_candidates(
        frame, items, min_support=1, shape=features.SMITE2, aspects=aspects
    )
    assert candidates
    for pool in candidates.values():
        # Six items, then the starter, then the Aspect.
        assert pool.shape[1] == 8
        assert (pool[:, 6] > 0).all()

    smite1 = recommend.extract_candidates(frame, items, min_support=1)
    for pool in smite1.values():
        assert pool.shape[1] == 6


def test_old_candidates_still_load_against_a_new_model():
    """A six-column pool must not index off the end of a Smite 2 model."""
    scorer = scorer_for(features.SMITE2.context_fields, 2)
    scorer.meta.update({"gods": {}, "items": {}, "roles": {}, "game": "smite2"})
    recommender = recommend.BuildRecommender(
        scorer, {5: np.ones((3, len(features.SMITE2.item_columns)), np.int64)}
    )
    items, starters, aspects = recommender._BuildRecommender__split(
        recommender.candidates[5]
    )
    assert items.shape == (3, 6)
    assert (starters == 0).all() and (aspects == 0).all()


def test_before_day_drops_the_holdout_and_undated_files():
    """The inverse of build_eval's cutoff, so an eval model sees no holdout."""
    paths = [
        "/c/match_details_2026-07-24.parquet",
        "/c/match_details_2026-07-25-part1.parquet",
        "/c/match_details_2026-07-26.parquet",
        "/c/match_details_2026-08-01.parquet",
        "/c/leftovers.parquet",
    ]
    kept = features.before_day(paths, "2026-07-26")
    assert kept == [
        "/c/match_details_2026-07-24.parquet",
        "/c/match_details_2026-07-25-part1.parquet",
    ]


def test_unrated_rows_are_imputed_at_the_mean_not_at_zero():
    """The bug that made the first Smite 2 model quote 6% to unrated players.

    Filling a missing rating with zero and then normalising over the mixture
    put it far below the mean instead of at it, and pulled the mean down for
    the rated rows as well.
    """
    frame = pd.DataFrame({"Rank_Stat_Conquest": [0, 0, 0, 0, 100, 200, 300]})
    skill, stats = features.encode_skill(frame, features.SMITE2)
    unrated = skill[:4, 0]
    assert np.allclose(unrated, 0.0)
    # Normalised over the observed rows alone: mean 200, so 100 sits below it
    # and 300 above, symmetrically.
    assert stats["Rank_Stat_Conquest"][0] == 200.0
    assert skill[4, 0] < 0 < skill[6, 0]
    assert np.isclose(skill[4, 0], -skill[6, 0])
    # The flag, not the value, is what still says the row was unrated.
    assert (skill[:4, 1] == 0.0).all() and (skill[4:, 1] == 1.0).all()


def test_smite1_skill_columns_keep_their_zero_fill():
    """No indicators means no imputation: Smite 1 keeps what it trained under."""
    frame = pd.DataFrame({column: [0, 0, 10, 20] for column in features.SKILL_FEATURES})
    skill, stats = features.encode_skill(frame, features.SMITE1)
    mean, _ = stats[features.SKILL_FEATURES[0]]
    assert mean == 7.5
    assert (skill[:2, 0] < 0).all()
