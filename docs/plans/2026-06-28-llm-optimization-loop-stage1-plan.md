# 实施计划 · LLM 优化闭环 阶段1(数值闭环 MVP)

> 日期: 2026-06-28 ｜ 状态: **pending approval**(待用户批准执行)
> 设计依据: `docs/specs/2026-06-28-llm-optimization-loop-design.md`(spec 已通过 brainstorming + 用户复审)
> 范围: spec §8 表的**阶段1** —— 仅 `tunable_search`(数值)改动,跑通整个闭环骨架(提案/搜索/分数/三道 gate/记忆/git 回滚)。结构(.tscn)/逻辑(.gd)改动属阶段2/3,本计划不实现,但架构为其预留接口。

## 1. 需求摘要

把上一环产出的 `report.json` 喂给 LLM,LLM 提出"该搜哪些数值参数 + 收窄范围"的假设,贝叶斯优化在该空间内搜最优值,**改完再试玩验证**,客观分数真变好才接受(否则 git 回滚),全程记忆。阶段1 只动 `tunables.json` 的数值(不碰代码),是最安全、能验证整套骨架的 MVP。

## 2. 关键前提与决策(基于代码现状核实)

| 事项 | 现状/决策 | 证据 |
|---|---|---|
| 端到端测试床 | **`/mnt/e/code/godot-study/platformer`**(真实可跑的 Godot 项目),非 `example_platformer` | `example_platformer/` 无 `project.godot`,是参考副本,只能 `--check-only` |
| `example_platformer` 角色 | 仅做"接入示范同步"+ 语法校验,不做端到端 | 同上 |
| Python 依赖 | 需在 `~/.local/share/godot-rl-venv` 装 `anthropic`、`scikit-optimize` | venv 现无这两个模块(已核实) |
| autoload 注册 | 须改测试床 `project.godot` 的 `[autoload]` 段加 `Tunables` | autoload 必须在 project.godot 注册 |
| 数值参数候选 | reward 系数(平衡)+ 关卡几何(难度) | `game_agent.gd:8-12,157,164,169,190,193,199` |
| 评估策略 | 阶段1 默认**纯推理评估**(`RETRAIN_EACH=0`):改数值后用现有策略试玩,反映"会玩的玩家在新难度下的体验" | spec §4.4 |
| LLM 模型 | 最新 Claude(`claude-opus-4-8` 或同代),`anthropic` SDK,key 走 `ANTHROPIC_API_KEY` 环境变量,绝不入库 | CLAUDE.md 隐私规则 |
| 计划/spec 路径 | 入库交付物放 `docs/specs/` + `docs/plans/` | 用户指示 |

## 3. 验收标准(可测)

- [ ] **AC1** `python -m pytest tests/ -q` 全绿,新增 `test_objective.py`/`test_memory.py`/`test_mutate.py`/`test_propose.py` 各覆盖其纯函数与边界(空 report、缺字段、protected 命中/不命中、非法 LLM JSON)。
- [ ] **AC2** `objective.score(report)` 对"高分难度(多 high issue + 通关率远离 target)"返回值 **严格大于** "改善后报告"的返回值(单测断言具体数值)。
- [ ] **AC3** `mutate.allowed(plan, protected)` 对命中 `harness/**`、`tunables.json` 的 `range` 字段的改动返回 False;对 `value` 字段改动返回 True(单测)。
- [ ] **AC4** `llm_propose` 能把一段合法 LLM JSON 解析成改动计划对象;喂非法/缺 `change_type` 的 JSON 时抛带原因的 `ValueError`(单测,API 调用 mock,不真调)。
- [ ] **AC5** 三道 gate 可独立验证:构造一个会让 `objective.score` 变差的 tunables 改动,`optimize.py` 一轮后该改动被 `git reset` 回滚、`memory.json` 记一条 `accepted:false, reason:"no score improvement"`(集成测试,可用桩报告)。
- [ ] **AC6** 端到端(测试床):`bash harness/run_optimize.sh`(STAGE=1)从 baseline 跑 ≥1 轮真实闭环,结束后:① git 优化分支有 commit 历史;② `memory.json` 含本轮记录;③ 终端打印"接受的改动 + score 前后 + 剩余 issue"总结。
- [ ] **AC7** `--check-only` 通过:`template/tunables.gd`、测试床改动后的 `game_agent.gd`、`example_platformer` 同步副本均语法合法。
- [ ] **AC8** `git ls-files | grep -iE 'key|secret|token|\.env'` 为空;`ANTHROPIC_API_KEY` 不出现在任何入库文件。

## 4. 实施步骤(TDD 优先,纯函数先行)

> 顺序按依赖排:先无依赖的纯函数(可 TDD、可单测),再编排器,最后游戏侧接入与端到端。

### Step 0 · 脚手架与依赖
- `~/.local/share/godot-rl-venv/bin/pip install anthropic scikit-optimize`(装仓库外 venv,不入库)。
- 确认测试床 `godot-study/platformer` 可跑前两环(`run_infer.sh` 能产 telemetry + diagnose)。
- 新建优化工作分支约定写进 `run_optimize.sh`(见 Step 7)。

### Step 1 · `harness/objective.py`(纯函数,TDD)
- **先** `tests/test_objective.py`:喂合成 report(多 high issue / 空 issue / 缺 `summary` 字段 / 不同 completion_rate)→ 断言 `score` 数值(SEV_W={high:3,med:1,low:0.3},w_issue=1,w_diff=2,w_unstable=0.3,target 默认 0.65,可传参)。
- **后** 实现 `score(report, weights=None, target=0.65)`,纯函数,容错缺字段。对应 spec §5.4。

### Step 2 · `harness/memory.py`(纯函数,TDD)
- **先** `tests/test_memory.py`:`add_round`/`load`/按 `scene` 过滤/跨 run 累积(写 tmp 文件)。
- **后** 实现读写 `memory.json`(schema 见 spec §5.3)。

### Step 3 · `harness/mutate.py`(protected+apply 走 TDD,git 部分集成验)
- **先** `tests/test_mutate.py`:`allowed(plan, protected_globs)` 的 glob 匹配(命中 `harness/**`、`range` 字段保护、`value` 放行);`apply_tunable(path, key, value)` 写回 tmp `tunables.json` 且 clamp 到 `range`。
- **后** 实现 `allowed` / `apply`(数值写回);`snapshot()`/`rollback()`/`commit(msg)` 用 `git`(subprocess 封装,这部分由 Step 9 集成验证,不单测)。protected 默认 `harness/**,.git/**,tests/**,docs/**` + `tunables.json` 仅准改 `value`。

### Step 4 · `harness/llm_propose.py`(解析 TDD,API mock)
- **先** `tests/test_propose.py`:`parse_plan(text)` 把合法 JSON → 改动计划;非法/缺 `change_type`/`search_space` 越界(超出 tunables.range)→ `ValueError`。LLM 调用用 mock。
- **后** 实现:组 prompt(注入 report + tunables schema + memory + STAGE 约束"阶段1只准 tunable_search")→ `anthropic` SDK structured output → `parse_plan`。prompt 内置铁律(提假设、参考失败记忆、不碰 protected)。对应 spec §4.3/§5.2。

### Step 5 · `harness/search.py`(贝叶斯)
- 封装 `skopt.gp_minimize`:把 `search_space`(LLM 圈定)映射成 `Real`/`Integer` 维度;`evaluate(point)` 回调由 optimize 注入(写 tunables→试玩→诊断→objective);`SEARCH_CALLS` 预算;小预算(<8)退化随机采样。
- 冒烟测试:用 mock `evaluate`(返回简单二次函数)断言能收敛到近最优点。

### Step 6 · `harness/optimize.py`(编排器主循环)
- 实现 spec §7 伪码:baseline→LLM 提案→git 快照→protected 检查→(数值类)贝叶斯内循环→三道 gate→objective 比分→接受 commit / 回滚→memory。
- 三道 gate:① 语法 `--check-only`(数值类天然过)② smoke 小步 `run_infer`(贝叶斯每点隐含过)③ 指标 `objective.score` 不差于 baseline。
- 早停:`MAX_ROUNDS`/`PATIENCE`/无 high issue/预算耗尽。配置经环境变量(spec §9)。

### Step 7 · `harness/run_optimize.sh`(入口协调)
- 建/切优化分支 → 若无 `report.json` 先跑 baseline(`run_infer.sh`+`diagnose.py`)→ `python optimize.py` → 收尾总结。沿用 `PROJ/SCENE/MODEL/SPEEDUP` 等前两环变量。

### Step 8 · 游戏侧参数化
- `template/tunables.json` + `template/tunables.gd`(autoload:`_ready` 读 `res://rl/tunables.json`,`get(key,default)`)+ 注释示范。
- 测试床 `godot-study/platformer`:`project.godot` 注册 `Tunables` autoload;`game_agent.gd` 关键常量(reward 系数 + GOAL_X/FALL_Y 等)由硬编码改 `Tunables.get(...)`(注意 `const`→`var` 或 `_ready` 读取);新建 `rl/tunables.json` 声明这些参数 + range。
- `example_platformer/` 同步对应改动(仅示范 + `--check-only`,不端到端)。

### Step 9 · 端到端验证(阶段1)
- 测试床跑 `bash harness/run_optimize.sh`(STAGE=1,小 `MAX_ROUNDS`/`SEARCH_CALLS` 省钱)。
- 核对 AC5/AC6:git 历史、memory.json、score 前后、报告 issue 是否改善;故意构造劣化改动验回滚。

### Step 10 · 文档
- `README.md`:补「优化闭环」章节 + 新环境变量(STAGE/TARGET_COMPLETION/MAX_ROUNDS/PATIENCE/SEARCH_CALLS/RETRAIN_EACH/PROTECTED_PATHS/ANTHROPIC_API_KEY)。
- `CLAUDE.md`:进化循环进度更新(优化环 阶段1 ✅)。

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 端到端依赖测试床 godot-study(本仓外) | 计划已明确测试床路径;骨架/单测全在本仓,不依赖测试床即可跑(AC1-5) |
| 贝叶斯每点试玩慢/花钱 | 默认纯推理评估、`SEARCH_CALLS`/`MAX_ROUNDS` 小、`PATIENCE` 早停;端到端验证用最小预算 |
| LLM 圈出越界/无效参数 | `parse_plan` 校验 search_space ⊆ tunables.range;objective 兜底(差就回滚);memory 喂回失败 |
| anthropic/skopt 装失败或 venv PATH 漂移 | Step 0 显式装;脚本用 venv 绝对路径 python(沿用前两环习惯) |
| 改 const→var 破坏 game_agent 语法 | AC7 `--check-only` gate;改动小而局部 |
| API key 泄露 | 环境变量 + .gitignore 已含 key/.env;AC8 自检 |
| git 回滚污染主分支 | 全程在优化分支跑,主分支不动(Step 7) |

## 6. 验证步骤(完成时执行)

1. `~/.local/share/godot-rl-venv/bin/python -m pytest tests/ -q` → 全绿(AC1-5)。
2. 测试床 `--check-only` 三个 .gd(AC7)。
3. 测试床端到端跑一轮(AC6)+ 劣化改动回滚验证(AC5)。
4. `git ls-files | grep -iE 'key|secret|token|\.env'` 为空(AC8)。
5. 人工抽查 git 优化分支历史 + memory.json,确认决策可追溯。

## 7. 交付物清单(阶段1)

新增:`harness/{objective,memory,mutate,llm_propose,search,optimize}.py`、`harness/run_optimize.sh`、`template/tunables.json`、`template/tunables.gd`、`tests/test_{objective,memory,mutate,propose}.py`。
改动:`example_platformer/*`(同步示范)、`README.md`、`CLAUDE.md`。
测试床(godot-study/platformer,本仓外):`project.godot`、`game_agent.gd`、`rl/tunables.json`。

## 8. 不在阶段1范围(后续)

- `structural`(.tscn patch)/ `logic`(.gd patch)改动 → 阶段2/3。
- 重训评估为主、自适应采样调优、多策略平衡优化、可视化 History。
