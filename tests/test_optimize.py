"""optimize.py 编排器集成测试(计划 AC5)。

验证三道 gate 的「指标回归」+ 回滚 + 记忆:
- 劣化改动(score 变差)→ git 回滚 + memory 记 accepted:false,不接受。
- 改善改动(score 变好)→ commit 接受 + memory 记 accepted:true。

git(snapshot/rollback/commit)与试玩(playtest_fn)均 mock/桩,不真跑 Godot/git。
"""
import json
import os
import sys

HARNESS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "harness")
if HARNESS not in sys.path:
    sys.path.insert(0, HARNESS)

import optimize       # noqa: E402
import mutate         # noqa: E402
import search         # noqa: E402


def _write(p, obj):
    p.write_text(json.dumps(obj), encoding="utf-8")


def _make_cfg(tmp_path, base_report):
    """构造一个隔离的 Config:tmp 路径 + 单轮。"""
    tun = tmp_path / "tunables.json"
    _write(tun, {"version": 1, "params": {
        "gap_width": {"value": 120, "range": [80, 160], "type": "float", "desc": "缺口"},
    }})
    rep = tmp_path / "report.json"
    _write(rep, base_report)

    cfg = optimize.Config()
    cfg.tunables_path = str(tun)
    cfg.report_path = str(rep)
    cfg.memory_path = str(tmp_path / "memory.json")
    cfg.scene = "res://test.tscn"
    cfg.max_rounds = 1
    cfg.patience = 3
    cfg.stage = 1
    cfg.target_completion = 0.65
    cfg.repo_root = str(tmp_path)
    return cfg


def _patch_git_and_search(monkeypatch, best_point):
    """mock git 副作用 + search.optimize;返回 calls 计数器。"""
    calls = {"rollback": 0, "commit": 0, "snapshot": 0}
    monkeypatch.setattr(mutate, "snapshot",
                        lambda root=".": calls.__setitem__("snapshot", calls["snapshot"] + 1) or "SNAP")
    monkeypatch.setattr(mutate, "allowed", lambda plan, protected: True)
    monkeypatch.setattr(mutate, "apply_tunable", lambda path, k, v: None)
    monkeypatch.setattr(mutate, "rollback",
                        lambda snap, root=".": calls.__setitem__("rollback", calls["rollback"] + 1))
    monkeypatch.setattr(mutate, "commit",
                        lambda msg, root=".": calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: (best_point, 0.0))
    return calls


# 有 high issue 的 baseline(进循环)+ 低通关率 → score 较高
_BASE = {
    "summary": {"completion_rate": 0.04},
    "issues": [{"id": "difficulty_too_hard", "severity": "high"}],
}

_PLAN = {
    "change_type": "tunable_search",
    "target_issue": "difficulty_too_hard",
    "search_space": [{"key": "gap_width", "range": [100, 160]}],
    "expected_effect": "提高通关率",
}


def test_degrading_change_is_rolled_back(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, _BASE)
    calls = _patch_git_and_search(monkeypatch, {"gap_width": 150.0})

    # 劣化:通关率更低 + 多一个 high issue → score 更大(更差)
    worse = {"summary": {"completion_rate": 0.02},
             "issues": [{"id": "difficulty_too_hard", "severity": "high"},
                        {"id": "death_hotspot", "severity": "high"}]}

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda report, tunables, mem, stage: _PLAN,
        playtest_fn=lambda c: worse,
    )

    assert calls["rollback"] == 1
    assert calls["commit"] == 0
    assert summary["accepted"] == []

    mem = json.loads((tmp_path / "memory.json").read_text())
    rounds = mem["rounds"]
    assert len(rounds) == 1
    assert rounds[0]["accepted"] is False
    assert "no score improvement" in rounds[0]["reason"]


def test_improving_change_is_committed(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, _BASE)
    calls = _patch_git_and_search(monkeypatch, {"gap_width": 110.0})

    # 改善:通关率接近 target + 无 issue → score 更小(更好)
    better = {"summary": {"completion_rate": 0.66}, "issues": []}

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda report, tunables, mem, stage: _PLAN,
        playtest_fn=lambda c: better,
    )

    assert calls["commit"] == 1
    assert calls["rollback"] == 0
    assert len(summary["accepted"]) == 1
    assert summary["accepted"][0]["score_after"] < summary["accepted"][0]["score_before"]

    mem = json.loads((tmp_path / "memory.json").read_text())
    assert mem["rounds"][0]["accepted"] is True


def test_protected_path_change_rejected(tmp_path, monkeypatch):
    """命中 protected 的改动被拒绝并记 memory(不进搜索/试玩)。"""
    cfg = _make_cfg(tmp_path, _BASE)
    calls = _patch_git_and_search(monkeypatch, {"gap_width": 110.0})
    # 覆盖 allowed → False(模拟命中 protected)
    monkeypatch.setattr(mutate, "allowed", lambda plan, protected: False)

    played = {"n": 0}

    def _playtest(c):
        played["n"] += 1
        return _BASE

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda report, tunables, mem, stage: _PLAN,
        playtest_fn=_playtest,
    )

    assert played["n"] == 0          # protected 拒绝,不应试玩
    assert calls["commit"] == 0
    assert summary["accepted"] == []
    mem = json.loads((tmp_path / "memory.json").read_text())
    assert mem["rounds"][0]["accepted"] is False
    assert "protected" in mem["rounds"][0]["reason"]
