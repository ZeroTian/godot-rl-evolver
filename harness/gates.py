"""harness/gates.py — 结构改动的语法 gate 与 smoke gate(优化闭环阶段 2)。

structural patch 应用后,接受前要过两道廉价 gate(spec §6 / 阶段2 计划 Task 3):

  ① syntax_gate(cfg):Godot `--headless --path . --import`,加载 autoload 编译全部脚本
     并退出。rc==0 且 stdout/stderr 无 'SCRIPT ERROR'/'Parse Error'/'Failed to load script'
     即通过。坏 .tscn / 语法错会被这里拦下,免得拿坏场景去跑昂贵评估。

  ② smoke_gate(cfg):以 EVAL_SEEDS[0]、SMOKE_MAX_STEPS、min_episodes=1 跑一次 run_infer,
     要求恰好 1 个 run_*.jsonl 且 ≥1 局即通过。确认改后的场景真能起、能出 episode。
     复用 optimize.run_one_seed 的覆盖参数(critic C3)拿到廉价 ≥1 局评估。

为避免与 optimize 顶层循环 import(optimize 在 structural 分支局部 import gates),
smoke_gate **函数局部** import optimize(critic M3)。两道 gate 返回 (passed, detail),
detail 供 memory.reason。

Godot 可执行路径走 GODOT 环境变量(默认 /mnt/d/Godot/Godot_console.exe,同 run_*.sh)。
"""
from __future__ import annotations

import os
import re
import subprocess

# 语法 gate 视为失败的标记(出现在 stdout/stderr 任一即判失败)
_SYNTAX_ERROR_MARKERS = ("SCRIPT ERROR", "Parse Error", "Failed to load script")

_DEFAULT_GODOT = "/mnt/d/Godot/Godot_console.exe"

# 含 ASCII 括号、需检查平衡的构造器(position/size/scale 等数值 patch 最常踩)
_CTOR_TOKENS = ("Vector2(", "Vector2i(", "Vector3(", "Vector3i(",
                "Rect2(", "Rect2i(", "Color(", "Transform2D(")
_DEF_SUB = re.compile(r'\[sub_resource\b[^\]]*\bid="([^"]+)"')
_DEF_EXT = re.compile(r'\[ext_resource\b[^\]]*\bid="([^"]+)"')
_REF_SUB = re.compile(r'SubResource\("([^"]+)"\)')
_REF_EXT = re.compile(r'ExtResource\("([^"]+)"\)')


def _godot_bin() -> str:
    return os.environ.get("GODOT", _DEFAULT_GODOT)


def tscn_sanity(paths, repo_root: str = ".") -> tuple[bool, str]:
    """纯 Python 的 .tscn 健全性检查,补 Godot `--import` 对 .tscn 的失效。

    实测:`--import` 对缺括号的 Vector2、悬空 SubResource 引用、错误 node type 一律
    rc=0 且无标记静默放过 —— 坏 patch 会拖到 smoke gate 才暴露,甚至被 Godot 静默
    用默认值吞掉(看似过 gate 实则没改游戏 → 测量隐患)。本检查在语法 gate 前廉价拦下
    两类最常见的 patch 破坏:
      ① 资源引用完整性:每个 SubResource("id")/ExtResource("id") 必须有对应定义;
      ② 构造器括号平衡:含 Vector2(/Rect2(/Color( 等的行 ASCII '(' 与 ')' 数相等。

    paths:repo-relative 或绝对路径列表;非 .tscn 或不存在的路径跳过(不误伤注入式单测)。
    返回 (passed, detail)。
    """
    for p in paths:
        if not str(p).endswith(".tscn"):
            continue
        full = p if os.path.isabs(p) else os.path.join(repo_root, p)
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8") as f:
            text = f.read()

        # ① 资源引用完整性
        defined = set(_DEF_SUB.findall(text)) | set(_DEF_EXT.findall(text))
        refs = set(_REF_SUB.findall(text)) | set(_REF_EXT.findall(text))
        missing = sorted(refs - defined)
        if missing:
            return False, "%s: 未定义的资源引用 %s" % (p, missing)

        # ② 构造器括号平衡
        for i, line in enumerate(text.splitlines(), 1):
            if any(tok in line for tok in _CTOR_TOKENS):
                if line.count("(") != line.count(")"):
                    return False, "%s:%d 括号不平衡: %s" % (p, i, line.strip())

    return True, "tscn sanity ok"


def syntax_gate(cfg) -> tuple[bool, str]:
    """Godot `--headless --path . --import` 语法 gate。

    rc==0 且 stdout/stderr 无错误标记即通过。返回 (passed, detail)。
    超时或 OSError 视为不过。cwd=cfg.proj(WSL 下 Godot 只认从项目目录起的 --path .)。
    """
    try:
        proc = subprocess.run(
            [_godot_bin(), "--headless", "--path", ".", "--import"],
            cwd=cfg.proj,
            capture_output=True,
            text=True,
            timeout=cfg.smoke_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "syntax gate 超时(>%ds)" % cfg.smoke_timeout_seconds
    except OSError as e:
        return False, "syntax gate 启动 Godot 失败: %s" % e

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    hits = [m for m in _SYNTAX_ERROR_MARKERS if m in combined]
    if proc.returncode != 0:
        return False, ("syntax gate rc=%d; %s"
                       % (proc.returncode,
                          (combined.strip() or "无输出")[-500:]))
    if hits:
        # detail 含命中的标记 + 相关行,供 memory.reason
        return False, ("syntax gate 命中 %s; %s"
                       % (",".join(hits), combined.strip()[-500:]))
    return True, "syntax gate ok"


def smoke_gate(cfg) -> tuple[bool, str]:
    """以 EVAL_SEEDS[0]、SMOKE_MAX_STEPS、min_episodes=1 跑一次廉价试玩。

    成功(拿到 ≥1 局的 RunResult)→ (True, detail);任何异常(0 局/起不来/超时)→ (False, detail)。
    为打破循环 import(optimize 局部 import gates),此处**函数局部** import optimize。
    artifact 放在 <artifact_root>/runs/<opt_run_id>/smoke/seed_<s>,与正式评估隔离。
    """
    import optimize  # 函数局部 import,打破 optimize↔gates 顶层循环(critic M3)

    seed = cfg.eval_seeds[0]
    artifact_dir = os.path.join(
        cfg.artifact_root, "runs", cfg.opt_run_id, "smoke", "seed_%d" % seed)
    try:
        rr = optimize.run_one_seed(
            cfg, seed=seed, artifact_dir=artifact_dir,
            min_episodes=1, max_eval_steps=cfg.smoke_max_steps)
    except Exception as e:  # noqa: BLE001 — 任何评估失败都判 smoke 不过
        return False, "smoke gate 失败: %s" % e
    n_ep = rr.report.get("summary", {}).get("n_episodes", 0)
    return True, "smoke gate ok (n_episodes=%s)" % n_ep
