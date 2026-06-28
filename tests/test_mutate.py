"""tests/test_mutate.py — TDD for harness/mutate.py 纯函数部分。

覆盖:
  allowed(plan, protected_globs) -> bool
  apply_tunable(path, key, value) 写回 + clamp
"""
import json
import tempfile
import os
import sys

# 确保能 import harness/mutate.py（conftest.py 已加 harness 到 sys.path）
import pytest

# --------------------------------------------------------------------------- #
# 辅助工具                                                                     #
# --------------------------------------------------------------------------- #

def _make_tunables(tmp_path, params: dict) -> str:
    """在 tmp_path 下写一个临时 tunables.json，返回路径。"""
    data = {"version": 1, "params": params}
    p = os.path.join(tmp_path, "tunables.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return p


# --------------------------------------------------------------------------- #
# allowed() 测试                                                                #
# --------------------------------------------------------------------------- #

class TestAllowed:
    """allowed(plan, protected_globs) -> bool"""

    def _plan(self, files=None, change_type="tunable_search", field=None):
        """构造最小 plan dict。"""
        p = {"change_type": change_type, "files": files or []}
        if field:
            p["field"] = field
        return p

    def test_default_protected_harness(self):
        """目标文件命中 harness/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["harness/mutate.py"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_default_protected_git(self):
        """目标文件命中 .git/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=[".git/HEAD"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_default_protected_tests(self):
        """目标文件命中 tests/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["tests/test_mutate.py"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_default_protected_docs(self):
        """目标文件命中 docs/** 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["docs/README.md"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_allowed_game_file(self):
        """目标文件不在 protected 范围内,返回 True。"""
        from mutate import allowed
        plan = self._plan(files=["example_platformer/level.gd"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is True

    def test_allowed_tunable_value_field(self):
        """plan 改的是 tunables.json 的 value 字段 → 允许。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="value")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is True

    def test_blocked_tunable_range_field(self):
        """plan 改 tunables.json 的 range 字段 → 拒绝。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="range")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_blocked_tunable_type_field(self):
        """plan 改 tunables.json 的 type 字段 → 拒绝。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="type")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_blocked_tunable_desc_field(self):
        """plan 改 tunables.json 的 desc 字段 → 拒绝。"""
        from mutate import allowed
        plan = self._plan(files=["rl/tunables.json"], field="desc")
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_multiple_files_one_protected(self):
        """多个目标文件，任意一个命中 protected → 返回 False。"""
        from mutate import allowed
        plan = self._plan(files=["example_platformer/level.gd", "harness/mutate.py"])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is False

    def test_empty_files_allowed(self):
        """无目标文件（纯 tunables search）且无 field 限制 → 允许。"""
        from mutate import allowed
        plan = self._plan(files=[])
        assert allowed(plan, ["harness/**", ".git/**", "tests/**", "docs/**"]) is True


# --------------------------------------------------------------------------- #
# apply_tunable() 测试                                                         #
# --------------------------------------------------------------------------- #

class TestApplyTunable:
    """apply_tunable(path, key, value) 写回 + clamp 到 range。"""

    def test_write_value_in_range(self, tmp_path):
        """在 range 内的值直接写入。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        apply_tunable(path, "gap_width", 150.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["gap_width"]["value"] == 150.0

    def test_clamp_above_max(self, tmp_path):
        """超过 range 上限时 clamp 到上限。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        apply_tunable(path, "gap_width", 999.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["gap_width"]["value"] == 200

    def test_clamp_below_min(self, tmp_path):
        """低于 range 下限时 clamp 到下限。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        apply_tunable(path, "gap_width", 10.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["gap_width"]["value"] == 60

    def test_int_param(self, tmp_path):
        """整数参数写回。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "enemy_hp": {"value": 3, "range": [1, 8], "type": "int", "desc": "敌人血量"}
        })
        apply_tunable(path, "enemy_hp", 5)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["params"]["enemy_hp"]["value"] == 5

    def test_other_params_untouched(self, tmp_path):
        """只改目标 key，其他参数原样保留。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"},
            "enemy_hp":  {"value": 3,   "range": [1, 8],    "type": "int",   "desc": "敌人血量"}
        })
        apply_tunable(path, "gap_width", 100.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # enemy_hp 原样
        assert data["params"]["enemy_hp"]["value"] == 3

    def test_range_fields_preserved(self, tmp_path):
        """apply_tunable 不得修改 range/type/desc 字段。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "jump_force": {"value": 400, "range": [300, 600], "type": "float", "desc": "跳跃力"}
        })
        apply_tunable(path, "jump_force", 500.0)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        p = data["params"]["jump_force"]
        assert p["range"] == [300, 600]
        assert p["type"] == "float"
        assert p["desc"] == "跳跃力"

    def test_key_not_found_raises(self, tmp_path):
        """key 不存在时应抛 KeyError。"""
        from mutate import apply_tunable
        path = _make_tunables(tmp_path, {
            "gap_width": {"value": 120, "range": [60, 200], "type": "float", "desc": "缺口宽度"}
        })
        with pytest.raises(KeyError):
            apply_tunable(path, "nonexistent_key", 100.0)
