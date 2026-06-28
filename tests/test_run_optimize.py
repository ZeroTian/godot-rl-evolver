"""tests/test_run_optimize.py — harness/run_optimize.sh 入口契约测试（Task 8）。

测试策略：在临时目录里建 fake git / fake python 命令拦截真实调用，
通过检查脚本行为（退出码、输出、环境变量传递）验证以下四条契约：
  1. 脏工作树（git status --porcelain 非空）在建/切分支之前就退出非 0。
  2. MODEL 文件不存在时退出非 0。
  3. 新增配置变量（EVAL_SEEDS/EVAL_EPISODES/MAX_EVAL_STEPS/
     EVAL_TIMEOUT_SECONDS/MIN_IMPROVEMENT/ARTIFACT_ROOT）全部被 export
     透传给 optimize.py。
  4. 默认 MEMORY_PATH 位于 .artifacts/opt/memory/ 而非 testbed tracked 目录。
  5. PROJ 默认值为 REPO_ROOT/testbed_platformer（不再依赖外部传入）。
  6. Gate 0（脏树检查）在创建/切换分支之前执行。
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parent.parent / "harness" / "run_optimize.sh"


def _write_exec(path: Path, content: str) -> None:
    """写可执行 shell 脚本（fake 命令桩）。"""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _base_env(tmp: Path, model_path: Path | None = None,
              create_model: bool = True) -> dict:
    """构造最小化运行环境，隔离真实 git/python。

    fake_bin/ 里放桩命令，优先于系统 PATH。
    create_model=False 时不创建模型文件，用于测试 Gate 1 缺模型场景。
    """
    fake_bin = tmp / "fake_bin"
    fake_bin.mkdir(exist_ok=True)

    # fake git：默认干净工作树；git rev-parse 成功；git checkout -b 静默
    _write_exec(fake_bin / "git", textwrap.dedent("""\
        #!/bin/bash
        # 记录调用参数到日志
        echo "$@" >> "$FAKE_GIT_LOG"
        case "$*" in
          "status --porcelain")
            echo -n ""  # 干净工作树：无输出
            exit 0 ;;
          "rev-parse --git-dir")
            echo ".git"
            exit 0 ;;
          checkout*)
            exit 0 ;;
          *)
            exit 0 ;;
        esac
    """))

    # fake python：把自身环境变量写到 $FAKE_PY_ENV_LOG 然后退出 0
    _write_exec(fake_bin / "python", textwrap.dedent("""\
        #!/bin/bash
        env >> "$FAKE_PY_ENV_LOG"
        exit 0
    """))

    # fake sha256sum（MODEL SHA-256 打印用）
    _write_exec(fake_bin / "sha256sum", textwrap.dedent("""\
        #!/bin/bash
        echo "aabbccdd1234  $1"
        exit 0
    """))

    model = model_path or (tmp / "ppo_game.zip")
    if create_model:
        model.write_bytes(b"dummy")

    git_log = tmp / "git_calls.log"
    py_env_log = tmp / "py_env.log"

    repo_root = tmp / "repo"
    repo_root.mkdir()
    # 建 testbed_platformer/rl/tunables.json 让脚本能找到
    tunables_dir = repo_root / "testbed_platformer" / "rl"
    tunables_dir.mkdir(parents=True)
    (tunables_dir / "tunables.json").write_text('{"version":1,"params":{}}')

    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "REPO_ROOT": str(repo_root),
        "MODEL": str(model),
        "SCENE": "res://rl/train_map.tscn",
        "ANTHROPIC_API_KEY": "sk-test-key",
        "VENV": str(tmp / "venv"),
        # 指向 fake python，让 activate 不出错
        "HOME": str(tmp),
        "FAKE_GIT_LOG": str(git_log),
        "FAKE_PY_ENV_LOG": str(py_env_log),
        # 阻止脚本真正 source venv（venv 不存在则报错）
        "BASH_ENV": "",
    }
    return env


def _run_script(tmp: Path, extra_env: dict | None = None,
                model_path: Path | None = None,
                create_model: bool = True) -> subprocess.CompletedProcess:
    """运行 run_optimize.sh，返回 CompletedProcess。"""
    env = _base_env(tmp, model_path, create_model=create_model)
    if extra_env:
        env.update(extra_env)

    # 让 "source venv/bin/activate" 不报错：建个 fake activate
    venv_bin = Path(env["VENV"]) / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    activate = venv_bin / "activate"
    if not activate.exists():
        activate.write_text("# fake activate\n")

    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# 契约 1：脏工作树在建分支前退出
# ---------------------------------------------------------------------------

def test_dirty_tree_exits_before_branch(tmp_path):
    """脏工作树（git status --porcelain 非空）必须在建/切分支前退出非 0。"""
    fake_bin = tmp_path / "fake_bin"
    # 先用 _base_env 建标准目录
    env = _base_env(tmp_path)
    fake_bin = tmp_path / "fake_bin"

    # 覆写 fake git：status --porcelain 返回非空（有未提交改动）
    _write_exec(fake_bin / "git", textwrap.dedent("""\
        #!/bin/bash
        echo "$@" >> "$FAKE_GIT_LOG"
        case "$*" in
          "status --porcelain")
            echo " M harness/run_optimize.sh"  # 脏树
            exit 0 ;;
          "rev-parse --git-dir")
            echo ".git"
            exit 0 ;;
          checkout*)
            # 如果到达这里，说明脏树检查没有阻止分支创建——测试应失败
            echo "BRANCH_CREATED" >> "$FAKE_GIT_LOG"
            exit 0 ;;
          *)
            exit 0 ;;
        esac
    """))

    venv_bin = Path(env["VENV"]) / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "activate").write_text("# fake activate\n")

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    # 必须非 0 退出
    assert result.returncode != 0, (
        f"脏工作树应退出非 0，实际 rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # git checkout 不应被调用
    git_log = tmp_path / "git_calls.log"
    if git_log.exists():
        calls = git_log.read_text()
        assert "checkout" not in calls, (
            f"脏树检查失败：在退出前仍执行了 git checkout:\n{calls}"
        )


# ---------------------------------------------------------------------------
# 契约 2：MODEL 文件不存在时退出
# ---------------------------------------------------------------------------

def test_missing_model_exits_nonzero(tmp_path):
    """MODEL 指向不存在的文件时脚本必须退出非 0。"""
    nonexistent = tmp_path / "no_such_model.zip"
    # create_model=False：不创建文件，让脚本 Gate 1 检查到文件缺失
    result = _run_script(tmp_path, model_path=nonexistent, create_model=False)

    assert result.returncode != 0, (
        f"缺 MODEL 文件应退出非 0，实际 rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# 契约 3：新变量全部透传给 optimize.py
# ---------------------------------------------------------------------------

def test_new_vars_exported_to_optimize(tmp_path):
    """EVAL_SEEDS/EVAL_EPISODES/MAX_EVAL_STEPS/EVAL_TIMEOUT_SECONDS/
    MIN_IMPROVEMENT/ARTIFACT_ROOT 必须全部 export 给 optimize.py（python）。"""
    result = _run_script(tmp_path)

    py_env_log = tmp_path / "py_env.log"
    assert py_env_log.exists(), (
        f"fake python 未被调用，env log 不存在\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    env_text = py_env_log.read_text()

    required_vars = [
        "EVAL_SEEDS",
        "EVAL_EPISODES",
        "MAX_EVAL_STEPS",
        "EVAL_TIMEOUT_SECONDS",
        "MIN_IMPROVEMENT",
        "ARTIFACT_ROOT",
    ]
    for var in required_vars:
        assert any(line.startswith(f"{var}=") for line in env_text.splitlines()), (
            f"变量 {var} 未透传给 optimize.py\n完整 env:\n{env_text}"
        )


# ---------------------------------------------------------------------------
# 契约 4：默认 MEMORY_PATH 位于 .artifacts/opt/memory/ 而非 testbed tracked 目录
# ---------------------------------------------------------------------------

def test_default_memory_path_in_artifacts(tmp_path):
    """默认 MEMORY_PATH 必须指向 .artifacts/opt/memory/ 目录，
    不得指向 testbed_platformer/rl/ 下（tracked 目录）。
    """
    result = _run_script(tmp_path)

    py_env_log = tmp_path / "py_env.log"
    if not py_env_log.exists():
        pytest.skip("fake python 未被调用，跳过此项")

    env_text = py_env_log.read_text()

    # 找到 MEMORY_PATH 值
    memory_line = next(
        (l for l in env_text.splitlines() if l.startswith("MEMORY_PATH=")),
        None
    )
    assert memory_line is not None, (
        f"MEMORY_PATH 未传给 optimize.py\n完整 env:\n{env_text}"
    )
    memory_val = memory_line.split("=", 1)[1]

    assert ".artifacts" in memory_val, (
        f"MEMORY_PATH 应在 .artifacts/ 下，实际: {memory_val}"
    )
    # 不得是 testbed tracked 目录（testbed_platformer/rl/opt_memory.json）
    assert "testbed_platformer/rl" not in memory_val, (
        f"MEMORY_PATH 不得指向 testbed tracked 目录，实际: {memory_val}"
    )


# ---------------------------------------------------------------------------
# 契约 5：PROJ 默认为 REPO_ROOT/testbed_platformer
# ---------------------------------------------------------------------------

def test_default_proj_is_testbed(tmp_path):
    """未设置 PROJ 时，脚本应默认 PROJ=$REPO_ROOT/testbed_platformer。"""
    # 不在 extra_env 里传 PROJ
    result = _run_script(tmp_path)

    py_env_log = tmp_path / "py_env.log"
    if not py_env_log.exists():
        pytest.skip("fake python 未被调用，跳过此项")

    env_text = py_env_log.read_text()
    proj_line = next(
        (l for l in env_text.splitlines() if l.startswith("PROJ=")),
        None
    )
    assert proj_line is not None, f"PROJ 未传给 optimize.py\n{env_text}"
    proj_val = proj_line.split("=", 1)[1]

    assert proj_val.endswith("testbed_platformer"), (
        f"默认 PROJ 应以 testbed_platformer 结尾，实际: {proj_val}"
    )


# ---------------------------------------------------------------------------
# 契约 7（阶段2）：smoke 预算变量 + STAGE 透传 + PROTECTED 含测量 glob
# ---------------------------------------------------------------------------

def test_stage2_smoke_and_protected_vars_exported(tmp_path):
    """SMOKE_MAX_STEPS/SMOKE_TIMEOUT_SECONDS 必须 export 给 optimize.py；
    STAGE 不写死(可由环境覆盖)；默认 PROTECTED_PATHS 含测量装置 glob。"""
    result = _run_script(tmp_path, extra_env={"STAGE": "2"})

    py_env_log = tmp_path / "py_env.log"
    assert py_env_log.exists(), (
        f"fake python 未被调用\nstdout: {result.stdout}\nstderr: {result.stderr}")
    env_text = py_env_log.read_text()
    lines = env_text.splitlines()

    for var in ["SMOKE_MAX_STEPS", "SMOKE_TIMEOUT_SECONDS"]:
        assert any(l.startswith(f"{var}=") for l in lines), (
            f"变量 {var} 未透传给 optimize.py\n完整 env:\n{env_text}")

    # STAGE 透传且尊重环境覆盖(=2)
    stage_line = next((l for l in lines if l.startswith("STAGE=")), None)
    assert stage_line == "STAGE=2", f"STAGE 应透传为 2，实际: {stage_line}"

    # 默认 PROTECTED_PATHS 点名测量装置文件
    prot_line = next((l for l in lines if l.startswith("PROTECTED_PATHS=")), None)
    assert prot_line is not None, f"PROTECTED_PATHS 未透传\n{env_text}"
    assert "*/rl/game_agent.gd" in prot_line, (
        f"默认 PROTECTED_PATHS 应含测量装置 glob，实际: {prot_line}")


# ---------------------------------------------------------------------------
# 契约 6：Gate 0（脏树）在 git checkout 前执行（顺序保证）
# ---------------------------------------------------------------------------

def test_gate0_before_checkout_order(tmp_path):
    """脏树检查必须在 git checkout -b 前执行——顺序验证。

    用有序日志：记录 'STATUS_CHECK' 和 'CHECKOUT' 时间戳顺序。
    """
    env = _base_env(tmp_path)
    fake_bin = tmp_path / "fake_bin"

    order_log = tmp_path / "order.log"

    _write_exec(fake_bin / "git", textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> "$FAKE_GIT_LOG"
        case "$*" in
          "status --porcelain")
            echo "STATUS_CHECK" >> "{order_log}"
            echo " M dirty"  # 脏树，触发退出
            exit 0 ;;
          "rev-parse --git-dir")
            echo ".git"
            exit 0 ;;
          checkout*)
            echo "CHECKOUT" >> "{order_log}"
            exit 0 ;;
          *)
            exit 0 ;;
        esac
    """))

    venv_bin = Path(env["VENV"]) / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "activate").write_text("# fake activate\n")

    subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert order_log.exists(), "order log 未被写入"
    lines = order_log.read_text().strip().splitlines()

    # 必须出现 STATUS_CHECK，且不能出现 CHECKOUT
    assert "STATUS_CHECK" in lines, "git status --porcelain 未被调用"
    assert "CHECKOUT" not in lines, (
        "脏树退出前不应执行 git checkout；实际调用顺序:\n" + "\n".join(lines)
    )
