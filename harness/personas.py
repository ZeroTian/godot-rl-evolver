"""harness/personas.py — Procedural personas(主观体验层 S2)。

persona = 一份 reward-shaping 权重 profile（`personas/<name>.json`，**冻结仪器面板**，
优化闭环 protected 永不改）。每 persona 用其权重 `WARM_START` 训出一个**冻结策略**
（外部 MODEL，不入库）。本模块提供：

- load_persona / list_personas：纯文件解析 + 校验（标准库）。
- run_persona_panel：多 persona 试玩编排——对一关依次用每 persona 的策略评估。

关键现实（设计 §C1）：训练/推理 reward 不对称——推理期丢弃 reward，故 persona 差异
100% 来自**加载哪个冻结 MODEL**。run_persona_panel 只切换 cfg.model，不改 reward 字面值。
"""
from __future__ import annotations

import copy
import json
import os

# 权威 reward 键表（与 game_agent.gd 字面值一一对应，Task2/4 共用）。
# load_persona 要求 reward_weights **恰好** = 此集合（缺/多均 ValueError），
# 共享常量避免 .gd 与校验器分叉（plan m3）。
REWARD_KEYS = {
    "progress", "time_penalty", "damage", "kill", "combat_shape",
    "hurt_penalty", "gap_edge_jump", "gap_cross", "goal", "fall", "hp_fail",
}


def load_persona(path: str) -> dict:
    """读 `personas/<name>.json`，校验 name/reward_weights/model 三字段。

    reward_weights 必须**恰好** = REWARD_KEYS（缺键或含未知键均 ValueError）。
    返回解析后的 dict。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("persona 配置必须是 JSON 对象: %s" % path)
    for field in ("name", "reward_weights", "model"):
        if field not in data:
            raise ValueError("persona 缺少字段 %r: %s" % (field, path))
    if not isinstance(data["name"], str) or not data["name"]:
        raise ValueError("persona name 必须是非空字符串: %s" % path)
    if not isinstance(data["model"], str):
        raise ValueError("persona model 必须是字符串路径: %s" % path)
    weights = data["reward_weights"]
    if not isinstance(weights, dict):
        raise ValueError("persona reward_weights 必须是对象: %s" % path)
    keys = set(weights)
    if keys != REWARD_KEYS:
        missing = REWARD_KEYS - keys
        unknown = keys - REWARD_KEYS
        raise ValueError(
            "persona reward_weights 键集必须恰好 = REWARD_KEYS"
            "（缺 %s，多 %s）: %s"
            % (sorted(missing), sorted(unknown), path))
    return data


def list_personas(personas_dir: str) -> list[dict]:
    """加载目录下全部 `*.json` persona 配置，按 name 排序返回。"""
    out = []
    for fname in os.listdir(personas_dir):
        if not fname.endswith(".json"):
            continue
        out.append(load_persona(os.path.join(personas_dir, fname)))
    out.sort(key=lambda p: p["name"])
    return out


def run_persona_panel(cfg, personas: list[dict], *,
                      panel_run_id: str) -> dict[str, "EvaluationResult"]:
    """对每个 persona 跑 evaluate_current，返回 {persona_name: EvaluationResult}。

    - 每 persona 用**其自己的 model**：浅拷贝 cfg → 覆盖 cfg2.model=persona['model']、
      cfg2.opt_run_id=panel_run_id（panel 自带 run_id，避免 OPT_RUN_ID 为空时 artifact
      目录撞 FileExistsError，plan m2）；scene 仍读 cfg.scene（不另传，plan m1）。
    - artifact 路径由 evaluate_current 按 opt_run_id + point_id 隔离到
      `<root>/runs/<panel_run_id>/persona_<name>/`。
    - 为避免循环 import，函数内局部 import optimize（且经模块属性调用，使测试 monkeypatch
      optimize.evaluate_current 生效）。
    - 一个 persona 评估失败（evaluate_current 抛）→ 记录并继续其余（不整体中止），返回里缺该 persona。
    """
    import optimize  # 局部 import 破循环依赖；属性访问让 monkeypatch 生效

    results: dict[str, "EvaluationResult"] = {}
    for persona in personas:
        name = persona["name"]
        cfg2 = copy.copy(cfg)          # 浅拷贝，避免污染调用方 cfg
        cfg2.model = persona["model"]
        cfg2.opt_run_id = panel_run_id
        try:
            results[name] = optimize.evaluate_current(
                cfg2, point_id="persona_%s" % name)
        except Exception as e:         # noqa: BLE001 单 persona 失败不拖垮整盘
            print("persona %r 评估失败，跳过: %s" % (name, e))
            continue
    return results
