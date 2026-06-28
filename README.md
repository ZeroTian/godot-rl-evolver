# godot-rl-evolver

**用强化学习驱动 Godot 4 游戏「自进化」的底子** —— 训练一个神经网络玩家自动试玩任意游戏,
产出「死亡热点 / 通关率 / 动作使用 / 难度 / 平衡 / 体感」等数据,**自动发现难度、平衡、单调、体感
等非故障问题**,再喂给 LLM 闭环改关卡/数值/玩法,跑「**试玩 → 度量 → 优化 → 再试玩**」的进化循环。
当前内置的 RL 试玩员是这条自进化链路里的「玩家」一环。

> 仓内附带可直接运行的 Godot 测试床 [`testbed_platformer/`](testbed_platformer/)（2D 平台跳跃，
> 来源 `godot-study/platformer`，已入仓）；参考样例见 [`example_platformer/`](example_platformer/)。
> **注意**：测试床本身入仓，但所用模型（`ppo_game.zip`）**不入仓**，须由 `MODEL` 环境变量显式指定
> 外部路径（默认 `~/.local/share/godot-rl-venv/ppo_game.zip`）；模型的 SHA-256 进入每条运行记录的
> `provenance` 字段，保证可追溯。

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
| `harness/optimize.py` | **通用**优化闭环编排器:report → LLM 提案 → 贝叶斯搜数值 → 验证 → 接受/回滚 |
| `harness/{llm_propose,search,objective,mutate,memory}.py` | 优化环子模块:LLM 提案/贝叶斯/客观分数/改动应用+git/记忆 |
| `harness/run_optimize.sh` | 优化闭环入口(优化分支隔离,主分支不污染) |
| `template/tunables.{json,gd}` | 游戏侧参数化:可调项清单 + `Tunables` autoload |
| `tests/` | 诊断器 + 优化环单测(106 例,pytest) |
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

## 优化闭环(自进化的「优化」环)

度量环产出 `report.json` 后,优化环让 LLM **提改动假设**、贝叶斯优化**搜数值**、改完**再试玩验证**,
客观分数真变好才接受(否则 git 回滚),全程记忆失败教训——闭合「试玩 → 度量 → 优化 → 再试玩」。

**四条铁律**(调研 RuleSmith/Nova/TITAN 一致):① 解耦(LLM 只提案,不负责玩,玩交给 RL agent)
② 不盲信 LLM 数值(改完必验,只有客观分数真变好才接受)③ 记忆失败教训喂回下一轮 ④ 用 `report` 派生的客观分数做锚,防 LLM 自欺。

**安全脊梁**(全自动改动的命门):三道 gate(① 语法 = `.tscn` 健全性检查 + Godot `--import` ②
smoke 试玩跑通 ③ 指标回归)+ 全程 git 优化分支可回滚 + `PROTECTED_PATHS` 禁改路径入口 +
预算上限(轮数/搜索次数/早停)。
> ⚠️ **`--import` 对 `.tscn` 不可靠**:实测它对缺括号 `Vector2`、悬空 `SubResource` 引用、错误 node
> type 一律 rc=0 静默放过(甚至把坏值吞成默认值=看似过 gate 实则没改游戏)。故阶段2 在 `--import`
> 前加了一道纯 Python `tscn_sanity`(资源引用完整性 + 构造器括号平衡),smoke gate 是运行期兜底。

**参数化接入**(在度量接入基础上加):把 `template/tunables.json` + `template/tunables.gd` 拷到
`res://rl/`,在 `project.godot` 注册 `Tunables` autoload,游戏脚本用 `Tunables.get_param("gap_width", 120)`
取代硬编码——数值改动改 JSON 即生效,无需碰代码(这是「解耦/可回滚」的载体)。
> ⚠️ 使用 `Tunables.get_param()` 而非 `Tunables.get()`：后者与 Godot 内置 `Object.get()` 冲突。

**跑**(LLM 后端二选一):
```bash
# ① 免 key:用本机 Claude Code CLI(复用订阅认证,推荐)
LLM_BACKEND=claude_cli MODEL=~/.local/share/godot-rl-venv/ppo_game.zip \
  SCENE=res://rl/train_map.tscn bash harness/run_optimize.sh
# ② 或用 anthropic API key
PROJ=... SCENE=... MODEL=... ANTHROPIC_API_KEY=sk-... bash harness/run_optimize.sh
# → 默认 PROJ=本仓 testbed_platformer;在 git 优化分支上跑闭环:LLM 圈参数 → 贝叶斯搜数值 →
#   配对验证 → 接受/回滚 → 记 memory;结束打印总结(接受的改动 / score 前后 / 剩余 issue)
```

> ⚠️ **分阶段**:**阶段1 = 数值闭环**(`STAGE=1`,只改 `tunables.json`,已建);**阶段2 = 结构闭环**
> (`STAGE=2`,LLM 对 `.tscn` 提 anchor patch,四步 gate 后接受/回滚,已建);阶段3 逻辑(`.gd`)后续交付。
> 阶段2 结构改动**无贝叶斯**(patch 是离散文本操作,一次提案=一个候选,直接四步 gate);测试床的
> 结构旋钮是灰盒踏脚石平台 `MidPlatform`(挪它改难度,**绝不**碰与 `GOAL_X` 耦合的 `GoalFlag`/reward/终止几何)。
> 跑结构闭环:`STAGE=2 MODEL=... SCENE=res://rl/train_map.tscn bash harness/run_optimize.sh`。
> **LLM 后端**:`LLM_BACKEND=auto`(默认)有 `ANTHROPIC_API_KEY` 走 anthropic SDK,否则用本机
> `claude` CLI(免 key);可显式设 `claude_cli`/`anthropic`。key 走环境变量,**绝不入库**。

## 主观体验层(procedural personas)

把"发现问题"从规则诊断的客观指标扩到**主观体验**,守业界三铁律:**相对而非绝对、对谁而非客观、
发现优先不进锚**(设计 `docs/specs/2026-06-28-subjective-experience-layer-design.md`,
调研 `.omc/research/2026-06-28-subjective-playtesting-signals.md`)。

**personas = 一组风格各异的冻结策略**(好战/求稳/速通/探索),回答"这关**对谁**难、对谁无聊":
- `personas/<name>.json` = reward-shaping profile(**冻结仪器面板**,优化闭环 protected 永不改)。
- **校准**(算力步骤,模型不入库):对每个 persona 各跑一次
  `PERSONA=aggressive MODEL=... SCENE=res://rl/train_map.tscn bash harness/run_train.sh`
  (可 `WARM_START` 复用基线加速)→ 得到该 persona 的冻结策略。
- **跑 panel**:`personas.run_persona_panel` 对一关逐 persona 试玩 → 每 persona 一份 report →
  `diagnose.cross_persona_profile` 出体验剖面(谁最难、`difficulty_varies_by_persona` 等 soft issue)。

> ⚠️ **关键(reward 训练/推理不对称)**:`infer_rl` 推理期**丢弃 reward**,故 persona reward 权重
> **只在训练期塑形策略**,persona 差异 100% 来自加载哪个冻结模型。`game_agent.gd` 仅 `PERSONA` env
> 非空时读权重,空则走字面默认(推理路径零回归)。
> ⚠️ **跨 persona 只比 reward 无关量**(通关率/死亡位置/term/熵/覆盖);`progress_stall`/
> `unstable_difficulty`(基于 return)被排除。**Goodhart 红线**:剖面 soft issue 标 `type:soft`、
> **默认只进报告不进优化锚**(`has_high_issue` 忽略 soft;`personas/*.json` 受 protected)。

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
| `PERSONA` | — | 训练期 persona 名;非空则 `game_agent.gd` 读 `personas/<PERSONA>.json` 的 reward 权重塑形该 persona(仅训练期生效) |
| `SAVE_PATH` / `MODEL` | venv/ppo_game.zip | 模型保存 / 推理加载路径（**模型不入库**，须显式外部路径） |
| `EVAL_SEED` | — | 单次评估随机种子；同时控制 Python/NumPy/PyTorch/SB3 + Godot `--env_seed` |
| `EVAL_SEEDS` | `1,2,3` | 优化闭环每轮使用的种子列表（逗号分隔）；配对差值消除种子噪声 |
| `EVAL_EPISODES` | 20 | 每个 seed 必须完成的局数；不足则该次子评估失败 |
| `MAX_EVAL_STEPS` | 40000 | 每个 seed 的步数预算；先于 episode 目标耗尽则非 0 退出 |
| `EVAL_TIMEOUT_SECONDS` | 900 | 单 seed 推理超时（秒）；超时视为失败 |
| `MIN_IMPROVEMENT` | 0.1 | 接受门阈值；配对改善均值必须**严格大于**此值才接受 |
| `ARTIFACT_ROOT` | `$REPO_ROOT/.artifacts/opt` | 运行产物根目录（telemetry/report/memory）；被 gitignore，不入库 |
| `DETERMINISTIC` | 0 | 推理是否用 argmax（概率性技能设 0） |
| `DIAGNOSE` | 1 | 推理结束后是否自动跑 `diagnose.py`（0=关） |
| `TELEMETRY_DIR` | `$PROJ/rl/telemetry` | telemetry JSONL 落盘目录 |
| `GRID_CELL` | 64 | 探索覆盖/死亡热点的网格边长（像素） |
| `STAGE` | 1 | 优化改动面：1=数值 / 2=+结构 / 3=+逻辑 |
| `TARGET_COMPLETION` | 0.65 | 优化目标通关率（客观分数用） |
| `MAX_ROUNDS` / `PATIENCE` | 8 / 3 | 闭环最大轮数 / 连续无改善早停轮数 |
| `SEARCH_CALLS` | 12 | 贝叶斯每轮评估预算（仅数值搜索；结构改动无贝叶斯） |
| `RETRAIN_EACH` | 0 | 评估是否每次热启动重训（0=纯推理省钱） |
| `SMOKE_MAX_STEPS` | 2000 | 阶段2 smoke gate 的步数预算（结构改动后跑 ≥1 局确认场景可起） |
| `SMOKE_TIMEOUT_SECONDS` | 120 | 阶段2 smoke / 语法 gate 单次墙钟超时（秒） |
| `PROTECTED_PATHS` | `harness/**,.git/**,tests/**,docs/**,*/rl/game_agent.gd,*/rl/telemetry.gd,*/rl/recorder.gd` | 禁止 LLM 修改的路径 glob（默认点名测量装置文件：`game_agent.gd` 含 `GOAL_X`/`FALL_Y`/reward，telemetry/recorder 是落盘装置） |
| `THRESHOLDS` | — | 覆盖 diagnose 默认阈值的 JSON（如 `{"hard_completion":0.5}`），调诊断灵敏度 |
| `LLM_BACKEND` | `auto` | `auto`/`anthropic`/`claude_cli`；auto 有 key 用 SDK,否则用本机 claude CLI（免 key） |
| `ANTHROPIC_API_KEY` | （与 claude CLI 二选一） | anthropic SDK 的 key，环境变量，**绝不入库**；变量名本身可出现在文档/脚本/测试中，值不可入库 |

> **固定种子链**：`EVAL_SEED`（或 `EVAL_SEEDS` 中每个值）同时传给 Python `random.seed` /
> `np.random.seed` / `torch.manual_seed` / `model.set_random_seed`，以及 Godot `--env_seed`，
> 保证同一种子的 candidate—baseline 配对差值消除共变噪声。
>
> **artifact 目录**：`.artifacts/opt/` 存放单次 run 的 telemetry/report 和跨 run 的 memory，
> 已被 `.gitignore` 排除，不会入库。优化 run 只向 `testbed_platformer/rl/tunables.json`
> 等白名单路径提交 git commit。
