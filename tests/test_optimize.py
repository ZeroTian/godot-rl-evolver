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

import pytest          # noqa: E402

import optimize       # noqa: E402
import mutate         # noqa: E402
import search         # noqa: E402
import evaluation     # noqa: E402


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


# ─────────────────────────────────────────────────────────────────────
# Task 4: 独立 artifact 与配对评估器
#
# run_one_seed(cfg, *, seed, artifact_dir):
#   ① 拒绝已存在 artifact_dir,建空目录 + telemetry/
#   ② 启动前算 model/tunables SHA-256
#   ③ DIAGNOSE=0 + 独立 TELEMETRY_DIR + 指定 seed 调 run_infer.sh,加超时
#   ④ 结束后 telemetry 目录恰好 1 个 run_*.jsonl(0/多个都失败)
#   ⑤ 调 evaluation.validate_telemetry() 诊断那个确切文件
#   ⑥ 返回带启动前 hash 的 RunResult
# evaluate_current(cfg, *, point_id):逐 seed 调用,返回 EvaluationResult。
# ─────────────────────────────────────────────────────────────────────


def _eval_cfg(tmp_path, *, eval_seeds=(1, 2)):
    """构造一个供评估器使用的 Config(隔离的 model/tunables/artifact_root)。"""
    model = tmp_path / "model.zip"
    model.write_bytes(b"FAKE_MODEL_BYTES")
    tun = tmp_path / "tunables.json"
    _write(tun, {"version": 1, "params": {
        "enemy_hp": {"value": 40, "range": [20, 100], "type": "int",
                     "desc": "hp", "files": ["res://x.gd"]},
    }})

    cfg = optimize.Config()
    cfg.proj = str(tmp_path / "proj")
    cfg.scene = "res://rl/train.tscn"
    cfg.model = str(model)
    cfg.speedup = 8
    cfg.tunables_path = str(tun)
    cfg.eval_seeds = tuple(eval_seeds)
    cfg.eval_episodes = 2
    cfg.max_eval_steps = 5000
    cfg.eval_timeout_seconds = 30
    cfg.artifact_root = str(tmp_path / "artifacts")
    cfg.target_completion = 0.65
    cfg.repo_root = str(tmp_path)
    return cfg


def _make_telemetry(path, *, scene, model, speedup, n_episodes,
                    run_id="170000", completion=0.5):
    """在 path 写一份最小合法 telemetry JSONL(契约同 telemetry.gd)。"""
    header = {"type": "run", "run_id": run_id, "scene": scene, "model": model,
              "speedup": speedup, "grid": {"cell": 64}, "max_ep": 1500}
    lines = [json.dumps(header)]
    for ep in range(n_episodes):
        lines.append(json.dumps({
            "type": "episode", "run_id": run_id, "ep": ep, "len": 100,
            "return": 1.0, "term": "goal",
            "actions": {"move": [0.3, 0.3, 0.4]}, "action_entropy": 1.0,
            "coverage": {"cells": 10, "entropy": 2.0},
            "end_pos": [0, 0], "events": [], "metrics": {},
        }))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_run_one_seed_does_not_fall_back_to_old_latest(tmp_path, monkeypatch):
    """共享目录有旧文件,但本次独立目录无输出 → 必须失败,不回退旧 latest。"""
    cfg = _eval_cfg(tmp_path)

    # 共享旧目录预置一个旧 latest(模拟历史 run)
    shared = tmp_path / "shared_telemetry"
    shared.mkdir()
    _make_telemetry(shared / "run_old.jsonl", scene=cfg.scene, model=cfg.model,
                    speedup=cfg.speedup, n_episodes=5, run_id="OLD")

    # run_infer.sh 桩:什么都不产(本次独立 telemetry 目录保持空)
    def _fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(optimize.subprocess, "run", _fake_run)

    art = tmp_path / "artifacts" / "p0" / "s1"
    with pytest.raises((RuntimeError, ValueError)):
        optimize.run_one_seed(cfg, seed=1, artifact_dir=str(art))


def test_run_one_seed_requires_exactly_one_new_jsonl(tmp_path, monkeypatch):
    """telemetry 目录 0 个或 2 个 run_*.jsonl 都失败;恰好 1 个才成功。"""

    # 用一个可配置的桩:依据 produce 数量在本次 TELEMETRY_DIR 写 N 份 JSONL
    def _make_fake_run(produce):
        def _fake_run(cmd, **kwargs):
            env = kwargs.get("env", {})
            tdir = env["TELEMETRY_DIR"]
            for i in range(produce):
                _make_telemetry(
                    os.path.join(tdir, "run_%d.jsonl" % i),
                    scene=cfg.scene, model=cfg.model, speedup=cfg.speedup,
                    n_episodes=3, run_id="RID%d" % i)
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return _fake_run

    # 0 个 → 失败
    cfg = _eval_cfg(tmp_path)
    monkeypatch.setattr(optimize.subprocess, "run", _make_fake_run(0))
    with pytest.raises((RuntimeError, ValueError)):
        optimize.run_one_seed(cfg, seed=1,
                              artifact_dir=str(tmp_path / "art_zero"))

    # 2 个 → 失败
    monkeypatch.setattr(optimize.subprocess, "run", _make_fake_run(2))
    with pytest.raises((RuntimeError, ValueError)):
        optimize.run_one_seed(cfg, seed=1,
                              artifact_dir=str(tmp_path / "art_two"))

    # 恰好 1 个 → 成功,返回 RunResult 且 hash 已填
    monkeypatch.setattr(optimize.subprocess, "run", _make_fake_run(1))
    rr = optimize.run_one_seed(cfg, seed=1,
                               artifact_dir=str(tmp_path / "art_one"))
    assert isinstance(rr, evaluation.RunResult)
    assert rr.seed == 1
    assert rr.provenance.get("model_sha256")
    assert rr.provenance.get("tunables_sha256")
    assert rr.telemetry_path.endswith("run_0.jsonl")


def test_run_one_seed_rejects_existing_artifact_dir(tmp_path, monkeypatch):
    """artifact_dir 已存在 → 拒绝(避免复用旧产物)。"""
    cfg = _eval_cfg(tmp_path)
    existing = tmp_path / "already_here"
    existing.mkdir()
    monkeypatch.setattr(optimize.subprocess, "run",
                        lambda *a, **k: pytest.fail("不应启动 run_infer.sh"))
    with pytest.raises((RuntimeError, ValueError, FileExistsError)):
        optimize.run_one_seed(cfg, seed=1, artifact_dir=str(existing))


def test_evaluate_current_passes_same_seed_order_for_every_point(tmp_path,
                                                                 monkeypatch):
    """对每个 point,evaluate_current 必须按完全相同的 seed 顺序逐个评估。"""
    cfg = _eval_cfg(tmp_path, eval_seeds=(7, 3, 11))

    seen_orders = []

    def _fake_run_one_seed(c, *, seed, artifact_dir):
        # 记录本 point 调用顺序的 seed,顺手返回一个最小 RunResult
        seen_orders[-1].append(seed)
        return evaluation.RunResult(
            seed=seed, telemetry_path="x", run_id="r%d" % seed,
            report={"summary": {"n_episodes": 2}}, score=float(seed),
            provenance={})

    monkeypatch.setattr(optimize, "run_one_seed", _fake_run_one_seed)

    for point_id in ("p0", "p1", "p2"):
        seen_orders.append([])
        optimize.evaluate_current(cfg, point_id=point_id)

    # 三个 point 都应收到 (7, 3, 11) 这个确切顺序
    assert seen_orders == [[7, 3, 11], [7, 3, 11], [7, 3, 11]]


def test_evaluate_current_returns_evaluation_result(tmp_path, monkeypatch):
    cfg = _eval_cfg(tmp_path, eval_seeds=(1, 2))

    def _fake_run_one_seed(c, *, seed, artifact_dir):
        return evaluation.RunResult(
            seed=seed, telemetry_path="x", run_id="r%d" % seed,
            report={}, score=float(seed), provenance={})

    monkeypatch.setattr(optimize, "run_one_seed", _fake_run_one_seed)
    result = optimize.evaluate_current(cfg, point_id="p0")
    assert isinstance(result, evaluation.EvaluationResult)
    assert set(result.by_seed) == {1, 2}
    assert result.mean_score == pytest.approx(1.5)


def _variance(xs):
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def test_paired_delta_variance_is_lower_than_unpaired_delta():
    """确定性伪噪声 score(x,seed)=(x-3)**2+seed*0.1:
    同 seed 配对作差抵消 seed*0.1 → 方差更低;非配对作差残留噪声 → 方差更高。
    禁用真实随机数。
    """
    def score(x, seed):
        return (x - 3.0) ** 2 + seed * 0.1

    seeds = [1, 2, 3, 4, 5]
    base_x, cand_x = 5.0, 4.0   # 两个不同参数点

    # 配对差值:同 seed 的 base - cand,seed*0.1 抵消 → 每个差值都相等
    paired_deltas = [score(base_x, s) - score(cand_x, s) for s in seeds]

    # 非配对差值:base 的 seed 与 cand 的另一 seed 错位作差,噪声不抵消
    shifted = seeds[1:] + seeds[:1]
    unpaired_deltas = [score(base_x, s) - score(cand_x, s2)
                       for s, s2 in zip(seeds, shifted)]

    assert _variance(paired_deltas) < _variance(unpaired_deltas)
    # 配对差值应恒等(方差为 0),证明 seed 配对确实抵消了噪声
    assert _variance(paired_deltas) == pytest.approx(0.0)
