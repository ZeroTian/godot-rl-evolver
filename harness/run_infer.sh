#!/bin/bash
# 通用推理回放:Python 加载训练好的策略(监听端口),Godot 非 headless 连入(开窗口渲染 + 截图)。
# 注意:推理的 SPEEDUP 必须与训练时一致(策略绑在控制频率上)。
#
# 用法:PROJ=/mnt/e/code/mygame SCENE=res://rl/train.tscn MODEL=.../ppo.zip \
#         bash harness/run_infer.sh
set -u
: "${GODOT:=/mnt/d/Godot/Godot_console.exe}"
: "${PROJ:?需设置 PROJ=游戏项目目录}"
: "${SCENE:?需设置 SCENE=res://训练场景.tscn}"
: "${VENV:=$HOME/.local/share/godot-rl-venv}"
: "${PORT:=11008}"
: "${SPEEDUP:=8}"          # 必须与训练一致!
: "${EVAL_SEED:=3}"        # 同一 seed 同时喂 Python(RNG/env/model)与 Godot(--env_seed)
: "${EVAL_EPISODES:=5}"    # 按 episode 停止:达到此局数才结束
: "${MAX_EVAL_STEPS:=40000}"  # 步数上限:先到则推理非 0 退出(评估失败)
HARNESS="$(cd "$(dirname "$0")" && pwd)"

source "$VENV/bin/activate"

echo "=== 启动 Python 策略回放 server(:$PORT, seed=$EVAL_SEED) ==="
PORT=$PORT SPEEDUP=$SPEEDUP \
  EVAL_SEED=$EVAL_SEED EVAL_EPISODES=$EVAL_EPISODES MAX_EVAL_STEPS=$MAX_EVAL_STEPS \
  python "$HARNESS/infer_rl.py" > /tmp/rl_infer.log 2>&1 &
PYPID=$!

sleep 6

echo "=== 启动 Godot(非 headless,开窗口渲染 + Recorder 截图) ==="
# WSL→Windows-Godot 默认不传自定义环境变量;用 WSLENV 把 TELEMETRY_DIR 以 /p(路径翻译
# /mnt/e/... → E:\...)传入,否则 telemetry.gd 收不到、落回 res://rl/telemetry,隔离失效。
# TELEMETRY_DIR 未设时传空,telemetry.gd 自动回退默认目录。
#   TELEMETRY_DIR 用 /p 翻译路径;MODEL 仅作 provenance 标识(telemetry run 头记录,供
#   validate_telemetry 比对),按原样传(不加 /p,否则 /home/... 被译成 UNC 路径反而对不上)。
( cd "$PROJ" && \
    TELEMETRY_DIR="${TELEMETRY_DIR:-}" MODEL="${MODEL:-}" \
    WSLENV="${WSLENV:+$WSLENV:}TELEMETRY_DIR/p:MODEL" \
    "$GODOT" --path . "$SCENE" --port=$PORT \
    --speedup=$SPEEDUP --env_seed=$EVAL_SEED ) \
  > /tmp/infer_godot.log 2>&1 &
GPID=$!

wait $PYPID
sleep 1
kill $GPID 2>/dev/null
echo "=== 回放结束 ==="

# 推理结束后自动诊断(DIAGNOSE=1 默认开;游戏侧需已接入 telemetry.gd)
if [ "${DIAGNOSE:-1}" = "1" ]; then
  TELEMETRY_DIR="${TELEMETRY_DIR:-$PROJ/rl/telemetry}"
  latest=$(ls -t "$TELEMETRY_DIR"/run_*.jsonl 2>/dev/null | head -1)
  if [ -n "$latest" ]; then
    echo "=== 诊断:$latest ==="
    python "$HARNESS/diagnose.py" "$latest"
  else
    echo "=== 未找到 telemetry($TELEMETRY_DIR),跳过诊断 ==="
    echo "    (确认 env/agent 已接入 telemetry.gd;见 README「度量 + 诊断」)"
  fi
fi
