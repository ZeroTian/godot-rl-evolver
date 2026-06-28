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
- **训练/推理 reward 不对称(critic C1,本设计的承重事实)**:`infer_rl.py:37` 推理时 `obs, _reward, done, _info
  = env.step(...)` —— **reward 在推理期被丢弃**,`model.predict(obs)` 永不消费 reward。故 **persona reward 权重
  只在训练期塑形策略,推理(试玩 panel)期完全无效**。推论:persona 的差异 100% 来自**加载哪个冻结 MODEL**,
  与运行期读不读权重无关。→ 因此 reward 权重读取必须**训练期专用**,推理/panel 路径**不读 persona JSON、
  reward 字面值原样不动**(才是真·零回归,且不把 reward 变成数据文件而新开 mutation surface)。
- persona reward profile 是**冻结仪器**:优化闭环(阶段1-3)的默认 `PROTECTED_PATHS` 追加**仓根 glob**
  `personas/*.json`(已验 `fnmatch('personas/aggressive.json','personas/*.json')==True`;**不要**用 `personas/**`
  依赖递归语义),闭环**永不**改任何 persona reward。注意 persona 文件在**仓根 `personas/`,不经 `res://`/proj_rel
  映射**,故保护靠 glob 默认 + Gate 0b 越界检查(M1),需测试坐实。
- 诊断跨 persona 比较**只用 reward 无关量**:`completion_rate`(基于 `term ∈ WIN_TERMS`,由几何 `GOAL_X` 决定,
  与 reward 无关)、death_pos、term 分布、动作熵、覆盖。**reward 耦合的 `mean_return`/`return_std`**(规则
  `progress_stall`/`unstable_difficulty` 用到)在跨 persona **绝不互比**(critic C2)。
- **Goodhart 防火墙(可执行点,critic M4)**:主观/剖面结论标 `type:"soft"`/`for_persona:true`,**绝不**放进被
  消费的 `report["issues"]`(它驱动 `has_high_issue` 早停 + 喂 `objective.score`);写独立产物。要有测试断言 soft
  issue 不影响 objective/早停。
- persona 策略是**外部 MODEL**,不入库;其 SHA-256 进 provenance(沿用阶段1 规矩)。
- GDScript 改动用 `--import` 校验(不用 `--check-only`)。
- **本计划只做 personas(设计 S2)**;粗粒度轨迹流 + LLM 相对裁判(设计 §4.2/§5.2,S1/S3)**不在本计划**。

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

def run_persona_panel(cfg, personas: list[dict], *, panel_run_id: str) -> dict[str, "EvaluationResult"]:
    """对每个 persona 跑 evaluate_current,返回 {persona_name: EvaluationResult}。
    - 每 persona 用**其自己的 model**:复制 cfg(`copy.replace`/浅拷贝)→ 覆盖 cfg2.model=persona['model']、
      cfg2.opt_run_id=panel_run_id(panel 自带 run_id,避免 OPT_RUN_ID 为空时 artifact 目录撞 FileExistsError,m2);
      scene 仍读 cfg.scene(不另传,m1)。
    - artifact 路径 `<root>/runs/<panel_run_id>/persona_<name>/`;复用 run_one_seed 隔离/新鲜度。
    - 为避免循环 import,函数内局部 import optimize。
    - 一个 persona 评估失败(evaluate_current 抛)→ 记录该 persona 失败并继续其余(不整体中止);返回里缺该 persona。"""
```

```python
# harness/diagnose.py(扩展)
# 跨 persona 可比的 reward 无关 issue 白名单(critic C2):
_PERSONA_COMPARABLE_ISSUES = {"difficulty_too_hard", "difficulty_too_easy",
    "death_hotspot", "done_reason_skew", "redundant_action", "monotony"}
# reward 耦合、跨 persona 不可比(基于 mean_return/return_std):progress_stall, unstable_difficulty

def cross_persona_profile(reports_by_persona: dict[str, dict], thresholds=None) -> dict:
    """输入 {persona: report},输出体验剖面:
    - per_persona: 各 persona 的 completion_rate/mean_len/term 摘要 + 其 issues(**仅保留
      _PERSONA_COMPARABLE_ISSUES**;progress_stall/unstable_difficulty 因 reward 耦合被剔除或标 non_comparable)
    - spread: 跨 persona 的 **completion_rate** 离散(max-min、谁最难/最易)——completion 由 term(几何)定,reward 无关
    - soft_issues: difficulty_varies_by_persona(completion 极差 > thresholds['persona_spread'])、
      persona_specific_hotspot(某 persona 独有 death_hotspot)
    所有 soft_issue 标 type='soft'、for_persona=true、agent_relative=true,**绝不**进被消费的 report['issues']。"""
```

`run_persona_panel` 复用既有 `evaluation.RunResult/EvaluationResult` 与 `run_one_seed`(stage2 已加预算覆盖参数)。
**成本注**:panel 评估 = persona 数 × `EVAL_SEEDS` × `EVAL_EPISODES`,是单 agent 的 N 倍,需预算意识。

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

### Task 2: game_agent.gd 训练期读 persona 权重(推理路径零改动)
**Files:** `testbed_platformer/rl/game_agent.gd`(✏️)、`personas/_keys.md` 或代码注释(权威键表)
> ⚠️ 关键(critic C1/M2):reward **只在训练期起作用**(推理丢弃)。所以**只在 env `PERSONA` 非空时**才从
> `personas/<PERSONA>.json` 覆盖权重;**`PERSONA` 为空(推理/panel/优化闭环默认路径)→ 完全走现有字面值,
> reward 计算字节级不变**(真·零回归,且不把 reward 变成永远生效的数据文件)。
- [ ] **Step 1 权威键表**:先定 reward 键 ↔ game_agent.gd 字面值的**唯一映射**(progress=0.01, time_penalty=0.002,
  damage=0.1, kill=25.0, combat_shape=0.5, hurt_penalty=0.5, gap_edge_jump=1.0, gap_cross=8.0, goal=30.0,
  fall=10.0, hp_fail=10.0;以 game_agent.gd:160-216 实际字面为准核对)。此表同时是 Task 1 `load_persona` 的校验键集
  (共享常量,避免 .gd 与校验器分叉,m3)。
- [ ] **Step 2 实现**:`_ready` 时若 `OS.get_environment("PERSONA")` 非空 → 读 `res://../personas/<PERSONA>.json`
  填一个 `_w` 字典;否则 `_w` 为空。reward 行用 `_wget("kill", 25.0)`(_w 有则用、无则用字面默认)。
  **`GOAL_X`/`FALL_Y`/终止几何绝不动**(测量装置)。
- [ ] **Step 3 校验**:① `( cd testbed_platformer && Godot --headless --path . --import )` rc=0 且无 SCRIPT ERROR;
  ② **PERSONA 未设时**,逐行核对 `_wget(k, lit)` 的 `lit` == 原字面(reward 路径字节级等价);
  ③ 设 `PERSONA=aggressive` 跑一次 `--import` 确认能读 JSON 不报错。
- [ ] **Step 4 Commit** `feat(subj): game_agent reads persona reward weights at TRAIN time only (infer path byte-identical)`。

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
  `test_cross_persona_spread_detects_difficulty_variance`(persona 间**通关率**极差大 → soft issue
  difficulty_varies_by_persona,标出谁最难)、`test_cross_persona_specific_hotspot`(某 persona 独有热点)、
  `test_cross_persona_all_similar_no_soft_issue`(都接近 → 不报)、
  **`test_cross_persona_excludes_return_coupled_issues`(critic C2:合成 report 含 progress_stall/
  unstable_difficulty,断言它们不出现在剖面的可比 issue 里、不参与 spread)**、
  **`test_soft_issues_marked_and_not_in_consumed_issues`(critic M4:soft issue 标 type='soft',
  且不混入会被 has_high_issue/objective 消费的 report['issues'])**。注意严格不等号边界(沿用诊断器 TDD 纪律)。
- [ ] **Step 2 RED** → **Step 3 实现** `cross_persona_profile`(纯函数;只用 `_PERSONA_COMPARABLE_ISSUES` 白名单 +
  completion_rate 算 spread;阈值进 THRESHOLDS 可 `--thresholds` 覆盖,如 `persona_spread`)。soft issue 全标
  type='soft'/for_persona/agent_relative,产出独立结构(不写回 report['issues'])。
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
  跑 `run_persona_panel` + `cross_persona_profile`,**验管线接通**:每 persona 独立 telemetry 目录、per-persona
  report、剖面聚合**执行**。⚠️ 诚实声明(critic M3):占位 = 同 model + (C1)reward 推理无效 → 两 persona telemetry
  近乎相同 → 剖面**正确地不报** spread soft issue;**spread 正向分支只由 Task 4 合成单测覆盖,不由本 e2e 覆盖**;
  真实 spread 需 Task 5 训出区分模型。可选:向 `cross_persona_profile` 注入两份**合成发散 report** 演示正向分支。
- [ ] **Step 2 PROTECTED + 防火墙(critic M1/M4)**:① 默认 `PROTECTED_PATHS` 追加仓根 glob `personas/*.json`
  (optimize.py `DEFAULT_PROTECTED` + run_optimize.sh),加 `tests/test_optimize.py`/`test_mutate.py` 断言
  `mutate.allowed` 拒绝触碰 `personas/aggressive.json` 的 plan(显式验 fnmatch 行为,不假定 `**`);
  ② 加测断言 cross_persona soft issue 不进 `has_high_issue`/`objective.score` 消费路径。
- [ ] **Step 3 全量回归 + 凭据扫描**:`pytest tests/ -q` 全绿;`git ls-files | rg` 凭据扫描空。
- [ ] **Step 4 文档**:README 主观体验层用法、CLAUDE.md 进度("主观体验层:personas 机制已建,LLM 相对裁判
  待建")、spec status 更新。
- [ ] **Step 5 Commit** `docs(subj): document procedural-personas machinery`。

## 完成定义
- 所有单测通过;testbed `--import` 通过。
- **训练/推理 reward 不对称已落实(C1)**:`PERSONA` 未设时 reward 路径字节级等价(真零回归);persona 权重仅训练期生效。
- persona 面板机制端到端接通(占位 persona 验**管线**):多 persona 独立试玩 → per-persona report → 剖面聚合执行;
  **诚实标注** spread 正向分支仅由合成单测覆盖、真实 spread 需外部训练。
- **跨 persona 只比 reward 无关量(C2)**:completion/death_pos/term/熵/覆盖;`progress_stall`/`unstable_difficulty`
  被排除,有测试坐实。
- persona reward profile 被默认 PROTECTED 仓根 glob `personas/*.json` 护住(fnmatch 行为有测试),优化闭环永不改。
- **Goodhart 防火墙可执行(M4)**:soft/剖面 issue 标 `type:soft` 且不进 `report['issues']`/objective/早停,有测试。
- 校准流程有文档与 `run_train.sh PERSONA=` 辅助;**真实 persona 策略训练是用户外部算力步骤**(模型不入库)。
