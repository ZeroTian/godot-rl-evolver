"""评估结果类型与 telemetry 真实性校验(优化闭环阶段 1)。

把每次试玩的 telemetry JSONL 绑定到本次进程:校验 run 头的 scene/model/
speedup、局数下限、以及 report run_id 与首行一致,杜绝复用旧报告或冒充
provenance。配对改善以相同 seed 的候选—baseline 分数差均值判定。

复用 diagnose.py 的 load_jsonl/aggregate/diagnose/build_report;纯标准库。
契约见 docs/plans/2026-06-28-llm-optimization-loop-stage1-plan.md §1 与
docs/specs/2026-06-28-llm-optimization-loop-design.md §5.5。
"""
import hashlib
from dataclasses import dataclass
from statistics import fmean

import diagnose


@dataclass(frozen=True)
class RunResult:
    seed: int
    telemetry_path: str
    run_id: str
    report: dict
    score: float
    provenance: dict


@dataclass(frozen=True)
class EvaluationResult:
    runs: tuple  # tuple[RunResult, ...]

    @property
    def by_seed(self):
        out = {run.seed: run for run in self.runs}
        if len(out) != len(self.runs):
            raise ValueError("duplicate evaluation seed")
        return out

    @property
    def mean_score(self):
        if not self.runs:
            raise ValueError("evaluation contains no runs")
        return fmean(run.score for run in self.runs)

    @property
    def representative_report(self):
        """seed 排序后第一份报告,仅供 LLM 阅读,不参与均值合并。"""
        if not self.runs:
            raise ValueError("evaluation contains no runs")
        return min(self.runs, key=lambda run: run.seed).report


def sha256_file(path):
    """文件内容的 SHA-256 十六进制摘要。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_telemetry(path, *, scene, model, speedup, min_episodes):
    """读 JSONL 首行校验 scene/model/speedup,诊断后校验局数与 run_id 绑定。

    步骤:
    1. load_jsonl 取记录;首行必须是 type:"run"。
    2. 校验 run 头 scene/model/speedup 与期望完全一致。
    3. 校验首行 run_id 与各 episode 行声明的 run_id 一致(防篡改/拼接)。
    4. aggregate→diagnose→build_report;用精确键 report["summary"]
       ["n_episodes"] 校验 >= min_episodes,report["run_id"] 与首行一致。

    返回 (report, run_id)。任何不符抛 ValueError。
    """
    records = diagnose.load_jsonl(path)
    if not records:
        raise ValueError("telemetry 为空: %s" % path)

    header = records[0]
    if header.get("type") != "run":
        raise ValueError("首行不是 run 头: %s" % path)

    if header.get("scene") != scene:
        raise ValueError(
            "run 头 scene 不符: 期望 %r,实际 %r" % (scene, header.get("scene")))
    if header.get("model") != model:
        raise ValueError(
            "run 头 model 不符: 期望 %r,实际 %r" % (model, header.get("model")))
    if int(header.get("speedup", -1)) != int(speedup):
        raise ValueError(
            "run 头 speedup 不符: 期望 %r,实际 %r"
            % (speedup, header.get("speedup")))

    header_run_id = header.get("run_id", "")
    if not header_run_id:
        raise ValueError("run 头缺少 run_id: %s" % path)

    # 每条 episode 行声明的 run_id 必须与首行一致(防止旧文件被改头冒充)
    for rec in records:
        if rec.get("type") == "episode":
            ep_rid = rec.get("run_id", "")
            if ep_rid != header_run_id:
                raise ValueError(
                    "episode run_id %r 与首行 run_id %r 不一致(疑似拼接/篡改)"
                    % (ep_rid, header_run_id))

    agg = diagnose.aggregate(records)
    issues = diagnose.diagnose(agg)
    report = diagnose.build_report(agg, issues)

    n_episodes = report["summary"]["n_episodes"]
    if n_episodes < min_episodes:
        raise ValueError(
            "有效局数不足: summary.n_episodes=%d < min_episodes=%d"
            % (n_episodes, min_episodes))

    if report["run_id"] != header_run_id:
        raise ValueError(
            "report run_id %r 与首行 run_id %r 不一致"
            % (report["run_id"], header_run_id))

    return report, header_run_id


def paired_improvement(base, candidate):
    """同 seed 配对:返回 mean(base.score - candidate.score)。

    要求 base 与 candidate 的 seed 集合完全相同,否则抛 ValueError。
    """
    base_by_seed = base.by_seed
    cand_by_seed = candidate.by_seed
    if set(base_by_seed) != set(cand_by_seed):
        raise ValueError(
            "配对改善要求 seed 集合完全相同: base=%s candidate=%s"
            % (sorted(base_by_seed), sorted(cand_by_seed)))
    diffs = [base_by_seed[s].score - cand_by_seed[s].score for s in base_by_seed]
    return fmean(diffs)
