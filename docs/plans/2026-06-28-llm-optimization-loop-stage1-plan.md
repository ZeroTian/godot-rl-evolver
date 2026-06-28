# 实施计划 · LLM 优化闭环 阶段1(数值闭环 MVP)——「测量完整性」修订版

> 日期: 2026-06-28 ｜ 状态: **pending approval**(待用户批准执行)
> 设计依据: `docs/specs/2026-06-28-llm-optimization-loop-design.md`(已据「测量完整性」复盘修订)
> 范围: spec §8 表的**阶段1** —— 仅 `tunable_search`(数值,且仅真实玩法参数)改动,跑通整个闭环骨架。结构(.tscn)/逻辑(.gd)改动属阶段2/3,本计划不实现,但架构为其预留接口。

## 0. 本计划的性质:修订,不是从零

阶段1 的骨架**已建成并提交**(`objective/memory/mutate/llm_propose/search/optimize.py` + `run_optimize.sh` + `template/tunables.*` + 106 例测试)。本次复盘发现 5 个测量完整性/安全缺陷,本计划是**把已有实现改造到新设计**,外加测试床纳入本仓 + 端到端。逐任务标注「改造已有/全新」。

**复盘确认的 5 个真实缺陷(均有代码证据)**:
1. `mutate.rollback` = `git reset --hard`、`commit` = `git add -A` → **会吞掉开发者在制改动**(`harness/mutate.py:130,135`)。
2. 接受门是裸 `new_score < base_score`,无噪声余量 → 把随机推理方差当改善(`optimize.py`/spec §7)。
3. 复用磁盘旧 `report.json`(`load_or_run_baseline`)→ 拿陈旧/不匹配报告算分。
4. 候选参数含 reward 系数 + `GOAL_X`/`FALL_Y` → **改测量尺子而非改游戏 = 自欺**(原计划 line 19/74)。
5. spec §7 伪码 `base_score = new_score` 重赋值在 reason 串之前 → 日志打成 "score X→X"(已在 spec 修正)。

## 1. 需求摘要

把上一环 `report.json` 喂给 LLM → LLM 提"搜哪些**真实玩法参数** + 收窄范围"的假设 → 贝叶斯在该空间搜最优值 → **配对重复试玩验证** → 客观分数改善**超过噪声余量**才接受(否则**定向**回滚) → 全程记忆。阶段1 只动 `tunables.json` 的真实玩法参数 `value`,是最安全、能验证整套骨架的 MVP。

## 2. 关键前提与决策(基于代码现状 + 复盘)

| 事项 | 现状/决策 | 依据 |
|---|---|---|
| 端到端测试床 | **纳入本仓 `testbed_platformer/`**(可运行 Godot 项目),取代外部 `godot-study/platformer` | 外部项目非 git 仓,闭环无法做 git 快照/回滚;纳入本仓后 git 只作用本仓、自包含可复现 |
| git 作用域 | **只作用于本仓**;启动前要求工作树干净;提交只暂存白名单、回滚只恢复白名单 | spec 原则 8;防吞掉开发者在制改动 |
| 可调参数边界 | **仅真实游戏设计旋钮**(敌人血量/速度、跳跃力、动态平台尺寸);**禁** reward 系数、`GOAL`/`FALL` 终止几何、telemetry、诊断阈值 | spec 原则 5;测量完整性 |
| 噪声防护 | baseline 与候选用**同一组固定种子**配对评估 ×`EVAL_REPEATS`,均值比分;改善须 > `MIN_IMPROVEMENT` | spec 原则 6 |
| baseline 新鲜度 | 每轮自跑 baseline;报告带 provenance(scene/model/speedup/tunables hash/episodes);拒陈旧/空/局数不足 | spec 原则 7、§5.5 |
| telemetry 契约 | **不动** `telemetry.gd` 落盘契约;provenance 由 `optimize.py` 就地计算 | 避免改上一环的度量定义 |
| Python 依赖 | venv 在仓外(`~/.local/share/godot-rl-venv`),**无 pip**,须用 `uv pip install --python <venv>/bin/python anthropic scikit-optimize` | uv 建的 venv 无 pip(已踩坑) |
| LLM 模型 | 最新 Claude(`claude-opus-4-8` 或同代),`anthropic` SDK,key 走 `ANTHROPIC_API_KEY` 环境变量,绝不入库 | CLAUDE.md 隐私规则 |

## 3. 验收标准(每条可独立测,对应一个或多个 TDD 任务)

- [ ] **AC1** `~/.local/share/godot-rl-venv/bin/python -m pytest tests/ -q` 全绿。
- [ ] **AC2(定向 git,缺陷1)** 在含其它未提交改动的 tmp git 仓里:`mutate.snapshot(paths)`/`rollback(snap)`/`commit(msg,paths)` **只**作用白名单文件;断言回滚后**其它文件的未提交改动原样保留**(对照旧 `reset --hard` 会丢)。无 `reset --hard`/`add -A`。
- [ ] **AC3(噪声余量,缺陷2)** 构造仅噪声级改善(< `MIN_IMPROVEMENT`)的候选 → 不被接受、记 memory `reason` 含 "no score improvement";改善超余量 → 接受。
- [ ] **AC4(配对评估,原则6)** `search`/`evaluate` 包装:同一参数点用固定种子组 ×`EVAL_REPEATS` 评估取均值;mock 注入噪声时,配对均值的方差显著小于单次(单测断言)。
- [ ] **AC5(新鲜度,缺陷3/原则7)** `episodes < MIN_EPISODES` 或 tunables_hash 不匹配的报告被拒绝(不进比分),记 memory;每轮 baseline 为自跑(不读磁盘旧 report)。
- [ ] **AC6(参数边界,缺陷4/原则5)** LLM 计划若 `search_space` 命中非真实玩法参数(reward/`GOAL_X`/`FALL_Y`/阈值,或目标文件命中测量装置)→ `parse_plan`/`allowed` 拒绝并带原因;合法玩法参数放行。
- [ ] **AC7(计分日志,缺陷5)** 接受轮 memory `reason` 打印**真实前后值**(`score A→B`, A≠B 当改善存在),非 "X→X"。
- [ ] **AC8(测试床+参数化)** `testbed_platformer/` 是可运行 Godot 项目,注册 `Tunables` autoload,真实玩法常量改 `Tunables.get(...)`(**未碰** reward/`GOAL`/`FALL`);`--check-only` 通过。
- [ ] **AC9(端到端)** 本仓跑 `bash harness/run_optimize.sh`(STAGE=1)≥1 轮:① 工作树非干净时拒跑;② git 优化分支 commit **只动 `testbed_platformer/rl/tunables.json`**;③ memory.json 含本轮记录;④ 终端打印接受改动 + score 前后 + 剩余 issue。
- [ ] **AC10** `git ls-files | grep -iE 'key|secret|token|\.env'` 为空;`ANTHROPIC_API_KEY` 不出现在任何入库文件。

## 4. TDD 任务拆分(可独立验收;每任务先写失败测试再实现)

> 顺序按依赖排。每个任务自带 RED(失败测试)→ GREEN(最小实现)→ verify(重跑测试)三步,可独立合并。

### T1 · `mutate.py` 定向 git(改造已有)→ AC2
- **RED** `tests/test_mutate.py` 新增:在 tmp git 仓里同时改白名单文件与"开发者文件",`snapshot([白名单])`→改两者→`rollback(snap)` 后断言白名单复原、开发者文件改动**仍在**;`commit(msg,[白名单])` 后 `git status` 显示开发者文件仍未暂存。
- **GREEN** `snapshot(paths)` 存白名单内容/blob;`rollback` 用 `git checkout -- <paths>`(或写回 snap 内容);`commit(msg, paths)` 用 `git add <paths>`。删除 `reset --hard`/`add -A`。

### T2 · 配对重复评估 `search.py`/`evaluate`(改造已有)→ AC4
- **RED** `tests/test_search.py` 新增:`evaluate` 包装对同一点用固定种子组评估 N 次取均值;mock 一个"真值+噪声"的打分,断言配对均值方差 < 单次、且能收敛近最优。
- **GREEN** 在 `optimize.make_evaluator` 注入 `EVAL_SEEDS`×`EVAL_REPEATS`,每点多次试玩取均值;种子组对 baseline 与候选**复用同一组**(配对)。

### T3 · 报告新鲜度/provenance(全新小模块)→ AC5
- **RED** `tests/test_freshness.py`:`report_provenance(report)`/`is_fresh(report, tunables_hash, min_episodes)` 对局数不足、hash 不匹配返回 False 并给原因;充分则 True。
- **GREEN** `harness/freshness.py`(纯函数):算 tunables sha1、读 `report.summary.episodes`(或聚合局数)、比对。`optimize.py` 触发试玩时就地附 provenance。

### T4 · 接受余量 + 计分日志(改造已有)→ AC3/AC7
- **RED** `tests/test_optimize.py` 改/增:仅噪声级改善 → `accepted False`/"no score improvement";真改善 → `accepted True` 且 `reason` 为 `score A→B`(A≠B,先存 `prev` 再赋值)。
- **GREEN** 接受门改 `base_score - cand_score > MIN_IMPROVEMENT`;赋值前存 `prev = base_score`,reason 用 `prev`。

### T5 · 参数边界(改造已有)→ AC6
- **RED** `tests/test_propose.py`/`test_mutate.py`:`search_space`/目标文件命中 reward/`GOAL_X`/`FALL_Y`/阈值 → `parse_plan` 或 `allowed` 拒绝并带原因;真实玩法参数放行。
- **GREEN** `llm_propose` prompt 注入"只准真实玩法参数、禁测量装置"铁律;`parse_plan` 校验 `search_space.key` ∈ tunables.params;`PROTECTED_PATHS` 默认补测量装置文件(若有)。

### T6 · `optimize.py` 主循环修订(改造已有)→ AC1/AC5
- **RED** `tests/test_optimize.py`:断言每轮 baseline 为自跑(mock `run_baseline` 被调用,不读磁盘旧 report);Gate 0 工作树非干净时拒跑;局数不足的 baseline 触发拒绝路径。
- **GREEN** 把 `load_or_run_baseline` 改为每轮 `run_baseline`;入口 `assert git_worktree_clean()`;集成 T1/T2/T3/T4 的调用(白名单快照、配对评估、新鲜度、余量门)。

### T7 · 测试床纳入本仓(全新)→ AC8
- 把可运行 platformer vendoring 到 `testbed_platformer/`(project.godot + 训练场景 + `rl/`),首次作为 baseline 提交。
- 注册 `Tunables` autoload(改 project.godot `[autoload]`);拷 `template/tunables.gd` → `testbed_platformer/rl/tunables.gd`。
- 新建 `testbed_platformer/rl/tunables.json` 声明**真实玩法参数**(敌人血量/速度、跳跃力、动态平台尺寸)。
- 把对应**真实玩法常量**改 `Tunables.get(...)`(注意 `const`→`var` 或 `_ready` 读取);**不碰** reward 系数 / `GOAL_X` / `FALL_Y` / telemetry。
- **验证**:`--check-only` 改过的 `.gd` 语法合法。

### T8 · `run_optimize.sh` 修订(改造已有)→ AC9
- `REPO_ROOT` 指向本仓;`TUNABLES_PATH`/`MEMORY_PATH` 默认指 `testbed_platformer/rl/`。
- 启动前 `git status --porcelain` 非空则拒跑并提示。
- 在本仓建/切优化分支;透传新环境变量(`EVAL_REPEATS`/`EVAL_SEEDS`/`MIN_IMPROVEMENT`/`MIN_EPISODES`)。

### T9 · 端到端(本仓 testbed)→ AC9
- 跑 `bash harness/run_optimize.sh`(STAGE=1,小 `MAX_ROUNDS`/`SEARCH_CALLS`/`EVAL_REPEATS` 省钱)。
- 核对 AC9:拒跑(脏工作树)、git 只动 tunables、memory.json、score 前后;再故意构造劣化改动验**定向**回滚不伤其它文件。

### T10 · 文档同步 → AC10
- `README.md`:补/改环境变量(`EVAL_REPEATS`/`EVAL_SEEDS`/`MIN_IMPROVEMENT`/`MIN_EPISODES`)+ 参数边界说明 + 测试床改为本仓。
- `CLAUDE.md`:进化循环进度、测试床路径(`testbed_platformer/`)、可调参数边界。
- 提交前自检 `git ls-files | grep -iE 'key|secret|token|\.env'` 为空。

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| git 作用本仓 → 误伤开发者改动 | T1 定向 git(只动白名单)+ T8 干净工作树前置;AC2 专测 |
| 测试床 vendoring 带入二进制资源、仓库变大 | 只纳入跑通所需最小资源;`.gitignore` 排除运行产物(telemetry/日志/gif) |
| 配对重复评估放大成本 | `EVAL_REPEATS` 可调小、纯推理评估、`SEARCH_CALLS`/`MAX_ROUNDS` 上限、`PATIENCE` 早停 |
| `const`→`var` 破坏 testbed 语法 | AC8 `--check-only` gate;改动小而局部,只动真实玩法常量 |
| LLM 仍尝试圈测量装置参数 | T5 prompt 铁律 + `parse_plan`/`allowed` 双重拒绝 + memory 喂回 |
| API key 泄露 | 环境变量 + `.gitignore` 含 key/.env;AC10 自检 |

## 6. 验证步骤(完成时执行)

1. `~/.local/share/godot-rl-venv/bin/python -m pytest tests/ -q` → 全绿(AC1-7)。
2. testbed `--check-only` 改过的 `.gd`(AC8)。
3. 本仓端到端跑一轮(AC9)+ 脏工作树拒跑 + 劣化改动定向回滚验证。
4. `git ls-files | grep -iE 'key|secret|token|\.env'` 为空(AC10)。
5. 人工抽查 git 优化分支历史(确认只动 tunables)+ memory.json,决策可追溯。

## 7. 交付物清单(阶段1 修订)

改造:`harness/{mutate,search,optimize}.py`、`harness/run_optimize.sh`、`tests/test_{mutate,search,optimize,propose}.py`。
新增:`harness/freshness.py`、`tests/test_freshness.py`、`testbed_platformer/`(可运行项目 + `rl/tunables.json` + `rl/tunables.gd`)。
不变:`harness/{objective,memory,llm_propose}.py` 主体(objective 的 `medium` 已正确,无需改)。
文档:`README.md`、`CLAUDE.md`、本 spec(已修订)。

## 8. 不在阶段1范围(后续)

- `structural`(.tscn patch)/ `logic`(.gd patch)改动 → 阶段2/3。
- 重训评估为主、自适应采样调优、多策略平衡优化、可视化 History。
