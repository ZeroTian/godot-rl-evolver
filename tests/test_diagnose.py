"""diagnose.py 单元测试(TDD)。"""
import json

import pytest

import diagnose


# ── 循环 A:load_jsonl ────────────────────────────────────────────────
def test_load_jsonl_skips_blank_and_parses(tmp_path):
    p = tmp_path / "run.jsonl"
    p.write_text(
        '{"type":"run","run_id":"x"}\n'
        "\n"  # 空行应被跳过
        '   \n'  # 纯空白行应被跳过
        '{"type":"episode","ep":0}\n'
    )
    recs = diagnose.load_jsonl(str(p))
    assert len(recs) == 2
    assert recs[0]["type"] == "run"
    assert recs[1]["ep"] == 0


def test_load_jsonl_raises_on_bad_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"ok":1}\n{not valid json}\n')
    with pytest.raises(ValueError) as ei:
        diagnose.load_jsonl(str(p))
    assert "2" in str(ei.value)  # 错误信息含行号


# ── 循环 B:aggregate ─────────────────────────────────────────────────
def _ep(ep=0, length=100, ret=1.0, term="timeout", actions=None,
        action_entropy=1.0, cov_cells=10, cov_entropy=2.0,
        end_pos=(0, 0), events=None, metrics=None):
    """构造一条 episode 记录(测试用)。"""
    return {
        "type": "episode", "run_id": "t", "ep": ep, "len": length,
        "return": ret, "term": term,
        "actions": actions or {"move": [0.3, 0.3, 0.4], "jump": [0.9, 0.1]},
        "action_entropy": action_entropy,
        "coverage": {"cells": cov_cells, "entropy": cov_entropy},
        "end_pos": list(end_pos),
        "events": events or [],
        "metrics": metrics or {},
    }


def _run_header(cell=64, max_ep=1500):
    return {"type": "run", "run_id": "t", "grid": {"cell": cell},
            "max_ep": max_ep, "scene": "res://t.tscn"}


def test_aggregate_completion_rate():
    recs = [_run_header()] + [_ep(term="goal")] * 2 + [_ep(term="fall")] * 8
    agg = diagnose.aggregate(recs)
    assert agg["n_episodes"] == 10
    assert agg["completion_rate"] == pytest.approx(0.2)


def test_aggregate_term_distribution_sums_to_one():
    recs = [_ep(term="goal"), _ep(term="fall"), _ep(term="hp"),
            _ep(term="timeout")]
    agg = diagnose.aggregate(recs)
    assert sum(agg["term_distribution"].values()) == pytest.approx(1.0)
    assert agg["term_distribution"]["fall"] == pytest.approx(0.25)


def test_aggregate_unknown_term_when_missing():
    e = _ep()
    del e["term"]
    agg = diagnose.aggregate([e])
    assert agg["term_distribution"]["unknown"] == pytest.approx(1.0)


def test_aggregate_action_usage_and_entropy_means():
    recs = [
        _ep(actions={"move": [0.0, 0.0, 1.0]}, action_entropy=0.0),
        _ep(actions={"move": [1.0, 0.0, 0.0]}, action_entropy=2.0),
    ]
    agg = diagnose.aggregate(recs)
    assert agg["action_usage"]["move"] == pytest.approx([0.5, 0.0, 0.5])
    assert agg["mean_action_entropy"] == pytest.approx(1.0)


def test_aggregate_means_and_std():
    recs = [_ep(length=100, ret=2.0), _ep(length=300, ret=4.0)]
    agg = diagnose.aggregate(recs)
    assert agg["mean_len"] == pytest.approx(200.0)
    assert agg["mean_return"] == pytest.approx(3.0)
    assert agg["return_std"] == pytest.approx(1.0)  # 总体标准差


def test_aggregate_end_pos_grid_prefers_death_events():
    # 有 death 事件时用 death.pos;cell=100 → (250,140) 落格 (2,1)
    recs = [
        _run_header(cell=100),
        _ep(end_pos=(999, 999),
            events=[{"name": "death", "pos": [250, 140], "cause": "fall"}]),
    ]
    agg = diagnose.aggregate(recs)
    assert agg["grid_cell"] == 100
    assert agg["end_pos_grid"].get((2, 1)) == 1
    assert (9, 9) not in agg["end_pos_grid"]  # end_pos 被 death 事件取代


def test_aggregate_end_pos_grid_fallback_to_end_pos():
    recs = [_run_header(cell=100), _ep(end_pos=(150, 50), events=[])]
    agg = diagnose.aggregate(recs)
    assert agg["end_pos_grid"].get((1, 0)) == 1


def test_aggregate_empty_is_safe():
    agg = diagnose.aggregate([])
    assert agg["n_episodes"] == 0
    assert agg["completion_rate"] == 0.0
    assert agg["term_distribution"] == {}
    assert agg["end_pos_grid"] == {}


# ── 循环 C:诊断规则 ──────────────────────────────────────────────────
def _ids(issues):
    return {i["id"] for i in issues}


def _agg(**over):
    """构造一个"健康"基线 agg,再用 over 覆盖个别字段以触发单条规则。
    基线刻意不触发任何规则。"""
    base = {
        "run_id": "t", "scene": "res://t.tscn", "grid_cell": 64,
        "n_episodes": 50, "completion_rate": 0.5,
        "term_distribution": {"goal": 0.5, "fall": 0.25, "timeout": 0.25},
        "mean_len": 300.0, "mean_return": 10.0,
        "return_std": 2.0, "len_std": 50.0,
        "action_usage": {"move": [0.3, 0.3, 0.4], "jump": [0.6, 0.4]},
        "mean_action_entropy": 1.5, "mean_coverage_entropy": 2.5,
        "mean_cells": 30.0,
        "end_pos_grid": {(1, 0): 2, (2, 0): 3, (3, 0): 2},
        "max_ep": 1500,
    }
    base.update(over)
    return base


def test_baseline_agg_is_healthy():
    assert diagnose.diagnose(_agg()) == []


def test_difficulty_too_hard():
    assert "difficulty_too_hard" in _ids(diagnose.diagnose(_agg(completion_rate=0.04)))
    assert "difficulty_too_hard" not in _ids(diagnose.diagnose(_agg(completion_rate=0.5)))


def test_difficulty_too_easy():
    over = dict(completion_rate=0.95, mean_len=100.0)  # 100 < 0.5*1500
    assert "difficulty_too_easy" in _ids(diagnose.diagnose(_agg(**over)))
    # 高通关但局长不短 → 不触发
    assert "difficulty_too_easy" not in _ids(
        diagnose.diagnose(_agg(completion_rate=0.95, mean_len=1000.0)))


def test_death_hotspot():
    # 一个格子远高于其他 → 触发(多个低值格,使尖峰明显超 mean+2σ)
    spike = {(i, 0): 1 for i in range(9)}
    spike[(9, 0)] = 20
    assert "death_hotspot" in _ids(diagnose.diagnose(_agg(end_pos_grid=spike)))
    # 均匀分布 → 不触发
    flat = {(1, 0): 3, (2, 0): 3, (3, 0): 3, (4, 0): 3}
    assert "death_hotspot" not in _ids(diagnose.diagnose(_agg(end_pos_grid=flat)))


def test_done_reason_skew():
    skew = {"goal": 0.1, "fall": 0.7, "timeout": 0.2}  # fall 0.7 > 0.6
    assert "done_reason_skew" in _ids(diagnose.diagnose(_agg(term_distribution=skew)))
    # 通关主导不算问题(goal 是 win term)
    win = {"goal": 0.7, "fall": 0.3}
    assert "done_reason_skew" not in _ids(diagnose.diagnose(_agg(term_distribution=win)))


def test_progress_stall():
    over = dict(mean_len=1400.0, mean_return=-1.0)  # 1400 >= 0.9*1500 且 return<0
    assert "progress_stall" in _ids(diagnose.diagnose(_agg(**over)))
    assert "progress_stall" not in _ids(diagnose.diagnose(_agg(mean_len=300.0)))


def test_redundant_action():
    over = {"action_usage": {"attack": [0.995, 0.005]}}  # 第二档 0.005 < 0.01
    issues = diagnose.diagnose(_agg(**over))
    assert "redundant_action" in _ids(issues)
    ra = next(i for i in issues if i["id"] == "redundant_action")
    assert "绕过" in ra["message"]  # 关联 7e 信号④
    # 均衡使用 → 不触发
    assert "redundant_action" not in _ids(
        diagnose.diagnose(_agg(action_usage={"attack": [0.5, 0.5]})))


def test_monotony():
    assert "monotony" in _ids(diagnose.diagnose(_agg(mean_action_entropy=0.2)))
    assert "monotony" in _ids(diagnose.diagnose(_agg(mean_coverage_entropy=0.5)))
    assert "monotony" not in _ids(diagnose.diagnose(_agg()))


def test_unstable_difficulty():
    over = dict(mean_return=2.0, return_std=10.0)  # cv=5 > 1.5
    assert "unstable_difficulty" in _ids(diagnose.diagnose(_agg(**over)))
    assert "unstable_difficulty" not in _ids(
        diagnose.diagnose(_agg(mean_return=10.0, return_std=2.0)))


def test_issue_shape():
    issue = diagnose.diagnose(_agg(completion_rate=0.04))[0]
    for k in ("id", "severity", "category", "metric", "value", "threshold",
              "message", "evidence"):
        assert k in issue
    assert issue["category"] in ("structural", "tuning", "fork")


# ── 循环 D:report / summary / main ───────────────────────────────────
def test_build_report_shape():
    agg = _agg(completion_rate=0.04)
    issues = diagnose.diagnose(agg)
    rep = diagnose.build_report(agg, issues)
    assert rep["agent_relative"] is True
    assert rep["run_id"] == "t"
    assert rep["generated_for"] == "res://t.tscn"
    assert isinstance(rep["issues"], list) and rep["issues"]
    assert rep["summary"]["n_episodes"] == 50
    assert "completion_rate" in rep["summary"]


def test_format_summary_human_readable():
    agg = _agg(completion_rate=0.04)
    rep = diagnose.build_report(agg, diagnose.diagnose(agg))
    text = diagnose.format_summary(rep)
    assert "difficulty_too_hard" in text
    assert "50" in text  # n_episodes 出现在摘要


def test_main_writes_report(tmp_path):
    jsonl = tmp_path / "run_1.jsonl"
    lines = [json.dumps(_run_header(cell=100, max_ep=1500))]
    lines += [json.dumps(_ep(term="fall", end_pos=(250, 140),
                             events=[{"name": "death", "pos": [250, 140],
                                      "cause": "fall"}]))
              for _ in range(50)]
    jsonl.write_text("\n".join(lines) + "\n")
    out = tmp_path / "report.json"
    rc = diagnose.main([str(jsonl), "--out", str(out)])
    assert rc == 0
    rep = json.loads(out.read_text())
    # 全 fall + 0 通关 → 必然有 difficulty_too_hard 与 done_reason_skew
    ids = {i["id"] for i in rep["issues"]}
    assert "difficulty_too_hard" in ids
    assert "done_reason_skew" in ids


def test_main_thresholds_override(tmp_path):
    jsonl = tmp_path / "run_2.jsonl"
    lines = [json.dumps(_run_header(max_ep=1500))]
    # 通关率 0.2:默认 hard=0.10 不触发;提高到 0.30 则触发
    lines += [json.dumps(_ep(term="goal")) for _ in range(2)]
    lines += [json.dumps(_ep(term="fall")) for _ in range(8)]
    jsonl.write_text("\n".join(lines) + "\n")
    out = tmp_path / "r.json"
    diagnose.main([str(jsonl), "--out", str(out),
                   "--thresholds", '{"hard_completion": 0.30}'])
    rep = json.loads(out.read_text())
    assert "difficulty_too_hard" in {i["id"] for i in rep["issues"]}
