"""
tests/test_memory.py — TDD for harness/memory.py

Schema (spec §5.3):
{
  "scene": "res://rl/train_map.tscn",
  "rounds": [
    {"round": 3, "target_issue": "...", "change_type": "tunable_search",
     "summary": "...", "score_before": 2.8, "score_after": 1.5,
     "accepted": true, "reason": "..."},
    ...
  ]
}
"""
import json
import os
import tempfile

import pytest

from harness.memory import load, add_round, get_rounds_for_scene


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCENE_A = "res://rl/train_map.tscn"
SCENE_B = "res://rl/level2.tscn"

ROUND_1 = {
    "round": 1,
    "target_issue": "difficulty_too_hard",
    "change_type": "tunable_search",
    "summary": "gap_width 120→96",
    "score_before": 2.8,
    "score_after": 1.5,
    "accepted": True,
    "reason": "completion 0.1→0.42",
}

ROUND_2 = {
    "round": 2,
    "target_issue": "death_hotspot",
    "change_type": "logic",
    "summary": "改 player.gd 落地判定",
    "score_before": 1.5,
    "score_after": None,
    "accepted": False,
    "reason": "syntax gate 失败:缩进错误",
}


@pytest.fixture
def tmp_memory(tmp_path):
    """Returns path to a fresh (non-existent) memory file in a temp dir."""
    return str(tmp_path / "memory.json")


@pytest.fixture
def seeded_memory(tmp_path):
    """Returns path to a memory file pre-seeded with ROUND_1 for SCENE_A."""
    path = str(tmp_path / "memory.json")
    data = {"scene": SCENE_A, "rounds": [ROUND_1]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------

class TestLoad:
    def test_load_nonexistent_returns_empty(self, tmp_memory):
        """load() on a missing file returns {"scene": "", "rounds": []}."""
        result = load(tmp_memory)
        assert result == {"scene": "", "rounds": []}

    def test_load_returns_correct_structure(self, seeded_memory):
        result = load(seeded_memory)
        assert result["scene"] == SCENE_A
        assert len(result["rounds"]) == 1
        assert result["rounds"][0]["round"] == 1

    def test_load_is_idempotent(self, seeded_memory):
        r1 = load(seeded_memory)
        r2 = load(seeded_memory)
        assert r1 == r2


# ---------------------------------------------------------------------------
# add_round()
# ---------------------------------------------------------------------------

class TestAddRound:
    def test_add_round_creates_file_if_missing(self, tmp_memory):
        add_round(tmp_memory, SCENE_A, ROUND_1)
        assert os.path.exists(tmp_memory)
        data = load(tmp_memory)
        assert len(data["rounds"]) == 1

    def test_add_round_sets_scene(self, tmp_memory):
        add_round(tmp_memory, SCENE_A, ROUND_1)
        data = load(tmp_memory)
        assert data["scene"] == SCENE_A

    def test_add_round_appends_to_existing(self, seeded_memory):
        """Cross-run accumulation: second round is appended, not overwritten."""
        add_round(seeded_memory, SCENE_A, ROUND_2)
        data = load(seeded_memory)
        assert len(data["rounds"]) == 2
        assert data["rounds"][1]["round"] == 2

    def test_add_round_preserves_all_fields(self, tmp_memory):
        add_round(tmp_memory, SCENE_A, ROUND_1)
        data = load(tmp_memory)
        stored = data["rounds"][0]
        assert stored["target_issue"] == ROUND_1["target_issue"]
        assert stored["change_type"] == ROUND_1["change_type"]
        assert stored["summary"] == ROUND_1["summary"]
        assert stored["score_before"] == ROUND_1["score_before"]
        assert stored["score_after"] == ROUND_1["score_after"]
        assert stored["accepted"] == ROUND_1["accepted"]
        assert stored["reason"] == ROUND_1["reason"]

    def test_add_round_accepted_false(self, tmp_memory):
        add_round(tmp_memory, SCENE_A, ROUND_2)
        data = load(tmp_memory)
        assert data["rounds"][0]["accepted"] is False
        assert data["rounds"][0]["score_after"] is None

    def test_add_multiple_rounds_sequentially(self, tmp_memory):
        for i in range(5):
            record = {**ROUND_1, "round": i, "summary": f"change {i}"}
            add_round(tmp_memory, SCENE_A, record)
        data = load(tmp_memory)
        assert len(data["rounds"]) == 5

    def test_add_round_updates_scene_on_change(self, seeded_memory):
        """If scene changes between runs, the stored scene is updated."""
        add_round(seeded_memory, SCENE_B, {**ROUND_2, "round": 2})
        data = load(seeded_memory)
        # scene should reflect the latest scene used
        assert data["scene"] == SCENE_B

    def test_add_round_file_is_valid_json(self, tmp_memory):
        add_round(tmp_memory, SCENE_A, ROUND_1)
        with open(tmp_memory, encoding="utf-8") as f:
            parsed = json.load(f)
        assert "rounds" in parsed


# ---------------------------------------------------------------------------
# get_rounds_for_scene()
# ---------------------------------------------------------------------------

class TestGetRoundsForScene:
    def test_returns_all_rounds_for_matching_scene(self, seeded_memory):
        rounds = get_rounds_for_scene(seeded_memory, SCENE_A)
        assert len(rounds) == 1
        assert rounds[0]["round"] == 1

    def test_returns_empty_for_nonmatching_scene(self, seeded_memory):
        rounds = get_rounds_for_scene(seeded_memory, SCENE_B)
        assert rounds == []

    def test_returns_empty_for_missing_file(self, tmp_memory):
        rounds = get_rounds_for_scene(tmp_memory, SCENE_A)
        assert rounds == []

    def test_returns_multiple_rounds(self, seeded_memory):
        add_round(seeded_memory, SCENE_A, ROUND_2)
        rounds = get_rounds_for_scene(seeded_memory, SCENE_A)
        assert len(rounds) == 2

    def test_filters_by_scene(self, tmp_memory):
        add_round(tmp_memory, SCENE_A, ROUND_1)
        add_round(tmp_memory, SCENE_B, ROUND_2)
        # memory.json is single-scene; after switching to SCENE_B only SCENE_B remains
        rounds_b = get_rounds_for_scene(tmp_memory, SCENE_B)
        assert len(rounds_b) == 1
        assert rounds_b[0]["target_issue"] == ROUND_2["target_issue"]
