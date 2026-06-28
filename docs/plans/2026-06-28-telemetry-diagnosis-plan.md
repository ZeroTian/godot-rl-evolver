# 通用度量 + 问题发现层 实施计划

## Overview

为 godot-rl-evolver 实现「试玩 → 度量 → 诊断」链路的第二环:Godot 侧通用 telemetry 采集 helper
(落盘 JSONL)+ Python 侧离线诊断器(规则引擎 → `report.json`)。设计详见
`docs/specs/2026-06-28-telemetry-diagnosis-design.md`,本计划是其逐步实现。

实现顺序为 bottom-up:先纯 Python 诊断器(用合成 fixture 完全 TDD,不依赖 Godot)→ Godot 采集 helper
→ example_platformer 真实接入并端到端验证 → 模板示范 + shell 集成 + 文档。

## Prerequisites

- 已有 Python venv(默认 `~/.local/share/godot-rl-venv`)。诊断器纯标准库,测试需 pytest。
- Godot 4 项目(`example_platformer/` 为参考片段;真实端到端跑在完整项目 `godot-study/platformer`)。
- 阅读 spec 的 §5(数据契约 schema)与 §6(诊断规则集)——字段名、阈值 key 以 spec 为准。

## Step 1: Python 诊断器核心 + 单元测试(纯函数,TDD)— 已完成

**Goal:** 实现 `diagnose.py` 的数据加载、聚合、规则引擎,能读 JSONL 产出 `report.json`。

**Files:** `harness/diagnose.py`、`tests/test_diagnose.py`、`tests/conftest.py`

**结构:** load_jsonl / aggregate / diagnose(8 规则) / build_report / format_summary / main。
阈值 THRESHOLDS 可被 `--thresholds` JSON 覆盖。death 事件从 episode 的 `events` 数组读(name=="death"),
无则用 end_pos 兜底。

**Success criteria:**
- [x] `pytest tests/test_diagnose.py -q` 全绿(25 passed)
- [x] CLI 实跑产出 report.json + 摘要
- [x] 8 条规则各有触发/不触发用例
- [x] 空 JSONL / 全 unknown term 不崩

## Step 2: Godot telemetry.gd 采集 helper — 已完成

**Goal:** `harness/telemetry.gd`(RefCounted),产出 Step 1 能消费的 JSONL。

**API:** start_run / record_action / tick / emit_event / set_metric / end_episode / finish。
事件内嵌进 episode 行的 `events` 字段(非独立 type:"event" 行)。未 start_run 时所有方法 no-op。

**Success criteria:**
- [x] `godot --headless --check-only` EXIT=0
- [x] API 与 spec §4.1 一致;容错 no-op

## Step 3: 真实接入 + 端到端验证 — 已完成

**Goal:** 把 telemetry 接进完整项目跑推理产出真实 JSONL,并让 diagnose.py 消费。

**说明:** `example_platformer/` 是残缺参考(无 project.godot/addon),无法独立运行。真实端到端跑在
完整项目 `godot-study/platformer`(有 addon + 现成模型 `ppo_game.zip`)。

**接入点(spec §4.4):**
- `game_env.gd`: preload Telemetry、`_ready` 中 `tele.start_run(...)` + `agent.tele = tele`、`_exit_tree` 中 `tele.finish()`
- `game_agent.gd`: `set_action` 末尾 `record_action`;`_physics_process` 帧初存 `_r_before`、帧末 `tick(reward-_r_before, pos)`;
  done 分支按真实终止条件(goal/fall/hp/ep>=MAX_EP)设 term + emit death,置 `_pending_record`;
  reset 握手在 `env.reset_episode()` 前 `end_episode`(仅当 `_pending_record`),并清 `done=false`

**关键坑(已解决):** godot_rl 的 reset 时序与每帧 `_physics_process` 不同步,`done` 未及时清零会在起点
产生一串 `len=1 timeout` 伪局。修复:① 仅真实终止条件才 `_pending_record=true` 并记录;② 握手清 `done=false` 阻断级联。

**Success criteria:**
- [x] 推理跑通,`telemetry/run_*.jsonl` 生成,每行可 `json.loads`
- [x] episode 行含 len/return/actions/action_entropy/coverage/end_pos/term
- [x] fall/hp 局 events 含 death 事件(死亡位置在缺口 x~595-624)
- [x] diagnose.py 消费真实 JSONL 产出 report.json 无报错
- [x] 修复后零伪局(term 仅 goal/fall)

## Step 4: 模板示范 + shell 集成 + 文档 — 待办

**Files:** `template/agent_template.gd`、`harness/run_infer.sh`、`README.md`

**Implementation:**
- `agent_template.gd`: 加 telemetry 接入示范(★ 注释标 4 个 hook 点 + `_pending_record` 模式)
- `run_infer.sh`: 推理后若 `DIAGNOSE=1`(默认)调 `diagnose.py <最新 jsonl>`
- `README.md`: 「度量 + 诊断」章节 + 接入 3 步 + 8 规则 + 环境变量(`DIAGNOSE`/`TELEMETRY_DIR`/`GRID_CELL`)

**Success criteria:**
- [ ] `bash -n harness/run_infer.sh` 通过
- [ ] template hook 注释与 example 实际接入对应
- [ ] README 有完整章节 + 环境变量表
- [ ] `DIAGNOSE=1` 跑推理后自动生成 report.json

## 收尾

- 每个 Step 完成后 `git commit`。
- `telemetry/` 与 `report.json` 属运行产物,加入 `.gitignore`。
