# godot-rl-playtest-kit

用 **强化学习 agent 自动玩任意 Godot 4 游戏** 的底子 —— 训练一个神经网络玩家压力测试游戏,
产出「死亡热点 / 通关率 / 动作使用 / 平衡」等数据,用于**自动试玩、找难度/平衡/单调等非故障问题**,
或作 LLM 闭环优化游戏的「试玩员」一环。

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
