# godot-rl-evolver

**用强化学习驱动 Godot 4 游戏「自进化」的底子** —— 训练一个神经网络玩家自动试玩任意游戏,
产出「死亡热点 / 通关率 / 动作使用 / 难度 / 平衡 / 体感」等数据,**自动发现难度、平衡、单调、体感
等非故障问题**,再喂给 LLM 闭环改关卡/数值/玩法,跑「**试玩 → 度量 → 优化 → 再试玩**」的进化循环。
当前内置的 RL 试玩员是这条自进化链路里的「玩家」一环。

> 从 `godot-study/platformer`(一个 2D 平台跳跃实战项目)的 RL 工程抽出通用部分而成。
> 完整可运行的样例见 [`example_platformer/`](example_platformer/)。

## 核心思路:虚拟手柄(不改游戏)

RL 控制器的 `set_action()` 把策略动作翻译成 `Input.action_press/release("move_right"/"jump"/...)`,
**让现成游戏的状态机/动画/战斗判定原样运行**。等于给任何「读 `Input`」的 Godot 游戏装了个神经网络玩家,
无需重写游戏逻辑。

```
Python(SB3 PPO, server, :11008) ←TCP→ Godot(Sync 节点 + AIController, client)
            ↑ 学习/推理                          ↑ set_action → Input 注入 → 真游戏 FSM
```

## 目录

| 路径 | 说明 |
|---|---|
| `harness/train_rl.py` `infer_rl.py` | **通用**,与游戏无关(不写死 obs/动作),走环境变量配置 |
| `harness/run_train.sh` `run_infer.sh` | **通用**,协调 Python+Godot 启动顺序(已处理 WSL 路径坑) |
| `harness/recorder.gd` | 推理时截图(可按帧/按 episode 录) |
| `harness/telemetry.gd` | **通用**度量采集 helper(RefCounted),按 episode 落盘 JSONL |
| `harness/diagnose.py` | **通用**离线诊断器:读 JSONL → 规则引擎 → `report.json` + 摘要 |
| `tests/test_diagnose.py` | 诊断器单测(24 例,纯标准库 + pytest) |
| `template/agent_template.gd` | RL 控制器骨架,标注 **★ FILL ★** 的 4 个钩子 |
| `template/env_template.gd` | env 根骨架(episode 复位 / 敌人重生 / 查询) |
| `example_platformer/` | platformer 真实跑通的完整实现(参考样例) |

## 接入一个新游戏(4 步)

1. **装依赖**:Godot 项目里装 [`godot_rl_agents`](https://github.com/edbeeching/godot_rl_agents) addon;
   Python 侧 `uv venv ~/.local/share/godot-rl-venv` + 装 `godot-rl stable-baselines3 torch`。
2. **拷模板进你的项目**:把 `template/agent_template.gd`、`template/env_template.gd`、`harness/recorder.gd`
   放到你项目的 `res://rl/`,新建一个训练场景(根=env 脚本,挂 `Sync` 节点 + 真地图/角色 + `Agent` 节点)。
3. **填 4 个钩子**(agent 脚本里 ★ FILL ★):
   - `get_obs()` — 观测什么(位置/距目标/地形/敌人/血量,归一化)
   - `get_action_space()` + `set_action()` — 你的游戏有哪些 `Input` 动作
   - `get_reward()` 累计 + 终止判定 — 奖励什么、何时算赢/输
   - env 的 `_reset_to_start()` — 怎么把一局重置回起点
4. **训练 / 推理**:
   ```bash
   PROJ=/mnt/e/code/你的游戏 SCENE=res://rl/train.tscn TIMESTEPS=60000 \
     bash harness/run_train.sh
   # 推理(SPEEDUP 必须与训练一致):
   PROJ=... SCENE=... MODEL=~/.local/share/godot-rl-venv/ppo_game.zip \
     bash harness/run_infer.sh
   ```

## 血泪坑(务必看,全在样例里踩过)

- **reset/done 握手**:godot_rl **不会因 `done` 自动复位**——终止时必须**自己也置 `needs_reset=true`**,
  否则 agent 死了不重生、episode 卡死。
- **训练/推理 `SPEEDUP` 必须一致**:`speed_up=N` 等于控制频率 ×N,策略绑在这个频率上;不一致会让跳跃/攻击时机系统性错位。
- **概率性技能用 `deterministic=False` 推理**:argmax 会把「~40% 概率才触发」的跳/砍压成不触发→每次失败。
- **稀疏大奖学不会**:硬探索动作(精准跳、连击)必须配**密集行为塑形**(缺口边起跳+1、近敌挥砍+0.5)。
- **别用「全或无」奖励门控**:它会让锚定奖励不可达,连带摧毁已学会的前置技能。
- **WSL**:Godot 只认 Windows 路径(脚本已 `cd $PROJ` 再 `--path .`);Windows-Godot↔WSL-Python 走 `localhost` TCP 直通。
- 改奖励/关卡后用 **`WARM_START=旧模型.zip` 热启动**继续训,保留已学技能。

更详细的工程笔记见 `godot-study/NOTES.md` 的 7d / 7e 节。

## 度量 + 诊断(自进化的「度量」环)

RL 试玩员跑起来后,`telemetry.gd` 自动把每局的**通用指标**落盘成 JSONL,`diagnose.py` 离线读取、
套**可配阈值规则**,产出结构化 `report.json`——把「试玩」变成机器/LLM 可消费的**问题清单**。

**两层采集**(对齐 `godot-study/NOTES` 7e 节的「四类诊断信号」):
- **自动通用层**(零配置):局长 / 累计回报 / 各动作档使用率 / 动作序列熵 / 探索覆盖(网格 cells+熵) / 终止位置
- **可选语义层**(游戏侧 emit,不填也能跑):`emit_event("death", {...})` / `set_metric(...)` / 终止原因 `term`

**8 条诊断规则**(阈值全部可配,标注「针对当前策略的相对结论」):
`difficulty_too_hard` / `difficulty_too_easy` / `death_hotspot` / `done_reason_skew`(终止原因偏斜) /
`progress_stall`(卡住) / `redundant_action`(冗余/绕过机制) / `monotony`(单调) / `unstable_difficulty`(体感不稳)。

**接入(在「接入新游戏 4 步」基础上加 telemetry,约 5 行)**:
1. 把 `harness/telemetry.gd` 拷到你项目的 `res://rl/`。
2. **env 根**(见 `template/env_template.gd`):`const Telemetry = preload("res://rl/telemetry.gd")`;
   `_ready` 里 `tele = Telemetry.new(); tele.start_run({...}); agent.tele = tele`;`_exit_tree` 里 `tele.finish()`。
3. **agent**(见 `template/agent_template.gd` 的 ★ 度量 注释):`set_action` 末尾 `tele.record_action(action)`;
   `_physics_process` 帧初存 `_r_before`、帧末 `tele.tick(reward - _r_before, p.global_position)`;
   done 分支按**真实终止条件**设 `term` + emit death + 置 `_pending_record`;
   reset 握手在 `env.reset_episode()` **之前** `end_episode`(仅当 `_pending_record`)并清 `done = false`。

> ⚠️ **godot_rl reset 时序坑(务必照做)**:godot_rl 的 reset 与每帧 `_physics_process` 不同步,
> `done` 未及时清零会在起点产生一串 `len=1` 的**伪局**污染数据。修复 = 上面第 3 步的两点:
> ① 仅真实终止才 `_pending_record`;② 握手清 `done = false`。(已在 `example_platformer/` 里照做。)

**跑**:`run_infer.sh` 在推理结束后自动调诊断(`DIAGNOSE=1` 默认开):
```bash
PROJ=... SCENE=... MODEL=~/.local/share/godot-rl-venv/ppo_game.zip \
  bash harness/run_infer.sh
# → 生成 res://rl/telemetry/run_*.jsonl,并打印诊断摘要 + 写 report.json
# 单独诊断历史数据:
python harness/diagnose.py 路径/run_*.jsonl [--out report.json] [--thresholds '{"hard_completion":0.3}']
```

样例真实输出(platformer,27 局推理):
```
通关率: 37%  终止原因分布: goal=37%, fall=63%
[HIGH] done_reason_skew — 坠落占 63% 主导失败,缺口疑似难度尖峰
[LOW]  unstable_difficulty — 回报 CV=1.62,通关稳定性差
```

## 环境变量速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROJ` | (必填) | 你的 Godot 项目目录 |
| `SCENE` | (必填) | `res://` 训练场景路径 |
| `GODOT` | `/mnt/d/Godot/Godot_console.exe` | Godot 控制台版二进制 |
| `VENV` | `~/.local/share/godot-rl-venv` | Python venv |
| `TIMESTEPS` | 60000 | 训练步数 |
| `SPEEDUP` | 8 | 加速倍率(训练/推理须一致) |
| `WARM_START` | — | 热启动的旧模型路径 |
| `SAVE_PATH` / `MODEL` | venv/ppo_game.zip | 模型保存 / 推理加载路径 |
| `INFER_STEPS` | 600 | 推理步数 |
| `DETERMINISTIC` | 0 | 推理是否用 argmax(概率性技能设 0) |
| `DIAGNOSE` | 1 | 推理结束后是否自动跑 `diagnose.py`(0=关) |
| `TELEMETRY_DIR` | `$PROJ/rl/telemetry` | telemetry JSONL 落盘目录 |
| `GRID_CELL` | 64 | 探索覆盖/死亡热点的网格边长(像素) |
