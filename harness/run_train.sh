#!/bin/bash
# 通用 RL 训练协调器:先起 Python(PPO server,监听端口),再命令行启 Godot 连入。
# 全部路径用环境变量配置 → 适配任意 Godot 项目,不写死。
#
# 用法示例:
#   PROJ=/mnt/e/code/mygame SCENE=res://rl/train.tscn TIMESTEPS=60000 \
#     bash harness/run_train.sh
#
# 可选:WARM_START=旧模型.zip 继续训(保留已学技能);SAVE_PATH=保存路径
set -u
: "${GODOT:=/mnt/d/Godot/Godot_console.exe}"            # Godot 控制台版(WSL 路径,能回显)
: "${PROJ:?需设置 PROJ=游戏项目目录(含 project.godot)}"
: "${SCENE:?需设置 SCENE=res://训练场景.tscn(挂了 Sync + Agent)}"
: "${VENV:=$HOME/.local/share/godot-rl-venv}"
: "${PORT:=11008}"
: "${SPEEDUP:=8}"
: "${TIMESTEPS:=60000}"
HARNESS="$(cd "$(dirname "$0")" && pwd)"

source "$VENV/bin/activate"

echo "=== reimport(确保脚本/资源最新) ==="
# WSL 坑:Godot 只认 Windows 路径 → cd 进项目再用 --path .
( cd "$PROJ" && "$GODOT" --headless --path . --import ) >/dev/null 2>&1

echo "=== 启动 Python PPO server(监听 :$PORT) ==="
TIMESTEPS=$TIMESTEPS PORT=$PORT SPEEDUP=$SPEEDUP \
  python "$HARNESS/train_rl.py" > /tmp/rl_train.log 2>&1 &
PYPID=$!

sleep 6   # 等 Python 在端口上监听好

echo "=== 启动 Godot 连入训练 ==="
( cd "$PROJ" && "$GODOT" --headless --path . "$SCENE" --port=$PORT --speedup=$SPEEDUP ) \
  > /tmp/rl_godot.log 2>&1 &
GPID=$!

wait $PYPID
kill $GPID 2>/dev/null
echo "=== 训练流程结束(日志:/tmp/rl_train.log /tmp/rl_godot.log) ==="
