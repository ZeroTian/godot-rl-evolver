"""通用试玩诊断器:读 telemetry JSONL → 聚合 → 规则引擎 → report.json + 摘要。

与具体游戏无关(纯标准库)。设计见
docs/superpowers/specs/2026-06-28-telemetry-diagnosis-design.md。
"""
import json
import math


def load_jsonl(path):
    """逐行 json.loads;跳过空行;非法行抛 ValueError(带行号)。"""
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError("第 %d 行 JSON 解析失败: %s" % (i, e)) from e
    return out


WIN_TERMS = {"goal", "win", "clear"}  # 视为通关的 term


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _pop_std(xs):
    """总体标准差(population std)。"""
    if not xs:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def aggregate(records):
    """把 run/episode 记录聚合成 run 级指标 dict(契约见 spec §5.2)。"""
    run_hdr = next((r for r in records if r.get("type") == "run"), {})
    eps = [r for r in records if r.get("type") == "episode"]
    grid_cell = int(run_hdr.get("grid", {}).get("cell", 64))

    n = len(eps)
    agg = {
        "run_id": run_hdr.get("run_id", ""),
        "scene": run_hdr.get("scene", ""),
        "grid_cell": grid_cell,
        "n_episodes": n,
        "completion_rate": 0.0,
        "term_distribution": {},
        "mean_len": 0.0, "mean_return": 0.0,
        "return_std": 0.0, "len_std": 0.0,
        "action_usage": {},
        "mean_action_entropy": 0.0,
        "mean_coverage_entropy": 0.0,
        "mean_cells": 0.0,
        "end_pos_grid": {},
        "max_ep": int(run_hdr.get("max_ep", 0)),
    }
    if n == 0:
        return agg

    terms = [e.get("term", "unknown") for e in eps]
    agg["completion_rate"] = sum(1 for t in terms if t in WIN_TERMS) / n
    tc = {}
    for t in terms:
        tc[t] = tc.get(t, 0) + 1
    agg["term_distribution"] = {t: c / n for t, c in tc.items()}

    lens = [float(e.get("len", 0)) for e in eps]
    rets = [float(e.get("return", 0.0)) for e in eps]
    agg["mean_len"] = _mean(lens)
    agg["mean_return"] = _mean(rets)
    agg["return_std"] = _pop_std(rets)
    agg["len_std"] = _pop_std(lens)
    if not agg["max_ep"]:
        agg["max_ep"] = int(max(lens)) if lens else 0

    # 动作占比:各维各档跨全 run 平均
    usage = {}
    for e in eps:
        for dim, probs in e.get("actions", {}).items():
            acc = usage.setdefault(dim, [0.0] * len(probs))
            for i, p in enumerate(probs):
                acc[i] += p
    agg["action_usage"] = {dim: [v / n for v in acc] for dim, acc in usage.items()}

    agg["mean_action_entropy"] = _mean([float(e.get("action_entropy", 0.0)) for e in eps])
    agg["mean_coverage_entropy"] = _mean(
        [float(e.get("coverage", {}).get("entropy", 0.0)) for e in eps])
    agg["mean_cells"] = _mean(
        [float(e.get("coverage", {}).get("cells", 0)) for e in eps])

    # 终止/死亡位置网格密度:优先 death 事件(内嵌 episode.events),否则 end_pos
    grid = {}
    for e in eps:
        deaths = [ev for ev in e.get("events", []) if ev.get("name") == "death"]
        if deaths:
            positions = [ev.get("pos", [0, 0]) for ev in deaths]
        else:
            positions = [e.get("end_pos", [0, 0])]
        for pos in positions:
            cell = (int(math.floor(pos[0] / grid_cell)),
                    int(math.floor(pos[1] / grid_cell)))
            grid[cell] = grid.get(cell, 0) + 1
    agg["end_pos_grid"] = grid
    return agg


# 默认阈值(spec §6;可被 --thresholds JSON 覆盖)
THRESHOLDS = {
    "hard_completion": 0.10, "easy_completion": 0.90, "easy_len_frac": 0.5,
    "hotspot_sigma": 2.0, "dominant_term_frac": 0.60,
    "stall_len_frac": 0.9, "stall_return": 0.0,
    "redundant_usage": 0.01, "ent_min": 0.5, "cov_ent_min": 1.0,
    "unstable_cv": 1.5,
}


def _issue(id, severity, category, metric, value, threshold, message, evidence):
    return {"id": id, "severity": severity, "category": category,
            "metric": metric, "value": value, "threshold": threshold,
            "message": message, "evidence": evidence}


def diagnose(agg, thresholds=None):
    """跑所有规则,返回 issue 列表(每条见 spec §5.3)。"""
    t = dict(THRESHOLDS)
    if thresholds:
        t.update(thresholds)
    issues = []
    if agg.get("n_episodes", 0) == 0:
        return issues

    cr = agg["completion_rate"]
    max_ep = agg.get("max_ep", 0)

    # difficulty_too_hard
    if cr < t["hard_completion"]:
        issues.append(_issue(
            "difficulty_too_hard", "high", "tuning", "completion_rate", cr,
            t["hard_completion"],
            "通关率 %.0f%%,低于 %.0f%%,对当前策略过难" % (cr * 100, t["hard_completion"] * 100),
            {"n_episodes": agg["n_episodes"],
             "top_fail_cells": _top_cells(agg["end_pos_grid"])}))

    # difficulty_too_easy
    if cr > t["easy_completion"] and max_ep and agg["mean_len"] < t["easy_len_frac"] * max_ep:
        issues.append(_issue(
            "difficulty_too_easy", "low", "tuning", "completion_rate", cr,
            t["easy_completion"],
            "通关率 %.0f%% 且平均局长偏短,可能过易" % (cr * 100),
            {"mean_len": agg["mean_len"], "max_ep": max_ep}))

    # death_hotspot:某格 count > mean + sigma*std
    grid = agg["end_pos_grid"]
    if len(grid) >= 2:
        counts = list(grid.values())
        m, s = _mean(counts), _pop_std(counts)
        if s > 0:
            thr = m + t["hotspot_sigma"] * s
            hot = {c: n for c, n in grid.items() if n > thr}
            if hot:
                top = max(hot.items(), key=lambda kv: kv[1])
                issues.append(_issue(
                    "death_hotspot", "high", "structural", "end_pos_grid",
                    top[1], round(thr, 2),
                    "终止位置在某点聚集(%d 次,超过 mean+%gσ),疑似难度尖峰/陷阱"
                    % (top[1], t["hotspot_sigma"]),
                    {"top_fail_cells": _top_cells(grid)}))

    # done_reason_skew:某非通关 term 占比 > dominant_term_frac
    skewed = [(term, frac) for term, frac in agg["term_distribution"].items()
              if term not in WIN_TERMS and frac > t["dominant_term_frac"]]
    if skewed:
        term, frac = max(skewed, key=lambda kv: kv[1])
        issues.append(_issue(
            "done_reason_skew", "high", "structural", "term_distribution", frac,
            t["dominant_term_frac"],
            "终止原因 '%s' 占比 %.0f%%,主导失败,对应位置可能是难度尖峰" % (term, frac * 100),
            {"term": term, "fraction": frac}))

    # progress_stall
    if max_ep and agg["mean_len"] >= t["stall_len_frac"] * max_ep \
            and agg["mean_return"] < t["stall_return"]:
        issues.append(_issue(
            "progress_stall", "medium", "structural", "mean_len", agg["mean_len"],
            t["stall_len_frac"] * max_ep,
            "平均局长接近上限且回报低,agent 疑似被卡住",
            {"mean_len": agg["mean_len"], "mean_return": agg["mean_return"]}))

    # redundant_action:某动作档使用占比 < redundant_usage
    for dim, probs in agg["action_usage"].items():
        for idx, p in enumerate(probs):
            if p < t["redundant_usage"]:
                issues.append(_issue(
                    "redundant_action", "medium", "tuning",
                    "action_usage.%s[%d]" % (dim, idx), p, t["redundant_usage"],
                    "动作 %s 档位 %d 几乎不用(%.2f%%),可能 agent 学会绕过此机制"
                    "(如跳过战斗);仅提示,勿据此自动删除(可能是 stepping-stone)"
                    % (dim, idx, p * 100),
                    {"dim": dim, "bin": idx, "usage": p}))

    # monotony
    if agg["mean_action_entropy"] < t["ent_min"] \
            or agg["mean_coverage_entropy"] < t["cov_ent_min"]:
        issues.append(_issue(
            "monotony", "low", "structural", "entropy",
            min(agg["mean_action_entropy"], agg["mean_coverage_entropy"]),
            min(t["ent_min"], t["cov_ent_min"]),
            "动作或空间探索熵偏低,玩法可能单调",
            {"mean_action_entropy": agg["mean_action_entropy"],
             "mean_coverage_entropy": agg["mean_coverage_entropy"]}))

    # unstable_difficulty
    mr = agg["mean_return"]
    if mr != 0:
        cv = agg["return_std"] / abs(mr)
        if cv > t["unstable_cv"]:
            issues.append(_issue(
                "unstable_difficulty", "low", "tuning", "return_cv", cv,
                t["unstable_cv"],
                "回报离散度高(CV=%.2f),通关稳定性差/运气成分大(体感粗信号)" % cv,
                {"mean_return": mr, "return_std": agg["return_std"]}))

    return issues


def _top_cells(grid, k=3):
    """按 count 降序取前 k 个格子的格索引(evidence 仅供参考)。"""
    items = sorted(grid.items(), key=lambda kv: kv[1], reverse=True)[:k]
    return [[c[0], c[1]] for c, _ in items]


def build_report(agg, issues, meta=None):
    """组装 report.json 顶层结构(spec §5.3)。"""
    return {
        "run_id": agg.get("run_id", ""),
        "generated_for": agg.get("scene", ""),
        "agent_relative": True,
        "summary": {
            "n_episodes": agg.get("n_episodes", 0),
            "completion_rate": round(agg.get("completion_rate", 0.0), 4),
            "mean_len": round(agg.get("mean_len", 0.0), 1),
            "mean_return": round(agg.get("mean_return", 0.0), 3),
            "term_distribution": {k: round(v, 3)
                                  for k, v in agg.get("term_distribution", {}).items()},
        },
        "issues": issues,
        **({"meta": meta} if meta else {}),
    }


_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def format_summary(report):
    """生成控制台人读摘要。"""
    s = report["summary"]
    lines = [
        "=== 试玩诊断报告 (针对当前训练策略的相对结论) ===",
        "场景: %s  局数: %d" % (report.get("generated_for", "?"), s["n_episodes"]),
        "通关率: %.1f%%  平均局长: %.0f  平均回报: %.2f"
        % (s["completion_rate"] * 100, s["mean_len"], s["mean_return"]),
        "终止原因分布: %s" % ", ".join(
            "%s=%.0f%%" % (k, v * 100) for k, v in s.get("term_distribution", {}).items()),
        "",
    ]
    issues = sorted(report["issues"], key=lambda i: _SEV_ORDER.get(i["severity"], 9))
    if not issues:
        lines.append("未发现问题。")
    else:
        lines.append("发现 %d 个问题:" % len(issues))
        for i in issues:
            lines.append("  [%s/%s] %s — %s"
                         % (i["severity"].upper(), i["category"], i["id"], i["message"]))
    return "\n".join(lines)


def main(argv=None):
    """CLI: diagnose.py <jsonl> [--out report.json] [--thresholds JSON]。"""
    import argparse
    import os

    ap = argparse.ArgumentParser(description="试玩 telemetry 诊断器")
    ap.add_argument("path", help="telemetry JSONL 文件路径")
    ap.add_argument("--out", default=None, help="report.json 输出路径(默认 jsonl 同目录)")
    ap.add_argument("--thresholds", default=None, help="覆盖默认阈值的 JSON 字符串")
    args = ap.parse_args(argv)

    thr = json.loads(args.thresholds) if args.thresholds else None
    records = load_jsonl(args.path)
    agg = aggregate(records)
    issues = diagnose(agg, thr)
    report = build_report(agg, issues)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.path)),
                                   "report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(format_summary(report))
    print("\n报告已写入: %s" % out)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
