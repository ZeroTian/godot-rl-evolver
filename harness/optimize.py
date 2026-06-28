"""harness/optimize.py — LLM 优化闭环编排器(spec §7,阶段1)。

主循环组合同目录子模块:
  objective.score  — report → 标量(越小越好)
  llm_propose      — LLM 提案(report+tunables+memory → 改动计划)
  search.optimize  — 贝叶斯内循环(仅 tunable_search)
  mutate           — 改动应用 / protected 检查 / git 快照·回滚·提交
  memory           — 跨轮记忆读写

闭环(spec §7 伪码):
  baseline → LLM 提案 → git 快照 → protected 检查
    → (tunable_search) 贝叶斯内循环搜最优数值
    → 三道 gate(① 语法 ② smoke ③ 指标回归)
    → objective 比分:真变好才 commit 接受,否则 git 回滚
    → 写 memory → 下一轮(早停:MAX_ROUNDS / PATIENCE / 无 high issue / 预算耗尽)

阶段1 只处理 change_type == "tunable_search"(数值改动,天然过语法 gate;
贝叶斯每点评估隐含过 smoke+指标)。结构/逻辑改动属阶段 2/3,本文件预留接口但不实现。

配置经环境变量(spec §9):
  STAGE / TARGET_COMPLETION / MAX_ROUNDS / PATIENCE / SEARCH_CALLS
  / RETRAIN_EACH / PROTECTED_PATHS / PROJ / SCENE / MODEL / SPEEDUP
  / TUNABLES_PATH / MEMORY_PATH / REPORT_PATH
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Optional

# 同目录子模块(harness 非包,靠 sys.path 注入;CLI 入口下方兜底)
import objective
import memory as memory_mod
import mutate
import llm_propose
import search


# --------------------------------------------------------------------------- #
# 配置                                                                          #
# --------------------------------------------------------------------------- #

DEFAULT_PROTECTED = "harness/**,.git/**,tests/**,docs/**"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    """从环境变量读取闭环配置(spec §9)。"""

    def __init__(self) -> None:
        self.stage = _env_int("STAGE", 1)
        self.target_completion = _env_float("TARGET_COMPLETION", 0.65)
        self.max_rounds = _env_int("MAX_ROUNDS", 8)
        self.patience = _env_int("PATIENCE", 3)
        self.search_calls = _env_int("SEARCH_CALLS", 12)
        self.retrain_each = _env_int("RETRAIN_EACH", 0)
        self.protected_paths = [
            p.strip()
            for p in os.environ.get("PROTECTED_PATHS", DEFAULT_PROTECTED).split(",")
            if p.strip()
        ]
        self.proj = os.environ.get("PROJ", "")
        self.scene = os.environ.get("SCENE", "")
        # 游戏侧 tunables.json(被优化对象);默认在 PROJ/rl/tunables.json
        self.tunables_path = os.environ.get(
            "TUNABLES_PATH",
            os.path.join(self.proj, "rl", "tunables.json") if self.proj else "",
        )
        self.memory_path = os.environ.get("MEMORY_PATH", "memory.json")
        self.report_path = os.environ.get("REPORT_PATH", "report.json")
        self.repo_root = os.environ.get("REPO_ROOT", ".")


# --------------------------------------------------------------------------- #
# JSON 小工具                                                                   #
# --------------------------------------------------------------------------- #

def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 试玩 + 诊断(默认实现,可被测试注入替换)                                       #
# --------------------------------------------------------------------------- #

def run_playtest_and_diagnose(cfg: Config) -> dict:
    """跑一次 run_infer.sh(试玩 + 诊断),读回 report.json。

    复用第一/二环工具:run_infer.sh 内部已 run_infer + diagnose。
    返回解析后的 report dict。失败抛 RuntimeError。
    """
    harness_dir = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(harness_dir, "run_infer.sh")
    env = dict(os.environ)
    env["DIAGNOSE"] = "1"
    proc = subprocess.run(
        ["bash", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"run_infer.sh 失败 (rc={proc.returncode}):\n{proc.stderr[-2000:]}"
        )
    # diagnose.py 默认把 report.json 写到 telemetry 目录;约定 REPORT_PATH 指向它。
    telemetry_dir = env.get(
        "TELEMETRY_DIR", os.path.join(cfg.proj, "rl", "telemetry")
    )
    report_path = cfg.report_path
    if not os.path.exists(report_path):
        candidate = os.path.join(telemetry_dir, "report.json")
        if os.path.exists(candidate):
            report_path = candidate
    if not os.path.exists(report_path):
        raise RuntimeError(f"未找到 report.json(查找 {cfg.report_path} / {telemetry_dir})")
    return _load_json(report_path)


# --------------------------------------------------------------------------- #
# 早停判定                                                                      #
# --------------------------------------------------------------------------- #

def has_high_issue(report: dict) -> bool:
    """report 是否仍有 high severity 的 issue。"""
    return any(i.get("severity") == "high" for i in report.get("issues", []))


# --------------------------------------------------------------------------- #
# 贝叶斯内循环的评估器工厂                                                       #
# --------------------------------------------------------------------------- #

def make_evaluator(
    cfg: Config,
    playtest_fn: Callable[[Config], dict],
) -> Callable[[dict], float]:
    """构造贝叶斯 evaluate(point):写 tunables → 试玩诊断 → objective 算分。

    point 是 {key: value} dict(由 search.optimize 映射好类型)。
    每点评估返回标量分数(越小越好)。
    """

    def evaluate(point: dict) -> float:
        for key, value in point.items():
            mutate.apply_tunable(cfg.tunables_path, key, value)
        report = playtest_fn(cfg)
        return objective.score(
            report, target=cfg.target_completion
        )

    return evaluate


# --------------------------------------------------------------------------- #
# 主循环                                                                        #
# --------------------------------------------------------------------------- #

def optimize_loop(
    cfg: Config,
    propose_fn: Optional[Callable] = None,
    playtest_fn: Optional[Callable] = None,
) -> dict:
    """运行优化闭环,返回总结 dict。

    Args:
        cfg:         配置。
        propose_fn:  LLM 提案函数,签名 (report, tunables, memory, stage)->plan。
                     默认 llm_propose.propose;测试可注入桩。
        playtest_fn: 试玩+诊断函数,签名 (cfg)->report。默认 run_playtest_and_diagnose;
                     测试可注入桩报告。

    返回总结:{"accepted": [...], "base_score": float, "rounds": int,
              "final_report": dict}
    """
    propose_fn = propose_fn or (
        lambda report, tunables, mem, stage: llm_propose.propose(
            report, tunables, mem, stage
        )
    )
    playtest_fn = playtest_fn or run_playtest_and_diagnose

    # baseline:已有 report 就用,否则跑一次试玩诊断
    if os.path.exists(cfg.report_path):
        report = _load_json(cfg.report_path)
    else:
        report = playtest_fn(cfg)

    base_score = objective.score(report, target=cfg.target_completion)
    no_improve = 0
    accepted: list[dict] = []

    for r in range(cfg.max_rounds):
        # 早停:无 high issue / 连续无改善达 PATIENCE
        if not has_high_issue(report) or no_improve >= cfg.patience:
            break

        tunables = _load_json(cfg.tunables_path)
        mem = memory_mod.load(cfg.memory_path)
        plan = propose_fn(report, tunables, mem, cfg.stage)

        snap = mutate.snapshot(cfg.repo_root)

        # protected 入口:命中则拒绝并记 memory
        if not mutate.allowed(plan, cfg.protected_paths):
            memory_mod.add_round(
                cfg.memory_path, cfg.scene,
                _record(r, plan, base_score, base_score, False, "protected path"),
            )
            no_improve += 1
            continue

        change_type = plan.get("change_type")

        if change_type == "tunable_search":
            # 贝叶斯内循环:每点评估隐含过 smoke + 指标(评估即试玩算分)。
            # 数值改动天然过语法 gate(不碰代码)。
            evaluate = make_evaluator(cfg, playtest_fn)
            search_space = _normalize_search_space(plan["search_space"], tunables)
            best_point, best_score = search.optimize(
                search_space, evaluate, n_calls=cfg.search_calls
            )
            # 把最优点写回 tunables(贝叶斯最后一次评估未必是最优点)
            for key, value in best_point.items():
                mutate.apply_tunable(cfg.tunables_path, key, value)
            new_report = playtest_fn(cfg)
            new_score = objective.score(new_report, target=cfg.target_completion)
            summary = _summarize_point(best_point)
        else:
            # 阶段 2/3:structural / logic。阶段1不处理 → 回滚跳过。
            mutate.rollback(snap, cfg.repo_root)
            memory_mod.add_round(
                cfg.memory_path, cfg.scene,
                _record(r, plan, base_score, base_score, False,
                        f"change_type={change_type} 不在阶段{cfg.stage}范围"),
            )
            no_improve += 1
            continue

        # 指标 gate:真变好(分数更小)才接受
        if new_score < base_score:
            mutate.commit(f"opt r{r}: {summary}", cfg.repo_root)
            memory_mod.add_round(
                cfg.memory_path, cfg.scene,
                _record(r, plan, base_score, new_score, True,
                        f"score {base_score:.3f}→{new_score:.3f}"),
            )
            accepted.append({"round": r, "summary": summary,
                             "score_before": base_score, "score_after": new_score})
            base_score = new_score
            report = new_report
            no_improve = 0
        else:
            mutate.rollback(snap, cfg.repo_root)
            memory_mod.add_round(
                cfg.memory_path, cfg.scene,
                _record(r, plan, base_score, new_score, False,
                        "no score improvement"),
            )
            no_improve += 1

    return {
        "accepted": accepted,
        "base_score": base_score,
        "rounds": r + 1 if cfg.max_rounds else 0,
        "final_report": report,
    }


# --------------------------------------------------------------------------- #
# 内部小工具                                                                    #
# --------------------------------------------------------------------------- #

def _record(round_idx, plan, score_before, score_after, accepted, reason) -> dict:
    """组一条 memory 轮次记录(spec §5.3)。"""
    return {
        "round": round_idx,
        "target_issue": plan.get("target_issue", ""),
        "change_type": plan.get("change_type", ""),
        "summary": plan.get("expected_effect", "") or reason,
        "score_before": round(score_before, 4),
        "score_after": round(score_after, 4),
        "accepted": accepted,
        "reason": reason,
    }


def _normalize_search_space(search_space: list, tunables: dict) -> list:
    """把 LLM 的 search_space([{key,range}])补上 type(从 tunables 读),供 search 用。"""
    params = tunables.get("params", {})
    out = []
    for entry in search_space:
        key = entry["key"]
        dtype = params.get(key, {}).get("type", "float")
        out.append({"key": key, "range": entry["range"], "type": dtype})
    return out


def _summarize_point(point: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in point.items())


def print_summary(summary: dict) -> None:
    """终端打印总结:接受的改动 + score 前后 + 剩余 issue。"""
    print("\n=== 优化闭环总结 ===")
    if summary["accepted"]:
        print("接受的改动:")
        for a in summary["accepted"]:
            print("  r%d: %s  (score %.3f → %.3f)"
                  % (a["round"], a["summary"], a["score_before"], a["score_after"]))
    else:
        print("接受的改动: 无")
    print("最终 score: %.3f" % summary["base_score"])
    remaining = summary["final_report"].get("issues", [])
    if remaining:
        print("剩余 issue (%d):" % len(remaining))
        for i in remaining:
            print("  [%s] %s: %s"
                  % (i.get("severity", "?"), i.get("id", "?"), i.get("message", "")))
    else:
        print("剩余 issue: 无")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    cfg = Config()
    summary = optimize_loop(cfg)
    print_summary(summary)
    return 0


if __name__ == "__main__":
    # harness 非包:确保同目录模块可 import
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    raise SystemExit(main())
