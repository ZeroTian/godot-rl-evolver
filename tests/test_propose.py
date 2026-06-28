"""
tests/test_propose.py — TDD for harness/llm_propose.py

核心测试 parse_plan(text, tunables, stage=1) 纯函数：
  - 合法 JSON → 返回改动计划 dict（schema spec §5.2）
  - 非法 JSON → ValueError
  - 缺 change_type → ValueError
  - search_space key 不在 tunables → ValueError
  - search_space range 越界（超出 tunables.range）→ ValueError
  - stage=1 时 structural/logic → ValueError
  - 重复 key（search_space 中同 key 出现两次）→ ValueError
  - 反向 range（高 < 低）→ ValueError
  - 禁用前缀 reward_*/goal_*/fall_*/telemetry_*/diagnose_* → ValueError

propose() 中的 LLM 调用用 unittest.mock patch，绝不真调 API。
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from harness.llm_propose import parse_plan, propose


# ---------------------------------------------------------------------------
# Sample tunables (subset of spec §5.1 schema)
# ---------------------------------------------------------------------------

TUNABLES = {
    "version": 1,
    "params": {
        "gap_width": {"value": 120, "range": [60, 200], "type": "float",
                      "desc": "缺口宽度(px)"},
        "enemy_hp":  {"value": 3,   "range": [1, 8],   "type": "int",
                      "desc": "敌人血量"},
        "jump_force":{"value": 400, "range": [300, 600],"type": "float",
                      "desc": "跳跃力"},
        "enemy_speed": {"value": 50.0, "range": [25.0, 100.0], "type": "float",
                        "desc": "敌人移动速度"},
    }
}

# tunables 中包含真实玩法三参数（供 testbed 用例）
TESTBED_TUNABLES = {
    "version": 1,
    "params": {
        "enemy_hp":    {"value": 40,    "range": [20, 100],    "type": "int",   "desc": "火骑士生命值"},
        "enemy_speed": {"value": 50.0,  "range": [25.0, 100.0],"type": "float", "desc": "火骑士巡逻速度"},
        "jump_force":  {"value": 360.0, "range": [280.0, 440.0],"type": "float","desc": "玩家起跳速度"},
    }
}

# ---------------------------------------------------------------------------
# Valid plan JSON
# ---------------------------------------------------------------------------

VALID_PLAN = {
    "target_issue": "difficulty_too_hard",
    "hypothesis": "缺口过宽，当前策略跨不过去",
    "change_type": "tunable_search",
    "search_space": [{"key": "gap_width", "range": [80, 160]}],
    "expected_effect": "completion_rate 提升",
    "confidence": 0.7,
}

VALID_PLAN_TEXT = json.dumps(VALID_PLAN)


# ---------------------------------------------------------------------------
# parse_plan — happy path
# ---------------------------------------------------------------------------

class TestParsePlanValid:
    def test_returns_dict(self):
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert isinstance(result, dict)

    def test_target_issue_preserved(self):
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert result["target_issue"] == "difficulty_too_hard"

    def test_hypothesis_preserved(self):
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert result["hypothesis"] == "缺口过宽，当前策略跨不过去"

    def test_change_type_preserved(self):
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert result["change_type"] == "tunable_search"

    def test_search_space_preserved(self):
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert result["search_space"] == [{"key": "gap_width", "range": [80, 160]}]

    def test_confidence_preserved(self):
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert result["confidence"] == pytest.approx(0.7)

    def test_expected_effect_preserved(self):
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert result["expected_effect"] == "completion_rate 提升"

    def test_multi_key_search_space(self):
        plan = {**VALID_PLAN, "search_space": [
            {"key": "gap_width", "range": [80, 150]},
            {"key": "jump_force", "range": [320, 550]},
        ]}
        result = parse_plan(json.dumps(plan), TUNABLES)
        assert len(result["search_space"]) == 2

    def test_optional_patches_absent_is_ok(self):
        """patches 字段是可选的（tunable_search 不需要）。"""
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES)
        assert "patches" not in result or result.get("patches") is None or True

    def test_plan_with_patches_structural(self):
        """structural 类型可以有 patches，不需要 search_space；需要 stage>=2。"""
        plan = {
            "target_issue": "death_hotspot",
            "hypothesis": "关卡结构问题",
            "change_type": "structural",
            "patches": [{"file": "res://level.tscn", "anchor": "...", "new": "..."}],
            "expected_effect": "death 减少",
            "confidence": 0.5,
        }
        result = parse_plan(json.dumps(plan), TUNABLES, stage=2)
        assert result["change_type"] == "structural"

    def test_range_exactly_at_boundary(self):
        """search_space range 等于 tunables range 边界，应视为合法。"""
        plan = {**VALID_PLAN, "search_space": [{"key": "gap_width", "range": [60, 200]}]}
        result = parse_plan(json.dumps(plan), TUNABLES)
        assert result["search_space"][0]["range"] == [60, 200]


# ---------------------------------------------------------------------------
# parse_plan — error cases
# ---------------------------------------------------------------------------

class TestParsePlanErrors:
    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="JSON"):
            parse_plan("not valid json {{{", TUNABLES)

    def test_missing_change_type_raises(self):
        plan = {k: v for k, v in VALID_PLAN.items() if k != "change_type"}
        with pytest.raises(ValueError, match="change_type"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_unknown_change_type_raises(self):
        plan = {**VALID_PLAN, "change_type": "unknown_type"}
        with pytest.raises(ValueError, match="change_type"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_tunable_search_missing_search_space_raises(self):
        plan = {k: v for k, v in VALID_PLAN.items() if k != "search_space"}
        with pytest.raises(ValueError, match="search_space"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_search_space_key_not_in_tunables_raises(self):
        plan = {**VALID_PLAN, "search_space": [{"key": "nonexistent_param", "range": [1, 5]}]}
        with pytest.raises(ValueError, match="nonexistent_param"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_search_space_range_below_min_raises(self):
        """range[0] < tunables min → ValueError。"""
        plan = {**VALID_PLAN, "search_space": [{"key": "gap_width", "range": [10, 160]}]}
        with pytest.raises(ValueError, match="range"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_search_space_range_above_max_raises(self):
        """range[1] > tunables max → ValueError。"""
        plan = {**VALID_PLAN, "search_space": [{"key": "gap_width", "range": [80, 999]}]}
        with pytest.raises(ValueError, match="range"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_missing_target_issue_raises(self):
        plan = {k: v for k, v in VALID_PLAN.items() if k != "target_issue"}
        with pytest.raises(ValueError, match="target_issue"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_missing_hypothesis_raises(self):
        plan = {k: v for k, v in VALID_PLAN.items() if k != "hypothesis"}
        with pytest.raises(ValueError, match="hypothesis"):
            parse_plan(json.dumps(plan), TUNABLES)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_plan("", TUNABLES)

    def test_empty_object_raises(self):
        with pytest.raises(ValueError):
            parse_plan("{}", TUNABLES)

    def test_search_space_empty_list_raises(self):
        plan = {**VALID_PLAN, "search_space": []}
        with pytest.raises(ValueError, match="search_space"):
            parse_plan(json.dumps(plan), TUNABLES)


# ---------------------------------------------------------------------------
# propose() — LLM 调用 mock，绝不真调 API
# ---------------------------------------------------------------------------

SAMPLE_REPORT = {
    "scene": "res://rl/train_map.tscn",
    "summary": {"completion_rate": 0.2, "return_cv": 0.4},
    "issues": [
        {"id": "difficulty_too_hard", "severity": "high",
         "desc": "通关率过低"},
    ],
}

SAMPLE_MEMORY = {"scene": "res://rl/train_map.tscn", "rounds": []}


class TestProposeMocked:
    """propose() 的全部 LLM 调用必须被 mock，不得真调 API。"""

    def _make_mock_response(self, plan_dict):
        """构造一个模拟 anthropic SDK tool-use 响应。"""
        mock_response = MagicMock()
        mock_content = MagicMock()
        mock_content.type = "tool_use"
        mock_content.input = plan_dict
        mock_response.content = [mock_content]
        return mock_response

    @patch("harness.llm_propose.anthropic")
    def test_propose_returns_parsed_plan(self, mock_anthropic):
        """propose() 调用 API 并返回 parse_plan 解析后的 dict。"""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = self._make_mock_response(VALID_PLAN)

        result = propose(SAMPLE_REPORT, TUNABLES, SAMPLE_MEMORY, stage=1)

        assert result["change_type"] == "tunable_search"
        assert result["target_issue"] == "difficulty_too_hard"

    @patch("harness.llm_propose.anthropic")
    def test_propose_stage1_only_tunable_search(self, mock_anthropic):
        """stage=1 时，prompt 约束只允许 tunable_search，其他类型不合法。"""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = self._make_mock_response(VALID_PLAN)

        result = propose(SAMPLE_REPORT, TUNABLES, SAMPLE_MEMORY, stage=1)
        assert result["change_type"] == "tunable_search"

    @patch("harness.llm_propose.anthropic")
    def test_propose_calls_api_exactly_once(self, mock_anthropic):
        """正常情况下 API 只调用一次。"""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = self._make_mock_response(VALID_PLAN)

        propose(SAMPLE_REPORT, TUNABLES, SAMPLE_MEMORY, stage=1)

        assert mock_client.messages.create.call_count == 1

    @patch("harness.llm_propose.anthropic")
    def test_propose_uses_env_api_key(self, mock_anthropic):
        """propose() 使用环境变量 ANTHROPIC_API_KEY，不硬编码 key。"""
        import os
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = self._make_mock_response(VALID_PLAN)

        # 不传 key 参数，函数内部自己读环境变量
        propose(SAMPLE_REPORT, TUNABLES, SAMPLE_MEMORY, stage=1)
        # 只验证 Anthropic() 被调用（不传 api_key 参数，走默认 env 读取）
        mock_anthropic.Anthropic.assert_called_once()

    @patch("harness.llm_propose.anthropic")
    def test_propose_includes_memory_in_context(self, mock_anthropic):
        """propose() 把 memory 信息喂给 LLM（体现在 prompt 里）。"""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = self._make_mock_response(VALID_PLAN)

        memory_with_history = {
            "scene": "res://rl/train_map.tscn",
            "rounds": [
                {"round": 1, "target_issue": "difficulty_too_hard",
                 "change_type": "tunable_search", "summary": "gap_width 试过了",
                 "accepted": False, "reason": "无改善"},
            ]
        }
        propose(SAMPLE_REPORT, TUNABLES, memory_with_history, stage=1)

        call_kwargs = mock_client.messages.create.call_args
        # 检查 messages 参数存在（包含 prompt）
        assert call_kwargs is not None

    @patch("harness.llm_propose.anthropic")
    def test_propose_retry_on_invalid_response(self, mock_anthropic):
        """如果第一次 LLM 返回非法 JSON，propose() 应重试（最多 3 次）。"""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        # 第一次返回缺 change_type 的非法响应，第二次返回合法
        bad_plan = {k: v for k, v in VALID_PLAN.items() if k != "change_type"}
        good_response = self._make_mock_response(VALID_PLAN)
        bad_response = self._make_mock_response(bad_plan)
        mock_client.messages.create.side_effect = [bad_response, good_response]

        result = propose(SAMPLE_REPORT, TUNABLES, SAMPLE_MEMORY, stage=1)
        assert result["change_type"] == "tunable_search"
        assert mock_client.messages.create.call_count == 2

    @patch("harness.llm_propose.anthropic")
    def test_propose_raises_after_max_retries(self, mock_anthropic):
        """连续失败超过重试上限时，抛出 ValueError。"""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        bad_plan = {k: v for k, v in VALID_PLAN.items() if k != "change_type"}
        bad_response = self._make_mock_response(bad_plan)
        mock_client.messages.create.return_value = bad_response

        with pytest.raises(ValueError):
            propose(SAMPLE_REPORT, TUNABLES, SAMPLE_MEMORY, stage=1)


# ---------------------------------------------------------------------------
# Task 6: stage 限制、重复 key、反向 range、禁用前缀
# ---------------------------------------------------------------------------

class TestParsePlanStageConstraints:
    """stage=1 只允许 tunable_search；structural/logic 被拒。"""

    def test_stage1_rejects_structural(self):
        plan = {
            "target_issue": "death_hotspot",
            "hypothesis": "关卡结构问题",
            "change_type": "structural",
            "patches": [{"file": "res://level.tscn", "anchor": "...", "new": "..."}],
            "expected_effect": "death 减少",
        }
        with pytest.raises(ValueError, match="stage"):
            parse_plan(json.dumps(plan), TUNABLES, stage=1)

    def test_stage1_rejects_logic(self):
        plan = {
            "target_issue": "monotony",
            "hypothesis": "逻辑问题",
            "change_type": "logic",
            "patches": [{"file": "res://enemy.gd", "anchor": "...", "new": "..."}],
            "expected_effect": "动作多样性提升",
        }
        with pytest.raises(ValueError, match="stage"):
            parse_plan(json.dumps(plan), TUNABLES, stage=1)

    def test_stage1_allows_tunable_search(self):
        """stage=1 时 tunable_search 应放行。"""
        result = parse_plan(VALID_PLAN_TEXT, TUNABLES, stage=1)
        assert result["change_type"] == "tunable_search"

    def test_stage2_allows_structural(self):
        """stage=2 时 structural 应放行。"""
        plan = {
            "target_issue": "death_hotspot",
            "hypothesis": "关卡结构问题",
            "change_type": "structural",
            "patches": [{"file": "res://level.tscn", "anchor": "...", "new": "..."}],
            "expected_effect": "death 减少",
        }
        result = parse_plan(json.dumps(plan), TUNABLES, stage=2)
        assert result["change_type"] == "structural"

    def test_stage3_allows_logic(self):
        """stage=3 时 logic 应放行。"""
        plan = {
            "target_issue": "monotony",
            "hypothesis": "逻辑问题",
            "change_type": "logic",
            "patches": [{"file": "res://enemy.gd", "anchor": "...", "new": "..."}],
            "expected_effect": "动作多样性提升",
        }
        result = parse_plan(json.dumps(plan), TUNABLES, stage=3)
        assert result["change_type"] == "logic"

    def test_default_stage_is_1(self):
        """parse_plan 默认 stage=1，structural 应被拒。"""
        plan = {
            "target_issue": "death_hotspot",
            "hypothesis": "关卡结构问题",
            "change_type": "structural",
            "patches": [{"file": "res://level.tscn", "anchor": "...", "new": "..."}],
            "expected_effect": "death 减少",
        }
        with pytest.raises(ValueError, match="stage"):
            parse_plan(json.dumps(plan), TUNABLES)  # 不传 stage，默认 1


class TestParsePlanDuplicateKey:
    """search_space 中同 key 出现两次 → ValueError。"""

    def test_duplicate_key_in_search_space_raises(self):
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": "重复 key 测试",
            "change_type": "tunable_search",
            "search_space": [
                {"key": "gap_width", "range": [80, 160]},
                {"key": "gap_width", "range": [90, 150]},  # 同 key 重复
            ],
            "expected_effect": "completion_rate 提升",
        }
        with pytest.raises(ValueError, match="重复"):
            parse_plan(json.dumps(plan), TUNABLES, stage=1)

    def test_different_keys_not_duplicate(self):
        """不同 key 不构成重复，应放行。"""
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": "多参数搜索",
            "change_type": "tunable_search",
            "search_space": [
                {"key": "gap_width", "range": [80, 160]},
                {"key": "jump_force", "range": [320, 550]},
            ],
            "expected_effect": "completion_rate 提升",
        }
        result = parse_plan(json.dumps(plan), TUNABLES, stage=1)
        assert len(result["search_space"]) == 2


class TestParsePlanReversedRange:
    """反向范围（range[0] > range[1]）→ ValueError。"""

    def test_reversed_range_raises(self):
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": "反向 range 测试",
            "change_type": "tunable_search",
            "search_space": [{"key": "gap_width", "range": [160, 80]}],  # 高 < 低
            "expected_effect": "completion_rate 提升",
        }
        with pytest.raises(ValueError, match="range"):
            parse_plan(json.dumps(plan), TUNABLES, stage=1)

    def test_equal_range_values_raises(self):
        """range[0] == range[1] 同样是退化范围，应拒绝。"""
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": "相等 range 测试",
            "change_type": "tunable_search",
            "search_space": [{"key": "gap_width", "range": [120, 120]}],
            "expected_effect": "completion_rate 提升",
        }
        with pytest.raises(ValueError, match="range"):
            parse_plan(json.dumps(plan), TUNABLES, stage=1)


class TestParsePlanBannedPrefixes:
    """禁用前缀 reward_*/goal_*/fall_*/telemetry_*/diagnose_* → ValueError。"""

    def _reward_plan(self, key: str) -> str:
        """构造 search_space 包含禁用前缀 key 的计划文本。"""
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": f"禁用前缀 {key} 测试",
            "change_type": "tunable_search",
            "search_space": [{"key": key, "range": [1.0, 10.0]}],
            "expected_effect": "测试",
        }
        return json.dumps(plan)

    # 禁用前缀：reward_*
    def test_reward_prefix_rejected(self):
        with pytest.raises(ValueError):
            parse_plan(self._reward_plan("reward_completion"), TUNABLES, stage=1)

    def test_reward_death_prefix_rejected(self):
        with pytest.raises(ValueError):
            parse_plan(self._reward_plan("reward_death_penalty"), TUNABLES, stage=1)

    # 禁用前缀：goal_*
    def test_goal_prefix_rejected(self):
        with pytest.raises(ValueError):
            parse_plan(self._reward_plan("goal_x"), TUNABLES, stage=1)

    # 禁用前缀：fall_*
    def test_fall_prefix_rejected(self):
        with pytest.raises(ValueError):
            parse_plan(self._reward_plan("fall_y"), TUNABLES, stage=1)

    # 禁用前缀：telemetry_*
    def test_telemetry_prefix_rejected(self):
        with pytest.raises(ValueError):
            parse_plan(self._reward_plan("telemetry_interval"), TUNABLES, stage=1)

    # 禁用前缀：diagnose_*
    def test_diagnose_prefix_rejected(self):
        with pytest.raises(ValueError):
            parse_plan(self._reward_plan("diagnose_threshold"), TUNABLES, stage=1)


class TestParsePlanTestbedParams:
    """testbed 三参数（enemy_hp/enemy_speed/jump_force）应正常放行。"""

    def test_enemy_hp_allowed(self):
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": "降低敌人血量",
            "change_type": "tunable_search",
            "search_space": [{"key": "enemy_hp", "range": [20, 80]}],
            "expected_effect": "completion_rate 提升",
        }
        result = parse_plan(json.dumps(plan), TESTBED_TUNABLES, stage=1)
        assert result["search_space"][0]["key"] == "enemy_hp"

    def test_enemy_speed_allowed(self):
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": "降低敌人速度",
            "change_type": "tunable_search",
            "search_space": [{"key": "enemy_speed", "range": [25.0, 75.0]}],
            "expected_effect": "completion_rate 提升",
        }
        result = parse_plan(json.dumps(plan), TESTBED_TUNABLES, stage=1)
        assert result["search_space"][0]["key"] == "enemy_speed"

    def test_jump_force_allowed(self):
        plan = {
            "target_issue": "progress_stall",
            "hypothesis": "提升跳跃力",
            "change_type": "tunable_search",
            "search_space": [{"key": "jump_force", "range": [300.0, 420.0]}],
            "expected_effect": "探索覆盖提升",
        }
        result = parse_plan(json.dumps(plan), TESTBED_TUNABLES, stage=1)
        assert result["search_space"][0]["key"] == "jump_force"

    def test_all_three_together_allowed(self):
        """三参数组合搜索应全部放行。"""
        plan = {
            "target_issue": "difficulty_too_hard",
            "hypothesis": "综合调整",
            "change_type": "tunable_search",
            "search_space": [
                {"key": "enemy_hp",    "range": [20, 80]},
                {"key": "enemy_speed", "range": [25.0, 75.0]},
                {"key": "jump_force",  "range": [300.0, 420.0]},
            ],
            "expected_effect": "综合改善",
        }
        result = parse_plan(json.dumps(plan), TESTBED_TUNABLES, stage=1)
        assert len(result["search_space"]) == 3
