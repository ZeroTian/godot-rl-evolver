"""客观分数:把 diagnose 产出的 report.json 压成一个标量(越小越好)。

供贝叶斯优化目标 + 接受判定共用(spec §5.4)。纯函数,容错缺字段。

  score = w_issue   * Σ SEV_W[severity]
        + w_diff    * |completion_rate - target|
        + w_unstable* max(0, return_cv - unstable_target)
"""

SEV_W = {"high": 3.0, "medium": 1.0, "low": 0.3}

DEFAULT_WEIGHTS = {"w_issue": 1.0, "w_diff": 2.0, "w_unstable": 0.3}

# return_cv 超过这个阈值才算"不稳定",开始计罚
UNSTABLE_TARGET = 0.5


def _extract_return_cv(report):
    """report.summary 里没有 return_cv;它出现在 unstable_difficulty issue 的 value。"""
    summary = report.get("summary") or {}
    if "return_cv" in summary:
        return summary["return_cv"]
    for issue in report.get("issues") or []:
        if issue.get("metric") == "return_cv":
            try:
                return float(issue["value"])
            except (TypeError, ValueError, KeyError):
                continue
    return None


def score(report, weights=None, target=0.65, unstable_target=UNSTABLE_TARGET):
    """report → 标量分数(越小越好)。容错缺字段/空 issues。"""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    summary = report.get("summary") or {}
    issues = report.get("issues") or []

    issue_term = sum(SEV_W.get(i.get("severity"), 0.0) for i in issues)

    completion = summary.get("completion_rate", 0.0)
    diff_term = abs(completion - target)

    return_cv = _extract_return_cv(report)
    if return_cv is None:
        unstable_term = 0.0
    else:
        unstable_term = max(0.0, return_cv - unstable_target)

    return (w["w_issue"] * issue_term
            + w["w_diff"] * diff_term
            + w["w_unstable"] * unstable_term)
