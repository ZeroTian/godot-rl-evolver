"""harness/mutate.py — 改动应用 + protected-path 检查 + git 快照/回滚。

纯函数（单测覆盖）:
  allowed(plan, protected_globs) -> bool
  apply_tunable(path, key, value)    写回 tunables.json 并 clamp 到 range

git 辅助（不单测，函数齐全即可）:
  snapshot() -> str           返回当前 HEAD 的 sha，用于回滚锚点
  rollback(sha)               git reset --hard <sha>
  commit(msg)                 git add -A && git commit -m <msg>

protected 默认:
  harness/**  .git/**  tests/**  docs/**
  tunables.json 仅允许改 value 字段（range/type/desc/files 受保护）
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from typing import Any

# --------------------------------------------------------------------------- #
# protected 路径匹配                                                            #
# --------------------------------------------------------------------------- #

# tunables.json 中仅允许修改的字段
_TUNABLE_ALLOWED_FIELDS = {"value"}
# 所有受保护字段（range/type/desc/files → 若 plan 指定这些字段则拒绝）
_TUNABLE_PROTECTED_FIELDS = {"range", "type", "desc", "files"}


def _matches_any_glob(path: str, globs: list[str]) -> bool:
    """路径 path 是否匹配 globs 中任意一个 glob 模式。"""
    for pattern in globs:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def allowed(plan: dict, protected_globs: list[str]) -> bool:
    """检查 plan 是否被允许执行。

    Args:
        plan: 改动计划 dict，字段参见 spec §5.2。
              关键字段:
                "files"  — 目标文件路径列表（可能为空，如纯 tunable_search）
                "field"  — 若改的是 tunables.json 的某字段，在此声明（可选）
        protected_globs: 禁止修改的 glob 列表，如 ["harness/**", ".git/**", ...]

    Returns:
        True 表示允许；False 表示拒绝。
    """
    # 检查 tunables.json 的字段保护
    # plan["field"] 存在且不在允许集内 → 拒绝
    field = plan.get("field")
    if field is not None and field in _TUNABLE_PROTECTED_FIELDS:
        return False

    # 检查目标文件路径不在 protected_globs 内
    files: list[str] = plan.get("files") or []
    for f in files:
        if _matches_any_glob(f, protected_globs):
            return False

    return True


# --------------------------------------------------------------------------- #
# apply_tunable                                                                #
# --------------------------------------------------------------------------- #

def apply_tunable(path: str, key: str, value: Any) -> None:
    """将 tunables.json 中 key 的 value 字段改为 value，并 clamp 到其 range。

    Args:
        path:  tunables.json 的文件路径。
        key:   要修改的参数名。
        value: 新值（会被 clamp 到 range）。

    Raises:
        KeyError: key 不存在于 params 中。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    params = data["params"]
    if key not in params:
        raise KeyError(f"参数 '{key}' 不在 tunables.json 的 params 中")

    param = params[key]
    lo, hi = param["range"]
    clamped = max(lo, min(hi, value))
    param["value"] = clamped

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# git 辅助（subprocess，不单测）                                                #
# --------------------------------------------------------------------------- #

def _run_git(*args: str, cwd: str | None = None) -> str:
    """运行 git 子命令，返回 stdout。失败时抛 RuntimeError。"""
    cmd = ["git", *args]
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败 (rc={result.returncode}):\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def snapshot(repo_root: str = ".") -> str:
    """返回当前 HEAD commit sha，作为 rollback 锚点。"""
    return _run_git("rev-parse", "HEAD", cwd=repo_root)


def rollback(sha: str, repo_root: str = ".") -> None:
    """回滚到指定 sha（git reset --hard）。"""
    _run_git("reset", "--hard", sha, cwd=repo_root)


def commit(msg: str, repo_root: str = ".") -> None:
    """暂存所有改动并提交。"""
    _run_git("add", "-A", cwd=repo_root)
    _run_git("commit", "-m", msg, cwd=repo_root)
