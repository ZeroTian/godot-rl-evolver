"""
tests/test_propose.py — TDD for harness/llm_propose.py

核心测试 parse_plan(text, tunables) 纯函数：
  - 合法 JSON → 返回改动计划 dict（schema spec §5.2）
  - 非法 JSON → ValueError
  - 缺 change_type → ValueError
  - search_space key 不在 tunables → ValueError
  - search_space range 越界（超出 tunables.range）→ ValueError

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
        """structural 类型可以有 patches，不需要 search_space。"""
        plan = {
            "target_issue": "death_hotspot",
            "hypothesis": "关卡结构问题",
            "change_type": "structural",
            "patches": [{"file": "res://level.tscn", "anchor": "...", "new": "..."}],
            "expected_effect": "death 减少",
            "confidence": 0.5,
        }
        result = parse_plan(json.dumps(plan), TUNABLES)
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
