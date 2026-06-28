# 主观体验层 · Procedural Personas(S2)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` / `superpowers:executing-plans`,task-by-task,checkbox 跟踪。
> 关联设计: `docs/specs/2026-06-28-subjective-experience-layer-design.md`(§4.1 personas / §5 契约 / §6 Goodhart)
> 关联调研: `.omc/research/2026-06-28-subjective-playtesting-signals.md`

**Goal:** 建成 procedural personas 能力——用一组**风格各异的冻结策略**(好战/求稳/速通/探索)对同一关试玩,产出
**「对谁而言」的体验剖面**(对谁难、对谁无聊、难度尖峰对谁存在)。这是领域"超越难度"的成熟正解
(Holmgård/Yannakakis 体系)。

**Architecture:** persona = 一份 reward-shaping 权重 profile(`personas/*.json`,**冻结仪器面板**,优化闭环
protected 永不改)。每 persona 用其权重 `WARM_START` 训出一个**冻结策略**(外部 MODEL,不入库)。试玩编排
对一关**依次用每 persona 的策略**跑 `EVAL_SEEDS` 局 → 每 persona 一份 telemetry → 复用 `diagnose.py` 出
per-persona report → **跨 persona 聚合**成体验剖面。诊断规则对每 persona **相同**(reward 只塑形了"怎么玩",
诊断量的是 reward 无关的行为事实:通关率/死亡位置/熵/覆盖),故跨 persona 可比。

**关键现实(诚实声明):** persona 的差异来自**真实训练**。本计划交付**机制 + 管线 + 训练辅助 + 测试**,并用
现有 `ppo_game.zip` 当**占位 persona** 把端到端机制验通;**真正训出 4 个区分开的 persona 策略是用户的外部
算力步骤**(像阶段1 的 MODEL,模型不入库)。完成定义区分"机制已验"与"persona 已校准"。

**Tech Stack:** Python 3、pytest、stable-baselines3/PyTorch、Godot 4/GDScript、Bash、Git。

## Global Constraints
- persona reward profile 是**冻结仪器**:优化闭环(阶段1-3)的 `PROTECTED_PATHS` 必须护住 `personas/**` 与
  `game_agent.gd`,闭环**永不**改任何 persona 的 reward(延续"reward 是尺子"——现在是一组尺子)。
- 诊断对每 persona **同一套规则**(reward 无关行为量),保证跨 persona 可比;**绝不**用各 persona 自己的
  return 互比。
- 主观/剖面结论**默认只进报告**,不进优化锚(Goodhart 红线,设计 §6)。
- persona 策略是**外部 MODEL**,不入库;其 SHA-256 进 provenance(沿用阶段1 规矩)。
- `game_agent.gd` 读 persona 权重时**默认值 = 现有硬编码值** → 不选 persona 时行为**零回归**。
- GDScript 改动用 `--import` 校验(不用 `--check-only`)。

---

## 1. 固定接口

```python
# harness/personas.py(新增)
def load_persona(path: str) -> dict:
    """读 personas/<name>.json,校验 name/reward_weights/model 三字段;
    reward_weights 必须含全部已知键(progress/time_penalty/damage/kill/combat_shape/
    hurt_penalty/gap_edge_jump/gap_cross/goal/fall/hp_fail),缺键/未知键报 ValueError。"""

def list_personas(personas_dir: str) -> list[dict]:
    """加载目录下全部 persona 配置,按 name 排序返回。"""

def run_persona_panel(cfg, personas: list[dict], *, scene: str) -> dict[str, "EvaluationResult"]:
    """对每个 persona(用其 model)跑 evaluate_current,返回 {persona_name: EvaluationResult}。
    复用 optimize.run_one_seed / evaluate_current 的隔离/新鲜度;每 persona 独立 artifact 子目录。"""
```

```python
# harness/diagnose.py(扩展)
def cross_persona_profile(reports_by_persona: dict[str, dict], thresholds=None) -> dict:
    """输入 {persona: report},输出体验剖面:
    - per_persona: 各 persona 的 completion/mean_len/term 摘要 + 各自 issues
    - spread: 跨 persona 的难度离散(max-min completion、谁最难/最易)
    - soft_issues: 如 difficulty_varies_by_persona(通关率跨 persona 极差 > 阈值)、
      persona_specific_hotspot(某 persona 独有死亡热点)
    所有结论标 for_persona=true、agent_relative=true(相对各 persona 自身)。"""
```

`run_persona_panel` 复用既有 `evaluation.RunResult/EvaluationResult` 与 `run_one_seed`(stage2 已加预算覆盖参数)。

---

### Task 1: persona 配置 + 加载器(TDD)
**Files:** `harness/personas.py`(🆕)、`personas/*.json`(🆕)、`tests/test_personas.py`(🆕)
- [ ] **Step 1 失败测试**:`test_load_persona_valid`、`test_load_persona_rejects_missing_weight_key`、
  `test_load_persona_rejects_unknown_key`、`test_list_personas_sorted`。
- [ ] **Step 2 RED** → `python -m pytest tests/test_personas.py -q`。
- [ ] **Step 3 实现** `load_persona`/`list_personas`(纯文件+校验,标准库)。建 4 份 profile
  (设计 §4.1 表:aggressive/cautious/speedrunner/explorer)+ `personas/default.json`(权重 = game_agent.gd 现值)。
- [ ] **Step 4 GREEN** + 全量 `pytest tests/ -q` 零回归。
- [ ] **Step 5 Commit** `feat(subj): persona reward-profile config + loader`。

### Task 2: game_agent.gd 读 persona 权重(默认零回归)
**Files:** `testbed_platformer/rl/game_agent.gd`(✏️)
- [ ] **Step 1**:加一个轻量权重读取——优先 `personas/<PERSONA>.json`(经 env `PERSONA` 或既有 Tunables 风格
  注入),**缺省回退到现有硬编码值**。把 §reward 计算里的字面系数(0.01/0.002/0.1/25/0.5/.../30/10)换成
  `_w("progress",0.01)` 之类的读取,默认参数 = 现值。**绝不**动 `GOAL_X`/`FALL_Y`/终止几何(仍是测量装置)。
- [ ] **Step 2 校验**:`( cd testbed_platformer && Godot --headless --path . --import )` rc=0 且无 SCRIPT ERROR;
  且**不选 persona 时 reward 行为与现状一致**(权重默认值逐一核对)。
- [ ] **Step 3 Commit** `feat(subj): game_agent reads persona reward weights (default unchanged)`。

### Task 3: 多 persona 试玩编排(TDD)
**Files:** `harness/personas.py`(✏️)、`tests/test_personas.py`(✏️)
- [ ] **Step 1 失败测试**:`test_run_persona_panel_uses_each_model_and_isolated_dirs`(monkeypatch
  `optimize.run_one_seed`/`evaluate_current`,断言每 persona 用其 model、各自独立 artifact 子目录、
  返回 {name: EvaluationResult})。不起 Godot。
- [ ] **Step 2 RED** → **Step 3 实现** `run_persona_panel`(对每 persona 设其 MODEL,调 evaluate_current;
  artifact 路径 `<root>/runs/<run_id>/persona_<name>/`)。为避免循环 import,在函数内局部 import optimize。
- [ ] **Step 4 GREEN** + 全量回归。
- [ ] **Step 5 Commit** `feat(subj): multi-persona playtest orchestration`。

### Task 4: 跨 persona 体验剖面诊断(TDD)
**Files:** `harness/diagnose.py`(✏️)、`tests/test_diagnose.py`(✏️)
- [ ] **Step 1 失败测试**:用合成 per-persona report 构造——
  `test_cross_persona_spread_detects_difficulty_variance`(persona 间通关率极差大 → soft issue
  difficulty_varies_by_persona,标出谁最难)、`test_cross_persona_specific_hotspot`(某 persona 独有热点)、
  `test_cross_persona_all_similar_no_soft_issue`(都接近 → 不报)。注意严格不等号边界(沿用诊断器 TDD 纪律)。
- [ ] **Step 2 RED** → **Step 3 实现** `cross_persona_profile`(纯函数,聚合 + 阈值;阈值进 THRESHOLDS 可
  `--thresholds` 覆盖,如 `persona_spread`)。soft issue 全标 for_persona/agent_relative。
- [ ] **Step 4 GREEN** + 全量回归。
- [ ] **Step 5 Commit** `feat(subj): cross-persona experience profile diagnosis`。

### Task 5: persona 训练辅助 + 校准流程文档
**Files:** `harness/run_train.sh`(✏️)、`README.md`(✏️)
- [ ] **Step 1**:`run_train.sh` 接受 `PERSONA=<name>`,训练时把对应 reward 权重注入(经 env/PERSONA 传给
  game_agent.gd)→ 产出该 persona 的冻结策略(`SAVE_PATH` 外部路径,不入库)。
- [ ] **Step 2 文档**:README 写"**校准 persona 面板**"流程:对 4 个 persona 各
  `PERSONA=aggressive ... bash harness/run_train.sh`(可 `WARM_START` 复用基线加速),得到 4 个外部 MODEL;
  并说明这是**算力步骤、模型不入库**。
- [ ] **Step 3 Commit** `feat(subj): persona training helper + calibration docs`。

### Task 6: 机制端到端验证 + 文档 + 进度
**Files:** runtime only + `README.md`/`CLAUDE.md`/spec status(✏️)
- [ ] **Step 1 机制 e2e(占位 persona)**:用现有 `ppo_game.zip` 注册成**两个占位 persona**(同 model 不同 name),
  跑 `run_persona_panel` + `cross_persona_profile`,**验机制管线跑通**(每 persona 独立 telemetry、per-persona
  report、剖面聚合输出)。明确标注:占位 persona 不产生真实风格差异,**真实差异需 Task 5 训练**。
- [ ] **Step 2**:把"优化闭环 PROTECTED_PATHS 追加 `personas/**`"落到默认(optimize.py/run_optimize.sh)——
  闭环永不改 persona 仪器(若本任务涉及 optimize 默认,加一条 `tests/test_optimize.py` 断言)。
- [ ] **Step 3 全量回归 + 凭据扫描**:`pytest tests/ -q` 全绿;`git ls-files | rg` 凭据扫描空。
- [ ] **Step 4 文档**:README 主观体验层用法、CLAUDE.md 进度("主观体验层:personas 机制已建,LLM 相对裁判
  待建")、spec status 更新。
- [ ] **Step 5 Commit** `docs(subj): document procedural-personas machinery`。

## 完成定义
- 所有单测通过;testbed `--import` 通过;不选 persona 时 reward 行为零回归。
- persona 面板机制端到端跑通(占位 persona 验管线):多 persona 独立试玩 → per-persona report → 跨 persona
  剖面 soft issue,全程相对/for_persona 标注。
- persona reward profile 被 PROTECTED 护住,优化闭环永不改;诊断对每 persona 用同一套 reward 无关规则。
- 校准流程有文档与 `run_train.sh PERSONA=` 辅助;**真实 persona 策略训练是用户外部算力步骤**(模型不入库)。
- 主观结论默认只进报告,未进优化锚(Goodhart 红线守住)。
