"""personas.py 单测(主观体验层 S2 · Task 1 + Task 3)。

Task 1: load_persona / list_personas —— 纯文件解析 + 校验(标准库)。
  persona = 一份 reward-shaping 权重 profile(冻结仪器面板)。reward_weights 必须
  **恰好** = REWARD_KEYS 集合(缺键/多未知键均 ValueError),与 game_agent.gd 的
  权威键表共享常量,避免 .gd 与校验器分叉。

Task 3: run_persona_panel —— 多 persona 试玩编排(不起 Godot,monkeypatch
  optimize.evaluate_current,断言每 persona 用其 model、各自独立 artifact 子目录)。
"""
import copy
import json
import os
import sys

HARNESS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "harness")
if HARNESS not in sys.path:
    sys.path.insert(0, HARNESS)

import pytest          # noqa: E402

import personas        # noqa: E402
import optimize        # noqa: E402


# --------------------------------------------------------------------------- #
# Task 1 脚手架                                                                 #
# --------------------------------------------------------------------------- #

def _valid_weights():
    """一份恰好覆盖 REWARD_KEYS 的合法权重(取权威键表原值)。"""
    return {
        "progress": 0.01, "time_penalty": 0.002, "damage": 0.1, "kill": 25.0,
        "combat_shape": 0.5, "hurt_penalty": 0.5, "gap_edge_jump": 1.0,
        "gap_cross": 8.0, "goal": 30.0, "fall": 10.0, "hp_fail": 10.0,
    }


def _write_persona(path, name="cautious", weights=None, model="m.zip"):
    obj = {"name": name, "reward_weights": weights or _valid_weights(),
           "model": model}
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_reward_keys_match_authoritative_table():
    """REWARD_KEYS 必须恰好是权威 11 键(供 .gd 与校验共享)。"""
    assert personas.REWARD_KEYS == set(_valid_weights().keys())


def test_load_persona_valid(tmp_path):
    p = _write_persona(tmp_path / "cautious.json")
    persona = personas.load_persona(str(p))
    assert persona["name"] == "cautious"
    assert persona["model"] == "m.zip"
    assert set(persona["reward_weights"]) == personas.REWARD_KEYS


def test_load_persona_rejects_missing_weight_key(tmp_path):
    w = _valid_weights()
    del w["kill"]
    p = _write_persona(tmp_path / "bad.json", weights=w)
    with pytest.raises(ValueError):
        personas.load_persona(str(p))


def test_load_persona_rejects_unknown_key(tmp_path):
    w = _valid_weights()
    w["bogus"] = 1.0
    p = _write_persona(tmp_path / "bad.json", weights=w)
    with pytest.raises(ValueError):
        personas.load_persona(str(p))


def test_load_persona_rejects_missing_field(tmp_path):
    """缺 name/model 等顶层字段 → ValueError。"""
    (tmp_path / "nomodel.json").write_text(
        json.dumps({"name": "x", "reward_weights": _valid_weights()}),
        encoding="utf-8")
    with pytest.raises(ValueError):
        personas.load_persona(str(tmp_path / "nomodel.json"))


def test_list_personas_sorted(tmp_path):
    _write_persona(tmp_path / "z.json", name="zeta")
    _write_persona(tmp_path / "a.json", name="alpha")
    _write_persona(tmp_path / "m.json", name="mid")
    got = personas.list_personas(str(tmp_path))
    assert [p["name"] for p in got] == ["alpha", "mid", "zeta"]


def test_shipped_persona_profiles_load():
    """仓内 personas/*.json 必须全部合法(default + 4 风格)。"""
    repo_root = os.path.dirname(os.path.dirname(__file__))
    pdir = os.path.join(repo_root, "personas")
    got = personas.list_personas(pdir)
    names = {p["name"] for p in got}
    assert {"default", "aggressive", "cautious",
            "speedrunner", "explorer"} <= names


# --------------------------------------------------------------------------- #
# Task 3 脚手架                                                                 #
# --------------------------------------------------------------------------- #

class _FakeCfg:
    """run_persona_panel 只需可浅拷贝 + 有 model/opt_run_id/scene 字段的容器。"""

    def __init__(self):
        self.model = "base.zip"
        self.opt_run_id = ""
        self.scene = "res://rl/train.tscn"
        self.artifact_root = "/tmp/art"


def _persona(name, model):
    return {"name": name, "reward_weights": _valid_weights(), "model": model}


def test_run_persona_panel_uses_each_model_and_isolated_dirs(monkeypatch):
    calls = []

    def _fake_eval(cfg, *, point_id):
        calls.append({"model": cfg.model, "opt_run_id": cfg.opt_run_id,
                      "point_id": point_id})
        return "EVAL_" + cfg.model

    monkeypatch.setattr(optimize, "evaluate_current", _fake_eval)

    cfg = _FakeCfg()
    plist = [_persona("aggressive", "agg.zip"),
             _persona("cautious", "cau.zip")]
    out = personas.run_persona_panel(cfg, plist, panel_run_id="RUN42")

    # 返回 {name: EvaluationResult}
    assert set(out) == {"aggressive", "cautious"}
    assert out["aggressive"] == "EVAL_agg.zip"
    assert out["cautious"] == "EVAL_cau.zip"

    # 每 persona 用其自己的 model + panel_run_id + 隔离的 point_id
    by_model = {c["model"]: c for c in calls}
    assert set(by_model) == {"agg.zip", "cau.zip"}
    for c in calls:
        assert c["opt_run_id"] == "RUN42"
        assert c["point_id"] in ("persona_aggressive", "persona_cautious")
    # 原始 cfg 未被污染(应为浅拷贝再覆盖)
    assert cfg.model == "base.zip"
    assert cfg.opt_run_id == ""


def test_run_persona_panel_continues_on_one_failure(monkeypatch):
    def _fake_eval(cfg, *, point_id):
        if cfg.model == "boom.zip":
            raise RuntimeError("evaluation blew up")
        return "EVAL_" + cfg.model

    monkeypatch.setattr(optimize, "evaluate_current", _fake_eval)

    cfg = _FakeCfg()
    plist = [_persona("aggressive", "agg.zip"),
             _persona("broken", "boom.zip"),
             _persona("cautious", "cau.zip")]
    out = personas.run_persona_panel(cfg, plist, panel_run_id="RUN9")

    # 失败的 persona 缺席,其余照常返回
    assert set(out) == {"aggressive", "cautious"}
    assert "broken" not in out
