"""evaluation.py 单元测试(TDD)。

覆盖 telemetry 真实性校验(run 头/局数/run_id 绑定)与配对改善。
契约见 docs/plans/2026-06-28-llm-optimization-loop-stage1-plan.md §1 与
docs/specs/2026-06-28-llm-optimization-loop-design.md §5.5。
"""
import json

import pytest

import evaluation


# ── 构造一份合法 telemetry JSONL(run 头 + n 条 episode) ──────────────
def _write_jsonl(path, *, scene, model, speedup, n_episodes,
                 run_id=None, max_ep=1500, cell=64, term="goal"):
    """写一份最小合法 telemetry;返回 run_id。

    与 telemetry.gd 落盘契约一致:首行 type:run(含 scene/model/speedup/
    run_id/grid/max_ep),其后每 episode 一行。
    """
    rid = run_id if run_id is not None else "1700000000"
    header = {
        "type": "run", "run_id": rid, "scene": scene, "model": model,
        "speedup": speedup, "grid": {"cell": cell}, "max_ep": max_ep,
    }
    lines = [json.dumps(header)]
    for ep in range(n_episodes):
        lines.append(json.dumps({
            "type": "episode", "run_id": rid, "ep": ep, "len": 100,
            "return": 1.0, "term": term,
            "actions": {"move": [0.3, 0.3, 0.4]},
            "action_entropy": 1.0,
            "coverage": {"cells": 10, "entropy": 2.0},
            "end_pos": [0, 0], "events": [], "metrics": {},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rid


# ── validate_telemetry:run 头不匹配 ──────────────────────────────────
def test_validate_rejects_wrong_run_header(tmp_path):
    """首行 scene 与期望不符 → ValueError。"""
    p = tmp_path / "run_1.jsonl"
    _write_jsonl(p, scene="res://other.tscn", model="m.zip", speedup=8,
                 n_episodes=3)
    with pytest.raises(ValueError):
        evaluation.validate_telemetry(
            str(p), scene="res://train.tscn", model="m.zip", speedup=8,
            min_episodes=1)


# ── validate_telemetry:局数不足 ──────────────────────────────────────
def test_validate_rejects_summary_n_episodes_below_target(tmp_path):
    """只有 1 局,min_episodes=2 → ValueError(用精确键 summary.n_episodes)。"""
    p = tmp_path / "run_1.jsonl"
    _write_jsonl(p, scene="res://train.tscn", model="m.zip", speedup=8,
                 n_episodes=1)
    with pytest.raises(ValueError):
        evaluation.validate_telemetry(
            str(p), scene="res://train.tscn", model="m.zip", speedup=8,
            min_episodes=2)


# ── validate_telemetry:report run_id 必须与首行一致 ──────────────────
def test_validate_requires_report_run_id_to_match_header(tmp_path):
    """episode 行 run_id 与首行不一致会让 build_report 的 run_id(来自首行)
    与 episode 内嵌 run_id 错位;此处篡改首行 run_id 以制造 report run_id
    与逐行声明不符,断言校验拒绝。"""
    p = tmp_path / "run_1.jsonl"
    # 写一份正常文件,再手工把首行 run_id 改掉,使其与 episode 行不符
    rid = _write_jsonl(p, scene="res://train.tscn", model="m.zip", speedup=8,
                       n_episodes=3, run_id="AAA")
    lines = p.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["run_id"] = "TAMPERED"
    lines[0] = json.dumps(header)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        evaluation.validate_telemetry(
            str(p), scene="res://train.tscn", model="m.zip", speedup=8,
            min_episodes=1)


# ── validate_telemetry:合法路径(冒烟,确认返回契约) ────────────────
def test_validate_accepts_consistent_run(tmp_path):
    p = tmp_path / "run_1.jsonl"
    rid = _write_jsonl(p, scene="res://train.tscn", model="m.zip", speedup=8,
                       n_episodes=3)
    report, run_id = evaluation.validate_telemetry(
        str(p), scene="res://train.tscn", model="m.zip", speedup=8,
        min_episodes=2)
    assert run_id == rid
    assert report["run_id"] == rid
    assert report["summary"]["n_episodes"] == 3


def test_validate_thresholds_override_changes_issues(tmp_path):
    """thresholds 覆盖能改变诊断结果(让闭环可调诊断灵敏度)。"""
    p = tmp_path / "run_fall.jsonl"
    _write_jsonl(p, scene="s", model="m", speedup=8, n_episodes=4, term="fall")
    # 默认 hard_completion=0.10:全 fall → completion=0 < 0.10 → difficulty_too_hard 触发
    rep_def, _ = evaluation.validate_telemetry(
        str(p), scene="s", model="m", speedup=8, min_episodes=1)
    assert any(i["id"] == "difficulty_too_hard" for i in rep_def["issues"])
    # 覆盖 hard_completion=0.0:0 < 0 为假 → 不再触发
    rep_thr, _ = evaluation.validate_telemetry(
        str(p), scene="s", model="m", speedup=8, min_episodes=1,
        thresholds={"hard_completion": 0.0})
    assert not any(i["id"] == "difficulty_too_hard" for i in rep_thr["issues"])


# ── paired_improvement:seed 集合必须完全相同 ─────────────────────────
def _eval(scores_by_seed):
    """构造 EvaluationResult,scores_by_seed = {seed: score}。"""
    runs = tuple(
        evaluation.RunResult(
            seed=s, telemetry_path="x", run_id="r%d" % s, report={},
            score=sc, provenance={})
        for s, sc in scores_by_seed.items())
    return evaluation.EvaluationResult(runs)


def test_paired_improvement_requires_same_seed_set():
    base = _eval({1: 1.0, 2: 2.0})
    cand = _eval({1: 1.0, 3: 2.0})  # {1,2} != {1,3}
    with pytest.raises(ValueError):
        evaluation.paired_improvement(base, cand)


# ── paired_improvement:用同 seed 配对差值的精确均值 ──────────────────
def test_paired_improvement_uses_seed_matched_differences():
    # base.score - cand.score 按 seed 配对:
    #   seed1: 10 - 6 = 4 ; seed2: 20 - 12 = 8 ; 均值 = 6.0
    base = _eval({1: 10.0, 2: 20.0})
    cand = _eval({1: 6.0, 2: 12.0})
    assert evaluation.paired_improvement(base, cand) == pytest.approx(6.0)
