"""harness/optimize.py — LLM 优化闭环编排器(spec §7,阶段1)。

主循环组合同目录子模块:
  objective.score  — report → 标量(越小越好)
  llm_propose      — LLM 提案(report+tunables+memory → 改动计划)
  search.optimize  — 贝叶斯内循环(仅 tunable_search)
  mutate           — 改动应用 / protected 检查 / git 快照·回滚·提交
  memory           — 跨轮记忆读写

闭环(spec §7 伪码):
  baseline → LLM 提案 → git 快照 → protected 检查
    → (tunable_search) 贝叶斯内循环搜最优数值
    → 三道 gate(① 语法 ② smoke ③ 指标回归)
    → objective 比分:真变好才 commit 接受,否则 git 回滚
    → 写 memory → 下一轮(早停:MAX_ROUNDS / PATIENCE / 无 high issue / 预算耗尽)

阶段1 只处理 change_type == "tunable_search"(数值改动,天然过语法 gate;
贝叶斯每点评估隐含过 smoke+指标)。结构/逻辑改动属阶段 2/3,本文件预留接口但不实现。

配置经环境变量(spec §9):
  STAGE / TARGET_COMPLETION / MAX_ROUNDS / PATIENCE / SEARCH_CALLS
  / RETRAIN_EACH / PROTECTED_PATHS / PROJ / SCENE / MODEL / SPEEDUP
  / TUNABLES_PATH / MEMORY_PATH / REPORT_PATH
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Optional

# 同目录子模块(harness 非包,靠 sys.path 注入;CLI 入口下方兜底)
import objective
import memory as memory_mod
import mutate
import llm_propose
import search
import evaluation


# --------------------------------------------------------------------------- #
# 配置                                                                          #
# --------------------------------------------------------------------------- #

# 默认 protected glob:除工具链/仓基础设施外,**点名测量装置文件**(critic C2)。
# game_agent.gd 含 GOAL_X/FALL_Y/reward,telemetry.gd/recorder.gd 是落盘装置——
# 这些是「尺子」,structural patch 绝不能碰。因目标游戏各异,须显式点名该游戏的测量文件。
DEFAULT_PROTECTED = (
    "harness/**,.git/**,tests/**,docs/**,"
    "*/rl/game_agent.gd,*/rl/telemetry.gd,*/rl/recorder.gd,"
    # persona reward profile = 冻结仪器面板(主观体验层),优化闭环永不改(critic M1)。
    # 仓根 glob;已验 fnmatch('personas/x.json','personas/*.json')==True。
    "personas/*.json")

# 阶段1 唯一可提交的白名单路径(repo-relative)。结构/逻辑改动属阶段 2/3,
# 阶段1 只允许写真实玩法参数,提交粒度固定到这一个文件。
STAGE1_TUNABLES_REL = "testbed_platformer/rl/tunables.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_seeds(name: str, default: tuple) -> tuple:
    """从逗号分隔环境变量解析种子组,如 "1,2,3"。空/非法回退默认。"""
    raw = os.environ.get(name)
    if not raw:
        return tuple(default)
    try:
        return tuple(int(x.strip()) for x in raw.split(",") if x.strip())
    except ValueError:
        return tuple(default)


class Config:
    """从环境变量读取闭环配置(spec §9)。"""

    def __init__(self) -> None:
        self.stage = _env_int("STAGE", 1)
        self.target_completion = _env_float("TARGET_COMPLETION", 0.65)
        self.max_rounds = _env_int("MAX_ROUNDS", 8)
        self.patience = _env_int("PATIENCE", 3)
        self.search_calls = _env_int("SEARCH_CALLS", 12)
        self.retrain_each = _env_int("RETRAIN_EACH", 0)
        self.protected_paths = [
            p.strip()
            for p in os.environ.get("PROTECTED_PATHS", DEFAULT_PROTECTED).split(",")
            if p.strip()
        ]
        self.proj = os.environ.get("PROJ", "")
        self.scene = os.environ.get("SCENE", "")
        # 外部模型文件(显式依赖,不入库);进程绑定的 provenance 用其 SHA-256。
        self.model = os.environ.get("MODEL", "")
        self.speedup = _env_int("SPEEDUP", 8)
        # 游戏侧 tunables.json(被优化对象);默认在 PROJ/rl/tunables.json
        self.tunables_path = os.environ.get(
            "TUNABLES_PATH",
            os.path.join(self.proj, "rl", "tunables.json") if self.proj else "",
        )
        self.memory_path = os.environ.get("MEMORY_PATH", "memory.json")
        self.report_path = os.environ.get("REPORT_PATH", "report.json")
        self.repo_root = os.environ.get("REPO_ROOT", ".")

        # 配对评估配置(spec §9;Task 7 会补严格校验,这里只读默认值)。
        self.eval_seeds = _env_seeds("EVAL_SEEDS", (1, 2, 3))
        self.eval_episodes = _env_int("EVAL_EPISODES", 20)
        self.max_eval_steps = _env_int("MAX_EVAL_STEPS", 40000)
        self.eval_timeout_seconds = _env_int("EVAL_TIMEOUT_SECONDS", 900)
        self.min_improvement = _env_float("MIN_IMPROVEMENT", 0.1)
        # smoke gate 预算(阶段2):廉价跑一局确认场景能起;独立于正式评估预算。
        self.smoke_max_steps = _env_int("SMOKE_MAX_STEPS", 2000)
        self.smoke_timeout_seconds = _env_int("SMOKE_TIMEOUT_SECONDS", 120)
        self.artifact_root = os.environ.get(
            "ARTIFACT_ROOT", os.path.join(".artifacts", "opt"))
        # 本次 run 的唯一 id(run_optimize.sh 注入)。用于把每次 run 的 artifact 目录隔离到
        # runs/<OPT_RUN_ID>/ 下(spec §4.2),否则二次运行会撞 run_one_seed 的「拒绝复用已存在
        # 目录」(固定路径 .artifacts/opt/baseline/seed_1 跨 run 冲突)。
        self.opt_run_id = os.environ.get("OPT_RUN_ID", "")
        # 可选:覆盖 diagnose 默认阈值的 JSON(让闭环可调诊断灵敏度,如收紧 hard_completion)。
        _thr = os.environ.get("THRESHOLDS", "")
        self.thresholds = json.loads(_thr) if _thr.strip() else None

    def validate(self) -> None:
        """严格校验配对评估配置(spec §6/§7;非法即抛 ValueError)。

        - eval_seeds: 非空、无重复整数。
        - eval_episodes: > 0。
        - max_eval_steps: >= eval_episodes(至少给每局一步)。
        - eval_timeout_seconds: > 0。
        - min_improvement: >= 0。
        - artifact_root: 非空字符串。
        """
        seeds = tuple(self.eval_seeds)
        if not seeds:
            raise ValueError("eval_seeds 不能为空")
        if len(set(seeds)) != len(seeds):
            raise ValueError("eval_seeds 不能有重复: %r" % (seeds,))
        if int(self.eval_episodes) <= 0:
            raise ValueError("eval_episodes 必须 > 0: %r" % self.eval_episodes)
        if int(self.max_eval_steps) < int(self.eval_episodes):
            raise ValueError(
                "max_eval_steps(%r) 必须 >= eval_episodes(%r)"
                % (self.max_eval_steps, self.eval_episodes))
        if int(self.eval_timeout_seconds) <= 0:
            raise ValueError(
                "eval_timeout_seconds 必须 > 0: %r" % self.eval_timeout_seconds)
        if float(self.min_improvement) < 0:
            raise ValueError(
                "min_improvement 必须 >= 0: %r" % self.min_improvement)
        if not self.artifact_root:
            raise ValueError("artifact_root 不能为空")


# --------------------------------------------------------------------------- #
# JSON 小工具                                                                   #
# --------------------------------------------------------------------------- #

def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 试玩 + 诊断(默认实现,可被测试注入替换)                                       #
# --------------------------------------------------------------------------- #

def run_playtest_and_diagnose(cfg: Config) -> dict:
    """跑一次 run_infer.sh(试玩 + 诊断),读回 report.json。

    复用第一/二环工具:run_infer.sh 内部已 run_infer + diagnose。
    返回解析后的 report dict。失败抛 RuntimeError。
    """
    harness_dir = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(harness_dir, "run_infer.sh")
    env = dict(os.environ)
    env["DIAGNOSE"] = "1"
    proc = subprocess.run(
        ["bash", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"run_infer.sh 失败 (rc={proc.returncode}):\n{proc.stderr[-2000:]}"
        )
    # diagnose.py 默认把 report.json 写到 telemetry 目录;约定 REPORT_PATH 指向它。
    telemetry_dir = env.get(
        "TELEMETRY_DIR", os.path.join(cfg.proj, "rl", "telemetry")
    )
    report_path = cfg.report_path
    if not os.path.exists(report_path):
        candidate = os.path.join(telemetry_dir, "report.json")
        if os.path.exists(candidate):
            report_path = candidate
    if not os.path.exists(report_path):
        raise RuntimeError(f"未找到 report.json(查找 {cfg.report_path} / {telemetry_dir})")
    return _load_json(report_path)


# --------------------------------------------------------------------------- #
# 早停判定                                                                      #
# --------------------------------------------------------------------------- #

def has_high_issue(report: dict) -> bool:
    """report 是否仍有 high severity 的 issue。

    Goodhart 防火墙(critic M4):主观/跨 persona 软问题(type=='soft')**绝不**参与早停或
    被优化锚消费——即便有人误把 soft issue 塞进 report['issues'],这里也防御性地忽略它。
    """
    return any(i.get("severity") == "high" and i.get("type") != "soft"
               for i in report.get("issues", []))


# --------------------------------------------------------------------------- #
# 独立 artifact + 配对评估器(spec §5.5,Task 4)                                #
# --------------------------------------------------------------------------- #

def run_one_seed(cfg: Config, *, seed: int, artifact_dir: str,
                 min_episodes: int | None = None,
                 max_eval_steps: int | None = None) -> evaluation.RunResult:
    """对单个 seed 在隔离目录里跑一次试玩并诊断,返回进程绑定的 RunResult。

    步骤(spec §5.5 / 原则 7):
      ① 拒绝已存在的 artifact_dir,创建空目录及其 telemetry/ 子目录;
      ② 启动前计算 model / tunables 的 SHA-256(provenance 不能事后补);
      ③ 以 DIAGNOSE=0、独立 TELEMETRY_DIR、指定 EVAL_SEED 调 run_infer.sh,
         施加 EVAL_TIMEOUT_SECONDS 墙钟超时;
      ④ 结束后要求 telemetry 目录中**恰好一个** run_*.jsonl(0/多个均失败);
      ⑤ 调 evaluation.validate_telemetry() 诊断**那个确切文件**,禁止搜索别处;
      ⑥ 用 objective.score 算分,返回带启动前 hash 的 RunResult。

    预算覆盖(critic C3,smoke_gate 复用):
      min_episodes / max_eval_steps 默认 None → 用 cfg.eval_episodes / cfg.max_eval_steps
      (阶段1 零回归)。覆盖值同时作用于:传给子进程的 EVAL_EPISODES / MAX_EVAL_STEPS
      与 validate_telemetry 的 min_episodes。smoke_gate 传 min_episodes=1 得廉价 ≥1 局评估。
    """
    eff_episodes = (min_episodes if min_episodes is not None
                    else cfg.eval_episodes)
    eff_max_steps = (max_eval_steps if max_eval_steps is not None
                     else cfg.max_eval_steps)
    # ① 隔离目录:已存在即拒绝,避免复用旧产物(原则 7 反对回退 latest)。
    if os.path.exists(artifact_dir):
        raise FileExistsError(f"artifact_dir 已存在,拒绝复用: {artifact_dir}")
    telemetry_dir = os.path.join(artifact_dir, "telemetry")
    os.makedirs(telemetry_dir, exist_ok=False)

    # ② 启动前算 hash —— 必须在试玩前,保证 provenance 与本次进程绑定。
    model_sha = evaluation.sha256_file(cfg.model) if cfg.model else ""
    tunables_sha = (
        evaluation.sha256_file(cfg.tunables_path) if cfg.tunables_path else "")

    # ③ DIAGNOSE=0 + 独立 TELEMETRY_DIR + 指定 seed 调 run_infer.sh。
    harness_dir = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(harness_dir, "run_infer.sh")
    env = dict(os.environ)
    env.update({
        "DIAGNOSE": "0",
        "TELEMETRY_DIR": telemetry_dir,
        "EVAL_SEED": str(seed),
        "EVAL_EPISODES": str(eff_episodes),
        "MAX_EVAL_STEPS": str(eff_max_steps),
        "PROJ": cfg.proj,
        "SCENE": cfg.scene,
        "MODEL": cfg.model,
        "SPEEDUP": str(cfg.speedup),
    })
    try:
        proc = subprocess.run(
            ["bash", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=cfg.eval_timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"seed={seed} 评估超时(>{cfg.eval_timeout_seconds}s)") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"seed={seed} run_infer.sh 失败 (rc={proc.returncode}):\n"
            f"{(proc.stderr or '')[-2000:]}")

    # ④ 要求本次独立 telemetry 目录恰好一个 run_*.jsonl(不回退共享 latest)。
    import glob
    found = sorted(glob.glob(os.path.join(telemetry_dir, "run_*.jsonl")))
    if len(found) != 1:
        raise RuntimeError(
            f"seed={seed} 期望恰好 1 个 run_*.jsonl,实际 {len(found)} 个"
            f"(目录 {telemetry_dir})")
    telemetry_path = found[0]

    # ⑤ 诊断**那个确切文件**,校验 run 头/局数/run_id 绑定。
    report, run_id = evaluation.validate_telemetry(
        telemetry_path,
        scene=cfg.scene, model=cfg.model, speedup=cfg.speedup,
        min_episodes=eff_episodes, thresholds=cfg.thresholds)

    # ⑥ 算分 + 组 provenance(hash 来自启动前)。
    sc = objective.score(report, target=cfg.target_completion)
    provenance = {
        "scene": cfg.scene,
        "model": cfg.model,
        "model_sha256": model_sha,
        "tunables_sha256": tunables_sha,
        "speedup": cfg.speedup,
        "seed": seed,
        "telemetry_path": os.path.abspath(telemetry_path),
        "run_id": run_id,
    }
    return evaluation.RunResult(
        seed=seed, telemetry_path=os.path.abspath(telemetry_path),
        run_id=run_id, report=report, score=sc, provenance=provenance)


def evaluate_current(cfg: Config, *, point_id: str) -> evaluation.EvaluationResult:
    """对 cfg.eval_seeds **按固定顺序**逐个 run_one_seed,返回 EvaluationResult。

    每个 (point, seed) 用启动前为空的独立 artifact 目录
    `<artifact_root>/runs/<OPT_RUN_ID>/<point_id>/seed_<seed>/`,保证报告新鲜、互不污染,
    且**跨 run 不冲突**(每次 run 的 OPT_RUN_ID 唯一)。seed 顺序对每个 point 完全一致(配对改善前提)。
    """
    runs = []
    for seed in cfg.eval_seeds:
        artifact_dir = os.path.join(
            cfg.artifact_root, "runs", cfg.opt_run_id, point_id, "seed_%d" % seed)
        runs.append(run_one_seed(cfg, seed=seed, artifact_dir=artifact_dir))
    return evaluation.EvaluationResult(tuple(runs))


def make_evaluator(cfg: Config) -> Callable[[dict], evaluation.EvaluationResult]:
    """构造贝叶斯 evaluate(point):写 tunables → evaluate_current → EvaluationResult。

    point 是 {key: value} dict(由 search.optimize 映射好类型)。每点用其值的
    稳定指纹做 point_id,使该点的所有 seed 落进同一独立目录簇。
    """

    def evaluate(point: dict) -> evaluation.EvaluationResult:
        for key, value in point.items():
            mutate.apply_tunable(cfg.tunables_path, key, value)
        point_id = _point_id(point)
        return evaluate_current(cfg, point_id=point_id)

    return evaluate


def _point_id(point: dict) -> str:
    """把参数点压成稳定的目录名指纹(排序 key + 短 hash,避免非法字符)。"""
    import hashlib
    blob = json.dumps(point, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return "p_" + digest


# --------------------------------------------------------------------------- #
# 主循环                                                                        #
# --------------------------------------------------------------------------- #

def optimize_loop(
    cfg: Config,
    propose_fn: Optional[Callable] = None,
    evaluator_fn: Optional[Callable] = None,
    baseline_fn: Optional[Callable] = None,
    tracked_changes_fn: Optional[Callable] = None,
    syntax_gate_fn: Optional[Callable] = None,
    smoke_gate_fn: Optional[Callable] = None,
) -> dict:
    """运行优化闭环(spec §7 主循环 + §6 三道 gate),返回总结 dict。

    baseline 生命周期(原则:不复用磁盘旧 report):
      - run 开始**必跑一次新 baseline 评估**(`baseline_fn`),绑定新 artifact;
        绝不读 cfg.report_path 磁盘旧报告。
      - 接受后 candidate(已验证 EvaluationResult)成为下一轮 baseline,
        不重复评估同一点。
      - 拒绝/失败后定向回滚白名单,baseline 不变(仍对应回滚后的 hash)。

    接受门(spec §6 ③):`paired_improvement(base, candidate) > min_improvement`,
    **严格大于**;等于阈值不接受。计分日志先存 `prev = base.mean_score` 再赋值
    `base = candidate`,reason 用 prev→candidate.mean_score(不得 X→X)。

    Args:
        cfg:               配置(进入前已 validate)。
        propose_fn:        LLM 提案,签名 (report, tunables, memory, stage)->plan。
        evaluator_fn:      贝叶斯每点评估,签名 (point)->EvaluationResult。
                           默认 make_evaluator(cfg)。
        baseline_fn:       baseline 评估,签名 (cfg)->EvaluationResult。
                           默认 evaluate_current(cfg, point_id="baseline")。
        tracked_changes_fn:Gate 0b,签名 (cfg)->list[str];返回白名单外的 tracked
                           改动路径,非空则立即中止本 run。默认探测 git。

    返回总结:{"accepted": [...], "base_score": float, "rounds": int,
              "final_report": dict, "aborted": bool}
    """
    cfg.validate()

    propose_fn = propose_fn or (
        lambda report, tunables, mem, stage: llm_propose.propose(
            report, tunables, mem, stage, code_summary=_code_summary(cfg)
        )
    )
    evaluator_fn = evaluator_fn or make_evaluator(cfg)
    baseline_fn = baseline_fn or (
        lambda c: evaluate_current(c, point_id="baseline"))
    tracked_changes_fn = tracked_changes_fn or _default_tracked_changes
    # gate 默认实现:函数体内局部 import gates,避免 optimize↔gates 顶层循环(critic M3)。
    if syntax_gate_fn is None or smoke_gate_fn is None:
        import gates
        syntax_gate_fn = syntax_gate_fn or gates.syntax_gate
        smoke_gate_fn = smoke_gate_fn or gates.smoke_gate

    mem_path = _memory_path_for(cfg)

    # baseline:run 开始必跑一次新评估,绝不读磁盘旧 report(原则:新鲜 baseline)。
    base = baseline_fn(cfg)
    no_improve = 0
    accepted: list[dict] = []
    aborted = False
    r = -1

    for r in range(cfg.max_rounds):
        report = base.representative_report
        # 早停:无 high issue / 连续无改善达 PATIENCE
        if not has_high_issue(report) or no_improve >= cfg.patience:
            break

        # Gate 0b(每轮边界):白名单外出现 tracked 改动 → 立即中止整个 run。
        outside = tracked_changes_fn(cfg)
        if outside:
            aborted = True
            memory_mod.add_round(
                mem_path, cfg.scene,
                _record(r, {}, base.mean_score, base.mean_score, False,
                        "aborted: 白名单外 tracked 改动 %s" % outside),
            )
            break

        tunables = _load_json(cfg.tunables_path)
        mem = memory_mod.load(mem_path)
        plan = propose_fn(report, tunables, mem, cfg.stage)

        # protected + 参数边界入口:命中则拒绝并记 memory(不快照/不搜索)。
        # proj_rel 让 structural 的 patches(res://)经映射后也过 protected 检查
        # (防御纵深第②层,critic C1/M4)。
        if not mutate.allowed(plan, cfg.protected_paths, proj_rel=_proj_rel(cfg)):
            memory_mod.add_round(
                mem_path, cfg.scene,
                _record(r, plan, base.mean_score, base.mean_score, False,
                        "protected path"),
            )
            no_improve += 1
            continue

        change_type = plan.get("change_type")

        # 本轮白名单:tunable_search 仍恰为唯一的 tunables.json(repo-relative,
        # 阶段1 提交粒度不变,critic M2);structural/logic 取 plan 声明的目标文件集。
        if change_type == "tunable_search":
            paths = [STAGE1_TUNABLES_REL]
        else:
            try:
                paths = mutate.target_files(plan, proj_rel=_proj_rel(cfg))
            except ValueError as e:
                memory_mod.add_round(
                    mem_path, cfg.scene,
                    _record(r, plan, base.mean_score, base.mean_score, False,
                            "invalid patch path: %s" % e),
                )
                no_improve += 1
                continue

        # ── structural 分支(stage>=2):无贝叶斯,四步 gate ──────────────────
        if change_type == "structural" and cfg.stage >= 2:
            base, accepted_rec = _run_structural_round(
                cfg, plan, paths, r, base, mem_path,
                syntax_gate_fn, smoke_gate_fn)
            if accepted_rec is not None:
                accepted.append(accepted_rec)
                no_improve = 0
            else:
                no_improve += 1
            continue

        if change_type != "tunable_search":
            # 阶段不支持的 change_type(如 stage<2 的 structural、logic)→ 跳过。
            memory_mod.add_round(
                mem_path, cfg.scene,
                _record(r, plan, base.mean_score, base.mean_score, False,
                        f"unsupported change type={change_type}"),
            )
            no_improve += 1
            continue

        # tunable_search:快照白名单,跑贝叶斯内循环。
        snap = mutate.snapshot(paths, cfg.repo_root)

        # 贝叶斯内循环:返回最优点 + 对应 candidate(EvaluationResult)。
        # 评估失败(0/多个 JSONL、局数不足、超时等)会从 evaluator 抛出 → 记失败回滚。
        search_space = _normalize_search_space(plan["search_space"], tunables)
        try:
            best_point, candidate = search.optimize(
                search_space, evaluator_fn, n_calls=cfg.search_calls)
            improvement = evaluation.paired_improvement(base, candidate)
        except (RuntimeError, ValueError) as e:
            mutate.rollback(snap, cfg.repo_root)
            memory_mod.add_round(
                mem_path, cfg.scene,
                _record(r, plan, base.mean_score, base.mean_score, False,
                        "evaluation failed: %s" % e),
            )
            no_improve += 1
            continue

        summary = _summarize_point(best_point)

        # 指标 gate(③):配对改善严格大于阈值才接受。
        if improvement > cfg.min_improvement:
            # 把最优点写回 tunables(贝叶斯最后评估点未必最优),再提交白名单。
            for key, value in best_point.items():
                mutate.apply_tunable(cfg.tunables_path, key, value)
            prev = base.mean_score                       # 先存 prev,再赋值(防 X→X)
            mutate.commit(f"opt r{r}: {summary}", paths, cfg.repo_root)
            after = candidate.mean_score
            memory_mod.add_round(
                mem_path, cfg.scene,
                _record(r, plan, prev, after, True,
                        f"score {prev:.3f}→{after:.3f}"),
            )
            accepted.append({"round": r, "summary": summary,
                             "score_before": prev, "score_after": after})
            base = candidate                             # 已验证候选成为下一轮 baseline
            no_improve = 0
        else:
            mutate.rollback(snap, cfg.repo_root)         # 定向回滚白名单,不动其它文件
            memory_mod.add_round(
                mem_path, cfg.scene,
                _record(r, plan, base.mean_score, candidate.mean_score, False,
                        "no score improvement"),
            )
            no_improve += 1

    return {
        "accepted": accepted,
        "base_score": base.mean_score,
        "rounds": r + 1 if cfg.max_rounds else 0,
        "final_report": base.representative_report,
        "aborted": aborted,
    }


# --------------------------------------------------------------------------- #
# baseline 生命周期辅助                                                          #
# --------------------------------------------------------------------------- #

def _scene_hash(scene: str) -> str:
    """scene 字符串的稳定短 hash,用作 memory 文件名(每场景独立记忆)。"""
    import hashlib
    return hashlib.sha256((scene or "").encode("utf-8")).hexdigest()[:12]


def _memory_path_for(cfg: Config) -> str:
    """memory 落盘路径:`<artifact_root>/memory/<scene-hash>.json`(不进 commit)。

    显式 MEMORY_PATH 环境变量(非默认值)优先;否则按 scene-hash 放进 artifact。
    """
    explicit = os.environ.get("MEMORY_PATH")
    if explicit:
        return explicit
    mem_dir = os.path.join(cfg.artifact_root, "memory")
    os.makedirs(mem_dir, exist_ok=True)
    return os.path.join(mem_dir, "%s.json" % _scene_hash(cfg.scene))


def _default_tracked_changes(cfg: Config, paths: list | None = None) -> list:
    """Gate 0b 默认实现:列出工作树里**当轮白名单**之外的 tracked 改动路径。

    用 `git status --porcelain` 取改动文件,放行集 = 当轮 paths 白名单 + `.artifacts/`
    (critic M2:取当轮,不累积,避免历史白名单永久放行而侵蚀边界)。任何残留(代码/
    场景/其它 tracked)即视为越界,返回非空。

    paths 缺省(None)时放行集回退到阶段1 唯一白名单 `[STAGE1_TUNABLES_REL]`,
    供循环开头 Gate 0b 单参调用(此时尚无当轮 plan)使用。
    失败(非 git 仓等)时返回空,交由上层默认放行(单测均显式注入此钩子)。
    """
    whitelist = set(paths) if paths is not None else {STAGE1_TUNABLES_REL}
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cfg.repo_root, capture_output=True, text=True, check=False)
    except (OSError, ValueError):
        return []
    if out.returncode != 0:
        return []
    changed = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if path in whitelist:
            continue
        if path.startswith(".artifacts/"):
            continue
        changed.append(path)
    return changed


# --------------------------------------------------------------------------- #
# 内部小工具                                                                    #
# --------------------------------------------------------------------------- #

def _record(round_idx, plan, score_before, score_after, accepted, reason) -> dict:
    """组一条 memory 轮次记录(spec §5.3)。"""
    return {
        "round": round_idx,
        "target_issue": plan.get("target_issue", ""),
        "change_type": plan.get("change_type", ""),
        "summary": plan.get("expected_effect", "") or reason,
        "score_before": round(score_before, 4),
        "score_after": round(score_after, 4),
        "accepted": accepted,
        "reason": reason,
    }


def _proj_rel(cfg: Config) -> str:
    """PROJ 相对 repo_root 的路径,用于 res:// → repo-relative 映射(critic C1/M5)。

    cfg.proj 为空(纯 tunable_search 场景未设 PROJ)时返回空串,_res_to_repo 退化为
    按原样路径匹配,不影响阶段1。
    """
    if not cfg.proj:
        return ""
    return os.path.relpath(cfg.proj, cfg.repo_root)


def apply_structural(cfg: Config, plan: dict, paths: list) -> None:
    """逐条 mutate.apply_patch 应用 plan['patches'](res:// 映射成 repo 路径)。

    每条 patch 把 cfg.protected_paths 透传给 apply_patch 的 protected_globs
    (防御纵深第③层,写文件前再拦一次)。anchor 未命中/歧义、路径越界、命中 protected
    都从 apply_patch 抛 ValueError,由调用方捕获并定向回滚。
    """
    pr = _proj_rel(cfg)
    for patch in plan.get("patches") or []:
        repo_rel = mutate._res_to_repo(patch["file"], pr)
        mutate.apply_patch(
            repo_rel, patch["anchor"], patch["new"], cfg.repo_root,
            protected_globs=cfg.protected_paths)


def _run_structural_round(cfg, plan, paths, r, base, mem_path,
                          syntax_gate_fn, smoke_gate_fn):
    """structural 一轮:snapshot→apply→语法 gate→smoke gate→指标回归。

    返回 (new_base, accepted_record):
      - 接受:new_base=candidate,accepted_record=dict;
      - 拒绝/失败:new_base=base(不变),accepted_record=None(均已记 memory + 回滚)。
    无贝叶斯内循环(patch 是离散文本操作,一次提案=一个候选)。
    """
    snap = mutate.snapshot(paths, cfg.repo_root)

    # ① 应用 patch(anchor/protected/越界异常 → 回滚)
    try:
        apply_structural(cfg, plan, paths)
    except (ValueError, FileNotFoundError) as e:
        mutate.rollback(snap, cfg.repo_root)
        memory_mod.add_round(
            mem_path, cfg.scene,
            _record(r, plan, base.mean_score, base.mean_score, False,
                    "apply failed: %s" % e))
        return base, None

    # ①.5 .tscn 健全性(纯 Python,补 --import 对 .tscn 的失效:缺括号/悬空资源引用
    #      Godot --import 会 rc=0 静默放过,甚至吞成默认值 → 看似过 gate 实则没改游戏)。
    import gates as _gates
    ok, detail = _gates.tscn_sanity(paths, cfg.repo_root)
    if not ok:
        mutate.rollback(snap, cfg.repo_root)
        memory_mod.add_round(
            mem_path, cfg.scene,
            _record(r, plan, base.mean_score, base.mean_score, False,
                    "syntax: tscn_sanity %s" % detail))
        return base, None

    # ② 语法 gate(Godot --import)
    ok, detail = syntax_gate_fn(cfg)
    if not ok:
        mutate.rollback(snap, cfg.repo_root)
        memory_mod.add_round(
            mem_path, cfg.scene,
            _record(r, plan, base.mean_score, base.mean_score, False,
                    "syntax: %s" % detail))
        return base, None

    # ③ smoke gate(≥1 episode)
    ok, detail = smoke_gate_fn(cfg)
    if not ok:
        mutate.rollback(snap, cfg.repo_root)
        memory_mod.add_round(
            mem_path, cfg.scene,
            _record(r, plan, base.mean_score, base.mean_score, False,
                    "smoke: %s" % detail))
        return base, None

    # ④ 指标回归:配对改善 > min_improvement 才接受
    try:
        candidate = evaluate_current(cfg, point_id="structural_r%d" % r)
        improvement = evaluation.paired_improvement(base, candidate)
    except (RuntimeError, ValueError) as e:
        mutate.rollback(snap, cfg.repo_root)
        memory_mod.add_round(
            mem_path, cfg.scene,
            _record(r, plan, base.mean_score, base.mean_score, False,
                    "evaluation failed: %s" % e))
        return base, None

    if improvement > cfg.min_improvement:
        prev = base.mean_score
        mutate.commit("opt r%d: structural %s" % (r, plan.get("target_issue", "")),
                      paths, cfg.repo_root)
        after = candidate.mean_score
        memory_mod.add_round(
            mem_path, cfg.scene,
            _record(r, plan, prev, after, True,
                    "score %.3f→%.3f" % (prev, after)))
        return candidate, {"round": r, "summary": "structural",
                           "score_before": prev, "score_after": after}

    mutate.rollback(snap, cfg.repo_root)
    memory_mod.add_round(
        mem_path, cfg.scene,
        _record(r, plan, base.mean_score, candidate.mean_score, False,
                "no score improvement"))
    return base, None


def _normalize_search_space(search_space: list, tunables: dict) -> list:
    """把 LLM 的 search_space([{key,range}])补上 type(从 tunables 读),供 search 用。"""
    params = tunables.get("params", {})
    out = []
    for entry in search_space:
        key = entry["key"]
        dtype = params.get(key, {}).get("type", "float")
        out.append({"key": key, "range": entry["range"], "type": dtype})
    return out


def _summarize_point(point: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in point.items())


# 阶段2 结构旋钮：测试床 train_map.tscn 里唯一可 patch 的 MidPlatform position。
# anchor 取「节点声明行 + position 行」多行块,保证文件内唯一可定位(critic M1)。
_MIDPLATFORM_ANCHOR = (
    '[node name="MidPlatform" type="StaticBody2D" parent="."]\n'
    "position = Vector2(600, 40)")


def _code_summary(cfg: Config) -> str:
    """阶段2≥ 喂给 LLM 的结构摘要:可 patch 的 anchor + 硬边界指引。

    阶段1(stage<2)返回空串 → propose prompt 零回归。stage>=2 时给出 MidPlatform
    的 anchor 多行块,并明确只准挪其 position、禁碰 GoalFlag/GOAL/FALL/reward。
    """
    if cfg.stage < 2:
        return ""
    return (
        "目标文件: res://rl/train_map.tscn\n"
        "可 patch 的 anchor(必须原样作为 patch.anchor,含节点声明行+position 行):\n"
        + _MIDPLATFORM_ANCHOR + "\n"
        "改动指引: 只准挪 MidPlatform 的 position(踏脚石平台位置),"
        "禁止碰 GoalFlag/GOAL/FALL/reward 与 telemetry/诊断装置。")


def print_summary(summary: dict) -> None:
    """终端打印总结:接受的改动 + score 前后 + 剩余 issue。"""
    print("\n=== 优化闭环总结 ===")
    if summary["accepted"]:
        print("接受的改动:")
        for a in summary["accepted"]:
            print("  r%d: %s  (score %.3f → %.3f)"
                  % (a["round"], a["summary"], a["score_before"], a["score_after"]))
    else:
        print("接受的改动: 无")
    print("最终 score: %.3f" % summary["base_score"])
    remaining = summary["final_report"].get("issues", [])
    if remaining:
        print("剩余 issue (%d):" % len(remaining))
        for i in remaining:
            print("  [%s] %s: %s"
                  % (i.get("severity", "?"), i.get("id", "?"), i.get("message", "")))
    else:
        print("剩余 issue: 无")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    cfg = Config()
    summary = optimize_loop(cfg)
    print_summary(summary)
    return 0


if __name__ == "__main__":
    # harness 非包:确保同目录模块可 import
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    raise SystemExit(main())
