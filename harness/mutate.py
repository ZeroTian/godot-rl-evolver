"""harness/mutate.py — 改动应用 + protected-path 检查 + git 快照/回滚。

纯函数（单测覆盖）:
  allowed(plan, protected_globs) -> bool
  apply_tunable(path, key, value)    写回 tunables.json 并 clamp 到 range

git 辅助（集成测试覆盖）:
  snapshot(paths, repo_root=".") -> dict[str, bytes | None]
      只读取白名单文件内容；文件不存在时记录 None。
  rollback(snapshot_data, repo_root=".")
      原子写恢复白名单文件；若快照为 None 则删除该文件。
  commit(msg, paths, repo_root=".")
      逐路径 git add -- <path>，再提交；只暂存白名单，不暂存其他文件。

所有路径均先解析为 repo-relative，realpath 必须位于 repo_root 内，
否则抛 ValueError。

protected 默认:
  harness/**  .git/**  tests/**  docs/**
  tunables.json 仅允许改 value 字段（range/type/desc/files 受保护）
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import tempfile
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


def _resolve_repo_root(repo_root: str) -> str:
    """解析并返回仓根的规范绝对路径。"""
    return os.path.realpath(os.path.abspath(repo_root))


def _check_path_in_repo(path: str, repo_root_real: str) -> str:
    """验证 path 在仓根内，返回规范绝对路径。仓外则抛 ValueError。

    path 可以是 repo-relative 或绝对路径。
    对 symlink 使用 realpath，防止链接跳出仓库。
    """
    if os.path.isabs(path):
        abs_path = path
    else:
        abs_path = os.path.join(repo_root_real, path)

    # 先用 abspath 规范化（处理 ../），再用 realpath 解析 symlink
    real_path = os.path.realpath(os.path.abspath(abs_path))

    if not real_path.startswith(repo_root_real + os.sep) and real_path != repo_root_real:
        raise ValueError(
            f"路径 {path!r} 解析后位于仓库根目录之外: {real_path!r} 不在 {repo_root_real!r} 内"
        )
    return real_path


def snapshot(paths: list[str], repo_root: str = ".") -> dict[str, bytes | None]:
    """读取白名单文件内容，返回快照字典。

    Args:
        paths:     repo-relative 或绝对路径列表（白名单）。
        repo_root: git 仓根路径，默认当前目录。

    Returns:
        dict，key 为传入的路径字符串，value 为文件 bytes；
        文件不存在时 value 为 None。

    Raises:
        ValueError: 路径解析后位于 repo_root 之外。
    """
    repo_root_real = _resolve_repo_root(repo_root)
    result: dict[str, bytes | None] = {}
    for p in paths:
        real = _check_path_in_repo(p, repo_root_real)
        if os.path.exists(real):
            with open(real, "rb") as f:
                result[p] = f.read()
        else:
            result[p] = None
    return result


def rollback(snapshot_data: dict[str, bytes | None], repo_root: str = ".") -> None:
    """将白名单文件原子写回快照内容。

    Args:
        snapshot_data: snapshot() 返回的字典。
        repo_root:     git 仓根路径。

    行为:
        - value 为 bytes：将内容写回文件（原子写，先写临时文件再 rename）。
        - value 为 None：删除该文件（如果存在）。

    Raises:
        ValueError: 路径解析后位于 repo_root 之外。
    """
    repo_root_real = _resolve_repo_root(repo_root)
    for p, content in snapshot_data.items():
        real = _check_path_in_repo(p, repo_root_real)
        if content is None:
            # 快照时不存在 → 删除
            if os.path.exists(real):
                os.remove(real)
        else:
            # 原子写：先写同目录临时文件，再 rename
            dir_ = os.path.dirname(real)
            os.makedirs(dir_, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dir_)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                os.replace(tmp, real)
            except Exception:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise


def commit(msg: str, paths: list[str], repo_root: str = ".") -> None:
    """只暂存白名单路径并提交。

    Args:
        msg:       commit 消息。
        paths:     repo-relative 或绝对路径列表（白名单）。
        repo_root: git 仓根路径。

    Raises:
        ValueError: 路径解析后位于 repo_root 之外。
    """
    repo_root_real = _resolve_repo_root(repo_root)
    for p in paths:
        _check_path_in_repo(p, repo_root_real)
        # 转为 repo-relative 路径传给 git，确保 git 能定位
        if os.path.isabs(p):
            rel = os.path.relpath(p, repo_root_real)
        else:
            rel = p
        _run_git("add", "--", rel, cwd=repo_root)
    _run_git("commit", "-m", msg, cwd=repo_root)
