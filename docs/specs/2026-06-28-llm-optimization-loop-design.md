# 设计文档 · LLM 优化闭环（LLM Optimization Loop）

> 日期: 2026-06-28
> 项目: godot-rl-evolver
> 状态: 设计已通过 brainstorming + 用户复审;2026-06-28 据「测量完整性」复盘修订(测试床纳入本仓 / 参数边界 / 噪声防护 / git 安全 / 新鲜 baseline)
> 关联: 上一环设计 `2026-06-28-telemetry-diagnosis-design.md`；调研 `.omc/research/2026-06-28-rl-playtest-diagnosis.md` §四（下游 LLM 闭环）
> 定位: 自进化循环的**第三环**。前两环（试玩 RL 玩家 + 度量/诊断）已建成并端到端验证；
> 本环把诊断产出的 `report.json` 变成**安全、可验证、会积累经验**的改动，闭合「试玩 → 度量 → 优化 → 再试玩」。

## 1. 背景与目标

前两环已产出结构化的问题清单 `report.json`（难度/热点/单调/体感等 issue）。本环实现「优化」：
读 `report.json` → 让 LLM 提出改动假设 → 应用到游戏 → **再试玩验证** → 接受或回滚 → 记忆 → 循环。

**核心难点不是"让 LLM 改东西"，而是"在全自动改代码的前提下不把游戏改坏、不自欺"。** 因此本设计
的脊梁是**安全 gate + 客观指标锚 + 记忆**，而非 LLM 本身。

**用户拍板的能力组合（最激进路线，RuleSmith 完整版）**：
- 改动载体：**全开** —— 数值参数（`tunables.json`）+ 场景结构（`.tscn`）+ 逻辑（`.gd`）
- 自动化：**全自动闭环** —— 改→重训/推理→诊断→再改，跑到达标 / 预算耗尽 / 连续无改善
- 数值决策：**LLM 提方向 + 贝叶斯优化搜数值**（LLM 圈定参数子集与范围，优化器搜最优值）

**In scope**
- 游戏侧参数化层：`tunables.json` schema + `Tunables` autoload（让数值改动免改代码即生效）
- Python 编排器 `optimize.py` 及其子模块（LLM 提案 / 贝叶斯搜索 / 客观分数 / 改动应用+git 回滚 / 记忆）
- 三道安全 gate（语法 / smoke / 指标回归）+ protected-paths 入口
- **可运行测试床纳入本仓**(`testbed_platformer/`):端到端闭环只在本仓 git 上操作,自包含可复现
- 纯函数部分（客观分数 / 改动应用 / 记忆 / 提案解析）的单元测试

**Out of scope（留后续）**
- 多策略对战式平衡优化（需多 agent，本环单 agent 单游戏）
- 可视化前端 / 跨迭代 History UI（memory.json 已存数据，可后续消费）
- 分布式 / 集群批跑（MVP 单机串行）

## 2. 设计原则（调研四铁律 + 安全）

1. **解耦**：LLM 只负责「提改动假设」，不负责「会玩」（玩交给已有 RL agent）。LLM 也不负责搜连续数值（交给贝叶斯优化）。
2. **不盲信 LLM 数值**：LLM 对复杂度量有幻觉 → **任何改动改完必须再试玩验证**，且只有客观分数真变好才接受。
3. **记忆失败**：每轮（改动 → 前后指标 → 接受/拒绝 → 原因）写入 `memory.json`，喂回下一轮提案，避免重复犯错。
4. **外部客观指标做锚**：用 `report.json` 派生的标量分数判定成败，防 LLM 自欺 / 多样性幻觉。
5. **测量完整性(防自欺的硬边界)**:优化器只能动**游戏设计旋钮**(敌人血量/速度、跳跃力、动态平台尺寸等真实玩法参数);**绝不能动测量装置本身** —— reward 系数、`GOAL`/`FALL` 等终止几何、telemetry 落盘、诊断阈值一律禁改。理由:reward 直接塑造 agent 被度量的行为,改 reward 抬通关率 = 改尺子而非改游戏;改终止几何/阈值 = 直接篡改度量。这条是原则 4(客观锚)的前提,违反则整个闭环退化为自欺。
6. **噪声不是改善**:`DETERMINISTIC=0` 随机推理本有方差。baseline 与候选用**同一组固定种子**做**配对重复评估**(`EVAL_REPEATS` 次)取均值;且只有改善幅度 **> `MIN_IMPROVEMENT`** 才算真变好,防止把噪声波动当成果接受。
7. **每轮新鲜 baseline**:不复用磁盘上的旧 `report.json`(可能来自不同 tunables/模型)。每轮强制重跑 baseline,报告带 provenance(scene/model/speedup/tunables hash),拒绝陈旧/空/局数不足的报告。
8. **安全优先**:全自动改代码,git 必须可回滚 + 多道 gate + protected-paths + 预算上限 + 可随时中断。**git 只作用于本仓**;启动前要求工作树干净;提交只暂存白名单文件,回滚只恢复本轮改动(**不用** `reset --hard`/`add -A` 这类会吞掉开发者在制改动的宽泛流程)。
9. **分阶段落地**：架构一次设计到位（容纳三种改动载体），但实施按风险递增分三阶段交付（见 §8）。

## 3. 架构总览

```
┌─ 游戏侧（被优化对象）──────────────┐      ┌─ Python 编排器（optimize.py）──────────────────┐
│ rl/tunables.json（可调项 + 范围）   │      │  主循环（§7）:                                   │
│ Tunables autoload                  │      │   ① llm_propose  → 改动计划（§5.2）             │
│   _ready 读 tunables.json          │ 读取 │   ② git 快照                                     │
│ 游戏脚本 Tunables.get("gap_width")  │◄─────┤   ③ mutate 应用（数值/结构/逻辑）+ protected 检查 │
│ (.tscn / .gd 也可被 patch 改)      │ 改写 │   ④ search（贝叶斯，仅数值类）                    │
│                                    │─────►│   ⑤ 三道 gate（语法/smoke/指标）→ 通过/回滚      │
│ ← 复用第一/二环:run_train/infer    │      │   ⑥ objective 算分 + memory 记忆                 │
│   + telemetry + diagnose           │      │  子模块: llm_propose / search / objective /     │
└────────────────────────────────────┘      │          mutate / memory                        │
                                             └──────────────────────────────────────────────────┘
```

**数据流**:`optimize.py` **每轮自跑 baseline**(不读磁盘旧 report)→(LLM 提案,受参数边界约束)→(白名单 git 快照)→(应用改动,数值类进贝叶斯内循环)→(三道 gate)→ 调用第一/二环的 `run_infer.sh`/`run_train.sh` + `diagnose.py`,**同种子组配对重复**产出候选 `report.json` →(objective 取均值算分)→ 改善超 `MIN_IMPROVEMENT` 才接受、否则**定向回滚白名单** →(写 memory)→ 下一轮。

**复用既有工具，不重造**：试玩/诊断完全复用第一二环（`run_train.sh` `run_infer.sh` `diagnose.py`）。
本环只新增"决策 + 改动 + 验证编排"。

## 4. 组件设计

### 4.1 游戏侧参数化层（低侵入）

**`rl/tunables.json`**（游戏作者声明哪些数值可被优化器动）：见 §5.1 schema。

**`Tunables` autoload（singleton）**：`_ready` 时读 `res://rl/tunables.json`，提供
`Tunables.get(key, default)`。游戏脚本把硬编码常量替换为 `Tunables.get("gap_width", 120)`。
- 数值改动 = 改 `tunables.json` 的 `value` 字段，下次启动即生效，**无需碰 .gd/.tscn**。这是"安全"与"可回滚"的关键载体，也是阶段 1 的全部改动面。
- **可声明参数的边界(原则 5)**:只有真实游戏设计旋钮可入 `tunables.json`(敌人血量/速度、跳跃力、动态平台尺寸…);reward 系数、`GOAL`/`FALL` 终止几何、telemetry/诊断阈值属测量装置,**禁止**纳入 tunables,也被 protected 护住。
- 模板提供 `template/tunables.json` + `template/tunables.gd`（autoload）+ 注释示范。

### 4.2 `harness/optimize.py`（编排器，新增）

主循环 + CLI/环境变量入口。组合下列子模块，自身不含业务规则细节，便于读懂与测试。
- **启动前置(Gate 0)**:校验 git 工作树干净(`git status --porcelain` 为空),否则拒跑(避免回滚/提交波及开发者在制改动,原则 8)。
- **每轮新鲜 baseline**:不读磁盘旧 report,自跑;校验报告对应当前 `tunables` hash + 配置,且 `episodes >= MIN_EPISODES`,否则拒绝采信(原则 7)。

### 4.3 `harness/llm_propose.py`（LLM 提案，新增）

- 输入：`report.json` + `tunables.json`（schema 部分）+ 相关代码摘要 + `memory.json` + 当前 STAGE。
- 调 Claude API（`anthropic` SDK，最新 Claude 模型；key 走环境变量 `ANTHROPIC_API_KEY`，**绝不入库**）。
- 输出：结构化「改动计划」（§5.2），强制 JSON（用 tool-use / structured output 约束格式，解析失败重试）。
- prompt 内置铁律：提假设而非保证、可选 `tunable_search`/`structural`/`logic` 三型、不得改 protected 路径、参考 memory 里失败教训。STAGE 限制可提的改动类型。

### 4.4 `harness/search.py`（贝叶斯优化，新增）

- 仅处理 `change_type == "tunable_search"`。在 LLM 圈定的参数子集 + 范围上 `minimize(score)`。
- 用 `scikit-optimize`（`gp_minimize`，EI 采集函数）。每次评估 = 写 tunables → **配对重复试玩**(同一组固定种子 ×`EVAL_REPEATS`)→ 诊断 → `objective` **取均值**算分(原则 6),压低随机推理方差。
- **成本控制**（调研 RuleSmith 自适应采样）：每点评估默认走**纯推理**（小数值改动策略有鲁棒性，省去重训）；
  可配 `RETRAIN_EACH=1` 对大改动改为 `WARM_START` 热启动重训。评估预算 `SEARCH_CALLS` 可配。

### 4.5 `harness/objective.py`（客观分数，新增，纯函数）

把 `report.json` 压成一个标量（越小越好），见 §5.4。供贝叶斯目标 + 接受判定共用。纯函数，易单测。

### 4.6 `harness/mutate.py`（应用改动 + git 安全，新增）

- `apply(change)`：数值类写 `tunables.json`；结构/逻辑类按 patch 改 `.tscn`/`.gd`。
- **protected 检查**：应用前对每个目标路径匹配 `PROTECTED_PATHS` glob，命中则**拒绝该改动**并记 memory（用户要求的"不允许修改"入口）。默认护住 `harness/**`、`.git/**`、`tests/**`、`docs/**`、`tunables.json` 的 `range`/`type`/`desc`/`files` 字段（只准改 `value`）。
- `snapshot()` / `rollback()` / `commit()`(原则 8,git 安全,**只作用白名单**):
  - `snapshot(paths)` 只记录**本轮白名单文件**(要动的 tunables/.tscn/.gd)的内容,作为定向回滚锚点。
  - `rollback(snap)` 只把白名单文件还原到锚点(`git checkout -- <files>` 或从 snap 写回),**绝不** `git reset --hard`(那会吞掉开发者在制改动)。
  - `commit(msg, paths)` 只 `git add <白名单>` 再提交,**绝不** `git add -A`。
  - 整个 run 在本仓专用优化分支上跑,主分支不受污染。

### 4.7 记忆（`memory.json`）

每轮一条记录（§5.3），由 `optimize.py` 维护，`llm_propose` 读取。跨 run 累积（按游戏/scene 维度）。

### 4.8 入口脚本 `harness/run_optimize.sh`（新增）

协调器：建/切优化分支 → 跑 baseline（若无 report）→ `python optimize.py` → 收尾打印总结（接受了哪些改动、指标前后对比、剩余 issue）。

## 5. 数据契约（Schema）

### 5.1 `tunables.json`（游戏侧声明）

```jsonc
{
  "version": 1,
  "params": {
    "enemy_hp":       {"value": 3,   "range": [1, 8],     "type": "int",
                       "desc": "敌人血量",          "files": ["res://enemy.gd"]},
    "enemy_speed":    {"value": 120, "range": [60, 220],  "type": "float",
                       "desc": "敌人移动速度(px/s)", "files": ["res://enemy.gd"]},
    "jump_force":     {"value": 400, "range": [300, 600], "type": "float",
                       "desc": "玩家跳跃力",        "files": ["res://player.gd"]},
    "platform_width": {"value": 120, "range": [64, 220],  "type": "float",
                       "desc": "动态平台宽度(px)",  "files": ["res://level.tscn"]}
  }
}
```
优化器只准改 `value`(在 `range` 内);`range`/`type`/`desc`/`files` 是游戏作者契约,受 protected 保护。

**只准声明真实游戏设计参数(原则 5)**:上例的血量/速度/跳跃力/平台尺寸都是玩家能感知的玩法旋钮。**禁止**把以下纳入 tunables —— 它们是测量装置,改了等于篡改尺子:
- **reward 系数**(塑造 agent 被度量的行为 → 改它抬通关率是自欺)
- **`GOAL_X`/`FALL_Y` 等终止判定几何**(定义 episode 怎么结束、坐标系)
- **telemetry 落盘字段、诊断阈值**

### 5.2 LLM 改动计划（`llm_propose` 输出 / `mutate` 输入）

```jsonc
{
  "target_issue": "difficulty_too_hard",      // 针对 report.json 里哪条 issue.id
  "hypothesis": "缺口过宽,当前策略跨不过去",   // 机制解释（Nova schema:结论+机制）
  "change_type": "tunable_search",            // tunable_search | structural | logic
  // change_type == tunable_search:
  "search_space": [{"key": "gap_width", "range": [80, 160]}],  // 圈定贝叶斯搜的子集+收窄范围
  // change_type == structural | logic（阶段2/3）:
  "patches": [{"file": "res://level.tscn", "anchor": "...", "new": "..."}],
  "expected_effect": "completion_rate 提升 / death_hotspot 消除",
  "confidence": 0.7
}
```

### 5.3 `memory.json`（记忆）

```jsonc
{
  "scene": "res://rl/train_map.tscn",
  "rounds": [
    {"round": 3, "target_issue": "difficulty_too_hard", "change_type": "tunable_search",
     "summary": "gap_width 120→96", "score_before": 2.8, "score_after": 1.5,
     "accepted": true,  "reason": "completion 0.1→0.42"},
    {"round": 4, "target_issue": "death_hotspot", "change_type": "logic",
     "summary": "改 player.gd 落地判定", "accepted": false,
     "reason": "syntax gate 失败:缩进错误"}
  ]
}
```

### 5.4 客观分数（`objective.score(report)`，越小越好）

```python
SEV_W = {"high": 3.0, "medium": 1.0, "low": 0.3}
score = w_issue   * sum(SEV_W[i.severity] for i in issues)        \
      + w_diff    * abs(completion_rate - TARGET_COMPLETION)      \
      + w_unstable* max(0, return_cv - unstable_target)
# 默认 w_issue=1.0, w_diff=2.0, w_unstable=0.3; TARGET_COMPLETION 可配(默认 0.65)
```
RuleSmith balance-loss 思路：把多维诊断压成干净标量，供搜索 + 接受判定共用。权重/目标全可配。

**接受判定带噪声余量(原则 6)**:候选取 `EVAL_REPEATS` 次配对评估的均值 `scorē`;只有 `base_score - scorē > MIN_IMPROVEMENT` 才算真变好(裸 `<` 会把随机推理的方差当改善)。

### 5.5 报告新鲜度(provenance,由 `optimize.py` 维护)

为防止拿陈旧/不匹配的 `report.json` 算分(原则 7),编排器**不读磁盘上预存的 report**,每轮自己触发试玩并就地记录产出环境:
- `scene` / `model`(+`mtime`) / `speedup` / `tunables_hash`(产出该报告时 `tunables.json` 的 sha1) / `episodes`(局数) / `eval_seeds`(配对评估固定种子组)。
- **校验**:`episodes >= MIN_EPISODES`(局数不足直接拒绝);baseline 与候选对比必须同 scene/model/speedup。
- 这些字段由 `optimize.py` 在触发试玩时就地计算(它已知 scene/model/speedup,tunables hash 自算),**无需改 `telemetry.gd` 落盘契约**(避免动到上一环的度量定义)。

## 6. 三道安全 Gate（全自动改代码的命门）

**Gate 0(前置,每轮入口)**:git 工作树干净 + 报告新鲜(对应当前 tunables hash、`episodes >= MIN_EPISODES`)。不满足则不进改动(原则 7/8)。

按顺序，任一不过即**定向** `rollback()` 并记 memory，跳过本轮：

| Gate | 检查 | 工具 | 失败含义 |
|---|---|---|---|
| ① 语法 | 改过的 `.gd`/`.tscn` 能解析 | Godot `--check-only`（`.gd`）/ `--headless --import`（场景） | LLM 产了非法代码 |
| ② smoke | 短推理能跑通不崩、能产出 ≥1 真 episode | 复用 `run_infer.sh`（小 `INFER_STEPS`，关诊断） | 改动破坏了可运行性 |
| ③ 指标回归 | 候选 `scorē`(配对重复均值)较 baseline 改善 **> `MIN_IMPROVEMENT`** | `objective.score` | 改动无效/有害/仅噪声 |

数值类（`tunable_search`）天然过 ①（不改代码）；贝叶斯内循环每点都隐含过 ②③（评估即试玩+算分）。
结构/逻辑类必须全部通过。

## 7. 闭环主循环（`optimize.py` 伪码）

```python
ensure_on_optimize_branch()
assert git_worktree_clean()                 # Gate 0:工作树干净,否则拒跑(原则 8)
report = run_baseline()                      # 每轮入口都自跑,不读磁盘旧 report(原则 7)
assert report.episodes >= MIN_EPISODES       # 局数不足的 baseline 不可信
base_score = objective.score(report)
no_improve = 0
for r in range(MAX_ROUNDS):
    if not high_issues(report) or budget_exhausted() or no_improve >= PATIENCE:
        break
    plan = llm_propose(report, tunables, code_summary, memory, stage=STAGE)
    if not mutate.allowed(plan, PROTECTED_PATHS):       # protected + 参数边界(原则 5)入口
        memory.add(plan, accepted=False, reason="protected path"); continue
    paths = plan.target_files()                         # 本轮白名单
    snap = mutate.snapshot(paths)                       # 只快照白名单(原则 8)
    if plan.change_type == "tunable_search":
        best, cand_score = search.optimize(plan.search_space, evaluate)  # 配对重复评估(原则 6)
        cand_report = report_for(best)
    else:                                               # structural / logic
        mutate.apply(plan)
        if not gate_syntax() or not gate_smoke():
            mutate.rollback(snap); memory.add(plan, accepted=False, reason="gate fail"); continue
        cand_report = run_playtest_paired()             # 同种子组 ×EVAL_REPEATS,取均值
        if cand_report.episodes < MIN_EPISODES:
            mutate.rollback(snap); memory.add(plan, accepted=False, reason="insufficient episodes"); continue
        cand_score = objective.score(cand_report)
    if base_score - cand_score > MIN_IMPROVEMENT:       # 指标 gate:改善超噪声余量才接受
        prev = base_score                               # 先存旧值,避免 reason 打成 X→X
        mutate.commit(f"opt r{r}: {plan.summary}", paths)   # 只暂存白名单
        base_score = cand_score; report = cand_report; no_improve = 0
        memory.add(plan, accepted=True, reason=f"score {prev:.2f}→{cand_score:.2f}")
    else:
        mutate.rollback(snap); no_improve += 1          # 定向回滚白名单,不动其它文件
        memory.add(plan, accepted=False, reason="no score improvement")
print_summary()                                         # 接受的改动 / 指标前后 / 剩余 issue
```

## 8. 分阶段实施（架构一次到位，交付按风险递增）

| 阶段 | 改动面（`STAGE`） | 验证目标 | 说明 |
|---|---|---|---|
| **1（MVP）** | 仅 `tunable_search`（数值,且仅真实玩法参数） | 在本仓 `testbed_platformer/` 闭环跑通:LLM 圈参数→贝叶斯搜→配对验证→接受/定向回滚→记忆 | 最安全，跑通整个骨架（提案/搜索/分数/gate/记忆/git） |
| **2** | + `structural`（`.tscn` patch） | 能挪平台/缺口位置并通过三道 gate | 引入语法/smoke gate 的实战考验 |
| **3** | + `logic`（`.gd` patch） | 能改逻辑且 git 回滚兜底有效 | 风险最高，依赖前两阶段验证过的安全网 |

每阶段独立 commit，在本仓 `testbed_platformer/` 端到端验证后再进下一阶段。

## 9. 配置（环境变量 / config）

| 变量 | 默认 | 说明 |
|---|---|---|
| `STAGE` | 1 | 1=数值 / 2=+结构 / 3=+逻辑 |
| `TARGET_COMPLETION` | 0.65 | 目标通关率（客观分数用） |
| `MAX_ROUNDS` | 8 | 闭环最大轮数 |
| `PATIENCE` | 3 | 连续无改善多少轮则停 |
| `SEARCH_CALLS` | 12 | 贝叶斯每轮评估预算 |
| `RETRAIN_EACH` | 0 | 评估是否每次热启动重训（0=纯推理省钱） |
| `EVAL_REPEATS` | 3 | 每点配对重复评估次数(降方差,原则 6) |
| `EVAL_SEEDS` | `1,2,3` | 配对评估固定种子组(baseline 与候选同组) |
| `MIN_IMPROVEMENT` | 0.1 | score 改善须超此余量才接受(防噪声,原则 6) |
| `MIN_EPISODES` | 20 | 报告局数下限,不足则拒绝(原则 7) |
| `PROTECTED_PATHS` | `harness/**,.git/**,tests/**,docs/**` | 禁止修改的 glob（用户要求的入口） |
| `ANTHROPIC_API_KEY` | (必填) | LLM key，环境变量，**绝不入库** |
| 复用 | — | `PROJ`/`SCENE`/`MODEL`/`SPEEDUP`/`WARM_START` 等沿用前两环 |

## 10. 测试策略

- `objective.py`：纯函数，TDD。给合成 report → 验证 score 计算、权重、target 偏移、空 issue 边界。
- `mutate.py`：`allowed()` 的 protected glob 匹配（命中/不命中/range 字段保护）单测；`apply()` 对 tunables 数值写回正确性（用 tmp 文件，不碰 git 的纯逻辑部分）。
- `memory.py`：增删读 + 跨 run 累积单测。
- `llm_propose.py`：解析 LLM 输出的 schema 校验单测（喂合法/非法 JSON）；LLM 调用本身用 mock（不在单测里真调 API）。
- `search.py`：mock `evaluate`(注入噪声)验证配对重复评估能压方差、能收敛近最优(原则 6)。
- 新鲜度:构造 tunables_hash 不匹配 / `episodes < MIN_EPISODES` 的报告,断言被拒绝并记 memory(原则 7)。
- 定向回滚:在含其它"开发者在制改动"的 tmp 仓里,断言 `rollback` 只还原白名单、不动其它文件(对照 `git reset --hard` 会破坏,原则 8)。
- 接受余量:构造仅噪声级改善的候选,断言不被接受(`MIN_IMPROVEMENT`)。
- 端到端：本仓 `testbed_platformer/` 阶段 1 跑一轮真实闭环，核对 git 历史(只动 tunables)、memory.json、score 前后。
- gate：构造一个故意语法错的 patch，断言 ① 拦截 + 定向回滚 + 记 memory。

## 11. 文件清单

| 文件 | 动作 |
|---|---|
| `harness/optimize.py` | 🆕 编排器主循环 |
| `harness/llm_propose.py` | 🆕 LLM 提案（anthropic SDK） |
| `harness/search.py` | 🆕 贝叶斯优化（scikit-optimize） |
| `harness/objective.py` | 🆕 客观分数（纯函数） |
| `harness/mutate.py` | 🆕 应用改动 + protected + git 快照/回滚 |
| `harness/memory.py` | 🆕 记忆读写 |
| `harness/run_optimize.sh` | 🆕 入口协调脚本 |
| `template/tunables.json` | 🆕 参数化示范 |
| `template/tunables.gd` | 🆕 Tunables autoload 示范 |
| `testbed_platformer/` | 🆕 可运行 Godot 测试床纳入本仓(project.godot+场景+rl/) |
| `testbed_platformer/rl/tunables.json` | 🆕 真实玩法参数声明(血量/速度/跳跃/平台) |
| `testbed_platformer/**/*.gd .tscn` | ✏️ 真实玩法常量改 `Tunables.get(...)`(**不碰** reward/终止几何) |
| `tests/test_{objective,mutate,memory,propose,search,optimize}.py` | 🆕 单测+集成(含新鲜度/定向回滚/噪声) |
| `README.md` | ✏️ 补「优化闭环」章节 + 新环境变量 |
| `CLAUDE.md` | ✏️ 进化循环进度更新（优化环✅） |

## 12. 风险与权衡

- **自欺(改尺子而非改游戏)**:最隐蔽的失败。若优化器能动 reward/终止几何/阈值,就能"优化"出虚假改善。缓解:原则 5 硬边界(只准声明真实玩法参数,测量装置 protected)+ 原则 6 噪声余量 + 原则 7 新鲜 baseline,三者共同守住客观锚。
- **成本高**：贝叶斯内循环 × 试玩（甚至重训）+ LLM token,且配对重复评估 ×`EVAL_REPEATS` 进一步放大成本。缓解：默认纯推理评估、`SEARCH_CALLS`/`MAX_ROUNDS` 上限、`PATIENCE` 早停、`EVAL_REPEATS` 可调小、自适应采样。
- **全自动改 .gd 风险**：LLM 可能产出能过语法但语义破坏游戏的代码。缓解：三道 gate + 指标回归 + git 回滚 + memory + 分阶段（阶段 3 才开）。仍残留"过了所有 gate 但悄悄变坏"的尾部风险 → 客观分数是最后防线，建议人事后抽查 git 历史。
- **纯推理评估的偏差**：不重训直接用旧策略评估改动，对"改动大到策略失效"的情形会误判。缓解：`RETRAIN_EACH` 可开；大范围 `search_space` 时编排器自动转重训（启发式，可配阈值）。
- **LLM 提案质量依赖 prompt/模型**：幻觉、乱圈参数。缓解：memory 喂回失败教训、structured output 强约束、客观分数兜底（差就回滚，不靠 LLM 自评）。
- **贝叶斯优化对离散/小预算不友好**：`gp_minimize` 对纯整数参数欠佳。缓解：整数参数用 skopt `Integer` 维度；小预算时退化为随机/网格搜索（可配）。
- **API key 隐私**：`ANTHROPIC_API_KEY` 走环境变量，`.gitignore` 已含 `.env`/`*key*`；提交前自检 `git ls-files | grep -iE 'key|secret|token'` 为空。
- **2D / 单 agent 假设**：沿用前两环。平衡类优化（多策略对战）仍 out of scope。
```
