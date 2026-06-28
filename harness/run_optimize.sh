#!/bin/bash
# LLM 优化闭环入口（阶段 1：数值闭环 MVP）。
#
# 改动要点（Task 8）：
#   · REPO_ROOT 默认本仓（godot-rl-evolver），不再是外部 PROJ。
#   · PROJ 默认 $REPO_ROOT/testbed_platformer（本仓测试床）。
#   · Gate 0：先 git status --porcelain 检查脏树，非空立即退出（在建分支前）。
#   · Gate 1：要求 MODEL 文件存在，否则退出。
#   · 生成 OPT_RUN_ID，所有 artifact/memory/report 落入 .artifacts/opt/。
#   · 建/切 opt/auto-* 分支在本仓（REPO_ROOT），而非 PROJ 游戏仓。
#   · 透传全部配置变量（含 EVAL_SEEDS/EVAL_EPISODES/MAX_EVAL_STEPS/
#     EVAL_TIMEOUT_SECONDS/MIN_IMPROVEMENT/ARTIFACT_ROOT）给 optimize.py。
#
# 用法：
#   MODEL=~/.local/share/godot-rl-venv/ppo_game.zip \
#   ANTHROPIC_API_KEY=sk-... \
#   bash harness/run_optimize.sh
#
# 前提：① 本仓 testbed_platformer/ 已接入 rl/tunables.json + Tunables autoload；
#       ② 有训练好的策略（MODEL 文件存在）；
#       ③ ANTHROPIC_API_KEY 已设置。
set -u

# ─── 工具路径 ────────────────────────────────────────────────────────────────
: "${GODOT:=/mnt/d/Godot/Godot_console.exe}"
: "${VENV:=$HOME/.local/share/godot-rl-venv}"
: "${SPEEDUP:=8}"

# ─── 本仓 REPO_ROOT（Gate 0 + 分支操作的 git 仓）────────────────────────────
HARNESS="$(cd "$(dirname "$0")" && pwd)"
: "${REPO_ROOT:=$(cd "$HARNESS/.." && pwd)}"

# ─── PROJ 默认本仓测试床 ─────────────────────────────────────────────────────
: "${PROJ:=$REPO_ROOT/testbed_platformer}"
: "${SCENE:?需设置 SCENE=res://训练场景.tscn}"

# ─── LLM 后端 ───────────────────────────────────────────────────────────────
# 二选一即可:① ANTHROPIC_API_KEY(走 anthropic SDK)② 本机 claude CLI(复用 Claude
# Code 订阅认证,免 key)。LLM_BACKEND 可显式指定 anthropic|claude_cli,默认 auto。
if [ -z "${ANTHROPIC_API_KEY:-}" ] && ! command -v claude >/dev/null 2>&1; then
  echo "!! 无可用 LLM 后端:既未设 ANTHROPIC_API_KEY,也找不到 claude CLI。" >&2
  echo "   二选一:export ANTHROPIC_API_KEY=...  或  安装 Claude Code CLI(claude)。" >&2
  exit 1
fi

# ─── Gate 0：脏工作树检查（必须在建分支前）──────────────────────────────────
if ! ( cd "$REPO_ROOT" && git rev-parse --git-dir >/dev/null 2>&1 ); then
  echo "!! $REPO_ROOT 不是 git 仓 —— 闭环需 git 做快照/回滚，退出。" >&2
  exit 1
fi

DIRTY="$(cd "$REPO_ROOT" && git status --porcelain 2>/dev/null)"
if [ -n "$DIRTY" ]; then
  echo "!! 工作树有未提交改动（Gate 0 失败）——请先 commit 或 stash 后再运行优化闭环。" >&2
  echo "   脏文件：" >&2
  echo "$DIRTY" | head -10 >&2
  exit 1
fi

# ─── Gate 1：MODEL 文件必须存在 ──────────────────────────────────────────────
: "${MODEL:=$VENV/ppo_game.zip}"
if [ ! -f "$MODEL" ]; then
  echo "!! MODEL 文件不存在：$MODEL" >&2
  echo "   请先训练策略，或通过 MODEL= 指定已有模型路径。" >&2
  exit 1
fi

# ─── 生成 OPT_RUN_ID ────────────────────────────────────────────────────────
OPT_RUN_ID="opt-$(date +%Y%m%d-%H%M%S)-$$"

# ─── 配对评估配置（Task 7 新增；均有默认值）─────────────────────────────────
: "${EVAL_SEEDS:=1,2,3}"
: "${EVAL_EPISODES:=20}"
: "${MAX_EVAL_STEPS:=40000}"
: "${EVAL_TIMEOUT_SECONDS:=900}"
: "${MIN_IMPROVEMENT:=0.1}"
: "${ARTIFACT_ROOT:=$REPO_ROOT/.artifacts/opt}"

# ─── Artifact / Memory / Report 路径（均在 .artifacts/opt/ 下，不入库）──────
: "${MEMORY_PATH:=$ARTIFACT_ROOT/memory}"
: "${REPORT_PATH:=$ARTIFACT_ROOT/runs/$OPT_RUN_ID/report.json}"

# ─── 闭环调优参数 ────────────────────────────────────────────────────────────
: "${STAGE:=1}"
: "${TARGET_COMPLETION:=0.65}"
: "${MAX_ROUNDS:=8}"
: "${PATIENCE:=3}"
: "${SEARCH_CALLS:=12}"
: "${RETRAIN_EACH:=0}"
# 阶段2 smoke gate 预算(结构改动:apply→语法→smoke→指标)。
: "${SMOKE_MAX_STEPS:=2000}"
: "${SMOKE_TIMEOUT_SECONDS:=120}"
# 默认 protected 点名测量装置文件(game_agent.gd 含 GOAL_X/FALL_Y/reward;
# telemetry/recorder 是落盘装置)——防 structural patch 改尺子(critic C2)。
: "${PROTECTED_PATHS:=harness/**,.git/**,tests/**,docs/**,*/rl/game_agent.gd,*/rl/telemetry.gd,*/rl/recorder.gd,personas/*.json}"

# ─── tunables 路径（阶段1 固定白名单）───────────────────────────────────────
: "${TUNABLES_PATH:=$PROJ/rl/tunables.json}"
if [ ! -f "$TUNABLES_PATH" ]; then
  echo "!! 未找到 $TUNABLES_PATH" >&2
  echo "   请先在项目里接入 tunables.json（参考 template/tunables.json）+ Tunables autoload。" >&2
  exit 1
fi

# ─── 激活 venv ───────────────────────────────────────────────────────────────
# shellcheck source=/dev/null
source "$VENV/bin/activate"

# ─── 打印摘要 ────────────────────────────────────────────────────────────────
MODEL_SHA256="$(sha256sum "$MODEL" 2>/dev/null | awk '{print $1}')"
echo "=== LLM 优化闭环 Task 8 ==="
echo "    Run ID     : $OPT_RUN_ID"
echo "    Model      : $MODEL"
echo "    Model SHA  : ${MODEL_SHA256:-（sha256sum 不可用）}"
echo "    PROJ       : $PROJ"
echo "    SCENE      : $SCENE"
echo "    Artifact   : $ARTIFACT_ROOT"
echo "    Memory     : $MEMORY_PATH"
echo "    Branch     : opt/auto-$OPT_RUN_ID（将在 REPO_ROOT 建立）"
echo ""

# ─── 建/切 opt/auto-* 分支（在本仓，主分支不污染）──────────────────────────
OPT_BRANCH="${OPT_BRANCH:-opt/auto-$OPT_RUN_ID}"
echo "=== 切到优化分支 $OPT_BRANCH（在 $REPO_ROOT）==="
( cd "$REPO_ROOT" && { git checkout -b "$OPT_BRANCH" 2>/dev/null || git checkout "$OPT_BRANCH"; } )

# ─── Export 全部配置给 optimize.py ──────────────────────────────────────────
export REPO_ROOT PROJ SCENE MODEL SPEEDUP GODOT VENV
export STAGE TARGET_COMPLETION MAX_ROUNDS PATIENCE SEARCH_CALLS RETRAIN_EACH PROTECTED_PATHS
export SMOKE_MAX_STEPS SMOKE_TIMEOUT_SECONDS
export TUNABLES_PATH MEMORY_PATH REPORT_PATH
export OPT_RUN_ID ARTIFACT_ROOT
export EVAL_SEEDS EVAL_EPISODES MAX_EVAL_STEPS EVAL_TIMEOUT_SECONDS MIN_IMPROVEMENT
export ANTHROPIC_API_KEY LLM_BACKEND THRESHOLDS

echo "=== 启动优化闭环（STAGE=$STAGE MAX_ROUNDS=$MAX_ROUNDS target=$TARGET_COMPLETION SEARCH_CALLS=$SEARCH_CALLS）==="
python "$HARNESS/optimize.py"
rc=$?

echo "=== 优化结束（rc=$rc）==="
echo "    分支 $OPT_BRANCH 保留供审阅：cd $REPO_ROOT && git log --oneline（看接受的改动）"
echo "    记忆：$MEMORY_PATH"
exit $rc
