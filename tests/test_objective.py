"""objective.score 纯函数单测(spec §5.4, 计划 AC2)。

score(report, weights=None, target=0.65) 越小越好:
  score = w_issue   * Σ SEV_W[severity]
        + w_diff    * |completion_rate - target|
        + w_unstable* max(0, return_cv - unstable_target)
SEV_W = {high:3, medium:1, low:0.3};默认 w_issue=1, w_diff=2, w_unstable=0.3。
容错缺字段 / 空 issues。
"""
import objective


def _report(completion_rate=0.65, issues=None, return_cv=None):
    summary = {"completion_rate": completion_rate}
    rep = {"summary": summary, "issues": issues or []}
    if return_cv is not None:
        # return_cv 通常以 unstable_difficulty issue 的 value 形式出现
        rep["issues"] = list(rep["issues"]) + [
            {"id": "unstable_difficulty", "severity": "low",
             "metric": "return_cv", "value": return_cv}
        ]
    return rep


def test_empty_report_scores_only_diff():
    # 空 issue + completion 命中 target → score 应为 0
    rep = _report(completion_rate=0.65, issues=[])
    assert objective.score(rep) == 0.0


def test_severity_weights():
    rep = _report(completion_rate=0.65, issues=[
        {"id": "a", "severity": "high"},
        {"id": "b", "severity": "medium"},
        {"id": "c", "severity": "low"},
    ])
    # 3 + 1 + 0.3 = 4.3,diff 项为 0
    assert abs(objective.score(rep) - 4.3) < 1e-9


def test_completion_diff_weighted():
    # completion 0.15,target 0.65 → diff=0.5,w_diff=2 → 1.0
    rep = _report(completion_rate=0.15, issues=[])
    assert abs(objective.score(rep) - 1.0) < 1e-9


def test_target_param_shifts_diff():
    rep = _report(completion_rate=0.4, issues=[])
    # target=0.4 → diff=0 → score 0
    assert objective.score(rep, target=0.4) == 0.0


def test_return_cv_penalty():
    # return_cv=0.6,unstable_target 默认 0.5 → max(0,0.1)*0.3 = 0.03
    # 该 issue 本身是 low(+0.3)。completion 命中 target。
    rep = _report(completion_rate=0.65, return_cv=0.6)
    assert abs(objective.score(rep) - (0.3 + 0.03)) < 1e-9


def test_return_cv_below_target_no_penalty():
    rep = _report(completion_rate=0.65, return_cv=0.3)
    # 仅 issue 本身 low=0.3,cv 罚项为 0
    assert abs(objective.score(rep) - 0.3) < 1e-9


def test_custom_weights():
    rep = _report(completion_rate=0.65, issues=[{"id": "a", "severity": "high"}])
    w = {"w_issue": 2.0, "w_diff": 0.0, "w_unstable": 0.0}
    assert abs(objective.score(rep, weights=w) - 6.0) < 1e-9


def test_missing_summary_field():
    # 缺 summary → completion 视为 0,diff=0.65,w_diff=2 → 1.3
    rep = {"issues": []}
    assert abs(objective.score(rep) - 1.3) < 1e-9


def test_missing_issues_field():
    rep = {"summary": {"completion_rate": 0.65}}
    assert objective.score(rep) == 0.0


def test_unknown_severity_ignored():
    rep = _report(completion_rate=0.65, issues=[{"id": "x", "severity": "weird"}])
    assert objective.score(rep) == 0.0


def test_hard_report_strictly_greater_than_improved():
    """AC2: 高难度报告 score 严格大于改善后报告。"""
    hard = _report(completion_rate=0.1, issues=[
        {"id": "difficulty_too_hard", "severity": "high"},
        {"id": "death_hotspot", "severity": "high"},
        {"id": "monotony", "severity": "medium"},
    ])
    improved = _report(completion_rate=0.6, issues=[
        {"id": "monotony", "severity": "low"},
    ])
    assert objective.score(hard) > objective.score(improved)
