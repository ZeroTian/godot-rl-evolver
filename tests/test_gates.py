"""harness/gates.py — 语法 gate(Godot --import)+ smoke gate(≥1 episode)。

全部 monkeypatch 假 subprocess / 假 run_one_seed,**不真起 Godot**。
syntax_gate:rc==0 且 stdout/stderr 无 SCRIPT ERROR/Parse Error/Failed to load script → 通过。
smoke_gate:以 EVAL_SEEDS[0]、SMOKE_MAX_STEPS、min_episodes=1 调 optimize.run_one_seed,
得到 ≥1 局 RunResult 即通过;异常即不过。两模块可共存导入(无循环 import)。
"""
import os
import subprocess
import sys

HARNESS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "harness")
if HARNESS not in sys.path:
    sys.path.insert(0, HARNESS)

import pytest          # noqa: E402

import gates           # noqa: E402
import optimize        # noqa: E402
import evaluation      # noqa: E402


def _gate_cfg(tmp_path):
    cfg = optimize.Config()
    cfg.proj = str(tmp_path / "proj")
    cfg.scene = "res://rl/train.tscn"
    cfg.model = str(tmp_path / "model.zip")
    cfg.speedup = 8
    cfg.eval_seeds = (1, 2, 3)
    cfg.eval_episodes = 20
    cfg.max_eval_steps = 40000
    cfg.smoke_max_steps = 2000
    cfg.smoke_timeout_seconds = 120
    cfg.artifact_root = str(tmp_path / "artifacts")
    cfg.opt_run_id = "RUN_X"
    return cfg


# --------------------------------------------------------------------------- #
# syntax_gate                                                                   #
# --------------------------------------------------------------------------- #

def _fake_proc(rc=0, stdout="", stderr=""):
    class R:
        returncode = rc
    R.stdout = stdout
    R.stderr = stderr
    return R


def test_syntax_gate_passes_on_clean_import(tmp_path, monkeypatch):
    cfg = _gate_cfg(tmp_path)
    monkeypatch.setattr(gates.subprocess, "run",
                        lambda *a, **k: _fake_proc(rc=0, stdout="Godot ok\n"))
    ok, detail = gates.syntax_gate(cfg)
    assert ok is True


def test_syntax_gate_fails_on_script_error(tmp_path, monkeypatch):
    cfg = _gate_cfg(tmp_path)
    monkeypatch.setattr(
        gates.subprocess, "run",
        lambda *a, **k: _fake_proc(
            rc=0, stdout="SCRIPT ERROR: Parse error on line 5\n"))
    ok, detail = gates.syntax_gate(cfg)
    assert ok is False
    assert "SCRIPT ERROR" in detail


def test_syntax_gate_fails_on_nonzero_rc(tmp_path, monkeypatch):
    cfg = _gate_cfg(tmp_path)
    monkeypatch.setattr(gates.subprocess, "run",
                        lambda *a, **k: _fake_proc(rc=1, stdout="", stderr="boom"))
    ok, detail = gates.syntax_gate(cfg)
    assert ok is False


def test_syntax_gate_fails_on_parse_error_in_stderr(tmp_path, monkeypatch):
    cfg = _gate_cfg(tmp_path)
    monkeypatch.setattr(
        gates.subprocess, "run",
        lambda *a, **k: _fake_proc(rc=0, stdout="",
                                   stderr="Parse Error: unexpected token"))
    ok, detail = gates.syntax_gate(cfg)
    assert ok is False
    assert "Parse Error" in detail


# --------------------------------------------------------------------------- #
# smoke_gate                                                                    #
# --------------------------------------------------------------------------- #

def test_smoke_gate_passes_with_one_episode(tmp_path, monkeypatch):
    cfg = _gate_cfg(tmp_path)
    captured = {"min_episodes": None, "max_eval_steps": None, "seed": None}

    def _fake_run_one_seed(c, *, seed, artifact_dir,
                           min_episodes=None, max_eval_steps=None):
        captured["seed"] = seed
        captured["min_episodes"] = min_episodes
        captured["max_eval_steps"] = max_eval_steps
        return evaluation.RunResult(
            seed=seed, telemetry_path="x", run_id="r",
            report={"summary": {"n_episodes": 1}}, score=0.0, provenance={})

    monkeypatch.setattr(optimize, "run_one_seed", _fake_run_one_seed)
    ok, detail = gates.smoke_gate(cfg)
    assert ok is True
    assert captured["min_episodes"] == 1
    assert captured["max_eval_steps"] == cfg.smoke_max_steps
    assert captured["seed"] == cfg.eval_seeds[0]


def test_smoke_gate_fails_when_no_episode(tmp_path, monkeypatch):
    cfg = _gate_cfg(tmp_path)

    def _fake_run_one_seed(c, *, seed, artifact_dir,
                           min_episodes=None, max_eval_steps=None):
        raise RuntimeError("有效局数不足: summary.n_episodes=0 < min_episodes=1")

    monkeypatch.setattr(optimize, "run_one_seed", _fake_run_one_seed)
    ok, detail = gates.smoke_gate(cfg)
    assert ok is False
    assert detail


# --------------------------------------------------------------------------- #
# tscn_sanity(纯 Python .tscn 健全性,补 --import 对 .tscn 的失效)               #
# --------------------------------------------------------------------------- #

_GOOD_TSCN = (
    '[gd_scene load_steps=2 format=3]\n\n'
    '[sub_resource type="RectangleShape2D" id="plat_shape"]\n'
    'size = Vector2(120, 24)\n\n'
    '[node name="MidPlatform" type="StaticBody2D" parent="."]\n'
    'position = Vector2(600, 40)\n\n'
    '[node name="MidPlatShape" type="CollisionShape2D" parent="MidPlatform"]\n'
    'shape = SubResource("plat_shape")\n'
)


def test_tscn_sanity_passes_on_valid(tmp_path):
    p = tmp_path / "ok.tscn"
    p.write_text(_GOOD_TSCN, encoding="utf-8")
    ok, detail = gates.tscn_sanity([str(p)])
    assert ok is True, detail


def test_tscn_sanity_catches_unbalanced_paren(tmp_path):
    """缺右括号的 Vector2 —— Godot --import 静默放过(实测 rc=0),纯检查必须抓。"""
    p = tmp_path / "bad.tscn"
    p.write_text(_GOOD_TSCN.replace("Vector2(600, 40)", "Vector2(600, 40"),
                 encoding="utf-8")
    ok, detail = gates.tscn_sanity([str(p)])
    assert ok is False
    assert "括号" in detail or "paren" in detail.lower()


def test_tscn_sanity_catches_dangling_subresource(tmp_path):
    """SubResource 引用了未定义的 id —— --import 也静默放过,纯检查必须抓。"""
    p = tmp_path / "bad2.tscn"
    p.write_text(_GOOD_TSCN.replace('SubResource("plat_shape")',
                                    'SubResource("nope_xyz")'),
                 encoding="utf-8")
    ok, detail = gates.tscn_sanity([str(p)])
    assert ok is False
    assert "nope_xyz" in detail


def test_tscn_sanity_skips_missing_and_non_tscn(tmp_path):
    """不存在的文件 / 非 .tscn 路径直接跳过,返回 ok(不误伤注入式单测)。"""
    ok, _ = gates.tscn_sanity([str(tmp_path / "ghost.tscn"),
                               str(tmp_path / "x.json")])
    assert ok is True


def test_no_circular_import():
    """gates 与 optimize 可共存导入,无循环 import 崩溃(critic M3)。"""
    code = ("import sys; sys.path.insert(0, %r); import gates, optimize"
            % HARNESS)
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
