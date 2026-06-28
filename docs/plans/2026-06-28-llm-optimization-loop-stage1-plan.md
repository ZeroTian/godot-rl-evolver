# LLM 优化闭环阶段 1（数值闭环 MVP）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有阶段 1 骨架修订为一个不会复用旧报告、不会把随机波动当改善、不会修改测量装置、并能在本仓 Godot 测试床安全提交或回滚真实玩法参数的闭环。

**Architecture:** 每个参数点按固定 `EVAL_SEEDS` 分别运行到 `EVAL_EPISODES`，每个 seed 使用独立且启动前为空的 artifact 目录。评估器从该目录读取唯一的新 JSONL，校验 run 头、局数和配置后生成 `RunResult`；搜索和接受门只消费 `EvaluationResult`，并以相同 seed 的候选—baseline 配对差值判定改善。Git 操作只针对 `testbed_platformer/rl/tunables.json` 等明确白名单，运行产物全部位于被忽略的 `.artifacts/opt/`。

**Tech Stack:** Python 3、pytest、stable-baselines3/PyTorch、scikit-optimize、Anthropic SDK、Godot 4/GDScript、Bash、Git。

## Global Constraints

- 阶段 1 只允许 `change_type == "tunable_search"`，只改 `tunables.json.params.*.value`。
- 可调项仅限真实玩法参数；reward、`GOAL_X`、`FALL_Y`、telemetry 和诊断阈值禁止进入 tunables。
- telemetry JSONL 字段契约不变；新鲜度靠独立目录和 run 头绑定实现。
- `EVAL_SEED` 必须同时控制 Python、NumPy、PyTorch、SB3 和 Godot `--env_seed`。
- 有效子评估必须满足 `report.summary.n_episodes >= EVAL_EPISODES`；步数或超时先到则失败。
- 禁止 `git reset --hard` 和 `git add -A`；提交、回滚只作用白名单路径。
- `.artifacts/`、Godot import 缓存、telemetry、报告、memory、模型和日志不得入库。
- Windows Godot 从 WSL 启动时必须 `cd "$PROJ"` 后使用 `--path .`；涉及时序的运行固定 `--fixed-fps 60`。

---

## 0. 当前状态与文件边界

现有骨架已经提交：`objective.py`、`memory.py`、`mutate.py`、`llm_propose.py`、`search.py`、`optimize.py`、`run_optimize.sh` 和对应测试。本计划只修订缺陷并加入本仓测试床。

| 文件 | 职责 |
|---|---|
| `harness/evaluation.py` | 新增结果类型、hash、JSONL/run 头验证、配对改善 |
| `harness/infer_rl.py` | 完整 RNG seed 链、按 episode 停止、步数上限 |
| `harness/run_infer.sh` | 向 Python/Godot 透传 seed 和评估预算；不替优化器选择 `latest` |
| `harness/optimize.py` | 独立 artifact、baseline 生命周期、搜索与接受门编排 |
| `harness/mutate.py` | 白名单快照、定向回滚、定向提交 |
| `harness/llm_propose.py` | 阶段/参数 schema 校验；拒绝测量装置 |
| `harness/run_optimize.sh` | Git Gate 0、分支、artifact/memory 默认路径、参数透传 |
| `testbed_platformer/` | 本仓可运行 Godot 项目；模型仍由显式 `MODEL` 外部路径提供 |
| `.artifacts/opt/runs/<run-id>/` | 单次 run 的 telemetry/report，忽略不入库 |
| `.artifacts/opt/memory/<scene-hash>.json` | 跨 run 本地记忆，忽略不入库 |

## 1. 固定接口

后续任务必须使用以下签名，避免各模块各自发明类型：

```python
# harness/evaluation.py
from dataclasses import dataclass
from statistics import fmean

@dataclass(frozen=True)
class RunResult:
    seed: int
    telemetry_path: str
    run_id: str
    report: dict
    score: float
    provenance: dict

@dataclass(frozen=True)
class EvaluationResult:
    runs: tuple[RunResult, ...]

    @property
    def by_seed(self) -> dict[int, RunResult]:
        out = {run.seed: run for run in self.runs}
        if len(out) != len(self.runs):
            raise ValueError("duplicate evaluation seed")
        return out

    @property
    def mean_score(self) -> float:
        if not self.runs:
            raise ValueError("evaluation contains no runs")
        return fmean(run.score for run in self.runs)

    @property
    def representative_report(self) -> dict:
        if not self.runs:
            raise ValueError("evaluation contains no runs")
        return min(self.runs, key=lambda run: run.seed).report
```

其余固定签名：

- `sha256_file(path: str) -> str`
- `validate_telemetry(path: str, *, scene: str, model: str, speedup: int, min_episodes: int) -> tuple[dict, str]`
- `paired_improvement(base: EvaluationResult, candidate: EvaluationResult) -> float`
- `run_one_seed(cfg: Config, *, seed: int, artifact_dir: str) -> RunResult`
- `evaluate_current(cfg: Config, *, point_id: str) -> EvaluationResult`
- `make_evaluator(cfg: Config) -> Callable[[dict], EvaluationResult]`
- `snapshot(paths: list[str], repo_root: str = ".") -> dict[str, bytes]`
- `rollback(snapshot_data: dict[str, bytes], repo_root: str = ".") -> None`
- `commit(msg: str, paths: list[str], repo_root: str = ".") -> None`

`search.optimize()` 继续返回 `(best_point, best_value)`；其中 `best_value` 改为对应点的 `EvaluationResult`，内部用于 GP 的标量是 `best_value.mean_score`。

---

### Task 1: 建立本仓 testbed 与运行产物边界

**Files:**
- Create: `testbed_platformer/`（来源 `/mnt/e/code/godot-study/platformer/`）
- Create: `testbed_platformer/PROVENANCE.md`
- Create: `testbed_platformer/rl/tunables.json`
- Create: `testbed_platformer/rl/tunables.gd`
- Modify: `testbed_platformer/project.godot`
- Modify: `testbed_platformer/rl/game_env.gd`
- Modify: `testbed_platformer/scenes/enemies/fire_knight/run_state.gd`
- Modify: `testbed_platformer/scenes/player/states/jump.gd`
- Modify: `.gitignore`

**Interfaces:**
- Produces three stage-1 tunables: `enemy_hp:int`, `enemy_speed:float`, `jump_force:float`。
- Does not produce `platform_width`;当前地图没有动态平台消费接口，避免声明一个实际不生效的旋钮。

- [ ] **Step 1: 清点来源与许可证**

Run:
```bash
find /mnt/e/code/godot-study/platformer -maxdepth 3 \
  \( -iname 'license*' -o -iname 'copying*' -o -iname 'credits*' \) -print
```
Expected: 把发现的许可证逐项记录到 `testbed_platformer/PROVENANCE.md`；没有明确再分发许可的第三方二进制素材不得提交，须换成仓内可分发的 grey-box 资源。`PROVENANCE.md` 同时记录来源路径、复制日期和源版本说明。

- [ ] **Step 2: 复制最小可运行项目**

复制 `project.godot`、`addons/godot_rl_agents/`、`rl/`、训练场景直接引用的 `scenes/` 与获准素材；排除 `.godot/`、`.import/`、`rl/telemetry/`、日志、截图、模型。完成后运行：
```bash
rg -n '/mnt/e/code/godot-study|res://.*\.zip' testbed_platformer
```
Expected: 无绝对源路径或入库模型引用。

- [ ] **Step 3: 注册 Tunables 并只接真实玩法参数**

`testbed_platformer/rl/tunables.json` 固定为：
```json
{
  "version": 1,
  "params": {
    "enemy_hp": {"value": 40, "range": [20, 100], "type": "int", "desc": "火骑士生命值", "files": ["res://rl/game_env.gd"]},
    "enemy_speed": {"value": 50.0, "range": [25.0, 100.0], "type": "float", "desc": "火骑士巡逻速度", "files": ["res://scenes/enemies/fire_knight/run_state.gd"]},
    "jump_force": {"value": 360.0, "range": [280.0, 440.0], "type": "float", "desc": "玩家起跳速度", "files": ["res://scenes/player/states/jump.gd"]}
  }
}
```

将三个硬编码分别替换成 `Tunables.get("enemy_hp", 40)`、`Tunables.get("enemy_speed", 50.0)`、`Tunables.get("jump_force", 360.0)`；不得修改 `game_agent.gd` 的 reward、`GOAL_X`、`FALL_Y`。

- [ ] **Step 4: 忽略运行产物**

在 `.gitignore` 增加：
```gitignore
.artifacts/
testbed_platformer/.godot/
testbed_platformer/.import/
```

- [ ] **Step 5: 验证 testbed 可导入且脚本可解析**

Run:
```bash
cd testbed_platformer
/mnt/d/Godot/Godot_console.exe --headless --path . --import 2>&1 | grep -iE 'SCRIPT ERROR|Compile Error|Failed to load script'
```
Expected: `--import` 退出码 0 且 grep **无输出**(它会加载 autoload 编译全部脚本,包括引用 `Tunables` 的三个参数化文件)。
> ⚠️ 不要用 `--check-only res://x.gd`(裸形式会被当主场景运行而**卡死**);也不要对引用 `Tunables` 的脚本用 `--check-only --script`(autoload 未加载会误报 `Identifier not found: Tunables`)。项目级校验一律用 `--import`。
> ✅ 已验证:`--import` rc=0,无脚本编译错误。

- [ ] **Step 6: Commit**

```bash
git add .gitignore testbed_platformer
git commit -m "feat(opt): add in-repo platformer testbed"
```

---

### Task 2: 定义 EvaluationResult 与真实性校验

**Files:**
- Create: `harness/evaluation.py`
- Create: `tests/test_evaluation.py`

**Interfaces:** 使用 §1 的 `RunResult`、`EvaluationResult`、`validate_telemetry()` 和 `paired_improvement()`。

- [ ] **Step 1: Write failing tests**

测试必须实现以下五个具体用例：`test_validate_rejects_wrong_run_header` 构造 scene 不匹配的 run 首行并断言 `ValueError`；`test_validate_rejects_summary_n_episodes_below_target` 构造 1 局并以 `min_episodes=2` 校验；`test_validate_requires_report_run_id_to_match_header` 篡改 report run_id；`test_paired_improvement_requires_same_seed_set` 使用 `{1,2}` 对 `{1,3}`；`test_paired_improvement_uses_seed_matched_differences` 用 seed 1/2 的已知分数断言精确均值。

`validate_telemetry()` 读取 JSONL 首行并调用现有 `diagnose.load_jsonl/aggregate/diagnose/build_report`；必须使用精确键 `report["summary"]["n_episodes"]`。

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest tests/test_evaluation.py -q`

Expected: FAIL，原因是 `harness/evaluation.py` 或目标符号尚不存在。

- [ ] **Step 3: Implement minimal module**

约束：

- `by_seed` 遇重复 seed 抛 `ValueError`。
- `representative_report` 选 seed 排序后的第一份报告，仅供 LLM 阅读，不参与均值合并。
- `validate_telemetry` 校验 run 头的 `scene/model/speedup`，构造报告并校验 `run_id` 与 `n_episodes`。
- `paired_improvement` 要求 seed 集合完全相同，返回 `mean(base.score - candidate.score)`。

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_evaluation.py tests/test_diagnose.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add harness/evaluation.py tests/test_evaluation.py
git commit -m "feat(opt): bind evaluation results to telemetry runs"
```

---

### Task 3: 打通 seed 与按 episode 停止链路

**Files:**
- Modify: `harness/infer_rl.py`
- Modify: `harness/run_infer.sh`
- Create: `tests/test_infer_rl.py`

**Interfaces:**
- Consumes: `EVAL_SEED`、`EVAL_EPISODES`、`MAX_EVAL_STEPS`。
- Produces: 进程退出码 0 仅表示达到 episode 目标；不足时非 0。

- [ ] **Step 1: Write failing pure tests**

把 `infer_rl.py` 改成 import-safe（网络连接只在 `main()` 中发生），实现三个测试：`test_seed_everything_sets_python_numpy_torch` monkeypatch 三个 seed 函数并断言均收到同一个值；`test_should_stop_only_after_episode_target` 喂入 done 序列并断言达到目标后才停；`test_step_budget_fails_before_episode_target` 断言预算耗尽抛 `RuntimeError`。

- [ ] **Step 2: Verify RED**

Run: `~/.local/share/godot-rl-venv/bin/python -m pytest tests/test_infer_rl.py -q`

Expected: FAIL，缺少 `seed_everything()`/episode 计数逻辑。

- [ ] **Step 3: Implement RNG and episode budget**

`seed_everything(seed)` 必须调用：
```python
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
```

加载模型后调用 `model.set_random_seed(seed)`；环境构造把 `seed=seed` 传给 `StableBaselinesGodotEnv`。循环累计 `np.count_nonzero(done)`，达到 `EVAL_EPISODES` 后再推进一个 reset 握手 step；若 `MAX_EVAL_STEPS` 先到则 `raise RuntimeError` 并非 0 退出。

`run_infer.sh` 必须把同一个值同时传给 Python 环境和 Godot：
```bash
EVAL_SEED=$EVAL_SEED EVAL_EPISODES=$EVAL_EPISODES MAX_EVAL_STEPS=$MAX_EVAL_STEPS \
  python "$HARNESS/infer_rl.py"
( cd "$PROJ" && "$GODOT" --path . "$SCENE" --port=$PORT \
  --speedup=$SPEEDUP --env_seed=$EVAL_SEED )
```

- [ ] **Step 4: Verify GREEN**

Run: `~/.local/share/godot-rl-venv/bin/python -m pytest tests/test_infer_rl.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add harness/infer_rl.py harness/run_infer.sh tests/test_infer_rl.py
git commit -m "feat(opt): make inference seeded and episode-bounded"
```

---

### Task 4: 独立 artifact 与配对评估器

**Files:**
- Modify: `harness/optimize.py`
- Modify: `harness/search.py`
- Modify: `tests/test_search.py`
- Modify: `tests/test_optimize.py`

**Interfaces:** 实现 §1 的 `run_one_seed()`、`evaluate_current()`、`make_evaluator()`。

- [ ] **Step 1: Write freshness and pairing failures**

新增四个测试：`test_run_one_seed_does_not_fall_back_to_old_latest` 在共享目录预置旧文件、让本次空目录无输出并断言失败；`test_run_one_seed_requires_exactly_one_new_jsonl` 分别覆盖 0 个和 2 个文件；`test_evaluate_current_passes_same_seed_order_for_every_point` 断言每个点收到完全相同顺序；`test_paired_delta_variance_is_lower_than_unpaired_delta` 按下一段公式验证配对差值。

最后一个测试使用确定性伪噪声 `score(x, seed) = (x-3)**2 + seed*0.1`；同 seed 作差应抵消 `seed*0.1`，非配对 seed 作差则不能。不要使用真实随机数，避免 flaky test。

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_search.py tests/test_optimize.py -q`

Expected: FAIL，现有 evaluator 仍只返回单个 float 且会读取共享 report。

- [ ] **Step 3: Implement isolated run directories**

`run_one_seed()` 必须：

1. 拒绝已存在的 `artifact_dir`，随后创建空目录及其 `telemetry/`。
2. 在启动前计算 model/tunables SHA-256。
3. 以 `DIAGNOSE=0`、独立 `TELEMETRY_DIR` 和指定 seed 调 `run_infer.sh`，并施加 `EVAL_TIMEOUT_SECONDS`。
4. 结束后要求 telemetry 目录中恰好一个 `run_*.jsonl`；0 个或多个都失败。
5. 调 Task 2 的 `validate_telemetry()` 诊断该确切文件，禁止搜索其它目录。
6. 返回带启动前 hash 的 `RunResult`。

`evaluate_current()` 对 `cfg.eval_seeds` 逐个调用并返回 `EvaluationResult(tuple(runs))`。`search.optimize()` 给 GP 的目标值是 `EvaluationResult.mean_score`，同时保存每个已评估点对应的完整结果并返回最优结果对象。

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_evaluation.py tests/test_search.py tests/test_optimize.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add harness/optimize.py harness/search.py tests/test_search.py tests/test_optimize.py
git commit -m "feat(opt): add isolated paired evaluations"
```

---

### Task 5: 定向 Git 与路径 containment

**Files:**
- Modify: `harness/mutate.py`
- Modify: `tests/test_mutate.py`

**Interfaces:** 使用 §1 的 `snapshot/rollback/commit`；所有路径先解析为 repo-relative，并要求 `realpath` 位于 `repo_root` 内。

- [ ] **Step 1: Write failing integration tests**

在 tmp git 仓同时修改 `allowed.json` 和 `developer.txt`，验证：

- snapshot/rollback 只恢复 `allowed.json`。
- commit 只暂存并提交 `allowed.json`，`developer.txt` 仍未暂存。
- `../outside.json`、绝对仓外路径和指向仓外的 symlink 均抛 `ValueError`。
- source 中不再出现 `reset --hard` 或 `add -A`。

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_mutate.py -q`

Expected: FAIL，旧实现仍使用 HEAD 快照和宽泛 Git 操作。

- [ ] **Step 3: Implement minimal targeted operations**

`snapshot()` 读取每个白名单文件的 bytes；`rollback()` 用原子写恢复 bytes；`commit()` 逐路径执行 `git add -- <path>` 后提交。运行前验证路径 containment，禁止修改仓外文件。

- [ ] **Step 4: Verify GREEN and scan**

```bash
python -m pytest tests/test_mutate.py -q
rg -n 'reset.*--hard|add.*-A' harness/mutate.py
```

Expected: 测试 PASS；`rg` 无输出。

- [ ] **Step 5: Commit**

```bash
git add harness/mutate.py tests/test_mutate.py
git commit -m "fix(opt): restrict git operations to mutation whitelist"
```

---

### Task 6: 参数边界与 LLM schema

**Files:**
- Modify: `harness/llm_propose.py`
- Modify: `harness/mutate.py`
- Modify: `template/tunables.json`
- Modify: `tests/test_propose.py`
- Modify: `tests/test_mutate.py`

**Interfaces:** `parse_plan(text, tunables, stage=1)` 只接受 tunables 中存在的 key、合法子范围和阶段允许的 change type。

- [ ] **Step 1: Write failing tests**

覆盖未知 key、反向范围、超出作者 range、重复 key、stage 1 的 structural/logic，以及示例中禁止出现 `reward_*`、`goal_*`、`fall_*`、`telemetry_*`、`diagnose_*`。合法 `enemy_hp/enemy_speed/jump_force` 放行。

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_propose.py tests/test_mutate.py -q`

Expected: 至少 stage 和重复 key 用例失败。

- [ ] **Step 3: Implement minimal validation**

安全边界以作者提供的 `tunables.params` 白名单为主；禁用前缀是防误配置的第二道保险。prompt 只展示合法 params，不展示 reward/终止测量常量。同步删掉 `template/tunables.json` 中 reward 参数和未被模板真实消费的虚构平台参数。

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_propose.py tests/test_mutate.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add harness/llm_propose.py harness/mutate.py template/tunables.json tests/test_propose.py tests/test_mutate.py
git commit -m "fix(opt): enforce gameplay-only tunable plans"
```

---

### Task 7: 主循环 baseline 生命周期与接受门

**Files:**
- Modify: `harness/optimize.py`
- Modify: `tests/test_optimize.py`

**Interfaces:** `optimize_loop()` 的 baseline/candidate 均为 `EvaluationResult`；接受条件固定为 `paired_improvement(base, candidate) > cfg.min_improvement`。

- [ ] **Step 1: Write failing loop tests**

覆盖：

- run 开始必调用一次新 baseline，不读取 `REPORT_PATH`。
- 接受后 candidate 成为下一轮 baseline，不重复跑同一点。
- 拒绝后 tunables 定向回滚，baseline 仍对应回滚后的 hash。
- seed 集不一致、episode 不足、0/多个 JSONL 均记失败且不计分。
- 改善等于阈值不接受；严格大于才接受。
- memory reason 使用赋值前后的真实 `mean_score`，不得出现 `X→X`。
- 每轮边界若出现白名单外 tracked 改动立即中止。

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_optimize.py -q`

Expected: FAIL，旧循环仍复用磁盘报告并比较单次 float。

- [ ] **Step 3: Implement minimal loop**

配置新增并严格校验：

```python
eval_seeds: tuple[int, ...]        # 非空、无重复
eval_episodes: int                 # > 0
max_eval_steps: int                # >= eval_episodes
eval_timeout_seconds: int          # > 0
min_improvement: float             # >= 0
artifact_root: str
```

每轮提交路径在阶段 1 固定为 repo-relative 的 `testbed_platformer/rl/tunables.json`；memory 写入 `.artifacts/opt/memory/<scene-hash>.json`，不加入 commit。

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_optimize.py tests/test_evaluation.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add harness/optimize.py tests/test_optimize.py
git commit -m "fix(opt): use paired fresh baselines in optimization loop"
```

---

### Task 8: 入口脚本、Gate 0 与配置透传

**Files:**
- Modify: `harness/run_optimize.sh`
- Create: `tests/test_run_optimize.py`

**Interfaces:** 默认 `PROJ=$REPO_ROOT/testbed_platformer`；显式要求 `MODEL` 存在；生成 `OPT_RUN_ID`，并设置 artifact/memory 路径。

- [ ] **Step 1: Write shell contract tests**

使用临时假 `git/python` 命令验证：脏工作树在创建/切换分支前退出；缺模型退出；新变量全部透传；默认 memory 位于 `.artifacts/opt/memory/` 而非 testbed tracked 目录。

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_run_optimize.py -q`

Expected: FAIL，旧脚本仍默认外部 PROJ 且缺少新变量。

- [ ] **Step 3: Implement entry contract**

默认值：

```bash
EVAL_SEEDS=1,2,3
EVAL_EPISODES=20
MAX_EVAL_STEPS=40000
EVAL_TIMEOUT_SECONDS=900
MIN_IMPROVEMENT=0.1
ARTIFACT_ROOT=$REPO_ROOT/.artifacts/opt
```

先检查 `git status --porcelain`，再建/切 `opt/auto-*` 分支。明确打印模型 SHA-256、run id、artifact 路径和分支名。

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_run_optimize.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add harness/run_optimize.sh tests/test_run_optimize.py
git commit -m "fix(opt): harden optimization entrypoint"
```

---

### Task 9: testbed baseline 与最小端到端

**Files:**
- Runtime only: `.artifacts/opt/`
- Possible fix files: only files already listed in Tasks 1–8

**Interfaces:** `MODEL` 是显式外部依赖；它的 SHA-256 进入每个 `RunResult.provenance`。testbed 入仓不等于模型入仓。

- [ ] **Step 1: Baseline compatibility gate**

Run:
```bash
PROJ="$PWD/testbed_platformer" \
SCENE=res://rl/train_map.tscn \
MODEL="$HOME/.local/share/godot-rl-venv/ppo_game.zip" \
EVAL_SEED=1 EVAL_EPISODES=2 MAX_EVAL_STEPS=5000 \
TELEMETRY_DIR="$PWD/.artifacts/opt/manual-smoke/telemetry" \
DIAGNOSE=0 bash harness/run_infer.sh
```

Expected: 退出码 0，独立目录恰好一个 JSONL，至少 2 条 `type:"episode"`。失败则先修 testbed 兼容性，禁止调用 LLM。

- [ ] **Step 2: Verify exact-run diagnosis**

对 Step 1 的确切 JSONL 运行 `diagnose.py --out`，断言 `report.run_id` 匹配首行且 `summary.n_episodes >= 2`。

- [ ] **Step 3: Run one cheap closed-loop round**

```bash
MODEL="$HOME/.local/share/godot-rl-venv/ppo_game.zip" \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
MAX_ROUNDS=1 SEARCH_CALLS=3 EVAL_SEEDS=1 EVAL_EPISODES=2 \
MAX_EVAL_STEPS=5000 bash harness/run_optimize.sh
```

Expected: 仅用于连通性，不据此宣称统计改善；分支提交至多只包含 `testbed_platformer/rl/tunables.json`，memory/report 位于 `.artifacts/`。

- [ ] **Step 4: Run acceptance-grade evaluation**

恢复 `EVAL_SEEDS=1,2,3`、`EVAL_EPISODES=20` 后再执行正式一轮。Expected: 每个 seed 都满足 episode 目标；接受或拒绝均有配对差值、模型/tunables hash 和确切 JSONL 可追溯。

- [ ] **Step 5: Full regression**

Run: `~/.local/share/godot-rl-venv/bin/python -m pytest tests/ -q`

Expected: 全绿。

---

### Task 10: 文档、凭据扫描与最终提交

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `.gitignore`（若 Task 1 未完整覆盖）

- [ ] **Step 1: Update docs**

记录 testbed 路径、外部 `MODEL` 前提、固定种子链、`EVAL_EPISODES/MAX_EVAL_STEPS/EVAL_TIMEOUT_SECONDS`、artifact 目录及“环境变量名不是秘密”的规则。

- [ ] **Step 2: Scan filenames and credential values**

Run:
```bash
git ls-files | rg -i '(^|/)(\.env($|\.)|.*cookies.*|.*secret.*|.*\.key$)'
git grep -nE 'sk-ant-[A-Za-z0-9_-]{20,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
```

Expected: 两条均无输出。`ANTHROPIC_API_KEY` 这个变量名允许出现在文档、脚本和测试中。

- [ ] **Step 3: Final verification**

```bash
~/.local/share/godot-rl-venv/bin/python -m pytest tests/ -q
git status --short
```

Expected: 测试全绿；只剩本任务预期文档改动，`.artifacts/` 不出现。

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md .gitignore
git commit -m "docs(opt): document reproducible stage1 loop"
```

## 完成定义

- 所有单测通过，testbed Godot import/check-only 通过。
- 本次失败不会回退到旧 telemetry；0/多个新 JSONL、header 不符或 episode 不足都会失败关闭。
- baseline 与 candidate 的 seed 集完全相同，正式接受以配对改善均值为准。
- 优化 run 只提交 tunables 白名单；memory/telemetry/report 均被忽略。
- reward、终止几何、telemetry、诊断阈值没有进入 tunables，也没有被优化器修改。
- 指定外部模型可在 testbed 完成 baseline，并有模型 hash、tunables hash、run_id 和精确 JSONL 路径可追溯。
