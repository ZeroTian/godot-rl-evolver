"""
harness/memory.py — 优化闭环记忆读写

Schema (spec §5.3):
{
  "scene": "res://rl/train_map.tscn",
  "rounds": [
    {"round": 3, "target_issue": "difficulty_too_hard", "change_type": "tunable_search",
     "summary": "gap_width 120→96", "score_before": 2.8, "score_after": 1.5,
     "accepted": true, "reason": "completion 0.1→0.42"},
    ...
  ]
}

记忆文件跨 run 累积（append rounds），供 llm_propose 读取失败教训。
"""
from __future__ import annotations

import json
import os
from typing import Any


def load(path: str) -> dict:
    """读取 memory.json，文件不存在则返回空结构。"""
    if not os.path.exists(path):
        return {"scene": "", "rounds": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def add_round(path: str, scene: str, record: dict[str, Any]) -> None:
    """
    向 memory.json 追加一条轮次记录（跨 run 累积）。

    - 若文件不存在则创建。
    - rounds 只 append，不覆盖（支持跨 run 累积）。
    - scene 变更时重置 rounds（每个 scene 独立记忆，防止跨场景混淆）。
    """
    data = load(path)
    if data["scene"] != scene:
        # 切换场景：旧 scene 的记录不再保留，开始新 scene 的记忆
        data = {"scene": scene, "rounds": []}
    data["rounds"].append(record)
    _write(path, data)


def get_rounds_for_scene(path: str, scene: str) -> list[dict]:
    """
    返回指定 scene 下的所有轮次记录。

    注：memory.json 是单 scene 文件；若当前存储 scene 与查询 scene 不符，
    则认为没有匹配记录（返回空列表）。
    """
    data = load(path)
    if data["scene"] != scene:
        return []
    return list(data["rounds"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write(path: str, data: dict) -> None:
    """原子写入：先写临时文件，再重命名，防止写到一半崩溃。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
