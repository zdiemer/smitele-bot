"""The Smite 1 corpus is 250 days deep on a network share and the aggregate is
built from whatever `corpus_paths` finds in these directories. If a Smite 2 path
ever resolves inside a Smite 1 one, the aggregate silently mixes two games'
builds and nothing raises. So the separation is asserted, not assumed.
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """A fresh `paths` bound to a temporary tree.

    It resolves its constants at import time, so the environment has to be set
    before the reload rather than after.
    """
    monkeypatch.setenv("SMITELE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMITELE_MATCH_DATA_DIR", str(tmp_path / "matchdata" / "output"))
    monkeypatch.setenv(
        "SMITELE_MATCH_ARCHIVE_DIR", str(tmp_path / "matchdata" / "archive")
    )
    monkeypatch.delenv("SMITELE_CACHE_DIR", raising=False)
    monkeypatch.delenv("SMITELE_MODEL_DIR", raising=False)
    monkeypatch.delenv("SMITELE_S2_MATCH_DATA_DIR", raising=False)
    monkeypatch.delenv("SMITELE_S2_MATCH_ARCHIVE_DIR", raising=False)

    import paths as module

    return importlib.reload(module)


def test_smite1_paths_are_the_unprefixed_ones(paths):
    from game import Game

    assert paths.game_match_data_dir(Game.SMITE) == paths.MATCH_DATA_DIR
    assert paths.game_match_archive_dir(Game.SMITE) == paths.MATCH_ARCHIVE_DIR
    assert paths.game_model_dir(Game.SMITE) == paths.MODEL_DIR
    assert paths.game_data_file(Game.SMITE, "gods.json") == paths.data_file("gods.json")
    assert paths.game_cache_parts(Game.SMITE) == ()


def test_smite2_corpus_is_not_inside_the_smite1_corpus(paths):
    """`corpus_paths` walks the Smite 1 directories; a Smite 2 file found there
    would be aggregated as though it were Smite 1."""
    from game import Game

    smite2 = os.path.realpath(paths.game_match_data_dir(Game.SMITE_2))
    smite1 = os.path.realpath(paths.MATCH_DATA_DIR)
    archive = os.path.realpath(paths.MATCH_ARCHIVE_DIR)

    assert not smite2.startswith(smite1 + os.sep)
    assert not smite2.startswith(archive + os.sep)
    assert smite2 != smite1


def test_smite2_model_dir_is_separate(paths):
    from game import Game

    assert paths.game_model_dir(Game.SMITE_2) != paths.game_model_dir(Game.SMITE)
    assert paths.game_model_dir(Game.SMITE_2).endswith("smite2")


def test_smite2_art_is_cached_under_its_own_prefix(paths):
    """Both games have an Anubis and both key art on the URL's last segment."""
    from game import Game

    assert paths.game_cache_parts(Game.SMITE_2) == ("smite2",)
    one = paths.cache_file(*paths.game_cache_parts(Game.SMITE), "gods", "anubis.jpg")
    two = paths.cache_file(*paths.game_cache_parts(Game.SMITE_2), "gods", "anubis.jpg")
    assert one != two


def test_smite2_corpus_dir_is_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("SMITELE_S2_MATCH_DATA_DIR", str(tmp_path / "elsewhere"))
    import paths as module

    module = importlib.reload(module)
    from game import Game

    assert module.game_match_data_dir(Game.SMITE_2) == str(tmp_path / "elsewhere")
