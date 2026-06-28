# LLM 优化闭环阶段 2（结构闭环 · `.tscn` patch）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在阶段 1 数值闭环之上，打通 `change_type == "structural"`：让 LLM 能对 `.tscn` 提
**anchor 文本 patch**，经「应用 → ① 语法 gate（`--import`）→ ② smoke gate（≥1 真 episode）→ ③ 指标回归」
四步后安全接受或定向回滚。验证目标（spec §8 阶段 2）：**能挪测试床里的踏脚石平台位置并通过三道 gate**；
改动是否真降分由指标 gate 诚实裁决，本阶段不要求结构改动一定带来统计改善。

**Architecture:** 结构改动**无贝叶斯内循环**（patch 是离散文本操作，不是连续数值搜索）——一次提案 = 一个
候选。流程：`snapshot(白名单)` → `mutate.apply_patch` 逐条改 `.tscn` → 语法 gate（Godot `--import`，
rc=0 且无 `SCRIPT ERROR`/`Parse Error`）→ smoke gate（`SMOKE_MAX_STEPS` 内出 ≥1 episode）→
`evaluate_current` 对 `EVAL_SEEDS` 配对评估 → `paired_improvement(base, candidate) > MIN_IMPROVEMENT`
才 `commit(被 patch 的 .tscn)`，否则 `rollback`。白名单从「阶段 1 写死的单个 tunables.json」**泛化**为
「本轮 plan 声明的目标文件集」（`res://` 经 PROJ↔REPO_ROOT 映射成 repo-relative）。

**Tech Stack:** Python 3、pytest、stable-baselines3/PyTorch、scikit-optimize（阶段 2 不用，仅
tunable_search 复用）、Claude（anthropic SDK 或 claude CLI 后端）、Godot 4/GDScript、Bash、Git。

## Global Constraints

- 阶段 2 允许 `change_type ∈ {tunable_search, structural}`；`logic`（`.gd` patch）仍属阶段 3，拒绝。
- **测量完整性硬边界不变（spec §5 原则 5），且必须防御纵深**：structural patch **绝不能**触碰
  `game_agent.gd` 的 `GOAL_X`/`FALL_Y`/reward、telemetry 落盘、诊断阈值。`GoalFlag` 节点位置与
  `GOAL_X=1520` 耦合，**禁止**作为结构旋钮；缺口在 `ground.tscn` 的二进制 `PackedByteArray` 里，
  不作 patch 目标。**防御纵深(critic C1/C2/M4 必修)**:这条边界不能只靠 `parse_plan` 一处把守——
  必须三层冗余:① `parse_plan` 拒绝触碰 protected 的 patch;② `mutate.allowed()` 扩展为也遍历
  `plan["patches"]` 的 `file`(经 res://→repo 映射)做 protected glob 匹配;③ `mutate.apply_patch`
  写文件前对目标路径再做一次 protected 匹配,命中即拒写。三层任一独立生效即守住尺子。
- **默认 `PROTECTED_PATHS` 必须点名测量装置文件(critic C2)**:现默认
  `harness/**,.git/**,tests/**,docs/**` **不覆盖** `testbed_platformer/rl/game_agent.gd`。阶段2 默认
  集追加测量装置 glob:`*/rl/game_agent.gd,*/rl/telemetry.gd,*/rl/recorder.gd`(`run_optimize.sh`
  与 `optimize.py`/`mutate.py` 默认值同步)。因目标游戏各异,protected 集须显式点名该游戏的测量文件。
- 结构旋钮 = 新增的灰盒**踏脚石平台** `MidPlatform`（`StaticBody2D`+`CollisionShape2D`，`collision_layer=1`
  与地面同层，玩家 `collision_mask=1` 能踩），其 `position` 行是唯一被 patch 的难度元素。
- patch 是 **anchor 精确文本替换**：anchor 必须在目标文件中**恰好出现一次**（0 次=未命中，>1 次=歧义，
  均拒绝 + 定向回滚 + 记 memory）。**anchor 须含节点身份上下文(critic M1)**:不能只取
  `position = Vector2(600, 40)` 这类裸行(多个节点都有 position,未来加第二个平台即歧义);anchor 取
  **节点声明行 + position 行的多行块**,例
  `[node name="MidPlatform" type="StaticBody2D" parent="."]\nposition = Vector2(600, 40)`,保证唯一可定位。
- 三道 gate 任一不过 → `mutate.rollback(snap)`（只还原本轮白名单）+ 记 memory + 跳过本轮；**禁止**
  `git reset --hard`/`git add -A`。
- 白名单与提交粒度按本轮 plan 目标文件动态确定；Gate 0b 越界检查须放行本轮白名单 + `.artifacts/`。
- telemetry JSONL 字段契约不变；新鲜度仍靠独立空目录 + run 头绑定（阶段 1 的 `run_one_seed` 原样复用）。
- Windows Godot 从 WSL 启动一律 `cd "$PROJ"` 后 `--path .`；语法 gate 用 `--import`（不要 `--check-only`）。

---

## 0. 当前状态与文件边界

阶段 1 已交付并端到端验证（commit 至 `dad0dc1`）。阶段 2 在其上扩展，**不回改**阶段 1 已固化的
evaluation/infer/search/run_infer 语义。

| 文件 | 阶段 2 职责 |
|---|---|
| `harness/mutate.py` | 🆕 `apply_patch(path, anchor, new, repo_root)` anchor 精确替换；`target_files(plan, ...)` 解析白名单 |
| `harness/gates.py` | 🆕 `syntax_gate(cfg)`（Godot `--import`）+ `smoke_gate(cfg)`（≥1 episode）|
| `harness/optimize.py` | ✏️ structural 分支（无贝叶斯、四步 gate）；白名单泛化；Gate 0b 放行动态白名单；`res://`↔repo 映射 |
| `harness/llm_propose.py` | ✏️ stage-2 prompt 注入 `.tscn` 结构摘要 + MidPlatform anchor；`parse_plan` 校验 structural patches |
| `testbed_platformer/rl/train_map.tscn` | ✏️ 新增 `MidPlatform` 踏脚石平台节点 + `plat_shape` 子资源 |
| `tests/test_mutate.py` | ✏️ apply_patch（命中/未命中/歧义/containment）|
| `tests/test_gates.py` | 🆕 语法 gate 拦截坏 .tscn、smoke gate（mock subprocess）|
| `tests/test_propose.py` | ✏️ stage-2 structural 放行 / 阶段1 仍拒 / patch 触碰 protected 拒 |
| `tests/test_optimize.py` | ✏️ structural 分支：应用→gate→评估→接受提交被 patch 文件；gate 失败回滚 |
| `harness/run_optimize.sh` | ✏️ 透传 `SMOKE_MAX_STEPS`/`SMOKE_TIMEOUT_SECONDS`；STAGE 可为 2 |
| `docs/specs/...llm-optimization-loop-design.md` | ✏️（如需）补 MidPlatform 结构旋钮的具体落点 |
| `README.md` / `CLAUDE.md` | ✏️ 阶段 2 用法 + 新环境变量 + 进度更新 |

## 1. 固定接口

后续任务必须使用以下签名，避免各模块各自发明类型：

```python
# harness/mutate.py（阶段 2 新增；阶段 1 的 snapshot/rollback/commit/apply_tunable 不变）
def apply_patch(path: str, anchor: str, new: str, repo_root: str = ".",
                protected_globs: list[str] | None = None) -> None:
    """对 path 做 anchor 精确文本替换：anchor 必须恰好出现一次，替换为 new；
    原子写；路径须在 repo_root 内（复用 _check_path_in_repo）。
    防御纵深第③层(critic C1):若 protected_globs 给定且目标路径命中,拒写。
    Raises:
        ValueError: anchor 出现 0 次（未命中）或 >1 次（歧义）；路径越界；命中 protected。
        FileNotFoundError: path 不存在。
    """

def allowed(plan: dict, protected_globs: list[str], *, proj_rel: str = "") -> bool:
    """阶段 1 行为不变(查 field/files);阶段2 扩展(critic C1):
    若 plan 含 'patches',遍历每条 patch['file'](res:// 经 proj_rel 映射成 repo-relative),
    命中 protected_globs 任一即返回 False。proj_rel 缺省时仅按原样路径匹配。"""

def target_files(plan: dict, *, proj_rel: str) -> list[str]:
    """从 plan 解析本轮白名单（repo-relative 路径列表）。
    - tunable_search → [<tunables 的 repo-relative 路径>]（由调用方传入，见 optimize）
    - structural/logic → 每个 patch.file 的 res:// 经 proj_rel 映射成 repo-relative
    proj_rel 例 'testbed_platformer'；'res://rl/x.tscn' → 'testbed_platformer/rl/x.tscn'。
    安全(critic M5):映射后必须仍在 proj_rel 目录内;含 '..' 段或越出 proj 的 res:// 路径抛 ValueError。
    多 patch 指向同文件去重保序。
    """
```

```python
# harness/optimize.py（阶段 2:run_one_seed 增可选覆盖,critic C3 —— smoke_gate 才能复用）
def run_one_seed(cfg, *, seed: int, artifact_dir: str,
                 min_episodes: int | None = None,
                 max_eval_steps: int | None = None) -> evaluation.RunResult:
    """阶段 1 行为不变(min_episodes/max_eval_steps 默认 None → 用 cfg.eval_episodes/cfg.max_eval_steps)。
    阶段2:smoke_gate 传 min_episodes=1、max_eval_steps=cfg.smoke_max_steps,得到廉价 ≥1 局评估。
    覆盖值同时作用于:传给 run_infer 的 EVAL_EPISODES/MAX_EVAL_STEPS 环境变量,与 validate_telemetry 的 min_episodes。"""
```

```python
# harness/gates.py（新增）
def syntax_gate(cfg) -> tuple[bool, str]:
    """Godot --headless --path . --import：rc==0 且 stdout/stderr 无
    'SCRIPT ERROR' / 'Parse Error' / 'Failed to load script' 即通过。
    返回 (passed, detail)；detail 供 memory.reason。"""

def smoke_gate(cfg) -> tuple[bool, str]:
    """在独立空 artifact 目录里以 EVAL_SEEDS[0]、SMOKE_MAX_STEPS 跑一次 run_infer，
    要求 telemetry 恰好 1 个 run_*.jsonl 且 summary.n_episodes >= 1 即通过。
    调 optimize.run_one_seed(min_episodes=1, max_eval_steps=cfg.smoke_max_steps);为避免循环 import,
    smoke_gate 内**函数局部** import optimize(critic M3)。"""
```

```python
# harness/optimize.py（structural 分支辅助）
def apply_structural(cfg: Config, plan: dict, paths: list[str]) -> None:
    """逐条 mutate.apply_patch 应用 plan['patches']（res:// 映射成 repo 路径）。"""
```

- `optimize_loop()` 接受门、baseline 生命周期、memory schema 全部沿用阶段 1；structural 分支只是在
  `tunable_search` 分支**之外**新增一条「无搜索、四步 gate」路径，复用同一 `evaluate_current`/
  `paired_improvement`/`commit`/`rollback`。
- structural 候选评估只调一次 `evaluate_current(cfg, point_id="structural_r{r}")`（无贝叶斯）。

---

### Task 1: 测试床新增 MidPlatform 踏脚石平台

**Files:**
- Modify: `testbed_platformer/rl/train_map.tscn`

**Interfaces:** 产出一个可被 anchor patch 的结构旋钮：`MidPlatform` 节点的 `position` 行。
**不**新增观测、**不**改 reward/终止几何，故无需重训模型（纯推理评估仍有效）。

- [ ] **Step 1: 加平台子资源 + 节点**

在 `train_map.tscn` 增加（紧邻已有 `wall_shape` 子资源后、节点区合适位置）：
```gdscript
[sub_resource type="RectangleShape2D" id="plat_shape"]
size = Vector2(120, 24)
```
并在节点区（建议放在 `LeftWall` 之后、`Player` 之前）新增：
```gdscript
[node name="MidPlatform" type="StaticBody2D" parent="."]
position = Vector2(600, 40)
collision_layer = 1
collision_mask = 0

[node name="MidPlatShape" type="CollisionShape2D" parent="MidPlatform"]
shape = SubResource("plat_shape")
```
注意：`load_steps` 从 `9` 改为 `10`（新增一个 sub_resource；critic m1:给出字面值,且 `--import`
**未必**捕获 load_steps 计错,不可只靠 Gate ① 验这条）。`collision_layer=1` 与地面同层，玩家
`collision_mask=1` 能踩；`position=Vector2(600, 40)` 落在缺口区下沿附近作为踏脚石初值。
**唯一性**:确认 `Vector2(600, 40)` 与现有四处 `position`(Player 80,-30 / LeftWall 50,-120 /
GoalFlag 1520,0 / Background 800,-40)均不同值;且阶段2 anchor 取多行块(节点行+position 行),
**绝不**以 `GoalFlag` 的 `position = Vector2(1520, 0)` 为 patch 目标(与 GOAL_X 耦合)。

- [ ] **Step 2: 验证 testbed 可导入**

Run（项目级，加载 autoload 编译全部脚本并退出）：
```bash
( cd testbed_platformer && /mnt/d/Godot/Godot_console.exe --headless --path . --import 2>&1 ) ; echo "rc=$?"
```
Expected: rc=0 且输出无 `SCRIPT ERROR`/`Parse Error`/`Failed to load script`。新平台不引用脚本，
不应引入编译错误。

- [ ] **Step 3: Commit**
```bash
git add testbed_platformer/rl/train_map.tscn
git commit -m "feat(opt): add MidPlatform structural knob to testbed (stage2)"
```

---

### Task 2: `mutate` patch/白名单/protected 防御纵深（TDD）

**Files:**
- Modify: `harness/mutate.py`
- Modify: `tests/test_mutate.py`

**Interfaces:** §1 的 `apply_patch()`、`target_files()`、扩展后的 `allowed()`。

- [ ] **Step 1: Write failing tests**

`test_mutate.py` 新增：
- `test_apply_patch_replaces_unique_anchor`：tmp 文件含唯一(多行)anchor → 替换为 new，断言内容变更且仅该处变。
- `test_apply_patch_rejects_missing_anchor`：anchor 不存在 → `ValueError`，文件**不变**。
- `test_apply_patch_rejects_ambiguous_anchor`：anchor 出现 2 次 → `ValueError`（歧义），文件**不变**。
- `test_apply_patch_path_containment`：`../outside.tscn` / 仓外绝对路径 / 越界 symlink 均 `ValueError`。
- `test_apply_patch_refuses_protected`(critic C1 第③层)：`protected_globs` 命中目标路径 → `ValueError`，文件**不变**。
- `test_allowed_rejects_patch_touching_protected`(critic C1 第②层)：structural plan 的
  `patches[].file='res://rl/game_agent.gd'` + `proj_rel='testbed_platformer'` +
  protected 含 `*/rl/game_agent.gd` → `allowed()` 返回 `False`。合法 `train_map.tscn` patch → `True`。
- `test_target_files_maps_res_paths`：`patches[].file='res://rl/train_map.tscn'` +
  `proj_rel='testbed_platformer'` → `['testbed_platformer/rl/train_map.tscn']`；多 patch 同文件去重。
- `test_target_files_rejects_escape`(critic M5)：`res://../../harness/x.py` 或含 `..` 段 → `ValueError`。
- `test_target_files_tunable_search`：tunable_search 计划返回调用方传入的 tunables 路径。

- [ ] **Step 2: Verify RED**
Run: `python -m pytest tests/test_mutate.py -q` → FAIL（符号不存在 / `allowed` 不看 patches）。

- [ ] **Step 3: Implement minimal**

`apply_patch`：读文件文本 → `count = text.count(anchor)`；`count==0`→`ValueError("anchor 未命中")`，
`count>1`→`ValueError("anchor 歧义：出现 N 次")`；写前 `_check_path_in_repo` 校验 containment，
**再**(若传入 `protected_globs`)对 repo-relative 路径做 `_matches_any_glob`,命中即 `ValueError`;
`text.replace(anchor, new, 1)` 后原子写（复用 `tempfile.mkstemp`+`os.replace`）。
`allowed`：保留现有 field/files 检查;**追加**遍历 `plan.get("patches", [])`,把每条 `file`
经 `_res_to_repo(file, proj_rel)` 映射后做 protected glob 匹配,命中返回 `False`。
`target_files`：structural/logic 遍历 `plan["patches"]`,`_res_to_repo` 映射(去 `res://` 拼 `proj_rel`),
拒绝含 `..` 段或映射后不在 `proj_rel` 内的路径(`ValueError`),去重保序。

- [ ] **Step 4: Verify GREEN + 扫描**
```bash
python -m pytest tests/test_mutate.py -q
rg -n 'reset.*--hard|add.*-A' harness/mutate.py   # 仍应无输出
```
Expected: PASS；rg 无输出。

- [ ] **Step 5: Commit**
```bash
git add harness/mutate.py tests/test_mutate.py
git commit -m "feat(opt): anchor patching + whitelist + patches-aware protected guard"
```

---

### Task 3: 语法 / smoke 两道 gate（TDD）

**Files:**
- Create: `harness/gates.py`
- Create: `tests/test_gates.py`

**Interfaces:** §1 的 `syntax_gate(cfg)` / `smoke_gate(cfg)`。

- [ ] **Step 1: Write failing tests**

先在 `tests/test_optimize.py` 加 `test_run_one_seed_honors_episode_and_step_overrides`(critic C3)：
monkeypatch 出 run_infer/telemetry,断言传入 `min_episodes=1, max_eval_steps=999` 时,
透传给子进程的 `EVAL_EPISODES=1`/`MAX_EVAL_STEPS=999` 且 `validate_telemetry` 收到 `min_episodes=1`;
不传时仍用 `cfg.eval_episodes`/`cfg.max_eval_steps`(阶段1 零回归)。
再写 `tests/test_gates.py`（monkeypatch 假 subprocess / 假 run_one_seed，**不真起 Godot**）：
- `test_syntax_gate_passes_on_clean_import`：假 subprocess rc=0、stdout 无错误标记 → `(True, ...)`。
- `test_syntax_gate_fails_on_script_error`：stdout 含 `SCRIPT ERROR` → `(False, detail)`，detail 含该行。
- `test_syntax_gate_fails_on_nonzero_rc`：rc≠0 → `(False, ...)`。
- `test_smoke_gate_passes_with_one_episode`：monkeypatch `optimize.run_one_seed` 返回
  `report.summary.n_episodes>=1` 的假 RunResult → `(True, ...)`，且断言它以 `min_episodes=1` 调用。
- `test_smoke_gate_fails_when_no_episode`：run_one_seed 抛 RuntimeError（局数不足）→ `(False, detail)`。
- `test_no_circular_import`：`python -c "import gates, optimize"` 等价断言两模块可共存导入。

- [ ] **Step 2: Verify RED**
Run: `python -m pytest tests/test_gates.py tests/test_optimize.py -q` → FAIL。

- [ ] **Step 3: Implement minimal**

先**重构 `run_one_seed`**(critic C3):签名增 `min_episodes=None, max_eval_steps=None`,
`eff_ep = min_episodes if min_episodes is not None else cfg.eval_episodes`(max_eval_steps 同理);
env 的 `EVAL_EPISODES`/`MAX_EVAL_STEPS` 与 `validate_telemetry(min_episodes=...)` 全用 eff 值。
`syntax_gate`：`subprocess.run([GODOT,"--headless","--path",".","--import"], cwd=cfg.proj,
capture_output=True, text=True, timeout=cfg.smoke_timeout_seconds)`；`bad = rc!=0 or 任一标记
in (stdout+stderr)`，标记集 `{"SCRIPT ERROR","Parse Error","Failed to load script"}`。
`smoke_gate`：**函数局部** `import optimize`(critic M3:打破循环),在
`<artifact_root>/runs/<run_id>/smoke/seed_<s>` 调 `optimize.run_one_seed(cfg, seed=cfg.eval_seeds[0],
artifact_dir=..., min_episodes=1, max_eval_steps=cfg.smoke_max_steps)`,异常即不过。Config 增
`smoke_max_steps`（默认 2000）、`smoke_timeout_seconds`（默认 120）。`optimize.py` 对 gates 的默认引用
也必须**函数局部** import(见 Task 5),两侧一致才无环。

- [ ] **Step 4: Verify GREEN**
Run: `python -m pytest tests/test_gates.py tests/test_optimize.py -q` 且
`~/.local/share/godot-rl-venv/bin/python -c "import sys; sys.path.insert(0,'harness'); import gates, optimize"` → 均 OK。

- [ ] **Step 5: Commit**
```bash
git add harness/gates.py harness/optimize.py tests/test_gates.py tests/test_optimize.py
git commit -m "feat(opt): syntax (--import) and smoke gates + run_one_seed budget overrides"
```

---

### Task 4: `llm_propose` stage-2 structural 校验 + prompt 摘要（TDD）

**Files:**
- Modify: `harness/llm_propose.py`
- Modify: `tests/test_propose.py`

**Interfaces:** `parse_plan(text, tunables, stage=2)` 接受 structural 计划并校验 patches；
`_build_prompt` 在 stage≥2 注入 `.tscn` 结构摘要。

- [ ] **Step 1: Write failing tests**

`test_propose.py` 新增：
- `test_parse_accepts_structural_at_stage2`：合法 structural（含非空 `patches`，每条有 file/anchor/new，
  file 形如 `res://rl/train_map.tscn`）在 stage=2 放行。
- `test_parse_rejects_structural_at_stage1`：同计划 stage=1 → `ValueError`（已被 `_STAGE_ALLOWED_TYPES` 覆盖，
  补断言）。
- `test_parse_rejects_structural_missing_patches`：structural 但 `patches` 空/缺 → `ValueError`。
- `test_parse_rejects_patch_touching_protected`：patch.file 命中 `harness/**` 等 protected 前缀
  或指向 `game_agent.gd`（含 GOAL/FALL/reward 的测量装置文件）→ `ValueError`。
- `test_parse_rejects_logic_at_stage2`：`change_type=="logic"` 在 stage=2 仍 `ValueError`。

- [ ] **Step 2: Verify RED**
Run: `python -m pytest tests/test_propose.py -q` → 至少 structural 校验用例 FAIL。

- [ ] **Step 3: Implement minimal**

在 `parse_plan` 的 change_type 分支补：`change_type in {"structural","logic"}` 时校验
`patches` 非空、每条含 `file`/`anchor`/`new` 三键且非空字符串；`file` 不得命中 protected glob，
也不得是 `game_agent.gd`（测量装置硬护栏）。**注意(critic C1/M4)**:这是测量边界的**第①层**;
真正的兜底在 `mutate.allowed()`(第②层)与 `apply_patch`(第③层),因为 TDD 注入的 `propose_fn`
会绕过 `parse_plan`——三层缺一不可,本任务只补第①层。
`_build_prompt` 在 stage≥2 段落追加一段 **结构摘要**：列出 `train_map.tscn` 中可 patch 的
`MidPlatform` 节点 anchor（节点行+`position = Vector2(600, 40)` 多行块)与改动指引（只准挪
MidPlatform 的 position，禁止碰 GoalFlag/GOAL/FALL/reward）。
**这是签名改动(critic m3)**:`propose()` 当前**无** `code_summary` 形参,`_build_prompt` 亦无;
本任务给两者加 `code_summary: str = ""` 形参,并同步改 `optimize.py:380-384` 默认 `propose_fn` lambda
与 Stage-1 `test_propose`/`test_optimize` 调用点(默认空串 → 阶段1 零回归)。

- [ ] **Step 4: Verify GREEN**
Run: `python -m pytest tests/test_propose.py -q` → PASS。

- [ ] **Step 5: Commit**
```bash
git add harness/llm_propose.py tests/test_propose.py
git commit -m "feat(opt): validate stage-2 structural plans + scene summary prompt"
```

---

### Task 5: `optimize_loop` structural 分支 + 白名单泛化（TDD）

**Files:**
- Modify: `harness/optimize.py`
- Modify: `tests/test_optimize.py`

**Interfaces:** structural 分支复用 `evaluate_current`/`paired_improvement`/`mutate.commit`/`rollback`；
白名单由 `mutate.target_files(plan, proj_rel=...)` 动态产生。

- [ ] **Step 1: Write failing loop tests**

`test_optimize.py` 新增（全部用注入的假 propose/evaluator/gate 钩子，不起 Godot）：
- `test_structural_accept_commits_patched_tscn`：stage=2，propose 返回 structural patch（挪 MidPlatform），
  语法/smoke gate 注入为通过，candidate 配对改善 > MIN_IMPROVEMENT → 断言 `mutate.commit` 收到的
  paths **恰为**被 patch 的 `testbed_platformer/rl/train_map.tscn`（不是 tunables.json）。
- `test_structural_rejected_when_patch_touches_protected`(critic C1/M4)：propose 返回触碰
  `res://rl/game_agent.gd` 的 structural patch(**绕过 parse_plan**,直接注入),断言被 `mutate.allowed()`
  在 snapshot 前拒绝,记 memory "protected path",**不** apply、不 commit。
- `test_structural_syntax_gate_failure_rolls_back`：语法 gate 注入失败 → `rollback(snap)` 被调用、
  memory 记 `reason` 含 "syntax"、`no_improve += 1`，不评估。
- `test_structural_smoke_gate_failure_rolls_back`：smoke gate 失败 → 同上，reason 含 "smoke"。
- `test_structural_no_improvement_rolls_back`：两 gate 过但配对改善 ≤ 阈值 → 回滚 + reason "no score improvement"。
- `test_tunable_whitelist_unchanged`(critic M2)：tunable_search 计划下 `paths` **恰为**
  `[STAGE1_TUNABLES_REL]`(白名单泛化不改阶段1 提交粒度)。
- `test_default_tracked_changes_real_impl`(critic M2,**不注入** `tracked_changes_fn`)：临时 git 仓里
  仅 tunables.json 脏 → `_default_tracked_changes` 返回空;另一文件脏 → 返回非空。固定默认实现行为防回归。

- [ ] **Step 2: Verify RED**
Run: `python -m pytest tests/test_optimize.py -q` → FAIL（当前 structural 走 "unsupported change type" 回滚分支）。

- [ ] **Step 3: Implement minimal**

`optimize_loop`：把硬编码 `paths = [STAGE1_TUNABLES_REL]` 改为
`paths = mutate.target_files(plan, proj_rel=_proj_rel(cfg))`(tunable_search 分支仍解析出
**恰为** `[STAGE1_TUNABLES_REL]`,保持阶段1 提交粒度,critic M2)。
**关键(critic C1/M4)**:在 `change_type` 分发**之前**,把现有 `mutate.allowed(plan, cfg.protected_paths)`
调用改为 `mutate.allowed(plan, cfg.protected_paths, proj_rel=_proj_rel(cfg))`,使 structural 的 `patches`
也过 protected 检查(命中即记 "protected path" 跳过,不快照不应用)。
`change_type == "structural"`（且 `cfg.stage >= 2`）时走新分支：
```text
snap = snapshot(paths)
try: apply_structural(cfg, plan, paths)         # 逐条 apply_patch(带 protected_globs);anchor/protected 异常
except (ValueError) : rollback(snap)+memory("apply failed: ...")+no_improve++; continue
ok, detail = syntax_gate_fn(cfg);  if not ok: rollback+memory("syntax: "+detail)+no_improve++; continue
ok, detail = smoke_gate_fn(cfg);   if not ok: rollback+memory("smoke: "+detail)+no_improve++; continue
candidate = evaluate_current(cfg, point_id=f"structural_r{r}")
improvement = paired_improvement(base, candidate)
if improvement > min_improvement: commit(paths) + memory(accepted) + base=candidate
else: rollback(snap) + memory("no score improvement") + no_improve++
```
`apply_structural` 把 `cfg.protected_paths` 透传给每条 `mutate.apply_patch` 的 `protected_globs`(第③层)。
注入点：`optimize_loop` 增可选形参 `syntax_gate_fn`/`smoke_gate_fn`(默认值在**函数体内局部** `import gates`
后取 `gates.syntax_gate`/`gates.smoke_gate`,避免顶层循环 import,critic M3)。
Gate 0b `_default_tracked_changes` 放行集从写死的 `STAGE1_TUNABLES_REL` 改为「**仅当轮** `paths` 白名单 +
`.artifacts/`」(critic 歧义点:取当轮,不累积,避免历史白名单永久放行而侵蚀边界);其余越界仍中止。
默认 `PROTECTED_PATHS` 追加测量装置 glob(critic C2):
`harness/**,.git/**,tests/**,docs/**,*/rl/game_agent.gd,*/rl/telemetry.gd,*/rl/recorder.gd`。
`_proj_rel(cfg)` = `os.path.relpath(cfg.proj, cfg.repo_root)`。

- [ ] **Step 4: Verify GREEN**
Run: `python -m pytest tests/test_optimize.py tests/test_evaluation.py -q` → PASS。

- [ ] **Step 5: Commit**
```bash
git add harness/optimize.py tests/test_optimize.py
git commit -m "feat(opt): structural change branch with 3-gate acceptance (stage2)"
```

---

### Task 6: 入口脚本透传 + 全量回归（TDD + 集成）

**Files:**
- Modify: `harness/run_optimize.sh`
- Modify: `tests/test_run_optimize.py`

**Interfaces:** 透传 `SMOKE_MAX_STEPS`/`SMOKE_TIMEOUT_SECONDS`；`STAGE=2` 可用。

- [ ] **Step 1: Write shell contract test**

`test_run_optimize.py` 新增断言：`SMOKE_MAX_STEPS`/`SMOKE_TIMEOUT_SECONDS` 出现在 export 列表；
`STAGE` 透传不写死 1；默认 `PROTECTED_PATHS` 含测量装置 glob(`*/rl/game_agent.gd` 等,critic C2)。

- [ ] **Step 2: Verify RED → 3: Implement**
在 run_optimize.sh 加 `: "${SMOKE_MAX_STEPS:=2000}"`、`: "${SMOKE_TIMEOUT_SECONDS:=120}"` 与对应 export;
把默认 `PROTECTED_PATHS` 改为
`harness/**,.git/**,tests/**,docs/**,*/rl/game_agent.gd,*/rl/telemetry.gd,*/rl/recorder.gd`
(与 `optimize.py`/`mutate.py` 默认值一致)。

- [ ] **Step 4: Verify GREEN（全量）**
Run: `~/.local/share/godot-rl-venv/bin/python -m pytest tests/ -q` → 全绿。

- [ ] **Step 5: Commit**
```bash
git add harness/run_optimize.sh tests/test_run_optimize.py
git commit -m "feat(opt): pass smoke-gate budget through optimize entrypoint"
```

---

### Task 7: 端到端结构闭环验证（testbed，真起 Godot）

**Files:**
- Runtime only: `.artifacts/opt/`
- Fix files: 仅限 Task 1–6 已列文件

**Interfaces:** `MODEL` 为显式外部依赖，其 SHA-256 进 provenance。复用阶段 1 已验证模型即可
（结构改动不改 obs，旧策略仍可推理）。

- [ ] **Step 1: 语法 gate 实测**
手动把 MidPlatform position 改成一个**合法新值**与一个**故意写坏的行**，分别跑
`( cd testbed_platformer && Godot --headless --path . --import )`，确认坏行被 `SCRIPT ERROR`/
`Parse Error` 捕获、好值 rc=0。验毕还原。

- [ ] **Step 2: 一轮廉价结构闭环（连通性）**
```bash
MODEL="$HOME/.local/share/godot-rl-venv/ppo_game.zip" \
STAGE=2 MAX_ROUNDS=1 EVAL_SEEDS=1 EVAL_EPISODES=2 MAX_EVAL_STEPS=5000 \
SMOKE_MAX_STEPS=2000 THRESHOLDS='{"hard_completion":0.6}' \
bash harness/run_optimize.sh
```
Expected: 闭环跑通 structural 分支（日志可见 apply→syntax→smoke→eval）；若被接受，分支 commit
**只含** `testbed_platformer/rl/train_map.tscn`（用 `git show --stat` 核对）；memory 落 `.artifacts/`。
仅验连通性，不据此宣称统计改善。

- [ ] **Step 3: 故意坏 patch 的 gate 拦截**
临时让 propose 返回一个 anchor 写坏的 structural patch（或用 THRESHOLDS 触发后人工注入），断言语法 gate
拦截 → 定向回滚（`git status` 干净，train_map.tscn 未变）→ memory 记 reason 含 "syntax"。

- [ ] **Step 4: 全量回归 + 凭据扫描**
```bash
~/.local/share/godot-rl-venv/bin/python -m pytest tests/ -q
git ls-files | rg -i '(^|/)(\.env($|\.)|.*cookies.*|.*secret.*|.*\.key$)'
git grep -nE 'sk-ant-[A-Za-z0-9_-]{20,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
```
Expected: 测试全绿；两条扫描均无输出。

---

### Task 8: 文档与进度更新

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/specs/2026-06-28-llm-optimization-loop-design.md`（如需补 MidPlatform 落点）

- [ ] **Step 1:** README 补阶段 2 用法（`STAGE=2`、`SMOKE_MAX_STEPS`/`SMOKE_TIMEOUT_SECONDS`、结构旋钮说明）。
- [ ] **Step 2:** CLAUDE.md「当前已建成」更新为「优化环阶段 1+2 已建成；未建阶段 3（`.gd` 逻辑）+ 循环编排」。
- [ ] **Step 3:** 凭据扫描复查 + 全量测试复跑。
- [ ] **Step 4:** Commit
```bash
git add README.md CLAUDE.md docs/specs/2026-06-28-llm-optimization-loop-design.md
git commit -m "docs(opt): document stage-2 structural loop"
```

## 完成定义

- 所有单测通过；testbed `--import` 通过（含新 MidPlatform 节点）。
- `apply_patch` 对未命中/歧义 anchor 一律拒绝且**不改文件**；路径越界抛 `ValueError`。
- structural 分支严格走「应用 → 语法 → smoke → 指标」四步；任一 gate 失败定向回滚 + 记 memory，
  **不** `git reset --hard`/`add -A`，不污染白名单外文件。
- 接受时提交粒度**恰为**被 patch 的 `.tscn`（非 tunables.json）；memory/telemetry/report 仍在 `.artifacts/`。
- **测量边界防御纵深成立(critic C1/C2/M4)**:即便注入的 `propose_fn` 绕过 `parse_plan` 返回触碰
  `game_agent.gd` 的 patch,也被 `mutate.allowed()`/`apply_patch` 拒绝;默认 `PROTECTED_PATHS` 点名了
  测量装置文件;有单测覆盖这条绕过路径。
- `GOAL_X`/`FALL_Y`/reward/telemetry/诊断阈值未进入任何 patch；`GoalFlag` 未被作为结构旋钮。
- **smoke_gate 真的廉价**(critic C3):`cfg.eval_episodes=20` 时 smoke 仍只要 1 局(`run_one_seed` 覆盖生效)。
- `import gates, optimize` 无循环导入崩溃(critic M3)。
- 端到端在 testbed 跑通 structural 闭环，被接受/拒绝均有配对差值、模型/tunables hash、确切 JSONL 可追溯。
- 阶段 1 行为零回归（evaluation/infer/search/tunable_search 全部原样通过）。
