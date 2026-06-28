"""
harness/llm_propose.py — LLM 提案解析与生成

两个公开接口：
  parse_plan(text, tunables, stage=1) -> dict
      纯函数：把 LLM 返回的 JSON 文本解析并校验为改动计划（spec §5.2）。
      校验失败抛 ValueError（含原因）。
      stage 参数控制允许的 change_type：
        stage=1：只允许 tunable_search
        stage=2：允许 tunable_search、structural
        stage=3：允许 tunable_search、structural、logic

  propose(report, tunables, memory, stage) -> dict
      组 prompt → 调 anthropic SDK（structured output via tool use）→ parse_plan。
      API key 走环境变量 ANTHROPIC_API_KEY，绝不入库。
      解析失败最多重试 MAX_RETRIES 次，全部失败抛 ValueError。

改动计划 schema（spec §5.2）：
{
  "target_issue": str,          # 针对 report.json 里哪条 issue.id
  "hypothesis": str,            # 机制解释
  "change_type": str,           # tunable_search | structural | logic
  "search_space": [...],        # change_type==tunable_search 时必填
  "patches": [...],             # change_type==structural|logic 时可填
  "expected_effect": str,
  "confidence": float,
}

安全边界（防自欺硬边界，spec §4.1/§5.1）：
  - search_space.key 必须在 tunables.params 白名单中（主要边界）
  - search_space.key 禁止使用 reward_*/goal_*/fall_*/telemetry_*/diagnose_* 前缀（第二道保险）
  - search_space.range 必须是子集（⊆ 作者 range），且 min < max（不允许反向/退化范围）
  - search_space 中同一 key 不得重复出现
  - 阶段 1 只允许 change_type=="tunable_search"
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from typing import Any

try:
    import anthropic
except ImportError:  # 允许在没装 SDK 的环境里 import（测试 mock 时不需要真 SDK）
    anthropic = None  # type: ignore

# claude CLI 单次调用墙钟超时(秒)
CLAUDE_CLI_TIMEOUT = int(os.environ.get("CLAUDE_CLI_TIMEOUT", "180"))

# 允许的 change_type 值
VALID_CHANGE_TYPES = {"tunable_search", "structural", "logic"}

# 各阶段允许的 change_type（stage → 允许集合）
_STAGE_ALLOWED_TYPES: dict[int, set[str]] = {
    1: {"tunable_search"},
    2: {"tunable_search", "structural"},
    3: {"tunable_search", "structural", "logic"},
}

# 必填字段（所有 change_type 共用）
REQUIRED_FIELDS = {"target_issue", "hypothesis", "change_type", "expected_effect"}

# 重试上限
MAX_RETRIES = 3

# 禁用前缀：这些前缀的参数禁止进入 tunables（防误配置第二道保险）
_BANNED_PREFIXES = ("reward_", "goal_", "fall_", "telemetry_", "diagnose_")

# structural/logic patch 的 file 不得命中的 protected glob（阶段2 测量边界第①层，
# critic C1/M4）。这是三层防御中的最外层：parse_plan 校验 LLM 提案；真正兜底在
# mutate.allowed（第②层）与 apply_patch（第③层），因为 TDD 注入的 propose_fn 会绕过
# 本函数。glob 对 res:// 去前缀后的相对路径匹配（含 '..' 穿越也覆盖）。
_PATCH_PROTECTED_GLOBS = (
    "harness/**", "harness/*", ".git/**", "tests/**", "docs/**",
    "*/rl/game_agent.gd", "*/rl/telemetry.gd", "*/rl/recorder.gd",
    "rl/game_agent.gd", "rl/telemetry.gd", "rl/recorder.gd",
    "*game_agent.gd",
)

# 改动计划的 tool schema（用于 anthropic structured output）
_PLAN_TOOL = {
    "name": "submit_change_plan",
    "description": "提交一个游戏优化改动计划，供 Python 编排器执行。",
    "input_schema": {
        "type": "object",
        "properties": {
            "target_issue": {
                "type": "string",
                "description": "针对 report.json 里哪条 issue.id",
            },
            "hypothesis": {
                "type": "string",
                "description": "机制解释（结论+机制，Nova schema）",
            },
            "change_type": {
                "type": "string",
                "enum": ["tunable_search", "structural", "logic"],
                "description": "改动类型",
            },
            "search_space": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "range": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "required": ["key", "range"],
                },
                "description": "change_type==tunable_search 时必填：贝叶斯搜索空间",
            },
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "anchor": {"type": "string"},
                        "new": {"type": "string"},
                    },
                    "required": ["file", "anchor", "new"],
                },
                "description": "change_type==structural|logic 时填写的 patch 列表",
            },
            "expected_effect": {
                "type": "string",
                "description": "预期效果描述",
            },
            "confidence": {
                "type": "number",
                "description": "置信度 0~1",
            },
        },
        "required": ["target_issue", "hypothesis", "change_type", "expected_effect"],
    },
}


# ---------------------------------------------------------------------------
# parse_plan — 纯函数，校验 LLM 输出
# ---------------------------------------------------------------------------

def parse_plan(text: str, tunables: dict, stage: int = 1) -> dict:
    """
    把 LLM 返回的 JSON 文本解析并校验为改动计划 dict。

    Args:
        text:     LLM 返回的 JSON 字符串（可能是整个响应体，也可能是 tool input 的 JSON）。
        tunables: tunables.json 全量（spec §5.1），用于校验 search_space。
        stage:    优化阶段（默认 1）。
                  stage=1 只允许 tunable_search；
                  stage=2 增加 structural；stage=3 增加 logic。

    Returns:
        校验通过的改动计划 dict。

    Raises:
        ValueError: JSON 解析失败、必填字段缺失、change_type 非法、
                    stage 不允许该 change_type、search_space 的 key/range 不合法、
                    search_space 中 key 重复、range 反向/退化、key 含禁用前缀。
    """
    # 1. JSON 解析
    if not text or not text.strip():
        raise ValueError("JSON 解析失败：输入为空")
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败：{e}") from e

    if not isinstance(plan, dict) or not plan:
        raise ValueError("JSON 解析失败：期望非空 object，实际得到 " + repr(plan))

    # 2. 必填字段
    for field in REQUIRED_FIELDS:
        if field not in plan:
            raise ValueError(f"改动计划缺少必填字段：{field!r}")

    # 3. change_type 合法性
    change_type = plan["change_type"]
    if change_type not in VALID_CHANGE_TYPES:
        raise ValueError(
            f"change_type 非法：{change_type!r}，合法值：{sorted(VALID_CHANGE_TYPES)}"
        )

    # 4. 阶段限制：stage 1 只允许 tunable_search
    allowed_types = _STAGE_ALLOWED_TYPES.get(stage, VALID_CHANGE_TYPES)
    if change_type not in allowed_types:
        raise ValueError(
            f"stage {stage} 不允许 change_type={change_type!r}，"
            f"当前阶段只允许：{sorted(allowed_types)}"
        )

    # 4b. structural/logic：校验 patches（阶段2 测量边界第①层，critic C1/M4）。
    if change_type in {"structural", "logic"}:
        patches = plan.get("patches")
        if not patches or not isinstance(patches, list):
            raise ValueError(
                f"change_type=={change_type} 时 patches 不能为空")
        for i, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise ValueError(f"patches[{i}] 必须是 object")
            for k in ("file", "anchor", "new"):
                v = patch.get(k)
                if not isinstance(v, str) or not v:
                    raise ValueError(
                        f"patches[{i}] 缺少非空字符串字段 {k!r}")
            _reject_protected_patch_file(patch["file"], i)

    # 5. tunable_search：校验 search_space
    if change_type == "tunable_search":
        search_space = plan.get("search_space")
        if not search_space:
            raise ValueError("change_type==tunable_search 时 search_space 不能为空")
        params = tunables.get("params", {})

        # 5a. 重复 key 检测
        seen_keys: set[str] = set()
        for entry in search_space:
            key = entry.get("key")
            if key in seen_keys:
                raise ValueError(
                    f"search_space 中 key {key!r} 重复出现，每个参数只能出现一次"
                )
            seen_keys.add(key)

        for entry in search_space:
            key = entry.get("key")

            # 5b. 禁用前缀检测（第二道保险，防止 reward/goal/fall/telemetry/diagnose 误入）
            if any(key.startswith(prefix) for prefix in _BANNED_PREFIXES):
                raise ValueError(
                    f"search_space 中的 key {key!r} 含禁用前缀（"
                    f"{', '.join(_BANNED_PREFIXES)}），这类参数禁止进入 tunables"
                )

            # 5c. 白名单检测：key 必须在 tunables.params 中
            if key not in params:
                raise ValueError(
                    f"search_space 中的 key {key!r} 不在 tunables.params 里"
                )

            proposed_range = entry.get("range", [])
            tunable_range = params[key].get("range", [])
            if len(proposed_range) != 2 or len(tunable_range) != 2:
                raise ValueError(
                    f"search_space[{key!r}].range 格式错误：期望 [min, max]，"
                    f"实际得到 {proposed_range!r}，tunables.range={tunable_range!r}"
                )

            p_min, p_max = proposed_range
            t_min, t_max = tunable_range

            # 5d. 反向/退化范围检测（min 必须严格小于 max）
            if p_min >= p_max:
                raise ValueError(
                    f"search_space[{key!r}].range [{p_min}, {p_max}] 是反向或退化范围"
                    f"（要求 min < max）"
                )

            # 5e. 子范围检测（提议 range 必须 ⊆ 作者 range）
            if p_min < t_min or p_max > t_max:
                raise ValueError(
                    f"search_space[{key!r}].range [{p_min}, {p_max}] "
                    f"越界：tunables.range=[{t_min}, {t_max}]"
                )

    return plan


def _reject_protected_patch_file(file: str, idx: int) -> None:
    """structural/logic patch 的 file 命中测量边界即拒（第①层硬护栏，critic C1/M4）。

    规则（任一命中即拒）：
      - 去 res:// 前缀后含 '..' 段（路径穿越，可能逃出项目目录碰 harness/ 等）。
      - 命中 _PATCH_PROTECTED_GLOBS 任一。
      - basename 是测量装置文件（game_agent.gd / telemetry.gd / recorder.gd），
        无论它在哪个目录，都不准作为结构旋钮。
    """
    rel = file[len("res://"):] if file.startswith("res://") else file
    rel = rel.replace(os.sep, "/")
    if ".." in rel.split("/"):
        raise ValueError(
            f"patches[{idx}].file 含 '..' 段，疑似路径穿越，拒绝: {file!r}")
    base = rel.rsplit("/", 1)[-1]
    if base in {"game_agent.gd", "telemetry.gd", "recorder.gd"}:
        raise ValueError(
            f"patches[{idx}].file 指向测量装置文件 {base!r}，禁止作为结构/逻辑旋钮: {file!r}")
    for pattern in _PATCH_PROTECTED_GLOBS:
        if fnmatch.fnmatch(rel, pattern):
            raise ValueError(
                f"patches[{idx}].file 命中 protected glob {pattern!r}，拒绝: {file!r}")


# ---------------------------------------------------------------------------
# propose — 组 prompt + 调 LLM + 重试
# ---------------------------------------------------------------------------

def propose(
    report: dict,
    tunables: dict,
    memory: dict,
    stage: int = 1,
    max_retries: int = MAX_RETRIES,
    code_summary: str = "",
) -> dict:
    """
    向 Claude API 提交优化请求，返回解析后的改动计划。

    Args:
        report:      diagnose.py 产出的 report.json（dict）。
        tunables:    tunables.json（dict，spec §5.1）。
        memory:      memory.json（dict，spec §5.3），供 LLM 参考失败教训。
        stage:       优化阶段（1=仅 tunable_search，2=+structural，3=+logic）。
        max_retries: 解析失败最多重试次数（默认 MAX_RETRIES=3）。

    后端选择（LLM_BACKEND 环境变量，默认 auto）：
        - "anthropic"  : 用 anthropic SDK（需 ANTHROPIC_API_KEY）
        - "claude_cli" : 用本机 `claude -p` CLI（复用 Claude Code 订阅认证，免 API key）
        - "auto"（默认）: 有 ANTHROPIC_API_KEY → anthropic；否则有 claude CLI → claude_cli

    Returns:
        parse_plan 校验通过的改动计划 dict。

    Raises:
        ValueError: 超过重试上限仍无法得到合法计划。
        RuntimeError: 无可用后端（既无 ANTHROPIC_API_KEY 也无 claude CLI）。
    """
    prompt = _build_prompt(report, tunables, memory, stage, code_summary)
    backend = _select_backend()
    if backend == "claude_cli":
        return _propose_via_claude_cli(prompt, tunables, stage, max_retries)
    return _propose_via_anthropic(prompt, tunables, stage, max_retries)


def _select_backend() -> str:
    """决定用哪个 LLM 后端。"""
    b = os.environ.get("LLM_BACKEND", "auto").lower()
    if b in ("anthropic", "claude_cli"):
        return b
    # auto：显式设了 API key 用 SDK,否则有 claude CLI 就用它
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if shutil.which("claude"):
        return "claude_cli"
    raise RuntimeError(
        "无可用 LLM 后端：未设 ANTHROPIC_API_KEY 且找不到 claude CLI。"
        "请设 ANTHROPIC_API_KEY,或安装 Claude Code CLI(claude)。"
    )


def _propose_via_anthropic(
    prompt: str, tunables: dict, stage: int, max_retries: int
) -> dict:
    """anthropic SDK 后端（structured output via tool use）。"""
    if anthropic is None:
        raise RuntimeError("anthropic SDK 未安装。请运行：pip install anthropic")

    client = anthropic.Anthropic()  # 自动读取 ANTHROPIC_API_KEY 环境变量
    last_error: Exception | None = None
    for _ in range(max_retries):
        response = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8"),
            max_tokens=1024,
            tools=[_PLAN_TOOL],
            tool_choice={"type": "tool", "name": "submit_change_plan"},
            messages=[{"role": "user", "content": prompt}],
        )
        plan_input = _extract_tool_input(response)
        try:
            return parse_plan(json.dumps(plan_input), tunables, stage)
        except ValueError as e:
            last_error = e

    raise ValueError(
        f"LLM(anthropic)在 {max_retries} 次尝试后仍未返回合法改动计划。"
        f"最后错误：{last_error}"
    )


def _propose_via_claude_cli(
    prompt: str, tunables: dict, stage: int, max_retries: int
) -> dict:
    """claude CLI 后端：`claude -p --output-format json --json-schema <schema>`。

    复用本机 Claude Code 认证,无需 ANTHROPIC_API_KEY。CLI 输出信封含
    structured_output(已解析对象)/ result(JSON 字符串)/ is_error。
    """
    schema = json.dumps(_PLAN_TOOL["input_schema"], ensure_ascii=False)
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", schema,
        prompt,
    ]
    last_error: Exception | None = None
    for _ in range(max_retries):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=CLAUDE_CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            last_error = e
            continue
        if proc.returncode != 0:
            last_error = RuntimeError(
                f"claude CLI rc={proc.returncode}: {(proc.stderr or '')[-500:]}"
            )
            continue
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            last_error = RuntimeError(f"claude CLI 输出非 JSON：{e}")
            continue
        if envelope.get("is_error"):
            last_error = RuntimeError(
                f"claude CLI 报错：{envelope.get('result') or envelope.get('subtype')}"
            )
            continue
        # 优先用已解析的 structured_output,回退到 result(JSON 字符串)
        plan_obj = envelope.get("structured_output")
        if plan_obj is None:
            try:
                plan_obj = json.loads(envelope.get("result", ""))
            except (json.JSONDecodeError, TypeError) as e:
                last_error = RuntimeError(f"claude CLI 无 structured_output 且 result 不可解析：{e}")
                continue
        try:
            return parse_plan(json.dumps(plan_obj), tunables, stage)
        except ValueError as e:
            last_error = e

    raise ValueError(
        f"LLM(claude_cli)在 {max_retries} 次尝试后仍未返回合法改动计划。"
        f"最后错误：{last_error}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_prompt(report: dict, tunables: dict, memory: dict, stage: int,
                  code_summary: str = "") -> str:
    """组装发给 LLM 的 prompt。

    code_summary（阶段2≥）：可 patch 的结构摘要（如 train_map.tscn 的 MidPlatform
    anchor 多行块 + 改动指引），stage>=2 时拼入 prompt 引导 structural 提案；
    默认空串 → 阶段1 prompt 零回归。
    """
    # 阶段限制
    stage_constraint = {
        1: "【阶段限制】当前为阶段1：change_type 只能是 tunable_search（仅调数值参数），不得提 structural 或 logic 改动。",
        2: "【阶段限制】当前为阶段2：change_type 可以是 tunable_search 或 structural（.tscn patch）。",
        3: "【阶段限制】当前为阶段3：change_type 可以是 tunable_search、structural 或 logic（.gd patch）。",
    }.get(stage, "")

    # 失败记忆摘要（避免重复犯错）
    rounds = memory.get("rounds", [])
    failed_rounds = [r for r in rounds if not r.get("accepted", True)]
    memory_summary = ""
    if failed_rounds:
        items = []
        for r in failed_rounds[-5:]:  # 最近 5 条失败记录
            items.append(
                f"  - 轮次 {r.get('round', '?')}：{r.get('summary', '?')} "
                f"→ 拒绝原因：{r.get('reason', '?')}"
            )
        memory_summary = "【失败记忆（请勿重复这些错误）】\n" + "\n".join(items)

    # tunables schema（只取 params 部分给 LLM 看）
    params_summary = json.dumps(tunables.get("params", {}), ensure_ascii=False, indent=2)

    # report issues
    issues_text = json.dumps(report.get("issues", []), ensure_ascii=False, indent=2)
    summary_text = json.dumps(report.get("summary", {}), ensure_ascii=False, indent=2)

    # 阶段2≥：可 patch 的结构摘要（anchor + 改动指引），引导 structural 提案。
    code_summary_block = ""
    if stage >= 2 and code_summary:
        code_summary_block = "\n【可 patch 的结构摘要（structural 改动只能动这些）】\n" + code_summary

    return f"""你是一个游戏平衡优化专家。请分析以下诊断报告，提出一个**可验证的改动假设**。

【铁律】
1. 提假设，而非保证——你不知道改动后的确切效果，只是提出假设。
2. 参考失败记忆，避免重复同类错误。
3. 不得修改 protected 路径（harness/**、.git/**、tests/**、docs/**）。
4. search_space 的 range 必须在 tunables.params 对应参数的 range 范围内。
5. 每次只提 1 个改动计划，聚焦最高优先级的 issue。

{stage_constraint}

【当前诊断 summary】
{summary_text}

【当前 issues（按 severity 排序）】
{issues_text}

【可调参数（tunables.params）】
{params_summary}

{memory_summary}
{code_summary_block}

请提交一个改动计划(严格符合改动计划 JSON schema:target_issue/hypothesis/change_type/expected_effect 必填,tunable_search 时含 search_space)。
""".strip()


def _extract_tool_input(response: Any) -> dict:
    """从 anthropic 响应中提取 tool_use input（已是 dict）。"""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    # fallback：尝试从 text block 解析 JSON
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text.strip()
            if text.startswith("{"):
                return json.loads(text)
    raise ValueError("LLM 响应中未找到 tool_use block 或可解析的 JSON")
