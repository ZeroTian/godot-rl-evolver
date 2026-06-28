# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

`godot-rl-evolver` 是**用强化学习驱动 Godot 4 游戏「自进化」的通用工具链**:训练一个神经网络
玩家自动试玩任意游戏 → 度量(死亡热点/通关率/动作分布/难度/体感)→ 自动发现难度/平衡/单调/
体感等**非故障问题** → 喂给 LLM 闭环改关卡/数值。进化循环:**试玩 → 度量 → 优化 → 再试玩**。

- **当前已建成**:试玩(RL 玩家)环 + 度量/诊断环。**未建**:LLM 优化环 + 循环编排。
- 这是**工具链/模板仓**,不是某个具体游戏。真实跑通的样例在 `example_platformer/`
  (从 `godot-study/platformer` 抽出)。
- 设计文档在 `docs/specs/2026-06-28-telemetry-diagnosis-design.md`,实现计划在
  `docs/plans/`。改度量/诊断相关代码前应先读 spec。

## 仓库的两套代码(关键心智模型)

| 层 | 文件 | 性质 |
|---|---|---|
| Python harness | `harness/*.py` `harness/*.sh` | **通用、与游戏无关**,全靠环境变量配置,不写死 obs/动作 |
| GDScript 模板 | `template/*.gd` `harness/*.gd` | 拷进**你的** Godot 项目的 `res://rl/`,填 ★FILL★ 钩子后使用 |
| 样例 | `example_platformer/*.gd *.tscn` | 模板在真实游戏里填好的参考实现 |
| 诊断器单测 | `tests/test_diagnose.py` | 纯标准库 + pytest,唯一的自动化测试 |

`harness/*.gd`(`telemetry.gd` `recorder.gd`)是 GDScript,**不在 Python 测试范围内**,只能靠
Godot `--check-only` 做语法校验。模板/样例的 `.gd` 同理。

## 常用命令

```bash
# 诊断器测试(唯一的自动化测试套件,24 例,纯标准库)
python -m pytest tests/ -q
python -m pytest tests/test_diagnose.py::test_load_jsonl_raises_on_bad_line -q   # 单测

# 离线诊断一份 telemetry JSONL(可覆盖阈值)
python harness/diagnose.py path/run_*.jsonl [--out report.json] \
  [--thresholds '{"hard_completion":0.3}']

# 训练 / 推理(协调 Python PPO server + Godot client,见下方架构)
PROJ=/mnt/e/code/你的游戏 SCENE=res://rl/train.tscn TIMESTEPS=60000 bash harness/run_train.sh
PROJ=... SCENE=... MODEL=~/.local/share/godot-rl-venv/ppo_game.zip bash harness/run_infer.sh
#   run_infer.sh 默认 DIAGNOSE=1:推理结束后自动对最新 telemetry 跑 diagnose.py

# GDScript 语法校验(无 Python 测试覆盖,改 .gd 后这样验)
( cd $PROJ && /mnt/d/Godot/Godot_console.exe --headless --path . --check-only res://rl/xxx.gd )
```

Python 依赖装在仓库**外**的 venv(`~/.local/share/godot-rl-venv`,装 `godot-rl
stable-baselines3 torch`),不入库。诊断器/测试只依赖标准库 + pytest。

## 架构(需跨文件才能看懂的大图)

**1. 虚拟手柄(不改游戏)**:RL 控制器的 `set_action()` 把策略动作翻成
`Input.action_press/release("jump"/...)`,让现成游戏的 FSM/动画/战斗原样运行。等于给任何「读
`Input`」的 Godot 游戏装个神经网络玩家。

```
Python(SB3 PPO, server, :11008) ←TCP(localhost)→ Godot(Sync 节点 + AIController, client)
        训练:train_rl.py / 推理:infer_rl.py        set_action→Input 注入→真游戏 FSM
```
两个 `run_*.sh` 负责启动顺序:先起 Python 监听端口,`sleep 6`,再 `cd $PROJ` 启 Godot 连入。

**2. 两层 telemetry → 离线诊断(度量环)**:`harness/telemetry.gd`(RefCounted)在游戏侧按
episode 把指标落盘成 **JSONL**;`harness/diagnose.py` 离线读取、套可配阈值规则,产出
`report.json` + 控制台摘要。**`telemetry.gd` 写的 JSONL 格式 = `diagnose.py` 读的契约**
(spec §4.1/§5.1 定义),改一边必须同步另一边:
- run 头行(`type:"run"`)+ 每 episode 一行(`type:"episode"`),death 等事件**内嵌**在
  episode 行的 `events` 字段,**不是**独立行。
- 自动通用层(零配置):局长/回报/各动作档使用率/动作序列熵/探索覆盖(网格 cells+熵)/终止位置。
- 可选语义层(游戏侧 emit):`emit_event("death",{...})` / `set_metric(...)` / 终止原因 `term`。
- `diagnose.py` 的 8 条规则(阈值见 `THRESHOLDS`,全可 `--thresholds` 覆盖):
  difficulty_too_hard/too_easy、death_hotspot、done_reason_skew、progress_stall、
  redundant_action、monotony、unstable_difficulty。结论都是**「针对当前策略的相对结论」**。

**3. 接入新游戏 = 拷模板 + 填 4 个钩子**:把 `template/*.gd` + `harness/{telemetry,recorder}.gd`
拷到目标项目 `res://rl/`,建训练场景(根=env 脚本 + `Sync` 节点 + 真地图/角色 + `Agent` 节点),
填 agent 的 ★FILL★ 钩子:`get_obs()` / `get_action_space()`+`set_action()` / `get_reward()`+终止
判定 / env 的 `_reset_to_start()`。其余(reset 握手、指标采集)通用。

## 必踩的坑(都在样例里踩过,改相关代码务必遵守)

- **godot_rl reset 时序坑**:godot_rl 的 reset 与每帧 `_physics_process` 不同步,`done` 未及时
  清零会在起点产生一串 `len=1` 的**伪局**污染数据。修复模式(`template/agent_template.gd` 已照做):
  ① 仅**真实终止**(goal/fall/hp/超时)才置 `_pending_record=true`;② reset 握手里
  `done=false`;③ `end_episode` 只在 `_pending_record` 时调用。
- **reset/done 握手**:godot_rl **不因 `done` 自动复位**,终止时必须自己也置 `needs_reset=true`,
  否则 agent 死了不重生、episode 卡死。
- **训练/推理 `SPEEDUP` 必须一致**:策略绑在控制频率上(`speed_up=N` = 控制频率 ×N),不一致会
  让跳跃/攻击时机系统性错位。
- **概率性技能用 `DETERMINISTIC=0` 推理**:argmax 会把「~40% 概率才触发」的动作压成不触发。
- **WSL**:Godot 只认 Windows 路径(脚本已 `cd $PROJ` 再 `--path .`);Windows-Godot ↔
  WSL-Python 走 `localhost` TCP 直通。
- `/mnt/e` 是 9p drvfs,有激进写缓存且曾发生过**整仓回滚**——重要改动要及时 commit 并 push
  到 GitHub(`ZeroTian/godot-rl-evolver`)作为离机备份。

## 改代码时

- 诊断器(`diagnose.py`)走 **TDD**:先在 `tests/test_diagnose.py` 写失败测试再实现;改阈值/
  规则/聚合逻辑都要有对应测试。注意边界:测试里 `count > mean+σ` 这类严格不等号,构造数据别让
  阈值恰好等于某 count(否则 `N > N` 为假)。
- 改 `telemetry.gd` 的落盘字段 → 必同步 `diagnose.py` 的 `aggregate()` 读取 + spec。
- 模板(`template/`)和样例(`example_platformer/`)是「同一份逻辑的骨架版与填好版」,改通用
  逻辑(握手/采集)时两边保持一致。
- 提交前自检无密钥:`git ls-files | grep -iE 'cookie|token|secret|key|\.env|password'` 应为空。
  `.omc/` 已在 `.gitignore`。

## 环境变量速查

`PROJ`(必填,Godot 项目目录)、`SCENE`(必填,`res://` 场景)、`GODOT`
(默认 `/mnt/d/Godot/Godot_console.exe`)、`VENV`、`SPEEDUP`(默认 8,训练/推理须一致)、
`TIMESTEPS`、`WARM_START`(热启动旧模型)、`MODEL`/`SAVE_PATH`、`INFER_STEPS`、
`DETERMINISTIC`(默认 0)、`DIAGNOSE`(默认 1)、`TELEMETRY_DIR`、`GRID_CELL`(默认 64)。
完整说明见 `README.md` 末尾的表。
