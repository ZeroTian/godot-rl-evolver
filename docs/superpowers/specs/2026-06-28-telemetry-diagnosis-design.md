# 设计文档 · 通用度量 + 问题发现层（Telemetry + Diagnosis Layer）

> 日期: 2026-06-28
> 项目: godot-rl-evolver
> 状态: 设计已通过 brainstorming，待用户复审 → 转实施计划
> 关联调研: `.omc/research/2026-06-28-rl-playtest-diagnosis.md`
> 设计依据: 原项目 `godot-study/NOTES/07-rl-agent-training.md` 的 **7e 节「诊断闭环核心思想」**——
> 本设计是其工具化实现。7e 列出 RL 能感知的四类信号:① 死亡位置热点 ② 奖励曲线平台期
> ③ Done 原因分布 ④ 涌现策略 vs 设计意图。本设计覆盖 ①③④(见 §6),② 属训练期学习曲线信号,列为后续(见 §9)。

## 1. 背景与目标

本项目要做「**用 RL agent 驱动 Godot 游戏自进化**」：跑「试玩 → 度量 → 优化 → 再试玩」循环。
当前仓库只实现了「**玩家**」一环（通用 PPO 训练/推理 harness + 虚拟手柄控制器模板 + platformer 样例）。

本设计实现**承上启下的第二环**：在不破坏现有「harness 与游戏无关」干净度的前提下，加一层
**通用的度量采集 + 问题发现**——把 agent 试玩自动变成结构化、可复现、可被下游 LLM 消费的诊断报告。

**本设计范围内（In scope）**
- Godot 侧通用 telemetry 采集 helper（落盘 JSONL）
- Python 侧离线诊断器（读 JSONL → 规则引擎 → `report.json` + 控制台摘要）
- 模板与样例的接入示范
- 诊断规则的单元测试

**本设计范围外（Out of scope，留给后续环节）**
- LLM 优化闭环（读报告 → 改关卡/数值 → 再试玩）
- 热力图等可视化渲染（JSONL 已含坐标数据，可后续加可视化工具消费）
- 多策略对战式平衡度量（单 agent 单游戏场景下用「冗余/单调」代偿）

## 2. 设计原则（来自调研的关键约束）

1. **零配置可跑，加语义更准**：通用指标全自动采集，不需要游戏侧任何配合；游戏侧可选 emit 语义事件加深诊断。
2. **低侵入**：通用采集逻辑全部收进 `telemetry.gd` helper，接入一个游戏只需在 agent 里加 3~4 行 hook 调用，游戏逻辑零改动。
3. **解耦**：诊断器是纯 Python 离线工具，与训练/推理解耦，可单测、可重跑、可重新分析历史数据。不碰 godot_rl 的 TCP 协议。
4. **阈值可配 + 相对诊断**：所有诊断阈值可配置；报告统一标注「针对当前训练策略的相对诊断」，不当成绝对玩家数据（学术界一致拒绝绝对阈值）。
5. **采集时机**：以**推理阶段**为主（用训练好的策略试玩，数据反映「一个会玩的玩家」的真实体验）。

## 3. 架构总览

```
┌─ Godot 侧（推理时）──────────────────┐         ┌─ Python 侧（离线）─────────────┐
│ agent.gd（被控角色的 RL 控制器）       │         │ diagnose.py                    │
│   set_action()      → tele.record_action(a)      落盘   │   读 run_*.jsonl              │
│   _physics_process()→ tele.tick(reward, pos)  ───────►  │   聚合 + 套阈值规则           │
│   done 时           → tele.end_episode({term,…})  JSONL │   → report.json + 控制台摘要  │
│   语义(可选)        → tele.emit_event("death",…) │       │   规则: 难度/单调/热点/卡住   │
│                                       │         │                                │
│ telemetry.gd（通用 helper, RefCounted）│         │ tests/test_diagnose.py         │
└────────────────────────────────────────┘         └────────────────────────────────┘
```

**数据流**：agent 在已有的 hook 点调用 helper → helper 累计通用指标并在 episode 结束时把一条
JSON 记录 append 到 JSONL 文件 → 推理结束 helper flush → `diagnose.py` 离线读文件聚合套规则出报告。

## 4. 组件设计

### 4.1 `harness/telemetry.gd`（新增，通用采集 helper）

`extends RefCounted`（或轻量 Node）。封装全部通用采集逻辑，对 agent 暴露最少 API。

**公开 API**
| 方法 | 调用位置 | 作用 |
|---|---|---|
| `start_run(cfg: Dictionary)` | env/agent `_ready` | 打开 JSONL 文件，写 `run` 头记录 |
| `record_action(action: Dictionary)` | agent `set_action()` 内 | 累计每个离散动作各档位的使用次数 |
| `tick(reward: float, pos: Vector2)` | agent `_physics_process()` 每帧 | 累计步数、reward、把 `pos` 投进网格更新访问计数 |
| `emit_event(name: String, data: Dictionary)` | 游戏侧可选 | 写一条 `event` 记录（如 death/checkpoint） |
| `set_metric(key: String, value)` | 游戏侧可选 | 设置本局自定义标量（写进 episode 的 `metrics`） |
| `end_episode(info: Dictionary)` | agent done/reset 时 | 计算本局聚合（动作占比、熵、覆盖、终止位置），写 `episode` 记录，重置局内累计 |
| `finish()` | 推理结束 | flush + 关闭文件 |

**内部计算**
- **动作占比**: 每个动作维度各档位计数 / 总步数 → `actions:{dim:[p0,p1,...]}`
- **动作序列熵** `action_entropy`: 对动作组合序列算 Shannon 熵（单调诊断信号）
- **探索覆盖** `coverage`: 玩家位置按 `grid.cell`（默认 64px）离散化进 `Dictionary<cell, count>`；
  本局产出 `cells`（访问过的不同格数）与 `entropy`（访问分布的 Shannon 熵）
- **终止位置** `end_pos`: episode 结束时玩家坐标（死亡热点的降级信号，无语义 death 事件时用）

**容错**：所有 hook 在 helper 未 `start_run` 时静默 no-op（保证模板示范代码不会因忘记初始化而崩）。

### 4.2 `harness/diagnose.py`（新增，纯 Python 离线诊断器）

**职责**：读一个或多个 `run_*.jsonl` → 聚合 → 套规则引擎 → 写 `report.json` + 打印人读摘要。

**结构**（函数式，便于单测）
- `load_jsonl(path) -> list[dict]`：逐行解析
- `aggregate(records) -> dict`：算 run 级聚合指标（见 §5.2）
- `RULES: list[Rule]`：每条规则是 `(id, category, severity_fn, predicate, message_fn, evidence_fn)`
- `diagnose(agg, thresholds) -> list[Issue]`：跑所有规则，产出 issue 列表
- `main()`：CLI / 环境变量入口，落盘 + 摘要

**阈值配置**：内置默认 `THRESHOLDS` dict，可被环境变量或 `--thresholds <json>` 覆盖。

### 4.3 改动文件

| 文件 | 改动 |
|---|---|
| `harness/run_infer.sh` | 推理结束后，若 `DIAGNOSE=1`（默认开）则调 `python diagnose.py <jsonl_dir>` |
| `template/agent_template.gd` | 加 telemetry 接入示范（4 个 hook 点，标 ★ 注释；语义 emit 标为可选） |
| `example_platformer/game_agent.gd` | 真实接入：`record_action`/`tick`/`end_episode` + 语义 `emit_event("death",{cause})`、`set_metric` |
| `example_platformer/game_env.gd` | `_ready` 调 `tele.start_run(...)`，env 释放时 `tele.finish()`（按现有 env 生命周期接） |
| `README.md` | 补「度量 + 诊断」章节 + 新环境变量（`DIAGNOSE`、`TELEMETRY_DIR`、`GRID_CELL`） |

### 4.4 语义事件接入点（基于原 platformer 代码，已在 Step3 实跑验证）

通用层零配置即可跑；语义事件让诊断更准。原 `godot-study/platformer` 现成接入点:

| 语义信号 | 接入点 | emit 方式 |
|---|---|---|
| `term`（终止原因） | agent `_physics_process()` done 分支（GOAL/FALL/HP/ep>=MAX_EP） | `end_episode({"term": ...})`，仅在真实终止条件触发时（`_pending_record`） |
| `death` | done 分支 fall/hp | `emit_event("death", {"pos": [...], "cause": "fall"/"hp"})` |
| `kill` | monster `take_hit()`→`queue_free()`；env `monster_count()` 递减 | `emit_event("kill", {...})`（可选） |
| `damage` | `player.health` 差值 | `set_metric("hp_left", env.player_hp())`（可选） |
| `checkpoint` | agent `_crossed_gap`（x≥630）、GOAL_X | `emit_event("checkpoint", {...})`（可选） |

> 关键坑（Step3 实测）：godot_rl reset 时序导致 `done` 未及时清零会产生 `len=1` 伪局。
> 接入必须:① done 分支仅在真实条件（goal/fall/hp/ep>=MAX_EP）置 `_pending_record=true`；
> ② reset 握手仅当 `_pending_record` 才 `end_episode`，并清 `done=false`。详见 §9。

## 5. 数据契约（Schema）

### 5.1 JSONL 采集格式（`telemetry/run_<ts>.jsonl`）

每行一条记录，`type` 区分三类：

```jsonc
// run 头（每次运行一条）
{"type":"run","run_id":"<ts>","ver":"<游戏版本/可选>","scene":"res://...","model":"ppo_game.zip",
 "speedup":8,"n_episodes":50,"action_space":{"move":3,"jump":2,"attack":2},"grid":{"cell":64}}

// episode 摘要（每局一条 —— 通用自动层，零配置）
{"type":"episode","run_id":"<ts>","ep":12,"len":340,"return":18.4,
 "term":"timeout",                                 // 语义可选；未 emit 则 "unknown"
 "actions":{"move":[0.21,0.10,0.69],"jump":[0.85,0.15],"attack":[0.97,0.03]},
 "action_entropy":1.42,
 "coverage":{"cells":37,"entropy":2.9},
 "end_pos":[630,140],
 "metrics":{"max_x":1180,"hp_left":40}}            // 语义可选标量

// event（语义可选层 —— 游戏侧 emit，不填也能跑）
{"type":"event","run_id":"<ts>","ep":12,"frame":210,"name":"death","pos":[630,140],"cause":"fall"}
```

**聚合主键**：`run_id` + `ep`（+ event 的 `frame`）。

### 5.2 run 级聚合指标（`aggregate` 产出）

- `n_episodes`、`completion_rate`（term=="goal" 或配置的 win 词占比）
- `term_distribution`（各终止原因 goal/fall/hp/timeout/unknown 的占比 —— 对齐 7e 信号③）
- `mean_len`、`mean_return`、`return_std`、`len_std`
- `action_usage`（各维各档跨全 run 的平均占比）
- `mean_action_entropy`、`mean_coverage_entropy`、`mean_cells`
- `end_pos_grid`（终止/死亡位置的网格密度直方图，用于热点）
- `max_ep`（从 run 头或推断，用于 stall 判定）

### 5.3 报告格式（`report.json`）

```jsonc
{
  "run_id":"<ts>","generated_for":"<scene>","agent_relative":true,
  "summary":{"n_episodes":50,"completion_rate":0.04,"mean_len":1180,"mean_return":-3.2},
  "issues":[
    {"id":"difficulty_too_hard","severity":"high","category":"tuning",
     "metric":"completion_rate","value":0.04,"threshold":0.10,
     "message":"通关率 4%，远低于 10%，对当前策略过难",
     "evidence":{"episodes":50,"goal":2,"top_fail_cells":[[630,140]]}}
  ]
}
```

`category ∈ {structural, tuning, fork}`（干预档，抄 Nova，供下游 LLM 选改动类型）。
`agent_relative: true` 标注全部诊断为「针对当前训练策略的相对结论」。

## 6. 诊断规则集（初始，阈值全部可配）

| id | 触发条件（默认阈值） | severity | category | 备注 |
|---|---|---|---|---|
| `difficulty_too_hard` | `completion_rate < 0.10` | high | tuning | 行业人类玩家锚点是 0.60；agent 弱，默认放宽，可配 |
| `difficulty_too_easy` | `completion_rate > 0.90` 且 `mean_len < easy_len` | low | tuning | |
| `death_hotspot` | 某网格终止/死亡数 `> mean + 2σ` | high | structural | 7e 信号①；有语义 death 事件用之，否则用 `end_pos` 降级 |
| `done_reason_skew` | 某非通关 term 占比 `> dominant_term_frac` | high | structural | 7e 信号③；如「fall 占 63% → 缺口是难度尖峰」 |
| `progress_stall` | `mean_len ≥ 0.9·max_ep` 且 `mean_return < stall_return` | medium | structural | agent 被卡住（7e 信号②推理期近似） |
| `redundant_action` | 某动作档 `usage < 0.01` | medium | tuning | 7e 信号④；message 点明「可能学会绕过此机制」；只提示勿自动删（stepping-stone） |
| `monotony` | `mean_action_entropy < ent_min` 或 `mean_coverage_entropy < cov_ent_min` | low | structural | 动作/空间单调 |
| `unstable_difficulty` | `return_std / |mean_return|` 超阈值 | low | tuning | 运气/不稳定的体感粗信号 |

**默认 `THRESHOLDS`**（示意，实施时定稿）：
```python
THRESHOLDS = {
  "hard_completion": 0.10, "easy_completion": 0.90, "easy_len_frac": 0.5,
  "hotspot_sigma": 2.0, "dominant_term_frac": 0.60,
  "stall_len_frac": 0.9, "stall_return": 0.0,
  "redundant_usage": 0.01, "ent_min": 0.5, "cov_ent_min": 1.0,
  "unstable_cv": 1.5,
}
```

## 7. 测试策略

- `tests/test_diagnose.py`（pytest）：**每条规则一个用例**，喂合成 JSONL fixture（构造触发/不触发两种），断言 issue 是否出现、severity/category/evidence 正确。
- `aggregate` 的纯函数单测：给定 records → 验证 completion_rate / 熵 / 网格密度计算正确。
- `telemetry.gd`：在 `example_platformer` 跑一局短推理，肉眼核对落盘 JSONL 格式合法（每行可被 `json.loads`）、字段齐全。
- 边界：空 JSONL、全 `term==unknown`（无语义）时诊断器不崩、仍输出可用报告（热点降级到 end_pos）。

## 8. 文件清单

| 文件 | 动作 |
|---|---|
| `harness/telemetry.gd` | 🆕 新增 |
| `harness/diagnose.py` | 🆕 新增 |
| `tests/test_diagnose.py` | 🆕 新增 |
| `harness/run_infer.sh` | ✏️ 改（推理后可选调 diagnose） |
| `template/agent_template.gd` | ✏️ 改（接入示范） |
| `example_platformer/game_agent.gd` | ✏️ 改（真实接入 + 语义事件） |
| `example_platformer/game_env.gd` | ✏️ 改（start_run/finish 生命周期） |
| `README.md` | ✏️ 改（度量+诊断章节 + 环境变量） |

## 9. 风险与权衡

- **平衡(balance)诊断弱**：真正的平衡度量需多策略/多角色对战数据；单 agent 单游戏下只能用「冗余动作/单调」近似。对战类游戏需求出现时再扩展 balance 规则（dominant strategy 检测等，见调研 §三）。
- **熵阈值需经验校准**：`ent_min`/`cov_ent_min` 的合理值依游戏而异，初值可能误报/漏报；做成可配并在 README 说明如何按实际 run 调。
- **death 语义依赖**：最有价值的死亡热点需游戏侧 emit `death` 事件；未 emit 时用 `end_pos` 降级，精度下降但不阻塞。
- **JSONL 体积 / 不默认逐帧**：按调研里 EA SEED 的体积控制经验，默认只记 episode 摘要 + 按需语义 event，不每物理帧记一条；`telemetry.gd` 保留可选逐帧开关备用。MVP 不做文件轮转。
- **2D 假设（本期范围）**：当前样例与控制器均为 `AIController2D`，`tick(pos)`/`end_pos`/覆盖网格均按 2D（`Vector2` + 单层网格）。3D 需升至 `Vector3` + 体素（调研 §SEED 配方），属后续；`diagnose.py` 只消费坐标数组，对维度无感知，无需改。
- **7e 信号② 奖励平台期（后续）**：「奖励曲线长期不涨=此处有墙」是训练期学习曲线信号，本期采集时机以推理为主，未纳入；推理期用 `progress_stall` 近似。后续加训练期采集可读 SB3 `ep_rew_mean` 序列做平台期检测。
- **godot_rl reset 时序坑（已在 Step3 实测并修复）**：godot_rl 的 reset 与每帧 `_physics_process` 不同步，`done` 未及时清零会在起点产生 `len=1` 伪局。接入时必须:① 仅真实终止条件才记录(`_pending_record`)；② 握手清 `done=false` 阻断级联。
