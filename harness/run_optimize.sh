#!/bin/bash
# LLM 优化闭环入口(阶段1:数值闭环 MVP)。
# 在「被优化项目(PROJ)」的 git 优化分支上跑闭环:
#   LLM 提案(圈参数+范围)→ 贝叶斯搜数值 → 试玩验证(复用 run_infer.sh)→ 指标变好才接受,否则 git 回滚。
# 全程在优化分支,主分支不污染;改动可追溯、可回滚。
#
# 用法:
#   PROJ=/mnt/e/code/godot-study/platformer SCENE=res://rl/train.tscn \
#     MODEL=~/.local/share/godot-rl-venv/ppo_game.zip ANTHROPIC_API_KEY=sk-... \
#     bash harness/run_optimize.sh
#
# 前提:① 项目已接入 rl/tunables.json(见 template/tunables.json)+ Tunables autoload;
#       ② 已有训练好的策略(MODEL);③ 前两环(telemetry + diagnose)已接入。
set -u
: "${GODOT:=/mnt/d/Godot/Godot_console.exe}"
: "${PROJ:?需设置 PROJ=游戏项目目录(含 project.godot 与 rl/tunables.json)}"
: "${SCENE:?需设置 SCENE=res://训练场景.tscn}"
: "${VENV:=$HOME/.local/share/godot-rl-venv}"
: "${MODEL:=$VENV/ppo_game.zip}"
: "${SPEEDUP:=8}"
: "${ANTHROPIC_API_KEY:?需设置 ANTHROPIC_API_KEY(走环境变量,绝不入库)}"

# 闭环参数(spec §9;均有默认)
: "${STAGE:=1}"
: "${TARGET_COMPLETION:=0.65}"
: "${MAX_ROUNDS:=8}"
: "${PATIENCE:=3}"
: "${SEARCH_CALLS:=12}"
: "${RETRAIN_EACH:=0}"
: "${PROTECTED_PATHS:=harness/**,.git/**,tests/**,docs/**}"

HARNESS="$(cd "$(dirname "$0")" && pwd)"
source "$VENV/bin/activate"

# 被优化对象路径(默认在 PROJ/rl 下)
: "${TUNABLES_PATH:=$PROJ/rl/tunables.json}"
: "${MEMORY_PATH:=$PROJ/rl/opt_memory.json}"
: "${REPORT_PATH:=$PROJ/rl/telemetry/report.json}"

if [ ! -f "$TUNABLES_PATH" ]; then
  echo "!! 未找到 $TUNABLES_PATH"
  echo "   请先在项目里接入 tunables.json(参考 template/tunables.json)+ Tunables autoload。"
  exit 1
fi

# 优化分支:在被优化项目仓里建,主分支不污染(git 仓是 mutate 回滚/提交的前提)
OPT_BRANCH="${OPT_BRANCH:-opt/auto-$(date +%Y%m%d-%H%M%S)}"
if ! ( cd "$PROJ" && git rev-parse --git-dir >/dev/null 2>&1 ); then
  echo "!! $PROJ 不是 git 仓 —— 闭环需 git 做快照/回滚,退出。"
  exit 1
fi
echo "=== 切到优化分支 $OPT_BRANCH(在 $PROJ)==="
( cd "$PROJ" && { git checkout -b "$OPT_BRANCH" 2>/dev/null || git checkout "$OPT_BRANCH"; } )

echo "=== 启动优化闭环(STAGE=$STAGE MAX_ROUNDS=$MAX_ROUNDS target=$TARGET_COMPLETION SEARCH_CALLS=$SEARCH_CALLS)==="
export PROJ SCENE MODEL SPEEDUP GODOT VENV
export STAGE TARGET_COMPLETION MAX_ROUNDS PATIENCE SEARCH_CALLS RETRAIN_EACH PROTECTED_PATHS
export TUNABLES_PATH MEMORY_PATH REPORT_PATH
export REPO_ROOT="$PROJ"

python "$HARNESS/optimize.py"
rc=$?

echo "=== 优化结束(rc=$rc)==="
echo "    分支 $OPT_BRANCH 保留供审阅:cd $PROJ && git log --oneline(看接受的改动)"
echo "    记忆:$MEMORY_PATH"
exit $rc
