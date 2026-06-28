# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

`godot-rl-evolver` 是**用强化学习驱动 Godot 4 游戏「自进化」的通用工具链**:训练一个神经网络
玩家自动试玩任意游戏 → 度量(死亡热点/通关率/动作分布/难度/体感)→ 自动发现难度/平衡/单调/
体感等**非故障问题** → 喂给 LLM 闭环改关卡/数值。进化循环:**试玩 → 度量 → 优化 → 再试玩**。

- **当前已建成**:试玩(RL 玩家)环 + 度量/诊断环 + LLM 优化闭环(阶段1 数值 + 阶段2 结构 `.tscn`)。**未建**:优化环阶段3(逻辑 `.gd` 改动)+ 循环编排。
  - 阶段2 结构闭环:LLM 对 `.tscn` 提 anchor patch → 四步 gate(`tscn_sanity` 健全性 → Godot `--import` → smoke 试玩 → 指标回归)→ 接受/定向回滚。无贝叶斯(一次提案=一个候选)。测量边界**三层防御**(`parse_plan` / `mutate.allowed` 看 patches / `apply_patch` 带 `protected_globs`),默认 `PROTECTED_PATHS` 点名 `game_agent.gd` 等测量装置。结构旋钮=测试床灰盒平台 `MidPlatform`,**绝不**碰与 `GOAL_X` 耦合的 `GoalFlag`/reward/终止几何。
  - ⚠️ **`--import` 对 `.tscn` 不可靠**(实测缺括号/悬空 SubResource/错误 node type 一律 rc=0 静默放过,甚至吞成默认值)→ 故 `harness/gates.py` 加纯 Python `tscn_sanity`(资源引用完整性+括号平衡)前置;smoke gate 是运行期最后兜底。
- 这是**工具链/模板仓**,不是某个具体游戏。仓内测试床 `testbed_platformer/`(2D 平台跳跃,
  来源 `godot-study/platformer`)是可直接运行的 Godot 项目,已入仓;参考样例在 `example_platformer/`。
  测试床所用**模型不入仓**,须由 `MODEL` 环境变量显式指定外部路径,模型 SHA-256 进入运行 provenance。
- 设计文档在 `docs/specs/2026-06-28-telemetry-diagnosis-design.md`,实现计划在
  `docs/plans/`。改度量/诊断相关代码前应先读 spec。

## 仓库的两套代码(关键心智模型)

| 层 | 文件 | 性质 |
|---|---|---|
| Python harness | `harness/*.py` `harness/*.sh` | **通用、与游戏无关**,全靠环境变量配置,不写死 obs/动作 |
| GDScript 模板 | `template/*.gd` `harness/*.gd` | 拷进**你的** Godot 项目的 `res://rl/`,填 ★FILL★ 钩子后使用 |
| 样例 | `example_platformer/*.gd *.tscn` | 模板在真实游戏里填好的参考实现 |
| 自动化测试 | `tests/` | pytest,诊断器 + 优化环(106 例);GDScript 不在内 |

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

# GDScript 校验(无 Python 测试覆盖,改 .gd 后这样验)
# 项目级(推荐,会加载 autoload 编译全部脚本并退出;rc=0 且无 "SCRIPT ERROR" 即过):
( cd $PROJ && /mnt/d/Godot/Godot_console.exe --headless --path . --import )
# 单个「不引用 autoload」的脚本可用 --check-only,但必须配 --script,否则会被当主场景运行而卡死:
( cd $PROJ && /mnt/d/Godot/Godot_console.exe --headless --path . --check-only --script res://rl/xxx.gd )
#   ⚠️ 引用 Tunables 等 autoload 的脚本用 --check-only --script 会误报 "Identifier not found",改用上面的 --import
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

- **godot_rl reset 时序坑 + done 保活**:telemetry 的局边界靠 `_pending_record`(仅**真实终止**
  goal/fall/hp/超时才置真)+ `end_episode` 只在 `_pending_record` 时调用 —— 这保证 telemetry **永远**
  不记 `len=1` 伪局,与 done 时序无关。**但 `done` 不要在 reset 握手里清零**(`done = false`):
  `done` 只活 1 物理帧时,被 `action_repeat`(默认 8)门控的 Sync 多半采样不到 → Python 收到的 done
  计数与真实局数**脱钩 ~20x**,`EVAL_EPISODES` 失效。正确做法是 **done 保活**:终止只置 `done=true`,
  清零交给 Sync 控制步 `_get_done_from_agents` 读后 `set_done_false`(模板/样例/测试床已照做)。
  适用前提(godot_rl_agents 默认满足):训练路径 `_reset_agents_if_done` 已注释、基类 `reset()` 不动 done。
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
`TIMESTEPS`、`WARM_START`(热启动旧模型)、`MODEL`/`SAVE_PATH`(模型**不入库**,显式外部路径)、
`EVAL_SEED`(单次评估种子,同时控制 Python/NumPy/PyTorch/SB3 + Godot `--env_seed`)、
`EVAL_SEEDS`(默认 `1,2,3`)、`EVAL_EPISODES`(默认 20)、`MAX_EVAL_STEPS`(默认 40000)、
`EVAL_TIMEOUT_SECONDS`(默认 900)、`MIN_IMPROVEMENT`(默认 0.1)、
`ARTIFACT_ROOT`(默认 `$REPO_ROOT/.artifacts/opt`,gitignore 不入库)、
`DETERMINISTIC`(默认 0)、`DIAGNOSE`(默认 1)、`TELEMETRY_DIR`、`GRID_CELL`(默认 64)。
完整说明见 `README.md` 末尾的表。
