# 设计文档 · 主观体验诊断层(Procedural Personas + LLM 相对裁判)

> 日期: 2026-06-28
> 项目: godot-rl-evolver
> 状态: 设计草案(待用户审定后再写实现计划)
> 关联调研: `.omc/research/2026-06-28-subjective-playtesting-signals.md`
> 定位: 把「发现问题」这条线从**客观规则诊断**(diagnose.py 的 8 条阈值)扩展到**主观体验**——
> 在领域硬约束下做到「领域正解」:**相对而非绝对、对谁而非客观、发现优先而非直接进优化锚**。

## 1. 背景与目标

现状:RL agent(单一冻结策略)试玩 → telemetry → `diagnose.py`(规则)→ `report.json` → LLM 优化闭环。
规则诊断只能答**预设的 8 类客观问题**,答不了「这关挫败吗 / 节奏断在哪 / 对新手是不是劝退」。

调研三条硬结论(决定本设计的形态,违反则不可靠):
1. **主观不可直接测** → 代理 + 三角验证 + **相对/序数排序**(人打绝对分不稳,排序稳)。
2. **「好玩」是相对「对谁」的** → 不测客观好玩,测**对不同玩家类型的体验差**(procedural personas,领域最成熟的"超越难度"正解)。
3. **LLM 评主观:只信相对难度/相对排序**(GPT-4 CoT vs 人类 r≈0.87),**绝对连续 engagement 不可靠**(打不过 baseline)。

**目标**:新增一个**主观体验诊断层**,由两块可独立、可组合的能力构成:
- **A. Procedural personas(玩)**:一组**风格各异的冻结策略**,产出「对谁而言」的体验剖面。
- **B. LLM 相对裁判(评)**:对**成对**轨迹(baseline vs candidate,或 persona A vs B)做主观维度的**相对**判断,输出带证据的"软问题"。

**In scope**
- persona 定义机制(reward-shaping profile → warm-start 冻结策略)+ 多 persona 试玩编排。
- 跨 persona 体验剖面诊断(per-persona report 聚合 → "对谁难/对谁无聊")。
- LLM 相对裁判模块(成对轨迹 → 结构化相对软问题)。
- telemetry 增一条**粗粒度轨迹流**(供 LLM 裁判读)。
- 全部产物**默认只进"发现/报告"**,不自动进优化锚。

**Out of scope(本设计明确不做)**
- LLM/VLM 给主观维度打**绝对连续分**(调研证实不可靠)。
- 把软指标**直接当优化目标**喂闭环(Goodhart 红线;留作后续,需专门护栏设计,见 §6)。
- 学习型 affect 模型(需人类标注数据集,单机开源工具不具备;见调研 §②)。
- 多模态视觉裁判(recorder 截图)——架构预留,首版不做(先文本轨迹)。

## 2. 设计原则(全部来自调研硬约束)

1. **相对优先**:LLM 裁判只做**成对比较**("A 比 B 更挫败吗"),永不吐绝对分。
2. **对谁而非客观**:用 personas 把"测不了的客观主观"换成"测得了的'对谁而言'"。
3. **发现优先**:主观信号默认只产**报告**;进优化锚是独立的、后续的、需额外护栏的决定。
4. **测量完整性延续**:persona 的 reward profile 是**冻结的仪器面板**,优化闭环**永不**改任何 persona 的 reward——和阶段1-3 的 protected 边界一致(reward 仍是尺子,只是现在有"一组尺子")。
5. **防 Goodhart**:软指标若将来进锚,必须 persona 多样性 + hold-out + 人类抽查(§6)。
6. **复用不重造**:试玩/telemetry/诊断复用现有环;新增只是"多 persona 编排"+"LLM 评判"。

## 3. 架构总览

```
┌─ Personas(玩,一组冻结策略)────────┐     ┌─ 主观诊断(评)──────────────────────┐
│ persona profile(reward 权重集)     │     │  per-persona report(复用 diagnose.py) │
│   aggressive / cautious /          │ 试玩 │   → 跨 persona 聚合:对谁难/对谁无聊   │
│   speedrunner / explorer           │────►│                                       │
│ 每 profile = warm-start 微调后冻结  │     │  LLM 相对裁判:                         │
│ (instrument 面板,优化闭环不可改)   │     │   成对轨迹(base vs cand / persA vs B) │
└─────────────────────────────────────┘     │   → 结构化"软问题"(挫败/节奏/单调…)   │
         ▲ telemetry(+粗粒度轨迹流)          │   仅相对,带证据+置信度                 │
         └────────────────────────────────►  └────────────────────────────────────────┘
```

**两块解耦**:personas 不依赖 LLM 裁判;LLM 裁判对单 agent 也能用(base vs cand)。组合时威力最大:
**LLM 裁判逐 persona 比较** → "对谨慎型,候选关更挫败;对速通型,节奏更好"。

## 4. 组件设计

### 4.1 Procedural personas(玩)

**persona = 一个 reward-shaping profile**(现有 game_agent.gd 的 reward 权重集的一组取值):

| persona | 倾向 | reward 权重侧重(基于现有系数) |
|---|---|---|
| aggressive | 好战 | 提高 kill(+25)/伤害(×0.1)权重,降时间惩罚 |
| cautious | 求稳 | 提高受伤惩罚,降 kill 权重,提高存活倾向 |
| speedrunner | 速通 | 提高 progress(×0.01)/时间惩罚,降 kill/explore |
| explorer | 探索 | 提高覆盖/探索塑形,降 progress 紧迫 |

**怎么造(关键工程决策)**:现有 reward 硬编码在 game_agent.gd(protected)。本设计**不在优化闭环里改 reward**,而是:
- 把 reward 权重抽成一份**persona 配置**(独立文件,如 `personas/*.json`),game_agent.gd 读取**当前 persona 权重**(类似 Tunables,但属"仪器配置"非"游戏旋钮")。
- 每个 persona:用对应权重 **warm-start 短训**(现有 `WARM_START` 已支持)得到一个**冻结策略**,存为独立 MODEL。
- 试玩编排:对一关,**依次用每个 persona 的冻结策略**跑 `EVAL_SEEDS` 局 → 每 persona 一份 telemetry → 一份 per-persona report。
- **CARMI 备选(更省,后续)**:单一可配置 agent + 把"风格向量"塞进 obs,训一次跨风格。首版用"多冻结策略"更简单直接;CARMI 留作优化。

**测量完整性**:persona 的 reward profile 是**冻结仪器面板**——优化闭环的 PROTECTED 边界把 `personas/*.json` 和 game_agent.gd 一并护住,闭环**永不**改任何 persona 的 reward。这与"reward 是尺子"完全一致,只是从"一把尺子"变成"一组校准好的尺子"。

**产物**:跨 persona 剖面,如:
```
难度: aggressive 通关 70% / cautious 40%(卡缺口) / speedrunner 85% / explorer 55%
→ 软结论: "此关对谨慎型偏难(缺口处),对速通型偏易" (相对各 persona 自身基线)
```

### 4.2 LLM 相对裁判(评)

**输入**:**成对**轨迹 + 两份 report 摘要。轨迹来自 telemetry 的**粗粒度轨迹流**(§5):位置/HP/事件按段采样的序列(文本)。
**输出**(structured,强制 JSON):对每个主观维度给**相对裁决 + 证据 + 置信度**:
```jsonc
{
  "dimension": "frustration",            // frustration|pacing|monotony|tension|flow
  "verdict": "A_worse",                  // A_worse | B_worse | tie  (相对,无绝对分)
  "evidence": "A 在 x=600-680 段 4 次连续坠落、动作熵骤升;B 同段一次通过",
  "confidence": 0.7
}
```
- **只做相对**:`verdict ∈ {A_worse, B_worse, tie}`,**没有**"frustration=7.3"这种绝对分。
- **开放式补充**:除固定维度外,允许 LLM 提"未预设"的主观问题(回应"开放式发现"诉求),同样要证据+相对措辞。
- **后端复用**:沿用现有 `llm_propose` 的 anthropic / claude_cli 双后端。
- **用途**:产"软问题"进**报告**(`report.soft_issues` 或独立 `llm_report.json`);默认**不**进优化锚。

### 4.3 跨 persona × LLM 裁判的组合
对每个 persona,用 LLM 裁判比较"改前 vs 改后"该 persona 的轨迹 → 得到**"这次改动对每类玩家的体验影响"**矩阵。这是领域最佳实践组合(personas 玩 + LLM 相对评 + 人类兜底)。

## 5. 数据契约

### 5.1 persona 配置 `personas/<name>.json`(仪器面板,protected)
```jsonc
{
  "name": "cautious",
  "reward_weights": {
    "progress": 0.01, "time_penalty": 0.002, "damage": 0.1, "kill": 10.0,
    "combat_shape": 0.5, "hurt_penalty": 1.0, "gap_edge_jump": 1.0,
    "gap_cross": 8.0, "goal": 30.0, "fall": 10.0, "hp_fail": 10.0
  },
  "model": "<外部路径,该 persona 的冻结策略,不入库>"
}
```
> game_agent.gd 把硬编码 reward 系数改为读 `当前 persona.reward_weights`(默认 = 现值,保持现有行为)。

### 5.2 粗粒度轨迹流(telemetry 新增,供 LLM 裁判)
每局除现有聚合行外,增一条**分段轨迹**(避免逐帧爆量):把一局按 N 段(如每 100 帧)采样,每段记 `{seg, x, hp, return_delta, events}`。LLM 读这个序列即可推理"哪段挫败/无聊/紧张"。
> 契约同步:telemetry.gd 落盘 ↔ LLM 裁判读取,改一边同步另一边(沿用 spec §4.1 纪律)。

### 5.3 软问题(进报告,不进锚)
```jsonc
{ "type": "soft", "dimension": "frustration", "persona": "cautious",
  "verdict": "candidate_worse", "evidence": "...", "confidence": 0.7,
  "agent_relative": true, "for_persona": true }
```

## 6. Goodhart 护栏(若将来软指标进优化锚)
默认**不进锚**。若将来要让闭环优化主观体验,必须:
- **persona 多样性约束**:接受一个改动须对**多数 persona**不变差(避免只讨好一类),呼应调研"persona-conscious 适应可缓解退化"。
- **hold-out persona**:留一个 persona 不参与优化、只做验证,检测过拟合到被优化指标。
- **人类抽查**:软指标驱动的接受改动,保留人事后审 git 历史的出口。
- **绝不单押一个软指标当 reward**(EDRL 把手工 fun 烤进 reward 的教科书反例)。

## 7. 分阶段实施(按价值/成本)

| 阶段 | 内容 | 成本 | 价值 |
|---|---|---|---|
| **S1** | LLM 相对裁判(单 agent,base vs cand)+ telemetry 轨迹流 | 低(复用 LLM 后端,无重训) | 立刻有"开放式主观发现",当周可验 |
| **S2** | Procedural personas(多冻结策略)+ 跨 persona 剖面诊断 | 中高(每 persona warm-start 训一次) | "对谁而言",领域正解 |
| **S3** | 组合:LLM 裁判逐 persona 比较 | 中 | 最佳实践全组合 |
| **S4(可选)** | 软指标进锚 + §6 护栏 / CARMI 单一可配置 agent | 高 | 真正"优化体验",风险最高 |

建议先 **S1**(便宜、自包含、立刻产出开放式主观发现),验稳后再 S2。

## 8. 风险与权衡
- **personas 真实性**:reward-shaping 出的"风格"是否真像对应人类玩家?——调研承认这是代理,非真人;价值在**相对差异**而非绝对保真。
- **LLM 相对裁判的可信度**:调研只在"相对难度"验到 r≈0.87,挫败/节奏的文本轨迹评估**缺直接 benchmark**(开放问题)。故首版定位**发现/提示**,需人复核,不可当定论。
- **成本**:S2 每 persona 一次 warm-start 训练 + 每关多 persona 试玩,成本随 persona 数线性放大。
- **轨迹流喂 LLM 的 token 成本**:粗粒度分段采样控制;必要时只喂关键段(死亡/卡顿附近)。
- **Goodhart**:见 §6,默认不进锚即规避。

## 9. 文件清单(预估,实现计划再细化)
| 文件 | 动作 |
|---|---|
| `harness/personas.py` | 🆕 persona 配置加载 + 多 persona 试玩编排 |
| `harness/llm_judge.py` | 🆕 LLM 相对裁判(成对轨迹 → 软问题) |
| `harness/telemetry.gd` | ✏️ 增粗粒度轨迹流落盘 |
| `harness/diagnose.py` | ✏️ 跨 persona 剖面聚合(per-persona report → 体验剖面) |
| `testbed_platformer/rl/game_agent.gd` | ✏️ reward 系数改读 persona 权重(仪器配置;仍 protected) |
| `personas/*.json` | 🆕 persona 仪器面板配置 |
| `tests/test_llm_judge.py` / `test_personas.py` | 🆕 解析/聚合/相对裁决的单测(LLM 调用 mock) |
| `README.md` / `CLAUDE.md` | ✏️ 主观体验层用法 + 进度 |
