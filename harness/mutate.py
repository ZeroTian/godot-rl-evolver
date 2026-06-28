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


def _res_to_repo(file: str, proj_rel: str) -> str:
    """把 plan 里的 res:// 路径映射成 repo-relative 路径。

    去掉 res:// 前缀后拼到 proj_rel 之下。proj_rel 为空时按原样路径返回
    （仅去前缀），供向后兼容。返回值使用 posix 风格的分隔符。
    """
    rel = file[len("res://"):] if file.startswith("res://") else file
    if proj_rel:
        return os.path.normpath(os.path.join(proj_rel, rel)).replace(os.sep, "/")
    return os.path.normpath(rel).replace(os.sep, "/")


def allowed(plan: dict, protected_globs: list[str], *, proj_rel: str = "") -> bool:
    """检查 plan 是否被允许执行。

    Args:
        plan: 改动计划 dict，字段参见 spec §5.2。
              关键字段:
                "files"   — 目标文件路径列表（可能为空，如纯 tunable_search）
                "field"   — 若改的是 tunables.json 的某字段，在此声明（可选）
                "patches" — 结构/逻辑 patch 列表，每条含 res:// 形式的 "file"（阶段 2）
        protected_globs: 禁止修改的 glob 列表，如 ["harness/**", ".git/**", ...]
        proj_rel: PROJ 相对 repo_root 的路径，用于把 patch 的 res:// 映射成
                  repo-relative 后再做 protected 匹配。缺省时按原样路径匹配。

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

    # 阶段 2（防御纵深第②层）：遍历 patches，把每条 file 经 res:// 映射后做
    # protected glob 匹配，命中即拒绝。
    for patch in plan.get("patches") or []:
        repo_rel = _res_to_repo(patch.get("file", ""), proj_rel)
        if _matches_any_glob(repo_rel, protected_globs):
            return False

    return True


def target_files(plan: dict, *, proj_rel: str) -> list[str]:
    """从 plan 解析本轮白名单（repo-relative 路径列表）。

    Args:
        plan:     改动计划 dict。
        proj_rel: PROJ 相对 repo_root 的路径，例 'testbed_platformer'。

    Returns:
        repo-relative 路径列表（去重保序）。
        - tunable_search：本函数不解析 tunables（路径由调用方另行传入），返回 []。
        - structural/logic：遍历 plan["patches"]，每条 file 的 res:// 经 proj_rel
          映射成 repo-relative。

    Raises:
        ValueError: patch 路径含 '..' 段，或映射后不在 proj_rel 目录内（critic M5）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for patch in plan.get("patches") or []:
        file = patch.get("file", "")
        rel = file[len("res://"):] if file.startswith("res://") else file
        # 拒绝任何含 '..' 段的路径（防穿越）
        if ".." in rel.replace(os.sep, "/").split("/"):
            raise ValueError(f"patch 路径含 '..' 段，拒绝: {file!r}")
        repo_rel = _res_to_repo(file, proj_rel)
        # 映射后必须仍在 proj_rel 目录内
        if proj_rel:
            prefix = proj_rel.replace(os.sep, "/").rstrip("/") + "/"
            if not (repo_rel == proj_rel.replace(os.sep, "/") or repo_rel.startswith(prefix)):
                raise ValueError(f"patch 路径越出项目目录: {file!r} → {repo_rel!r}")
        if repo_rel not in seen:
            seen.add(repo_rel)
            out.append(repo_rel)
    return out


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
# apply_patch（anchor 精确文本替换，阶段 2）                                     #
# --------------------------------------------------------------------------- #

def apply_patch(path: str, anchor: str, new: str, repo_root: str = ".",
                protected_globs: list[str] | None = None) -> None:
    """对 path 做 anchor 精确文本替换。

    anchor 必须在目标文件中恰好出现一次，替换为 new；原子写。

    Args:
        path:            目标文件路径（repo-relative 或绝对，须在 repo_root 内）。
        anchor:          要被替换的精确文本（含节点身份上下文的多行块）。
        new:             替换后的文本。
        repo_root:       git 仓根路径。
        protected_globs: 若给定且目标 repo-relative 路径命中任一 glob，拒写
                         （防御纵深第③层）。

    Raises:
        ValueError:        anchor 出现 0 次（未命中）或 >1 次（歧义）；路径越界；
                           命中 protected。
        FileNotFoundError: path 不存在。
    """
    repo_root_real = _resolve_repo_root(repo_root)
    # ① containment 校验（路径越界先于一切）
    real = _check_path_in_repo(path, repo_root_real)

    # ② 防御纵深第③层：protected glob 匹配（写前再拦一次）
    if protected_globs:
        repo_rel = os.path.relpath(real, repo_root_real).replace(os.sep, "/")
        if _matches_any_glob(repo_rel, protected_globs):
            raise ValueError(f"路径 {repo_rel!r} 命中 protected glob，拒绝 patch")

    # ③ 读文件（不存在 → FileNotFoundError）
    with open(real, encoding="utf-8") as f:
        text = f.read()

    count = text.count(anchor)
    if count == 0:
        raise ValueError(f"anchor 未命中: {anchor!r}")
    if count > 1:
        raise ValueError(f"anchor 歧义：出现 {count} 次: {anchor!r}")

    patched = text.replace(anchor, new, 1)

    # ④ 原子写（复用 tempfile.mkstemp + os.replace）
    dir_ = os.path.dirname(real)
    fd, tmp = tempfile.mkstemp(dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(patched)
        os.replace(tmp, real)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


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
