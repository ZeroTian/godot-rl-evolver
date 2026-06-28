"""optimize.py 编排器集成测试(计划 Task 4 + Task 7)。

Task 7 验证主循环 baseline 生命周期与接受门:
- run 开始必跑**新** baseline(EvaluationResult),不读磁盘旧 REPORT_PATH。
- 接受后 candidate 成为下一轮 baseline,不重复跑同一点。
- 拒绝后 tunables **定向回滚**白名单,baseline 仍对应回滚后的 hash。
- seed 集不一致 / 局数不足 / 0或多个 JSONL → 记失败且不计分。
- 改善**等于**阈值不接受,**严格大于** min_improvement 才接受。
- memory reason 用赋值前后真实 mean_score,不得出现 X→X。
- 每轮边界出现白名单外 tracked 改动立即中止。

git(snapshot/rollback/commit)、试玩(evaluator/baseline)均 mock/桩,不真跑 Godot/git。
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


# --------------------------------------------------------------------------- #
# Task 7 主循环测试脚手架                                                       #
# --------------------------------------------------------------------------- #

# 一个有 high issue 的代表性报告(进循环的前提:有 high issue 才提案)
_REP_HIGH = {
    "summary": {"completion_rate": 0.04, "n_episodes": 20},
    "issues": [{"id": "difficulty_too_hard", "severity": "high"}],
}
# 无 issue 的报告(用于停止条件)
_REP_CLEAN = {"summary": {"completion_rate": 0.66, "n_episodes": 20}, "issues": []}

_PLAN = {
    "change_type": "tunable_search",
    "target_issue": "difficulty_too_hard",
    "search_space": [{"key": "enemy_hp", "range": [20, 60]}],
    "files": ["res://rl/game_env.gd"],
    "expected_effect": "降低难度提高通关率",
}


def _eval_result(*, mean, report=_REP_HIGH, seeds=(1, 2, 3)):
    """构造一个 mean_score == `mean` 的 EvaluationResult(各 seed 同分,便于配对推断)。"""
    runs = tuple(
        evaluation.RunResult(
            seed=s, telemetry_path="t%d" % s, run_id="r%d" % s,
            report=report, score=float(mean), provenance={})
        for s in seeds
    )
    return evaluation.EvaluationResult(runs)


def _loop_cfg(tmp_path):
    """主循环用 Config:隔离 tmp 路径、单/少轮、stage 1。"""
    tun = tmp_path / "tunables.json"
    _write(tun, {"version": 1, "params": {
        "enemy_hp": {"value": 40, "range": [20, 100], "type": "int",
                     "desc": "hp", "files": ["res://rl/game_env.gd"]},
    }})
    cfg = optimize.Config()
    cfg.tunables_path = str(tun)
    cfg.report_path = str(tmp_path / "report.json")  # 故意指向(可能存在的)旧报告
    cfg.scene = "res://rl/train.tscn"
    cfg.max_rounds = 1
    cfg.patience = 3
    cfg.stage = 1
    cfg.target_completion = 0.65
    cfg.repo_root = str(tmp_path)
    cfg.eval_seeds = (1, 2, 3)
    cfg.eval_episodes = 20
    cfg.max_eval_steps = 40000
    cfg.eval_timeout_seconds = 30
    cfg.min_improvement = 0.1
    cfg.artifact_root = str(tmp_path / ".artifacts" / "opt")
    return cfg


def _patch_git(monkeypatch):
    """mock 白名单 git 副作用,返回 calls 记录(含传入路径)。"""
    calls = {"snapshot": [], "rollback": 0, "commit": []}

    def _snap(paths, repo_root="."):
        calls["snapshot"].append(list(paths))
        return {"SNAP": b""}

    monkeypatch.setattr(mutate, "snapshot", _snap)
    # allowed 被 optimize_loop 以 proj_rel= kwarg 调用(阶段2),mock 须兼容 kwarg。
    monkeypatch.setattr(mutate, "allowed",
                        lambda plan, protected, *, proj_rel="": True)
    monkeypatch.setattr(mutate, "apply_tunable", lambda path, k, v: None)
    monkeypatch.setattr(mutate, "rollback",
                        lambda snap, repo_root=".": calls.__setitem__(
                            "rollback", calls["rollback"] + 1))
    monkeypatch.setattr(mutate, "commit",
                        lambda msg, paths, repo_root=".": calls["commit"].append(
                            list(paths)))
    return calls


def _read_mem(cfg):
    path = optimize._memory_path_for(cfg)
    return json.loads(open(path, encoding="utf-8").read())


def test_run_start_evaluates_fresh_baseline_not_disk_report(tmp_path, monkeypatch):
    """run 开始必调用一次新 baseline 评估,绝不读取磁盘旧 REPORT_PATH。"""
    cfg = _loop_cfg(tmp_path)
    # 在磁盘预置一份"旧报告" —— 若循环误读它就算回归
    _write(tmp_path / "report.json", {"summary": {"completion_rate": 0.99,
                                                   "n_episodes": 20},
                                      "issues": []})
    _patch_git(monkeypatch)

    baseline_calls = {"n": 0}

    def _baseline(c):
        baseline_calls["n"] += 1
        return _eval_result(mean=5.0)

    # 不进搜索:让 baseline 无 high issue 以外的逻辑 —— 这里 baseline 有 high issue,
    # 但我们让 propose 抛出以确认"先评估 baseline"已发生即可。
    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=5.0)))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=_baseline,
        evaluator_fn=lambda point: _eval_result(mean=5.0),
        tracked_changes_fn=lambda c: [],
    )

    assert baseline_calls["n"] == 1
    # 不依赖磁盘旧 report:base_score 应来自 _baseline(=5.0),不是 0.99 报告算出的分
    assert summary["base_score"] == pytest.approx(5.0)


def test_accepted_candidate_becomes_next_baseline(tmp_path, monkeypatch):
    """接受后 candidate 成为下一轮 baseline,不重复评估同一点。"""
    cfg = _loop_cfg(tmp_path)
    cfg.max_rounds = 2
    calls = _patch_git(monkeypatch)

    # baseline 只在 run 开始评估一次
    baseline_calls = {"n": 0}

    def _baseline(c):
        baseline_calls["n"] += 1
        return _eval_result(mean=5.0, report=_REP_HIGH)

    # 第一轮 candidate 明显更好(mean 1.0,改善 4.0 > 0.1);第二轮 candidate 也更好
    cand_means = iter([1.0, 0.5])
    cand_reports = iter([_REP_HIGH, _REP_HIGH])

    def _search(space, ev, n_calls):
        return ({"enemy_hp": 30},
                _eval_result(mean=next(cand_means), report=next(cand_reports)))

    monkeypatch.setattr(search, "optimize", _search)

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=_baseline,
        evaluator_fn=lambda point: _eval_result(mean=1.0),
        tracked_changes_fn=lambda c: [],
    )

    assert baseline_calls["n"] == 1          # baseline 只 run 开始评估一次
    assert len(calls["commit"]) == 2         # 两轮都接受
    assert len(summary["accepted"]) == 2
    # 第二轮的 score_before 应等于第一轮接受后的 mean(=1.0),证明 candidate 成了 baseline
    assert summary["accepted"][1]["score_before"] == pytest.approx(1.0)
    assert summary["accepted"][1]["score_after"] == pytest.approx(0.5)


def test_rejected_change_is_targeted_rolled_back(tmp_path, monkeypatch):
    """拒绝后定向回滚白名单,baseline 不变(仍对应回滚后的 hash)。"""
    cfg = _loop_cfg(tmp_path)
    calls = _patch_git(monkeypatch)

    def _baseline(c):
        return _eval_result(mean=5.0)

    # candidate 不够好(mean 4.95,改善 0.05 < 0.1)→ 拒绝
    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=4.95)))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=_baseline,
        evaluator_fn=lambda point: _eval_result(mean=4.95),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["rollback"] == 1
    assert calls["commit"] == []
    assert summary["accepted"] == []
    # 回滚后 baseline 仍是原 baseline(mean 5.0)
    assert summary["base_score"] == pytest.approx(5.0)
    # snapshot 应传白名单路径列表(阶段1 = testbed_platformer/rl/tunables.json)
    assert calls["snapshot"], "应对白名单路径快照"
    assert any("tunables.json" in p for plist in calls["snapshot"] for p in plist)


def test_improvement_equal_to_threshold_is_rejected(tmp_path, monkeypatch):
    """改善**等于** min_improvement 不接受;严格大于才接受(严格不等号边界)。"""
    cfg = _loop_cfg(tmp_path)
    cfg.min_improvement = 0.5
    calls = _patch_git(monkeypatch)

    # baseline 5.0,candidate 4.5 → paired_improvement = 0.5 == 阈值 → 不接受
    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=4.5)))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        evaluator_fn=lambda point: _eval_result(mean=4.5),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["commit"] == []
    assert calls["rollback"] == 1
    assert summary["accepted"] == []


def test_improvement_strictly_above_threshold_is_accepted(tmp_path, monkeypatch):
    """改善严格大于阈值才接受。"""
    cfg = _loop_cfg(tmp_path)
    cfg.min_improvement = 0.5
    calls = _patch_git(monkeypatch)

    # baseline 5.0,candidate 4.49 → improvement 0.51 > 0.5 → 接受
    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=4.49)))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        evaluator_fn=lambda point: _eval_result(mean=4.49),
        tracked_changes_fn=lambda c: [],
    )

    assert len(calls["commit"]) == 1
    assert calls["rollback"] == 0
    assert len(summary["accepted"]) == 1


def test_memory_reason_uses_real_scores_no_x_to_x(tmp_path, monkeypatch):
    """memory reason 用赋值前后真实 mean_score,不得出现 X→X。"""
    cfg = _loop_cfg(tmp_path)
    _patch_git(monkeypatch)

    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=1.0)))

    optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        evaluator_fn=lambda point: _eval_result(mean=1.0),
        tracked_changes_fn=lambda c: [],
    )

    mem = _read_mem(cfg)
    reason = mem["rounds"][0]["reason"]
    assert mem["rounds"][0]["accepted"] is True
    # reason 须含真实的 5.x→1.x,且 prev != after(不得 X→X)
    import re
    nums = re.findall(r"\d+\.\d+", reason)
    assert len(nums) >= 2, "reason 应含前后两个分数: %r" % reason
    assert nums[0] != nums[1], "不得出现 X→X: %r" % reason
    assert float(nums[0]) == pytest.approx(5.0)
    assert float(nums[1]) == pytest.approx(1.0)


def test_seed_set_mismatch_records_failure_no_score(tmp_path, monkeypatch):
    """candidate 与 baseline seed 集不一致 → paired_improvement 抛错 → 记失败不计分。"""
    cfg = _loop_cfg(tmp_path)
    calls = _patch_git(monkeypatch)

    # candidate 用不同 seed 集 {1,2,4} vs baseline {1,2,3}
    bad_cand = _eval_result(mean=1.0, seeds=(1, 2, 4))
    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30}, bad_cand))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0, seeds=(1, 2, 3)),
        evaluator_fn=lambda point: bad_cand,
        tracked_changes_fn=lambda c: [],
    )

    assert calls["commit"] == []
    assert calls["rollback"] == 1            # 失败也要回滚白名单
    assert summary["accepted"] == []
    mem = _read_mem(cfg)
    assert mem["rounds"][0]["accepted"] is False


def test_evaluation_error_records_failure_no_score(tmp_path, monkeypatch):
    """评估抛错(0/多个 JSONL、局数不足等) → 记失败且不计分,定向回滚。"""
    cfg = _loop_cfg(tmp_path)
    calls = _patch_git(monkeypatch)

    def _search(space, ev, n_calls):
        raise RuntimeError("seed=1 期望恰好 1 个 run_*.jsonl,实际 0 个")

    monkeypatch.setattr(search, "optimize", _search)

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        evaluator_fn=lambda point: _eval_result(mean=1.0),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["commit"] == []
    assert calls["rollback"] == 1
    assert summary["accepted"] == []
    mem = _read_mem(cfg)
    assert mem["rounds"][0]["accepted"] is False


def test_unexpected_tracked_change_aborts_round(tmp_path, monkeypatch):
    """每轮边界出现白名单外 tracked 改动 → 立即中止(不提交)。"""
    cfg = _loop_cfg(tmp_path)
    calls = _patch_git(monkeypatch)

    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=1.0)))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        evaluator_fn=lambda point: _eval_result(mean=1.0),
        # Gate 0b:发现白名单外 tracked 改动
        tracked_changes_fn=lambda c: ["harness/optimize.py"],
    )

    assert calls["commit"] == []
    assert summary.get("aborted") is True


def test_loop_commits_only_stage1_tunables_whitelist(tmp_path, monkeypatch):
    """阶段1 commit 路径固定为 repo-relative testbed_platformer/rl/tunables.json。"""
    cfg = _loop_cfg(tmp_path)
    calls = _patch_git(monkeypatch)

    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=1.0)))

    optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        evaluator_fn=lambda point: _eval_result(mean=1.0),
        tracked_changes_fn=lambda c: [],
    )

    assert len(calls["commit"]) == 1
    committed = calls["commit"][0]
    assert committed == ["testbed_platformer/rl/tunables.json"]


def test_config_validate_rejects_bad_eval_fields(tmp_path):
    """Config.validate() 严格校验配对评估字段。"""
    cfg = _loop_cfg(tmp_path)

    # 空 eval_seeds
    cfg.eval_seeds = ()
    with pytest.raises(ValueError):
        cfg.validate()

    # 重复 seed
    cfg.eval_seeds = (1, 1, 2)
    with pytest.raises(ValueError):
        cfg.validate()

    # eval_episodes <= 0
    cfg.eval_seeds = (1, 2, 3)
    cfg.eval_episodes = 0
    with pytest.raises(ValueError):
        cfg.validate()

    # max_eval_steps < eval_episodes
    cfg.eval_episodes = 20
    cfg.max_eval_steps = 5
    with pytest.raises(ValueError):
        cfg.validate()

    # eval_timeout_seconds <= 0
    cfg.max_eval_steps = 40000
    cfg.eval_timeout_seconds = 0
    with pytest.raises(ValueError):
        cfg.validate()

    # min_improvement < 0
    cfg.eval_timeout_seconds = 30
    cfg.min_improvement = -0.1
    with pytest.raises(ValueError):
        cfg.validate()

    # 全部合法 → 不抛
    cfg.min_improvement = 0.1
    cfg.validate()


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


def test_run_one_seed_honors_episode_and_step_overrides(tmp_path, monkeypatch):
    """run_one_seed 传 min_episodes/max_eval_steps 覆盖时,透传给子进程的
    EVAL_EPISODES/MAX_EVAL_STEPS 与 validate_telemetry 的 min_episodes 都用覆盖值;
    不传则用 cfg 默认(阶段1 零回归,critic C3)。"""
    cfg = _eval_cfg(tmp_path)         # cfg.eval_episodes=2, cfg.max_eval_steps=5000
    captured = {"env": None, "min_episodes": None}

    def _fake_run(cmd, **kwargs):
        env = kwargs.get("env", {})
        captured["env"] = dict(env)
        tdir = env["TELEMETRY_DIR"]
        _make_telemetry(os.path.join(tdir, "run_0.jsonl"),
                        scene=cfg.scene, model=cfg.model, speedup=cfg.speedup,
                        n_episodes=3, run_id="RID")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    def _fake_validate(path, *, scene, model, speedup, min_episodes, thresholds=None):
        captured["min_episodes"] = min_episodes
        return ({"summary": {"n_episodes": 3, "completion_rate": 0.5},
                 "issues": [], "run_id": "RID"}, "RID")

    monkeypatch.setattr(optimize.subprocess, "run", _fake_run)
    monkeypatch.setattr(optimize.evaluation, "validate_telemetry", _fake_validate)

    # 覆盖:min_episodes=1, max_eval_steps=999
    optimize.run_one_seed(cfg, seed=1, artifact_dir=str(tmp_path / "art_override"),
                          min_episodes=1, max_eval_steps=999)
    assert captured["env"]["EVAL_EPISODES"] == "1"
    assert captured["env"]["MAX_EVAL_STEPS"] == "999"
    assert captured["min_episodes"] == 1

    # 不覆盖:用 cfg 默认值(零回归)
    captured["env"] = None
    captured["min_episodes"] = None
    optimize.run_one_seed(cfg, seed=2, artifact_dir=str(tmp_path / "art_default"))
    assert captured["env"]["EVAL_EPISODES"] == str(cfg.eval_episodes)
    assert captured["env"]["MAX_EVAL_STEPS"] == str(cfg.max_eval_steps)
    assert captured["min_episodes"] == cfg.eval_episodes


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


def test_evaluate_current_isolates_artifact_dir_by_run_id(tmp_path, monkeypatch):
    """artifact_dir 必须含 OPT_RUN_ID,使不同 run 互不冲突。

    回归:固定路径 .artifacts/opt/baseline/seed_1 二次运行撞 run_one_seed 的
    FileExistsError(端到端实跑暴露)。
    """
    cfg = _eval_cfg(tmp_path, eval_seeds=(1,))
    cfg.opt_run_id = "RUN_ABC"
    captured = []

    def _fake(c, *, seed, artifact_dir):
        captured.append(artifact_dir)
        return evaluation.RunResult(seed=seed, telemetry_path="x", run_id="r",
                                    report={}, score=0.0, provenance={})

    monkeypatch.setattr(optimize, "run_one_seed", _fake)
    optimize.evaluate_current(cfg, point_id="baseline")
    assert "RUN_ABC" in captured[0]
    assert captured[0].endswith(os.path.join("RUN_ABC", "baseline", "seed_1"))


# ─────────────────────────────────────────────────────────────────────
# Task 5: structural 分支 + 白名单泛化 + Gate0b 防回归 + PROTECTED 默认
# 全用注入的假 propose/evaluator/gate 钩子,不起 Godot。
# ─────────────────────────────────────────────────────────────────────

_TRAIN_MAP_REL = "testbed_platformer/rl/train_map.tscn"

_STRUCT_PLAN = {
    "change_type": "structural",
    "target_issue": "difficulty_too_hard",
    "patches": [{
        "file": "res://rl/train_map.tscn",
        "anchor": '[node name="MidPlatform" type="StaticBody2D" parent="."]\n'
                  "position = Vector2(600, 40)",
        "new": '[node name="MidPlatform" type="StaticBody2D" parent="."]\n'
               "position = Vector2(700, 40)",
    }],
    "expected_effect": "挪踏脚石平台降低难度",
}


def _struct_cfg(tmp_path):
    """stage=2 structural 用 Config:proj 是 repo 子目录(proj_rel=testbed_platformer)。"""
    cfg = _loop_cfg(tmp_path)
    cfg.stage = 2
    cfg.proj = os.path.join(str(tmp_path), "testbed_platformer")
    return cfg


def _patch_apply(monkeypatch):
    """mock mutate.apply_patch(structural 不真改文件),记调用。"""
    calls = []

    def _ap(path, anchor, new, repo_root=".", protected_globs=None):
        calls.append({"path": path, "anchor": anchor, "new": new,
                      "protected_globs": protected_globs})

    monkeypatch.setattr(mutate, "apply_patch", _ap)
    return calls


def test_structural_accept_commits_patched_tscn(tmp_path, monkeypatch):
    """structural 接受 → commit 收到的 paths 恰为被 patch 的 train_map.tscn。"""
    cfg = _struct_cfg(tmp_path)
    calls = _patch_git(monkeypatch)
    _patch_apply(monkeypatch)
    monkeypatch.setattr(optimize, "evaluate_current",
                        lambda c, *, point_id: _eval_result(mean=1.0))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _STRUCT_PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        syntax_gate_fn=lambda c: (True, "ok"),
        smoke_gate_fn=lambda c: (True, "ok"),
        tracked_changes_fn=lambda c: [],
    )

    assert len(calls["commit"]) == 1
    assert calls["commit"][0] == [_TRAIN_MAP_REL]
    assert len(summary["accepted"]) == 1


def test_structural_rejected_when_patch_touches_protected(tmp_path, monkeypatch):
    """注入触碰 game_agent.gd 的 patch(绕过 parse_plan)→ mutate.allowed 在 snapshot
    前拒绝,记 "protected path",不 apply/不 commit(critic C1/M4 第②层)。

    此用例**不** mock mutate.allowed,用真实实现验证第②层确实拦下;仅 mock 不应被
    触达的 snapshot/rollback/commit/apply_patch 以捕获是否被错误调用。
    """
    cfg = _struct_cfg(tmp_path)
    # 默认 protected_paths 含 */rl/game_agent.gd,真实 allowed 应拦下
    cfg.protected_paths = list(optimize.DEFAULT_PROTECTED.split(","))

    calls = {"snapshot": [], "rollback": 0, "commit": []}
    monkeypatch.setattr(mutate, "snapshot",
                        lambda paths, repo_root=".": calls["snapshot"].append(
                            list(paths)) or {"SNAP": b""})
    monkeypatch.setattr(mutate, "rollback",
                        lambda snap, repo_root=".": calls.__setitem__(
                            "rollback", calls["rollback"] + 1))
    monkeypatch.setattr(mutate, "commit",
                        lambda msg, paths, repo_root=".": calls["commit"].append(
                            list(paths)))
    apply_calls = _patch_apply(monkeypatch)
    monkeypatch.setattr(optimize, "evaluate_current",
                        lambda c, *, point_id: pytest.fail("不应评估"))

    bad_plan = {
        "change_type": "structural",
        "target_issue": "x",
        "patches": [{"file": "res://rl/game_agent.gd",
                     "anchor": "a", "new": "b"}],
        "expected_effect": "y",
    }

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: bad_plan,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        syntax_gate_fn=lambda c: pytest.fail("不应过 gate"),
        smoke_gate_fn=lambda c: pytest.fail("不应过 gate"),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["commit"] == []
    assert calls["snapshot"] == []           # 命中 protected → 不快照
    assert apply_calls == []                  # 不 apply
    mem = _read_mem(cfg)
    assert "protected" in mem["rounds"][0]["reason"]


def test_structural_syntax_gate_failure_rolls_back(tmp_path, monkeypatch):
    cfg = _struct_cfg(tmp_path)
    calls = _patch_git(monkeypatch)
    _patch_apply(monkeypatch)
    monkeypatch.setattr(optimize, "evaluate_current",
                        lambda c, *, point_id: pytest.fail("syntax 不过不应评估"))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _STRUCT_PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        syntax_gate_fn=lambda c: (False, "SCRIPT ERROR: boom"),
        smoke_gate_fn=lambda c: (True, "ok"),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["rollback"] == 1
    assert calls["commit"] == []
    mem = _read_mem(cfg)
    assert "syntax" in mem["rounds"][0]["reason"]


def test_structural_smoke_gate_failure_rolls_back(tmp_path, monkeypatch):
    cfg = _struct_cfg(tmp_path)
    calls = _patch_git(monkeypatch)
    _patch_apply(monkeypatch)
    monkeypatch.setattr(optimize, "evaluate_current",
                        lambda c, *, point_id: pytest.fail("smoke 不过不应评估"))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _STRUCT_PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        syntax_gate_fn=lambda c: (True, "ok"),
        smoke_gate_fn=lambda c: (False, "no episode"),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["rollback"] == 1
    assert calls["commit"] == []
    mem = _read_mem(cfg)
    assert "smoke" in mem["rounds"][0]["reason"]


def test_structural_no_improvement_rolls_back(tmp_path, monkeypatch):
    cfg = _struct_cfg(tmp_path)
    calls = _patch_git(monkeypatch)
    _patch_apply(monkeypatch)
    # 两 gate 过,但 candidate 不够好(改善 0.05 < 0.1)
    monkeypatch.setattr(optimize, "evaluate_current",
                        lambda c, *, point_id: _eval_result(mean=4.95))

    summary = optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _STRUCT_PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        syntax_gate_fn=lambda c: (True, "ok"),
        smoke_gate_fn=lambda c: (True, "ok"),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["rollback"] == 1
    assert calls["commit"] == []
    assert summary["accepted"] == []
    mem = _read_mem(cfg)
    assert "no score improvement" in mem["rounds"][0]["reason"]


def test_tunable_whitelist_unchanged(tmp_path, monkeypatch):
    """tunable_search 下 paths 恰为 [STAGE1_TUNABLES_REL](白名单泛化不改阶段1 粒度)。"""
    cfg = _loop_cfg(tmp_path)
    calls = _patch_git(monkeypatch)

    monkeypatch.setattr(search, "optimize",
                        lambda space, ev, n_calls: ({"enemy_hp": 30},
                                                    _eval_result(mean=1.0)))

    optimize.optimize_loop(
        cfg,
        propose_fn=lambda rep, tun, mem, stage: _PLAN,
        baseline_fn=lambda c: _eval_result(mean=5.0),
        evaluator_fn=lambda point: _eval_result(mean=1.0),
        tracked_changes_fn=lambda c: [],
    )

    assert calls["snapshot"] == [[optimize.STAGE1_TUNABLES_REL]]
    assert calls["commit"] == [[optimize.STAGE1_TUNABLES_REL]]


def test_default_tracked_changes_real_impl(tmp_path):
    """不注入 tracked_changes_fn:临时 git 仓,仅 tunables 脏→空;别的文件脏→非空。"""
    import subprocess as sp
    repo = tmp_path / "repo"
    (repo / "testbed_platformer" / "rl").mkdir(parents=True)
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    tun = repo / optimize.STAGE1_TUNABLES_REL
    tun.write_text('{"v":1}', encoding="utf-8")
    other = repo / "testbed_platformer" / "rl" / "train_map.tscn"
    other.write_text("x", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    cfg = optimize.Config()
    cfg.repo_root = str(repo)

    # 仅 tunables 脏 → 当轮白名单放行 → 空
    tun.write_text('{"v":2}', encoding="utf-8")
    assert optimize._default_tracked_changes(cfg, [optimize.STAGE1_TUNABLES_REL]) == []

    # 另一文件也脏,但不在当轮白名单 → 非空
    other.write_text("y", encoding="utf-8")
    outside = optimize._default_tracked_changes(cfg, [optimize.STAGE1_TUNABLES_REL])
    assert outside  # 非空,含 train_map.tscn
    assert any("train_map.tscn" in p for p in outside)


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


def test_has_high_issue_ignores_soft_issues():
    """Goodhart 防火墙(critic M4):主观软问题(type='soft')即便误带 severity=high
    也不参与早停/不被消费;真正的 high 客观 issue 才触发。"""
    soft_only = {"issues": [
        {"id": "difficulty_varies_by_persona", "severity": "high", "type": "soft"}]}
    assert optimize.has_high_issue(soft_only) is False
    mixed = {"issues": [
        {"id": "difficulty_varies_by_persona", "severity": "high", "type": "soft"},
        {"id": "difficulty_too_hard", "severity": "high"}]}
    assert optimize.has_high_issue(mixed) is True
